#!/usr/bin/env python3
"""Flag seat blocks that break the owner's seat-precedence rule (AGENT-SYNC § Identity Rules, 2026-10-09).

A skill or rules file must never stamp its platform seat over a seat a launcher assigned:  BotFleet runs
Claude, Codex and other CLIs as engines for its own bots, and those engines load these files.  Flags:

  bare-export     `export AGENT_SEAT=<TAG>`, which overwrites a seat that is already set
  launcher-write  a write to AGENT_LAUNCH_SEAT or AGENT_LAUNCHER (only a launcher writes them)
  no-clause       a session-start skill or rules file with no launcher clause
  retired-seat    a retired seat (MONET, RENOIR, HARNESS, DSH, KIMI, GROK-BUILD) pinned as the reader's own

    python3 scripts/check-seat-blocks.py            # the repo's rendered trees (CI runs this)
    python3 scripts/check-seat-blocks.py --home     # also the owner's rules files and skill homes (read-only)
    python3 scripts/check-seat-blocks.py PATH ...   # any files or directories

Exit 0 clean, 1 on any finding.  It reads files only and prints paths and line numbers, never contents.
Fix a repo finding by re-rendering (`python3 scripts/install-fleet-skills.py --repo-only`).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_TREES = (
    "skills",
    "docs/fleet-skills/by-seat",
    ".claude/skills",
    ".cursor/skills",
    ".grok/skills",
)
# The owner's rules files and skill homes (design section 2).  Missing ones are skipped.
HOME_FILES = (
    "~/.claude/CLAUDE.md", "~/AGENTS.md", "~/.codex/AGENTS.md", "~/.cursor/rules/agent-sync-zulip.mdc",
    "~/.gemini/config/AGENTS.md", "~/.grok/GROK.md", "~/.fx/AGENTS.md", "~/.minimax/AGENTS.md",
    "~/.minimax/memory/user.md", "~/.clutch/dsh/AGENTS.md", "~/.kimi-code/AGENTS.md", "~/.vibe/AGENTS.md",
    "~/.copilot/copilot-instructions.md",
)
HOME_TREES = (
    "~/.claude/skills", "~/.codex/skills", "~/.cursor/skills", "~/.gemini/skills", "~/.grok/skills",
    "~/.fx/skills", "~/.minimax/skills", "~/.config/muse/skills", "~/.clutch/dsh/skills",
)
# Catalog copies of retired seats are history, not anyone's working pack.
RETIRED_CATALOG = {"monet", "renoir", "deepseek", "kimi", "grok-build"}
RETIRED_SEATS = ("MONET", "RENOIR", "HARNESS", "DSH", "KIMI", "GROK-BUILD")

BARE_EXPORT = re.compile(r"(?m)^\s*export\s+AGENT_SEAT=(?![\"']?\$)[A-Za-z0-9<]")
LAUNCHER_WRITE = re.compile(r"(?m)(?:^|[;&|(]\s*|\bexport\s+|\benv\s+)(AGENT_LAUNCH_SEAT|AGENT_LAUNCHER)=")
# The launcher clause:  the rendered sentence, or the design's seat block (which reads AGENT_LAUNCH_SEAT).
CLAUSE = re.compile(r"Never write `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`|\$\{AGENT_LAUNCH_SEAT:-\}")
RETIRED_PIN = re.compile(r"(?m)^\s*export\s+AGENT_SEAT=[\"']?(%s)\b" % "|".join(re.escape(s) for s in RETIRED_SEATS))


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_text(path: str, text: str, *, needs_clause: bool, retired_ok: bool = False) -> list[str]:
    found: list[str] = []
    for match in BARE_EXPORT.finditer(text):
        found.append("%s:%d: bare-export (a bare `export AGENT_SEAT=<TAG>` overwrites a launcher's seat)"
                     % (path, _line(text, match.start())))
    for match in LAUNCHER_WRITE.finditer(text):
        found.append("%s:%d: launcher-write (%s is written only by a launcher)"
                     % (path, _line(text, match.start()), match.group(1)))
    if not retired_ok:
        for match in RETIRED_PIN.finditer(text):
            found.append("%s:%d: retired-seat (%s is retired and is no one's seat)"
                         % (path, _line(text, match.start()), match.group(1)))
    if needs_clause and "AGENT_SEAT" in text and not CLAUSE.search(text):
        found.append("%s: no-clause (names AGENT_SEAT but has no launcher clause)" % path)
    return found


def _is_retired_catalog(path: Path) -> bool:
    parts = path.parts
    return "by-seat" in parts and parts[parts.index("by-seat") + 1] in RETIRED_CATALOG


def check_path(path: Path, *, rules_file: bool = False) -> list[str]:
    found: list[str] = []
    files = [path] if path.is_file() else sorted(path.rglob("SKILL.md")) if path.is_dir() else []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        retired = _is_retired_catalog(file)
        stop_only = retired and "## 1. Identity" not in text
        needs_clause = rules_file or (file.parent.name == "session-start" and not stop_only)
        try:
            shown = str(file.relative_to(ROOT))
        except ValueError:
            shown = str(file)
        found += check_text(shown, text, needs_clause=needs_clause, retired_ok=retired)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", help="files or directories (default: the repo's rendered trees)")
    parser.add_argument("--home", action="store_true", help="also check the owner's rules files and skill homes")
    args = parser.parse_args(argv)
    found: list[str] = []
    if args.paths:
        for raw in args.paths:
            found += check_path(Path(raw))
    else:
        for tree in REPO_TREES:
            found += check_path(ROOT / tree)
    if args.home:
        for raw in HOME_FILES:
            path = Path(os.path.expanduser(raw))
            if path.is_file():
                found += check_path(path, rules_file=True)
        for raw in HOME_TREES:
            path = Path(os.path.expanduser(raw))
            if path.is_dir():
                found += check_path(path)
    for line in found:
        print(line)
    if found:
        print("check-seat-blocks: %d finding%s.  Repo trees:  python3 scripts/install-fleet-skills.py --repo-only"
              % (len(found), "" if len(found) == 1 else "s"), file=sys.stderr)
        return 1
    print("check-seat-blocks: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
