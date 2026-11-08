"""Z Code (`zcode`, seat ZC, on the Mac and the server) on `platform-letter`:  an Operator session letter on every
interactive Z Code chat (owner request, Nov 7, 2026).  The library (`platform_letter`) does the registering, the note
and the retries;  this file is what is Z Code's own.

What Z Code gives a hook (read from the 3.17.2 runtime, `~/.zcode/server/releases/*/runtime/zcode.cjs`, Nov 7, 2026).
- Hooks live in `~/.zcode/cli/config.json` under `hooks.events`, in Claude Code's shape:  an event name maps to a list
  of `{matcher?, hooks: [{type: "command", command, timeout}]}`.  The lane guard already sits there under PreToolUse.
- The events are SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, PostToolUseFailure and
  Stop.  There is no SessionEnd, so nothing is closed on an end:  the library's 36 hour sweep frees idle letters.
- The payload is JSON on stdin with `session_id`, `hook_event_name`, `agent_type` (the agent's name), `cwd`, `source`
  (SessionStart) and `prompt` (UserPromptSubmit), and the environment has ZCODE_SESSION_ID.  The main agent is named
  `zcode-agent`;  any other name is a sub-agent, and Z Code names no parent chat to a hook, so a sub-agent takes no
  letter.
- An answer may carry `hookSpecificOutput.additionalContext` (or a top-level `additionalContext`), which reaches the
  model.  The output schema has no title field, and the one rename Z Code has (`renameSession`) is a command of its own
  app-server protocol, not something a hook can send.  So TITLES ARE NOT SET:  the chat is told its letter, and the
  owner can put "XX: " on a title by hand.
- `zcode -p "prompt"` is a one-shot run:  it takes no letter.  The process that runs the hook is looked at for that.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from . import platform_letter as PL

PLATFORM = "zcode"
SEAT = "ZC"
SOURCE = "zcode-letter-hook"
DISPLAY = "ZCode"
MAIN_AGENT = "zcode-agent"

ENV_SESSION = "ZCODE_SESSION_ID"
CONFIG_FILE = os.path.join(".zcode", "cli", "config.json")
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15}

PS_PATHS = ("/bin/ps", "/usr/bin/ps")
PS_WAIT = 2.0
PROCESS_HOPS = 5  # how far up from the hook the zcode process is looked for
PRINT_FLAGS = ("-p", "--prompt")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# --------------------------------------------------------------------------------------------
# Who runs the hook
# --------------------------------------------------------------------------------------------


def process_row(pid: int) -> tuple[int, list[str]]:
    """(the parent pid, the argument words) of a process, or (0, []) when ps cannot say."""
    tool = next((p for p in PS_PATHS if os.access(p, os.X_OK)), "")
    if not tool:
        return 0, []
    try:
        proc = subprocess.run(
            [tool, "-o", "ppid=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PS_WAIT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, []
    words = proc.stdout.split() if proc.returncode == 0 else []
    if len(words) < 2 or not words[0].isdigit():
        return 0, []
    return int(words[0]), words[1:]


def zcode_arguments(pid: int | None = None, hops: int = PROCESS_HOPS) -> list[str] | None:
    """The arguments after the `zcode` word of the process that runs this hook, or [] when none is found.  Z Code runs
    on node, so the word is looked for anywhere in a process's arguments, not only first."""
    # [] when the processes could be read and none is Z Code's;  None when not even the hook's parent could be read.
    pid = os.getppid() if pid is None else pid
    for hop in range(hops):
        if pid <= 1:
            return []
        parent, words = process_row(pid)
        if not words:
            return None if hop == 0 else []
        for i, word in enumerate(words):
            if os.path.basename(word) in ("zcode", "zcode.cjs"):
                return words[i + 1 :]
        pid = parent
    return []


def one_shot(words: list[str]) -> bool:
    """True for `zcode -p "prompt"` (also --prompt and --prompt=...):  a single prompt run, never a chat."""
    return any(w.partition("=")[0] in PRINT_FLAGS for w in words)


def zcode_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """ "subagent" for any agent but the main one (Z Code names no parent, so it takes no letter), "headless" for a
    `zcode -p` run, else "chat".  A run whose processes cannot be read takes no letter."""
    agent = _text(payload.get("agent_type")) or _text(payload.get("agentName"))
    if agent and agent != MAIN_AGENT:
        return "subagent"
    # When not even the hook's own parent can be read, a one-shot run cannot be told from a chat:  fail closed.
    words = zcode_arguments()
    if words is None or one_shot(words):
        return "headless"
    return "chat"


def no_title(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """Z Code puts no title in a hook payload."""
    return ""


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.either(PL.from_payload("session_id", "sessionId"), PL.from_env(ENV_SESSION)),
    kind=zcode_kind,
    current_title=no_title,
    events={"SessionStart": "start", "UserPromptSubmit": "prompt"},
    close_reasons=(),
    output="json",
)


# --------------------------------------------------------------------------------------------
# The hook:  the library's answer, as additional context
# --------------------------------------------------------------------------------------------


def answer_for(text: str, env: Mapping[str, str], sid: str, event: str) -> dict[str, Any]:
    """The hook's answer for what the library printed:  additional context for the model, or an empty object."""
    answer = PL._last_json(text)
    body = answer.get("platform_letter") if answer else None
    if not isinstance(body, dict):
        return {}
    message = PL.untitled_context(body, env, SEAT, sid, DISPLAY, "Z Code")
    if not message:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def hook_main(
    env: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    *,
    runner: PL.Runner = subprocess.run,
    transport: PL.Transport | None = None,
    location_flag: str = "",
    remote_flag: str = "",
    stderr: TextIO | None = None,
    now: float | None = None,
    platform: str = sys.platform,
) -> int:
    """`platform-letter hook --platform zcode`:  always exit 0, and one JSON object on stdout when there is context to
    give (Z Code reads stdout as JSON only when it starts with "{", so nothing at all is also a valid answer)."""
    answer: dict[str, Any] = {}
    try:
        data = PL.read_hook_input(stdin)
        event = _text(data.get("hook_event_name"))
        if event in SPEC.events:
            printed = io.StringIO()
            PL.hook(
                SPEC,
                env,
                io.StringIO(json.dumps(data)),
                printed,
                runner=runner,
                now=now,
                platform=platform,
                transport=transport,
                location_flag=location_flag,
                remote_flag=remote_flag,
                stderr=stderr,
            )
            sid = SPEC.session_id(data, env)
            answer = answer_for(printed.getvalue(), env, sid, event)
    except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
        try:
            (stderr or sys.stderr).write("platform-letter hook: %s\n" % type(exc).__name__)
        except (OSError, ValueError):
            pass
        answer = {}
    if answer:
        stdout.write(json.dumps(answer) + "\n")
    return 0


# --------------------------------------------------------------------------------------------
# Installing the hooks
# --------------------------------------------------------------------------------------------


def events_key(doc: dict[str, Any]) -> dict[str, Any]:
    """Where Z Code keeps its events:  `hooks.events`, created when absent."""
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = doc["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ValueError("the hooks field is not an object")
    events = hooks.get("events")
    if events is None:
        events = hooks["events"] = {}
    if not isinstance(events, dict):
        raise ValueError("the hooks.events field is not an object")
    return events


INSTALL = PL.JsonHookInstaller(path=CONFIG_FILE, events=HOOK_TIMEOUTS, locate=events_key)
