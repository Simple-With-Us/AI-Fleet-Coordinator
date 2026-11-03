"""The Operator's chat-thread registry:  one aggregate row per Zulip stream topic, kept by the
listener itself and mirrored to the "Operator Thread Registry" Google Sheet.

Why this exists (owner, Tue, Nov 3, 2026):  the Operator should maintain a registry of chat
threads in a NEW Google Sheet, separate from the SWU Fleet References workbook, with all the
info about the threads it can carry — human-visible first, and usable as a registry surface
(read back by tools) if it works out that way.  The full design note, with the source of every
column, is docs/operator-thread-registry.md.

Shape:

  * The daemon observes every stream message it routes (Daemon.route_message) and folds it
    into a per-thread aggregate in <state root>/operator/thread-registry.json.  Observation
    is deduplicated by Zulip message id, so the same message delivered to several seat
    runners, or replayed through backfill, counts once.  Nothing here ever reads Zulip
    history:  the registry knows only what the listener itself has seen since the store
    was created, which keeps every sync bounded.
  * The sync projects the aggregates to sheet rows and upserts them into the sheet keyed
    by Thread Key (stream id + case-folded topic — Zulip groups topics case-insensitively,
    the same folding state.cursor_key uses).  The sync is idempotent:  a row whose cells
    (ignoring the Registry Updated At stamp) already match is not rewritten, and a re-run
    with no new observations changes nothing.
  * The Google write itself runs in thread_registry_export.mjs, which owns the service
    account auth (Node's crypto signs the service-account JWT;  the Python standard library
    cannot).  This module owns the semantics — aggregation, projection, upsert planning —
    and sync_to_sheet() runs the same plan against any adapter with the documented
    protocol, which is what the tests exercise.

Deterministic only:  no column is ever guessed.  Purpose has no deterministic source in a
message event, so it is written blank;  a human may type into that column in the sheet and
the sync preserves a non-blank Purpose on rows it updates (see plan_upserts).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import live as L
from .identity import seat_tag_for

__all__ = [
    "ENV_STORE_PATH",
    "HEADERS",
    "SHEET_TAB",
    "SPREADSHEET_ID",
    "SPREADSHEET_URL",
    "SheetHeaderError",
    "ThreadRegistryStore",
    "extract_linear_issues",
    "extract_pr_references",
    "participant_tag",
    "plan_upserts",
    "status_for",
    "sync_to_sheet",
    "thread_key",
    "thread_store_path",
]

#: The workbook the connector created for this registry (Tue, Nov 3, 2026).  The id is not a
#: secret;  it is also recorded in docs/operator-thread-registry.md and in
#: thread_registry_export.mjs, which performs the live Google writes.
SPREADSHEET_ID = "1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE"
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/%s/edit" % SPREADSHEET_ID
SHEET_TAB = "Zulip Threads"

#: Column order is the sheet's header row, exactly.  validateHeaders in the exporter and
#: sync_to_sheet both refuse to write when the live header differs.
HEADERS = [
    "Thread Key",
    "Stream",
    "Stream ID",
    "Topic",
    "Status",
    "Participants",
    "Participant Count",
    "Message Count",
    "First Seen (UTC)",
    "Last Activity (UTC)",
    "Last Message ID",
    "Linear Issues",
    "PR References",
    "Purpose",
    "Registry Updated At (UTC)",
]

COL_KEY = 0
COL_PURPOSE = 13
COL_UPDATED = 14

ENV_STORE_PATH = "AGENT_SYNC_OPERATOR_THREAD_REGISTRY"
STORE_VERSION = 1
#: How many processed message ids the dedupe set retains.  Ids are Zulip-global and roughly
#: monotonic per realm, so the newest ids are the ones a replay can actually re-deliver;
#: evicting the oldest is safe in practice and bounds the store file.
PROCESSED_ID_CAP = 20_000

ACTIVE_WINDOW_S = 24 * 3600
IDLE_WINDOW_S = 14 * 24 * 3600

# A Linear issue mention:  two or more capitals/digits, a dash, and a number (AFC-412,
# AFC-565).  Mentions are harvested from BOTH sources the listener sees for a thread — the
# topic name and each message body — and the per-thread set only ever grows:  deduplicated,
# sorted, never removed once added, so a key named once stays on the thread's row for good.
# This is a mention extractor, not a verifier:  a string shaped like an issue id is recorded
# whether or not the issue exists.
_LINEAR_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b")
# PR references, three cheap shapes:  a full GitHub pull URL, an explicit "PR #123", and a
# repo-qualified "owner/repo#123".  URLs are normalised to owner/repo#123 so the same PR
# mentioned both ways lands once.
_PR_URL_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
_PR_REPO_RE = re.compile(r"\b([\w.-]+)/([\w.-]+)#(\d+)\b")
_PR_BARE_RE = re.compile(r"\bPR\s+#(\d+)\b", re.IGNORECASE)


def thread_store_path(root: str, env: Mapping[str, str] | None = None) -> str:
    """Where the thread aggregates live:  beside the session registry under
    <state root>/operator/, with the same env-override seam (AGENT_SYNC_OPERATOR_THREAD_REGISTRY)."""
    if env:
        override = env.get(ENV_STORE_PATH)
        if override:
            return os.path.expanduser(override)
    return os.path.join(str(root), "operator", "thread-registry.json")


def thread_key(stream_id: int, topic: str) -> str:
    """The upsert key:  one row per (stream, topic), topics folded the way Zulip groups them."""
    return "%d:%s" % (stream_id, str(topic or "").strip().casefold())


def participant_tag(message: Mapping[str, Any]) -> str:
    """The stable participant label for a message's sender:  a seat tag for a bot
    (identity.seat_tag_for:  muse-assist-bot@ -> MA, and the fleet's email overrides),
    the sender's first name upper-cased for a human.  Canonical tags follow the owner's
    seat-tag ruling (Tue, Nov 3, 2026):  Zcode is ZC and Kimi is KIMI wherever a seat's
    own bot email yields them through seat_tag_for."""
    # Zulip message events do not carry an is_bot flag for the sender;  the daemon's realm
    # map does, and the daemon hook enriches the message with sender_is_bot when the runner
    # knows the sender.  Without it, fall back to the realm's bot naming (a "-bot" local
    # part), which is how every fleet bot account is named.
    is_bot = message.get("sender_is_bot")
    if not isinstance(is_bot, bool):
        local = str(message.get("sender_email") or "").split("@", 1)[0].lower()
        is_bot = local.endswith("-bot") or local.endswith("_bot")
    user = {
        "is_bot": is_bot,
        "email": message.get("sender_email") or "",
        "full_name": message.get("sender_full_name") or "",
    }
    return seat_tag_for(user)


def extract_linear_issues(content: str) -> list[str]:
    """The Linear issue keys mentioned in one text.  Callers feed it both a thread's topic
    name and each message body;  the returned list is sorted and deduplicated."""
    return sorted(set(_LINEAR_RE.findall(content or "")))


def extract_pr_references(content: str) -> list[str]:
    text = content or ""
    found: set[str] = set()
    for owner, repo, num in _PR_URL_RE.findall(text):
        found.add("%s/%s#%s" % (owner, repo, num))
    text = _PR_URL_RE.sub(" ", text)
    for owner, repo, num in _PR_REPO_RE.findall(text):
        found.add("%s/%s#%s" % (owner, repo, num))
    for num in _PR_BARE_RE.findall(text):
        found.add("PR #%s" % num)
    return sorted(found)


def status_for(topic: str, last_activity: float, now: float) -> str:
    """Derived, never stored:  a topic Zulip shows as resolved (its check-mark prefix) is
    closed;  otherwise the age of the last observed message decides."""
    if str(topic or "").lstrip().startswith("✔"):
        return "closed"
    age = max(0.0, now - last_activity)
    if age <= ACTIVE_WINDOW_S:
        return "active"
    if age <= IDLE_WINDOW_S:
        return "idle"
    return "dormant"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class ThreadRegistryStore:
    """The listener's own thread aggregates, persisted as one JSON file.

    The file is rewritten atomically (live.write_json) on every observation that changes
    it.  The aggregate is small — one record per thread, never one per message — so the
    rewrite stays cheap at fleet message rates, and a crash loses nothing already routed.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    # ---- persistence ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            raw = L.read_json(self.path)
            threads = raw.get("threads")
            processed = raw.get("processed_ids")
            self._data = {
                "version": STORE_VERSION,
                "threads": threads if isinstance(threads, dict) else {},
                "processed_ids": processed if isinstance(processed, list) else [],
            }
        return self._data

    def _flush(self) -> None:
        assert self._data is not None
        parent = os.path.dirname(self.path)
        if parent:
            L.private_dir(parent)
        L.write_json(self.path, self._data)

    # ---- observation ------------------------------------------------------------------

    def observe(self, message: Mapping[str, Any], *, stream_name: str | None = None) -> bool:
        """Fold one routed stream message into its thread aggregate.  Returns True when the
        message was counted, False when it was a duplicate delivery or not a stream
        message this registry can place.  Never raises on a malformed message."""
        if not isinstance(message, Mapping) or message.get("type") != "stream":
            return False
        message_id = _as_int(message.get("id"))
        stream_id = _as_int(message.get("stream_id"))
        ts = _as_float(message.get("timestamp"))
        if message_id is None or stream_id is None or ts is None:
            return False
        topic = str(message.get("subject") or message.get("topic") or "")
        recipient = message.get("display_recipient")
        stream = recipient if isinstance(recipient, str) and recipient else (stream_name or "")
        content = str(message.get("content") or "")
        with self._lock:
            data = self._load()
            processed: list[int] = data["processed_ids"]
            if message_id in set(processed):
                return False
            processed.append(message_id)
            if len(processed) > PROCESSED_ID_CAP:
                del processed[: len(processed) - PROCESSED_ID_CAP]
            key = thread_key(stream_id, topic)
            threads: dict[str, Any] = data["threads"]
            record = threads.get(key)
            if record is None:
                record = {
                    "stream_id": stream_id,
                    "stream": stream,
                    "topic": topic,
                    "participants": [],
                    "message_count": 0,
                    "first_seen": ts,
                    "last_activity": ts,
                    "last_message_id": message_id,
                    "linear_issues": [],
                    "pr_references": [],
                }
                threads[key] = record
            if stream:
                record["stream"] = stream
            if topic:
                record["topic"] = topic
            tag = participant_tag(message)
            participants = set(record.get("participants") or [])
            participants.add(tag)
            record["participants"] = sorted(participants)
            record["message_count"] = int(record.get("message_count") or 0) + 1
            record["first_seen"] = min(float(record.get("first_seen") or ts), ts)
            if ts >= float(record.get("last_activity") or 0):
                record["last_activity"] = ts
                record["last_message_id"] = max(int(record.get("last_message_id") or 0), message_id)
            # Linear Issues contract:  keys mentioned in the topic name or in any message
            # body join the thread's set, deduplicated;  the union only ever grows, so a
            # key once added is never removed by a later, quieter message.
            mentioned = set(extract_linear_issues(content)) | set(extract_linear_issues(topic))
            record["linear_issues"] = sorted(set(record.get("linear_issues") or []) | mentioned)
            record["pr_references"] = sorted(
                set(record.get("pr_references") or []) | set(extract_pr_references(content))
            )
            self._flush()
            return True

    # ---- projection -------------------------------------------------------------------

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
            threads = data["threads"]
            return [threads[key] for key in sorted(threads)]

    def rows(self, now: float | None = None) -> list[list[str]]:
        """The sheet rows for every known thread, in Thread Key order.  `now` is the sync
        run's clock:  Status and the Registry Updated At stamp are computed from it, so a
        caller that passes one fixed `now` gets a self-consistent sheet."""
        stamp = _iso(now if now is not None else time.time())
        out: list[list[str]] = []
        for record in self.records():
            stream_id = int(record.get("stream_id") or 0)
            topic = str(record.get("topic") or "")
            participants = [str(p) for p in record.get("participants") or []]
            out.append(
                [
                    thread_key(stream_id, topic),
                    str(record.get("stream") or ""),
                    str(stream_id),
                    topic,
                    status_for(topic, float(record.get("last_activity") or 0), float(now or 0)),
                    ", ".join(participants),
                    str(len(participants)),
                    str(int(record.get("message_count") or 0)),
                    _iso(float(record.get("first_seen") or 0)),
                    _iso(float(record.get("last_activity") or 0)),
                    str(int(record.get("last_message_id") or 0)),
                    ", ".join(str(v) for v in record.get("linear_issues") or []),
                    ", ".join(str(v) for v in record.get("pr_references") or []),
                    "",  # Purpose:  no deterministic source;  see plan_upserts for preservation
                    stamp,
                ]
            )
        return out


# --------------------------------------------------------------------------------------------
# The sheet sync
# --------------------------------------------------------------------------------------------


class SheetHeaderError(RuntimeError):
    """The live sheet's header row is not HEADERS;  the sync writes nothing."""


def _cells_match(existing: list[str], desired: list[str]) -> bool:
    """Equality for idempotency:  every column except the Registry Updated At stamp, and a
    non-blank Purpose a human typed into the sheet counts as a match for the blank the
    projection carries (the sync never erases it)."""
    if len(existing) < len(desired):
        existing = existing + [""] * (len(desired) - len(existing))
    for idx, want in enumerate(desired):
        if idx in (COL_UPDATED,):
            continue
        if idx == COL_PURPOSE and existing[idx] and not want:
            continue
        if existing[idx] != want:
            return False
    return True


def _merged_cells(existing: list[str], desired: list[str]) -> list[str]:
    """The cells to write for an update:  the desired row, with a human-entered Purpose
    preserved when the projection has none."""
    cells = list(desired)
    if len(existing) > COL_PURPOSE and existing[COL_PURPOSE] and not cells[COL_PURPOSE]:
        cells[COL_PURPOSE] = existing[COL_PURPOSE]
    return cells


def plan_upserts(existing: list[Mapping[str, Any]], desired: list[list[str]]) -> dict[str, Any]:
    """Plan one sync.  `existing` rows carry {rowNumber, cells} (1-based sheet rows, header
    is row 1).  Returns {updates: [(rowNumber, cells)], appends: [cells], unchanged: int,
    duplicate_rows: [rowNumber]}.  When the sheet somehow holds two rows with one Thread
    Key, the first is the row of record and the rest are reported as duplicates — never
    written, never deleted by the sync."""
    by_key: dict[str, Mapping[str, Any]] = {}
    duplicates: list[int] = []
    for row in existing:
        cells = [str(c) for c in (row.get("cells") or [])]
        if not cells or not cells[COL_KEY]:
            continue
        key = cells[COL_KEY]
        if key in by_key:
            duplicates.append(int(row.get("rowNumber") or 0))
        else:
            by_key[key] = row
    updates: list[tuple[int, list[str]]] = []
    appends: list[list[str]] = []
    unchanged = 0
    for cells in desired:
        key = cells[COL_KEY]
        current = by_key.get(key)
        if current is None:
            appends.append(cells)
            continue
        current_cells = [str(c) for c in (current.get("cells") or [])]
        if _cells_match(current_cells, cells):
            unchanged += 1
        else:
            updates.append(
                (int(current.get("rowNumber") or 0), _merged_cells(current_cells, cells))
            )
    return {
        "updates": updates,
        "appends": appends,
        "unchanged": unchanged,
        "duplicate_rows": duplicates,
    }


def sync_to_sheet(
    store: ThreadRegistryStore, adapter: Any, *, now: float | None = None
) -> dict[str, Any]:
    """Upsert the store's rows through `adapter` and report exactly what happened.

    Adapter protocol (the live implementation is thread_registry_export.mjs's drain;  the
    tests use an in-memory adapter with the same surface):

        validate_headers() -> list[str]   the sheet's live header row
        read_rows() -> [{rowNumber, cells}]
        append_row(cells) -> rowNumber
        update_row(rowNumber, cells) -> None

    The header is validated before any write, so a renamed or reordered column leaves the
    sheet untouched.  Writes stop at the first failure:  rows already written stay written,
    the result says partial=True with the counts so far, and the next run reconciles by
    Thread Key — the same discipline as operator_sheets_export.mjs's drain."""
    headers = [str(h) for h in adapter.validate_headers()]
    if headers != HEADERS:
        raise SheetHeaderError("sheet header mismatch: expected %r, found %r" % (HEADERS, headers))
    moment = now if now is not None else time.time()
    plan = plan_upserts(adapter.read_rows(), store.rows(now=moment))
    result: dict[str, Any] = {
        "updated": 0,
        "appended": 0,
        "unchanged": plan["unchanged"],
        "duplicate_rows": plan["duplicate_rows"],
        "failed": 0,
        "partial": False,
        "error": None,
    }
    try:
        for row_number, cells in plan["updates"]:
            adapter.update_row(row_number, cells)
            result["updated"] += 1
        for cells in plan["appends"]:
            adapter.append_row(cells)
            result["appended"] += 1
    except Exception as exc:  # a failed write stops the sync;  nothing is retried blindly
        result["failed"] = 1
        result["partial"] = True
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    return result


def _main(argv: list[str]) -> int:
    """Operator-side CLI:  `export-rows` writes the desired rows as JSON for the Node drain
    (thread_registry_export.mjs);  `stats` prints the store's size.  Neither talks to
    Google — the drain is the only Google writer."""
    import argparse

    parser = argparse.ArgumentParser(prog="thread_registry")
    parser.add_argument("command", choices=["export-rows", "stats"])
    parser.add_argument("--state-root", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    root = args.state_root or L.state_root(os.environ)
    store = ThreadRegistryStore(thread_store_path(root, os.environ))
    if args.command == "stats":
        records = store.records()
        print(
            json.dumps(
                {
                    "threads": len(records),
                    "messages": sum(int(r.get("message_count") or 0) for r in records),
                    "store": store.path,
                }
            )
        )
        return 0
    payload = {
        "spreadsheetId": SPREADSHEET_ID,
        "tab": SHEET_TAB,
        "headers": HEADERS,
        "rows": store.rows(),
    }
    text = json.dumps(payload, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(os.sys.argv[1:]))
