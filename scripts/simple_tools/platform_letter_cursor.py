"""Cursor (the IDE's agent chats and the `cursor-agent` CLI, seat CURSOR, on the Mac and the server) on
`platform-letter`:  an Operator session letter on every interactive Cursor chat (owner request, Nov 7, 2026).

What Cursor gives a hook (read from the cursor-agent 2026.10.22 build, Nov 7, 2026).
- Hooks live in `~/.cursor/hooks.json` (and a project's `.cursor/hooks.json`):  `{"version": 1, "hooks": {<event>:
  [{"command", "timeout", "failClosed"}]}}`, a flat list per event with no matcher.  The lane guard already sits there
  under beforeShellExecution.
- `sessionStart` sends `conversation_id`, `model`, `is_background_agent` and `composer_mode`;  `beforeSubmitPrompt`
  sends `conversation_id`, `generation_id` and `prompt`;  `sessionEnd` sends `conversation_id` and `reason`.  Each also
  carries `hook_event_name`.  Both of the first two take `additional_context` in the answer, which reaches the model.
- No hook payload carries a title and no answer sets one.  Cursor's local chats do keep their title in a store this
  host can read and write, `~/.cursor/acp-sessions/<conversation_id>/meta.json` (the store the chat catalog shows as
  `cursor-local`), so the title is put there the moment the Operator answers with a letter:  `current_title` reads
  its `title`, and `title_writer` writes `XX: words` into the same file, atomically and with every other field kept.
  A chat whose directory does not exist yet is left pending and titled at its next prompt.
- A Cloud agent (`is_background_agent` true) runs its hooks on Cursor's own workers, which drop `additional_context`
  from sessionStart and cannot reach the Operator's registry.  Cloud agents take their letters from
  `cursor_cloud_letters` instead, which reads them from the Cursor API and steers the agent to rename itself;  the hook
  leaves them alone.

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
import stat
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from . import live as L
from . import platform_letter as PL
from . import session_ids as SI

PLATFORM = "cursor"
SEAT = "CURSOR"
SOURCE = "cursor-letter-hook"
DISPLAY = "Cursor"

HOOKS_FILE = os.path.join(".cursor", "hooks.json")
HOOK_TIMEOUTS = {"sessionStart": 20, "beforeSubmitPrompt": 15}
CONTEXT_EVENTS = ("sessionStart", "beforeSubmitPrompt")
#: Cursor's local chat store, one folder per conversation with its `meta.json` (title, cwd, timestamps).
ACP_SESSIONS = os.path.join(".cursor", "acp-sessions")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def cloud_mark(env: Mapping[str, str], cid: str) -> str:
    """The file that says a conversation is a Cloud agent's, kept from its sessionStart for its later events."""
    return os.path.join(L.state_root(env), SEAT, "cloud-agents", cid)


def cursor_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """ "headless" for a Cloud agent (its letter comes from `cursor_cloud_letters`), else "chat".

    Only sessionStart says `is_background_agent`, so its answer is kept in a mark file, and a later event of the same
    conversation (beforeSubmitPrompt has no such field) is judged by the mark.  A conversation with no mark and no
    field is a local chat, such as one resumed from before the install, which never had a sessionStart here."""
    try:
        safe = SI.check(
            _text(payload.get("conversation_id")), "the conversation id"
        )  # no "/" can reach the path
    except ValueError:
        safe = ""
    flag = payload.get("is_background_agent")
    if flag is True:
        if safe:
            try:
                os.makedirs(os.path.dirname(cloud_mark(env, safe)), mode=0o700, exist_ok=True)
                with open(cloud_mark(env, safe), "w", encoding="utf-8"):
                    pass
            except OSError:
                pass  # without the mark a later event of this agent may register it; its hooks reach no registry
        return "headless"
    if flag is None and safe and os.path.exists(cloud_mark(env, safe)):
        return "headless"
    return "chat"


def session_file(env: Mapping[str, str], cid: object) -> str:
    """This conversation's `meta.json` in Cursor's local store, or "" when the id cannot name a folder."""
    cid = _text(cid)
    if not cid or os.path.basename(cid) != cid:
        return ""
    try:
        SI.check(cid, "the conversation id")
    except ValueError:
        return ""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    return os.path.join(home, ACP_SESSIONS, cid, "meta.json")


def _meta(env: Mapping[str, str], cid: object) -> dict[str, Any] | None:
    """The store's record for one conversation, or None when it cannot be read as an object."""
    path = session_file(env, cid)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def cursor_title(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """The title Cursor's own local store shows for this conversation, or "" when it has no record yet.

    A Cloud agent has no local record (and its title is the cloud sweep's business), and the payload itself never
    carries a title, so this store is the only place a local chat's title is read."""
    if env is None:
        return ""
    doc = _meta(env, PL.from_payload("conversation_id")(payload, env))
    return _text(doc.get("title")) if doc else ""


def cursor_rename(request: PL.TitleRequest) -> bool:
    """Put `XX: Words` in the local store's `meta.json`, which is the title Cursor lists and resumes by.

    The write is atomic and keeps every other field and the file's mode.  A record that is absent (the app has not
    made it yet), unreadable or not an object is no write:  the library keeps the title pending and asks again at
    the next prompt, so a hook never fails a chat and never invents a record."""
    path = session_file(request.env, request.session_id)
    if not path:
        return False
    temporary = ""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return False
        if _text(doc.get("title")) == request.title:
            return True
        doc["title"] = request.title
        mode = stat.S_IMODE(os.stat(path).st_mode)
        temporary = path + ".%d.platform-letter.tmp" % os.getpid()
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        return True
    except (OSError, ValueError):
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return False


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("conversation_id"),
    kind=cursor_kind,
    current_title=cursor_title,
    title_writer=cursor_rename,
    events={"sessionStart": "start", "beforeSubmitPrompt": "prompt"},
    close_reasons=(),
    output="json",
    session_env=("CURSOR_CONVERSATION_ID", "CURSOR_AGENT_CHAT_ID"),
    unlettered_feed=(
        "hook-only:  a Cursor hook names no chat to replay, and the local ACP store holds no parent or placeholder"
        " ledger this adapter trusts, so a chat with no letter asks again at its next prompt;  the store's title is"
        " read and written for the title itself"
    ),
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
    transport: PL.Transport | None = None,
    location_flag: str = "",
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
                now=now,
                platform=platform,
                transport=transport,
                location_flag=location_flag,
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
