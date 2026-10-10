#!/usr/bin/env python3
"""PreToolUse hook: deny fleet-repo checkouts (clones, worktrees, extracted archives) in temp dirs.

Reads the hook JSON on stdin, asks `fleet_lanes.guard.evaluate`, and on a deny prints the
platform's deny JSON and exits 0.  On an allow it prints nothing and exits 0.  Anything
unexpected (bad JSON, an import error, a crash) is an allow: exit 0, no output.

    python3 /abs/path/scripts/fleet_lanes/lane_guard_hook.py [--format FMT] [--exit2]
    cd scripts && python3 -m fleet_lanes.lane_guard_hook [--format FMT] [--exit2]

Formats (default claude):
  claude       {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "deny", "permissionDecisionReason": REASON}}
               Codex accepts the same shape; `codex` is an alias.
  grok         {"decision": "deny", "reason": REASON}
  antigravity  same as grok (hook matcher run_command); the payload fields it sends are
               UNVERIFIED, so the guard also reads tool_input.CommandLine and Cwd
  cursor       {"permission": "deny", "user_message": SHORT, "agent_message": REASON}
               for beforeShellExecution, which sends command, cwd and workspace_roots
  muse         UNVERIFIED: mirrors claude
  kimi         the exit-2 contract below (Kimi Code documents exit code 2 with the reason on stderr)
--exit2 is the plain-text contract: on a deny the reason goes to stderr and the exit code is 2,
with nothing on stdout.

Tests: fleet_lanes/tests/test_guard.py (HookCliTests).
"""
from __future__ import annotations

import json
import os
import sys
from typing import IO, Sequence

FORMATS = ("claude", "codex", "grok", "antigravity", "cursor", "muse", "kimi")
EXIT2_FORMATS = ("kimi",)       # formats that are the plain-text exit-2 contract, whatever flags come with them
DEFAULT_FORMAT = "claude"


def _bootstrap() -> None:
    """Make `fleet_lanes` importable when this file is run by path (sys.path[0] is then the
    fleet_lanes directory itself, not scripts/)."""
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def _parse_args(argv: Sequence[str]) -> tuple[str, bool]:
    """--format FMT, --format=FMT and --exit2.  Never exits: an unknown format or flag falls back
    to the defaults, because a hook that dies on its own arguments must not block the tool."""
    fmt = DEFAULT_FORMAT
    exit2 = False
    args = list(argv)
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--exit2":
            exit2 = True
        elif a == "--format" and i < len(args):
            fmt = args[i]
            i += 1
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
    fmt = fmt.strip().lower()
    fmt = fmt if fmt in FORMATS else DEFAULT_FORMAT
    return fmt, exit2 or fmt in EXIT2_FORMATS


def render(reason: str, fmt: str, destination: str = "") -> str:
    """The deny JSON for one platform, as one line."""
    if fmt in ("grok", "antigravity"):
        body: dict = {"decision": "deny", "reason": reason}
    elif fmt == "cursor":
        short = "Blocked a fleet-repo checkout in a temp directory"
        short += f": {destination}." if destination else "."
        body = {"permission": "deny", "user_message": short, "agent_message": reason}
    else:  # claude, codex, muse
        body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    return json.dumps(body)


def emit_deny(reason: str, fmt: str, destination: str, exit2: bool, stdout: IO, stderr: IO) -> int:
    """Write one deny in the platform's contract and return the exit code.  Never raises: a hook that cannot
    write its answer allows (exit 0)."""
    try:
        if exit2:
            stderr.write(reason + "\n")
            stderr.flush()
            return 2
        stdout.write(render(reason, fmt, destination) + "\n")
        stdout.flush()
    except Exception:
        return 0
    return 0


def _read_stdin(stdin: IO) -> str:
    buf = getattr(stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8", "replace")
    return stdin.read()


def main(argv: Sequence[str] | None = None, stdin: IO | None = None, stdout: IO | None = None,
         stderr: IO | None = None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        fmt, exit2 = _parse_args(sys.argv[1:] if argv is None else argv)
        payload = json.loads(_read_stdin(stdin))
        try:
            from fleet_lanes import guard
        except ImportError:
            _bootstrap()
            from fleet_lanes import guard
        decision = guard.evaluate(payload)
    except Exception:
        return 0
    if decision.action != guard.DENY or not decision.reason:
        return 0
    return emit_deny(decision.reason, fmt, decision.destination, exit2, stdout, stderr)


if __name__ == "__main__":
    sys.exit(main())
