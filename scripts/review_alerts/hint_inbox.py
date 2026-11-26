"""The durable hint inbox:  the enqueue-only boundary between the webhook
listener and the single review-alerts worker.

Listener threads only validate and append;  the existing review-alerts worker
alone drains and executes.  Append-only JSONL, fsynced, torn-tail repaired -
the same durability discipline as the event store - because an HTTP ack must
mean the hint is DURABLE:  accepted hints survive a process crash, and a
redelivery after a crash dedupes against the file, not against memory.

Durability is PER TARGET:  every hint row is individually written and
fsynced before the next is attempted.  A process crash mid-delivery can
leave a durable PREFIX of the delivery's targets;  in that case no HTTP
success was acknowledged (the accept never returned).  GitHub does NOT
automatically redeliver failed deliveries (official webhook docs), so the
missing tail is NOT guaranteed to arrive:  coverage of OWNED PR review
activity (reviews, review comments, check runs) comes from the regular
poll, which reads the source of truth each cycle and derives missed events
for those subjects without any webhook.  Issue and issue-comment subjects
stay durably HELD until the reviewed canonical consumer contract covers
them;  nothing claims fallback coverage for them here.  The durable prefix
stands as evidence and dedupes any later redelivery (manual or
source-initiated) by identity.  Capacity preflight
is the one all-or-nothing step:  when the inbox cannot take every target
of a delivery, NOTHING is written and the listener answers 503 - a visible
refusal, never a relied-upon retry.

Every accepted hint keeps its own row with its full evidence (delivery id,
source event id/ref/time/action, head advisory).  Scheduling may coalesce
EXECUTION (one reconcile per target per batch);  it never erases a hint's
evidence row.  Dispositions:  queued (awaiting the worker), held (visible
unresolved, durable, never executed), consumed (the worker ran it).  A
redelivery of an already-recorded target is a duplicate:  acked, never
re-appended.

Retries are bounded and durable:  a transient execution failure requeues the
row with an attempts counter and a not-before time (both persisted), and a
row that exhausts its attempts becomes durably HELD with the failure reason -
a poison hint can neither hot-loop the worker nor be silently dropped.

Consumed rows are compacted after RETENTION_S:  the accepted-delivery
dedupe window far outlasts any plausible manual redelivery, and a later
redelivery (there is no automatic one) simply re-executes the reconcile,
which the event ledger's own dedupe makes idempotent.  Held and queued
rows are never compacted:  they are unresolved evidence.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import threading
import time

INBOX_FIELDS = (
    "ts",
    "delivery_id",
    "subject",
    "repo",
    "number",
    "hint_kind",
    "source_event_id",
    "source_time",
    "source_ref",
    "source_action",
    "head_sha",
    "disposition",
    "reason",
    "attempts",
    "not_before",
)

#: The hint kinds, mirrored from the agreed pure-mapper contract.  pr-subject
#: kinds reconcile one owned PR;  issue-subject kinds are held with evidence
#: until an explicitly reviewed issue-ownership contract exists.
HINT_KINDS_PR = (
    "pr",
    "pr_review",
    "pr_review_comment",
    "pr_comment",
    "check_run",
    "check_suite",
    "merge_group",
)
HINT_KINDS_ISSUE = ("issue", "issue_comment_issue")
HINT_KINDS = HINT_KINDS_PR + HINT_KINDS_ISSUE

#: Upper bound on unresolved (queued + held) rows.  Capacity is checked
#: BEFORE anything is appended, so a delivery is recorded atomically:  when
#: the inbox cannot take every target, NOTHING is written and the listener
#: answers retryably - no whole-delivery acknowledgement without durable
#: per-target disposition.  Held rows are unresolved evidence and count
#: toward this bound:  when unresolved evidence fills the inbox the listener
#: answers 503-retry, a visible refusal - never a silent drop.
MAX_ACTIVE = 1024

#: Transient execution retry policy:  bounded exponential backoff, persisted
#: per row, so a restart neither resets the budget nor hot-loops.  A row
#: that exhausts MAX_ATTEMPTS becomes durably held (visible, reason-bound),
#: never dropped and never retired forever.
MAX_ATTEMPTS = 6
RETRY_BASE_S = 5.0
RETRY_CAP_S = 120.0

#: Consumed rows are pure dedupe/evidence history.  They are compacted away
#: after RETENTION_S, far beyond any plausible manual redelivery (GitHub
#: does not automatically redeliver failed deliveries);  a later redelivery
#: re-executes the reconcile, which the event ledger's own dedupe makes
#: idempotent.  Compaction is triggered on load and whenever consumed rows
#: outnumber the active bound.
RETENTION_S = 7 * 86400.0

#: The active-identity bound is NOT a disk bound:  every disposition
#: transition appends a journal line, and a workload of repeated
#: hold/requeue cycles grows the file forever with zero consumed rows.
#: When the journal exceeds JOURNAL_MAX lines the file is checkpointed:
#: rewritten with ONLY the latest disposition row per identity (every
#: unresolved row's complete current evidence stands;  superseded
#: transitions are history, and history is what compaction removes).
JOURNAL_MAX = 4 * MAX_ACTIVE

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_HEAD_RE = re.compile(r"[0-9a-f]{40}")


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class InboxFull(Exception):
    """No capacity for every target of this delivery:  nothing was written."""


def validate_hint(hint: dict) -> dict:
    """One normalized hint row, or ValueError.  Strict fields and bounds:  an
    extra key or an out-of-shape value is a mapper contract breach, never
    something to guess around.  number 0 is valid only for repo-level held
    evidence (a supported event whose targets need canonical discovery)."""
    if not isinstance(hint, dict):
        raise ValueError("hint-not-an-object")
    allowed = {
        "subject",
        "repo",
        "number",
        "hint_kind",
        "source_event_id",
        "source_time",
        "source_ref",
        "source_action",
        "head_sha",
    }
    if set(hint) - allowed:
        raise ValueError("hint-unknown-fields: %s" % sorted(set(hint) - allowed))
    subject = hint.get("subject")
    repo = hint.get("repo")
    number = hint.get("number")
    kind = hint.get("hint_kind")
    if subject not in ("pr", "issue"):
        raise ValueError("hint-bad-subject")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise ValueError("hint-bad-repo")
    if type(number) is not int or number < 0 or number > 9999999999:
        raise ValueError("hint-bad-number")
    if subject == "pr" and kind not in HINT_KINDS_PR:
        raise ValueError("hint-bad-kind")
    if subject == "issue" and kind not in HINT_KINDS_ISSUE:
        raise ValueError("hint-bad-kind")
    out = {field: hint.get(field) for field in allowed}
    for field in ("source_event_id", "source_time", "source_action"):
        value = out.get(field)
        if value is None:
            out[field] = ""
        elif not isinstance(value, str) or len(value) > 128:
            raise ValueError("hint-bad-%s" % field)
    ref = out.get("source_ref")
    if ref is None:
        out["source_ref"] = ""
    elif not isinstance(ref, str) or len(ref) > 512:
        raise ValueError("hint-bad-source_ref")
    head = out.get("head_sha")
    if head in (None, ""):
        out["head_sha"] = ""
    elif not isinstance(head, str) or not _HEAD_RE.fullmatch(head):
        raise ValueError("hint-bad-head_sha")
    return out


def _identity(row: dict) -> tuple:
    """Per-target identity:  the reviewer's (delivery, subject, repo, number)
    PLUS the source event id, so two distinct comments arriving for one
    target in one delivery never collapse into one evidence row."""
    return (
        str(row.get("delivery_id") or ""),
        str(row.get("subject") or ""),
        str(row.get("repo") or ""),
        int(row.get("number") or 0),
        str(row.get("source_event_id") or ""),
    )


class HintInbox:
    def __init__(self, path: str, nudge: threading.Event | None = None, max_active: int = MAX_ACTIVE):
        self._path = path
        self._nudge = nudge
        self._max_active = int(max_active)
        self._lock = threading.Lock()
        self._rows: dict[tuple, dict] = {}
        self._order: list[tuple] = []
        self._journal = 0
        self._repair_torn_tail()
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and "disposition" in row:
                        self._journal += 1
                        self._fold(row)
        with self._lock:
            self._compact_locked()

    def _fold(self, row: dict) -> None:
        key = _identity(row)
        if key not in self._rows:
            self._order.append(key)
        # Latest disposition wins:  a consumed row supersedes the queued row
        # it settles.  Evidence is the append-only file;  the index is a fold.
        self._rows[key] = row

    def _repair_torn_tail(self) -> None:
        """Same torn-tail discipline as the event store:  cut a partial final
        line;  keep and terminate a complete one missing only its newline."""
        if not os.path.exists(self._path):
            return
        with open(self._path, "rb") as fh:
            data = fh.read()
        if not data or data.endswith(b"\n"):
            return
        cut = data.rfind(b"\n")
        tail = data[cut + 1 :] if cut >= 0 else data
        try:
            parsed = json.loads(tail.decode("utf-8"))
        except (UnicodeError, ValueError):
            parsed = None
        with open(self._path, "r+b") as fh:
            if isinstance(parsed, dict) and "disposition" in parsed:
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            else:
                fh.truncate(0 if cut < 0 else cut + 1)

    def _append_locked(self, row: dict) -> None:
        """One row durably appended:  file fsync, and on first creation the
        parent directory fsync too - a file's own fsync does not publish a
        NEW filename durably.  Caller holds the lock."""
        created = not os.path.exists(self._path)
        line = json.dumps({f: row.get(f) for f in INBOX_FIELDS}, sort_keys=True) + "\n"
        self._repair_torn_tail()
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._journal += 1
        if created:
            dir_fd = os.open(os.path.dirname(self._path) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        self._fold(row)

    def _compact_locked(self) -> None:
        """Bound the journal, transactionally (temp file, fsync, atomic
        replace, directory fsync).  Two triggers, one rewrite:
          - retention:  consumed rows older than RETENTION_S are dropped;
          - checkpoint:  when the journal exceeds JOURNAL_MAX lines, only
            the LATEST disposition row per identity survives (superseded
            transitions are history, not evidence).
        Queued and held rows are unresolved evidence:  their latest row is
        never dropped by either trigger.  Runs on load, on accept when
        consumed rows outnumber the active bound, and when the journal
        crosses JOURNAL_MAX."""
        if not os.path.exists(self._path):
            return
        now = time.time()
        kept = []
        dropped = 0
        checkpoint = self._journal > JOURNAL_MAX
        for key in self._order:
            row = self._rows[key]
            if row.get("disposition") == "consumed":
                try:
                    # ts is UTC ("...Z"):  age it with timegm, never local
                    # mktime - a local-time parse shifts the retention
                    # boundary by the host's timezone.
                    age = now - calendar.timegm(
                        time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ")
                    )
                except (ValueError, OverflowError):
                    age = 0.0
                if age > RETENTION_S:
                    dropped += 1
                    continue
            kept.append(row)
        if not dropped and not checkpoint:
            return
        tmp = self._path + ".compact"
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in kept:
                fh.write(json.dumps({f: row.get(f) for f in INBOX_FIELDS}, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)
        dir_fd = os.open(os.path.dirname(self._path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self._rows = {}
        self._order = []
        for row in kept:
            self._fold(row)
        self._journal = len(kept)

    def _active(self) -> int:
        return sum(
            1 for row in self._rows.values() if row.get("disposition") in ("queued", "held")
        )

    def _consumed(self) -> int:
        return sum(1 for row in self._rows.values() if row.get("disposition") == "consumed")

    def accept(
        self,
        delivery_id: str,
        hints: list,
        *,
        disposition: str = "queued",
        reason: str | None = None,
    ) -> list:
        """Record one delivery's targets with durable per-target semantics.

        Returns one outcome per hint:  queued/held for newly durable rows,
        duplicate for a target whose identity already has a disposition.
        InboxFull means NOTHING was appended;  the listener answers 503 - a
        visible refusal, since GitHub does not automatically redeliver
        failed deliveries.  The canonical poll covers refused OWNED PR
        targets from current source state (issue subjects stay held under
        their own contract);  any later redelivery (manual or
        source-initiated) completes the missing targets with per-target
        idempotence.  A crash mid-accept can leave a durable prefix;  no
        success was acknowledged and the prefix dedupes by identity."""
        if not isinstance(delivery_id, str) or not 1 <= len(delivery_id) <= 128:
            raise ValueError("bad-delivery-id")
        if disposition not in ("queued", "held"):
            raise ValueError("bad-disposition")
        normalized = [validate_hint(h) for h in hints]
        if disposition == "queued" and any(h["number"] == 0 for h in normalized):
            # number 0 is repo-level HELD evidence only;  queued work always
            # names an exact target.
            raise ValueError("queued-hint-without-target")
        with self._lock:
            outcomes = []
            fresh = []
            for hint in normalized:
                row = dict(hint)
                row.update(
                    ts=_utcnow(),
                    delivery_id=delivery_id,
                    disposition=disposition,
                    reason=reason,
                    attempts=0,
                    not_before=0.0,
                )
                if _identity(row) in self._rows:
                    outcomes.append("duplicate")
                else:
                    outcomes.append(disposition)
                    fresh.append(row)
            if self._active() + len(fresh) > self._max_active:
                raise InboxFull("hint inbox active bound %d" % self._max_active)
            for row in fresh:
                self._append_locked(row)
            if fresh and disposition == "queued" and self._nudge is not None:
                # Set under the queue lock:  the worker clears the nudge only
                # while holding this same lock, so a hint landing between the
                # worker's empty-check and its wait is never lost.
                self._nudge.set()
            if self._consumed() > self._max_active or self._journal > JOURNAL_MAX:
                self._compact_locked()
            return outcomes

    def drain(self, limit: int, now: float | None = None) -> list:
        """The oldest READY queued rows (not-before reached), up to limit.
        Draining does NOT consume:  rows leave the queued state only through
        consume(), hold_row() or retry().  `now` is WALL time (the
        persisted not-before is a wall-clock deadline)."""
        if now is None:
            now = time.time()
        out = []
        with self._lock:
            for key in self._order:
                row = self._rows.get(key)
                if (
                    row is not None
                    and row.get("disposition") == "queued"
                    and float(row.get("not_before") or 0.0) <= now
                ):
                    out.append(dict(row))
                    if len(out) >= limit:
                        break
        return out

    def consume(self, rows: list) -> None:
        """Mark executed rows consumed (append-only:  each consumption is its
        own row;  the original queued row's evidence stands in the file)."""
        with self._lock:
            for row in rows:
                settled = dict(row)
                settled.update(ts=_utcnow(), disposition="consumed", reason=row.get("reason"))
                self._append_locked(settled)

    def hold_row(self, row: dict, reason: str) -> None:
        """Mark one row durably HELD:  visible unresolved evidence with its
        bounded reason, persisted like every other disposition.  Held rows
        leave hold only through an explicit requeue (a new hint for the same
        target is a NEW row and reconciles independently)."""
        with self._lock:
            settled = dict(row)
            settled.update(ts=_utcnow(), disposition="held", reason=str(reason)[:80])
            self._append_locked(settled)

    def retry(self, row: dict, exc_name: str) -> str:
        """Record one transient execution failure:  requeue with a persisted
        attempts counter and exponential not-before, or - attempts exhausted -
        durably HOLD the row with the failure reason.  Returns the new
        disposition.  The budget survives restart;  a poison hint backs off
        instead of hot-looping, and is never silently dropped."""
        exc_name = str(exc_name)[:40]
        with self._lock:
            current = self._rows.get(_identity(row), row)
            attempts = int(current.get("attempts") or 0) + 1
            settled = dict(current)
            if attempts >= MAX_ATTEMPTS:
                settled.update(
                    ts=_utcnow(),
                    disposition="held",
                    reason="retry-exhausted:%s" % exc_name,
                    attempts=attempts,
                    not_before=0.0,
                )
                self._append_locked(settled)
                return "held"
            delay = min(RETRY_BASE_S * (2 ** (attempts - 1)), RETRY_CAP_S)
            settled.update(
                ts=_utcnow(),
                disposition="queued",
                reason="retry:%s" % exc_name,
                attempts=attempts,
                # WALL-CLOCK deadline:  this row is persisted, and a
                # monotonic clock is per-boot - a rebooted host would read
                # a stale monotonic value as far future (stalled hints) or
                # far past (skipped backoff).  Wall time survives reboot;
                # readiness comparisons use time.time() everywhere.
                # Rollout note:  this ingress was never activated with the
                # prior monotonic format, so no legacy rows exist to
                # migrate.  If one were ever encountered, its small
                # monotonic value would read as an already-expired wall
                # deadline:  one immediate retry with the attempts budget
                # preserved, never a stall - no interpreting an arbitrary
                # number as a known clock.
                not_before=time.time() + delay,
            )
            self._append_locked(settled)
            return "queued"

    def requeue_held(self, targets: set, reason_prefixes: tuple = ("owner-unknown", "bind:")) -> int:
        """Requeue held rows whose (repo, number) is in targets and whose
        reason matches one of the prefixes - the retry-on-ownership-change
        half of held semantics:  a poll cycle that now sees the owner revives
        the evidence row for one fresh reconcile.  Discovery (number 0) and
        contract-blocked holds (comment-read, issue-owner) are NOT revived
        here:  they wait for their own reviewed contract."""
        count = 0
        with self._lock:
            for key in list(self._order):
                row = self._rows.get(key)
                if row is None or row.get("disposition") != "held":
                    continue
                reason = str(row.get("reason") or "")
                if not reason.startswith(reason_prefixes):
                    continue
                target = (str(row.get("repo") or ""), int(row.get("number") or 0))
                if target not in targets:
                    continue
                revived = dict(row)
                revived.update(
                    ts=_utcnow(), disposition="queued", reason=None, attempts=0, not_before=0.0
                )
                self._append_locked(revived)
                count += 1
            if count and self._nudge is not None:
                self._nudge.set()
            if self._journal > JOURNAL_MAX:
                self._compact_locked()
        return count

    def ready(self, now: float | None = None) -> bool:
        """Any queued row whose not-before has passed (wall time)."""
        if now is None:
            now = time.time()
        with self._lock:
            return any(
                row.get("disposition") == "queued" and float(row.get("not_before") or 0.0) <= now
                for row in self._rows.values()
            )

    def next_wake(self, now: float | None = None) -> float | None:
        """The earliest future not-before among queued rows (WALL clock),
        or None.  The worker converts this to its monotonic wait deadline
        and waits no longer than it for backoff work, so a retrying hint
        can neither starve the poll nor be forgotten."""
        if now is None:
            now = time.time()
        with self._lock:
            future = [
                float(row.get("not_before") or 0.0)
                for row in self._rows.values()
                if row.get("disposition") == "queued" and float(row.get("not_before") or 0.0) > now
            ]
        return min(future) if future else None

    def mark_idle_if_empty(self, now: float | None = None) -> bool:
        """Clear the nudge only when no READY queued work remains, under the
        same lock accept() sets it:  no lost wakeup between drain and wait.
        Rows still backing off keep their not-before deadline via
        next_wake();  they do not need the nudge held.  `now` is WALL time."""
        if now is None:
            now = time.time()
        with self._lock:
            idle = not any(
                row.get("disposition") == "queued" and float(row.get("not_before") or 0.0) <= now
                for row in self._rows.values()
            )
            if idle and self._nudge is not None:
                self._nudge.clear()
            return idle

    def held(self) -> list:
        with self._lock:
            return [
                dict(self._rows[key])
                for key in self._order
                if self._rows[key].get("disposition") == "held"
            ]

    def stats(self) -> dict:
        with self._lock:
            out = {"queued": 0, "held": 0, "consumed": 0}
            for row in self._rows.values():
                disposition = row.get("disposition")
                if disposition in out:
                    out[disposition] += 1
            return out
