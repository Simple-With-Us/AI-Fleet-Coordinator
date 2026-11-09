"""A whole synthetic morning, used by several test modules."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .builders import (NOW, World, ago as _ago, board_row, board_show, comment, doctor_checkout, doctor_report, gql_node, gql_page)

REPO = "Simple-With-Us/example-repo"


def pr(number: int, *, mergeable: str = "MERGEABLE", review: str | None = None, head_hours: float = 5.0, updated_hours: float = 1.0,
       ref: str | None = None, title: str | None = None, draft: bool = False, now: datetime = NOW, **kw) -> dict:
    node = gql_node(number, ref=ref, title=title, mergeable=mergeable, updated=_ago(updated_hours, now), **kw)
    node["reviewDecision"] = review
    node["isDraft"] = draft
    node["commits"]["nodes"][0]["commit"]["committedDate"] = _ago(head_hours, now)
    return node


def lane(path: str, **fields) -> dict:
    return doctor_checkout(path, **fields)


def standard_world(home: Path, bin_dir: Path, *, programs: bool = True, now: datetime = NOW, unknown_pr: bool = True) -> World:
    """`now` is the clock the data is relative to (a subprocess test uses the real one)."""
    ago = lambda hours: _ago(hours, now)  # noqa: E731
    w = World()
    w.pages = {"": gql_page([
        pr(1, mergeable="CONFLICTING", head_hours=60, updated_hours=0.5, title="Conflicted and old", ref="claude/ffx-1-work", now=now),
        pr(2, review="CHANGES_REQUESTED", head_hours=47.9, title="Changes asked, just under", now=now),
        pr(3, mergeable="CONFLICTING", head_hours=48.0, title="Conflicted at the line", now=now),
        pr(4, head_hours=300, title="Clean and old", now=now),
        pr(5, mergeable="UNKNOWN" if unknown_pr else "MERGEABLE", head_hours=90, title="Still being computed", now=now),
        pr(6, cross=True, owner="outside-person", title="fork text that must not leak", now=now),
    ], None, 6)}
    w.in_progress = [
        board_row("FFX-1", seats=("CLAUDE",), n=1, title="Idle fifty hours"),
        board_row("FFX-2", seats=("MM",), n=2, title="Idle forty hours"),
        board_row("FFX-3", seats=("CODEX",), n=3, title="Busy five hours ago"),
        board_row("FFX-4", seats=("GROK",), n=4, title="Its comments cannot be read"),
        board_row("FFX-5", seats=("CURSOR",), n=5, title="Fresh updated_at, old signals", updated=ago(0.1)),
        board_row("FFX-6", seats=(), n=6, title="Nobody holds this"),
    ]
    w.show = {
        "FFX-1": board_show("FFX-1", location="Mac, ~/apps/lanes/Example/claude-work @ claude/ffx-1-work",
                            comments=[comment("CLAUDE", "CLAIMED", ago(100)), comment("CLAUDE", "[CLAUDE] progress", ago(50))]),
        "FFX-2": board_show("FFX-2", comments=[comment("MM", "CLAIMED", ago(90)), comment("MM", "[MM] note", ago(40))]),
        "FFX-3": board_show("FFX-3", comments=[comment("CODEX", "CLAIMED", ago(90)), comment("CODEX", "[CODEX] note", ago(5))]),
        "FFX-4": board_show("FFX-4", comments=[comment("GROK", "CLAIMED", ago(90))]),
        "FFX-5": board_show("FFX-5", comments=[comment("CURSOR", "CLAIMED", ago(80)), comment("CURSOR", "[CURSOR] note", ago(70))]),
        "FFX-6": board_show("FFX-6"),
    }
    w.show_fail = {"FFX-4"}
    w.needs_owner = [
        board_row("FFX-10", seats=("CLAUDE",), n=10, title="OWNER DECISION:  Pick a color", status="open"),
        board_row("FFX-11", seats=("CLAUDE",), n=11, title="OWNER DECISION:  @**all** ship it?", status="open"),
        board_row("FFX-12", seats=("CODEX",), n=12, title="OWNER DECISION:  Already answered", status="open"),
        board_row("FFX-13", seats=("CODEX",), n=13, title="OWNER DECISION:  Done and closed", status="completed"),
    ]
    w.lane_report = doctor_report([
        lane("/work/lanes/Example/claude-stale", unpushed=3, last_change_at=ago(30), safety="NEEDS-REVIEW"),
        lane("/work/lanes/Example/claude-busy", dirty_tracked=2, dirty_oldest_age_hours=100, last_change_at=ago(1), safety="NEEDS-REVIEW"),
        lane("/work/lanes/Example/claude-never", never_pushed=True, never_pushed_age_hours=30, last_change_at=ago(29), safety="NEEDS-REVIEW"),
        lane("/work/lanes/Example/claude-landed", unpushed=2, last_change_at=ago(100), safety="SAFE-TO-REMOVE", pr_state="MERGED"),
        lane("/work/lanes/Example/claude-cache", unpushed=1, last_change_at=ago(100), safety="TOOL-CACHE"),
        lane("/work/lanes/Example/claude-running", unpushed=1, last_change_at=ago(100), safety="ACTIVE"),
        lane("/work/lanes/Example/review-pr-9", dirty_tracked=1, dirty_oldest_age_hours=200, last_change_at=ago(150)),
        lane("/work/lanes/Example/claude-clean"),
        lane("/work/lanes/Example/claude-snap", unpushed=2, last_change_at=ago(40), safety="NEEDS-REVIEW"),
    ], generated_at=ago(0.1))
    w.docs = {
        "fleet-autopush": {"lanes": [
            {"path": "/work/lanes/Example/claude-snap", "repo": REPO, "branch": "claude/snap", "snapshot_only": True, "idle_hours": 1.0,
             "unpushed_commits": 2, "dirty_unsnapshotted": 0, "state": "snapshot", "last_snapshot_push_at": ago(1)},
            {"path": "/work/lanes/Example/claude-armed", "repo": REPO, "branch": "claude/armed", "armed_local_work": True,
             "idle_hours": 6.0, "pr_number": 7, "unpushed_commits": 1},
            {"path": "/work/lanes/Example/claude-ok", "repo": REPO, "branch": "claude/ok", "idle_hours": 3.0},
        ], "refused": [{"path": "/work/lanes/Example/claude-gate", "reason": "gate failure:  a secret-shaped string"}]},
        "needs-owner": {"items": [
            {"identifier": "FFX-10", "seat": "CLAUDE", "low_stakes": [1, 2], "topic": "OWNER FFX-10 pick a color", "state": "open", "over_five": True},
            {"identifier": "FFX-11", "seat": "CLAUDE", "low_stakes": [], "state": "open", "over_five": True},
            {"identifier": "FFX-12", "seat": "CODEX", "state": "replied", "replied": 4242},
        ]},
    }
    if programs:
        w.programs(bin_dir, ["gh", "simple-board", "fleet-autopush", "needs-owner", "simple-chat", "fleet-facts"])
        (home / "apps").mkdir(exist_ok=True)
        w.programs(home / "apps", ["lane"])
    return w


GAP = "  "


def make_dm(facts: dict, *, extra: str = "", spaces: str = GAP) -> str:
    """A DM that passes the lint for these facts (sentence gaps are `spaces`)."""
    pr_url = facts["stuck_prs"]["items"][0]["url"]
    parts = ["Good morning." + spaces + "Two decisions wait on you.", "Stuck:  [PR 1](%s)." % pr_url,
             "Work Not on GitHub Yet:  3 items.", "Full list:  " + facts["full_list_url"], "Facts " + facts["facts_id"] + "."]
    if facts["missing"]:
        parts.append("Missing:  " + ", ".join(m["source"] for m in facts["missing"]))
    if extra:
        parts.append(extra)
    return "\n".join(parts) + "\n"
