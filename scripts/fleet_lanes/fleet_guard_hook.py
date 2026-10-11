#!/usr/bin/env python3
"""PreToolUse hook: the lane guard AND the secret guard in one command (installed as `fleet-guard-hook`).

Reads the hook JSON on stdin and checks it twice, in this order:

  1. the lane guard (`fleet_lanes.guard.evaluate`): fleet-repo checkouts, clones and worktrees in temp dirs;
  2. the secret guard (`scripts/hooks/secret-guard-pretooluse.py`): any `ps`, a dump of a secret-bearing file,
     a Bearer value in arguments, a byte-dump of a key variable, stderr merged into a secrets path.

The first deny wins and is printed in the platform's contract, exactly as `lane_guard_hook` prints it (same
`--format FMT` and `--exit2`).  Anything unexpected, in either guard, is an allow:  exit 0 and no output.  A
failure of one guard never switches off the other.

The secret guard is loaded from a file, never copied into this package by hand.  The stable copy that
`install_tools apply tools` writes carries it as `fleet_lanes/secret_guard.py` (the same bytes as the repo's
`scripts/hooks/secret-guard-pretooluse.py`); in a checkout it is found at `../hooks/secret-guard-pretooluse.py`.

    python3 -m fleet_lanes.fleet_guard_hook [--format FMT] [--exit2]

Python 3.9 safe.  Tests: fleet_lanes/tests/test_fleet_guard_hook.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import IO, Optional, Sequence

SECRET_GUARD_NAMES = ("secret_guard.py", os.path.join("..", "hooks", "secret-guard-pretooluse.py"))


def _load_secret_guard():
    """The secret guard module, or None when no copy of it is next to this file.  Each candidate is loaded by path
    under a private module name, so nothing it does at import time can reach the caller's modules."""
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in SECRET_GUARD_NAMES:
        cand = os.path.normpath(os.path.join(here, rel))
        if not os.path.isfile(cand):
            continue
        try:
            spec = importlib.util.spec_from_file_location("fleet_secret_guard", cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if callable(getattr(mod, "check_command", None)) and callable(getattr(mod, "extract_command", None)):
                return mod
        except Exception:
            continue
    return None


def _lane_hook():
    try:
        from fleet_lanes import lane_guard_hook as lgh
    except ImportError:
        scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from fleet_lanes import lane_guard_hook as lgh
    return lgh


def decide(payload: dict, secret_guard=None):
    """(reason, destination) of the first deny, or (None, "").  The two guards fail open independently."""
    try:
        lgh = _lane_hook()
        try:
            from fleet_lanes import guard
        except ImportError:
            lgh._bootstrap()
            from fleet_lanes import guard
        decision = guard.evaluate(payload)
        if decision.action == guard.DENY and decision.reason:
            return decision.reason, decision.destination
    except Exception:
        pass
    try:
        sg = secret_guard if secret_guard is not None else _load_secret_guard()
        if sg is not None:
            command = sg.extract_command(payload)
            if command:
                reason = sg.check_command(command)
                if reason:
                    return reason, ""
    except Exception:
        pass
    return None, ""


def main(argv: Optional[Sequence[str]] = None, stdin: Optional[IO] = None, stdout: Optional[IO] = None,
         stderr: Optional[IO] = None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        lgh = _lane_hook()
        fmt, exit2 = lgh._parse_args(sys.argv[1:] if argv is None else argv)
        payload = json.loads(lgh._read_stdin(stdin))
        if not isinstance(payload, dict):
            return 0
        reason, destination = decide(payload)
    except Exception:
        return 0
    if not reason:
        return 0
    return lgh.emit_deny(reason, fmt, destination, exit2, stdout, stderr)


if __name__ == "__main__":
    sys.exit(main())
