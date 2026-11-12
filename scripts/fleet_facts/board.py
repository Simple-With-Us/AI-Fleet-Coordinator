"""Linear, through the installed `board` CLI, read verbs only.

  board list --status in_progress --json --limit 2000     every claimed (or orphaned) issue
  board list --label needs-owner --json --limit 2000      the decisions waiting for the owner
  board show <identifier> --json                          one call per In Progress issue, in order

Read-only is enforced here, not by the environment:  `_board` refuses any verb but `list` and `show`.  Calls are made one at a
time (the Fleet app's request budget is shared) with a timeout each and a time budget for the whole detail
pass;  an issue whose `show` fails or runs out of budget is marked `linear_comments.ok = false`, which makes
its activity "unknown", never idle.

A claim is an In Progress issue held by a seat:  by its `claimed:<SEAT>` label (`seat_basis` "label") or, when
it has no such label yet, by the import footer's `addressed by` seat that `board show` reports as
`addressed_by` (`seat_basis` "footer"), which is the board's own holder rule.  An In Progress issue the board
itself says nobody holds is an orphan and is listed separately (claim expiry treats it differently).  An issue
whose holder cannot be told (a list row with no labels, a `show` that failed, ran out of budget or came back
malformed) is neither:  it goes in `unreadable`, so it is never taken for an orphan.  Linear's `updated_at` is
deliberately not copied into any record.

Claim time:  the newest `CLAIMED` comment, else the import footer's `claimed <Central date and time>` (read
in America/Chicago), else None with basis "unknown".  Comments keep only `{at, author, sweeper, claimed}`:
their text is dropped here.  `sweeper` comes from `activity.is_sweeper_text`, the one definition.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from . import safety
from .activity import is_sweeper_text, parse_time
from .runner import Runner, failed, ok, which

READ_VERBS = ("list", "show")
LIST_LIMIT = 2000
LIST_TIMEOUT_S = 180
SHOW_TIMEOUT_S = 60
DETAIL_BUDGET_S = 600
CLAIMED_COMMENT_PREFIX = "CLAIMED"
CENTRAL = "America/Chicago"

_IDENT = re.compile(r"^[A-Z][A-Z0-9]{0,7}-\d{1,7}$")
_LINEAR_URL = re.compile(r"^https://linear\.app/[A-Za-z0-9._-]+/issue/[A-Za-z0-9._/-]+$")
_SEVERITY = re.compile(r"^P[0-4]$")
_BRANCH_IN_LOCATION = re.compile(r"\s@\s+([^\s()]+)")
_FOOTER_CLAIMED = re.compile(
    r"\bclaimed\s+[A-Za-z]{3},\s+([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s?([ap]m)\b",
    re.IGNORECASE,
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}


def board_command() -> str | None:
    """The board CLI to read through:  $FLEET_FACTS_BOARD when it names an executable (a cloud host points it at
    `scripts/linear-board/board` in its clone), else `board` on PATH or in ~/.local/bin."""
    override = os.environ.get("FLEET_FACTS_BOARD")
    if override:
        return which(override)
    return which("simple-board")


class BoardWriteRefused(RuntimeError):
    """A verb that could write was asked for.  A bug in the caller, never a runtime condition."""


def _board(run: Runner, board: str, verb: str, args: list[str], timeout: float):
    if verb not in READ_VERBS:
        raise BoardWriteRefused("board.py only runs %s, not %r" % ("/".join(READ_VERBS), verb))
    return run([board, verb, *args], timeout=timeout)


def _json(result) -> Any:
    if result.reason:
        raise ValueError("board %s" % result.reason)
    if result.rc != 0:
        raise ValueError("board exited %s" % result.rc)
    try:
        return json.loads(result.out)
    except ValueError:
        raise ValueError("board did not print JSON")


def parse_footer_claimed(footer: Any) -> datetime | None:
    """The `claimed Thu, Oct 8, 2026, 10:44pm` stamp from an imported issue's footer, as UTC, or None.  The
    stamp is Central time.  Looser forms (`claimed: mon, oct 5, 2026 ...`, no time of day) give None."""
    if not isinstance(footer, str):
        return None
    match = _FOOTER_CLAIMED.search(footer)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    hour = int(match.group(4))
    if month is None or not 1 <= hour <= 12:
        return None
    if match.group(6).lower() == "pm" and hour != 12:
        hour += 12
    elif match.group(6).lower() == "am" and hour == 12:
        hour = 0
    try:
        from zoneinfo import ZoneInfo

        local = datetime(
            int(match.group(3)),
            month,
            int(match.group(2)),
            hour,
            int(match.group(5)),
            tzinfo=ZoneInfo(CENTRAL),
        )
    except Exception:  # no tz database, or a date that does not exist
        return None
    return local.astimezone(UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _labels(row: dict) -> list[str]:
    return [l for l in (row.get("labels") or []) if isinstance(l, str)]


def _seats(labels: list[str]) -> list[str]:
    out = []
    for label in labels:
        if label.startswith("claimed:"):
            seat = safety.safe_token(label.split(":", 1)[1], 40)
            if seat and seat not in out:
                out.append(seat)
    return sorted(out)


def _identifier(row: dict) -> str | None:
    value = row.get("identifier")
    return value if isinstance(value, str) and _IDENT.match(value) else None


def _base(row: dict) -> dict:
    url = row.get("url")
    severity = row.get("severity")
    env = row.get("env")
    labels = _labels(row)
    return {
        "identifier": _identifier(row),
        "url": url if isinstance(url, str) and _LINEAR_URL.match(url) else None,
        "title": safety.clean(row.get("title"), 200),
        "severity": severity if isinstance(severity, str) and _SEVERITY.match(severity) else None,
        "status": safety.clean(row.get("status"), 20) or None,
        "app": safety.clean(row.get("app"), 40) or None,
        "env": env if env in ("Mac", "Cloud") else None,
        "created_at": row.get("created_at") if parse_time(row.get("created_at")) else None,
        "labels": [safety.clean(l, 60) for l in labels][:30],
        "seats": _seats(labels),
    }


def read_detail(shown: Any) -> dict:
    """From one `board show --json` document:  branch, location, claim time and basis, and the comment
    events (text dropped).  Pure."""
    if not isinstance(shown, dict) or not isinstance(shown.get("comments"), list):
        return _unreadable("board show returned an unexpected shape")
    comments_ok = True
    events: list[dict] = []
    claimed_comment: datetime | None = None
    for comment in shown["comments"]:
        if not isinstance(comment, dict):
            comments_ok = False
            continue
        when = parse_time(comment.get("created_at"))
        if when is None:
            comments_ok = False
            continue
        text = comment.get("text") if isinstance(comment.get("text"), str) else ""
        is_claim = text.lstrip().startswith(CLAIMED_COMMENT_PREFIX)
        events.append(
            {
                "at": _iso(when),
                "author": safety.clean(comment.get("author"), 60) or "unknown",
                "sweeper": is_sweeper_text(text),
                "claimed": is_claim,
            }
        )
        if is_claim and (claimed_comment is None or when > claimed_comment):
            claimed_comment = when
    location = shown.get("location") if isinstance(shown.get("location"), str) else ""
    found = _BRANCH_IN_LOCATION.search(location)
    branch, exact = safety.safe_ref(found.group(1)) if found else (None, True)
    if claimed_comment is not None:
        claimed_at, basis = _iso(claimed_comment), "claimed_comment"
    else:
        footer = parse_footer_claimed(shown.get("provenance"))
        claimed_at, basis = (_iso(footer), "import_footer") if footer else (None, "unknown")
    holders = [
        h
        for h in (
            safety.safe_token(x.strip(), 40)
            for x in str(shown.get("addressed_by") or "").split(",")
        )
        if h
    ]
    return {
        "detail_ok": True,
        "holders": holders,
        "location": safety.clean(location, 200) or None,
        "branch": branch,
        "branch_exact": exact,
        "claimed_at": claimed_at,
        "claimed_basis": basis,
        "linear_comments": {"ok": comments_ok, "events": events},
    }


def _unreadable(reason: str) -> dict:
    return {
        "detail_ok": False,
        "holders": [],
        "location": None,
        "branch": None,
        "branch_exact": True,
        "claimed_at": None,
        "claimed_basis": "unknown",
        "linear_comments": {"ok": False, "events": [], "reason": reason},
    }


def gather(
    run: Runner,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    detail_budget_s: float = DETAIL_BUDGET_S,
) -> dict:
    """The `linear` source."""
    board = board_command()
    if board is None:
        return failed("the board CLI is not installed")
    try:
        in_progress = _json(
            _board(
                run,
                board,
                "list",
                ["--status", "in_progress", "--json", "--limit", str(LIST_LIMIT)],
                LIST_TIMEOUT_S,
            )
        )
        needs_owner = _json(
            _board(
                run,
                board,
                "list",
                ["--label", "needs-owner", "--json", "--limit", str(LIST_LIMIT)],
                LIST_TIMEOUT_S,
            )
        )
    except ValueError as exc:
        return failed(str(exc))
    if not isinstance(in_progress, list) or not isinstance(needs_owner, list):
        return failed("board list did not return a list")
    if not all(isinstance(r, dict) for r in in_progress) or not all(
        isinstance(r, dict) for r in needs_owner
    ):
        return failed("board list returned a row that is not an object")
    rows = in_progress
    started = monotonic()
    claims: list[dict] = []
    orphans: list[dict] = []
    unreadable: list[dict] = []
    detail_failed = 0
    for row in rows:
        record = _base(row)
        ident = record["identifier"]
        labelled = isinstance(row.get("labels"), list)
        if not labelled:
            detail = _unreadable("the list row has no labels, so its holder cannot be told")
        elif ident is None:
            detail = _unreadable("the issue has no usable identifier")
        elif monotonic() - started > detail_budget_s:
            detail = _unreadable("the detail time budget ran out")
        else:
            try:
                detail = read_detail(
                    _json(_board(run, board, "show", [ident, "--json"], SHOW_TIMEOUT_S))
                )
            except ValueError as exc:
                detail = _unreadable(str(exc))
        holders = detail.pop("holders")
        record.update(detail)
        if record["seats"]:
            record["seat_basis"] = "label"
        elif labelled and detail["detail_ok"] and holders:
            # No claimed: label yet (an import not relabelled):  the footer's "addressed by" still holds it,
            # exactly as the board's own holder rule reads it.
            record["seats"], record["seat_basis"] = sorted(holders), "footer"
        else:
            record["seat_basis"] = "none"
        record["seat"] = record["seats"][0] if record["seats"] else None
        if not record["linear_comments"]["ok"]:
            detail_failed += 1
        if record["seats"]:
            claims.append(record)
        elif labelled and detail["detail_ok"]:
            orphans.append(record)  # the board itself says nobody holds it
        else:
            unreadable.append(record)  # not knowing the holder is not the same as no holder
    owner_items = []
    for row in needs_owner:
        item = _base(row)
        item["claimant"] = (item["seats"] or [None])[0]
        owner_items.append(item)
    data = {
        "claims": claims,
        "orphans": orphans,
        "unreadable": unreadable,
        "needs_owner": owner_items,
        "in_progress_total": len(rows),
        "detail_failed": detail_failed,
        "truncated": len(in_progress) >= LIST_LIMIT or len(needs_owner) >= LIST_LIMIT,
    }
    if data["truncated"]:
        return failed(
            "a board list hit the %d row limit, so it may be missing items" % LIST_LIMIT, data
        )
    return ok(data)
