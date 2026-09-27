#!/usr/bin/env python3
"""Verify every fleet-apps.json app is mentioned in the known registries.

Exits 1 if any required file is missing a repo, acronym, live board, or
DEFAULT_REPOS entry. Run from the AI-Fleet-Coordinator worktree:

    python3 scripts/check-fleet-registry.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_activity_repos import PUBLIC_REPOS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APPS = Path.home() / "apps"
REGISTRY = ROOT / "fleet-apps.json"


def load() -> dict:
    return json.loads(REGISTRY.read_text())


def contains(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    return needle in path.read_text(errors="replace")


def main() -> int:
    data = load()
    apps = data.get("apps", [])
    errors: list[str] = []

    digest = (ROOT / "scripts" / "build-fleet-daily-digest.py").read_text()
    public_repos = set(PUBLIC_REPOS)
    protocol = (ROOT / "EFFORT-LOG-PROTOCOL.md").read_text()
    agent_sync = (ROOT / "AGENT-SYNC.md").read_text()
    live_protocol = APPS / "EFFORT-LOG-PROTOCOL.md"
    live_sync = APPS / "AGENT-SYNC.md"
    live_quick = APPS / "AGENT-COORDINATION-QUICKSTART.md"

    for app in apps:
        repo = app["repo"]
        acronym = app["acronym"]
        board = app["liveBoard"]
        slack = app.get("slackRepo") or repo

        if repo in public_repos and f'"{repo}": (' not in digest:
            errors.append(f"digest REPO_BADGE missing {repo}")
        color = app.get("digestColor") or ""
        badge = app.get("badgeClass") or ""
        if repo in public_repos and color and badge.startswith("repo-"):
            css_var = badge[len("repo-"):]
            needle = f"--{css_var}: {color}"
            if needle not in digest:
                errors.append(
                    f"digest CSS --{css_var} does not match digestColor {color} for {repo}"
                )
        if repo not in protocol and board not in protocol:
            errors.append(f"coordinator EFFORT-LOG-PROTOCOL.md missing {repo} / {board}")
        if slack not in agent_sync and repo not in agent_sync:
            errors.append(f"coordinator AGENT-SYNC.md missing repo {repo} / slack {slack}")
        if acronym not in agent_sync:
            errors.append(f"coordinator AGENT-SYNC.md missing acronym {acronym}")

        if live_protocol.is_file() and repo not in live_protocol.read_text() and board not in live_protocol.read_text():
            errors.append(f"~/apps/EFFORT-LOG-PROTOCOL.md missing {repo} / {board}")
        if live_sync.is_file():
            live = live_sync.read_text()
            if slack.casefold() not in live.casefold() and repo.casefold() not in live.casefold():
                errors.append(f"~/apps/AGENT-SYNC.md missing {repo}")
            if acronym not in live:
                errors.append(f"~/apps/AGENT-SYNC.md missing acronym {acronym}")
        if live_quick.is_file() and repo not in live_quick.read_text() and board not in live_quick.read_text():
            # fleet-infra has no row in the quickstart table on purpose
            if app.get("kind") != "infra":
                errors.append(f"~/apps/AGENT-COORDINATION-QUICKSTART.md missing {repo} / {board}")

        live_board = APPS / board
        if not live_board.is_file():
            errors.append(f"live board missing: {live_board}")

        icon = app.get("iconFile")
        if app.get("hasAppIcon") and icon:
            if not (ROOT / icon).is_file() and not (ROOT / "agent-logos" / Path(icon).name).is_file():
                errors.append(f"missing app icon {icon}")

    colors: dict[str, str] = {}
    for app in apps:
        color = (app.get("digestColor") or "").lower()
        repo = app["repo"]
        if not color:
            continue
        prev = colors.get(color)
        if prev:
            errors.append(f"digestColor {color} reused by {prev} and {repo}")
        else:
            colors[color] = repo

    for wf_name in ("fleet-activity-site.yml", "agent-calendar.yml"):
        wf = ROOT / ".github" / "workflows" / wf_name
        if not wf.is_file():
            errors.append(f"missing .github/workflows/{wf_name}")
            continue
        lists = re.findall(r"FLEET_REPOS:\s*(\S+)", wf.read_text())
        if not lists:
            errors.append(f"{wf_name} missing FLEET_REPOS")
            continue
        for lst in lists:
            configured = set(lst.split(","))
            if configured != public_repos:
                errors.append(f"{wf_name} FLEET_REPOS must match verified public allowlist")

    backup_py = ROOT / "scripts" / "backup-fleet-to-gdrive.py"
    if backup_py.is_file():
        backup_src = backup_py.read_text()
        if "fleet-apps.json" not in backup_src:
            errors.append("scripts/backup-fleet-to-gdrive.py must read fleet-apps.json so new apps are included")
        gha = ROOT / ".github" / "workflows" / "backup-repos.yml"
        if not gha.is_file():
            errors.append("missing .github/workflows/backup-repos.yml (GitHub artifact backup)")
        elif "fleet-apps.json" not in gha.read_text():
            errors.append(".github/workflows/backup-repos.yml must read fleet-apps.json")

    if errors:
        print("fleet registry check FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"fleet registry check OK ({len(apps)} apps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
