"""Reading needs-owner items back from the board.

An item is an issue with Kind/agent-report AND the flat `needs-owner` label.  The title is never
looked at to decide that:  a GitHub-imported issue titled "OWNER DECISION" with no label is not an
item, and an issue with the label but no valid block is an item that is not adopted yet.

`board list --json` rows have no description or comments, so each item is read with `board show
--json`.  State is derived, never stored:

  closed          the board status is completed, deployed, wontfix, duplicate or canceled
  replied         an `owner replied:` record exists (records.py);  the filer closes the item
  unadopted       the label is there, but no valid block
  notified        the filer's bot posted the OWNER topic (`[needs-owner] notified msg <id>`)
  notify_pending  filed, not announced:  a `notify_pending` record, or no notice record at all

Awaiting Jay means unadopted, notify_pending or notified.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field

from . import records
from . import text as T
from .block import Block, BlockError, find_fence, parse_block
from .errors import BoardFailed
from .proc import Runner, first_line

LABEL = "needs-owner"
KIND = "agent-report"
CLOSED_STATUSES = ("completed", "deployed", "wontfix", "duplicate", "canceled")
OPEN_STATUSES = "open,in_progress,addressed,parked,backlog"
AWAITING = ("unadopted", "notify_pending", "notified")
LIST_LIMIT = 200
OVER_FIVE = 5
_TAG_RE = re.compile(r"^\[([A-Z][A-Z0-9-]*)")


@dataclass
class Item:
    identifier: str
    id: str
    url: str
    title: str
    status: str
    severity: str | None
    app: str
    created_at: str | None
    labels: list[str]
    kind: str | None
    reported_by: str | None
    block: Block | None = None
    problem: str | None = None
    block_author: str | None = None  # the seat that wrote the block comment of an adopted issue
    block_in_comment: bool = False
    notified: tuple[int, str | None] | None = None
    pending: str | None = None
    replied: dict | None = None
    replied_by: str | None = None
    comments: list[dict] = field(default_factory=list)

    @property
    def is_item(self) -> bool:
        return LABEL in self.labels and self.kind == KIND

    @property
    def state(self) -> str:
        if self.status in CLOSED_STATUSES:
            return "closed"
        if self.replied:
            return "replied"
        if self.block is None:
            return "unadopted"
        if self.notified:
            return "notified"
        return "notify_pending"

    @property
    def seat(self) -> str | None:
        return self.block.filed_by if self.block else None

    @property
    def seat_verified(self) -> bool:
        """The block's 'Filed by' equals the seat that signed the text carrying it:  the issue's own tag line
        for a filed item, the block comment's tag for an adopted one."""
        if not self.block:
            return False
        signer = self.block_author if self.block_in_comment else self.reported_by
        return bool(signer) and signer == self.block.filed_by

    @property
    def topic(self) -> str | None:
        if self.notified and self.notified[1]:
            return self.notified[1]
        if self.block and self.block.mirror_of:
            return None
        if self.block:
            return T.topic_for(self.identifier, self.block.question)
        return None


def _newest(comments: list[dict], parse):
    """(parsed value, comment) of the newest comment `parse` accepts."""
    for comment in reversed(comments):
        value = parse(comment.get("text") or "")
        if value is not None:
            return value, comment
    return None, None


def build_item(show: dict) -> Item:
    """An Item from the JSON of `board show --json`."""
    comments = list(show.get("comments") or [])
    item = Item(
        identifier=str(show.get("identifier") or ""), id=str(show.get("id") or ""), url=str(show.get("url") or ""),
        title=str(show.get("title") or ""), status=str(show.get("status") or ""), severity=show.get("severity"),
        app=str(show.get("app") or ""), created_at=show.get("created_at"), labels=list(show.get("labels") or []),
        kind=show.get("source_kind"), reported_by=show.get("reported_by"), comments=comments)
    description = str(show.get("description") or "")
    if find_fence(description) is not None:
        try:
            item.block = parse_block(description)
        except BlockError as exc:
            item.problem = str(exc)
    if item.block is None:
        for comment in reversed(comments):
            if not records.is_block_comment(comment.get("text") or ""):
                continue
            try:
                item.block = parse_block(comment.get("text") or "")
                item.block_author = comment.get("author")
                item.block_in_comment = True
                item.problem = None
                break
            except BlockError as exc:
                item.problem = item.problem or str(exc)
    if item.block is None and item.problem is None:
        item.problem = "no needs-owner block"
    item.notified, _ = _newest(comments, records.parse_notified)
    item.pending, _ = _newest(comments, records.parse_pending)
    item.replied, reply_comment = _newest(comments, records.parse_reply)
    if reply_comment is not None:
        item.replied_by = reply_comment.get("author")
    return item


def fetch(runner: Runner, ref: str) -> Item | None:
    """The item for `ref` (an identifier or board id), or None when no such issue exists."""
    result = runner.board("show", ref, "--json")
    if not result.ok:
        message = (result.err or "").lower()
        # board says "no issue AFC-9", or (for text it cannot read as an id) "... is not a board id ..."
        if result.code in (1, 2) and ("no issue" in message or "not a board id" in message or "no imported board id" in message):
            return None
        raise BoardFailed("board show failed:  %s" % (first_line(result.err) or "exit %d" % result.code), result.code)
    try:
        return build_item(json.loads(result.out))
    except ValueError:
        raise BoardFailed("board show did not return JSON") from None


def load(runner: Runner, *, closed_too: bool = False) -> tuple[list[Item], bool]:
    """(items, truncated).  Open (not closed) items by default, every labelled issue with `closed_too`.
    Only issues with Kind/agent-report and the label count, whatever the title says."""
    args = ["list", "--label", LABEL, "--kind", KIND, "--limit", str(LIST_LIMIT), "--json"]
    if not closed_too:
        args[1:1] = ["--status", OPEN_STATUSES]
    rows = runner.board_json(*args)
    items: list[Item] = []
    for row in rows if isinstance(rows, list) else []:
        if LABEL not in (row.get("labels") or []) or row.get("source_kind") != KIND:
            continue
        item = fetch(runner, str(row.get("identifier") or row.get("id")))
        if item is not None and item.is_item:
            items.append(item)
    items.sort(key=lambda i: (i.created_at or "", i.identifier))
    return items, isinstance(rows, list) and len(rows) >= LIST_LIMIT


def open_count_for(items: list[Item], seat: str) -> int:
    return sum(1 for i in items if i.seat == seat and i.state in AWAITING)


# ---- JSON ------------------------------------------------------------------------------------

def now() -> dt.datetime:
    """The current time;  NEEDS_OWNER_TEST_NOW (an ISO time) pins it for the golden test."""
    pinned = os.environ.get("NEEDS_OWNER_TEST_NOW")
    if pinned:
        return dt.datetime.fromisoformat(pinned.replace("Z", "+00:00"))
    return dt.datetime.now(dt.timezone.utc)


def age_hours(created_at: str | None, at: dt.datetime) -> float | None:
    if not created_at:
        return None
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return round(max(0.0, (at - created).total_seconds() / 3600.0), 1)


def to_json(item: Item, at: dt.datetime, over_five_seats: set[str] | frozenset[str] = frozenset()) -> dict:
    """The record `list --json` and `status --json` print.  It never holds an answer's text."""
    block = item.block
    return {
        "id": item.id,
        "identifier": item.identifier,
        "url": item.url,
        "seat": block.filed_by if block else None,
        "seat_verified": item.seat_verified,
        "for_seat": block.for_seat if block else None,
        "app": item.app,
        "question": block.question if block else T.clean_text(item.title.removeprefix(T.TITLE_PREFIX.strip()).strip() or item.title,
                                                              "title", 500, required=False),
        "options": list(block.options) if block else [],
        "low_stakes": list(block.low_stakes) if block else [],
        "recommend": block.recommend if block else None,
        "severity": item.severity,
        "urgent": item.severity == "P1",
        "parent": block.parent if block else None,
        "context": block.context if block else None,
        "topic": item.topic,
        "created_at": item.created_at,
        "notified_msg_id": item.notified[0] if item.notified else None,
        "replied": ({"kind": item.replied["kind"], "id": item.replied["id"], "option": item.replied["option"],
                     "by": item.replied_by} if item.replied else None),
        "state": item.state,
        "status": item.status,
        "age_hours": age_hours(item.created_at, at),
        "over_five": bool(block and block.filed_by in over_five_seats),
        "uid": block.uid if block else None,
        "version": block.version if block else None,
        "block_ok": block is not None,
        "edited": bool(block and not block.hash_matches_uid()),
        "problem": item.problem if block is None else None,
    }


def over_five_seats(items: list[Item]) -> set[str]:
    """Seats with more than five items awaiting Jay.  A warning for the digest, never a refusal."""
    counts: dict[str, int] = {}
    for item in items:
        if item.seat and item.state in AWAITING:
            counts[item.seat] = counts.get(item.seat, 0) + 1
    return {seat for seat, n in counts.items() if n > OVER_FIVE}
