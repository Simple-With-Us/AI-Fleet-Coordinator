"""Synthetic data for the fleet_facts tests.  Nothing here is real:  repositories are `example-*`, Linear
identifiers use the team key FFX (which is not a real team), and every UUID is 00000000-0000-4000-8000-<12 hex>.
`check_fixtures.py` fails the build if a real path, identifier or UUID shows up in this folder."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

ORG = "Simple-With-Us"
REPO = ORG + "/example-repo"


def uuid(n: int) -> str:
    return "00000000-0000-4000-8000-%012x" % n


def gql_node(number: int, *, repo: str = REPO, ref: str | None = None, title: str | None = None, cross: bool = False,
             owner: str | None = ORG, mergeable: str = "MERGEABLE", author: str = "fleet-author", labels=("area-core",),
             threads=(True, False), files=("src/app.py",), checks=("SUCCESS", "FAILURE"), auto_merge: bool = False,
             updated: str = "2026-10-10T12:00:00Z", node_id: str | None = None, url: str | None = None) -> dict:
    node = {
        "id": node_id or "PR_example_%d" % number,
        "number": number,
        "title": title if title is not None else "Example change %d" % number,
        "isDraft": False,
        "mergeable": mergeable,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "createdAt": "2026-10-09T08:00:00Z",
        "updatedAt": updated,
        "headRefName": ref if ref is not None else "claude/example-%d" % number,
        "isCrossRepository": cross,
        "repository": {"nameWithOwner": repo},
        "headRepositoryOwner": ({"login": owner} if owner else None),
        "author": {"login": author},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "autoMergeRequest": ({"enabledAt": "2026-10-10T12:00:00Z", "mergeMethod": "SQUASH"} if auto_merge else None),
        "commits": {"nodes": [{"commit": {
            "oid": "a" * 40,
            "committedDate": "2026-10-10T11:00:00Z",
            "statusCheckRollup": {"state": "FAILURE" if "FAILURE" in checks else "SUCCESS", "contexts": {
                "totalCount": len(checks),
                "nodes": [({"__typename": "CheckRun", "name": "check-%d" % i, "status": "COMPLETED", "conclusion": c})
                          if i % 2 == 0 else ({"__typename": "StatusContext", "context": "status-%d" % i, "state": c})
                          for i, c in enumerate(checks)]}}}}]},
        "reviewThreads": {"totalCount": len(threads), "nodes": [{"isResolved": r} for r in threads]},
        "files": {"totalCount": len(files), "nodes": [{"path": p} for p in files]},
    }
    if url is not None:
        node["url"] = url
    return node


def gql_page(nodes, next_cursor: str | None = None, issue_count: int | None = None) -> dict:
    return {"data": {"search": {
        "issueCount": issue_count if issue_count is not None else len(nodes),
        "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
        "nodes": list(nodes)}}}


def doctor_checkout(path: str, **fields) -> dict:
    base = {"path": path, "realpath": path, "kind": "linked-worktree", "owner_repo": REPO, "location_class": "LANE_NESTED",
            "layout_status": "correct", "branch": "claude/example", "unpushed": 0, "dirty_tracked": 0, "dirty_untracked": 0,
            "never_pushed": False, "git_op_in_progress": [], "dropped_ball": False, "safety": "SAFE-TO-REMOVE",
            "safety_reasons": [], "pr_state": "NONE", "pr_number": None, "janitor_keep": False,
            "keep": {"present": False}, "lane_age_days": 3.0, "idle_days": 1.0}
    base.update(fields)
    return base


def doctor_report(checkouts, *, generated_at: str, gh: str = "ok", lsof: str = "ok") -> dict:
    return {"schema": 2, "generated_at": generated_at, "host": "example-host", "gh": gh, "lsof": lsof,
            "checkouts": list(checkouts), "cleaner_candidates": [],
            "summary": {"total": len(checkouts), "by_safety": {"SAFE-TO-REMOVE": len(checkouts)},
                        "by_location_class": {"LANE_NESTED": len(checkouts)}}}


def board_row(identifier: str, *, seats=("CLAUDE",), title: str = "Example issue", status: str = "in_progress",
              labels=(), n: int = 1, url: str | None = None, updated: str = "2026-10-11T10:00:00.000Z") -> dict:
    all_labels = list(labels) + ["Kind/agent-report", "Severity/P2", "Env/Mac"] + ["claimed:%s" % s for s in seats] + ["seat:%s" % s for s in seats]
    return {"id": uuid(n), "identifier": identifier,
            "url": url if url is not None else "https://linear.app/example-workspace/issue/%s/example-issue" % identifier,
            "title": title, "severity": "P2", "status": status, "state": "In Progress", "app": "example-app", "team": "FFX",
            "source_kind": "agent-report", "env": "Mac", "addressed_by": ",".join(seats), "seats": list(seats),
            "labels": all_labels, "priority": 3, "created_at": "2026-10-01T08:00:00.000Z", "updated_at": updated}


def board_show(identifier: str, *, comments=(), location: str = "Mac, ~/apps/lanes/Example/claude-example @ claude/example",
               provenance: str = "**Filed with the board CLI**\n\n* Reported by CLAUDE", addressed_by: str | None = None) -> dict:
    return {"identifier": identifier, "location": location, "provenance": provenance, "comments": list(comments),
            "addressed_by": addressed_by}


def comment(author: str, text: str, at: str) -> dict:
    return {"id": uuid(99), "author": author, "text": text, "where": "", "sessions": "", "created_at": at}


def write_shim(directory: Path, name: str, source_name: str) -> Path:
    """Copy a fixture program into `directory` as `name`, with this Python as its interpreter."""
    here = Path(__file__).resolve().parent / "fixtures" / "bin" / source_name
    target = Path(directory) / name
    body = here.read_text(encoding="utf-8").replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
    target.write_text(body, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def write_scenario(path: Path, scenario: dict) -> Path:
    Path(path).write_text(json.dumps(scenario), encoding="utf-8")
    return Path(path)


def read_log(path: Path) -> list[list[str]]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
