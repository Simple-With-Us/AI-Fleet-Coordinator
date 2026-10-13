"""The sections of the facts that are not claims:  decisions, stuck PRs, work not on GitHub, unpushed work, asks.

Every function takes the F6-style part answers ({"status", "reason", "data"}) and returns a block that carries
its own `status` and `reason`.  When the source behind a block is not ok its counts are None, not 0:  a count
taken from a source nobody could read would look like "nothing to report".  Partial data from a failed source
(a truncated Linear list) is still listed, with the count withheld.

Ages come from the facts' own times, never from Linear's `updated_at`.
"""
from __future__ import annotations

import re
from datetime import datetime

from fleet_facts.activity import parse_time

from . import urls, views

__all__ = ["decisions", "stuck_prs", "not_on_github", "unpushed_over_a_day", "owner_asks",
           "STUCK_HOURS", "UNPUSHED_HOURS", "ACTIVE_HOURS"]

STUCK_HOURS = 48
UNPUSHED_HOURS = 24
ACTIVE_HOURS = 2
CLOSED = frozenset({"completed", "deployed", "addressed", "wontfix", "duplicate", "canceled", "done"})
_PREFIX = re.compile(r"^\s*owner decision\s*:\s*", re.IGNORECASE)


def _base(part: dict, **extra) -> dict:
    block = {"status": part.get("status"), "reason": part.get("reason"), "count": None}
    block.update(extra)
    return block


def _hours(since: str | None, now: datetime) -> float | None:
    when = parse_time(since)
    return round(max(0.0, (now - when).total_seconds() / 3600.0), 1) if when else None


def decisions(linear: dict, view: dict | None, now: datetime, book: urls.UrlBook) -> dict:
    """Linear needs-owner items, oldest first.  `view` is views.needs_owner_view (C6, Mac only) or None.

    C6 is asked for every row (`needs-owner list --all`), so the C6 `state` decides what Linear cannot:  `replied`
    (the owner answered, the filer has not closed it) is counted in `answered_awaiting_close` and not listed, and
    `closed` is neither.  A Linear row that C6 does not know at all (a hand-labelled issue) is listed:  being absent
    from a list is never proof that the owner answered."""
    block = _base(linear, items=[], answered_awaiting_close=None, low_stakes=None, over_five=None)
    data = linear.get("data")
    if not isinstance(data, dict):
        return block
    known = bool(view and view.get("ok"))
    items, answered = [], 0
    for row in data.get("needs_owner") or []:
        ident = row.get("identifier")
        if not ident or (row.get("status") or "").lower() in CLOSED:
            continue
        extra = view["by_ident"].get(ident) if known else None
        if extra and extra.get("state") == "closed":
            continue
        if extra and extra["replied"]:
            answered += 1
            continue
        question = (extra or {}).get("question") or _PREFIX.sub("", row.get("title") or "") or row.get("title")
        topic = (extra or {}).get("topic")
        age = _hours(row.get("created_at"), now)
        if age is None and extra:
            age = extra.get("age_hours")
        items.append({
            "identifier": ident,
            "url": book.add(urls.linear_url(row.get("url"))),
            "question": question,
            "seat": (extra or {}).get("seat") or row.get("claimant"),
            "severity": row.get("severity"),
            "age_hours": age,
            "low_stakes": extra["low_stakes"] if extra else None,
            "tap_to_answer": bool(extra["low_stakes"]) if extra else None,
            "topic_url": book.add(urls.topic_url(topic)) if topic else None,
        })
    items.sort(key=lambda i: (-(i["age_hours"] if i["age_hours"] is not None else -1.0), i["identifier"]))
    block["items"] = items
    if linear.get("status") == "ok":
        block["count"] = len(items)
        block["answered_awaiting_close"] = answered if known else None
    if known:
        block["low_stakes"] = sum(1 for i in items if i["tap_to_answer"])
        block["over_five"] = list(view["over_five_seats"])
    return block


def stuck_prs(github: dict, now: datetime, book: urls.UrlBook) -> dict:
    """Same-repo PRs that are CONFLICTING or CHANGES_REQUESTED whose head commit is 48 hours old or more.
    The age is the head commit's age, not the time spent in that state."""
    block = _base(github, items=[], unknown_age=0, mergeable_pending=None, threshold_hours=STUCK_HOURS,
                  note="head age (the head commit's age), not time in that state")
    data = github.get("data")
    if not isinstance(data, dict):
        return block
    items, unknown_age = [], 0
    for pr in data.get("prs") or []:
        if pr.get("same_repo") is False:
            continue
        conflicting = pr.get("mergeable") == "CONFLICTING"
        changes = pr.get("review_decision") == "CHANGES_REQUESTED"
        if not (conflicting or changes):
            continue
        age = _hours((pr.get("head_commit") or {}).get("committed_at"), now)
        if age is None:
            unknown_age += 1
            continue
        if age >= STUCK_HOURS:
            items.append({
                "repo": pr.get("repo"), "number": pr.get("number"), "url": book.add(pr.get("url")),
                "title": pr.get("title"), "author": pr.get("author"), "is_draft": bool(pr.get("is_draft")),
                "state": [s for s, on in (("conflicting", conflicting), ("changes_requested", changes)) if on],
                "head_age_hours": age,
            })
    items.sort(key=lambda i: (-i["head_age_hours"], i["repo"] or "", i["number"] or 0))
    block["items"] = items
    block["unknown_age"] = unknown_age
    block["mergeable_pending"] = data.get("mergeable_pending")
    if github.get("status") == "ok":
        block["count"] = len(items)
    return block


def _lane_item(lane: dict, book: urls.UrlBook, reason: str | None = None) -> dict:
    repo, number = lane.get("repo"), lane.get("pr_number")
    return {
        "lane": lane.get("lane"), "repo": repo, "branch": lane.get("branch"), "state": lane.get("state"),
        "idle_hours": lane.get("idle_hours"), "unpushed": lane.get("unpushed"), "dirty": lane.get("dirty"),
        "pr_number": number, "pr_url": book.add(urls.pr_url(repo, number)) if repo and number else None,
        "reason": reason if reason is not None else lane.get("reason"),
    }


def not_on_github(part: dict, view: dict | None, book: urls.UrlBook) -> dict:
    """Work that exists only on this Mac or in a snapshot:  every snapshot-only lane (NO age floor, a lane an
    hour old is listed), armed PRs with local work, and every C1 refusal with its reason."""
    block = _base(part, snapshot_only=None, armed_local_work=None, refusals=None)
    if part.get("status") != "ok":
        return block
    if not view or not view.get("ok"):
        block["status"], block["reason"] = "failed", (view or {}).get("reason") or "no C1 status"
        return block
    rows = view["rows"]
    idle = lambda lane: -(lane["idle_hours"] if lane["idle_hours"] is not None else -1.0)  # noqa: E731
    snap = sorted((l for l in rows if l["snapshot_only"] and not l["armed_local_work"]), key=lambda l: (idle(l), l["lane"] or ""))
    armed = sorted((l for l in rows if l["armed_local_work"]), key=lambda l: (idle(l), l["lane"] or ""))
    block["snapshot_only"] = {"count": len(snap), "items": [_lane_item(l, book) for l in snap]}
    block["armed_local_work"] = {"count": len(armed), "items": [_lane_item(l, book) for l in armed]}
    block["refusals"] = {"count": len(view["refusals"]), "items": [{"lane": r["lane"], "reason": r["reason"]} for r in view["refusals"]]}
    block["count"] = len(snap) + len(armed) + len(view["refusals"])
    return block


def unpushed_over_a_day(part: dict, skip: set[str], now: datetime) -> dict:
    """Lanes whose unpushed commits or dirty tracked files are more than a day old and which nobody has touched
    for two hours.  A lane whose age cannot be told is counted under `unknown_age`, never as young.  Lanes that
    section 3 already lists (`skip`) are not repeated, and the doctor's SAFE-TO-REMOVE (landed), TOOL-CACHE and
    ACTIVE verdicts are counted and left out:  on today's Mac 84 landed lanes still show "unpushed" commits."""
    block = _base(part, items=[], unknown_age=0, active=0, landed=0, tool_cache=0, threshold_hours=UNPUSHED_HOURS)
    data = part.get("data")
    if not isinstance(data, dict):
        return block
    items, unknown, active, landed, tool_cache = [], 0, 0, 0, 0
    for rec in data.get("attention") or []:
        name = views.lane_name(rec)
        if (name or "").startswith("review-pr-") or name in skip:
            continue
        unpushed = rec.get("unpushed") or 0
        dirty = (rec.get("dirty_tracked") or 0) + (rec.get("dirty_untracked") or 0)
        never = rec.get("never_pushed") is True
        if not (unpushed > 0 or dirty > 0 or never):
            continue
        # The doctor's own verdict comes first:  a lane whose PR landed (squash merges leave "unpushed" commits
        # behind), a tool's cache folder and a lane a process is working in are not lost work.
        verdict = rec.get("safety")
        if verdict == "SAFE-TO-REMOVE":
            landed += 1
            continue
        if verdict == "TOOL-CACHE":
            tool_cache += 1
            continue
        if verdict == "ACTIVE":
            active += 1
            continue
        since_change = _hours(rec.get("last_change_at"), now)
        ages = []
        if (rec.get("dirty_tracked") or 0) > 0 and rec.get("dirty_oldest_age_hours") is not None:
            ages.append(float(rec["dirty_oldest_age_hours"]))
        if never and rec.get("never_pushed_age_hours") is not None:
            ages.append(float(rec["never_pushed_age_hours"]))
        if unpushed > 0 and since_change is not None:
            ages.append(since_change)
        if not ages:
            unknown += 1
            continue
        if since_change is not None and since_change < ACTIVE_HOURS:
            active += 1
            continue
        age = round(max(ages), 1)
        if age > UNPUSHED_HOURS:
            items.append({
                "lane": name, "repo": rec.get("repo"), "branch": rec.get("branch"), "age_hours": age,
                "unpushed": unpushed, "dirty": dirty, "never_pushed": never, "pr_state": rec.get("pr_state"),
                "keep_reason": rec.get("keep_reason"), "git_op": rec.get("git_op_in_progress") or [],
            })
    items.sort(key=lambda i: (-i["age_hours"], i["lane"] or ""))
    block["items"], block["unknown_age"], block["active"] = items, unknown, active
    block["landed"], block["tool_cache"] = landed, tool_cache
    if part.get("status") == "ok":
        block["count"] = len(items)
    return block


def owner_asks(part: dict, view: dict | None, book: urls.UrlBook) -> dict:
    """The owner's earlier asks that are still open (C3, Phase 2).  Absent until C3 exists."""
    block = _base(part, items=[])
    if part.get("status") != "ok":
        return block
    if not view or not view.get("ok"):
        block["status"], block["reason"] = "failed", (view or {}).get("reason") or "no asks status"
        return block
    block["count"] = view["count"]
    block["items"] = [{"text": i["text"], "identifier": i["identifier"], "age_hours": i["age_hours"]} for i in view["items"]]
    return block
