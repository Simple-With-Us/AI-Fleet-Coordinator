"""The alert service:  one poll cycle and the daemon loop.

Per cycle, per confirmed ownership row on the allowlist:  read the PR, its
reviews, its inline comments and its head's check runs through the relay's
service_read lane;  seed a first-sight baseline instead of alerting on
history;  derive events;  submit one wake per new event to the ATTESTED
origin session;  then settle pending receipts.  Every event lands in the
store exactly once;  anything that cannot be delivered is held with a named
reason and counted, never dropped.

Besides review activity, two states of the PR itself are events:  a check run
on the current head that failed, and a current head in merge conflict with
the base branch.  Both take the same road as a review event (one Operator
status line, then one exact-origin wake), so the chat that owns the PR hears
what blocks it.  Mergeability is read from the PR body;  a body without it
(the relay's reduced read) derives no conflict event, in either direction.

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
from .operator_pr_origin import hold_label


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
                        self.binding_holds[repo + "#" + str(number)] = hold_label(exc)
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
                pull=pr,
            )
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
            owner_seat = seat_for(str((owner or {}).get("seat") or ""), self.seat_map)
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
                if reason in wakes.REDRIVE_REASONS and int(row.get("epoch") or 0) + 1 < MAX_EPOCHS:
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
