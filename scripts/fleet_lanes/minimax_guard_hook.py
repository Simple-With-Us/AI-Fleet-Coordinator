#!/usr/bin/env python3
"""MiniMax's own PreToolUse adapter, using its Claude-compatible local plugin contract.

The installed runtime maps bash/read/write/edit to Bash/Read/Write/Edit and sends
file_path for compatible file tools, but both path field names are checked on every
file tool, and a relative path with no usable cwd is resolved against home.
No Claude settings or home-local hooks are read.
Shared shell policy stays shared; file content is never interpreted as shell code.
Unexpected payloads and failures allow, as in MiniMax's command-hook runtime.

The file policy is the seat-neutral one in file_guard.py.  This adapter supplies MiniMax's tool
names (its native read/write/edit and the Claude-compatible Read/Write/Edit, both carrying
`file_path` or `path`) and the "MiniMax" label.  MiniMax's subprocess allowlist excludes
AGENT_SEAT, so a lane write needs a launcher's seat (file_guard.trusted_seat).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from typing import IO

from fleet_lanes import file_guard
from fleet_lanes import fleet_guard_hook as fleet
from fleet_lanes import guard, lane_guard_hook, layout  # noqa: F401 -- layout: tests patch it here

MINIMAX_FILE_TOOLS = file_guard.FileTools(
    kinds={
        "Read": file_guard.READ,
        "read": file_guard.READ,
        "Write": file_guard.WRITE,
        "write": file_guard.WRITE,
        "Edit": file_guard.WRITE,
        "edit": file_guard.WRITE,
    },
    path_fields=("file_path", "path"),
)
MINIMAX_FILE_POLICY = file_guard.FilePolicy(label="MiniMax", tools=MINIMAX_FILE_TOOLS)


def file_reason(payload: dict, ctx=None, secret_guard=None) -> str | None:
    return file_guard.file_reason(payload, MINIMAX_FILE_POLICY, ctx, secret_guard)


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
        normalized = dict(
            payload, tool_name="Bash", tool_input={"command": command}, cwd=cwd
        )
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
        payload = json.loads(
            lane_guard_hook._read_stdin(sys.stdin if stdin is None else stdin)
        )
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
