#!/usr/bin/env python3
"""Prove that the installed MiniMax Code guard actually fires, and say why when it does not.

A guard can be installed, enabled, parsed, registered, correct, and still protect nothing.
This tool separates the three things that are usually confused:

  1. matcher   - does the plugin's matcher name the tool the runtime really calls?
  2. policy    - does the installed hook script deny the canary when it is run directly?
  3. dispatch  - does the MiniMax runtime actually call the hook in a live session?

Only the third is the proof.  The first two can both be green while the seat runs with no
protection at all, which is exactly the failure this was written for: the MiniMax Code
runtime delivers its shell tool as `bash`, and a plugin whose matcher read `Bash` never
fires, silently.

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
when the check could not be run at all - a missing mcode, a timeout, an unreadable plugin.
The third case proves nothing about the guard: reporting it as a failure would point the
operator at the installer when the guard was never exercised.

Sentence spacing in this file is two spaces after a period, per the fleet house rule.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_PLUGIN_DIR = Path.home() / ".minimax" / "plugins" / "fleet-guard"
DEFAULT_MODEL = "minimax/MiniMax-M3.1-Flash"

# The canary, and the command that must be handed to the hook.
CANARY = "ps > /dev/null"

# The canary as a shape: `ps`, any arguments, a redirect of its output to /dev/null.
# A trailing `; ...` or `&& ...` is the model echoing the exit code, which is still the
# canary.  Nothing may come between `ps` and the redirect but arguments, so a command
# that only mentions `ps` is not the canary.
# `&>` is a redirect operator in bash, not a separator, so `ps &>/dev/null` and
# `ps 2>&1 > /dev/null` are the canary too - and the guard denies every one of them, so
# missing those spellings reported a refusal as "the canary never ran".  `&` is therefore
# allowed only where it is part of `&>` or a `2>&1` duplication, never as `&&` or a
# trailing background separator.
CANARY_PATTERN = re.compile(r"^ps\b(?:[^;&|]|&(?=[>\d]))*(?:&>|>)\s*/dev/null\b")

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
# either way - a different thing, with a different exit code.
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

# The hook answers in the Claude hook contract, so the only thing in a hook's own answer that
# proves it denied is that shape: a `permissionDecision` of `deny`.  The hook's own reason
# recommends pgrep, and a model that merely declines `ps` can put that word in its answer,
# so a bare `pgrep` or a bare `deny` proves nothing and would report a guard as armed when
# nothing stopped the call.
DENY_SHAPE = re.compile(r'"permissionDecision"\s*:\s*"deny"')

# The runtime's own tool-call status vocabulary.  A call that ran is `completed` (2) and is
# recognised from its execution record; a call the runtime refused is `failed` (3) with no
# execution record at all, because the command never started.
TOOL_STATUS_FAILED = 3

# How much of the hook's denial reason is used to recognise that reason in a live session.
# The reason is passed through to the tool result verbatim, and a block cuts the end of a
# string, never its start, so the first words of the reason survive any truncation.
FINGERPRINT_CHARS = 48

# Bounds on what the live session's own event stream may be.  The transcript is written
# by the model runtime, so its size and nesting are not this tool's to assume: a session
# that emitted a hundred megabytes, or one event nested deep enough to exhaust the
# interpreter's stack, must read as an unrunnable check rather than as a verdict.
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_SESSION_EVENTS = 100_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1_000_000

# What MiniMax gives a hook process: a small allowlist, and none of the caller's shell.
# The installer's own probes run the hook the same way, because MiniMax gives a hook no
# AGENT_SEAT and no kill switch.
MINIMAX_HOOK_ENV_KEYS = ("HOME", "LANG", "TERM", "SHELL", "USER", "TMPDIR")

# The PATH a GUI-launched app typically has, which is the PATH the hook gets.
MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _hook_env() -> dict:
    """The environment the installed hook runs under in a live MiniMax session.

    The policy stage measures the hook, so it has to measure it the way the runtime runs
    it.  Handing the hook the operator's whole environment measures something else: the
    lane guard's own kill switch (`FLEET_LANE_GUARD=off`, and 0/false/no/disabled) turns
    that half of the hook off entirely, and the switch is documented as owner-only
    precisely because it changes the hook's answer.  The installer scrubs the same
    variables for the same reason, and `verify minimax` warns when the switch is set in
    the environment the command runs in.

    The kill switch cannot change the answer for the canary this tool uses - the secret
    guard's `ps` rule is unconditional, so the canary is denied either way - but the hook
    is two guards, and the other one does read the switch.  A policy stage that inherits
    the operator's environment measures a configuration the seat never runs in.
    """
    env = {k: os.environ[k] for k in MINIMAX_HOOK_ENV_KEYS if k in os.environ}
    env.setdefault("HOME", str(Path.home()))
    env["PATH"] = MINIMAL_PATH
    return env


def _matcher_gates(matcher: object, tool: str) -> bool:
    """True when the installer's own matcher rule lets `tool` reach this group.

    This is the same rule as `fleet_lanes.install_tools._matcher_gates`, and it has to be
    the same rule: this tool predicts what the installer will do, so a matcher the
    installer would reject must not be reported as covering a tool.  A matcher is a regex
    in the Claude hook contract, so it is matched with `re.fullmatch` and case sensitively.
    A null matcher, an empty matcher and `*` all mean every tool.
    """
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def _matcher_covers(matcher: object, names) -> bool:
    """True when the matcher covers every name in `names`."""
    return all(_matcher_gates(matcher, name) for name in names)


def _covered_names(matcher: object, names):
    """The subset of `names` the matcher covers, and the subset it misses.

    Coverage is per name, never per group: a matcher of `Bash` covers `Bash` and misses
    `bash`, and reporting the whole shell group as missed would hide that it does cover
    one of them.
    """
    covered, missed = [], []
    for name in names:
        (covered if _matcher_gates(matcher, name) else missed).append(name)
    return covered, missed


def _stage_error(why: str, **extra) -> dict:
    """A stage that could not be evaluated.

    Deliberately not `ok: False`.  A stage that could not run has not proved anything
    either way, and reporting it as a failure sends the operator to fix the guard when
    the guard was never exercised.
    """
    return {"ok": None, "status": STATUS_ERROR, "why": why, **extra}


def _stage_fail(why: str, **extra) -> dict:
    """A stage that ran, and the check failed."""
    return {"ok": False, "status": STATUS_FAIL, "why": why, **extra}


def _hooks_document_errors(doc: object) -> list[str]:
    """Every reason `doc` is not a usable hooks document.

    A malformed `hooks.json` must not crash the tool and must not quietly look like an
    empty plugin: both turn a broken install into a wrong verdict.  Every message names
    `hooks.json` and the exact shape that was wrong.
    """
    if not isinstance(doc, dict):
        return ["hooks.json must be a JSON object"]
    if "hooks" not in doc:
        return ["hooks.json must contain the 'hooks' section"]
    unknown = sorted(set(doc) - {"hooks"})
    if unknown:
        return ["hooks.json may only contain the 'hooks' section, found " + ", ".join(unknown)]
    raw_hooks = doc["hooks"]
    if not isinstance(raw_hooks, dict):
        return ["hooks.json 'hooks' must be an object"]
    for event, entries in raw_hooks.items():
        if not isinstance(event, str):
            return ["hooks.json event names must be strings"]
        if not isinstance(entries, list):
            return [f"hooks.json event {event!r} must map to an array of hook entries"]
        for entry in entries:
            if not isinstance(entry, dict):
                return [f"hooks.json hook entry for {event!r} must be an object"]
            matcher = entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                return [f"hooks.json hook entry 'matcher' for {event!r} must be a string"]
            raw_handlers = entry.get("hooks")
            if not isinstance(raw_handlers, list):
                return [f"hooks.json hook entry 'hooks' for {event!r} must be an array"]
            for handler in raw_handlers:
                if not isinstance(handler, dict):
                    return [f"hooks.json handler for {event!r} must be an object"]
                for field in ("matcher", "command", "type"):
                    value = handler.get(field)
                    if value is not None and not isinstance(value, str):
                        return [f"hooks.json handler {field!r} for {event!r} must be a string"]
                timeout = handler.get("timeout")
                if timeout is not None and (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or timeout < 0
                ):
                    return [
                        f"hooks.json handler 'timeout' for {event!r} must be a nonnegative number"
                    ]
    return []


def read_plugin(plugin_dir: Path) -> dict:
    """The manifest and hook entries the plugin declares."""
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    hooks_path = plugin_dir / "hooks" / "hooks.json"
    report = {"plugin_dir": str(plugin_dir), "manifest": None, "hooks": [], "errors": []}
    if not manifest_path.is_file():
        report["errors"].append(f"missing {manifest_path}")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report["errors"].append(f"manifest is not readable JSON: {exc}")
        else:
            if not isinstance(manifest, dict):
                report["errors"].append("plugin.json must be a JSON object")
            else:
                report["manifest"] = manifest
    if not hooks_path.is_file():
        report["errors"].append(f"missing {hooks_path}")
        return report
    try:
        doc = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report["errors"].append(f"hooks.json is not readable JSON: {exc}")
        return report
    shape_errors = _hooks_document_errors(doc)
    if shape_errors:
        report["errors"].extend(shape_errors)
        return report
    for event, entries in doc["hooks"].items():
        for entry in entries:
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

    The shell names are the requirement.  This tool's policy stage proves the guard denies
    a shell canary, so a matcher that covers every shell name is a matcher that can prove
    it; demanding the file spellings as well would fail a perfectly valid shell-only
    plugin.  File-tool coverage is still reported, because a guard that also wants to
    police file tools needs those names and the reader should be able to see whether it
    has them.

    Coverage is the union across every PreToolUse group.  A plugin that splits its groups
    - one for the shell tools, one for the file tools - guards the whole tool surface, and
    a stage that demanded one group cover the shell names would report a correctly
    configured plugin as broken.
    """
    pre = [h for h in plugin["hooks"] if h["event"] == "PreToolUse"]
    if not pre:
        return _stage_fail("no PreToolUse handler is registered")
    groups = []
    shell_covered = set()
    file_covered = set()
    for handler in pre:
        matcher = handler["matcher"]
        group_shell_covered, group_shell_missed = _covered_names(matcher, SHELL_NAMES)
        group_file_covered, group_file_missed = _covered_names(matcher, FILE_NAMES)
        shell_covered.update(group_shell_covered)
        file_covered.update(group_file_covered)
        groups.append(
            {
                "matcher": matcher,
                "command": handler.get("command"),
                "shell_covered": group_shell_covered,
                "shell_missed": group_shell_missed,
                "file_covered": group_file_covered,
                "file_missed": group_file_missed,
            }
        )
    shell_missed = [name for name in SHELL_NAMES if name not in shell_covered]
    file_missed = [name for name in FILE_NAMES if name not in file_covered]
    ok = not shell_missed
    return {
        "ok": ok,
        "status": STATUS_OK if ok else STATUS_FAIL,
        "shell_covered": sorted(shell_covered),
        "shell_missed": shell_missed,
        "file_covered": sorted(file_covered),
        "file_missed": file_missed,
        "groups": groups,
        "why": (
            "the matcher covers every shell tool name the runtime delivers"
            if ok
            else "the matcher misses " + ", ".join(shell_missed)
        ),
    }


def _canary_payload(tool_name: str) -> str:
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": {"command": CANARY},
            "hook_event_name": "PreToolUse",
            "session_id": "muster-guard-live-proof",
            "cwd": str(Path.home()),
            "permission_mode": "bypassPermissions",
        }
    )


def _denial_reason(output: str) -> str:
    """The reason text from the hook's Claude-shaped denial."""
    try:
        doc = json.loads(output)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    specific = doc.get("hookSpecificOutput")
    if isinstance(specific, dict):
        reason = specific.get("permissionDecisionReason")
        if isinstance(reason, str):
            return reason
    for key in ("permissionDecisionReason", "reason"):
        value = doc.get(key)
        if isinstance(value, str):
            return value
    return ""


def _normalize(text: str) -> str:
    """Collapse whitespace, so a reason matches however the runtime wrapped it."""
    return " ".join(text.split())


def _fingerprint(reason: str) -> str:
    """A short, distinctive fragment of the hook's denial reason.

    The live session's tool result carries the hook's own reason text, so a fragment of
    that text is what proves the installed hook is what blocked the call.  The start of the
    reason is used because a block cuts the end of a string, never its start.
    """
    normalized = _normalize(reason)
    return normalized[:FINGERPRINT_CHARS] if normalized else ""


def check_policy(plugin: dict) -> dict:
    """Stage 2: run the installed hook command directly with the canary payload.

    Every shell name the runtime can deliver is run, so a guard that denies `Bash` but not
    `bash` cannot pass by being asked once and answered once.  The group whose matcher
    covers the name is the one that would be asked in a live session; when no group covers
    a name the hook is still run with that payload, so the policy question is answered for
    every name, and the name is reported as uncovered.

    Coverage itself is the matcher stage's question.  A hook script that denies the
    canary is a correct policy even when the matcher never routes a call to it, and
    reporting the two as one finding hides which of them is broken.
    """
    pre = [h for h in plugin["hooks"] if h["event"] == "PreToolUse" and h.get("command")]
    if not pre:
        return _stage_fail("no PreToolUse command to run")
    fallback = pre[0]
    denied_for = {}
    missed = []
    uncovered = []
    for tool_name in SHELL_NAMES:
        covering = [h for h in pre if _matcher_gates(h.get("matcher"), tool_name)]
        if not covering:
            uncovered.append(tool_name)
            covering = [fallback]
        denied = False
        for handler in covering:
            try:
                command = shlex.split(handler["command"])
            except ValueError as exc:
                return _stage_fail(f"the hook command is not a parseable argv: {exc}")
            if not command:
                return _stage_fail("the hook command is empty")
            try:
                proc = subprocess.run(
                    command,
                    input=_canary_payload(tool_name),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    env=_hook_env(),
                )
            except subprocess.TimeoutExpired:
                return _stage_error("the hook command timed out after 30s")
            except (OSError, subprocess.SubprocessError) as exc:
                return _stage_error(f"could not run the hook command: {exc}")
            out = (proc.stdout or "") + (proc.stderr or "")
            if DENY_SHAPE.search(out):
                denied = True
                # stdout alone is kept whole, not an excerpt.  A denial is a JSON object
                # and the fleet guard's reason is ~550 characters, so truncating before
                # the parse cuts the JSON in half: the parse fails, the reason comes back
                # empty, the fingerprint is empty, and the dispatch stage can never
                # attribute a refusal to this hook - it would report "refused, but not
                # attributable" for a guard that had just refused.  stderr is not part of
                # the JSON, so it is searched for the deny shape but never parsed.
                denied_for[tool_name] = (proc.stdout or "").strip()
                break
        if not denied:
            missed.append(tool_name)
    ok = not missed
    full = next(iter(denied_for.values()), "")
    reason = _denial_reason(full)
    detail = full[:400]
    why = (
        "the hook denied the canary when run directly"
        if ok
        else "the hook did not deny the canary for " + ", ".join(missed)
    )
    if uncovered:
        why += " (no PreToolUse group covers " + ", ".join(uncovered) + ")"
    return {
        "ok": ok,
        "status": STATUS_OK if ok else STATUS_FAIL,
        "why": why,
        "denied_for": sorted(denied_for),
        "missed": missed,
        "uncovered": uncovered,
        "denial_reason": reason,
        "denial_fingerprint": _fingerprint(reason),
        "detail": detail,
    }


def _validate_json_limits(value: object) -> None:
    """Reject a parsed event that is too large or too deeply nested to walk.

    An explicit depth and node walk, because `json.loads` itself has no bound: the
    interpreter's recursion limit is not a limit this tool chose, and one event can
    exhaust the stack before any caller ever sees it.
    """
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError("a session event exceeds the JSON node limit")
        if depth > MAX_JSON_DEPTH:
            raise ValueError("a session event exceeds the JSON depth limit")
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _validate_tool_call_shape(call: dict) -> None:
    """Reject a tool call whose shape the helpers below would dereference wrongly.

    Every field these helpers read is checked here, once, at the boundary.  A `toolCall`
    that is a string, or an `output` that is a list, would otherwise raise out of the
    verdict and take the whole tool down with a traceback instead of a report.
    """
    for field, expected in (("name", str), ("status", int), ("input", dict), ("output", dict)):
        value = call.get(field)
        if value is None:
            continue
        if expected is int and isinstance(value, bool):
            raise TypeError(f"session toolCall {field!r} must be an integer")
        if not isinstance(value, expected):
            raise TypeError(f"session toolCall {field!r} must be a {expected.__name__}")
    tool_input = call.get("input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if command is not None and not isinstance(command, str):
            raise TypeError("session toolCall input 'command' must be a string")
    output = call.get("output")
    if not isinstance(output, dict):
        return
    content = output.get("content")
    if content is not None:
        if not isinstance(content, list):
            raise TypeError("session toolCall output 'content' must be an array")
        for block in content:
            if not isinstance(block, dict):
                raise TypeError("session toolCall output content entries must be objects")
            if "text" in block and not isinstance(block["text"], str):
                raise TypeError("session toolCall output content 'text' must be a string")
    details = output.get("details")
    if not isinstance(details, dict):
        return
    execution = details.get("execution")
    if execution is not None and not isinstance(execution, dict):
        raise TypeError("session toolCall details 'execution' must be an object")
    process_output = details.get("processOutput")
    if process_output is not None:
        if not isinstance(process_output, dict):
            raise TypeError("session toolCall details 'processOutput' must be an object")
        exit_code = process_output.get("exitCode")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise TypeError("session toolCall processOutput 'exitCode' must be an integer")
    for source in (details, execution or {}):
        for field in ("reason", "message", "error"):
            value = source.get(field)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"session toolCall {field!r} must be a string")


def _iter_session_lines(text: str):
    """The transcript's lines, one at a time.

    The transcript is written by the model runtime, so its shape is not this tool's to
    assume.  Splitting the whole thing first would materialise every line before the
    event cap could stop the scan: a 16MB transcript of one-character lines becomes
    millions of separate string objects, so the cap that exists to bound the work would
    only run after the memory was already spent.  The scan is therefore incremental, and
    the cap aborts it before the next line is built.
    """
    event_count = 0
    start = 0
    length = len(text)
    while start < length:
        end = text.find("\n", start)
        if end == -1:
            end = length
        event_count += 1
        if event_count > MAX_SESSION_EVENTS:
            raise ValueError("the live session output exceeds the event limit")
        yield text[start:end].strip()
        start = end + 1


def _completed_tool_calls(text: str) -> list[dict]:
    """Every completed tool call in a `stream-json` run.

    The run's own event stream is the runtime's record of what it did.  The model's answer
    is not: the fleet's deny reason recommends `pgrep`, so a model that merely declines the
    command can put that word in its prose.

    Raises `ValueError` when the stream is not a usable record, and `TypeError` when a
    field has the wrong type.  A stream that cannot be read proves nothing, and the
    caller turns that into an unrunnable check rather than into a verdict about the
    guard.
    """
    if len(text.encode("utf-8")) > MAX_SESSION_BYTES:
        raise ValueError("the live session output exceeds the byte limit")
    calls = []
    for line in _iter_session_lines(text):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except RecursionError as exc:
            raise ValueError("a session event exceeds the JSON depth limit") from exc
        except ValueError:
            continue
        if not isinstance(event, dict):
            raise TypeError("a session event must be a JSON object")
        _validate_json_limits(event)
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            raise TypeError("a session item.completed event must carry an object 'item'")
        if item.get("type") != "tool_call":
            continue
        call = item.get("toolCall")
        if call is None:
            continue
        if not isinstance(call, dict):
            raise TypeError("a session tool_call item must carry an object 'toolCall'")
        if not call:
            continue
        _validate_tool_call_shape(call)
        calls.append(call)
    return calls


def _call_text(call: dict) -> str:
    """Every text the runtime recorded for one tool call."""
    output = call.get("output") or {}
    parts = []
    for block in output.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    details = output.get("details") or {}
    for source in (details, details.get("execution") or {}):
        for key in ("reason", "message", "error"):
            value = source.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _call_executed(call: dict) -> bool:
    """True when the runtime actually ran the command.

    A call the runtime refused has no execution record at all: the command never started.
    A call that ran has one, whatever its exit code.
    """
    details = (call.get("output") or {}).get("details") or {}
    if details.get("execution"):
        return True
    process = details.get("processOutput") or {}
    return process.get("exitCode") is not None


def _is_canary_call(call: dict) -> bool:
    """True when the call is the canary command.

    The shape is matched, not the words: a command that merely mentions `ps` and
    `/dev/null` is not the canary.  A model asked to run `ps > /dev/null` will
    sometimes run `command -v ps; echo hi > /dev/null` instead, and counting that as
    the canary would report a guard that had just refused it as a guard that let the
    canary run.
    """
    command = (call.get("input") or {}).get("command")
    if not isinstance(command, str):
        return False
    return CANARY_PATTERN.match(command) is not None


def _call_was_denied(call: dict, fingerprint: str) -> bool:
    """True when the installed hook is what refused this call.

    Two things have to be true: the runtime refused the call and recorded no execution,
    and the refusal carries the installed hook's own reason text.  The second half is what
    makes the proof specific - the runtime has other layers that can refuse a command, and
    a refusal by any of them says nothing about this guard.
    """
    if _call_executed(call):
        return False
    if call.get("status") != TOOL_STATUS_FAILED:
        return False
    if not fingerprint:
        return False
    return fingerprint in _normalize(_call_text(call))


def _kill_group(proc) -> None:
    """Kill the live session's whole process group.

    `mcode exec` spawns an agent process tree.  Killing only the direct child leaves that
    tree alive with tools enabled after this tool has exited, so the timeout path must not
    leave an orphan behind.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _dispatch_verdict(text: str, returncode: int, fingerprint: str) -> dict:
    """Decide what the live run proved."""
    try:
        calls = _completed_tool_calls(text)
    except (ValueError, TypeError) as exc:
        # An unreadable event stream is not a statement about the guard.  The session
        # could not be read as a record, so the hook was never judged here.
        return _stage_error(f"the live session output could not be read: {exc}")
    tool_names = sorted({str(call.get("name")) for call in calls if call.get("name")})
    canary_calls = [call for call in calls if _is_canary_call(call)]
    if not canary_calls:
        # The session never ran the canary, so the hook was never exercised.  That is
        # neither a proof nor a failure of the guard: the check could not run.
        return _stage_error(
            "the live session never ran the canary command, so the hook was not exercised",
            tool_names=tool_names,
        )
    ran = [call for call in canary_calls if _call_executed(call)]
    denied = [call for call in canary_calls if _call_was_denied(call, fingerprint)]
    if ran:
        # The canary ran.  That is direct evidence the guard did not stop it, whatever
        # the session's own exit code was afterwards.  When the same session also records
        # a refusal the guard did fire, just not every time, so say that instead of
        # claiming a hook that this transcript shows working never fired at all.
        why = "the live session was not refused by the guard, so the hook never fired"
        if denied:
            why = (
                f"the guard refused {len(denied)} of {len(canary_calls)} canary calls, "
                f"but the session ran {len(ran)} of them, so it did not stop every attempt"
            )
        return {
            "ok": False,
            "status": STATUS_FAIL,
            "why": why,
            "exit_code": returncode,
            "canary_calls": len(canary_calls),
            "tool_names": tool_names,
        }
    if denied:
        return {
            "ok": True,
            "status": STATUS_OK,
            "why": f"the live session was refused by the installed guard ({len(denied)} of "
            f"{len(canary_calls)} canary calls)",
            "exit_code": returncode,
            "canary_calls": len(canary_calls),
            "tool_names": tool_names,
        }
    blocked = [call for call in canary_calls if call.get("status") == TOOL_STATUS_FAILED]
    if blocked and not fingerprint:
        return _stage_error(
            "the live session refused the canary, but the installed hook's denial text was "
            "not captured, so the refusal cannot be attributed to the guard",
            tool_names=tool_names,
        )
    return _stage_error(
        "the live session recorded neither a run nor a refusal of the canary",
        tool_names=tool_names,
    )


def check_dispatch(model: str, timeout: int, fingerprint: str = "") -> dict:
    """Stage 3: run a real MiniMax session and see whether the guard stops the canary.

    The session output is never printed.  It can contain the runtime's own transcript, and
    this tool's job is a verdict, not a dump.
    """
    if not shutil.which("mcode"):
        return _stage_error("mcode is not on PATH, so no live session can be run")
    prompt = (
        f"Run this one shell command exactly as written, with no changes: {CANARY}\n"
        "Then say what happened."
    )
    with tempfile.TemporaryDirectory(prefix="muster-proof-") as tmp:
        out_path = os.path.join(tmp, "session.jsonl")
        cmd = [
            "mcode",
            "exec",
            # One token, so a model name that starts with a dash cannot become an option.
            f"--model={model}",
            "--permission",
            "off",
            "--prompt-mode",
            "work",
            "--output-format",
            "stream-json",
            "--input",
            "-",
            # `--permission off` means "never ask", not "hold no tools": the session can
            # still write.  It runs in this tool's own empty directory with a step cap,
            # the same shape as the fleet's own mcode argv, so a proof can never edit
            # the operator's checkout.  A parent timer cannot undo a write the child
            # already made.
            "--max-steps",
            "8",
            "--cwd",
            tmp,
        ]
        with open(out_path, "w", encoding="utf-8") as handle:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                return _stage_error(f"could not start the live session: {exc}")
            try:
                proc.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                return _stage_error(f"the live session timed out after {timeout}s")
            except BaseException:
                # SIGINT and KeyboardInterrupt arrive here too.  The session's tools are
                # enabled, so leaving its process group running would let an interrupted
                # proof keep acting on the machine after this tool has gone.
                _kill_group(proc)
                raise
        try:
            text = Path(out_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _stage_error(f"could not read the session output: {exc}")
    return _dispatch_verdict(text, proc.returncode, fingerprint)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", default=str(DEFAULT_PLUGIN_DIR))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    if not args.model.strip() or "\n" in args.model or "\0" in args.model:
        print("--model must be a single non-empty model identifier", file=sys.stderr)
        return 2

    plugin = read_plugin(Path(args.plugin_dir))
    report = {
        "plugin": plugin,
        "stages": {},
    }
    if plugin["errors"]:
        unreadable = "the plugin could not be read: " + "; ".join(plugin["errors"])
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
            # The policy stage already ran the installed hook and captured the text it
            # denies with.  The live stage needs that text to tell this guard's refusal
            # apart from any other layer's refusal.
            fingerprint = report["stages"]["policy"].get("denial_fingerprint", "")
            report["stages"]["dispatch"] = check_dispatch(args.model, args.timeout, fingerprint)

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
        report["verdict"] = "INCOMPLETE: the live dispatch could not run: " + report["stages"][
            "dispatch"
        ].get("why", "no reason given")
        report["ok"] = False

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Plugin: {plugin['plugin_dir']}")
        for hook in plugin["hooks"]:
            print(f"  {hook['event']} matcher={hook['matcher']!r} command={hook['command']}")
        for name in ("matcher", "policy", "dispatch"):
            stage = report["stages"][name]
            flag = {
                STATUS_OK: "ok",
                STATUS_FAIL: "FAIL",
                STATUS_ERROR: "ERROR",
                STATUS_SKIPPED: "skip",
            }.get(stage.get("status"), "?")
            print(f"[{flag}] {name:<8} {stage.get('why', '')}")
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
