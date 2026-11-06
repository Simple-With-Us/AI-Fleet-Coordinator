"""The fleet's seat-neutral file guard:  one path policy for every seat's native file tools.

A runtime adapter (minimax_guard_hook, claude_file_guard_hook, and the later seats) describes
its own file tools with a ``FileTools`` map and its reason prefix with a ``FilePolicy``, and
calls ``file_reason(payload, policy)``.  The policy itself lives here, once:

  * The path is resolved the way the runtime would open it:  ``~`` against the home, a
    relative path against the payload's absolute ``cwd`` (or the home when there is none,
    so a missing cwd never skips the policy), then ``layout.resolve_path`` (symlinks).
    Both the lexical and the resolved spelling are checked.
  * A secret-bearing path (a ``.secrets`` component, or a name matching the secret guard's
    ``SECRET_FILE_PATTERNS``) is refused for reads and writes.
  * The human integration tree ``<Code root>/<Repo>`` (the registry's integration names)
    is refused for writes, and for reads unless the policy allows integration reads.
  * A write is refused in a read-only review lane, in another seat's lane, and in any seat
    lane while the trusted seat is unknown (``trusted_seat``).
  * A missing or invalid secret table, or a missing registry, refuses the file operation
    (after the path checks above, so a known denial keeps its own reason).

Every payload field named in ``FileTools.path_fields`` is checked on every file tool, so a
field this adapter does not expect can never be the reason a file operation is allowed.
Unknown tools and payloads without a tool input are not file operations (None).  A crash is
the adapter's to handle:  the fleet's hooks fail open on an exception, as the shell guard does.

Python 3.9 safe (the hook import path runs under macOS /usr/bin/python3).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from fleet_lanes import fleet_guard_hook as fleet
from fleet_lanes import guard, layout

READ = "read"
WRITE = "write"
KINDS = (READ, WRITE)

# The payload fields that carry a path in the Claude-shaped runtimes.  `path` is the native
# spelling of several runtimes, `notebook_path` is NotebookEdit's.  An adapter may name more.
DEFAULT_PATH_FIELDS = ("file_path", "path", "notebook_path")

__all__ = [
    "DEFAULT_PATH_FIELDS",
    "KINDS",
    "READ",
    "WRITE",
    "FilePolicy",
    "FileTools",
    "file_reason",
    "load_secret_patterns",
    "path_reason",
    "tool_kind",
    "trusted_seat",
]


@dataclass(frozen=True)
class FileTools:
    """Which tool names of one runtime are file tools, and where their paths are.

    ``kinds`` maps a tool name, exactly as the runtime sends it, to ``READ`` or ``WRITE``
    (an edit, a multi-edit and a notebook edit are writes).  ``path_fields`` is every
    ``tool_input`` key that may hold a path;  all of them are checked on every file tool.
    """

    kinds: Mapping[str, str]
    path_fields: tuple = DEFAULT_PATH_FIELDS

    def __post_init__(self):
        bad = sorted(k for k, v in self.kinds.items() if v not in KINDS)
        if bad:
            raise ValueError("file tool kinds must be read or write: %s" % ", ".join(bad))
        if not self.path_fields:
            raise ValueError("a file tool map needs at least one path field")


@dataclass(frozen=True)
class FilePolicy:
    """One seat's file guard:  its tool map, the label its reasons start with, and switches.

    ``label`` is the seat or runtime name the reason strings carry ("MiniMax", "Claude").
    ``deny_integration_reads`` refuses reads of the human integration tree too;  a seat whose
    sessions are opened in that tree (Claude Code) allows reading it and still refuses writes.
    """

    label: str
    tools: FileTools
    deny_integration_reads: bool = True

    def reason(self, key: str) -> str:
        return _REASONS[key].format(label=self.label)


_REASONS = {
    "secret": "{label} file tool blocked a secret-bearing path.  Use the approved secrets interface.",
    "integration": "{label} file tool blocked the human integration tree.  Use a fresh owned lane.",
    "review": "{label} file tool blocked a read-only review lane.  Use an owned task lane.",
    "no_seat": "{label} file tool refused: trusted launch/seat identity is unavailable for lane authorization.",
    "other_seat": "{label} file tool blocked another seat's lane.  Use the assigned seat's own lane.",
    "table": "{label} file tool refused: SECRET_FILE_PATTERNS is unavailable or invalid.",
    "registry": "{label} file tool refused: required FLEET_APPS_JSON registry is unavailable or invalid.",
}


def tool_kind(payload: Mapping, tools: FileTools) -> str | None:
    """READ or WRITE for a file tool payload this runtime sends, else None."""
    name = payload.get("tool_name")
    if not isinstance(name, str):
        return None
    return tools.kinds.get(name)


def load_secret_patterns(secret_guard=None) -> tuple:
    """(compiled SECRET_FILE_PATTERNS, table_error).  A missing, empty or invalid table is an error."""
    try:
        sg = secret_guard if secret_guard is not None else fleet._load_secret_guard()
        patterns = sg.SECRET_FILE_PATTERNS if sg is not None else None
        if (
            not patterns
            or not isinstance(patterns, (tuple, list))
            or not all(isinstance(p, str) for p in patterns)
        ):
            raise ValueError("invalid secret file pattern table")
        return tuple(re.compile(p, re.IGNORECASE) for p in patterns), False
    except Exception:  # noqa: BLE001 -- refuse affected file operations
        return (), True


def trusted_seat(env: Mapping, registry):
    """The seat a file write may act for, or None when no trusted seat is known.

    A launcher's seat assignment counts on its own (identity.resolve_seat:  the launcher
    "set AGENT_LAUNCHER, AGENT_LAUNCH_SEAT, or both"), so AGENT_LAUNCH_SEAT names the seat even
    without AGENT_LAUNCHER and beats an ambient AGENT_SEAT.  AGENT_LAUNCHER alone names no seat:
    a launched session without an assigned seat takes no fleet identity, never the ambient one.
    The seat is never inferred from the engine or the destination folder.  AFC-620 role lanes:
    a BF-<ROLE>/GB-<ROLE> name is the synthesized role-lane seat.
    """
    if env.get("AGENT_LAUNCH_SEAT"):
        seat_name = env.get("AGENT_LAUNCH_SEAT")
    elif env.get("AGENT_LAUNCHER"):
        seat_name = None
    else:
        seat_name = env.get("AGENT_SEAT")
    seat = registry.seat_by_name(seat_name) if seat_name else None
    if seat is None and seat_name:
        seat = layout.role_lane_seat(seat_name, registry)
    return seat


def file_reason(payload: Mapping, policy: FilePolicy, ctx=None, secret_guard=None) -> str | None:
    """The denial for one file tool payload, or None to allow it (or when it is not a file tool)."""
    kind = tool_kind(payload, policy.tools)
    args = payload.get("tool_input")
    if kind is None or not isinstance(args, dict):
        return None
    ctx = guard.make_context() if ctx is None else ctx
    roots = layout.make_roots(ctx.home, ctx.env, registry=ctx.registry)
    secret_patterns, table_error = load_secret_patterns(secret_guard)
    cwd = payload.get("cwd")
    for name in policy.tools.path_fields:
        value = args.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        reason = path_reason(kind, value, cwd, ctx, roots, secret_patterns, policy)
        if reason:
            return reason
    if table_error:
        return policy.reason("table")
    if ctx.registry.source == "fallback" or not ctx.registry.registered_apps:
        return policy.reason("registry")
    return None


def _resolve(path: str, cwd, home: str, roots) -> tuple:
    """(lexical, resolved) spellings of a payload path."""
    if not os.path.isabs(path) and not path.startswith("~"):
        # No usable cwd: resolve against home rather than skip the whole file policy.
        path = os.path.join(cwd if isinstance(cwd, str) and os.path.isabs(cwd) else home, path)
    lexical = os.path.normpath(os.path.join(home, path[2:]) if path.startswith("~/") else path)
    return lexical, layout.resolve_path(path, roots)


def path_reason(kind: str, path: str, cwd, ctx, roots, secret_patterns, policy: FilePolicy) -> str | None:
    """The denial for one path of a READ or WRITE, or None."""
    lexical, resolved = _resolve(path, cwd, ctx.home, roots)
    candidates = (lexical, resolved)
    if any(".secrets" in p.lower().split(os.sep) for p in candidates) or (
        any(pattern.search(p) for p in candidates for pattern in secret_patterns)
    ):
        return policy.reason("secret")
    fold = str.casefold if roots.case_insensitive else (lambda value: value)
    code = fold(str(roots.code_root))
    integration_names = {
        fold(n).replace("-", "").replace(".", "") for n in ctx.registry.integration_names()
    }

    def integration(candidate):
        rel = os.path.relpath(fold(candidate), code).split(os.sep)
        return rel[0].replace("-", "").replace(".", "") in integration_names

    if (kind == WRITE or policy.deny_integration_reads) and any(
        integration(p) for p in candidates
    ):
        return policy.reason("integration")
    if kind == READ:
        return None
    if layout.classify_location(resolved, roots) == layout.LocationClass.REVIEW:
        return policy.reason("review")
    seat = trusted_seat(ctx.env, ctx.registry)
    lane = layout.explain_lane_name(resolved, ctx.registry, roots)
    if lane.seat is not None and seat is None:
        return policy.reason("no_seat")
    if seat is not None and lane.seat is not None and lane.seat.lower() != seat.suffix.lower():
        return policy.reason("other_seat")
    return None
