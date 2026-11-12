"""Claude Code on `platform-letter`:  the Claude row of the one letter table (owner, Nov 11, 2026:  "one operator
that has one tool/process for all platforms").  The library (`platform_letter`) does the registering, the note, the
retries, the "??" title, the end-of-turn report and the install;  this file is only what is Claude Code's own.

- The hooks are Claude Code's:  SessionStart starts, UserPromptSubmit prompts, Stop ends a turn and SessionEnd ends
  the chat.  The session id is the payload's `session_id` (CLAUDE_CODE_SESSION_ID in the chat's own shell, for
  `simple-chat letter request`).
- The title goes out in the hook's own answer (`hookSpecificOutput.sessionTitle`), which Claude Code applies after a
  startup, resume or fork start and on a prompt.  The payload's `session_title` is the title the chat shows now.
- A chat ends for good on `/clear` and on logout, which frees its letter;  every other end keeps it for a resume.
- Only interactive chats take part:  CLAUDE_CODE_ENTRYPOINT `cli` or `claude-desktop`, not a `claude -p` or SDK run,
  and not a session SIMPLE_TOOLS_ATTACH=0 or a launcher started.
- The parent of a chat another chat started is in the desktop app's own record of the chat
  (`~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/<app id>.json`, the app id being
  CLAUDE_CODE_HOST_SESSION_ID):  its `spawnedFrom.sessionId` names the parent's app id, whose record holds the
  parent's session id.  A record is used only when it is this chat's own (its `cliSessionId` is the payload's
  session id), so a `claude` started inside a chat never takes that chat's parent.  Anything unreadable is no parent.

Installing.  `platform-letter install --platform claude [--location server|mac] [--check | --remove]` adds the four
hooks to `~/.claude/settings.json` with the shared JSON installer, and replaces in place the `simple-chat letter
hook` entries installed before Claude joined the table.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from . import identity as I
from . import platform_letter as PL

PLATFORM = "claude"
SEAT = "CLAUDE"
SOURCE = "claude-letter-hook"
DISPLAY = "Claude"
ENTRYPOINTS = ("cli", "claude-desktop")
EVENTS = {
    "SessionStart": "start",
    "UserPromptSubmit": "prompt",
    "Stop": "turn",
    "SessionEnd": "end",
}
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15, "Stop": 15, "SessionEnd": 15}
CLOSE_REASONS = ("clear", "logout")
STORE = ("Library", "Application Support", "Claude", "claude-code-sessions")
HOST_ID = re.compile(r"local_[A-Za-z0-9-]{1,80}")
RECORD_BYTES = 4 << 20


def claude_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """An interactive chat, or a headless run (`claude -p`, the SDK, a session the attach switch turns off).  A Grok
    run that imports the Claude hooks and inherited CLAUDE_CODE_ENTRYPOINT is Grok's chat, never Claude's."""
    if str(env.get(I.ENV_ATTACH, "")).strip() == "0":
        return "headless"
    if env.get("GROK_HOOK_EVENT") or env.get("GROK_SESSION_ID"):
        return "headless"
    return "chat" if env.get("CLAUDE_CODE_ENTRYPOINT") in ENTRYPOINTS else "headless"


def _record(home: str, host_id: str) -> dict[str, Any]:
    """The desktop app's record of one chat, by its app id, or {}."""
    if not HOST_ID.fullmatch(host_id or ""):
        return {}
    root = os.path.join(home, *STORE)
    try:
        accounts = [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        orgs = [e.path for a in accounts for e in os.scandir(a) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return {}
    for org in orgs:
        path = os.path.join(org, host_id + ".json")
        if os.path.isfile(path) and not os.path.islink(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.loads(fh.read(RECORD_BYTES))
            except (OSError, ValueError, RecursionError):
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def claude_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The session id of the chat that started this one, from the desktop app's records, or ""."""
    sid = PL.from_payload("session_id")(payload, env)
    home = str(env.get("HOME") or os.path.expanduser("~"))
    mine = _record(home, str(env.get("CLAUDE_CODE_HOST_SESSION_ID") or ""))
    if not sid or mine.get("cliSessionId") != sid:
        return ""
    spawned = mine.get("spawnedFrom")
    if not isinstance(spawned, dict) or spawned.get("deletedParent"):
        return ""
    parent = _record(home, str(spawned.get("sessionId") or "")).get("cliSessionId")
    return parent if isinstance(parent, str) and parent != sid else ""


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("session_id"),
    parent_id=claude_parent,
    kind=claude_kind,
    events=EVENTS,
    close_reasons=CLOSE_REASONS,
    title_in_output=True,
    output="hook",
    session_env=("CLAUDE_CODE_SESSION_ID",),
)

INSTALL = PL.JsonHookInstaller(path=os.path.join(".claude", "settings.json"), events=HOOK_TIMEOUTS)
