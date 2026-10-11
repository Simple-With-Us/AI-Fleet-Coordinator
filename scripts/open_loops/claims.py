"""Claim bands for the digest, from the one activity definition (fleet_facts.activity).

For each claim (an In Progress Linear issue held by a seat) this passes the activity function every signal
this host could read, and only those:

  linear_comments   from the Linear gather (any comment but the sweepers' own, any seat's)
  pr_update         the same-repo PR from the claim's branch (its updatedAt), when the GitHub census is ok
  branch_push       that PR's head commit time (a stand-in for the push), when a PR exists
  c1_snapshot       the lane's last snapshot push, from the C1 status, when that is readable
  zulip_topic       the claimant's posts in the issue's topic (zulip_topics), for claims that need it

A source that was tried and failed is passed as {"ok": False}, which makes the claim "unknown", never idle.
That includes a Zulip topic list that could not be read and a claim the Zulip read budget skipped.
A source that could not be tried here (no PR for the branch, no C1, no agent-sync) is not passed and is
listed under the claim's `unchecked`:  it can only make a claim look MORE idle, so the digest may flag a
claim that is in fact moving, but never hides one that is stuck.  Linear's `updated_at` is never read.

Until claim expiry launches (CLAIM_EXPIRY_LAUNCH_AT, in the past) the block says `clock_started: false` and
"ages, not releases".
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from fleet_facts import activity

from . import urls

__all__ = ["build", "NOTE_NOT_STARTED", "OWNER_SEATS"]

NOTE_NOT_STARTED = "claim clock not started;  these are ages, not releases"
OWNER_SEATS = ("OWNER", "JAY")
ZULIP_READER = Callable[[list[dict]], dict]


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _prs_by_branch(github: dict) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    data = github.get("data") or {}
    for pr in data.get("prs") or []:
        if pr.get("same_repo") and pr.get("head_ref_exact", True) and pr.get("head_ref"):
            index.setdefault(pr["head_ref"], []).append(pr)
    return index


def _snapshots_by_branch(rows: list[dict]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for row in rows:
        if row.get("branch") and row.get("branch_exact", True) and row.get("last_snapshot_push_at"):
            index.setdefault(row["branch"], []).append(row["last_snapshot_push_at"])
    return index


def _signals(claim: dict, github: dict, prs: dict[str, list[dict]], autopush: dict, snaps: dict[str, list[str]]) -> dict:
    signals: dict = {"linear_comments": claim.get("linear_comments") or {"ok": False}}
    branch = claim.get("branch") if claim.get("branch_exact", True) else None
    if not branch:
        return signals
    if github.get("status") == "ok":
        matches = prs.get(branch, [])
        signals["pr_update"] = {"ok": True, "events": [{"at": p["updated_at"], "same_repo": True} for p in matches if p.get("updated_at")]}
        pushes = [{"at": p["head_commit"]["committed_at"]} for p in matches if (p.get("head_commit") or {}).get("committed_at")]
        if matches and pushes:
            signals["branch_push"] = {"ok": True, "events": pushes}
    elif github.get("status") == "failed":
        signals["pr_update"] = {"ok": False}
    if autopush.get("status") == "ok":
        signals["c1_snapshot"] = {"ok": True, "events": [{"at": t} for t in snaps.get(branch, [])]}
    elif autopush.get("status") == "failed":
        signals["c1_snapshot"] = {"ok": False}
    return signals


def _record(claim: dict, signals: dict, launch_at: datetime | None, now: datetime, book: urls.UrlBook) -> dict:
    item = {"signals": signals, "launch_at": launch_at, "claimed_at": claim.get("claimed_at"), "seat": claim.get("seat")}
    detail = activity.explain(signals, launch_at, claim.get("claimed_at"), seat=claim.get("seat"))
    hours = activity.idle_hours(item, now)
    band = activity.idle_band(item, now)
    at = detail["at"]
    return {
        "identifier": claim.get("identifier"),
        "url": book.add(urls.linear_url(claim.get("url"))),
        "title": claim.get("title"),
        "seat": claim.get("seat"),
        "branch": claim.get("branch"),
        "claimed_at": claim.get("claimed_at"),
        "claimed_basis": claim.get("claimed_basis"),
        "activity_at": _iso(at) if isinstance(at, datetime) else None,
        "basis": detail["basis"],
        "idle_hours": round(hours, 1) if isinstance(hours, float) else None,
        "band": band,
        "checked": sorted(k for k, v in signals.items() if isinstance(v, dict) and v.get("ok") is True),
        "unchecked": sorted(set(activity.SOURCES) - set(signals)),
        "unknown_sources": detail["unknown_sources"],
    }


def build(linear: dict, github: dict, autopush: dict, autopush_rows: list[dict], *, launch_at: datetime | None,
          now: datetime, zulip: ZULIP_READER | None, book: urls.UrlBook) -> dict:
    """The `claims` block of the facts.  `linear`, `github` and `autopush` are the F6-style part answers
    ({"status", "reason", "data"});  `autopush_rows` are the lanes from `views.autopush_view`."""
    started = launch_at is not None and launch_at <= now
    floor = launch_at if started else None
    block: dict = {
        "status": linear.get("status"),
        "reason": linear.get("reason"),
        "clock_started": started,
        "launch_at": _iso(launch_at),
        "note": None if started else NOTE_NOT_STARTED,
        "total": None, "counts": None, "idle_36h": [], "idle_48h": [], "unknown": [],
        "orphans": None, "unreadable": None, "owner_held": 0,
        "zulip": None,
    }
    data = linear.get("data")
    if not isinstance(data, dict):
        return block
    prs = _prs_by_branch(github)
    snaps = _snapshots_by_branch(autopush_rows)
    records: list[tuple[dict, dict, dict]] = []   # (claim, signals, record)
    for claim in data.get("claims") or []:
        if claim.get("seat") in OWNER_SEATS:
            block["owner_held"] += 1
            continue
        signals = _signals(claim, github, prs, autopush, snaps)
        records.append((claim, signals, _record(claim, signals, floor, now, book)))
    wanted = [{"identifier": c["identifier"], "seat": c.get("seat")} for c, _, r in records
              if r["band"] != activity.BAND_NONE and c.get("identifier")]
    if zulip is not None and wanted:
        result = zulip(wanted)
        block["zulip"] = {k: result.get(k) for k in ("status", "reason", "reads", "skipped")}
        by_ident = result.get("by_ident") or {}
        asked = {w["identifier"] for w in wanted}
        for index, (claim, signals, _) in enumerate(records):
            ident = claim.get("identifier")
            if ident not in asked:
                continue
            seen = by_ident.get(ident)
            if seen is None and result.get("status") != "absent":
                # The source was tried and gave this claim no answer:  the topic list failed, or the read budget ran
                # out before the claim's turn.  That is "unknown", never idle.  Only a host with no agent-sync at all
                # ("absent") could not try, and that stays "unchecked".
                seen = {"ok": False}
            if seen is not None:
                signals["zulip_topic"] = seen
                records[index] = (claim, signals, _record(claim, signals, floor, now, book))
    counts = {activity.BAND_NONE: 0, activity.BAND_36H: 0, activity.BAND_48H: 0, activity.UNKNOWN: 0}
    for _, _, record in records:
        counts[record["band"]] += 1
    order = lambda r: (-(r["idle_hours"] or 0.0), r["identifier"] or "")  # noqa: E731
    block.update({
        "total": len(records) + block["owner_held"],
        "counts": counts,
        "idle_36h": sorted((r for _, _, r in records if r["band"] == activity.BAND_36H), key=order),
        "idle_48h": sorted((r for _, _, r in records if r["band"] == activity.BAND_48H), key=order),
        "unknown": sorted((r for _, _, r in records if r["band"] == activity.UNKNOWN), key=lambda r: r["identifier"] or ""),
        "orphans": len(data.get("orphans") or []),
        "unreadable": len(data.get("unreadable") or []),
    })
    return block
