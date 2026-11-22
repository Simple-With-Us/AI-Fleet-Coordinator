"""The alert event store:  append-only JSONL, fsynced, torn-tail repaired.

One row per event.  Rows are never rewritten:  a later row with the same
(repo, number, kind, event_id) key upgrades the delivery status (submitted ->
delivered/held), first row wins for the event itself.  A re-alert after a
certain-undelivered wake appends the SAME key with a higher epoch (and a new
wake id);  the fold keeps the highest epoch.  The whole index is a fold of
the file, so a rebuild from the file is exact.

Statuses:  submitted (wake accepted by the router, receipt pending),
delivered (router receipt says the wake reached the chat's input - TRANSPORT
acknowledgement only, never proof the chat read or acted), unknown (the
router's own record cannot prove delivery either way), held (no delivery and
none coming;  the reason names why).
"""

from __future__ import annotations

import json
import os
import threading
import time

EVENT_FIELDS = (
    "ts",
    "repo",
    "number",
    "kind",
    "event_id",
    "epoch",
    "actor",
    "head",
    "current_head",
    "head_matches",
    "summary",
    "status",
    "wake_id",
    "reason",
)

KINDS = ("review_submitted", "review_completed", "findings_added")
TERMINAL = ("delivered", "unknown", "held")
#: submitted = wake accepted, receipt pending;  submit-pending = the submit's
#: outcome is not settled (transport failure or a possibly-lost answer) and is
#: retried or reconciled by receipt, never re-detected.
NONTERMINAL = ("submitted", "submit-pending")


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def key_of(row) -> tuple:
    return (row["repo"], int(row["number"]), row["kind"], str(row["event_id"]))


class EventStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._by_key: dict[tuple, dict] = {}
        self._seeds: dict[tuple, dict] = {}
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
                    if not isinstance(row, dict) or "kind" not in row:
                        continue
                    self._fold(row)

    def _fold(self, row: dict) -> None:
        if row.get("kind") == "seed":
            self._seeds[(row["repo"], int(row["number"]))] = row
            return
        key = key_of(row)
        epoch = int(row.get("epoch") or 0)
        existing = self._by_key.get(key)
        if existing is None:
            self._by_key[key] = row
            return
        existing_epoch = int(existing.get("epoch") or 0)
        if epoch > existing_epoch:
            # A re-alert epoch replaces the dead attempt's row.
            self._by_key[key] = row
            return
        if epoch < existing_epoch:
            return
        if existing.get("status") not in TERMINAL and row.get("status") in TERMINAL + NONTERMINAL:
            self._by_key[key] = row

    def _repair_torn_tail(self) -> None:
        """A crash may leave a partial final line;  cut it before the next
        append.  A final line that is a COMPLETE row missing only its newline
        is kept and terminated instead:  truncating it would silently lose an
        event that was safely written."""
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
            if isinstance(parsed, dict) and "kind" in parsed:
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            else:
                fh.truncate(0 if cut < 0 else cut + 1)

    def append(self, row: dict) -> dict:
        row = {field: row.get(field) for field in EVENT_FIELDS}
        row["ts"] = row["ts"] or _utcnow()
        row["epoch"] = int(row.get("epoch") or 0)
        line = json.dumps(row, sort_keys=True) + "\n"
        with self._lock:
            self._repair_torn_tail()
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._fold(row)
            folded = (
                self._seeds.get((row["repo"], int(row["number"])))
                if row.get("kind") == "seed"
                else self._by_key.get(key_of(row))
            )
        return dict(folded if folded is not None else row)

    def get(self, repo: str, number: int, kind: str, event_id: str) -> dict | None:
        with self._lock:
            row = self._by_key.get((repo, int(number), kind, str(event_id)))
            return dict(row) if row is not None else None

    def pending(self) -> list[dict]:
        """Unsettled events:  submitted (receipt pending) and submit-pending
        (submit outcome unsettled;  reconcile or resubmit)."""
        with self._lock:
            return [dict(r) for r in self._by_key.values() if r.get("status") in NONTERMINAL]

    def held(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._by_key.values() if r.get("status") == "held"]

    def seed(
        self, repo: str, number: int, review_id: int, comment_id: int, check_id: int = 0
    ) -> None:
        """First-sight baseline:  existing activity is NOT alerted on;  only
        what lands after the seed.  Recorded like any other row."""
        with self._lock:
            if (repo, int(number)) in self._seeds:
                return
        self.append(
            {
                "repo": repo,
                "number": number,
                "kind": "seed",
                "event_id": "%d:%d:%d" % (review_id, comment_id, check_id),
                "status": "seed",
                "summary": "baseline; nothing before this is alerted",
            }
        )

    def has_seed(self, repo: str, number: int) -> bool:
        with self._lock:
            return (repo, int(number)) in self._seeds

    def cursor(self, repo: str, number: int) -> dict:
        """The high-water marks and alerted completion heads derived from the
        seed and stored events (ids only grow;  heads only accumulate)."""
        reviews = 0
        comments = 0
        checks = 0
        completed_heads: set = set()
        with self._lock:
            seed = self._seeds.get((repo, int(number)))
            if seed is not None:
                parts = str(seed["event_id"]).split(":")
                if len(parts) >= 1 and parts[0].isdigit():
                    reviews = int(parts[0])
                if len(parts) >= 2 and parts[1].isdigit():
                    comments = int(parts[1])
                if len(parts) >= 3 and parts[2].isdigit():
                    checks = int(parts[2])
            for (r, n, kind, event_id), row in self._by_key.items():
                if r != repo or n != int(number):
                    continue
                if kind == "review_submitted" and str(event_id).isdigit():
                    reviews = max(reviews, int(event_id))
                elif kind == "review_completed" and str(event_id).isdigit():
                    checks = max(checks, int(event_id))
                elif kind == "findings_added" and str(event_id).startswith("c"):
                    tail = str(event_id)[1:]
                    if tail.isdigit():
                        comments = max(comments, int(tail))
                if kind in ("review_submitted", "review_completed"):
                    head = str(row.get("head") or "")
                    if head:
                        completed_heads.add(head)
        return {
            "review_id": reviews,
            "comment_id": comments,
            "check_id": checks,
            "completed_heads": sorted(completed_heads),
        }

    def counts(self) -> dict:
        out: dict = {}
        with self._lock:
            for row in self._by_key.values():
                status = row.get("status") or "?"
                out[status] = out.get(status, 0) + 1
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_key)
