#!/usr/bin/env python3
"""Claude Code's file guard:  a PreToolUse hook for its native file tools.

Claude Code sends Read, Write, Edit, MultiEdit and NotebookEdit to this hook (the installer's
CLAUDE_FILE_MATCHER group in ~/.claude/settings.json, beside the shell group that runs
lane-guard-hook).  The policy is the seat-neutral one in file_guard.py, with the "Claude" label:

  * secret-bearing paths are refused for reads and writes;
  * the human integration tree is refused for writes.  Reads of it are allowed, because the owner
    opens Claude Code sessions in that tree (Code/<Repo>) and Claude reads it there;
  * writes into a review lane, another seat's lane, or any seat lane without a trusted seat are
    refused (Claude Code passes AGENT_SEAT from its settings env block to the hook);
  * a missing secret table or registry refuses the file operation.

Shell commands are not judged here:  lane-guard-hook and the secret guard hook own the shell.
A payload that is not one of these file tools, or that is not JSON, allows.  An exception inside
the policy for one of these file tools refuses it (fail closed);  a shim that cannot start Python
at all still exits non-zero, which Claude Code does not treat as a deny, so `install_tools verify
claude` and Muster prove the deny path.  The deny is the Claude PreToolUse shape.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import IO

from fleet_lanes import file_guard, lane_guard_hook

CLAUDE_FILE_TOOLS = file_guard.FileTools(
    kinds={
        "Read": file_guard.READ,
        "Write": file_guard.WRITE,
        "Edit": file_guard.WRITE,
        "MultiEdit": file_guard.WRITE,
        "NotebookEdit": file_guard.WRITE,
    },
    path_fields=file_guard.DEFAULT_PATH_FIELDS,
)
CLAUDE_FILE_POLICY = file_guard.FilePolicy(
    label="Claude", tools=CLAUDE_FILE_TOOLS, deny_integration_reads=False
)


def file_reason(payload: dict, ctx=None, secret_guard=None) -> str | None:
    return file_guard.file_reason(payload, CLAUDE_FILE_POLICY, ctx, secret_guard)


def decide(payload: dict, ctx=None, secret_guard=None):
    """(reason or None, destination).

    Fails closed for Claude's own file tools:  when the policy itself raises (the fleet rule "fail closed when
    required configuration or registry is unavailable"), a Read, Write, Edit, MultiEdit or NotebookEdit is refused
    and the reason says the guard could not check it.  Any other payload is not this hook's to judge and allows.

    Why this cannot brick file tools on a transient error:  the resolver's ValueError and OSError are handled in
    file_guard.path_reason, and a missing secret table or registry is a refusal inside file_reason, so what can
    still raise here is building the context, roots and registry (guard.make_context, layout.make_roots), which is
    configuration.  The policy makes no network call and reads only path metadata, so a failure is deterministic
    and shows at once in `install_tools verify claude` and Muster's L1 probe;  Bash stays available for repair.
    MiniMax keeps its fail-open contract (review on #1423).
    """
    try:
        return file_reason(payload, ctx, secret_guard), ""
    except Exception as exc:
        try:
            ours = file_guard.tool_kind(payload, CLAUDE_FILE_TOOLS) is not None
        except Exception:
            ours = False
        if ours:
            return (
                "Claude file tool refused: the file guard could not check this call "
                f"({type(exc).__name__}).  Run `python3 -m fleet_lanes.install_tools verify claude`."
            ), ""
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
