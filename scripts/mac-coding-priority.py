#!/usr/bin/env python3
"""Run ONE explicitly named coding build/test job at reduced background priority.

Why this exists (AFC-609, 2026-11-03 review):
the Mac's native taskpolicy can give a command a lower background scheduling
and I/O priority.  The reviewed alternative, App Tamer, needs beta software, a
privileged helper, one administrator authorisation and a paid licence, and it
caps RUNNING apps -- which would put the foreground app and every live session
at risk.  This helper takes the native route and keeps it inside the one
boundary the review drew: it lowers the priority of jobs it launches ITSELF,
and of nothing else on the machine.

The rules it enforces, each one a rule the review named:

  * EXPLICIT LAUNCH ONLY.  It starts the job.  It has no mode that changes an
    already running process: the native API cannot read another process's
    Darwin background flag, so a blind change-then-promote cannot restore the
    exact prior policy.  A pid argument is refused, not ignored.
  * THE FOREGROUND APP AND EVERY EXISTING SESSION ARE UNTOUCHED.  Nothing
    outside the process tree this helper creates is read or written.  No
    application is terminated, no session is closed, and no RAM is purged.
  * AN EXACT ALLOWLIST.  Only a named coding build/test entrypoint may be
    launched under the clamp.  Generic process matching is not used anywhere:
    this helper never scans the process table looking for a runtime name to
    guess which processes are coding workers, because that cannot tell a
    coding worker from a protected service.
  * IT FAILS CLOSED.  The clamp is VERIFIED before the job starts, and the
    job does not start if the clamp did not apply.  A receipt records what was
    observed, so "the control is in place" is a measurement and not a claim.

How the clamp is applied and verified:
  a nice value of 10 with the taskpolicy utility QoS clamp -- the same
  combination the fleet's BotFleet background jobs already use.  The Darwin
  background flag is NOT used: it starved com.jay.grok-idle-unload at load
  ~300, where a four second dry run took over 100 seconds.  The utility QoS
  clamp plus a nice value is the supported, bounded choice.

  `nice -n 10` is a RELATIVE increment, so the increment is computed from
  the niceness this process already has.  The job therefore lands on nice
  10 whether the caller is at nice 0, at nice 10 or at nice 20, and the
  helper works when it is launched by something that is already
  deprioritised -- a coding worker, a launchd job -- which is the case it
  exists for.  A caller that is already nicer than the target keeps its own
  value: the target is a floor, and raising a process's priority needs
  privileges this helper neither has nor asks for.

  Both tools exec in place, so the PID survives the whole chain.  The
  re-executed process therefore reads its OWN priority back and checks it
  BEFORE it starts the job -- no race with a job that exits before it can be
  sampled, and no window in which an unclamped job runs.

Usage:
  python3 mac-coding-priority.py --label <name> -- <command> [args...]
  python3 mac-coding-priority.py --dry-run --label <name> -- <command> [args...]

Exit codes:
  0   the job ran and the clamp was verified
  64  usage error
  65  the command is not an allowlisted coding build/test entrypoint
  66  the priority clamp could not be applied or verified (the job did NOT run)
  67  an interactive or GUI launch was refused
  *   otherwise the job's own exit code

The receipt never records the command line: on this Mac an argv can carry a
live API key, and a build command can carry a token.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
LOG_FILE = Path(
    os.environ.get("MAC_CODING_PRIORITY_LOG")
    or HOME / "Library" / "Logs" / "mac-coding-priority.log"
)

# The nice value and the QoS clamp the helper asks for, and the value the
# kernel reports back for the clamp.  The observable is the nice value and the
# scheduling priority of one known pid: a normal process reads ni=0 and pri=31,
# the utility clamp reads pri=20, and Darwin background reads pri=4.  The
# expected pair is overridable so an operator can follow a macOS change
# without editing code; the default is what this host reports today.
DEFAULT_NICE = 10
DEFAULT_EXPECTED_PRI = 20
NORMAL_PRI = 31
# The clamp must be BELOW normal, and it must be the utility clamp and not the
# Darwin background one.  Darwin background reads pri=4 and starves jobs, so the
# band is enforced on EVERY reading of the clamp, including one where an
# operator asked for a different expected value.  Without that, an expected
# value of 4 would accept the Darwin background clamp itself.
MIN_ACCEPTED_PRI = 10
MAX_ACCEPTED_PRI = 25
# The nicest value macOS will set for an ordinary process.  A request above
# this cannot be honoured, so it is refused as a usage error instead of
# being silently clamped to something the requester did not ask for.
MAX_NICE_VALUE = 20

# The coding build/test entrypoints this helper may launch under the clamp.
# Exact basenames, matched case-sensitively.  A bare RUNTIME is NOT here: a
# runtime name identifies a protected service as easily as a coding worker,
# and the review ruled that matching one is not a way to tell the difference.
# So node, python, deno and bun are out even when they also run a test
# suite; the package runner that fronts them (npm, pnpm, yarn) is in.  A
# shell is allowed only with a script file, so an interactive shell can never
# be launched this way.
ALLOWED_ENTRYPOINTS = frozenset(
    {
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "xcodebuild",
        "swift",
        "swiftc",
        "swiftlint",
        "swiftformat",
        "cargo",
        "rustc",
        "go",
        "pytest",
        "tox",
        "uv",
        "hatch",
        "poetry",
        "pdm",
        "gradle",
        "gradlew",
        "mvn",
        "mvnw",
        "make",
        "cmake",
        "ninja",
        "meson",
        "bazel",
        "buck",
        "just",
        "task",
        "tsc",
        "jest",
        "vitest",
        "playwright",
        "clang",
        "clang++",
        "gcc",
        "g++",
        "sccache",
    }
)
# A shell may run a script, never an interactive session and never a GUI.
SHELL_ENTRYPOINTS = frozenset({"bash", "sh"})
# `open` launches a GUI application into the foreground.  Lowering the priority
# of the foreground app is exactly what this plan must not do.
GUI_ENTRYPOINTS = frozenset({"open"})

# Exit codes, so a caller can tell a refusal from a failed job.
EXIT_OK = 0
EXIT_USAGE = 64
EXIT_NOT_ALLOWED = 65
EXIT_NO_CLAMP = 66
EXIT_INTERACTIVE = 67

# Where the tools live.  launchd's default PATH has no /usr/sbin, and
# taskpolicy is in /usr/sbin, so the paths are resolved here and passed on
# the command line rather than looked up again.
TOOL_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/opt/homebrew/bin")

# The option pair that reads one known process's nice value and scheduling
# priority.  It asks for two numbers for ONE pid: it never prints a command
# line, and it never scans the process table.
PRIORITY_READ = ("/bin/ps", "-o", "ni=,pri=")


def _find_tool(name: str) -> str | None:
    """Absolute path of a required tool, or None."""
    for directory in TOOL_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def relative_nice_increment(current_nice: int, target_nice: int) -> int:
    """The `nice -n` argument that lands a child on an ABSOLUTE nice value.

    `nice -n N` is a RELATIVE increment: it adds N to the niceness the
    process already has.  So `nice -n 10` from a parent that is already at
    nice 10 produces a child at nice 20, not 10.  That matters here because
    the helper is meant to be launched by things that are already
    deprioritised -- a coding worker, a launchd job -- and the fixed
    comparison in clamp_applied would then refuse a job whose clamp was
    perfectly good, and the job would never run.

    The increment is computed from what the parent actually is, so the
    child lands on the requested absolute value either way.  A parent that
    is ALREADY nicer than the target keeps its value: the target is a
    floor, not something to improve upon by raising a process's priority,
    which needs privileges the helper does not have and must not ask for.
    """
    if current_nice >= target_nice:
        return 0
    return target_nice - current_nice


def read_priority(pid: int) -> tuple[int, int] | None:
    """(nice, scheduling priority) of one process, or None when unreadable.

    On this Mac a bare process listing can print another process's argv, and an
    argv can carry a live API key, so this asks about one known pid and reads
    two numbers.
    """
    try:
        proc = subprocess.run(
            [*PRIORITY_READ, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    fields = proc.stdout.split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[0]), int(fields[1])
    except ValueError:
        return None


def clamp_applied(
    observed: tuple[int, int] | None, nice: int, expected_pri: int
) -> tuple[bool, str]:
    """Is the clamp in place?  Returns (ok, explanation).

    `nice` is the ABSOLUTE nice value the job has to carry, not an
    increment.  `nice -n 10` is relative, so a launcher that is itself
    already niced would hand the job 20 and the fixed comparison would
    refuse to run a job whose clamp was perfectly good.

    The nice value is checked as a FLOOR: the job has to be at least as
    deprioritised as was asked for, and never less.  Less would mean the
    job runs at MORE priority than the request intended, which is the
    direction that has to fail closed.  More is safe, and happens when the
    caller was already deprioritised further than the target.
    """
    if observed is None:
        return False, "the priority of the launched process could not be read"
    got_nice, got_pri = observed
    if got_nice < nice:
        return False, f"nice is {got_nice}, expected at least {nice}"
    if got_pri >= NORMAL_PRI:
        # The clamp has to be BELOW normal.  Without this, an operator who set
        # the expected priority to the normal one would have the helper accept
        # an unclamped process.
        return False, f"the QoS clamp did not apply (pri={got_pri}, still normal)"
    if not (MIN_ACCEPTED_PRI <= got_pri <= MAX_ACCEPTED_PRI):
        # The band is checked on every reading, not only when the value is
        # unexpected.  Otherwise an expected value of 4 would accept the
        # Darwin background clamp, which is the one that starved
        # com.jay.grok-idle-unload.
        return False, f"pri={got_pri} is not the utility clamp (expected {expected_pri})"
    if got_pri != expected_pri:
        # The utility clamp is in place and macOS is reporting a different
        # number for it than this host normally shows.  The band is what
        # decides, so this is a pass with the difference recorded.
        return True, (
            f"nice={got_nice} pri={got_pri} (utility clamp; this host usually reports "
            f"{expected_pri})"
        )
    return True, f"nice={got_nice} pri={got_pri}"


def check_entrypoint(argv: list[str]) -> tuple[bool, str, int]:
    """Is this command an allowlisted coding build/test entrypoint?

    Returns (allowed, reason, refusal code).  The reason names what was
    refused and why.
    """
    if not argv:
        return False, "no command was given", EXIT_USAGE
    entry = os.path.basename(argv[0])
    if entry in GUI_ENTRYPOINTS:
        return (
            False,
            f"{entry} launches a GUI application, and the foreground app is exempt",
            EXIT_INTERACTIVE,
        )
    if entry in SHELL_ENTRYPOINTS:
        if len(argv) < 2:
            return (
                False,
                f"{entry} with no script file would open an interactive shell",
                EXIT_INTERACTIVE,
            )
        script = argv[1]
        if script.startswith("-") or not Path(script).is_file():
            return (
                False,
                f"{entry} must be given a script file to run",
                EXIT_INTERACTIVE,
            )
        return True, entry, EXIT_OK
    if entry in ALLOWED_ENTRYPOINTS:
        return True, entry, EXIT_OK
    return (
        False,
        f"{entry} is not an allowlisted coding build/test entrypoint",
        EXIT_NOT_ALLOWED,
    )


def append_receipt(receipt: dict) -> None:
    """Append one JSON receipt.  Never records the command line."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    except OSError:
        pass


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run_inner(nice: int, expected_pri: int, label: str, argv: list[str]) -> int:
    """The re-executed half: verify the clamp, then run the job.

    This process carries the clamp already, because both tools exec in place.
    So it can read its OWN priority back and refuse to start the job if the
    clamp did not take, with no race and no window in which an unclamped job
    runs.
    """
    observed = read_priority(os.getpid())
    ok, detail = clamp_applied(observed, nice, expected_pri)
    if not ok:
        append_receipt(
            {
                "at": _timestamp(),
                "label": label,
                "status": "refused",
                "reason": "the priority clamp was not applied, so the job did not start",
                "observed": detail,
            }
        )
        print(f"mac-coding-priority: {detail}", file=sys.stderr)
        return EXIT_NO_CLAMP
    started = time.time()
    try:
        child = subprocess.Popen(argv)
    except OSError as exc:
        append_receipt(
            {
                "at": _timestamp(),
                "label": label,
                "status": "launch-failed",
                "reason": type(exc).__name__,
                "observed": detail,
            }
        )
        return EXIT_USAGE
    # The child inherits the clamp across fork and exec, so this is the
    # inherited state, not a promise.  A job that exits before it can be
    # sampled is recorded as unobserved rather than as a failure.
    child_observed = read_priority(child.pid)
    code = child.wait()
    receipt = {
        "at": _timestamp(),
        "label": label,
        "status": "ran",
        "verified": detail,
        "child_observed": (
            f"ni={child_observed[0]} pri={child_observed[1]}"
            if child_observed
            else "unobserved (the job exited before it could be sampled)"
        ),
        "exit": code,
        "duration_s": round(time.time() - started, 2),
    }
    append_receipt(receipt)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one allowlisted coding build/test job at reduced background priority."
    )
    parser.add_argument("--label", default="", help="a short name for the receipt")
    parser.add_argument(
        "--nice",
        type=int,
        default=DEFAULT_NICE,
        help="the absolute nice value the job must carry (a floor, not an increment)",
    )
    parser.add_argument(
        "--expect-pri",
        type=int,
        default=DEFAULT_EXPECTED_PRI,
        help="the scheduling priority the clamp must report back",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would run, and stop")
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.print_usage(sys.stderr)
        print("mac-coding-priority: give the command to run after --", file=sys.stderr)
        return EXIT_USAGE

    label = args.label.strip() or os.path.basename(command[0])

    if args.inner:
        return run_inner(args.nice, args.expect_pri, label, command)

    if args.nice < 0 or args.nice > MAX_NICE_VALUE:
        print(
            f"mac-coding-priority: --nice must be between 0 and {MAX_NICE_VALUE}; "
            f"got {args.nice}.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Refuse the one thing this helper must never do: act on a process that is
    # already running.
    if command[0] in ("--pid", "-p", "--pgid", "--signal", "--kill"):
        reason = (
            "this helper launches a job; it never changes an already running process, "
            f"so {command[0]} is refused"
        )
        print(f"mac-coding-priority: {reason}.", file=sys.stderr)
        append_receipt(
            {
                "at": _timestamp(),
                "label": label,
                "status": "refused",
                "reason": reason,
            }
        )
        return EXIT_USAGE

    allowed, reason, refusal = check_entrypoint(command)
    if not allowed:
        print(f"mac-coding-priority: refused: {reason}", file=sys.stderr)
        append_receipt(
            {
                "at": _timestamp(),
                "label": label,
                "status": "refused",
                "reason": reason,
            }
        )
        return refusal

    nice_bin = _find_tool("nice")
    taskpolicy_bin = _find_tool("taskpolicy")
    if not nice_bin or not taskpolicy_bin:
        missing = "nice" if not nice_bin else "taskpolicy"
        print(
            f"mac-coding-priority: {missing} is not on this host, so the priority clamp "
            "cannot be applied; the job was not started.",
            file=sys.stderr,
        )
        append_receipt(
            {
                "at": _timestamp(),
                "label": label,
                "status": "refused",
                "reason": f"{missing} is missing, so no clamp could be applied",
            }
        )
        return EXIT_NO_CLAMP

    if args.dry_run:
        print(
            f"mac-coding-priority: would run {reason} at nice={args.nice} "
            f"qos=utility (expecting pri={args.expect_pri})"
        )
        return EXIT_OK

    # `nice -n N` is relative, so the increment is computed from the
    # niceness this process already has.  That way the child lands on the
    # requested ABSOLUTE nice value whether the caller is at nice 0, at
    # nice 10 or at nice 20.  The inner half then verifies that absolute
    # value, which is the value the clamp is supposed to have.
    parent_observed = read_priority(os.getpid())
    if parent_observed is None:
        print(
            "mac-coding-priority: the niceness of this process could not be read, "
            "so the priority clamp cannot be applied; the job was not started.",
            file=sys.stderr,
        )
        append_receipt(
            {
                "at": _timestamp(),
                "label": label,
                "status": "refused",
                "reason": "this process's own niceness could not be read",
            }
        )
        return EXIT_NO_CLAMP
    increment = relative_nice_increment(parent_observed[0], args.nice)

    # Re-exec in place under the clamp.  Both tools exec, so this process
    # keeps its PID and can read its own priority back.
    inner = [
        nice_bin,
        "-n",
        str(increment),
        taskpolicy_bin,
        "-c",
        "utility",
        sys.executable,
        os.path.abspath(__file__),
        "--inner",
        "--nice",
        str(args.nice),
        "--expect-pri",
        str(args.expect_pri),
        "--label",
        label,
        "--",
        *command,
    ]
    os.execv(nice_bin, inner)
    return EXIT_USAGE  # unreachable: execv does not return


if __name__ == "__main__":
    sys.exit(main())
