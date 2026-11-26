"""Review loop hosted by the existing canonical Operator process.

No signer or ledger access crosses the service boundary. Receipts come through
relay's scoped service-read lane; binding uses this Operator's existing in-process
RegistryService and transactional store. No alternate registry is opened here.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time

from . import events, github_read, hint_inbox, issue_events, messages, service, wakes
from .operator_origin_bridge import RegistryOriginBinder
from .operator_pr_origin import Held

#: Bounded hint drain per worker iteration:  a hint burst can never postpone
#: the independent poll deadline forever, and status writes stay cheap.
HINT_BATCH_LIMIT = 32

#: Outer-loop error backoff:  an unexpected cycle-level fault retries well
#: inside the poll cadence instead of instantly (nudge-hot-loop) or a full
#: interval late.
ERROR_BACKOFF_S = 30.0


def _execution_key(row: dict) -> tuple:
    """Scheduling coalescing:  one execution per target+class per batch.
    Coalescing collapses EXECUTION only - every hint's own durable inbox row
    and evidence stands regardless."""
    subject = str(row.get("subject") or "")
    kind = str(row.get("hint_kind") or "")
    repo = str(row.get("repo") or "")
    number = int(row.get("number") or 0)
    if subject == "issue":
        return ("issue", repo, number, kind, str(row.get("source_ref") or row.get("source_event_id") or ""))
    if kind == "pr_comment":
        return ("pr-comment", repo, number, str(row.get("source_ref") or ""))
    return ("pr-scan", repo, number)


def _wait_for_work(stop, nudge, deadline) -> None:
    """Sleep until the poll deadline, a nudge, or stop - whichever first.
    stop stays responsive (bounded slices), and the poll deadline is the
    poll's own:  hint bursts never move it."""
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if nudge.wait(min(remaining, 5.0)):
            return


class ProtectedReceipts:
    def __init__(self, reader, repos):
        self.reader = reader
        self.repos = tuple(repos)
        # Conservative until every repo answers with an explicit complete
        # marker:  a missing or unrecognized marker is never complete, and
        # partial coverage never stops confirmed-row processing.
        #
        # Coverage belongs to a SCAN, not to the latest single read:  a scan
        # accumulates every census read it makes (enumeration and later
        # binder lookups alike), a complete read never repairs a partial one
        # within the same scan, and a failed or malformed read marks the
        # scan partial instead of leaving a prior complete standing.  Only
        # begin_scan() opens a fresh scan.
        self._scan_partial = False
        self.coverage = "partial"

    def begin_scan(self):
        """Open a new scan:  no failures proven yet, and the coverage word
        stays partial until a fully complete census proves it."""
        self._scan_partial = False
        self.coverage = "partial"

    def rows(self):
        out = []
        try:
            partial = False
            for repo in self.repos:
                answer = self.reader._read_full(dict(repo=repo, verb="creation_receipts"))
                if not isinstance(answer, dict):
                    raise Held("receipt-census-invalid")
                if answer.get("coverage") != "complete":
                    partial = True
                rows = answer.get("body")
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise Held("receipt-census-invalid")
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or row.get("repo") != repo
                        or type(row.get("number")) is not int
                        or row.get("confirmed") is not True
                        or row.get("source") != "open_pr"
                    ):
                        raise Held("receipt-invalid")
                    out.append(row)
        except Exception:
            # A failed read never leaves an earlier complete standing for
            # the scan it happened in.
            self._scan_partial = True
            self.coverage = "partial"
            raise
        self._scan_partial = self._scan_partial or partial
        self.coverage = "partial" if self._scan_partial else "complete"
        return out

    def lookup(self, repo, number):
        rows = [r for r in self.rows() if r["repo"] == repo and r["number"] == number]
        return dict(rows[0]) if len(rows) == 1 else None


def start(env, registry, stop, log, *, operator_send=None, destination_for_origin=None):
    """Return no thread when not configured; active errors surface, never disable other wakes."""
    from .config import configured

    env = configured(env)
    if str(env.get("RA_ENABLED", "")).lower() not in ("1", "true", "yes"):
        return None
    if registry is None:
        raise Held("canonical-operator-registry-required")
    if not callable(getattr(registry, "_op_bind_pr", None)):
        raise Held("canonical-pr-binding-capability-required")
    repos = {s.strip() for s in str(env.get("RA_REPOS", "")).split(",") if s.strip()}
    seat_map = service.seat_map_from(env)
    platforms = json.loads(env.get("RA_PLATFORM_MAP_JSON") or "{}")
    state = str(env.get("RA_STATE_DIR") or "")
    if (
        not repos
        or not seat_map
        or not isinstance(platforms, dict)
        or not state
        or not os.path.isabs(state)
    ):
        raise Held("review-service-config-incomplete")
    if not callable(operator_send) or not callable(destination_for_origin):
        raise Held("review-service-message-callbacks-required")
    messenger = messages.OperatorMessages(operator_send, destination_for_origin)
    token_file = env.get("RA_SERVICE_TOKEN_FILE", "")
    wake_file = env.get("RA_WAKE_KEY_FILE", "")
    if not service._credential_from_file(token_file) or not service._credential_from_file(
        wake_file
    ):
        raise Held("review-service-credential-missing")

    class StopAwareRead(github_read.ServiceReadClient):
        def _read_full(self, payload):
            if stop.is_set():
                raise Held("operator-stopping")
            return super()._read_full(payload)

    webhook_server = None
    reader = StopAwareRead(
        env.get("RA_RELAY_URL", "http://127.0.0.1:8471"),
        lambda: service._credential_from_file(token_file),
    )
    receipts = ProtectedReceipts(reader, repos)

    def registry_call(op, payload):
        # Existing service object supplies ownership checks and the one database.
        if stop.is_set():
            raise Held("operator-stopping")
        status, answer = getattr(registry, "_op_" + op.replace("-", "_"))("OPERATOR", payload)
        if status != 200:
            raise Held("registry-operation-refused")
        return answer

    binder = RegistryOriginBinder(receipts, registry_call, seat_map, platforms)

    # The standalone-issue lane (explicit bound_issues).  Wired only when the Operator's own
    # registry service offers its in-process complete census;  without it the lane stays
    # inert (held-evidence only) and the PR lane is unchanged.  No registry route is used.
    issue_census_fn = getattr(registry, "issue_census", None)
    issue_lane = {}
    if callable(issue_census_fn):
        from . import operator_issue_census
        from .operator_issue_origin_bridge import RegistryIssueOriginBinder

        issue_census = operator_issue_census.IssueCensus(
            issue_census_fn, seat_map, platforms, stop=stop.is_set, repos=repos
        )
        issue_lane = dict(
            issue_owner_provider=issue_census.owners,
            issue_origin_binder=RegistryIssueOriginBinder(
                issue_census.sessions, seat_map, platforms
            ),
            # Fresh per read from the same protected census:  the gate seat's own live row
            # and an unbound fixed probe issue.  Nothing synthetic, nothing static.
            issue_probe_target=lambda: issue_census.probe_target(gate),
        )

    class StopAwareRouter(wakes.WakeClient):
        def submit(self, *args, **kwargs):
            if stop.is_set():
                raise Held("operator-stopping")
            return super().submit(*args, **kwargs)

    router = StopAwareRouter(
        env.get("RA_ROUTER_URL", "http://127.0.0.1:8794"),
        lambda: service._credential_from_file(wake_file),
    )
    gate = str(env.get("RA_SELFTEST_SEAT") or "")
    if gate not in platforms:
        raise Held("review-service-gate-unconfigured")
    os.makedirs(state, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(state, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        raise Held("review-service-already-running")
    try:
        runner = service.AlertService(
            events.EventStore(os.path.join(state, "events.jsonl")),
            reader,
            router,
            "",
            seat_map,
            repos,
            gate_seat=gate,
            owner_provider=receipts.rows,
            coverage_provider=lambda: receipts.coverage,
            coverage_reset=receipts.begin_scan,
            origin_binder=binder.bind,
            cancelled=stop.is_set,
            message_poster=messenger.post,
            issue_store=issue_events.IssueEventStore(os.path.join(state, "issues.jsonl")),
            **issue_lane,
        )
        interval = max(60, float(env.get("RA_INTERVAL_S", "900")))
        # Post-repair recovery selection:  explicit exact attempts or none.
        # A malformed entry is a configuration refusal, never a guess.
        recovery_selection = service._recovery_selection(env)
        # The durable hint inbox and its dedicated nudge:  ingress threads
        # enqueue ONLY, under the inbox lock;  the review-alerts thread below
        # is the sole executor of both poll and hinted (targeted) work.
        nudge = threading.Event()
        inbox = hint_inbox.HintInbox(os.path.join(state, "hints.jsonl"), nudge)
        listen = str(env.get("RA_WEBHOOK_LISTEN") or "").strip()
        webhook_secret_file = str(env.get("RA_WEBHOOK_SECRET_FILE") or "").strip()
        if listen or webhook_secret_file:
            # Both or neither:  a listener without its HMAC credential (or
            # the reverse) is a configuration refusal, never a degraded mode.
            # The secret file is a NEW service-owned credential (proposal
            # name REVIEW_ALERTS_GITHUB_WEBHOOK_SECRET), never an Operator or
            # wake key, delivered by the loader, never in config values.
            if not listen or not webhook_secret_file:
                raise Held("review-webhook-config-incomplete")
            if not service._credential_from_file(webhook_secret_file):
                raise Held("review-webhook-secret-missing")
            try:
                from . import webhook_intake
            except ImportError:
                # The pure Wise module is integrated only after its exact
                # head clears review;  without it the listener cannot exist.
                raise Held("review-webhook-intake-module-missing") from None
            from . import webhook_listener

            webhook_server = webhook_listener.serve(
                listen,
                lambda: service._credential_from_file(webhook_secret_file),
                verify=webhook_intake.verify,
                map_event=webhook_intake.map_event,
                allowlist=frozenset(repos),
                inbox=inbox,
                log=log,
            )
    except ValueError as exc:
        if webhook_server is not None:
            webhook_server.shutdown()
            webhook_server.server_close()
        os.close(lock_fd)
        raise Held("review-service-recovery-selection-invalid") from exc
    except Exception:
        if webhook_server is not None:
            webhook_server.shutdown()
            webhook_server.server_close()
        os.close(lock_fd)
        raise

    def _settle_row(row: dict, result: dict | None, exc_name: str | None) -> tuple:
        """Route ONE executed hint to its durable next state.  A transient
        failure (a RetryHint or any per-row exception) requeues with
        bounded persisted backoff, never consumes;  a reconcile-declared
        hold becomes a durable visible held row;  a cancelled reconcile
        leaves the row queued for the next start.  Returns (disposition,
        reason) so coalesced siblings inherit the EXACT held reason of
        their executed row - a generic reason would never match the
        ownership-change requeue prefixes."""
        if exc_name is not None:
            return inbox.retry(row, exc_name), exc_name
        outcome = str((result or {}).get("outcome") or "")
        if outcome == "cancelled":
            return "queued", ""
        if (result or {}).get("disposition") == "held":
            reason = outcome[5:] if outcome.startswith("held:") else (outcome or "held")
            inbox.hold_row(row, reason)
            return "held", reason
        return "consumed", ""

    def loop():
        qualified = False
        # The first cycle runs immediately, as before;  the poll deadline
        # then advances on its own schedule, independent of hint traffic.
        next_poll = time.monotonic()
        try:
            while not stop.is_set():
                try:
                    if not qualified:
                        router.check_required_session_support(gate, probe_id=str(time.time_ns()))
                        qualified = True
                        if recovery_selection:
                            # Post-repair recovery:  one explicitly
                            # selected, bounded pass at process start,
                            # after the capability gate, before the first
                            # cycle, under the single-writer lock.  No
                            # selection, no pass.
                            log(
                                "review-alert-recovery",
                                **runner.recover_held(recovery_selection),
                            )
                    if stop.is_set():
                        break
                    batch = inbox.drain(HINT_BATCH_LIMIT)
                    if batch:
                        outcomes: dict = {}
                        consumed = []
                        for row in batch:
                            if stop.is_set():
                                break
                            key = _execution_key(row)
                            if key in outcomes:
                                # Coalesced:  shares its executed sibling's
                                # disposition AND its exact reason (a
                                # coalesced owner-unknown/bind hold must
                                # carry the real reason, or it would never
                                # match requeue_held when ownership
                                # appears).  Coalescing never consumes a
                                # row its target's failure should keep.
                                sibling_outcome, sibling_reason = outcomes[key]
                                if sibling_outcome == "consumed":
                                    consumed.append(row)
                                elif sibling_outcome == "held":
                                    inbox.hold_row(row, sibling_reason)
                                else:
                                    inbox.retry(row, sibling_reason or "coalesced")
                                continue
                            try:
                                result = runner.reconcile_hint(row)
                            except service.RetryHint as exc:
                                result, exc_name = None, (str(exc.args[0]) if exc.args else "retry")
                            except Exception as exc:  # noqa: BLE001 - per-row isolation:  a poison
                                # hint backs off;  it never blocks later targets or the poll.
                                result, exc_name = None, type(exc).__name__
                            else:
                                exc_name = None
                            if result is not None:
                                log("review-alert-hint", **result)
                            else:
                                log(
                                    "review-alert-hint",
                                    hint=str(row.get("hint_kind") or ""),
                                    outcome="retry:%s" % exc_name,
                                )
                            outcomes[key] = _settle_row(row, result, exc_name)
                            if outcomes[key][0] == "consumed":
                                consumed.append(row)
                        inbox.consume(consumed)
                        service._write_status(
                            state, dict(runner.status_snapshot(), hints=inbox.stats())
                        )
                    if stop.is_set():
                        break
                    if time.monotonic() >= next_poll:
                        counts = runner.cycle()
                        next_poll = time.monotonic() + interval
                        # Retry-on-ownership-change:  a poll cycle that now
                        # sees an owner revives its durably held
                        # owner-unknown/bind hints for one fresh reconcile.
                        revived = inbox.requeue_held(runner.owned_targets())
                        if revived:
                            log("review-alert-hints-revived", count=revived)
                        service._write_status(
                            state, dict(runner.status_snapshot(), hints=inbox.stats())
                        )
                        log("review-alert-cycle", **counts)
                except Exception as exc:
                    log("review-alert-error", reason=type(exc).__name__)
                    # Error backoff is a REAL positive wait on stop alone:
                    # independent of the nudge (a queued hint must not
                    # hot-loop a failing cycle) and of the poll deadline
                    # (an already-due poll must not zero the backoff).
                    # stop.wait returns immediately on shutdown.
                    stop.wait(ERROR_BACKOFF_S)
                    continue
                # Clear the nudge only when no READY work remains, under
                # the inbox lock (no lost wakeup);  rows still backing off
                # are covered by the bounded next_wake deadline below, and
                # the poll deadline stays independent of hint traffic.
                # Inbox deadlines are WALL time (persisted);  the wait
                # deadline is monotonic - convert, never mix.
                if inbox.mark_idle_if_empty():
                    deadline = next_poll
                    wake = inbox.next_wake()
                    if wake is not None:
                        deadline = min(
                            deadline, time.monotonic() + max(0.0, wake - time.time())
                        )
                    _wait_for_work(stop, nudge, deadline)
        finally:
            # Stop accepting BEFORE releasing the singleton lock:  a
            # listener that outlives its worker would accept hints no
            # executor drains, and could outlive the flock protection.
            if webhook_server is not None:
                webhook_server.shutdown()
                webhook_server.server_close()
            os.close(lock_fd)

    thread = threading.Thread(target=loop, name="review-alerts", daemon=True)
    try:
        thread.start()
    except Exception:
        if webhook_server is not None:
            webhook_server.shutdown()
            webhook_server.server_close()
        os.close(lock_fd)
        raise
    return thread
