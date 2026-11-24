"""The alert service:  one poll cycle and the daemon loop.

Per cycle, per confirmed ownership row on the allowlist:  read the PR, its
reviews, its inline comments and its head's check runs through the relay's
service_read lane;  seed a first-sight baseline instead of alerting on
history;  derive events;  submit one wake per new event to the ATTESTED
origin session;  then settle pending receipts.  Every event lands in the
store exactly once;  anything that cannot be delivered is held with a named
reason and counted, never dropped.

Only claimed-ownership rows drive alerts:  session, letter AND generation
claims come from the open_pr caller;  a row missing any of them is held
("no-origin-claim"), never delivered.  The claims are untrusted hints:  the
wake-router checks all three against the live registry row, and require_pr
pins the wake to the session's own bound_prs entry, before delivering.

Configuration (environment;  no secrets in argv or the environment itself):
  RA_OWNERS_PATH         the relay pr_owners JSONL (read-only)
  RA_STATE_DIR           this service's own state folder (event store, lock,
                         status.json)
  RA_RELAY_URL           default http://127.0.0.1:8471
  RA_SERVICE_TOKEN_FILE  this service's dedicated relay service_read bearer
  RA_ROUTER_URL          default http://127.0.0.1:8794
  RA_WAKE_KEY_FILE       this service's dedicated wake-router credential
  RA_REPOS               comma allowlist of qualified repos (REQUIRED)
  RA_SEAT_MAP_JSON       relay slug -> router seat mapping (explicit; no default)
  RA_SELFTEST_SEAT       router seat with at least one live chat, for the
                         startup require-session support probe (required)
  RA_INTERVAL_S          poll cadence, default 900
"""

from __future__ import annotations

import calendar
import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime

from . import detect, events, github_read, messages, wakes


# Relay seat slug -> wake-router seat tag.  There is NO default and NO
# derivation:  the mapping is deploy-time config (RA_SEAT_MAP_JSON), because a
# derived tag is a guess about identity, and an unmapped seat is held, never
# guessed.
def seat_map_from(env) -> dict:
    raw = str(env.get("RA_SEAT_MAP_JSON") or "").strip()
    if not raw:
        return {}
    table = json.loads(raw)
    if not isinstance(table, dict):
        raise ValueError("RA_SEAT_MAP_JSON must be a JSON object")
    return {str(k): str(v).upper() for k, v in table.items()}


def load_owner_rows(path: str) -> list[dict]:
    rows: dict[tuple, dict] = {}
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "repo" in row and "number" in row:
                rows[(row["repo"], int(row["number"]))] = row
    return [r for r in rows.values() if r.get("confirmed")]


def _instant(text) -> datetime | None:
    """Parse an ISO-8601 timestamp as an INSTANT, not a string:  trailing Z,
    fractional seconds and numeric offsets all normalise to an aware UTC
    datetime.  Returns None when missing or unparseable, so callers can pick
    a conservative fallback instead of comparing lexical strings (where
    '2026-01-01T00:00:00+05:30' sorts against '2025-12-31T23:59:59Z')."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A naive timestamp is ambiguous:  treat it as UTC rather than guess
        # a local zone, and never mix naive with aware in comparisons.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def seat_for(slug: str, seat_map: dict) -> str:
    return seat_map.get(slug, "")


def _credential_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


#: A wake whose receipt cannot be read at all (404 past the router's own
#: retention, or unreachable through every retry) is bounded:  after this
#: many seconds unsettled the event is held "receipt-lost" so it surfaces
#: instead of pending forever.
RECEIPT_DEADLINE_S = 3600
#: Upper bound on re-alert epochs for recoverable holds (not-live /
#: ambiguous):  each epoch mints a fresh wake id only after the prior wake
#: record is certainly undelivered.
MAX_EPOCHS = 5

#: Reasons the bounded recovery pass may revisit:  the delivery-time store
#: miss is the one hold that accumulates terminal rows below the epoch cap
#: (it was never redrive-eligible, so such rows exist).  The pick-time
#: holds in wakes.REDRIVE_REASONS are already redriven while pending by
#: every settle pass;  a below-cap terminal row carrying one of them is a
#: state current code cannot produce, and recovering it is outside this
#: gap.  Membership here is only context for which rows a selection may
#: name:  it never substitutes for the row's authoritative delivered:false
#: receipt, which the pass reads live.  REDRIVE_REASONS itself is NOT
#: expanded:  the settle path's reasons are unchanged.
RECOVERY_REASONS = ("session-not-found",)

#: The marker a recovery next-attempt carries on its ledger row in the
#: dedicated `recovery` field:  set when the pass persists the selected
#: attempt and NEVER cleared by any status transition, so reconciliation
#: can keep the attempt bounded to its selection in every state.  A marked
#: row whose receipt is missing (or expired past retention) is NEVER
#: resubmitted - after an uncertain acceptance the absence of a record
#: cannot prove the wake did not run, and same-id dedupe is not
#: permanent.  It fails closed instead.
RECOVERY_ATTEMPT = "recovery-attempt"

#: Upper bound on redispatches in ONE recovery pass, whatever the explicit
#: selection names:  recovery is a bounded post-repair act, not a scan.
MAX_RECOVERY_SENDS = 8

#: Selector fields, exactly:  the event, the prior wake that must still be
#: its ledger row, the epoch that row must sit at, and the posted Operator
#: message the redispatch will reference.
SELECTION_FIELDS = ("repo", "number", "kind", "event_id", "wake_id", "epoch", "message_id")


def _recovery_selection(env) -> list:
    """The explicit recovery selection (RA_RECOVER_HELD):  a JSON array of
    objects, one per exact stored attempt::

        [{"repo": "owner/name", "number": 2196, "kind": "review_completed",
          "event_id": "51", "wake_id": "ra-...", "epoch": 0,
          "message_id": 91}]

    Structured JSON because event ids legitimately contain separators
    (merge_conflict ids are head:base commit pairs) - a delimited string
    cannot carry them.  Empty, unset, "[]", "0", "false" or "no" means
    recovery is OFF:  there is no default-on startup scan.  A malformed
    document or entry is a configuration refusal, never a guess.  Kinds
    validate against events.KINDS as adopted NOW.
    """
    raw = str(env.get("RA_RECOVER_HELD") or "").strip()
    if not raw or raw.lower() in ("0", "false", "no"):
        return []
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError("RA_RECOVER_HELD is not a JSON array of selectors: %s" % exc)
    if not isinstance(doc, list):
        raise ValueError("RA_RECOVER_HELD must be a JSON array of selectors")
    selected = []
    for item in doc:
        if not isinstance(item, dict) or set(item) != set(SELECTION_FIELDS):
            raise ValueError(
                "RA_RECOVER_HELD entry must have exactly the fields %s: %r"
                % (", ".join(SELECTION_FIELDS), item)
            )
        repo = item["repo"]
        number = item["number"]
        kind = item["kind"]
        event_id = item["event_id"]
        wake_id = item["wake_id"]
        epoch = item["epoch"]
        message_id = item["message_id"]
        ok = (
            type(repo) is str
            and type(number) is int
            and wakes.PR_RE.fullmatch("%s#%d" % (repo, number)) is not None
            and kind in events.KINDS
            and type(event_id) is str
            and bool(event_id)
            and type(wake_id) is str
            and wakes.WAKE_ID_RE.fullmatch(wake_id) is not None
            and type(epoch) is int
            and 0 <= epoch
            and epoch + 1 < MAX_EPOCHS
            and type(message_id) is int
            and message_id > 0
        )
        if not ok:
            raise ValueError("RA_RECOVER_HELD entry is not a valid selector: %r" % (item,))
        selected.append((repo, number, kind, event_id, wake_id, epoch, message_id))
    return selected


class AlertService:
    def __init__(
        self,
        store: events.EventStore,
        gh,
        router: wakes.WakeClient,
        owners_path: str,
        seat_map: dict,
        repos: set[str],
        clock=time.time,
        *,
        gate_seat: str,
        owner_provider=None,
        origin_binder=None,
        cancelled=None,
        message_poster=None,
    ):
        if not repos:
            raise ValueError("an explicit repo allowlist is required (no fleet-wide default)")
        if not gate_seat:
            # The per-cycle require_pr probe is mandatory:  without a gate
            # seat the service cannot detect a router rollback before
            # dispatching, so there is no supported silent no-gate mode.
            raise ValueError("a gate seat is required for the per-cycle capability probe")
        self.cancelled = cancelled or (lambda: False)
        self.message_poster = message_poster
        self.binding_holds = {}
        self.bound_this_cycle = set()
        self.origins_this_cycle = {}
        self.owner_provider = owner_provider
        self.origin_binder = origin_binder
        self.store = store
        self.gh = gh
        self.router = router
        self.owners_path = owners_path
        self.seat_map = dict(seat_map)
        self.repos = set(repos)
        self.clock = clock
        # When set, every cycle re-probes the router's require_pr validation
        # before any wake goes out:  a rollback between cycles holds the
        # cycle (nothing dispatches, no cursor advances) instead of sending
        # wakes whose PR binding would go unenforced.
        self.gate_seat = gate_seat

    def cycle(self) -> dict:
        counts = {
            "prs": 0,
            "seeded": 0,
            "events": 0,
            "submitted": 0,
            "delivered": 0,
            "held": 0,
            "unknown": 0,
            "read_errors": 0,
            "redriven": 0,
        }
        if self.gate_seat:
            self.router.check_require_pr_support(self.gate_seat, probe_id=str(int(self.clock())))
        self.bound_this_cycle = set()
        self.origins_this_cycle = {}
        self.binding_holds = {}
        owners = self.owner_provider() if self.owner_provider else load_owner_rows(self.owners_path)
        for row in owners:
            repo, number = row["repo"], int(row["number"])
            if repo not in self.repos:
                continue
            counts["prs"] += 1
            if self.origin_binder is not None:
                try:
                    if self.cancelled():
                        continue
                    origin = self.origin_binder(repo, number)
                    self.origins_this_cycle[(repo, number)] = origin
                    self.bound_this_cycle.add((repo, number))
                except Exception as exc:
                    if len(self.binding_holds) < 1000:
                        self.binding_holds[repo + "#" + str(number)] = type(exc).__name__
                    # No cursor advances while binding is unavailable. A later
                    # cycle resumes from the original event watermark.
                    counts["held"] += 1
                    continue
            try:
                pr = self.gh.pull(repo, number)
                if str(pr.get("state") or "") != "open":
                    continue
                current_head = str((pr.get("head") or {}).get("sha") or "")
                cursor = self.store.cursor(repo, number)
                reviews = self.gh.reviews(repo, number)
                comments = self.gh.review_comments(repo, number)
                check_runs = self.gh.check_runs(repo, current_head) if current_head else []
            except github_read.GithubReadError:
                # A failed or uncertain read advances no cursor and creates no
                # event:  next cycle retries from the same watermark.
                counts["read_errors"] += 1
                continue
            if not self.store.has_seed(repo, number):
                # First sight:  baseline only what predates the PR's
                # REGISTRATION row (the relay's creation record).  Activity
                # landing between registration and this first poll is after
                # the baseline and alerts normally - first sight is not a
                # silent discard.
                # A row with no registration time baselines EVERYTHING
                # (conservative:  no replay), never discards silently.
                owner_instant = _instant(row.get("ts"))
                if owner_instant is None:
                    # Registration time missing or unparseable:  baseline
                    # EVERYTHING (no replay), same as the old "9999" sentinel.
                    predates = lambda ts, _missing: True
                else:

                    def predates(ts, missing_seeded):
                        item = _instant(ts)
                        if item is None:
                            # Unparseable item time keeps the old per-type
                            # posture:  reviews/comments suppressed, checks
                            # still alerted (the old "" vs "9" sentinels).
                            return missing_seeded
                        return item <= owner_instant

                max_review = max(
                    [
                        int(r["id"])
                        for r in reviews
                        if isinstance(r.get("id"), int) and predates(r.get("submitted_at"), True)
                    ]
                    or [0]
                )
                max_comment = max(
                    [
                        int(c["id"])
                        for c in comments
                        if isinstance(c.get("id"), int) and predates(c.get("created_at"), True)
                    ]
                    or [0]
                )
                max_check = max(
                    [
                        int(r["id"])
                        for r in check_runs
                        if isinstance(r.get("id"), int) and predates(r.get("completed_at"), False)
                    ]
                    or [0]
                )
                self.store.seed(repo, number, max_review, max_comment, max_check)
                counts["seeded"] += 1
            found = detect.derive(
                repo,
                number,
                current_head,
                reviews,
                comments,
                check_runs,
                self.store.cursor(repo, number),
            )
            found.extend(detect.merge_conflict(repo, number, pr))
            link = str(pr.get("html_url") or "")
            for event in found:
                if self.store.get(repo, number, event["kind"], event["event_id"]):
                    continue
                counts["events"] += 1
                self._dispatch(row, event, link, counts)
        self._settle(counts, owners)
        return counts

    def _dispatch(self, owner: dict, event: dict, link: str, counts: dict, epoch: int = 0) -> None:
        repo, number = event["repo"], event["number"]
        if not self._effects_allowed(event["repo"], event["number"]):
            return
        session = str(owner.get("origin_session_claim") or "")
        letter = str(owner.get("origin_letter_claim") or "")
        generation = owner.get("origin_generation_claim")
        seat = seat_for(str(owner.get("seat") or ""), self.seat_map)
        head_note = "" if event["head_matches"] else " (against an older head)"
        topic = "%s#%s: %s%s" % (repo, number, event["summary"], head_note)
        base = dict(
            event,
            epoch=epoch,
            status="held",
            wake_id=None,
            reason=None,
            message_id=event.get("message_id"),
            link=link or event.get("link") or "",
        )
        if not session or not letter or type(generation) is not int or generation < 1:
            # Without all three claims there is nothing for the router to
            # check against the live row:  hold visibly, never guess.
            base["reason"] = "no-origin-claim"
        elif not seat:
            base["reason"] = "no-seat-mapping"
        else:
            if type(base.get("message_id")) is not int or base["message_id"] <= 0:
                if self.message_poster is None:
                    base["reason"] = "operator-message-unconfigured"
                    self.store.append(base)
                    counts["held"] += 1
                    return
                base.update(status="message-pending")
                self.store.append(base)
                self._post_message(owner, base, counts)
                return
            wake_id = wakes.wake_id_for(repo, number, event["kind"], event["event_id"], epoch)
            try:
                self.router.submit(
                    seat,
                    wake_id,
                    session,
                    letter,
                    generation,
                    "%s#%s" % (repo, number),
                    topic,
                    link,
                    message_id=base["message_id"],
                )
            except wakes.WakeRetryable:
                # The wake may or may not have landed:  reconcile by receipt
                # next cycle (same wake id), never a new one.
                base.update(status="submit-pending", wake_id=wake_id, reason=None)
            except wakes.WakeError as exc:
                base["reason"] = "wake-submit: %s" % exc
            else:
                base.update(status="submitted", wake_id=wake_id, reason=None)
                counts["submitted"] += 1
        if base["status"] == "held":
            counts["held"] += 1
        self.store.append(base)

    def _settle(self, counts: dict, owners: list) -> None:
        now = self.clock()
        for pending in self.store.pending():
            row = self.store.get(
                pending["repo"], pending["number"], pending["kind"], pending["event_id"]
            )
            if row is None or (
                not row.get("wake_id") and row.get("status") not in events.MESSAGE_PENDING
            ):
                continue
            owner = self._owner_row(owners, pending["repo"], pending["number"])
            # A recovery attempt's receipt is always read from the seat the
            # wake was actually submitted to (frozen on its row):  owner
            # rotation or disappearance never reroutes the lookup, and a
            # gone owner still permits safe frozen reconciliation.
            owner_seat = str(row.get("target_seat") or "") or seat_for(
                str((owner or {}).get("seat") or ""), self.seat_map
            )
            if not owner_seat:
                continue
            age = now - self._row_epoch_seconds(row)
            if row["status"] in events.MESSAGE_PENDING:
                if row["status"] == "message-posting":
                    # A crash after durable intent can have happened after send.
                    self._finish(row, "held", "operator-message-outcome-unknown")
                    counts["held"] += 1
                elif self._effects_allowed(row["repo"], row["number"]):
                    if row["status"] == "message-pending":
                        self._post_message(owner, row, counts)
                    else:
                        self._dispatch(
                            owner or {},
                            row,
                            row.get("link") or "",
                            counts,
                            epoch=int(row.get("epoch") or 0),
                        )
                continue
            if type(row.get("message_id")) is not int or row["message_id"] <= 0:
                self._finish(row, "held", "legacy-wake-without-message")
                counts["held"] += 1
                continue
            try:
                receipt = self.router.receipt(owner_seat, row["wake_id"])
            except wakes.WakeRetryable:
                self._maybe_expire(row, age, counts)
                continue
            except wakes.WakeError:
                continue
            if receipt is None:
                # 404:  the router has no record.  For a submit-pending row
                # the submit was lost:  resubmit the SAME wake id (idempotent
                # if it did land).  For a submitted row the router's record
                # is gone past retention:  bounded by the deadline.
                if row["status"] == "submit-pending":
                    if row.get("recovery") == RECOVERY_ATTEMPT:
                        # A recovery's next attempt is never resubmitted
                        # blind:  after an uncertain acceptance a missing
                        # (expired or removed) record cannot prove the wake
                        # did not run, and same-id dedupe is not permanent.
                        # Fail closed;  only a fresh explicit selection
                        # against proven evidence may retry, next epoch.
                        self._finish(row, "held", "recovery-receipt-uncertain")
                        counts["held"] += 1
                    else:
                        self._resubmit(row, owner, counts)
                else:
                    self._maybe_expire(row, age, counts)
                continue
            terminal, outcome = self.router.receipt_terminal(receipt)
            if not terminal:
                self._maybe_expire(row, age, counts)
                continue
            if outcome == "delivered":
                self._finish(row, "delivered", None)
                counts["delivered"] += 1
            elif outcome == "unknown":
                self._finish(row, "unknown", "transport-unproven")
                counts["unknown"] += 1
            else:
                reason = outcome[5:]
                if (
                    reason in wakes.REDRIVE_REASONS
                    and int(row.get("epoch") or 0) + 1 < MAX_EPOCHS
                    # A recovery attempt (its marker rides the row through
                    # every status) is bounded to the explicitly selected
                    # attempt in every reconciliation state:  no automatic
                    # fresh epoch, and never a target change from rotated
                    # current claims.  It settles held with the receipt's
                    # reason and waits for a fresh explicit selection.
                    and row.get("recovery") != RECOVERY_ATTEMPT
                ):
                    if not self._effects_allowed(row["repo"], row["number"]):
                        continue
                    # The wake record is certainly undelivered;  a fresh wake
                    # id (next epoch) may retry while the origin chat is away.
                    event = {
                        k: row.get(k)
                        for k in (
                            "repo",
                            "number",
                            "kind",
                            "event_id",
                            "actor",
                            "head",
                            "current_head",
                            "head_matches",
                            "summary",
                            "message_id",
                            "link",
                        )
                    }
                    self._dispatch(
                        owner or {}, event, "", counts, epoch=int(row.get("epoch") or 0) + 1
                    )
                    counts["redriven"] += 1
                else:
                    self._finish(row, "held", reason)
                    counts["held"] += 1

    def recover_held(self, selected: list) -> dict:
        """One bounded post-repair pass over terminal-held events, only for
        the exact attempts the selection names.  Requires the protected
        owner provider and the live origin binder:  without fresh binding
        there is no protected identity to act on, so the pass refuses
        outright rather than skipping the check.

        A held row is redispatched only when ALL of these hold:
          - the selection names THIS exact event (repo, number, kind,
            event_id) AND the row still carries the selection's expected
            prior wake id, epoch and posted message id - the ledger row is
            the original envelope of what was submitted, and a row that
            moved on is never recovered on guesswork;
          - the pass has not reached MAX_RECOVERY_SENDS;
          - the hold reason is in RECOVERY_REASONS (the store-miss gap
            this pass exists for), and the next epoch stays below
            MAX_EPOCHS;
          - the AUTHORITATIVE prior receipt, fetched now from the router
            by the row's exact wake id, exists and is readable (an absent
            or expired-past-retention record, a read failure, or an
            unknown or delivered outcome all fail closed), proves explicit
            delivered IS false with the same failure code, and its pick
            identity (seat, session, letter, generation) matches what the
            redispatch would use NOW from the current owner claims.  The
            receipt carries no require_pr or message_id, so PR and message
            provenance come from the validated ledger row alone;  the
            receipt proves outcome and chat target, nothing more;
          - the origin still binds NOW (fresh resolve + bind-pr against
            the live registry, exactly as a cycle does) and effects are
            allowed.
        The exact next attempt (deterministic wake id at the next epoch,
        same posted message id) is persisted as submit-pending BEFORE the
        router is called, marked RECOVERY_ATTEMPT with its dispatch target
        frozen on the row (target_seat/session/letter/generation - current
        owner claims may rotate later;  provenance never does).  Ordinary
        reconciliation then owns it:  a receipt settles it as usual, but a
        MISSING receipt is never answered with a blind resubmit of the
        same id - after an uncertain acceptance that can re-execute a wake
        that already ran - and fails closed to held
        recovery-receipt-uncertain, retriable only by a fresh explicit
        selection against proven evidence.  The settle pass's own
        delivered-is-False semantics are untouched, and REDRIVE_REASONS is
        not expanded.  Append-only:  no ledger row is rewritten."""
        if self.owner_provider is None or self.origin_binder is None:
            raise ValueError(
                "held recovery requires the protected owner provider"
                " and the live origin binder"
            )
        counts = {
            "candidates": 0,
            "recovered": 0,
            "skipped": 0,
            "mismatched": 0,
            "unmatched": 0,
            "submitted": 0,
            "submit-pending": 0,
            "held": 0,
        }
        owners = self.owner_provider()
        wanted = {(e[0], e[1], e[2], e[3]): e for e in selected}
        seen = set()
        rows = sorted(
            self.store.held(),
            key=lambda r: (r["repo"], int(r["number"]), r["kind"], str(r["event_id"])),
        )
        for row in rows:
            repo, number = row["repo"], int(row["number"])
            key = (repo, number, row["kind"], str(row.get("event_id")))
            expect = wanted.get(key)
            if expect is None:
                continue
            seen.add(key)
            reason = str(row.get("reason") or "")
            old_wake_id = str(row.get("wake_id") or "")
            epoch = int(row.get("epoch") or 0)
            message_id = row.get("message_id")
            if (
                repo not in self.repos
                or reason not in RECOVERY_REASONS
                or old_wake_id != expect[4]
                or epoch != expect[5]
                or epoch + 1 >= MAX_EPOCHS
                or message_id != expect[6]
                or type(message_id) is not int
                or message_id <= 0
            ):
                # The ledger no longer shows the exact attempt the
                # selection names:  refuse rather than recover a different
                # attempt on guesswork.
                counts["mismatched"] += 1
                continue
            if counts["recovered"] >= MAX_RECOVERY_SENDS:
                break
            counts["candidates"] += 1
            owner = self._owner_row(owners, repo, number)
            if owner is None:
                counts["skipped"] += 1
                continue
            owner_seat = seat_for(str(owner.get("seat") or ""), self.seat_map)
            if not owner_seat:
                counts["skipped"] += 1
                continue
            # The authoritative outcome evidence:  the router's own record
            # of THIS wake, read now.  Only an explicit delivered:false
            # with the same code proves certain-undelivered;  a missing,
            # expired, unreadable, unknown or delivered record fails
            # closed.
            try:
                receipt = self.router.receipt(owner_seat, old_wake_id)
            except wakes.WakeError:
                counts["skipped"] += 1
                continue
            if not isinstance(receipt, dict):
                counts["skipped"] += 1
                continue
            session = str(owner.get("origin_session_claim") or "")
            letter = str(owner.get("origin_letter_claim") or "")
            generation = owner.get("origin_generation_claim")
            if (
                receipt.get("delivered") is not False
                or str(receipt.get("wake_id") or "") != old_wake_id
                or str(receipt.get("code") or receipt.get("reason") or "") != reason
                or str(receipt.get("seat") or "") != owner_seat
                or not session
                or not letter
                or type(generation) is not int
                or generation < 1
                or str(receipt.get("session_id") or "") != session
                or str(receipt.get("picked_letter") or "") != letter
                or receipt.get("picked_generation") != generation
            ):
                counts["skipped"] += 1
                continue
            try:
                if self.cancelled():
                    counts["skipped"] += 1
                    continue
                origin = self.origin_binder(repo, number)
                self.origins_this_cycle[(repo, number)] = origin
                self.bound_this_cycle.add((repo, number))
            except Exception:  # noqa: BLE001 - a bind failure holds, never guesses
                counts["skipped"] += 1
                continue
            if not self._effects_allowed(repo, number):
                counts["skipped"] += 1
                continue
            # Persist the exact next attempt BEFORE crossing the router,
            # marked, with its dispatch target frozen on the row.
            next_epoch = epoch + 1
            next_wake = wakes.wake_id_for(
                repo, number, row["kind"], str(row.get("event_id")), next_epoch
            )
            intent = {
                k: row.get(k)
                for k in (
                    "repo",
                    "number",
                    "kind",
                    "event_id",
                    "actor",
                    "head",
                    "current_head",
                    "head_matches",
                    "summary",
                )
            }
            intent.update(
                ts=None,
                epoch=next_epoch,
                status="submit-pending",
                wake_id=next_wake,
                reason=None,
                recovery=RECOVERY_ATTEMPT,
                message_id=message_id,
                link=row.get("link") or "",
                target_seat=owner_seat,
                target_session=session,
                target_letter=letter,
                target_generation=generation,
            )
            self.store.append(intent)
            head_note = "" if row.get("head_matches") else " (against an older head)"
            topic = "%s#%s: %s%s" % (repo, number, row.get("summary") or "", head_note)
            try:
                self.router.submit(
                    owner_seat,
                    next_wake,
                    session,
                    letter,
                    generation,
                    "%s#%s" % (repo, number),
                    topic,
                    row.get("link") or "",
                    message_id=message_id,
                )
            except wakes.WakeRetryable:
                # Unsettled:  the marked intent row stands;  ordinary
                # reconciliation reads the receipt and never blind-
                # resubmits this attempt.
                counts["submit-pending"] += 1
            except wakes.WakeError as exc:
                self._finish(intent, "held", "wake-submit: %s" % exc)
                counts["held"] += 1
            else:
                self._finish(intent, "submitted", None)
                counts["submitted"] += 1
            counts["recovered"] += 1
        counts["unmatched"] = len(wanted) - len(seen)
        return counts

    def _post_message(self, owner: dict, row: dict, counts: dict) -> None:
        if not self._effects_allowed(row["repo"], row["number"]):
            return
        if self.message_poster is None:
            self._finish(row, "held", "operator-message-unconfigured")
            counts["held"] += 1
            return
        origin = self.origins_this_cycle.get((row["repo"], row["number"]))
        if not isinstance(origin, dict) or not origin:
            self._finish(row, "held", "no-origin-claim")
            counts["held"] += 1
            return
        # Persist intent before crossing the existing Operator post boundary.
        posting = dict(row, status="message-posting", reason=None)
        self.store.append(posting)
        try:
            posting_owner = dict(
                owner or {},
                _review_origin=origin,
            )
            message_id = self.message_poster(posting_owner, row, row.get("link") or "")
            if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
                raise ValueError("invalid-message-id")
        except messages.MessageDestinationUnresolved:
            self._finish(posting, "message-pending", "operator-message-destination-unresolved")
            return
        except messages.MessageRetryable:
            self._finish(posting, "message-pending", "operator-message-unavailable")
            return
        except Exception:
            self._finish(posting, "held", "operator-message-outcome-unknown")
            counts["held"] += 1
            return
        posted = dict(posting, status="message-posted", message_id=message_id)
        self.store.append(posted)
        self._dispatch(
            owner or {},
            posted,
            posted.get("link") or "",
            counts,
            epoch=int(posted.get("epoch") or 0),
        )

    def _effects_allowed(self, repo, number):
        return not self.cancelled() and (
            self.origin_binder is None or (repo, number) in self.bound_this_cycle
        )

    def _resubmit(self, row: dict, owner: dict | None, counts: dict) -> None:
        """A submit-pending wake with no router record:  resubmit the SAME
        wake id.  If the original did land, the router answers its existing
        record (idempotent), so no duplicate delivery results."""
        if owner is None or not self._effects_allowed(row["repo"], row["number"]):
            return
        session = str(owner.get("origin_session_claim") or "")
        letter = str(owner.get("origin_letter_claim") or "")
        generation = owner.get("origin_generation_claim")
        if not session or not letter or type(generation) is not int or generation < 1:
            self._finish(row, "held", "no-origin-claim")
            return
        head_note = "" if row.get("head_matches") else " (against an older head)"
        topic = "%s#%s: %s%s" % (row["repo"], row["number"], row.get("summary") or "", head_note)
        try:
            self.router.submit(
                seat_for(str(owner.get("seat") or ""), self.seat_map),
                row["wake_id"],
                session,
                letter,
                generation,
                "%s#%s" % (row["repo"], row["number"]),
                topic,
                row.get("link") or "",
                message_id=row["message_id"],
            )
        except wakes.WakeRetryable:
            return  # still unsettled;  next cycle reconciles
        except wakes.WakeError as exc:
            self._finish(row, "held", "wake-submit: %s" % exc)
            return
        self._finish(row, "submitted", None)

    def _maybe_expire(self, row: dict, age: float, counts: dict) -> None:
        if age > RECEIPT_DEADLINE_S:
            self._finish(row, "held", "receipt-lost")
            counts["held"] += 1

    def _finish(self, row: dict, status: str, reason) -> None:
        self.store.append(dict(row, status=status, reason=reason))

    @staticmethod
    def _row_epoch_seconds(row: dict) -> float:
        try:
            return calendar.timegm(time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, OverflowError):
            return time.time()

    @staticmethod
    def _owner_row(owners: list, repo: str, number: int) -> dict | None:
        for row in owners:
            if row["repo"] == repo and int(row["number"]) == int(number):
                return row
        return None

    def status_snapshot(self) -> dict:
        """The operationally visible surface:  counts, every held event with
        its reason, and every pending event.  Written to status.json each
        cycle so a stuck alert is found here, not by reading the log."""
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": self.store.counts(),
            "binding_holds": dict(self.binding_holds),
            "held": [
                {
                    k: r.get(k)
                    for k in (
                        "repo",
                        "number",
                        "kind",
                        "event_id",
                        "epoch",
                        "reason",
                        "ts",
                        "message_id",
                    )
                }
                for r in self.store.held()
            ],
            "pending": [
                {
                    k: r.get(k)
                    for k in ("repo", "number", "kind", "event_id", "epoch", "status", "ts")
                }
                for r in self.store.pending()
            ],
        }


def _write_status(state_dir: str, snapshot: dict) -> None:
    tmp = os.path.join(state_dir, ".status.json.tmp")
    final = os.path.join(state_dir, "status.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ
    owners_path = str(env.get("RA_OWNERS_PATH") or "").strip()
    state_dir = str(env.get("RA_STATE_DIR") or "").strip()
    selftest_seat = str(env.get("RA_SELFTEST_SEAT") or "").strip()
    token_file = str(env.get("RA_SERVICE_TOKEN_FILE") or "").strip()
    wake_key_file = str(env.get("RA_WAKE_KEY_FILE") or "").strip()
    missing = [
        name
        for name, value in (
            ("RA_OWNERS_PATH", owners_path),
            ("RA_STATE_DIR", state_dir),
            ("RA_SELFTEST_SEAT", selftest_seat),
            ("RA_SERVICE_TOKEN_FILE", token_file),
            ("RA_WAKE_KEY_FILE", wake_key_file),
        )
        if not value
    ]
    if missing:
        print("required: %s" % ", ".join(missing), file=sys.stderr)
        return 2
    os.makedirs(state_dir, mode=0o700, exist_ok=True)

    # Single writer:  one instance per state dir, or none.
    lock_fd = os.open(os.path.join(state_dir, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another review-alerts instance holds this state dir", file=sys.stderr)
        return 0

    router = wakes.WakeClient(
        str(env.get("RA_ROUTER_URL") or "http://127.0.0.1:8794"),
        lambda: _credential_from_file(wake_key_file),
    )
    # Rollout gate:  never send a real wake to a router without hold semantics.
    router.check_required_session_support(selftest_seat, probe_id=str(int(time.time())))
    gh = github_read.ServiceReadClient(
        str(env.get("RA_RELAY_URL") or "http://127.0.0.1:8471"),
        lambda: _credential_from_file(token_file),
    )
    repos = {r.strip() for r in str(env.get("RA_REPOS") or "").split(",") if r.strip()}
    if not repos:
        print(
            "RA_REPOS must name the qualified repo allowlist (no fleet-wide default)",
            file=sys.stderr,
        )
        return 2
    store = events.EventStore(os.path.join(state_dir, "events.jsonl"))
    service = AlertService(
        store, gh, router, owners_path, seat_map_from(env), repos=repos, gate_seat=selftest_seat
    )
    try:
        recovery_selection = _recovery_selection(env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if recovery_selection:
        # Post-repair recovery:  one explicitly selected, bounded pass at
        # process start, before the first cycle, under the single-writer
        # lock.  With no selection there is no pass at all, and without
        # the protected owner provider and live origin binder the pass
        # refuses (this standalone wiring has neither).
        try:
            recovery = service.recover_held(recovery_selection)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"recovery": recovery}, sort_keys=True), flush=True)
    if "--once" in argv:
        counts = service.cycle()
        _write_status(state_dir, service.status_snapshot())
        print(json.dumps(counts, sort_keys=True))
        return 0
    interval = float(env.get("RA_INTERVAL_S") or 900)
    while True:
        try:
            counts = service.cycle()
            _write_status(state_dir, service.status_snapshot())
            print(json.dumps(counts, sort_keys=True), flush=True)
        except Exception as exc:  # a bad cycle must not kill the daemon
            print(json.dumps({"cycle_error": type(exc).__name__}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
