#!/usr/bin/env python3
"""MiniMax's own PreToolUse adapter, using its Claude-compatible local plugin contract.

The installed runtime maps bash/read/write/edit to Bash/Read/Write/Edit and sends
file_path for compatible file tools, but both path field names are checked on every
file tool, and a relative path with no usable cwd is resolved against home.
No Claude settings or home-local hooks are read.
Shared shell policy stays shared; file content is never interpreted as shell code.
Unexpected payloads and failures allow, as in MiniMax's command-hook runtime.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from typing import IO

from fleet_lanes import fleet_guard_hook as fleet
from fleet_lanes import guard, lane_guard_hook, layout


def file_reason(payload: dict, ctx=None, secret_guard=None) -> str | None:
    raw_tool = payload.get("tool_name")
    tool = {"read": "Read", "write": "Write", "edit": "Edit"}.get(raw_tool, raw_tool)
    args = payload.get("tool_input")
    if tool not in ("Read", "Write", "Edit") or not isinstance(args, dict):
        return None
    ctx = guard.make_context() if ctx is None else ctx
    roots = layout.make_roots(ctx.home, ctx.env, registry=ctx.registry)
    table_error = False
    try:
        sg = secret_guard if secret_guard is not None else fleet._load_secret_guard()
        patterns = sg.SECRET_FILE_PATTERNS if sg is not None else None
        if not patterns or not isinstance(patterns, (tuple, list)) or not all(isinstance(p, str) for p in patterns):
            raise ValueError("invalid secret file pattern table")
        secret_patterns = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    except Exception:  # noqa: BLE001 -- refuse affected file operations below
        secret_patterns = ()
        table_error = True
    cwd = payload.get("cwd")
    # Check every field name this tool might carry: a name this adapter does not read
    # must never be the reason a file operation is allowed.
    for field in ("file_path", "path"):
        value = args.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        reason = _path_reason(tool, value, cwd, ctx, roots, secret_patterns)
        if reason:
            return reason
    if table_error:
        return "MiniMax file tool refused: SECRET_FILE_PATTERNS is unavailable or invalid."
    if ctx.registry.source == "fallback" or not ctx.registry.registered_apps:
        return "MiniMax file tool refused: required FLEET_APPS_JSON registry is unavailable or invalid."
    return None


def _path_reason(tool: str, path: str, cwd, ctx, roots, secret_patterns) -> str | None:
    if not os.path.isabs(path) and not path.startswith("~"):
        # No usable cwd: resolve against home rather than skip the whole file policy.
        path = os.path.join(cwd if isinstance(cwd, str) and os.path.isabs(cwd) else ctx.home, path)
    lexical = os.path.normpath(os.path.join(ctx.home, path[2:]) if path.startswith("~/") else path)
    path = layout.resolve_path(path, roots)
    candidates = (lexical, path)
    if any(".secrets" in p.lower().split(os.sep) for p in candidates) or (
        any(pattern.search(p) for p in candidates for pattern in secret_patterns)
    ):
        return (
            "MiniMax file tool blocked a secret-bearing path.  Use the approved secrets interface."
        )
    location = layout.classify_location(path, roots)
    fold = str.casefold if roots.case_insensitive else (lambda value: value)
    code = fold(str(roots.code_root))
    integration_names = {
        fold(n).replace("-", "").replace(".", "") for n in ctx.registry.integration_names()
    }

    def integration(candidate):
        rel = os.path.relpath(fold(candidate), code).split(os.sep)
        return rel[0].replace("-", "").replace(".", "") in integration_names

    if any(integration(p) for p in candidates):
        return "MiniMax file tool blocked the human integration tree.  Use a fresh owned lane."
    if tool == "Read":
        return None
    if location == layout.LocationClass.REVIEW:
        return "MiniMax file tool blocked a read-only review lane.  Use an owned task lane."
    # MiniMax's subprocess allowlist excludes AGENT_SEAT.  Never infer MM from the
    # engine or destination folder: a trusted launcher may have assigned another seat.
    seat_name = (
        ctx.env.get("AGENT_LAUNCH_SEAT")
        if ctx.env.get("AGENT_LAUNCHER")
        else ctx.env.get("AGENT_SEAT")
    )
    seat = ctx.registry.seat_by_name(seat_name) if seat_name else None
    lane = layout.explain_lane_name(path, ctx.registry, roots)
    if lane.seat is not None and seat is None:
        return "MiniMax file tool refused: trusted launch/seat identity is unavailable for lane authorization."
    if seat is not None and lane.seat is not None and lane.seat.lower() != seat.suffix.lower():
        return "MiniMax file tool blocked another seat's lane.  Use the assigned seat's own lane."
    return None


def decide(payload: dict, ctx=None, secret_guard=None):
    """Return a known-policy denial.

    Shell policy is `fleet.decide`, which isolates its lane and secret checks from each other.
    Required registry and secret-policy failures refuse affected file operations.  Known
    independent denials still take precedence.  A lane write without a valid trusted
    seat is refused; the host's restricted environment cannot attest launcher identity.
    """
    if payload.get("tool_name") in ("Bash", "bash"):
        extracted = guard._extract(payload)
        if extracted is None:
            return None, ""
        command, cwd, _scratch = extracted
        normalized = dict(payload, tool_name="Bash", tool_input={"command": command}, cwd=cwd)
        denial = fleet.decide(normalized, secret_guard)
        if denial[0]:
            return denial
        # Check a final bare process-list command without growing the command
        # past the shared guard's maximum accepted size.
        if re.search(r"(?:^|[;&|\n(])\s*ps\s*$", command):
            return "MiniMax shell tool blocked process argument listing.", ""
        return denial
    try:
        return file_reason(payload, ctx, secret_guard), ""
    except Exception:
        return None, ""


def main(
    argv: Sequence[str] | None = None,
    stdin: IO | None = None,
    stdout: IO | None = None,
    stderr: IO | None = None,
) -> int:
    try:
        payload = json.loads(lane_guard_hook._read_stdin(sys.stdin if stdin is None else stdin))
        if not isinstance(payload, dict):
            return 0
        reason, destination = decide(payload)
        if reason:
            return lane_guard_hook.emit_deny(
                reason,
                "claude",
                destination,
                False,
                sys.stdout if stdout is None else stdout,
                sys.stderr if stderr is None else stderr,
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
