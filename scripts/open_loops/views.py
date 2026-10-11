"""Tolerant readers for the C1, C3 and C6 documents that fleet_facts passes through untouched.

Each of those components is built by a later unit, so their exact output is not final.  These readers take
the field names the plan gives and answer {"ok": False, "reason": ...} for a shape they do not recognise:
an unreadable document is Missing in the digest, never zero.  Every free-text field is cleaned with the F3
`render_untrusted` (plain form);  a lane is named by its folder name, never by its full path.
"""
from __future__ import annotations

import os
import re
from typing import Any

from fleet_facts import safety
from fleet_facts.activity import parse_time

__all__ = ["autopush_view", "needs_owner_view", "asks_view", "lane_name", "IDENT"]

IDENT = re.compile(r"^[A-Z][A-Z0-9]{0,7}-\d{1,7}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
ROW_KEYS = ("lanes", "rows", "checkouts")
REFUSAL_KEYS = ("refused", "refusals", "refused_dirty")


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> str | None:
    return value if isinstance(value, str) and parse_time(value) is not None else None


def lane_name(row: dict) -> str | None:
    """The folder name of a lane's path, else its `lane`, `name` or branch.  Never the whole path."""
    path = row.get("path")
    if isinstance(path, str) and path.strip():
        base = os.path.basename(path.rstrip("/"))
        if base:
            return safety.clean(base, 80) or None
    for key in ("lane", "name", "branch"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return safety.clean(value, 80) or None
    return None


def _dicts(value: Any) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    return [r for r in value if isinstance(r, dict)]


def _lane(row: dict) -> dict:
    repo = row.get("repo") if isinstance(row.get("repo"), str) else row.get("owner_repo")
    branch, exact = safety.safe_ref(row.get("branch"))
    state = safety.clean(row.get("state"), 40) or None
    return {
        "lane": lane_name(row),
        "repo": repo if isinstance(repo, str) and _REPO.match(repo) else None,
        "branch": branch,
        "branch_exact": exact,
        "state": state,
        "idle_hours": _num(row.get("idle_hours")),
        "last_change_at": _time(row.get("last_change_at")),
        "last_snapshot_push_at": _time(row.get("last_snapshot_push_at")),
        "unpushed": _int(row.get("unpushed_commits")),
        "dirty": _int(row.get("dirty_unsnapshotted")),
        "snapshot_only": row.get("snapshot_only") is True,
        "armed_local_work": row.get("armed_local_work") is True or state == "armed_local_work",
        "pr_number": _int(row.get("pr_number")),
        "pr_is_auto_draft": row.get("pr_is_auto_draft") if isinstance(row.get("pr_is_auto_draft"), bool) else None,
        "reason": safety.clean(row.get("blocked_reason") or row.get("reason"), 160) or None,
    }


def autopush_view(data: Any) -> dict:
    """C1 `fleet-autopush status --json` as passed through by fleet_facts (`{"document": ...}`).

    {"ok": True, "rows": [lane...], "refusals": [{"lane", "reason"}...]} or {"ok": False, "reason": ...}.
    A row with a blocked_reason and work that is not safe (unpushed commits or dirty files) is a refusal too."""
    document = data.get("document") if isinstance(data, dict) else None
    rows: list[dict] | None = None
    refusals_raw: list[dict] | None = None
    if isinstance(document, list):
        rows = _dicts(document)
    elif isinstance(document, dict):
        for key in ROW_KEYS:
            if key in document:
                rows = _dicts(document[key])
                break
        for key in REFUSAL_KEYS:
            if key in document:
                refusals_raw = _dicts(document[key])
                break
    if rows is None and refusals_raw is None:
        return {"ok": False, "reason": "the fleet-autopush status has a shape this version does not know"}
    lanes = [_lane(r) for r in (rows or [])]
    refusals: list[dict] = []
    seen: set[str] = set()
    for raw in refusals_raw or []:
        name = lane_name(raw)
        reason = safety.clean(raw.get("reason") or raw.get("blocked_reason"), 160) or "no reason given"
        refusals.append({"lane": name, "reason": reason})
        if name:
            seen.add(name)
    for lane in lanes:
        if lane["reason"] and lane["lane"] not in seen and ((lane["unpushed"] or 0) > 0 or (lane["dirty"] or 0) > 0):
            refusals.append({"lane": lane["lane"], "reason": lane["reason"]})
            if lane["lane"]:
                seen.add(lane["lane"])
    return {"ok": True, "rows": lanes, "refusals": refusals}


def _option_numbers(value: Any) -> list[int]:
    if isinstance(value, list):
        out = []
        for item in value:
            number = _int(item) if not isinstance(item, dict) else _int(item.get("n") or item.get("number") or item.get("index"))
            if number is not None and number > 0 and number not in out:
                out.append(number)
        return sorted(out)
    return []


def needs_owner_view(data: Any) -> dict:
    """C6 `needs-owner list --json` as passed through by fleet_facts.

    {"ok": True, "by_ident": {ID: {...}}, "over_five_seats": [...]} or {"ok": False, "reason": ...}."""
    document = data.get("document") if isinstance(data, dict) else None
    rows = None
    if isinstance(document, list):
        rows = _dicts(document)
    elif isinstance(document, dict):
        for key in ("items", "issues", "decisions"):
            if key in document:
                rows = _dicts(document[key])
                break
    if rows is None:
        return {"ok": False, "reason": "the needs-owner list has a shape this version does not know"}
    by_ident: dict[str, dict] = {}
    over: set[str] = set()
    for row in rows:
        ident = row.get("identifier") if isinstance(row.get("identifier"), str) else row.get("id")
        if not (isinstance(ident, str) and IDENT.match(ident)):
            continue
        seat = safety.safe_token(row.get("seat"), 40)
        topic = row.get("topic") if isinstance(row.get("topic"), str) and 0 < len(row.get("topic")) <= 60 else None
        state = (safety.clean(row.get("state"), 20) or "").lower() or None
        replied = row.get("replied") not in (None, False, "", {}, [], 0) or state == "replied"
        options = _option_numbers(row.get("low_stakes"))
        by_ident[ident] = {
            "seat": seat,
            "question": safety.clean(row.get("question"), 200) or None,
            "low_stakes": options,
            "topic": topic,
            "state": state,
            "replied": bool(replied),
            "over_five": row.get("over_five") is True,
            "age_hours": _num(row.get("age_hours")),
        }
        if row.get("over_five") is True and seat:
            over.add(seat)
    return {"ok": True, "by_ident": by_ident, "over_five_seats": sorted(over)}


def asks_view(data: Any) -> dict:
    """C3 `agent-sync asks status --json`:  the owner's earlier asks that are still open."""
    document = data.get("document") if isinstance(data, dict) else None
    rows = None
    if isinstance(document, list):
        rows = _dicts(document)
    elif isinstance(document, dict):
        for key in ("open", "items", "asks"):
            if key in document:
                rows = _dicts(document[key])
                break
    if rows is None:
        return {"ok": False, "reason": "the asks status has a shape this version does not know"}
    items = []
    for row in rows[:50]:
        ident = row.get("identifier")
        items.append({
            "text": safety.clean(row.get("title") or row.get("text") or row.get("ask"), 200),
            "identifier": ident if isinstance(ident, str) and IDENT.match(ident) else None,
            "age_hours": _num(row.get("age_hours")),
        })
    return {"ok": True, "count": len(rows), "items": items}
