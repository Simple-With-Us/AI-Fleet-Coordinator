#!/usr/bin/env python3
"""The decision behind the fleet build shim (shims/build).  Exit 77 means block;  every other exit runs the real tool.

    build_shim.py TOOL <the argv TOOL was given>

shims/build is linked as `xcodebuild`, `swift`, `cargo`, `npm`, `pnpm`, `yarn` and `bun`, and calls this only when the
command could write into system temp:  the working directory is in temp, an argument names a temp folder, or
CARGO_TARGET_DIR does.  Output is judged by guard.evaluate_build_argv, the same rule the hook applies to an agent's typed
command (the temp-build class):  build output in $TMPDIR, /private/tmp, /tmp, /var/tmp or /var/folders, outside the session
scratchpad /private/tmp/claude-<uid>/, which is excused, and outside an in-date temp-build or package-temp row of
placement-exceptions.json (Homebrew and pip build in temp folders they remove themselves).

The mode file decides:  `detect` (the default) logs to lane-guard-events.jsonl and prints nothing, because callers parse a
build's output, and only `block` or `block temp-build` refuses, with a reason naming the lane-local or cache folder to use
instead.  It fails open exactly as the git shim does:  a crash, an unreadable file or more than TIME_LIMIT_S seconds runs the
real tool, with a fail-open event in the log.

Not yet linked by install_tools:  the owner turns it on by linking shims/build under those names in a PATH folder ahead of
the real tools.  Python 3.9 safe.  Tests: fleet_lanes/tests/test_build_shim.py.
"""
from __future__ import annotations

import os
import signal
import sys
from collections.abc import Mapping, Sequence

BLOCK_EXIT = 77
ENV_DENY_NOTE = "FLEET_BUILD_SHIM_DENY_NOTE"
TIME_LIMIT_S = 3


def decide(tool: str, args: Sequence[str], cwd: str, env: Mapping[str, str], *, stderr=None) -> int:
    """The exit code for this build.  Logs anything temp-build would block, and writes the reason when it does block."""
    from fleet_lanes import git_shim, guard, placement, temp_build

    stderr = sys.stderr if stderr is None else stderr
    if tool not in (*temp_build.TOOLS, "xcrun"):
        return 0
    env = git_shim.with_fleet_roots(env)
    home = env.get("HOME") or os.path.expanduser("~")
    mode = placement.read_mode(home, env)
    if mode.mode == placement.MODE_OFF:
        return 0
    ctx = guard.make_context(env, home=home)
    verdict = guard.evaluate_build_argv(tool, list(args), cwd, ctx, env=env)
    if verdict is None:
        return 0
    cls, decision = verdict
    blocked = cls != guard.CLASS_EXCUSED and mode.blocks(cls)
    event = {
        "via": "build-shim", "action": "excused" if cls == guard.CLASS_EXCUSED else "block" if blocked else "detect",
        "class": cls, "rule": decision.rule_id, "dest": decision.destination, "source": decision.source, "cwd": cwd,
        "seat": env.get("AGENT_SEAT", ""), "host": placement.host_kind(env),
    }
    if ctx.exceptions_problem:
        event["exceptions"] = "unusable"
    placement.log_event(home, event, env)
    if not blocked:
        return 0
    note = env.get(ENV_DENY_NOTE)
    try:
        if note:
            with open(note, "w", encoding="utf-8") as fh:
                fh.write(f"error: {decision.reason}\n")
        else:
            stderr.write(f"error: {decision.reason}\n")
    except (OSError, ValueError, AttributeError):
        pass
    return BLOCK_EXIT


def _alarm(_signum, _frame):
    """Out of time:  run the real tool.  The trace goes through log_event's test guard, and a second alarm kills a hung write."""
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(1)
    try:
        from fleet_lanes import placement

        placement.log_event(os.environ.get("HOME") or os.path.expanduser("~"),
                            {"via": "build-shim", "action": "fail-open", "detail": "time-limit"})
    finally:
        os._exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    previous = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(TIME_LIMIT_S)
        here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        if here not in sys.path:
            sys.path.insert(0, here)
        return decide(args[0], args[1:], os.getcwd(), os.environ)
    except Exception as exc:  # noqa: BLE001 - the shim must never stop a build
        try:
            from fleet_lanes import placement

            home = os.environ.get("HOME") or os.path.expanduser("~")
            placement.log_event(home, {"via": "build-shim", "action": "fail-open", "detail": type(exc).__name__})
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    sys.exit(main())
