#!/usr/bin/env python3
"""Install the iOS Simulator idle reaper and the Claude Code turn-end hook into this Mac's home.

  python3 scripts/install-sim-idle-reaper.py --dry-run     # show the plan, change nothing
  python3 scripts/install-sim-idle-reaper.py               # install or update (idempotent)

What it does, from a checkout of AI-Fleet-Coordinator:
  1. copies scripts/sim-idle-reaper.py to ~/apps/sim-idle-reaper.py;
  2. copies scripts/hooks/sim-session-hook.{py,sh} to ~/.claude/hooks/;
  3. registers the hook in ~/.claude/settings.json for PreToolUse, PostToolUse and
     PostToolUseFailure (matcher Bash|mcp__Claude_Code_iOS_Simulator__.*) and for Stop,
     SubagentStop and SessionEnd.  A backup settings.json.bak-sim-reaper-<stamp> is written
     first.  An entry is ours only when its command equals HOOK_COMMAND exactly;  every other
     key and hook is kept, and nothing else in the file is printed (commands can carry
     credentials).  A settings.json that does not parse is never rewritten;
  4. copies scripts/launchd/com.jay.sim-idle-reaper.plist to ~/Library/LaunchAgents/ and, when it
     changed or is not loaded, boots it out and bootstraps it (RunAtLoad is false, so this never
     shuts a simulator down by itself).
A replaced file is kept as <file>.bak-sim-reaper-<stamp>.  --skip-hooks and --skip-launchd leave
those parts alone.  The live mac-process-watch.sh is not touched here;  copy it from the same
checkout (it lists com.jay.sim-idle-reaper in expect_scheduled).
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time

REPO_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
STAMP = time.strftime("%Y%m%d-%H%M%S")
LABEL = "com.jay.sim-idle-reaper"
HOOKS_DIR = os.path.join(HOME, ".claude", "hooks")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
HOOK_COMMAND = f"/bin/sh {HOOKS_DIR}/sim-session-hook.sh || true"
TOOL_MATCHER = "Bash|mcp__Claude_Code_iOS_Simulator__.*"
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
END_EVENTS = ("Stop", "SubagentStop", "SessionEnd")

FILES = [
    (os.path.join(REPO_SCRIPTS, "sim-idle-reaper.py"), os.path.join(HOME, "apps", "sim-idle-reaper.py"), 0o755),
    (os.path.join(REPO_SCRIPTS, "hooks", "sim-session-hook.py"), os.path.join(HOOKS_DIR, "sim-session-hook.py"), 0o755),
    (os.path.join(REPO_SCRIPTS, "hooks", "sim-session-hook.sh"), os.path.join(HOOKS_DIR, "sim-session-hook.sh"), 0o755),
]
PLIST_SRC = os.path.join(REPO_SCRIPTS, "launchd", f"{LABEL}.plist")
PLIST_DST = os.path.join(HOME, "Library", "LaunchAgents", f"{LABEL}.plist")


def say(msg: str) -> None:
    print(f"install-sim-idle-reaper: {msg}")


def copy(src: str, dst: str, mode: int, dry: bool) -> bool:
    """Copy src over dst when they differ.  Returns True when dst changed (or would)."""
    if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
        say(f"unchanged {dst}")
        return False
    if dry:
        say(f"would {'update' if os.path.exists(dst) else 'create'} {dst}")
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        shutil.copy2(dst, f"{dst}.bak-sim-reaper-{STAMP}")
    shutil.copy2(src, dst)
    os.chmod(dst, mode)
    say(f"installed {dst}")
    return True


def our_group(event: str) -> dict:
    hook = {"type": "command", "command": HOOK_COMMAND, "timeout": 5}
    if event in TOOL_EVENTS:
        return {"matcher": TOOL_MATCHER, "hooks": [hook]}
    return {"hooks": [hook]}


def has_ours(groups) -> bool:
    for g in groups if isinstance(groups, list) else []:
        for h in (g.get("hooks") if isinstance(g, dict) else None) or []:
            if isinstance(h, dict) and h.get("command") == HOOK_COMMAND:
                return True
    return False


def register_hooks(dry: bool) -> None:
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            text = fh.read()
        doc = json.loads(text)
    except FileNotFoundError:
        text, doc = "", {}
    except ValueError:
        say(f"skipped {SETTINGS}: not valid JSON, left untouched")
        return
    if not isinstance(doc, dict):
        say(f"skipped {SETTINGS}: not a JSON object")
        return
    hooks = doc.setdefault("hooks", {})
    added = []
    for event in TOOL_EVENTS + END_EVENTS:
        groups = hooks.get(event)
        if has_ours(groups):
            continue
        if not isinstance(groups, list):
            groups = hooks[event] = []
        groups.append(our_group(event))
        added.append(event)
    if not added:
        say(f"unchanged {SETTINGS} (hook already registered for all six events)")
        return
    if dry:
        say(f"would register the hook in {SETTINGS} for: {', '.join(added)}")
        return
    if text:
        shutil.copy2(SETTINGS, f"{SETTINGS}.bak-sim-reaper-{STAMP}")
    new = json.dumps(doc, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
    tmp = f"{SETTINGS}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.replace(tmp, SETTINGS)
    say(f"registered the hook in {SETTINGS} for: {', '.join(added)} (backup .bak-sim-reaper-{STAMP})")


def launchd(changed: bool, dry: bool) -> None:
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
    if loaded and not changed:
        say(f"unchanged {LABEL} (loaded)")
        return
    if dry:
        say(f"would {'reload' if loaded else 'bootstrap'} {LABEL}")
        return
    if loaded:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        time.sleep(1)
    rc = subprocess.run(["launchctl", "bootstrap", domain, PLIST_DST]).returncode
    say(f"bootstrap {LABEL} rc={rc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install the simulator idle reaper and Claude hook.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-hooks", action="store_true")
    ap.add_argument("--skip-launchd", action="store_true")
    a = ap.parse_args()
    if sys.platform != "darwin":
        say("macOS only")
        return 1
    for src, dst, mode in FILES:
        if a.skip_hooks and dst.startswith(HOOKS_DIR):
            continue
        copy(src, dst, mode, a.dry_run)
    if not a.skip_hooks:
        register_hooks(a.dry_run)
    if not a.skip_launchd:
        changed = copy(PLIST_SRC, PLIST_DST, 0o644, a.dry_run)
        launchd(changed, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
