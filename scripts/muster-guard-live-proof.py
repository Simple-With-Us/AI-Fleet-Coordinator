#!/usr/bin/env python3
"""Prove that the installed MiniMax Code guard actually fires, and say why when it does not.

A guard can be installed, enabled, parsed, registered, correct, and still protect nothing.
This tool separates the three things that are usually confused:

  1. matcher   - does the plugin's matcher name the tool the runtime really calls?
  2. policy    - does the installed hook script deny the canary when it is run directly?
  3. dispatch  - does the MiniMax runtime actually call the hook in a live session?

Only the third is the proof.  The first two can both be green while the seat runs with no
protection at all, which is exactly the failure this was written for: the MiniMax Code
runtime (3.1.1) delivers its shell tool as `bash`, and a plugin whose matcher read `Bash`
never fires, silently.

The canary is `ps > /dev/null`.  The sensitive guard denies it, so when the guard works the
command never runs; when the guard is inert the command runs and prints nothing, so the
canary is harmless either way and leaks no process list into a report.

Usage:
    muster-guard-live-proof.py [--plugin-dir DIR] [--model M] [--json] [--skip-live]

    --plugin-dir   the installed plugin directory (default ~/.minimax/plugins/fleet-guard)
    --model        the model for the live session (default minimax/MiniMax-M3.1-Flash)
    --skip-live    run the matcher and policy checks only, no MiniMax session
    --json         print the report as JSON

Exit code is 0 when the live dispatch denies the canary, 1 when it ran and did not deny, and 2
when the check could not be run at all — a missing mcode, a timeout, an unreadable plugin.  The
third case proves nothing about the guard: reporting it as a failure would point the operator at
the installer when the guard was never exercised.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_PLUGIN_DIR = Path.home() / ".minimax" / "plugins" / "fleet-guard"
DEFAULT_MODEL = "minimax/MiniMax-M3.1-Flash"

# The canary, and the command that must be handed to the hook.
CANARY = "ps > /dev/null"

# The tool names the MiniMax runtime has been observed to deliver.  The Mac 3.1.1 runtime
# delivers lowercase `bash`; the 0.6.12 server runtime adapts a native shell call to `Bash`
# for a compatible plugin.  A matcher has to cover both shell spellings to prove anything
# here, because the policy stage drives a shell canary.  The file spellings are reported for
# information: a plugin that also polices file tools wants them, a shell-only plugin does not.
SHELL_NAMES = ("bash", "Bash")
FILE_NAMES = ("Read", "Write", "Edit", "read", "write", "edit")

# A stage result carries an explicit status, so an operational failure is never read as a
# negative result.  `ok: False` means the stage ran and the guard failed the check.
# `ok: None` with status `error` means the stage could not run at all, which proves nothing
# either way — a different thing, with a different exit code.
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

# The hook's own deny reason recommends pgrep, so a session that shows it was the hook and
# not the model's own reluctance.  A model that declines to run ps will not say pgrep.
DENY_MARKERS = ("pgrep", "permissionDecision")


def _matcher_covers(matcher: str, names) -> bool:
    """True when the matcher covers every name in `names`.

    A matcher is a regex in the Claude hook contract, so it is matched case sensitively
    here: the fleet plugins use exact names joined by `|`, and a case-insensitive reading
    would hide the very drift this tool exists to catch.
    """
    if matcher in ("", None):
        return False
    try:
        pattern = re.compile(matcher)
    except re.error:
        return False
    return all(pattern.search(name) for name in names)


def _covered_names(matcher: str, names):
    """The subset of `names` the matcher covers, and the subset it misses.

    Coverage is per name, never per group: a matcher of `Bash` covers `Bash` and misses
    `bash`, and reporting the whole shell group as missed would hide that it does cover
    one of them.
    """
    if matcher in ("", None):
        return [], list(names)
    try:
        pattern = re.compile(matcher)
    except re.error:
        return [], list(names)
    covered, missed = [], []
    for name in names:
        (covered if pattern.search(name) else missed).append(name)
    return covered, missed


def _stage_error(why: str) -> dict:
    """A stage that could not be evaluated.

    Deliberately not `ok: False`.  A stage that could not run has not proved anything
    either way, and reporting it as a failure sends the operator to fix the guard when
    the guard was never exercised.
    """
    return {"ok": None, "status": STATUS_ERROR, "why": why}


def _stage_fail(why: str, **extra) -> dict:
    """A stage that ran, and the check failed."""
    return {"ok": False, "status": STATUS_FAIL, "why": why, **extra}


def _stage_ok(why: str, **extra) -> dict:
    """A stage that ran, and the check passed."""
    return {"ok": True, "status": STATUS_OK, "why": why, **extra}


def read_plugin(plugin_dir: Path) -> dict:
    """The manifest and hook entries the plugin declares."""
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    hooks_path = plugin_dir / "hooks" / "hooks.json"
    report = {"plugin_dir": str(plugin_dir), "manifest": None, "hooks": [], "errors": []}
    if not manifest_path.is_file():
        report["errors"].append("missing %s" % manifest_path)
    else:
        try:
            report["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report["errors"].append("manifest is not readable JSON: %s" % exc)
    if not hooks_path.is_file():
        report["errors"].append("missing %s" % hooks_path)
        return report
    try:
        doc = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report["errors"].append("hooks.json is not readable JSON: %s" % exc)
        return report
    for event, entries in (doc.get("hooks") or {}).items():
        for entry in entries or []:
            for handler in entry.get("hooks") or []:
                report["hooks"].append(
                    {
                        "event": event,
                        "matcher": entry.get("matcher"),
                        "command": handler.get("command"),
                        "timeout": handler.get("timeout"),
                        "type": handler.get("type"),
                    }
                )
    return report


def check_matcher(plugin: dict) -> dict:
    """Stage 1: does any PreToolUse matcher cover the names the runtime delivers?

    The shell names are the requirement.  This tool's policy stage proves the guard
    denies a shell canary, so a matcher that covers every shell name is a matcher that
    can prove it; demanding the file spellings as well would fail a perfectly valid
    shell-only plugin.  File-tool coverage is still reported, because a guard that also
    wants to police file tools needs those names and the reader should be able to see
    whether it has them.
    """
    pre = [h for h in plugin["hooks"] if h["event"] == "PreToolUse"]
    if not pre:
        return _stage_fail("no PreToolUse handler is registered")
    best = None
    for handler in pre:
        matcher = handler["matcher"]
        shell_covered, shell_missed = _covered_names(matcher, SHELL_NAMES)
        file_covered, file_missed = _covered_names(matcher, FILE_NAMES)
        ok = not shell_missed
        if best is None or ok:
            best = {
                "ok": ok,
                "status": STATUS_OK if ok else STATUS_FAIL,
                "matcher": matcher,
                "shell_covered": shell_covered,
                "shell_missed": shell_missed,
                "file_covered": file_covered,
                "file_missed": file_missed,
                "why": (
                    "the matcher covers every shell tool name the runtime delivers"
                    if ok
                    else "the matcher misses %s" % ", ".join(shell_missed)
                ),
            }
        if ok:
            break
    return best


def _canary_payload(tool_name: str) -> str:
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": {"command": CANARY + " "},
            "hook_event_name": "PreToolUse",
            "session_id": "muster-guard-live-proof",
            "cwd": str(Path.home()),
            "permission_mode": "bypassPermissions",
        }
    )


def check_policy(plugin: dict) -> dict:
    """Stage 2: run the installed hook command directly with the canary payload."""
    handler = next(
        (h for h in plugin["hooks"] if h["event"] == "PreToolUse" and h.get("command")),
        None,
    )
    if handler is None:
        return _stage_fail("no PreToolUse command to run")
    command = shlex.split(handler["command"])
    denied = False
    detail = ""
    for tool_name in SHELL_NAMES:
        try:
            proc = subprocess.run(
                command,
                input=_canary_payload(tool_name),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _stage_error("could not run the hook command: %s" % exc)
        out = (proc.stdout or "") + (proc.stderr or "")
        if "deny" in out:
            denied = True
            detail = out.strip()[:400]
            break
    return {
        "ok": denied,
        "status": STATUS_OK if denied else STATUS_FAIL,
        "why": "the hook denied the canary when run directly"
        if denied
        else "the hook allowed the canary when run directly",
        "command": handler["command"],
        "detail": detail,
    }


def check_dispatch(model: str, timeout: int) -> dict:
    """Stage 3: run a real MiniMax session and see whether the guard stops the canary.

    The session output is never printed.  It can contain the runtime's own transcript, and
    this tool's job is a verdict, not a dump.
    """
    if not shutil.which("mcode"):
        return _stage_error("mcode is not on PATH, so no live session can be run")
    prompt = (
        "Run this one shell command exactly as written, with no changes: %s\n"
        "Then say what happened." % CANARY
    )
    with tempfile.TemporaryDirectory(prefix="muster-proof-") as tmp:
        out_path = os.path.join(tmp, "session.txt")
        cmd = [
            "mcode",
            "exec",
            "--model",
            model,
            "--permission",
            "off",
            prompt,
        ]
        try:
            with open(out_path, "w", encoding="utf-8") as handle:
                proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return _stage_error("the live session timed out after %ds" % timeout)
        except OSError as exc:
            return _stage_error("could not start the live session: %s" % exc)
        try:
            text = Path(out_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _stage_error("could not read the session output: %s" % exc)
    markers = [m for m in DENY_MARKERS if m in text]
    return {
        "ok": bool(markers),
        "status": STATUS_OK if markers else STATUS_FAIL,
        "why": "the live session was refused by the guard (%s)" % ", ".join(markers)
        if markers
        else "the live session was not refused by the guard, so the hook never fired",
        "exit_code": proc.returncode,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", default=str(DEFAULT_PLUGIN_DIR))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    plugin = read_plugin(Path(args.plugin_dir))
    report = {
        "plugin": plugin,
        "stages": {},
    }
    if plugin["errors"]:
        unreadable = "the plugin could not be read: %s" % "; ".join(plugin["errors"])
        report["stages"]["matcher"] = _stage_error(unreadable)
        report["stages"]["policy"] = _stage_error(unreadable)
        report["stages"]["dispatch"] = _stage_error(unreadable)
    else:
        report["stages"]["matcher"] = check_matcher(plugin)
        report["stages"]["policy"] = check_policy(plugin)
        if args.skip_live:
            report["stages"]["dispatch"] = {
                "ok": None,
                "status": STATUS_SKIPPED,
                "why": "skipped (--skip-live): the live dispatch was not proven",
            }
        else:
            report["stages"]["dispatch"] = check_dispatch(args.model, args.timeout)

    dispatch = report["stages"]["dispatch"].get("ok")
    dispatch_status = report["stages"]["dispatch"].get("status")
    if dispatch is True:
        report["verdict"] = "PROVED: the installed guard fired and denied the canary"
        report["ok"] = True
    elif dispatch is False:
        report["verdict"] = "NOT PROVED: the guard did not fire in a live session"
        report["ok"] = False
    elif dispatch_status == STATUS_SKIPPED:
        report["verdict"] = "INCOMPLETE: the live dispatch was skipped, so the guard was not proven"
        report["ok"] = False
    else:
        # The check could not be run.  Say so plainly: this is not a statement about
        # the guard, and the remediation for a broken tool is not a fix in the installer.
        report["verdict"] = "INCOMPLETE: the live dispatch could not run: %s" % report["stages"][
            "dispatch"
        ].get("why", "no reason given")
        report["ok"] = False

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Plugin: %s" % plugin["plugin_dir"])
        for hook in plugin["hooks"]:
            print("  %s matcher=%r command=%s" % (hook["event"], hook["matcher"], hook["command"]))
        for name in ("matcher", "policy", "dispatch"):
            stage = report["stages"][name]
            flag = {
                STATUS_OK: "ok",
                STATUS_FAIL: "FAIL",
                STATUS_ERROR: "ERROR",
                STATUS_SKIPPED: "skip",
            }.get(stage.get("status"), "?")
            print("[%s] %-8s %s" % (flag, name, stage.get("why", "")))
        print(report["verdict"])
        if report["verdict"].startswith("NOT PROVED"):
            print()
            print("Where to fix it:")
            print("  the matcher is read in install_tools.py (render_minimax_hooks); a")
            print("  hand edit of the installed plugin is reverted by the next")
            print("  `apply minimax`, so the installer is the place to change it.")
    if report["ok"]:
        return 0
    # 1 means the check ran and the guard failed it; 2 means the check could not run.
    return 1 if report["stages"]["dispatch"].get("ok") is False else 2


if __name__ == "__main__":
    sys.exit(main())
