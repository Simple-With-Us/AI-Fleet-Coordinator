"""Antigravity (the `agy` CLI, Antigravity.app and the Antigravity IDE; seat AG, on the Mac and on the server) on
`platform-letter` (AFC-564 / AFC-665, owner request Oct 29, 2026; updated Nov 8, 2026): an Operator session letter on
every interactive Antigravity conversation, with automatic `XX: Title` conversation renaming.  The library
(`platform_letter`) does the registering, the note and the retries; this file is what is Antigravity's own.

How `XX: Title` renaming works (`antigravity_rename`).  Antigravity's `AnnotationsManager` persists user-defined
conversation titles in `<product root>/annotations/<conversation_id>.pbtxt` (`title:"XX: Words"`) and syncs them with
`<product root>/conversation_summaries.db`.  Updating `conversation_summaries.db` alone does not last across turns because
`AnnotationsManager` overwrites the summary row from its annotation state; writing both `<product root>/annotations/<id>.pbtxt`
(preserving any other fields such as `last_user_view_time`) and `<product root>/conversation_summaries.db` keeps the
`"XX: Words"` prefix across turns on both the server and the Mac.  When a person renames the chat with `/rename`, the
hook reads the stored title, keeps their words under the assigned letter, and sends the bare words to the Operator.

What Antigravity gives a hook (read from the hook documentation the agy binary embeds, and from live runs).
- A hook is a command in a named group of `~/.gemini/config/hooks.json`, which the CLI, the app and the IDE all load.
  It gets JSON on stdin and answers with JSON on stdout.  Every payload has `conversationId`, `workspacePaths`,
  `transcriptPath`, `artifactDirectoryPath` (`<product root>/brain/<id>`, where the root is `antigravity-cli`,
  `antigravity` or `antigravity-ide`), `modelName` and `lastUserInput`, and the hook's environment has
  `ANTIGRAVITY_CONVERSATION_ID`.  A sub-agent's conversation adds `parentConversationId`, `rootConversationId` and
  `agentName` (the hook proto's common fields); that part is read from the proto, not from a live sub-agent run.
- The payload does not name its event, so the event is told from its fields.  SessionStart carries only the common
  fields and fires once, when a new conversation takes its first prompt.  PreInvocation adds `invocationNum` and
  `initialNumSteps` and fires before every model call; `invocationNum` is 0 for the first call after a prompt, whether
  the conversation is new or resumed.  A resumed conversation (`--conversation`, `/resume`) gets no SessionStart.
  Stop adds `executionNum`, `terminationReason` and `fullyIdle`.  `UserPromptSubmit` and `SessionEnd` are never sent.
- So the hook is installed on SessionStart (register, tell the chat its letter) and PreInvocation (the first call of a
  prompt: register a conversation the hook has not seen, such as one that was resumed from before the install, put the
  Operator's title words right, mark the chat seen).  A later model call of the same prompt has nothing to do, and
  `scripts/platform-letter` answers `{}` for it from the payload text before it starts the Python 3.11 re-run or
  imports the library (`normalize` below turns the same payload away if that quick check lets it through).  Such a call
  costs a process start and a read of standard input, where the import alone costs 30 to 60 ms more, and a tool loop
  makes dozens of them.  Nothing is closed on an end: a closed conversation can be resumed, and the library's 36 hour
  sweep frees the letters of idle ones.
- The answer that works is `{"injectSteps": [{"systemMessage": {"systemMessage": "<text>"}}]}`: agy stores it as a
  step of the conversation ("The following system message was injected by a SessionStart hook"), so the model has it
  for the rest of the conversation.  The documented `metadata.user_markdown` does not work in agy 1.4.10: given an
  object protojson refuses it, given a string the hook caller panics ("cannot convert string to message").  agy logs
  either and goes on without the step, so a bad answer never stops a chat.

Who takes a letter.  An interactive conversation, found from the process that runs the hook: the `agy` process's
arguments say whether it is a one-shot `agy -p "prompt"` run (headless: no letter) or a TUI or a long-lived
`--input-format stream-json` bridge (a chat).  The app and the IDE have no `agy` process, so their conversations are
chats.  A conversation with `parentConversationId` is a sub-thread: it takes the pair after its parent's.  A session a
launcher started never does (the library checks AGENT_LAUNCHER and AGENT_LAUNCH_SEAT).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import platform_letter as PL

PLATFORM = "antigravity"
SEAT = "AG"
SOURCE = "antigravity-letter-hook"
DISPLAY = "Antigravity"

ENV_CONVERSATION = "ANTIGRAVITY_CONVERSATION_ID"
ENV_OFF = "ANTIGRAVITY_LETTER"
HOOKS_FILE = os.path.join(".gemini", "config", "hooks.json")
GROUP = "session-letter"
HOOK_TIMEOUTS = {"SessionStart": 20, "PreInvocation": 15}
STORE_PRODUCTS = ("antigravity-cli", "antigravity")

STORE_WAIT = 0.5  # seconds to wait for a locked summaries database before reading nothing
PS = "/bin/ps"
PS_WAIT = 2.0
PROCESS_HOPS = (
    4  # how far up from the hook the `agy` process is looked for (the hook runs under `sh -c`)
)
AGY_NAMES = ("agy",)

# Fields that only the events this adapter ignores carry: PostInvocation, Stop and the tool events.
OTHER_EVENT_FIELDS = (
    "modelOutput",
    "modelThinking",
    "executionNum",
    "terminationReason",
    "fullyIdle",
    "toolCall",
    "stepIdx",
)
PRINT_FLAGS = ("-p", "--print", "--prompt")
INTERACTIVE_PROMPT_FLAGS = ("-i", "--prompt-interactive")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# --------------------------------------------------------------------------------------------
# The payload: which event, and which conversation
# --------------------------------------------------------------------------------------------


def normalize(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """The library's view of an agy hook payload, or None for a payload this adapter does not act on.

    agy names no event, so it is told from the fields: a payload with `initialNumSteps` or `invocationNum` is
    PreInvocation, and only its first model call (`invocationNum` 0, or absent where an encoder leaves a zero out)
    is a prompt; a payload with a field of another event (Stop, PostInvocation, a tool) is none of ours; a payload
    with only the common fields is SessionStart."""
    cid = _text(data.get("conversationId"))
    if not cid or any(key in data for key in OTHER_EVENT_FIELDS):
        return None
    if "invocationNum" in data or "initialNumSteps" in data:
        number = data.get("invocationNum") or 0
        if isinstance(number, bool) or not isinstance(number, int) or number != 0:
            return None  # a later model call of the same prompt
        event = "PreInvocation"
    else:
        event = "SessionStart"
    return dict(
        data, hook_event_name=event, session_id=cid, prompt=_text(data.get("lastUserInput"))
    )


# --------------------------------------------------------------------------------------------
# Who runs the hook (interactive or one-shot)
# --------------------------------------------------------------------------------------------


def process_row(pid: int) -> tuple[int, list[str]]:
    """(the parent pid, the argument words) of a process, or (0, []) when ps cannot say.  `ps` joins the arguments
    with spaces, so a word that held a space is several words; the flag scan below allows for that."""
    try:
        proc = subprocess.run(
            [PS, "-o", "ppid=,args=", "-p", str(pid)],
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


def agy_arguments(pid: int | None = None, hops: int = PROCESS_HOPS) -> list[str]:
    """The arguments of the `agy` process that runs this hook, or [] when none is found within `hops` parents (the
    app and the IDE run hooks from a language server, not from `agy`)."""
    pid = os.getppid() if pid is None else pid
    for _ in range(hops):
        if pid <= 1:
            return []
        parent, words = process_row(pid)
        if words and os.path.basename(words[0]) in AGY_NAMES:
            return words
        pid = parent
    return []


def one_shot(words: list[str]) -> bool:
    """True for `agy -p "prompt"` (also --print and --prompt): a single prompt run, never a chat.  A print run that
    reads its prompts from stdin (`--input-format stream-json`) is a bridge that stays up for many turns, so it is a
    chat.  The words after -i are that flag's prompt and are not scanned for flags."""
    printing = streaming = False
    i = 1
    while i < len(words):
        name, eq, value = words[i].partition("=")
        if name in INTERACTIVE_PROMPT_FLAGS:
            break
        if name in PRINT_FLAGS:
            printing = True
        elif name == "--input-format":
            streaming = (
                value if eq else (words[i + 1] if i + 1 < len(words) else "")
            ) == "stream-json"
        i += 1
    return printing and not streaming


def antigravity_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """ "headless" for a one-shot `agy -p` run (and the sub-agents inside it), "subagent" for a conversation another
    one started, else "chat".  A host where no process can be read counts as a chat."""
    if one_shot(agy_arguments()):
        return "headless"
    return "subagent" if antigravity_parent(payload, env) else "chat"


# --------------------------------------------------------------------------------------------
# The title and lineage agy stores (read only)
# --------------------------------------------------------------------------------------------


def summaries_db(payload: Mapping[str, Any]) -> str:
    """The summaries database of the product that runs the conversation, from its artifact directory
    (`<product root>/brain/<id>`), or "" when the payload does not say or the product keeps none (the IDE)."""
    art = _text(payload.get("artifactDirectoryPath"))
    brain = os.path.dirname(art)
    if not art or os.path.basename(brain) != "brain":
        return ""
    root = os.path.dirname(brain)
    if not os.path.basename(root).startswith("antigravity"):
        return ""
    path = os.path.join(root, "conversation_summaries.db")
    return path if os.path.isfile(path) else ""


def candidate_dbs(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> list[str]:
    """The summaries databases to consult for `payload`: the one its `artifactDirectoryPath` names first, then the
    host's `~/.gemini/antigravity-cli` and `~/.gemini/antigravity` stores when `HOME` is set."""
    found: list[str] = []
    direct = summaries_db(payload)
    if direct:
        found.append(direct)
    home = _text((env or {}).get("HOME"))
    if home:
        for product in STORE_PRODUCTS:
            cand = os.path.join(home, ".gemini", product, "conversation_summaries.db")
            if cand not in found and os.path.isfile(cand):
                found.append(cand)
    return found


def antigravity_parent(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """The parent conversation's native id, from `parentConversationId` in the payload or `parent_conversation_id` in
    the summaries database."""
    direct = _text(payload.get("parentConversationId"))
    if direct:
        return direct
    cid = _text(payload.get("conversationId")) or _text(payload.get("session_id"))
    if not cid:
        return ""
    for db in candidate_dbs(payload, env):
        try:
            conn = sqlite3.connect("%s?mode=ro" % Path(db).as_uri(), uri=True, timeout=STORE_WAIT)
            try:
                row = conn.execute(
                    "SELECT parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            continue
        if row and isinstance(row[0], str) and row[0].strip():
            return row[0].strip()
    return ""


def antigravity_chat_payload(sid: str) -> Mapping[str, Any]:
    """The start payload a conversation's own SessionStart would carry, so the library can enroll a parent that
    holds no live registration before its sub-agent."""
    return {
        "hook_event_name": "SessionStart",
        "session_id": sid,
        "conversationId": sid,
        "source": "startup",
    }


def stored_title(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """The conversation's title as agy shows it in `/resume` (its own, or the one it generated), "" when unknown.
    The database is opened read only and never written: a hook runs inside a CLI that owns it."""
    cid = _text(payload.get("conversationId")) or _text(payload.get("session_id"))
    if not cid:
        return ""
    for db in candidate_dbs(payload, env):
        try:
            conn = sqlite3.connect("%s?mode=ro" % Path(db).as_uri(), uri=True, timeout=STORE_WAIT)
            try:
                row = conn.execute(
                    "SELECT title FROM conversation_summaries WHERE conversation_id = ?", (cid,)
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            continue
        if row and isinstance(row[0], str) and PL.tidy(row[0]):
            return PL.tidy(row[0])
    return ""


_TITLE_PBTXT_RE = re.compile(r'title:\s*"(?:\\.|[^"\\])*"')
_SAFE_CID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def candidate_roots(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> list[str]:
    """The Antigravity product roots (`<root>/brain/<id>` first, then `~/.gemini/<product>`) that exist on disk."""
    roots: list[str] = []
    art = _text(payload.get("artifactDirectoryPath"))
    brain = os.path.dirname(art)
    if art and os.path.basename(brain) == "brain":
        root = os.path.dirname(brain)
        if os.path.basename(root).startswith("antigravity") and os.path.isdir(root):
            roots.append(root)
    home = _text((env or {}).get("HOME"))
    if home:
        for product in STORE_PRODUCTS:
            cand = os.path.join(home, ".gemini", product)
            if cand not in roots and os.path.isdir(cand):
                roots.append(cand)
    return roots


def _write_annotation_title(root: str, cid: str, title: str) -> bool:
    """Write `title:"<escaped>"` into `<root>/annotations/<cid>.pbtxt`, preserving any other fields."""
    ann_dir = os.path.join(root, "annotations")
    path = os.path.join(ann_dir, "%s.pbtxt" % cid)
    escaped = title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    field_text = 'title:"%s"' % escaped
    existing = ""
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
    except OSError:
        existing = ""
    if existing and _TITLE_PBTXT_RE.search(existing):
        updated = _TITLE_PBTXT_RE.sub(lambda _: field_text, existing, count=1)
    elif existing.strip():
        updated = "%s  %s" % (field_text, existing.lstrip())
    else:
        updated = field_text
    tmp = ""
    try:
        os.makedirs(ann_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=ann_dir, prefix=".ann-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(updated)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def antigravity_rename(request: PL.TitleRequest) -> bool:
    """Put the lettered title (`XX: Words`) on the conversation in `<root>/annotations/<cid>.pbtxt` and
    `<root>/conversation_summaries.db` so Antigravity's `AnnotationsManager` and `/resume` picker keep it."""
    cid = _text(request.session_id)
    title = PL.tidy(request.title)
    if not cid or not title or not _SAFE_CID_RE.fullmatch(cid):
        return False
    wrote = False
    for root in candidate_roots(request.data, request.env):
        if _write_annotation_title(root, cid, title):
            wrote = True
        db = os.path.join(root, "conversation_summaries.db")
        if os.path.isfile(db):
            try:
                conn = sqlite3.connect(db, timeout=STORE_WAIT)
                try:
                    with conn:
                        conn.execute(
                            "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                            (title, cid),
                        )
                    wrote = True
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                pass
    return wrote


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.either(
        PL.from_payload("session_id", "conversationId"), PL.from_env(ENV_CONVERSATION)
    ),
    parent_id=antigravity_parent,
    kind=antigravity_kind,
    current_title=stored_title,
    stored_title=stored_title,
    events={"SessionStart": "start", "PreInvocation": "prompt"},
    close_reasons=(),
    chat_payload=antigravity_chat_payload,
    title_writer=antigravity_rename,
    output="json",
    locations=("mac", "server"),
    env_off=ENV_OFF,
)


# --------------------------------------------------------------------------------------------
# The hook: the library's answer, as injected steps
# --------------------------------------------------------------------------------------------


def _note(env: Mapping[str, str], sid: str) -> dict[str, Any]:
    try:
        with open(PL.note_path(env, SEAT, sid), encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return {}
    return note if isinstance(note, dict) else {}


def message_for(body: Mapping[str, Any], env: Mapping[str, str], sid: str) -> str:
    """The text of the injected step for one library answer.  A letter is told with what is true of Antigravity: the
    chat is addressed by it, and its title does not carry it.  Any other line (no letter yet, another seat's) is
    the library's own."""
    context = _text(body.get("context"))  # not `tidy`: the library's sentence gaps stay as they are
    label = _text(body.get("label"))
    head, mark, tail = context.partition("Operator session letter:")
    if not (label and mark):
        return context
    note = _note(env, sid)
    gen = note.get("generation") or 1
    where = "server" if (note.get("location") == "server" or ", on the server." in tail) else "Mac"
    ref = "%s %s%s" % (
        DISPLAY,
        label,
        " (gen %d)" % gen if isinstance(gen, int) and gen > 1 else "",
    )
    # Rendered copy: a sentence gap is U+00A0 plus a space.
    return (
        '%sOperator session letter: %s, on the %s.  This chat is addressed as "@**%s** %s", and its title '
        'starts with "%s: "; keep that prefix if the chat is renamed.'
        % (head, ref, where, DISPLAY, label, label)
    )


def injection(text: str, env: Mapping[str, str], sid: str) -> dict[str, Any]:
    """The hook's answer for what the library printed: injected steps with the context line, or an empty object."""
    answer = PL._last_json(text)
    body = answer.get("platform_letter") if answer else None
    if not isinstance(body, dict):
        return {}
    message = message_for(body, env, sid)
    if not message:
        return {}
    return {"injectSteps": [{"systemMessage": {"systemMessage": message}}]}


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
    """`platform-letter hook --platform antigravity`: always exit 0 and always one JSON object on stdout (agy reads an
    answer from every hook), `{}` when there is nothing to inject."""
    answer: dict[str, Any] = {}
    try:
        data = normalize(PL.read_hook_input(stdin))
        if data is not None:
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
            answer = injection(printed.getvalue(), env, data["session_id"])
    except Exception as exc:  # noqa: BLE001 - the hook never blocks a chat
        try:
            (stderr or sys.stderr).write("platform-letter hook: %s\n" % type(exc).__name__)
        except (OSError, ValueError):
            pass
        answer = {}
    stdout.write(json.dumps(answer) + "\n")
    return 0


# --------------------------------------------------------------------------------------------
# Installing the hook group
# --------------------------------------------------------------------------------------------


def entry(command: str, timeout: int, event: str) -> dict[str, Any]:
    """One handler in agy's shape: SessionStart and PreInvocation take a flat list of handlers, with no matcher."""
    return {"type": "command", "command": command, "timeout": timeout}


INSTALL = PL.JsonHookInstaller(
    path=HOOKS_FILE,
    events=HOOK_TIMEOUTS,
    locate=PL.named_group(GROUP, enabled=True),
    build=entry,
    trust="",
)


def _load(path: str) -> dict[str, Any] | None:
    """hooks.json as an object, or None when it cannot be read as one."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _save(path: str, doc: Mapping[str, Any]) -> str:
    """Rewrite hooks.json with `doc`, after a backup (the name is returned), keeping the file's mode."""
    mode = os.stat(path).st_mode & 0o777
    backup = PL._backup(path)
    tmp = path + ".letter.tmp"
    PL._write_private(
        tmp, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), mode
    )
    os.replace(tmp, path)
    return backup


def prune_group(path: str) -> bool:
    """Take this adapter's group out of hooks.json once nothing is left in it but `enabled`, after a backup.  The
    group is this adapter's own, so removing it is part of removing the hooks; any group with another key stays."""
    doc = _load(path)
    group = doc.get(GROUP) if doc else None
    if doc is None or not isinstance(group, dict) or set(group) - {"enabled"}:
        return False
    del doc[GROUP]
    _save(path, doc)
    return True


def _switched_off(value: object) -> bool:
    """`"enabled": false`, and the 0 a hand edit may leave for it;  a missing key or null is on."""
    return value is False or (isinstance(value, int) and not isinstance(value, bool) and value == 0)


def enable_group(path: str) -> str:
    """Switch the group back on when it is off (`"enabled": false`, which is how a person or agy turns a group off, and
    agy then runs none of its hooks), after a backup.  Returns the backup's name, or "" when the group was on or absent.
    Installing is asking for the hooks to run, so it undoes the switch, as the lane-guard installer does for its own
    group; any other hook in the group is switched back on with it."""
    doc = _load(path)
    group = doc.get(GROUP) if doc else None
    if doc is None or not isinstance(group, dict) or not _switched_off(group.get("enabled")):
        return ""
    group["enabled"] = True
    return _save(path, doc)


def command_path(home: str) -> str:
    """Where the hook command this adapter writes lives: the host's own `platform-letter`."""
    return os.path.join(home, ".local", "bin", PL.ENTRY)


def unrunnable(path: str) -> str:
    """Why `path` cannot be run as a hook command ("does not exist", "is not executable"), or "" when it can.  A link
    that points nowhere is not a file."""
    if not os.path.isfile(path):
        return "does not exist"
    if not os.access(path, os.X_OK):
        return "is not executable"
    return ""


def hook_problems(path: str) -> list[str]:
    """What keeps the hooks that are installed in hooks.json from running: the group switched off, or a hook command
    that is not there (agy would run it before every model call, and fail).  Empty when all is well, and when nothing is
    installed (the installer's own answer says that)."""
    doc = _load(path)
    group = doc.get(GROUP) if doc else None
    if not isinstance(group, dict):
        return []
    problems = []
    if _switched_off(group.get("enabled")):
        problems.append(
            'the "%s" group is switched off ("enabled": false), so agy runs none of its hooks'
            % GROUP
        )
    used: dict[str, list[str]] = {}  # each command that is installed, with the events that run it
    for event in HOOK_TIMEOUTS:
        entries = group.get(event)
        for item in entries if isinstance(entries, list) else []:
            words = PL.installed_command(item, PLATFORM)
            if words:
                used.setdefault(words[0], []).append(event)
    for command, events in used.items():
        why = unrunnable(command)
        if why:
            problems.append("the hook command %s %s (%s)" % (command, why, ", ".join(events)))
    return problems


def install(options: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """`platform-letter install --platform antigravity [--check | --remove]`: the `session-letter` group of
    ~/.gemini/config/hooks.json, next to the groups already there, which are never touched.  `--remove` drops the
    group when its last handler is gone.  agy reads the file when a session starts, so a session that is already
    running is not changed (not checked).

    The hooks have to be able to run, and the installer says so rather than report a file that is merely well formed.
    An install writes nothing (exit 2) while the command it would write is not there, because agy would run a missing
    command before every model call; it switches the group on if it is off.  `--check` adds `problems` and `healthy`
    to the answer, and exits 1 for a group that is off or a command that is missing; `current` then reads false for
    every event, since a healthy install is not what is there.  `--remove` needs neither."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    path = os.path.join(home, HOOKS_FILE)
    command = command_path(home)
    if not (options.check or options.remove):
        why = unrunnable(command)
        if why:
            return PL._emit(
                stdout,
                {
                    "platform": PLATFORM,
                    "changed": [],
                    "path": path,
                    "error": "nothing written: the hook command %s %s.  The Mac links it with "
                    "scripts/simple_tools/install.sh, and the server gets it from the fleet-cli release."
                    % (command, why),
                },
                2,
            )
    printed = io.StringIO()
    code = PL.install_json_hooks(INSTALL, options, env, printed)
    answer = PL._last_json(printed.getvalue()) or {}
    if options.remove:
        if answer.get("changed") and prune_group(path):
            answer["removed_group"] = GROUP
    elif options.check:
        problems = hook_problems(path)
        if problems:
            answer["problems"] = problems
            answer["current"] = {event: False for event in HOOK_TIMEOUTS}
            code = code or 1
        answer["healthy"] = not problems and code == 0
    elif code == 0:
        backup = enable_group(path)
        if backup:
            answer["enabled_group"] = GROUP
            answer["enable_backup"] = backup
    return PL._emit(stdout, answer, code)


def sweep_main(argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    """`sweep [--apply] [--db FILE] [--location server|mac]`: close the registrations of conversations the summaries
    store has marked `killed` (or dropped), which fire no SessionEnd hook of their own.  Without --apply it only
    reports."""
    import argparse
    import concurrent.futures

    parser = argparse.ArgumentParser(prog="platform-letter-antigravity sweep")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default="")
    parser.add_argument("--location", default="server", choices=PL.LOCATIONS)
    args = parser.parse_args(argv)
    home = str(env.get("HOME") or os.path.expanduser("~"))
    dbs = (
        [args.db]
        if args.db
        else [
            os.path.join(home, ".gemini", product, "conversation_summaries.db")
            for product in STORE_PRODUCTS
            if os.path.isfile(os.path.join(home, ".gemini", product, "conversation_summaries.db"))
        ]
    )
    if not dbs or not all(os.path.isfile(p) for p in dbs):
        stdout.write(
            json.dumps({"error": "no Antigravity summaries database at %s" % (args.db or home)})
            + "\n"
        )
        return 2
    shape = _SAFE_CID_RE
    gone: list[str] = []
    known: set[str] = set()
    for path in dbs:
        try:
            conn = sqlite3.connect("%s?mode=ro" % Path(path).as_uri(), uri=True, timeout=5)
            try:
                cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
                }
                if "conversation_id" not in cols:
                    rows, every = [], []
                elif "killed" in cols:
                    rows = conn.execute(
                        "SELECT conversation_id FROM conversation_summaries WHERE COALESCE(killed, 0) != 0"
                    ).fetchall()
                    every = conn.execute(
                        "SELECT conversation_id FROM conversation_summaries WHERE COALESCE(killed, 0) = 0"
                    ).fetchall()
                else:
                    rows = []
                    every = conn.execute(
                        "SELECT conversation_id FROM conversation_summaries"
                    ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            stdout.write(
                json.dumps({"error": "the store could not be read (%s)" % type(exc).__name__})
                + "\n"
            )
            return 2
        for r in rows:
            if isinstance(r[0], str) and shape.fullmatch(r[0]) and r[0] not in gone:
                gone.append(r[0])
        for r in every:
            if isinstance(r[0], str) and shape.fullmatch(r[0]):
                known.add(r[0])
    from . import registry_api as RA

    env = PL.with_registry_env(env)
    if RA.base_url(env) and known:
        answer = RA.call(env, "history", {"platform": PLATFORM, "limit": 1000})
        history = answer.get("items") if isinstance(answer, dict) else None
        if isinstance(history, list):
            for row in history:
                sid = str((row or {}).get("native_session_id") or "")
                host = str((row or {}).get("verified_host") or "")
                if (
                    shape.fullmatch(sid)
                    and (not host or host == args.location)
                    and not (row or {}).get("closed_at")
                    and str((row or {}).get("state") or "") != "closed"
                    and sid not in known
                    and sid not in gone
                ):
                    gone.append(sid)
    closed = []
    failed = []
    absent = []
    if not args.apply:
        for sid in gone:
            closed.append({"session_id": sid, "would_close": True})
    elif gone:

        def _close_one(sid: str) -> tuple[str, int, str]:
            buf = io.StringIO()
            code = PL.registry_main(
                ["close", "--platform", PLATFORM, "--location", args.location, "--session-id", sid],
                env=env,
                stdout=buf,
            )
            return sid, code, buf.getvalue()

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(gone))) as pool:
            results = list(pool.map(_close_one, gone))
        for sid, code, raw in results:
            line = PL._last_json(raw)
            text = str((line or {}).get("error") or "")
            if (
                code != 0
                and PL._NOT_REGISTERED_MARK not in text
                and "no session with this identity" not in text
                and text != PL.NO_REGISTRATION
            ):
                stdout.write(raw)
                failed.append(sid)
            elif code != 0:
                absent.append(sid)
            else:
                closed.append({"session_id": sid, "closed": True})
    stdout.write(
        json.dumps(
            {
                "platform": PLATFORM,
                "found": len(gone),
                "closed": closed,
                "absent": absent,
                "failed": failed,
            }
        )
        + "\n"
    )
    return 0 if not failed else 1


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = stdout or sys.stdout
    if argv[:1] == ["sweep"]:
        return sweep_main(argv[1:], env, out)
    out.write(
        "usage: python3 -m simple_tools.platform_letter_antigravity sweep [--apply] [--db FILE] [--location server|mac]\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
