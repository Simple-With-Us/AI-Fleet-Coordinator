#!/usr/bin/env python3
"""Verify every fleet-apps.json app is mentioned in the known registries.

Exits 1 if any required file is missing a repo, acronym, live board, or
DEFAULT_REPOS entry.  It also checks the registry's Zulip fields:  the realm and
channel must match the agent-sync CLI, and each seat's bot email and credential
file code must be well formed and agree with the CLI's file-name rule.  Run from
the AI-Fleet-Coordinator worktree:

    python3 scripts/check-fleet-registry.py

Live-board checks compare against a real board checkout, which exists on
operator seats but never on CI runners. With FLEET_BOARD_HOME set, the live
checks run against that directory and a missing board fails the check. Without
it, ~/apps is used when present; when neither exists the live checks are
skipped and only the repository-portable assertions run.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_activity_repos import PUBLIC_REPOS  # noqa: E402
from agent_sync.zulip import DEFAULT_CHANNEL, REALM, credential_file_name  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "fleet-apps.json"

# FLEET_BOARD_HOME opts the live-board checks into an explicit board checkout;
# without it ~/apps is used when it exists (operator seats) and the live
# checks are skipped when it does not (CI runners).
_board_home_env = os.environ.get("FLEET_BOARD_HOME") or ""
APPS = Path(_board_home_env) if _board_home_env else Path.home() / "apps"


def load() -> dict:
    return json.loads(REGISTRY.read_text())


def contains(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    return needle in path.read_text(errors="replace")


# <short name>-bot@<realm host>.  Zulip appends "-bot" to the short name itself.
BOT_EMAIL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-bot@(?P<host>[a-z0-9.-]+)$")


def zulip_errors(data: dict) -> list[str]:
    """Check the registry's Zulip fields against the agent-sync CLI and each other.

    Presence is not required:  GROK-BOT is a set of owner-managed personas with no zuliprc, and
    a retired seat has no bot.  What is present must be well formed.  The bot email cannot be
    derived from the tag (muse-assist-bot@, grok-build-bot@), so it is checked for shape and
    uniqueness only; the file code is derived by the CLI, so it is compared against the CLI.
    """
    errors: list[str] = []
    if data.get("zulipRealm") != REALM:
        errors.append(f"zulipRealm {data.get('zulipRealm')!r} must be {REALM!r} (scripts/agent_sync/zulip.py REALM)")
    if data.get("zulipChannel") != DEFAULT_CHANNEL:
        errors.append(
            f"zulipChannel {data.get('zulipChannel')!r} must be {DEFAULT_CHANNEL!r} "
            "(scripts/agent_sync/zulip.py DEFAULT_CHANNEL)"
        )
    host = urlparse(REALM).hostname or ""
    seats = [s for s in data.get("seats", []) if isinstance(s, dict)]
    tags = [s.get("tag") or "" for s in seats]
    for tag in sorted({t for t in tags if tags.count(t) > 1}):
        errors.append(f"seat {tag or '(no tag)'} appears more than once in fleet-apps.json")
    live = {s["tag"] for s in seats if s.get("tag") and not s.get("retired")}
    emails: dict[str, str] = {}
    for seat in seats:
        tag = seat.get("tag") or "(no tag)"
        email = seat.get("zulipBotEmail")
        code = seat.get("zulipFileCode")
        alias = seat.get("aliasOf")
        if alias is not None:
            if not seat.get("retired"):
                errors.append(f"seat {tag} has aliasOf {alias!r} but is not retired; an alias is always retired")
            if alias not in live:
                errors.append(f"seat {tag} is an alias of {alias!r}, which is not a live seat in fleet-apps.json")
        if seat.get("retired") and (email or code):
            errors.append(f"seat {tag} is retired and must not carry a Zulip bot (retired seats have none)")
        if bool(email) != bool(code):
            errors.append(f"seat {tag} must carry zulipBotEmail and zulipFileCode together, or neither")
        if code and seat.get("tag") and code != credential_file_name(tag).removesuffix("-zuliprc"):
            errors.append(
                f"seat {tag} zulipFileCode {code!r} disagrees with the agent-sync file-name rule "
                f"({credential_file_name(tag).removesuffix('-zuliprc')!r})"
            )
        if email:
            match = BOT_EMAIL_RE.match(email)
            if not match or match.group("host") != host:
                errors.append(f"seat {tag} zulipBotEmail {email!r} must look like <short-name>-bot@{host}")
            elif email.split("@", 1)[0].endswith("-bot-bot"):
                # Zulip appends -bot itself; a short name typed as "codex-bot" made codex-bot-bot@ (2026-10-07).
                errors.append(f"seat {tag} zulipBotEmail {email!r} ends in -bot-bot; the short name must not end in -bot")
            elif email in emails:
                errors.append(f"seat {tag} and seat {emails[email]} share the Zulip bot {email}")
            else:
                emails[email] = tag
    return errors


def engine_only_errors(data: dict, agent_sync: str) -> list[str]:
    """No live seat may sit in AGENT-SYNC.md's "Engine-only CLIs" row (Platform Defaults).

    That row lists the CLIs that are not seats.  Owner 2026-10-10 made OpenCode a seat, and a merge that
    keeps the old row would put it back (two PRs edit that row).  A retired seat may be listed.
    """
    row = re.search(r"\*\*Engine-only CLIs\*\*\s*\(([^)]*)\)", agent_sync)
    if not row:
        return []
    errors: list[str] = []
    for seat in data.get("seats", []):
        if not isinstance(seat, dict) or seat.get("retired"):
            continue
        for name in sorted({seat.get("tag") or "", seat.get("notesName") or ""} - {""}):
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", row.group(1), re.IGNORECASE):
                errors.append(
                    f"AGENT-SYNC.md lists live seat {seat.get('tag')} ({name}) in its Engine-only CLIs row; "
                    "a seat is not an engine-only CLI"
                )
                break
    return errors


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
    live_checks = APPS.is_dir() if _board_home_env else (APPS / "EFFORT-LOG-PROTOCOL.md").is_file()
    if _board_home_env and not live_checks:
        errors.append(f"FLEET_BOARD_HOME is not a directory: {APPS}")
    elif not live_checks:
        print(f"note: live board checks skipped (no FLEET_BOARD_HOME, {APPS} absent or not a board checkout)")

    for app in apps:
        repo = app["repo"]
        acronym = app["acronym"]
        board = app["liveBoard"]

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
        if repo not in agent_sync:
            errors.append(f"coordinator AGENT-SYNC.md missing repo {repo}")
        if acronym not in agent_sync:
            errors.append(f"coordinator AGENT-SYNC.md missing acronym {acronym}")

        if live_protocol.is_file() and repo not in live_protocol.read_text() and board not in live_protocol.read_text():
            errors.append(f"~/apps/EFFORT-LOG-PROTOCOL.md missing {repo} / {board}")
        if live_sync.is_file():
            live = live_sync.read_text()
            if repo.casefold() not in live.casefold():
                errors.append(f"~/apps/AGENT-SYNC.md missing {repo}")
            if acronym not in live:
                errors.append(f"~/apps/AGENT-SYNC.md missing acronym {acronym}")
        if live_quick.is_file() and repo not in live_quick.read_text() and board not in live_quick.read_text():
            # fleet-infra has no row in the quickstart table on purpose
            if app.get("kind") != "infra":
                errors.append(f"~/apps/AGENT-COORDINATION-QUICKSTART.md missing {repo} / {board}")

        if live_checks:
            live_board = APPS / board
            if not live_board.is_file():
                errors.append(f"live board missing: {live_board}")

        icon = app.get("iconFile")
        if app.get("hasAppIcon") and icon:
            if not (ROOT / icon).is_file() and not (ROOT / "agent-logos" / Path(icon).name).is_file():
                errors.append(f"missing app icon {icon}")

    errors.extend(zulip_errors(data))
    errors.extend(engine_only_errors(data, agent_sync))

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

    # The admin panel inlines its own copy of the registry because the Worker has
    # no filesystem, and nothing else compared the two.  That drift is invisible
    # until the panel goes red: a renamed repo answers GitHub with a 301, the
    # Worker refuses to follow redirects (correctly — it must not leak auth
    # headers cross-origin), and the row silently loses its CI status forever.
    # Socratic-Trade shipped as `Socratic.Trade` and did exactly that.
    panel = ROOT / "scripts" / "admin-panel" / "src" / "index.js"
    if panel.is_file():
        panel_src = panel.read_text(errors="replace")
        block = re.search(r"const APPS = \[(.*?)\n\];", panel_src, re.S)
        if not block:
            errors.append("admin-panel src/index.js has no `const APPS = [ ... ];` block")
        else:
            panel_repos = set(re.findall(r"repo:\s*'([^']+)'", block.group(1)))
            registry_repos = {a["repo"] for a in apps}
            for repo in sorted(registry_repos - panel_repos):
                errors.append(f"admin-panel APPS missing repo {repo}")
            for repo in sorted(panel_repos - registry_repos):
                errors.append(f"admin-panel APPS has repo {repo}, which is not in fleet-apps.json")
    else:
        errors.append("missing scripts/admin-panel/src/index.js")

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
