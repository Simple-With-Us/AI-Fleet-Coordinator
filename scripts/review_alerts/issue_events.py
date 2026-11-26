"""Standalone-issue alert state:  a SEPARATE append-only ledger from the PR
event store, under the same single worker and router.

Namespace isolation, not a second producer:  the PR store's seeds and cursors
are keyed (repo, number) alone, so sharing them with issue numbers would
contaminate both.  Issue rows live in their own file with their own fold;
the PR ledger's bytes, seeds, cursors and fences are untouched by anything
here.

Ownership for standalone issues comes ONLY from the registry's explicit
bound_issues association (the session's own seat-lane write, surfaced by
the reviewed issue-ownership contract), never from an assignee, label,
title or letter guess.  Without a configured census and binder every issue
row is still HELD with its full hint evidence (source event id, ref, time,
action, delivery id), under the same discipline as recover_held:  fresh
binding, frozen provenance, bounded counts.

Baselines:  a per-issue "seed" row records the comment high-water at first
sight (only what postdates the census row's binding instant alerts), and a
single ledger-level "rollout" seed records the instant this service first
scanned the issue census.  The rollout instant is the grounded boundary for
issue_opened:  a canonical opening AFTER it is current-state notification;
anything at or before it (every pre-adoption binding) is retrospective
history and is baselined, never replayed.  No intermediate closed/deleted
history is claimed from current-state reads.
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import events

ISSUE_COMMENT_KINDS = ("issue_comment_added", "issue_comment_edited")
ISSUE_KINDS = ("issue_opened",) + ISSUE_COMMENT_KINDS + ("seed",)

ISSUE_FIELDS = (
    "ts",
    "repo",
    "number",
    "kind",
    "event_id",
    "diagnostic",
    "subject",
    "source_time",
    "source_ref",
    "source_action",
    "delivery_id",
    "summary",
    "status",
    "reason",
    # Dispatch and reconciliation fields (parallel to the PR store):  the
    # wake identity, the operator message id, the observed revision state.
    "epoch",
    "actor",
    "head",
    "current_head",
    "head_matches",
    "wake_id",
    "message_id",
    "link",
    "revision",
    "observed_updated",
)

#: The rollout boundary marker lives under a reserved identity no real issue
#: can collide with (the empty repo, number 0).
ROLLOUT_KEY = ("", 0)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def key_of(row) -> tuple:
    return (row["repo"], int(row["number"]), row["kind"], str(row["event_id"]))


class IssueEventStore:
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
                    if isinstance(row, dict) and row.get("kind") in ISSUE_KINDS:
                        self._fold(row)

    def _fold(self, row: dict) -> None:
        if row.get("kind") == "seed":
            # Last seed wins (the completion marker supersedes an earlier
            # partial record), same as the PR store.
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
            self._by_key[key] = row
            return
        if epoch < existing_epoch:
            return
        if existing.get("status") not in events.TERMINAL and row.get(
            "status"
        ) in events.TERMINAL + events.NONTERMINAL:
            self._by_key[key] = row

    def _repair_torn_tail(self) -> None:
        """Same torn-tail discipline as the PR event store:  cut a partial
        final line;  keep and terminate a complete one missing its newline."""
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
        if row.get("kind") not in ISSUE_KINDS:
            raise ValueError("unknown issue kind: %r" % (row.get("kind"),))
        if row.get("subject") != "issue":
            raise ValueError("issue rows carry subject=issue")
        row = {field: row.get(field) for field in ISSUE_FIELDS}
        row["ts"] = row["ts"] or _utcnow()
        line = json.dumps(row, sort_keys=True) + "\n"
        with self._lock:
            self._repair_torn_tail()
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._fold(row)
        return dict(row)

    def get(self, repo: str, number: int, kind: str, event_id: str) -> dict | None:
        with self._lock:
            row = self._by_key.get((repo, int(number), kind, str(event_id)))
            return dict(row) if row is not None else None

    def pending(self) -> list:
        with self._lock:
            return [
                dict(r) for r in self._by_key.values() if r.get("status") in events.NONTERMINAL
            ]

    def held(self) -> list:
        with self._lock:
            return [dict(r) for r in self._by_key.values() if r.get("status") == "held"]

    def seed(self, repo: str, number: int, comment_high: int) -> None:
        """First-sight baseline:  existing comments are NOT alerted on;  only
        what lands after the seed.  First seed wins;  baseline revision rows
        are written BEFORE this completion marker so a crash between them is
        resumed, never skipped."""
        with self._lock:
            if (repo, int(number)) in self._seeds:
                return
        self.append(
            {
                "repo": repo,
                "number": number,
                "kind": "seed",
                "event_id": "%d" % comment_high,
                "subject": "issue",
                "status": "seed",
                "summary": "baseline; nothing before this is alerted",
            }
        )

    def has_seed(self, repo: str, number: int) -> bool:
        with self._lock:
            return (repo, int(number)) in self._seeds

    def seed_row(self, repo: str, number: int) -> dict | None:
        with self._lock:
            row = self._seeds.get((repo, int(number)))
            return dict(row) if row is not None else None

    def cursor(self, repo: str, number: int) -> dict:
        """The fixed comment baseline boundary:  the seed's high-water ALONE.
        Observed comment events never advance it, so a sparse hint for a high
        id can never hide an unseen lower id from the full ascending scan."""
        high = 0
        with self._lock:
            seed = self._seeds.get((repo, int(number)))
            if seed is not None and str(seed["event_id"]).isdigit():
                high = int(str(seed["event_id"]))
        return {"comment_id": high}

    def comment_rows(self, repo: str, number: int) -> list:
        """The recorded issue_comment_added/issue_comment_edited rows for one
        issue (baseline rows included), in durable fold (observation) order:
        the observed-revision state the shared reconciliation reads."""
        with self._lock:
            return [
                dict(row)
                for (r, n, kind, _eid), row in self._by_key.items()
                if r == repo and n == int(number) and kind in ISSUE_COMMENT_KINDS
            ]

    def record_rollout(self, ts: str | None = None) -> None:
        """The one-time census-rollout boundary:  the instant this service
        first scanned the issue census.  First record wins;  the caller
        passes an explicit instant only in tests."""
        with self._lock:
            if ROLLOUT_KEY in self._seeds:
                return
        self.append(
            {
                "repo": ROLLOUT_KEY[0],
                "number": ROLLOUT_KEY[1],
                "kind": "seed",
                "event_id": "rollout",
                "subject": "issue",
                "ts": ts or _utcnow(),
                "status": "seed",
                "summary": "issue-census rollout boundary; openings at or before this are baseline history",
            }
        )

    def rollout_ts(self) -> str | None:
        with self._lock:
            row = self._seeds.get(ROLLOUT_KEY)
            return str(row.get("ts") or "") if row is not None else None

    def counts(self) -> dict:
        out: dict = {}
        with self._lock:
            for row in self._by_key.values():
                status = row.get("status") or "?"
                out[status] = out.get(status, 0) + 1
        return out
