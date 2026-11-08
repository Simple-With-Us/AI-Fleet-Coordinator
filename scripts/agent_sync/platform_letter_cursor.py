"""Cursor (the IDE's agent chats and the `cursor-agent` CLI, seat CURSOR, on the Mac and the server) on
`platform-letter`:  an Operator session letter on every interactive Cursor chat (owner request, Nov 7, 2026).

What Cursor gives a hook (read from the cursor-agent 2026.10.22 build, Nov 7, 2026).
- Hooks live in `~/.cursor/hooks.json` (and a project's `.cursor/hooks.json`):  `{"version": 1, "hooks": {<event>:
  [{"command", "timeout", "failClosed"}]}}`, a flat list per event with no matcher.  The lane guard already sits there
  under beforeShellExecution.
- `sessionStart` sends `conversation_id`, `model`, `is_background_agent` and `composer_mode`;  `beforeSubmitPrompt`
  sends `conversation_id`, `generation_id` and `prompt`;  `sessionEnd` sends `conversation_id` and `reason`.  Each also
  carries `hook_event_name`.  Both of the first two take `additional_context` in the answer, which reaches the model.
- No hook payload carries a title and no answer sets one, so TITLES ARE NOT SET here:  the chat is told its letter.
- A Cloud agent (`is_background_agent` true) runs its hooks on Cursor's own workers, which drop `additional_context`
  from sessionStart and cannot reach the Operator's registry.  Cloud agents take their letters from
  `cursor_cloud_letters` instead, which reads them from the Cursor API, so the hook leaves them alone.

Cursor's chats live in the `cursor` letter namespace, which the registry folds `cursor-local` and `cursor-cloud` into.
Nothing is closed on an end.  The CLI sends `sessionEnd` when its process exits, and a chat resumed later with
`--resume` gets no `sessionStart`, so closing on that end would leave a resumed chat with no letter.  The prompt event
registers a chat the hook has not seen, and the registry's 36 hour sweep frees the letters of idle chats.

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

PLATFORM = "cursor"
SEAT = "CURSOR"
SOURCE = "cursor-letter-hook"
DISPLAY = "Cursor"

HOOKS_FILE = os.path.join(".cursor", "hooks.json")
HOOK_TIMEOUTS = {"sessionStart": 20, "beforeSubmitPrompt": 15}
CONTEXT_EVENTS = ("sessionStart", "beforeSubmitPrompt")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def cursor_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """ "headless" for a Cloud agent (its letter comes from `cursor_cloud_letters`), else "chat"."""
    return "headless" if payload.get("is_background_agent") is True else "chat"


def no_title(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """Cursor puts no title in a hook payload."""
    return ""


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("conversation_id"),
    kind=cursor_kind,
    current_title=no_title,
    events={"sessionStart": "start", "beforeSubmitPrompt": "prompt"},
    close_reasons=(),
    output="json",
)


def answer_for(text: str, env: Mapping[str, str], sid: str, event: str) -> dict[str, Any]:
    """The hook's answer for what the library printed:  `additional_context` for the model on the two events that take
    it, or an empty object."""
    if event not in CONTEXT_EVENTS:
        return {}
    answer = PL._last_json(text)
    body = answer.get("platform_letter") if answer else None
    if not isinstance(body, dict):
        return {}
    message = PL.untitled_context(body, env, SEAT, sid, DISPLAY, "Cursor")
    return {"additional_context": message} if message else {}


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
    """`platform-letter hook --platform cursor`:  always exit 0 and always one JSON object on stdout (Cursor reads one
    from every hook), `{}` when there is nothing to say.  An empty object never blocks a prompt:  only an explicit
    `"continue": false` does."""
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
            answer = answer_for(printed.getvalue(), env, SPEC.session_id(data, env), event)
    except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
        try:
            (stderr or sys.stderr).write("platform-letter hook: %s\n" % type(exc).__name__)
        except (OSError, ValueError):
            pass
        answer = {}
    stdout.write(json.dumps(answer) + "\n")
    return 0


def versioned_hooks(doc: dict[str, Any]) -> dict[str, Any]:
    """Where Cursor keeps its events:  the top-level `hooks`, in a file that also says `"version": 1`."""
    doc.setdefault("version", 1)
    return PL.hooks_key(doc)


def entry(command: str, timeout: int, event: str) -> dict[str, Any]:
    """One handler in Cursor's shape.  `failClosed` is false, so a broken hook never blocks a chat."""
    return {"command": command, "timeout": timeout, "failClosed": False}


INSTALL = PL.JsonHookInstaller(
    path=HOOKS_FILE, events=HOOK_TIMEOUTS, locate=versioned_hooks, build=entry
)
