"""L1 sentinel probes and L2 headless canaries:  the proof machinery of ``fleet_config``.

A check that reads a config file proves only that the file says the right thing.  The
proof machinery proves that the harness would ACT on it:  L1 pipes a synthetic
PreToolUse payload into the installed hook and requires the hook to deny it, and L2
runs the platform headless and requires the deny to show up in the transcript.

The levels live here and nowhere else (design doc, section 5):  L0 is inside the
per-kind check modules, L1 and L2 are here.

Safety, stated once and enforced in code:

  * The L1 probe NEVER writes anything.  The payload it sends is a command TEXT, and
    the probe's only child process is the hook, which reads that text on stdin.  There
    is no code path that runs the sentinel command, so a broken hook cannot turn a
    status run into a write.  When the hook allows the sentinel the probe reports the
    failure and stops.
  * The sentinel destination lives inside a throwaway directory the probe creates and
    deletes, and it is asserted to be inside the system temporary directory before the
    payload is built.  The source it names does not exist, so even a command that ran
    would fail at once and write nothing.
  * The L2 canary never runs implicitly.  It is an explicit function call, it is the
    only part of this engine that may spend a model turn, and it runs the platform in
    a throwaway directory that is removed afterwards, pass or fail.
  * A child process gets the environment from ``CheckContext.child_env()``, never from
    ``os.environ`` directly, and no transcript text is copied into a result, so a
    result can never carry a secret.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.  This package never
imports ``fleet_lanes`` at runtime:  the hook's CLI and its deny shapes are pinned
here as data, re-implemented from what the installed hook does.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace

from .checks import CheckContext, CheckError, build_result, kill_process_group
from .types import (
    CheckResult,
    CheckState,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

__all__ = [
    "HOOK_CONFIG_SCHEMAS",
    "L2_DENY_PHRASE",
    "L2_PLATFORMS",
    "L2_TIMEOUT_SECONDS",
    "PLATFORM_ADAPTERS",
    "SENTINEL_PREFIX",
    "SENTINEL_SOURCE_NAME",
    "HookConfigSchema",
    "L1Outcome",
    "L2Outcome",
    "L2Platform",
    "PlatformAdapter",
    "ProofOutcome",
    "apply_proof",
    "build_payload",
    "default_hook_command",
    "hook_format",
    "platform_adapter",
    "proof_to_result",
    "prove_rule",
    "resolve_hook_command",
    "run_l1_probe",
    "run_l2_canary",
    "schema_commands",
]

# --------------------------------------------------------------------------- pinned facts

# The prefix of the throwaway directory one probe owns.  The directory name is also
# the marker the deny reason must name, so every probe's evidence is unique.
SENTINEL_PREFIX = "fleet-config-sentinel"

# The source the sentinel command names.  It is a path that does not exist, under the
# home's Code root, which is what makes the guard treat it as a fleet source (the
# guard matches on the path string, never on the filesystem).  A command that ran
# would fail at once with "does not exist" and write nothing.
SENTINEL_SOURCE_NAME = "fleet-config-l1-sentinel-source"

# The hook names the fleet installs.  A config entry naming one of them is the hook
# under test.
GUARD_HOOK_NAMES = ("lane-guard-hook", "fleet-guard-hook")

# The guard's own sentence.  A permission denial from the platform would not carry it,
# so it is the L2 evidence that the HOOK fired rather than the platform's own policy.
L2_DENY_PHRASE = "fleet-repo checkouts are not allowed in temp directories"

# A model turn is minutes, not seconds, so the L2 budget is its own.  The default is
# generous enough for a slow turn and short enough that a stuck canary is noticed.
L2_TIMEOUT_SECONDS = 300

# The session id the synthetic payload carries.  It is a fixed string, not a real one.
SENTINEL_SESSION_ID = "fleet-config-l1"


@dataclass(frozen=True)
class PlatformAdapter:
    """How one platform's hook is invoked and how that platform says "deny".

    ``payload_shape`` is the JSON the platform sends on stdin;  ``hook_format`` is the
    ``--format`` value the hook is installed with, which decides the deny shape;
    ``config_rel`` is the platform's hook config file, relative to the home, and is
    empty for a platform whose hook lives in a plugin or a patch.
    """

    platform: str
    payload_shape: str
    hook_format: str
    config_rel: str = ""


PLATFORM_ADAPTERS: dict[str, PlatformAdapter] = {
    "claude": PlatformAdapter("claude", "claude", "claude", ".claude/settings.json"),
    "codex": PlatformAdapter("codex", "claude", "codex", ".codex/hooks.json"),
    "grok": PlatformAdapter("grok", "claude", "grok", ".grok/hooks/lane-guard.json"),
    "antigravity": PlatformAdapter(
        "antigravity", "antigravity", "antigravity", ".gemini/config/hooks.json"
    ),
    "cursor": PlatformAdapter("cursor", "cursor", "cursor", ".cursor/hooks.json"),
    "muse": PlatformAdapter("muse", "muse", "muse"),
    "muse-code": PlatformAdapter("muse-code", "muse", "muse"),
    "clutch": PlatformAdapter("clutch", "muse", "claude"),
    "kimi": PlatformAdapter("kimi", "claude", "kimi"),
    "minimax": PlatformAdapter("minimax", "claude", "claude"),
}


@dataclass(frozen=True)
class L2Platform:
    """One platform's headless command, for the L2 canary.

    ``argv`` is the argument list after the binary.  The placeholders ``{prompt}`` and
    ``{workdir}`` are replaced before the command runs;  every other argument is
    fixed.  The canary never runs a platform that has no entry here.
    """

    platform: str
    binary: str
    argv: tuple[str, ...]
    timeout: int = L2_TIMEOUT_SECONDS


L2_PLATFORMS: dict[str, L2Platform] = {
    "claude": L2Platform(
        "claude",
        "claude",
        (
            "-p",
            "{prompt}",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Bash",
        ),
    ),
    "codex": L2Platform(
        "codex",
        "codex",
        (
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-C",
            "{workdir}",
            "{prompt}",
        ),
    ),
}

# The L2 prompt.  It is one fixed sentence with the home and the throwaway
# destination substituted;  it carries no secret and no user input.  It asks for one
# command, so the only write the platform could possibly make is the clone into the
# throwaway directory, which the guard is expected to refuse.
#
# The prompt asks for that one write on purpose.  The guard denies checkout shapes
# and not reads, so a prompt that asked for nothing would produce no deny evidence
# and the canary would prove nothing.  The write it asks for is confined to the
# throwaway directory, and the source it names does not exist, so even a platform
# that ignored the hook would fail at once and write nothing.
L2_PROMPT_TEMPLATE = (
    "Run exactly this one shell command and then stop:\n"
    "    git clone {source} {destination}\n"
    "Do not run any other command, do not read or write any other file, "
    "and do not explain the command."
)


# --------------------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class ProofOutcome:
    """One proof run:  what it proved, at which level, and why it says so.

    ``passed`` is true only for a proof that ran to completion and succeeded.
    ``state`` is the check state the proof maps to, and ``reason_code`` is the machine
    token the verdict math reads.  ``detail`` is a short, non-secret note (an exit
    code, a marker, a timing);  it never carries a transcript or a path.  ``level`` is
    the level that was ACHIEVED, so it is None when no proof ran or the proof failed.
    """

    level: ProofLevel | None
    passed: bool
    state: CheckState
    reason: str
    reason_code: ReasonCode
    detail: str = ""
    achieved: ProofLevel | None = None


@dataclass(frozen=True)
class L1Outcome(ProofOutcome):
    """The outcome of one L1 sentinel probe."""


@dataclass(frozen=True)
class L2Outcome(ProofOutcome):
    """The outcome of one L2 headless canary."""


# --------------------------------------------------------------------------- helpers


def platform_adapter(platform: str) -> PlatformAdapter | None:
    """The adapter for a platform, or None when this phase has no L1 adapter for it."""
    return PLATFORM_ADAPTERS.get(platform)


def build_payload(adapter: PlatformAdapter, command: str, cwd: str) -> dict[str, object]:
    """The synthetic PreToolUse payload for one platform.

    The shape is the one the platform's hook runner sends, because the guard reads
    the command out of the payload by shape.  The command is a TEXT:  the payload
    describes a write, it does not perform one.
    """
    if adapter.payload_shape == "cursor":
        return {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": cwd,
            "workspace_roots": [cwd],
        }
    if adapter.payload_shape == "antigravity":
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "run_command",
            "tool_input": {"CommandLine": command, "Cwd": cwd},
        }
    if adapter.payload_shape == "muse":
        return {
            "session_id": SENTINEL_SESSION_ID,
            "hook_event_name": "PreToolUse",
            "cwd": cwd,
            "permission_mode": "default",
            "tool_name": "bash",
            "tool_input": {
                "command": command,
                "description": "fleet-config L1 sentinel probe",
            },
        }
    return {
        "session_id": SENTINEL_SESSION_ID,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": command,
            "description": "fleet-config L1 sentinel probe",
        },
        "cwd": cwd,
    }


def hook_format(command: str, platform: str) -> str:
    """The ``--format`` value a hook command carries, or the platform's default.

    The format decides the deny shape, so it is read from the installed command
    rather than assumed:  a platform may be configured with a different format.
    """
    match = re.search(r"--format[= ]+([A-Za-z0-9_-]+)", command)
    if match:
        return match.group(1).strip().lower()
    adapter = PLATFORM_ADAPTERS.get(platform)
    return adapter.hook_format if adapter is not None else "claude"


# --------------------------------------------------------------------------- the hook config schema

# A platform's hook config file is another program's file, so it is read as untrusted
# input:  a file bigger than this, a document nested deeper than this, one holding more
# nodes than this, or one naming more commands than this is refused before any command
# is taken from it.
MAX_CONFIG_BYTES = 262144
MAX_CONFIG_DEPTH = 8
MAX_CONFIG_ELEMENTS = 2048
MAX_CONFIG_COMMANDS = 64

# One command is a command line;  a longer one is not a hook this package will run.
MAX_COMMAND_CHARS = 4096

# The events the harnesses name their hooks under.  A config that keeps hooks in named
# groups is read only where one of these is the key, so a sibling section is never
# mistaken for a hook list.
#
# The list holds the Claude-family names plus the ones the other harnesses use for the
# same moment:  Cursor gates its tool calls on `beforeShellExecution`,  which is the
# only event where its guard hook can fire,  so leaving it out would make the probe
# read no command from a Cursor config that plainly has one.
HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Notification",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "SubagentStop",
    "beforeShellExecution",
    "beforeTool",
    "afterTool",
)


def _event_key(name: str) -> str:
    """A comparable key for an event name:  case, underscores and dashes do not matter."""
    return name.lower().replace("_", "").replace("-", "")


@dataclass(frozen=True)
class HookConfigSchema:
    """Where one platform keeps the commands its hooks run,  and which event gates them.

    ``shape`` is ``event-map`` (a mapping of event name to a list of hook entries)  or
    ``named-groups`` (one group per hook name at the top level,  each holding its own
    event lists).  ``section`` is the key the event map lives under.

    ``event`` is the ONE event the platform gates its tool calls on,  and it is the only
    event this package reads,  validates or bounds.  A sibling event,  and a section the
    schema does not name,  is never read,  so a malformed or huge unrelated part of a
    settings file cannot fail a healthy seat's proof.
    """

    shape: str
    section: str = "hooks"
    event: str = "PreToolUse"


HOOK_CONFIG_SCHEMAS: dict[str, HookConfigSchema] = {
    # Claude, Codex and Grok keep ``hooks.<event>`` as a list of groups, each with its
    # own ``hooks`` list;  Cursor keeps ``hooks.<event>`` as a flat list of hook objects.
    "claude": HookConfigSchema("event-map", event="PreToolUse"),
    "codex": HookConfigSchema("event-map", event="PreToolUse"),
    "grok": HookConfigSchema("event-map", event="PreToolUse"),
    "cursor": HookConfigSchema("event-map", event="beforeShellExecution"),
    # Antigravity keeps one group per hook name at the top level.
    "antigravity": HookConfigSchema("named-groups", event="PreToolUse"),
}


def _assert_bounded(document: object, platform: str, path: str) -> None:
    """Refuse a part of a hook config that nests too deep or holds too many nodes.

    Only the part this schema reads is counted, so a large or deeply nested unrelated
    part of the settings file cannot make an otherwise healthy seat fail.
    """
    seen = 0
    stack: list[tuple[object, int]] = [(document, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_CONFIG_DEPTH:
            raise CheckError(
                "the hook config %s for the platform %s nests deeper than %d levels"
                % (path, platform, MAX_CONFIG_DEPTH),
                ReasonCode.UNPARSEABLE,
            )
        seen += 1
        if seen > MAX_CONFIG_ELEMENTS:
            raise CheckError(
                "the hook config %s for the platform %s holds more than %d values"
                % (path, platform, MAX_CONFIG_ELEMENTS),
                ReasonCode.UNPARSEABLE,
            )
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise CheckError(
                        "the hook config %s for the platform %s has a key that is not text"
                        % (path, platform),
                        ReasonCode.UNPARSEABLE,
                    )
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))


def _event_entries(section: dict[str, object], event: str) -> object | None:
    """The entries of one event in a hook section,  or None when it names no event.

    The event name is compared without case,  underscores or dashes,  because the
    harnesses spell the same event in more than one way.  A key that is not the event
    is not read at all,  so a sibling event can never be mistaken for this one.
    """
    wanted = _event_key(event)
    for key, value in section.items():
        if not isinstance(key, str) or _event_key(key) != wanted:
            continue
        return value
    return None


def _commands_from_entries(
    entries: object, what: str, platform: str, path: str, found: list[str]
) -> None:
    """Append the command strings of one event's entries, checking each entry's shape.

    An entry is a group whose own ``hooks`` list holds the hook objects, or a hook
    object itself;  in both cases only a hook object's ``command`` is read, so no other
    key of the config can reach the probe as a command.
    """
    if not isinstance(entries, list):
        raise CheckError(
            "the hook list for %s in the hook config %s for the platform %s is not a list"
            % (what, path, platform),
            ReasonCode.UNPARSEABLE,
        )
    for entry in entries:
        if not isinstance(entry, dict):
            raise CheckError(
                "a hook entry for %s in the hook config %s for the platform %s is not an object"
                % (what, path, platform),
                ReasonCode.UNPARSEABLE,
            )
        inner = entry.get("hooks")
        if inner is None:
            hooks: list[object] = [entry]
        elif isinstance(inner, list):
            hooks = inner
        else:
            raise CheckError(
                "the hooks of an entry for %s in the hook config %s for the platform %s "
                "are not a list" % (what, path, platform),
                ReasonCode.UNPARSEABLE,
            )
        for hook in hooks:
            if not isinstance(hook, dict):
                raise CheckError(
                    "a hook entry for %s in the hook config %s for the platform %s "
                    "is not an object" % (what, path, platform),
                    ReasonCode.UNPARSEABLE,
                )
            command = hook.get("command")
            if command is None:
                continue
            if not isinstance(command, str) or len(command) > MAX_COMMAND_CHARS:
                raise CheckError(
                    "a command for %s in the hook config %s for the platform %s is not a "
                    "short piece of text" % (what, path, platform),
                    ReasonCode.UNPARSEABLE,
                )
            if command.strip():
                found.append(command)
            if len(found) > MAX_CONFIG_COMMANDS:
                raise CheckError(
                    "the hook config %s for the platform %s names more than %d commands"
                    % (path, platform, MAX_CONFIG_COMMANDS),
                    ReasonCode.UNPARSEABLE,
                )


def _commands_from_event_map(
    document: dict[str, object], schema: HookConfigSchema, platform: str, path: str
) -> list[str]:
    """The commands under ``<section>.<event>``, the shape Claude, Codex, Grok and Cursor use.

    Only the platform's own event is read.  A sibling event in the same section is
    never looked at,  so a malformed one is skipped instead of aborting the proof.
    """
    section = document.get(schema.section)
    if section is None:
        return []
    if not isinstance(section, dict):
        raise CheckError(
            "the %s section of the hook config %s for the platform %s is not an object"
            % (schema.section, path, platform),
            ReasonCode.UNPARSEABLE,
        )
    entries = _event_entries(section, schema.event)
    if entries is None:
        return []
    _assert_bounded(entries, platform, path)
    found: list[str] = []
    _commands_from_entries(entries, "the event %r" % schema.event, platform, path, found)
    return found


def _commands_from_named_groups(
    document: dict[str, object], schema: HookConfigSchema, platform: str, path: str
) -> list[str]:
    """The commands under each named group, the shape Antigravity uses.

    Only the platform's own event is read in each group.  A group that holds no such
    event is not read at all,  so an unrelated group in the owner's settings file can
    neither fail the proof nor make it slow.
    """
    found: list[str] = []
    for name, group in document.items():
        if not isinstance(group, dict):
            continue
        entries = _event_entries(group, schema.event)
        if entries is None:
            continue
        _assert_bounded(entries, platform, path)
        _commands_from_entries(
            entries, "the group %r event %r" % (name, schema.event), platform, path, found
        )
    return found


def schema_commands(
    document: object, schema: HookConfigSchema, platform: str, path: str
) -> list[str]:
    """Every command string the schema pins, after the part it reads is checked.

    The schema reads exactly one event:  the one the platform gates its tool calls on.
    That part is checked for its shape and for its size before any command is taken
    from it,  and a part the schema does not read is never checked,  never bounded and
    never turned into a command.

    Raises ``CheckError`` naming the platform and the config path when the document
    does not hold the shape the schema pins, so a command this package cannot vouch for
    is never taken from it.
    """
    if not isinstance(document, dict):
        raise CheckError(
            "the hook config %s for the platform %s is not a JSON object" % (path, platform),
            ReasonCode.UNPARSEABLE,
        )
    if schema.shape == "named-groups":
        return _commands_from_named_groups(document, schema, platform, path)
    return _commands_from_event_map(document, schema, platform, path)


def resolve_hook_command(platform: str, ctx: CheckContext) -> str | None:
    """The installed hook command for a platform, read from its own config file.

    The command is the one the platform would run, verbatim, so the probe tests the
    installed hook and never a copy from the repo.  None when the platform has no
    config file to read, when the file cannot be parsed, or when it names no guard
    hook.

    The file is read as untrusted input:  it is bounded before it is parsed (byte
    limit),  and the part the platform's own schema names is checked for its shape and
    for its size before any command is taken from it,  so an unfamiliar section,  a
    ``command`` key outside a hook entry,  an over-nested hook section or an oversized
    file cannot reach the probe.  A file that fails one of those checks aborts with a
    ``CheckError`` naming the platform and the config path,  which the aggregator reads
    as a failing ``UNKNOWN``;  it never falls back to a command read from a document
    this package cannot vouch for.
    """
    adapter = PLATFORM_ADAPTERS.get(platform)
    if adapter is None or not adapter.config_rel:
        return None
    path = os.path.join(os.fspath(ctx.home), adapter.config_rel)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_CONFIG_BYTES:
        raise CheckError(
            "the hook config %s for the platform %s is larger than %d bytes, so no "
            "command is read from it" % (path, platform, MAX_CONFIG_BYTES),
            ReasonCode.UNPARSEABLE,
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    schema = HOOK_CONFIG_SCHEMAS.get(platform)
    if schema is None:
        raise CheckError(
            "there is no hook config schema for the platform %s, so %s is not read"
            % (platform, path),
            ReasonCode.UNSUPPORTED,
        )
    for command in schema_commands(document, schema, platform, path):
        if any(name in command for name in GUARD_HOOK_NAMES):
            return command
    return None


def default_hook_command(platform: str, ctx: CheckContext) -> str:
    """The stable-dir fleet hook command, for a platform whose config names none.

    The stable dir is ``<home>/apps/lane-tools`` and the hook is ``fleet-guard-hook``,
    the lane guard and the secret guard in one command.

    This is a fallback for a platform that has NO hook config to read at all.  It is
    NOT a fallback for a config that exists but names no guard hook:  that means the
    platform would run something else,  and proving the stable-dir hook would be a
    false PASS.  The caller distinguishes the two cases,  and a config that cannot be
    read at all fails closed.
    """
    adapter = PLATFORM_ADAPTERS.get(platform)
    fmt = adapter.hook_format if adapter is not None else "claude"
    shim = os.path.join(os.fspath(ctx.home), "apps", "lane-tools", "fleet-guard-hook")
    return "%s --format %s" % (shlex.quote(shim), fmt)


def hook_config_exists(platform: str, ctx: CheckContext) -> bool:
    """True when the platform HAS a hook config file to read.

    The L1 probe uses this to tell "this platform has no hook surface" (which the
    stable-dir fallback can prove) from "this platform has a hook config and it does
    not name the guard" (which the fallback must not paper over).
    """
    adapter = PLATFORM_ADAPTERS.get(platform)
    if adapter is None or not adapter.config_rel:
        return False
    path = os.path.join(os.fspath(ctx.home), adapter.config_rel)
    return os.path.isfile(path)


def _probe_env(ctx: CheckContext) -> dict[str, str]:
    """The environment a hook child gets.

    It is the caller's environment (``CheckContext.child_env()``), never
    ``os.environ`` read here, with ``HOME`` pointed at the home being checked, so the
    hook resolves the same roots the check does.
    """
    env = ctx.child_env()
    env["HOME"] = os.fspath(ctx.home)
    return env


def _guard_disabled(env: dict[str, str]) -> bool:
    """True when the environment carries the guard's own off switch.

    The switch is the owner's escape hatch.  A probe that hid it would report a pass
    for a hook that is switched off, so the switch is left in place and named in the
    reason when it is the reason the hook allowed.
    """
    value = (env.get("FLEET_LANE_GUARD") or "").strip().lower()
    return value in ("off", "0", "false", "no", "disabled")


# --------------------------------------------------------------------------- the sentinel


@dataclass(frozen=True)
class Sentinel:
    """The throwaway target one L1 probe describes.

    ``root`` is a throwaway directory the probe creates and then deletes.  The
    ``destination`` is the path the payload's command would write to;  it is never
    created.  ``marker`` is the token the deny reason must name, so a deny can be
    attributed to this probe and not to anything else.
    """

    root: str
    destination: str
    marker: str
    source: str
    command: str


def _make_sentinel(ctx: CheckContext) -> Sentinel:
    """A fresh sentinel:  a throwaway directory, a destination inside it, a marker.

    The source is resolved with ``realpath`` because the guard resolves the home the
    same way, and an unresolved home would not match the guard's fleet roots.
    """
    root = tempfile.mkdtemp(prefix=SENTINEL_PREFIX + "-")
    marker = os.path.basename(root)
    destination = os.path.join(root, marker)
    source = os.path.realpath(os.path.join(os.fspath(ctx.home), "Code", SENTINEL_SOURCE_NAME))
    command = "git clone %s %s" % (source, destination)
    return Sentinel(
        root=root,
        destination=destination,
        marker=marker,
        source=source,
        command=command,
    )


def _assert_sentinel_confined(sentinel: Sentinel) -> None:
    """The sentinel must sit inside the throwaway directory, inside the temp root.

    This is the belt to the braces.  The probe never runs the sentinel command, so
    nothing can be written at all,  but this check makes sure that a refactor which
    moved the sentinel would fail loudly instead of aiming at a real path.
    """
    root = os.path.realpath(sentinel.root)
    destination = os.path.realpath(sentinel.destination)
    temp = os.path.realpath(tempfile.gettempdir())
    if not destination.startswith(root + os.sep):
        raise CheckError("the L1 sentinel destination is outside the probe's throwaway directory")
    if root != temp and not root.startswith(temp + os.sep):
        raise CheckError("the L1 sentinel root is not inside the system temporary directory")


# --------------------------------------------------------------------------- child processes


@dataclass(frozen=True)
class _Run:
    """One child process's result.  ``timed_out`` means the budget ran out."""

    returncode: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    error: str = ""


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill a child's whole process group and reap it, so nothing is left running.

    The child is started in its own session,  so an interruption of the runner does
    not reach it:  without this it would outlive the probe.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate()
    except BaseException:
        pass


def _run_shell(command: str, stdin: bytes, env: dict[str, str], timeout: int, cwd: str) -> _Run:
    """Run a hook command the way a platform runs one:  through a shell, on stdin.

    The process group is killed on a timeout,  and on an interruption,  so a probe never
    leaves a hook running.
    """
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        return _Run(error="cannot start the hook: %s" % exc)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        out, err = proc.communicate()
        return _Run(returncode=None, stdout=out or b"", stderr=err or b"", timed_out=True)
    except BaseException:
        # An interruption (a Ctrl-C,  a SystemExit) must not leave the hook running:
        # the process group is killed and the child is reaped before the interruption
        # is re-raised.
        kill_process_group(proc)
        _reap(proc)
        _close_pipes(proc)
        raise
    return _Run(returncode=proc.returncode, stdout=out or b"", stderr=err or b"")


def _run_argv(argv: list[str], stdin: bytes, env: dict[str, str], timeout: int, cwd: str) -> _Run:
    """Run an argument list directly, with exactly `env`, and kill the group on a timeout."""
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        return _Run(error="cannot start %s: %s" % (argv[0] if argv else "?", exc))
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        out, err = proc.communicate()
        return _Run(returncode=None, stdout=out or b"", stderr=err or b"", timed_out=True)
    except BaseException:
        kill_process_group(proc)
        _reap(proc)
        _close_pipes(proc)
        raise
    return _Run(returncode=proc.returncode, stdout=out or b"", stderr=err or b"")


def _reap(proc: subprocess.Popen) -> None:
    """Wait for a killed child,  so it does not linger as a zombie."""
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - the process is already dead;  reaping is best effort
        pass


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close the child's pipes,  so an interrupted probe leaks no descriptor."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- deny shapes


def _exit2_reason(text_out: str, text_err: str) -> tuple[str | None, str]:
    """The reason of an exit-2 deny, and the problem with reading it.

    The exit-2 contract puts the reason on stderr.  A hook that also wrote to stdout
    still denied, so stdout is only a problem when there is no reason to read.
    """
    reason = text_err.strip()
    if reason:
        return reason, ""
    if text_out.strip():
        return None, "the exit-2 contract keeps the reason on stderr"
    return None, ""


def _read_deny(
    fmt: str, stdout: bytes, stderr: bytes, returncode: int | None
) -> tuple[str | None, str]:
    """The deny reason a hook printed, and the problem with reading it.

    The reason is None when the hook did not deny.  The problem names why the output
    could not be read at all, and is empty when the output was read fine.

    The deny shapes are the hook's own:  the Claude family prints a
    ``hookSpecificOutput`` deny and exits 0,  Grok and Antigravity print
    ``{"decision": "deny"}`` and exit 0,  Cursor prints ``{"permission": "deny"}`` and
    exits 0,  and the exit-2 contract prints the reason on stderr and exits 2.  An
    exit 2 counts as a deny for every format, because that is the plain-text
    contract a platform honours;  the reason still has to be there to be evidence.
    """
    text_out = stdout.decode("utf-8", "replace")
    text_err = stderr.decode("utf-8", "replace")
    if fmt == "kimi":
        if returncode != 2:
            return None, ""
        return _exit2_reason(text_out, text_err)
    if returncode == 2:
        # The plain-text exit-2 contract:  stderr is the reason.
        return _exit2_reason(text_out, text_err)
    if returncode != 0:
        return None, ""
    if not text_out.strip():
        return None, ""
    try:
        body = json.loads(text_out)
    except ValueError:
        return None, "the hook's stdout is not JSON"
    if not isinstance(body, dict):
        return None, "the hook's stdout JSON is not an object"
    if fmt in ("claude", "codex", "muse"):
        specific = body.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            return None, ""
        if specific.get("hookEventName") != "PreToolUse":
            return None, ""
        if specific.get("permissionDecision") != "deny":
            return None, ""
        reason = specific.get("permissionDecisionReason")
    elif fmt in ("grok", "antigravity"):
        if body.get("decision") != "deny":
            return None, ""
        reason = body.get("reason")
    elif fmt == "cursor":
        if body.get("permission") != "deny":
            return None, ""
        reason = body.get("agent_message")
    else:
        return None, "unknown hook format %r" % fmt
    if not isinstance(reason, str) or not reason.strip():
        return None, "the deny carries no reason"
    return reason, ""


# --------------------------------------------------------------------------- L1


def _l1_outcome(
    state: CheckState, reason: str, reason_code: ReasonCode, detail: str = ""
) -> L1Outcome:
    """An L1 outcome that is not a pass."""
    return L1Outcome(
        level=ProofLevel.L1,
        passed=False,
        state=state,
        reason=reason,
        reason_code=reason_code,
        detail=detail,
        achieved=None,
    )


def _interpret_l1(run: _Run, fmt: str, marker: str, budget: int, guard_off: bool) -> L1Outcome:
    """Turn one hook run into an L1 outcome, fail closed.

    A deny that names the sentinel is a pass.  A hook that allowed the sentinel is
    drift:  the hook is present and it does not enforce.  A hook that could not be
    started, that timed out, that crashed, or whose output could not be read is
    unknown, and unknown is failing.
    """
    if run.error:
        return _l1_outcome(
            CheckState.UNKNOWN,
            "the hook could not be started: %s" % run.error,
            ReasonCode.ERROR,
            "no exit code",
        )
    if run.timed_out:
        return _l1_outcome(
            CheckState.UNKNOWN,
            "the hook did not answer within %ds, so the sentinel write was never judged" % budget,
            ReasonCode.TIMEOUT,
            "timed out",
        )
    reason, problem = _read_deny(fmt, run.stdout, run.stderr, run.returncode)
    if problem:
        return _l1_outcome(
            CheckState.UNKNOWN,
            "the hook's answer could not be read: %s" % problem,
            ReasonCode.UNPARSEABLE,
            "exit %s" % run.returncode,
        )
    if reason is None:
        if run.returncode not in (0, 2):
            return _l1_outcome(
                CheckState.UNKNOWN,
                "the hook exited %s instead of answering" % run.returncode,
                ReasonCode.ERROR,
                "exit %s" % run.returncode,
            )
        note = ""
        if guard_off:
            note = "  The FLEET_LANE_GUARD switch is off in this environment."
        return _l1_outcome(
            CheckState.DRIFT,
            "the hook did not deny the sentinel write, so it does not enforce the lane "
            "guard.%s" % note,
            ReasonCode.PROOF_FAILED,
            "exit %s, no deny" % run.returncode,
        )
    if marker not in reason:
        return _l1_outcome(
            CheckState.DRIFT,
            "the hook denied, but its reason does not name the sentinel destination, "
            "so the deny cannot be attributed to this probe",
            ReasonCode.PROOF_FAILED,
            "exit %s" % run.returncode,
        )
    return L1Outcome(
        level=ProofLevel.L1,
        passed=True,
        state=CheckState.PASS,
        reason="the installed hook denied the sentinel write at L1",
        reason_code=ReasonCode.OK,
        detail="exit %s, the %s deny named the sentinel" % (run.returncode, fmt),
        achieved=ProofLevel.L1,
    )


def run_l1_probe(
    platform: str,
    ctx: CheckContext,
    hook_command: str | None = None,
    timeout: int | None = None,
) -> L1Outcome:
    """Prove at L1 that a platform's installed hook denies a write into a sentinel lane.

    The probe builds a synthetic PreToolUse payload whose command names a write into
    a throwaway sentinel lane, pipes it into the installed hook on stdin, and reads
    the platform's deny signal.  A deny that names the sentinel is a pass;  anything
    else fails closed.

    ``hook_command`` overrides the command, which is how a caller proves a command it
    resolved itself.  Without it the command is read from the platform's own config
    file, and the stable-dir fleet hook is the fallback.  ``timeout`` overrides
    ``ctx.timeout``, which is 10 seconds by default;  a probe that overruns its
    budget is a failed proof, never a pass.

    The probe writes nothing.  The payload's command is a text the hook reads, the
    only child process is the hook itself, and the sentinel lives in a throwaway
    directory that is deleted before this function returns.
    """
    adapter = PLATFORM_ADAPTERS.get(platform)
    if adapter is None:
        return L1Outcome(
            level=ProofLevel.L1,
            passed=False,
            state=CheckState.UNSUPPORTED,
            reason="this phase has no L1 adapter for the platform %r" % platform,
            reason_code=ReasonCode.UNSUPPORTED,
            detail="",
            achieved=None,
        )
    if hook_command:
        command = hook_command
    else:
        command = resolve_hook_command(platform, ctx)
        if not command:
            # A platform that HAS a hook config but does not name the guard hook would
            # run something else,  so proving the stable-dir hook would be a false
            # PASS.  The fallback is only honest for a platform with no hook config at
            # all,  which is the platform whose hook lives in a plugin or a bundle.
            if hook_config_exists(platform, ctx):
                raise CheckError(
                    "the hook config of the platform %r names no guard hook,  so there is "
                    "nothing installed to prove" % platform,
                    ReasonCode.PROOF_FAILED,
                )
            command = default_hook_command(platform, ctx)
    fmt = hook_format(command, platform)
    budget = int(timeout) if timeout else int(ctx.timeout or 10)
    env = _probe_env(ctx)
    sentinel = _make_sentinel(ctx)
    try:
        _assert_sentinel_confined(sentinel)
        payload = build_payload(adapter, sentinel.command, sentinel.root)
        run = _run_shell(
            command,
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            env,
            budget,
            sentinel.root,
        )
    finally:
        shutil.rmtree(sentinel.root, ignore_errors=True)
    return _interpret_l1(run, fmt, sentinel.marker, budget, _guard_disabled(env))


# --------------------------------------------------------------------------- L2


def _l2_outcome(
    state: CheckState, reason: str, reason_code: ReasonCode, detail: str = ""
) -> L2Outcome:
    """An L2 outcome that is not a pass."""
    return L2Outcome(
        level=ProofLevel.L2,
        passed=False,
        state=state,
        reason=reason,
        reason_code=reason_code,
        detail=detail,
        achieved=None,
    )


def _l2_has_deny_evidence(text: str, marker: str) -> bool:
    """True when a transcript holds the guard's deny.

    The guard's own sentence is the evidence, because a permission denial from the
    platform would not carry it.  The weaker check (the marker plus a deny word) is
    a fallback for a guard whose wording changed, and it is only reached when the
    guard's sentence is absent.
    """
    if L2_DENY_PHRASE in text:
        return True
    lowered = text.lower()
    return marker in text and ("deny" in lowered or "denied" in lowered)


def run_l2_canary(
    platform: str,
    ctx: CheckContext,
    timeout: int | None = None,
) -> L2Outcome:
    """Prove at L2 that a platform actually loads the hook, by running it headless.

    The canary runs the platform's headless command in a throwaway directory with one
    fixed prompt, and looks for the guard's deny in the transcript.  It is the only
    part of this engine that may spend a model turn, so it never runs implicitly:  a
    caller must ask for it, by name.

    The budget is separate from ``ctx.timeout`` on purpose:  a model turn is minutes,
    not seconds, so the default is ``L2_TIMEOUT_SECONDS`` (300).  ``ctx.timeout`` is
    respected as a floor, never as a ceiling:  a caller who raised it gets a longer
    canary, and a caller who wants a different budget passes ``timeout``.  The child
    gets the environment from ``ctx.child_env()`` and the prompt carries no secret,
    and no transcript text is copied into the outcome.

    A platform whose binary is not installed is "unsupported", not a failure and not a
    pass.  A platform with no canary defined is unsupported too.
    """
    spec = L2_PLATFORMS.get(platform)
    if spec is None:
        return _l2_outcome(
            CheckState.UNSUPPORTED,
            "this phase has no L2 canary for the platform %r" % platform,
            ReasonCode.UNSUPPORTED,
        )
    if shutil.which(spec.binary) is None:
        return _l2_outcome(
            CheckState.UNSUPPORTED,
            "the %s CLI is not installed on this machine, so the canary cannot run" % spec.binary,
            ReasonCode.UNSUPPORTED,
        )
    budget = int(timeout) if timeout else max(int(spec.timeout), int(ctx.timeout or 0))
    workdir = tempfile.mkdtemp(prefix="fleet-config-l2-")
    try:
        source = os.path.realpath(os.path.join(os.fspath(ctx.home), "Code", SENTINEL_SOURCE_NAME))
        destination = os.path.join(workdir, "fleet-config-l2-sentinel-destination")
        prompt = L2_PROMPT_TEMPLATE.format(source=source, destination=destination)
        argv = [spec.binary]
        for part in spec.argv:
            argv.append(part.replace("{prompt}", prompt).replace("{workdir}", workdir))
        run = _run_argv(argv, b"", ctx.child_env(), budget, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if run.error:
        return _l2_outcome(
            CheckState.UNKNOWN, "the canary could not be started: %s" % run.error, ReasonCode.ERROR
        )
    if run.timed_out:
        return _l2_outcome(
            CheckState.UNKNOWN,
            "the %s canary did not finish within %ds" % (spec.binary, budget),
            ReasonCode.TIMEOUT,
            "timed out",
        )
    text = run.stdout.decode("utf-8", "replace") + "\n" + run.stderr.decode("utf-8", "replace")
    marker = os.path.basename(destination)
    if _l2_has_deny_evidence(text, marker):
        return L2Outcome(
            level=ProofLevel.L2,
            passed=True,
            state=CheckState.PASS,
            reason="the %s canary loaded the hook and the guard denied the sentinel write at L2"
            % spec.binary,
            reason_code=ReasonCode.OK,
            detail="exit %s, the transcript held the guard's deny" % run.returncode,
            achieved=ProofLevel.L2,
        )
    if run.returncode not in (0, None):
        return _l2_outcome(
            CheckState.UNKNOWN,
            "the %s canary exited %s, so it proved nothing about the hook"
            % (spec.binary, run.returncode),
            ReasonCode.ERROR,
            "exit %s" % run.returncode,
        )
    return _l2_outcome(
        CheckState.DRIFT,
        "the %s canary ran but its transcript held no deny, so the platform did not load the hook"
        % spec.binary,
        ReasonCode.PROOF_FAILED,
        "exit %s, no deny in the transcript" % run.returncode,
    )


# --------------------------------------------------------------------------- results


def prove_rule(
    rule: Rule,
    platform: str,
    ctx: CheckContext,
    prove_l2: bool = False,
) -> ProofOutcome:
    """Prove one rule, at the level the rule requires.

    This is the seam the aggregator calls, and the reason it exists as one function:
    the aggregator decides WHICH rule needs a proof, and this module decides HOW to
    get one.  The aggregator reads the answer through ``.level``, ``.reason`` and
    ``.reason_code``.

    The level the rule requires decides the probe:

      * ``L0`` needs no proof.  The outcome says so and reports the level, which is
        what the aggregator folds into an already-passing L0 result.
      * ``L1`` runs the sentinel probe, which is cheap and runs on every status run.
      * ``L2`` runs the headless canary, and ONLY when ``prove_l2`` is true, because
        it spends a model turn.  Without it the outcome is "not run", never a pass.

    The outcome carries ``level`` as the level that was ACHIEVED, which is None when
    no proof ran, and the aggregator turns that into ``UNPROVEN`` with reason code
    ``proof-not-run``.  A proof that ran and did not deny carries reason code
    ``proof-failed``, which the contract already treats as failing.
    """
    required = rule.proof_level
    if required is ProofLevel.L0:
        return ProofOutcome(
            level=ProofLevel.L0,
            passed=True,
            state=CheckState.PASS,
            reason="no proof is required for this rule",
            reason_code=ReasonCode.OK,
            detail="",
            achieved=ProofLevel.L0,
        )
    if required is ProofLevel.L2:
        if not prove_l2:
            return ProofOutcome(
                level=None,
                passed=False,
                state=CheckState.UNPROVEN,
                reason=("the L2 canary spends a model turn, so it runs only under --prove l2"),
                reason_code=ReasonCode.PROOF_NOT_RUN,
                detail="L2 not requested",
                achieved=None,
            )
        outcome = run_l2_canary(platform, ctx)
        return _with_achieved(outcome)
    outcome = run_l1_probe(platform, ctx)
    return _with_achieved(outcome)


def _with_achieved(outcome: ProofOutcome) -> ProofOutcome:
    """The outcome with ``level`` set to the level it actually achieved.

    A proof that passed achieved its level;  a proof that failed achieved nothing,
    so its level is None.  The aggregator reads ``level`` as "achieved", so this is
    where a failed proof stops looking like a successful one.
    """
    if outcome.passed and outcome.state is CheckState.PASS:
        return replace(outcome, achieved=outcome.level, level=outcome.level)
    return replace(outcome, achieved=None, level=None)


def proof_to_result(
    rule: Rule,
    platform: str,
    outcome: ProofOutcome,
    ctx: CheckContext,
) -> CheckResult:
    """One checked result for a proof, built with the shared stamps.

    The proof's own state is the result's state, and the reason code is the machine
    token the verdict math reads:  a failed proof is ``proof-failed``, which the
    contract already treats as failing.
    """
    code = outcome.reason_code
    if outcome.state is CheckState.PASS:
        code = ReasonCode.OK
    return build_result(
        rule,
        platform,
        outcome.state,
        outcome.reason,
        code,
        ObservedState(),
        ctx,
        proof_level_achieved=outcome.achieved,
    )


def apply_proof(base: CheckResult, outcome: ProofOutcome) -> CheckResult:
    """The L0 result with the proof's verdict folded in, in the design's step order.

    A result that is not PASS keeps its own verdict:  the proof never overrules a
    failure at L0, because the design applies the steps in order (step 5, the L0
    comparison, comes before steps 6 and 7, the proof steps).  A passing L0 result
    becomes PASS, DRIFT, UNPROVEN, UNSUPPORTED or UNKNOWN, depending on the proof.
    """
    if base.state is not CheckState.PASS:
        return base
    if outcome.state is CheckState.PASS:
        return replace(
            base,
            state=CheckState.PASS,
            reason=outcome.reason,
            reason_code=ReasonCode.OK,
            proof_level_achieved=outcome.achieved,
        )
    if outcome.state is CheckState.DRIFT:
        return replace(
            base,
            state=CheckState.DRIFT,
            reason=outcome.reason,
            reason_code=ReasonCode.PROOF_FAILED,
            proof_level_achieved=None,
        )
    if outcome.state is CheckState.UNPROVEN:
        return replace(
            base,
            state=CheckState.UNPROVEN,
            reason=outcome.reason,
            reason_code=ReasonCode.PROOF_NOT_RUN,
            proof_level_achieved=None,
        )
    if outcome.state is CheckState.UNSUPPORTED:
        return replace(
            base,
            state=CheckState.UNSUPPORTED,
            reason=outcome.reason,
            reason_code=ReasonCode.UNSUPPORTED,
            proof_level_achieved=None,
        )
    return replace(
        base,
        state=CheckState.UNKNOWN,
        reason=outcome.reason,
        reason_code=outcome.reason_code,
        proof_level_achieved=None,
    )
