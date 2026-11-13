"""Claude Code on `platform-letter`:  the Claude row of the one letter table (owner, Nov 11, 2026:  "one operator
that has one tool/process for all platforms").  The library (`platform_letter`) does the registering, the note, the
retries, the "??" title, the end-of-turn report, the gone report and the install;  this file is only what is Claude
Code's own.

- The hooks are Claude Code's:  SessionStart starts, UserPromptSubmit prompts, Stop ends a turn and SessionEnd ends
  the chat.  The session id is the payload's `session_id` (CLAUDE_CODE_SESSION_ID in the chat's own shell, for
  `simple-chat letter request`).
- The title goes out in the hook's own answer (`hookSpecificOutput.sessionTitle`), which Claude Code applies after a
  startup, resume or fork start and on a prompt.  The payload's `session_title` is the title the chat shows now.
- A chat ends for good on `/clear` and on logout, which frees its letter;  every other end keeps it for a resume.
  Archiving a chat sends no end reason of its own (it arrives as `other`, which an app quit or a dropped ssh
  connection also sends), so an archived chat's letter is freed by the hooks' gone report instead (`claude_gone`).
- Only interactive chats take part:  CLAUDE_CODE_ENTRYPOINT `cli` or `claude-desktop`, not a `claude -p` or SDK run,
  and not a session SIMPLE_TOOLS_ATTACH=0 or a launcher started.

The desktop app's record of a chat.  The app keeps one JSON file per chat on the Mac, at

    <home>/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json

The file name is the app's own id for the chat (CLAUDE_CODE_HOST_SESSION_ID in the chat's process), and its
`cliSessionId` is the chat's Claude Code session UUID, the key the Operator's registry uses.  The record is the app's
private, unversioned file, and it also holds the chat's MCP wiring and message text, so each reader below takes only
the fields it names and drops the rest before a record leaves its file.  Every read is bounded, every id is checked
before it builds a path, a file caught mid-write is read again, and a surprise of any kind is "not known":  a hook
never fails a chat's start over this file.

- The parent (`claude_parent`, `lookup`).  A chat another chat started carries `spawnedFrom.sessionId`, the parent's
  app id, whose record holds the parent's session UUID.  The chat's own record is found by its app id, else by its
  session UUID among the newest records.  A `claude` started inside a chat (a Bash tool, tmux) inherits that chat's
  app id, so a record whose valid `cliSessionId` is not this chat's is not used:  this chat is then looked for by its
  UUID, and a nested run, which has no record, has no parent.  A parent is a session id that is not this chat's own;
  anything else is no parent, and the chat is picked like any other.  A user fork (`forkedFromSessionId`) is not a
  sub-thread here.
- A parent, asked of the Mac (`parent_from_mac`).  The app keeps its records on the Mac, and it runs most chats
  on the server over ssh, where the chat's hook finds no store.  A hook on a host with no store asks the Mac, when
  it was told where the Mac is (SESSION_LETTER_MAC_SSH, the hook's `--mac-ssh`):  one read-only ssh call whose
  whole command is the question (`platform_letter.parent_ssh_command`), answered with the parent's session id or
  nothing.  On the Mac the key is forced to `platform-letter parent-ssh-gate`, which matches that command whole
  and runs nothing else, so the key gives no shell.  The ids are checked before they reach the command line, the
  call is bounded (MAC_TIMEOUT_SECONDS), and a Mac that does not answer is no parent.  Nothing but that question
  crosses:  the registry is never reached over ssh.
- A parent registered first (`claude_chat_payload`).  The library registers a sub-thread's parent ahead of it when the
  parent holds no letter on this host, and asks this file about the parent with a payload that says so.  Only a chat
  whose record this host holds and that runs on this host is one:  a chat the app runs over ssh (`sshConfig`) has its
  registration under the other host's location, so it is never registered from here.
- The title the app shows (`claude_title_advice`).  The app takes a hook's `sessionTitle` only while the chat's title
  is the app's own (`titleSource` "auto").  A chat another session started keeps the title that session gave it
  (`titleSource` "tool"), so its letter never shows in the sidebar, whatever the hook sends.  The app's own rename
  tool is the one thing it takes, and only the chat's model can call that, so the model is told to, once per title
  (the library holds a repeat back);  a chat another chat has just started is told to check its title itself.  A
  name a person gave (`titleSource` "user") is left alone:  the tool would stop to ask them to approve it.
- A resumed chat's title (`claude_stored_title`).  A chat that is resumed with no title gets the one its first
  prompt gives, read from the first lines of its own transcript, and only a transcript under `~/.claude`.
- Archived chats (`claude_gone`, `archive_states`).  The app marks an archived chat in its record (`isArchived`).
  A chat's start has the library report this host's archived chats to the assigner, which frees their letters
  (`platform-letter gone --platform claude` shows them).  Any doubt keeps a chat:  an unreadable or missing
  record, or a session id held by two records that do not both say archived.  A chat the app runs over ssh has
  its note on the other host, so the Mac names those from its records alone (`claude_gone_elsewhere`):  archived,
  run over ssh, with a session id, the record changed within REMOTE_SCAN_DAYS and not in the last hour.

Installing.  `platform-letter install --platform claude [--location server|mac] [--check | --remove]` adds the four
hooks to `~/.claude/settings.json` with the shared JSON installer, and replaces in place the `simple-chat letter
hook` entries installed before Claude joined the table.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import identity as I
from . import platform_letter as PL
from . import secretscan
from . import session_ids as SI

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
ENV_HOST_SESSION = "CLAUDE_CODE_HOST_SESSION_ID"

STORE = ("Library", "Application Support", "Claude", "claude-code-sessions")
# The app's id for a chat.  It builds a file name, so nothing but these characters is ever used.
HOST_ID = re.compile(r"local_[A-Za-z0-9-]{1,80}")
# A record is about 0.65 MB (it holds the chat's MCP wiring);  nothing near this is a record.
RECORD_BYTES = 8 << 20
SCAN_RECORDS = 300  # the newest records looked through when the chat's app id is not known
SCAN_BYTES = 128 << 20  # the most a scan reads, counting a file too big to read at its limit
LOAD_ATTEMPTS = 3  # reads of a record the app may be rewriting
# Seconds between them:  longer than a truncate-then-write leaves a file empty, short for a hook.
RETRY_PAUSE = 0.05

# Why no parent was found (`lookup`).  "" means one was.
NO_STORE = "no_store"
NO_RECORD = "no_record"
NOT_STARTED = "not_started_by_chat"
PARENT_GONE = "parent_gone"
PARENT_NEVER_RAN = "parent_never_ran"
UNREADABLE = "unreadable"
SAME_CHAT = "parent_is_self"

#: The payload key that says the library is asking about a chat this hook did not fire for (`claude_chat_payload`).
ASKED_ABOUT = "asked_about_chat"
#: The `titleSource` of a title an agent set (`start_session`'s title, or the app's rename tool).
AGENT_TITLE = "tool"
AGENT_TITLE_CHARS = 200  # the most of an agent's title handed back to the model for a rename
RENAME_TOOL = "mcp__ccd_session_mgmt__set_session_title"
READ_TOOL = "mcp__ccd_session_mgmt__get_session"

#: How a record names its chat's session id, wherever in the record that is.
CLI_ID_VALUE = re.compile(r'"cliSessionId"\s*:\s*"([^"\\]{1,200})"')
# Chats the app runs on another host (`claude_gone_elsewhere`).  Such a chat's hook runs there and says `--location
# server`, as every host that is not the Mac must, so that is the location its registration is under.  Only the
# newest REMOTE_SCAN_RECORDS records changed in the last REMOTE_SCAN_DAYS are looked at (an older chat's letter
# went with the assigner's idle sweep long ago), and one changed in the last REMOTE_QUIET_SECONDS is left alone:
# the app rewrites a record as its chat works, so that chat is plainly not gone.
REMOTE_LOCATION = "server"
REMOTE_SCAN_DAYS = 14
REMOTE_SCAN_RECORDS = 300
REMOTE_QUIET_SECONDS = 3600
# Asking the Mac for a chat's parent (`parent_from_mac`):  how long ssh may take to connect, and the whole round
# trip (an asleep or unreachable Mac costs this much, once per RETRY_SECONDS for a chat).  It must fit the
# tightest hook budget together with what follows it in the same hook run.  A chat with no letter asks on a
# prompt or a turn end, whose budget is 15 seconds:  2 for this call, then the register chain of at most three
# registry calls of 4 seconds each (platform_letter._API_TIMEOUT), 14 in all.  A start has 20:  2, the same 12,
# and one gone report of 4.
MAC_CONNECT_SECONDS = 2
MAC_TIMEOUT_SECONDS = 2.0
# ssh as it is run:  no prompt, the connect bounded, no connection shared with (or left behind for) any other
# ssh of the account (ControlMaster=no starts none and ControlPath=none joins none, so an open master of the account
# can never carry this command, and it never runs on a channel that skips the key's forced command), nothing
# forwarded, and only the key the destination's own ssh config names (IdentitiesOnly=yes, so an agent's other keys are
# not offered first), whatever the account's own ssh config says for the host.  What bounds the child when the hook that
# started it is killed (a SIGTERM runs no cleanup):  ConnectTimeout=2 ends a connect, ServerAliveInterval=1 with
# ServerAliveCountMax=2 ends a connected session whose peer stops answering after about 2 seconds more, BatchMode never
# waits for a person, and the command on the Mac is a short read-only lookup.  A live hook kills it at the hard timeout
# (`subprocess.run` kills and waits), so the child never outlives its hook by more than about 4 seconds.
SSH_OPTIONS = (
    "BatchMode=yes",
    "ConnectTimeout=%d" % MAC_CONNECT_SECONDS,
    "ServerAliveInterval=1",
    "ServerAliveCountMax=2",
    "ControlMaster=no",
    "ControlPath=none",
    "IdentitiesOnly=yes",
    "ClearAllForwardings=yes",
    "ForwardAgent=no",
)
# A resumed chat's title (`claude_stored_title`):  how much of its transcript is read for its first prompt.
TRANSCRIPT_LINES = 400
TRANSCRIPT_BYTES = 4 << 20
# A chat's state in the app (`archive_states`).  A chat with no record at all is left out of the answer.
ARCHIVED = "archived"
UNARCHIVED = "unarchived"
# The archive reader reads every record, not the newest few, because a chat archived long after it last ran can sit
# anywhere in the folder.  These bound it:  a record past them is not looked at, and once any record was not looked
# at no chat is called archived, so the bound can only keep a letter, never free one.
ARCHIVE_SCAN_RECORDS = 3000
# A record is about 0.65 MB (the real store:  112 records, 69 MB, a scan of it takes 0.15 to 0.5 s with the files
# cached).  512 MiB is some 800 records, or 5 s of reading at a slow disk's 100 MB/s, so ARCHIVE_SCAN_SECONDS bounds a
# scan by the clock as well:  a hook scans twice (for its own chats and for the ones the app runs elsewhere) inside one
# SessionStart's budget of 20 seconds, after 2 for the ssh call and 12 for the registry calls, so two scans may take
# 4 at most.  A scan that runs out of either stops, and what it did not read keeps its chats (`unseen`).
ARCHIVE_SCAN_BYTES = 512 << 20
ARCHIVE_SCAN_SECONDS = 2.0


# --------------------------------------------------------------------------------------------
# Reading the desktop app's records
# --------------------------------------------------------------------------------------------


def _home(env: Mapping[str, str]) -> str:
    return str(env.get("HOME") or os.path.expanduser("~"))


def store_root(home: str) -> str:
    return os.path.join(home, *STORE)


def _subdirs(path: str) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _org_dirs(root: str) -> list[str]:
    return [org for account in _subdirs(root) for org in _subdirs(account)]


def _find(root: str, host_id: str) -> str:
    """The path of the record named for an app id, or "" (the id is checked here, so it cannot leave the store)."""
    if not isinstance(host_id, str) or not HOST_ID.fullmatch(host_id):
        return ""
    for org in _org_dirs(root):
        path = os.path.join(org, host_id + ".json")
        if os.path.isfile(path) and not os.path.islink(path):
            return path
    return ""


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(RECORD_BYTES + 1)
    except OSError:
        return None
    return text if len(text) <= RECORD_BYTES else None


Projection = Callable[[object], dict[str, Any] | None]


def _project(data: object) -> dict[str, Any] | None:
    """The lineage fields of a record and nothing else of it (its title, its MCP wiring and its message text stay in
    the file), or None when the record is not an object:  its session UUID, the app id of the chat that started it,
    whether that parent was deleted, and whether the app runs the chat on another host over ssh."""
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {"remote": bool(data.get("sshConfig"))}
    if isinstance(data.get("cliSessionId"), str):
        out["cliSessionId"] = data["cliSessionId"]
    spawned = data.get("spawnedFrom")
    if isinstance(spawned, dict):
        out["spawnedFrom"] = {
            "sessionId": spawned["sessionId"] if isinstance(spawned.get("sessionId"), str) else "",
            "deletedParent": bool(spawned.get("deletedParent")),
        }
    return out


def _project_title(data: object) -> dict[str, Any] | None:
    """The title fields of a record, `cliSessionId`, `title` and `titleSource`, and nothing else of it."""
    if not isinstance(data, dict):
        return None
    return {
        key: data[key]
        for key in ("cliSessionId", "title", "titleSource")
        if isinstance(data.get(key), str)
    }


def _archived_flag(data: Mapping[str, Any]) -> bool | None:
    """What a record's `isArchived` says:  True, False (a field that is absent says the chat is not archived), or None
    when the field is there and is not a flag.  A JSON true or 1 is archived and a false or 0 is not;  anything else (a
    string, null, a list, a number past 1) is not a flag at all, and the chat is kept (`archive_states` answers it
    UNREADABLE) rather than guessed at."""
    if "isArchived" not in data:
        return False
    value = data["isArchived"]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _project_archive(data: object) -> dict[str, Any] | None:
    """The two fields the archive reader takes, `cliSessionId` and `isArchived`, and nothing else of the record."""
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {"isArchived": _archived_flag(data)}
    if isinstance(data.get("cliSessionId"), str):
        out["cliSessionId"] = data["cliSessionId"]
    return out


def _parse(text: str, project: Projection = _project) -> dict[str, Any] | None:
    try:
        return project(json.loads(text))
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser will follow
        return None


def _load(path: str, project: Projection = _project) -> dict[str, Any] | None:
    """What `project` keeps of the record, or None when it cannot be read.  A file the app is rewriting (a truncate,
    then the write) can be caught empty or half written, and a read right after lands in the same window, so it is
    read up to LOAD_ATTEMPTS times with a short pause between."""
    for attempt in range(LOAD_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSE)
        text = _read(path)
        if text is None:
            return None
        record = _parse(text, project)
        if record is not None:
            return record
    return None


def _session_id(value: object) -> str:
    """`value` when it is a session id, else ""."""
    if not isinstance(value, str):
        return ""
    try:
        return SI.check(value, "the chat's session id")
    except ValueError:
        return ""


def _cli_id(record: Mapping[str, Any]) -> str:
    return _session_id(record.get("cliSessionId"))


def _records(root: str) -> list[tuple[float, int, str]]:
    """(modified, size, path) of every record in the store, newest first."""
    files: list[tuple[float, int, str]] = []
    for org in _org_dirs(root):
        try:
            with os.scandir(org) as entries:
                for e in entries:
                    try:
                        if (
                            e.name.endswith(".json")
                            and HOST_ID.fullmatch(e.name[: -len(".json")])
                            and e.is_file(follow_symlinks=False)
                        ):
                            st = e.stat()
                            files.append((st.st_mtime, st.st_size, e.path))
                    except OSError:
                        continue  # one file that vanished, not the rest of the folder
        except OSError:
            continue
    files.sort(reverse=True)
    return files


def _record_for_cli_id(root: str, cli_session_id: str) -> tuple[dict[str, Any] | None, bool]:
    """(the record whose cliSessionId is this Claude Code session UUID, or None;  whether one that held the UUID could
    not be read), found by looking through the newest records."""
    spent = 0
    unreadable = False
    for _, size, path in _records(root)[:SCAN_RECORDS]:
        if size > RECORD_BYTES:
            spent += RECORD_BYTES
        else:
            text = _read(path)
            if text is not None:
                spent += len(text)
                # Most records do not hold the id at all, so most are never parsed.
                if cli_session_id in text:
                    record = _parse(text)
                    if record is None:
                        record = _load(path)  # caught mid-write:  read it once more
                    if record is None:
                        unreadable = True
                    elif record.get("cliSessionId") == cli_session_id:
                        return record, unreadable
        if spent > SCAN_BYTES:
            break
    return None, unreadable


# --------------------------------------------------------------------------------------------
# Which chat started this one
# --------------------------------------------------------------------------------------------


def lookup(
    home: str, *, host_session_id: str = "", cli_session_id: str = "", allow_lag: bool = False
) -> tuple[str, str]:
    """(the Claude Code session UUID of the chat that started this one, why none):  ("<uuid>", "") when one is known,
    ("", reason) otherwise, the reason one of the constants above.

    The chat is found by its app id (`host_session_id`, from CLAUDE_CODE_HOST_SESSION_ID), else by its Claude Code
    session UUID.  Only a chat the app started from another chat (`spawnedFrom`) has a parent.

    A `claude` started inside a chat (a Bash tool, tmux) inherits that chat's app id, so the record the id names can
    be the outer chat's, not this one's.  When that record's own session UUID is valid and is not this chat's, it is
    not used:  this chat is looked for by its session UUID instead, and a nested run, which has no record, has no
    parent.  `allow_lag` lets the app id decide anyway, for the start after a `/clear`, when the app may not have
    written the new session UUID into the record yet."""
    root = store_root(home)
    if not os.path.isdir(root):
        return "", NO_STORE
    cli_session_id = _session_id(cli_session_id)
    path = _find(root, host_session_id) if host_session_id else ""
    record = None
    if path:
        record = _load(path)
        if record is None:
            return "", UNREADABLE
        mine = _cli_id(record)
        if mine and cli_session_id and mine != cli_session_id and not allow_lag:
            record = None  # another chat's record:  this chat is judged by its own session UUID
    if record is None:
        if not cli_session_id:
            return "", NO_RECORD
        record, unreadable = _record_for_cli_id(root, cli_session_id)
        if record is None:
            return "", UNREADABLE if unreadable else NO_RECORD
        # The app id named another chat's record, so it says nothing about this one.
        host_session_id = ""
    spawned = record.get("spawnedFrom")
    if not spawned or not spawned["sessionId"]:
        return "", NOT_STARTED
    parent_host = spawned["sessionId"]
    if spawned["deletedParent"]:
        return "", PARENT_GONE
    if parent_host == host_session_id:
        return "", SAME_CHAT
    parent_path = _find(root, parent_host)
    if not parent_path:
        return "", PARENT_GONE
    parent_record = _load(parent_path)
    if parent_record is None:
        return "", UNREADABLE
    parent = _cli_id(parent_record)
    if not parent:
        return "", PARENT_NEVER_RAN
    if parent == (_cli_id(record) or cli_session_id):
        return "", SAME_CHAT
    return parent, ""


def _run_ssh(cmd: list[str], timeout: float) -> Any:
    """Run one ssh command with nothing on its standard input and its own messages thrown away (a banner or a
    warning is not the answer, and a full pipe nobody reads must not hold it up).  The one seam a test replaces."""
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )


def _answered(out: object) -> str:
    """The session id on the last line ssh printed, or "".  Only a whole line counts:  an answer cut off before its
    newline is not one."""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    if not isinstance(out, str) or not out.endswith("\n"):
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return _session_id(lines[-1]) if lines else ""


#: What the Mac answered in this process, so one hook run asks it once however many readers want the parent.
_ASKED: dict[tuple[str, ...], str] = {}


def parent_from_mac(
    env: Mapping[str, str], sid: str, host_session_id: str = "", cleared: bool = False
) -> str:
    """The session id of the chat that started chat `sid`, asked of the Mac, or "".

    The desktop app's records are on the Mac, and a chat the app runs over ssh has its hook on the other host.  So
    a hook that finds no store asks the Mac, with ONE ssh call that only reads.  The command it sends is the
    question itself, exactly the string the Mac's gate accepts (`platform_letter.parent_ssh_command`):  the key is
    forced to `platform-letter parent-ssh-gate` there, which matches the string whole and answers with the
    parent's session id or nothing, so the key can run nothing else and no shell reads the ids.  The destination
    is the host's own setting (`platform_letter.mac_ssh`);  with none, nothing is asked.  The session id and the
    app id are checked against their own patterns first (an app id that fails is left out, a session id that
    fails asks nothing), ssh may not prompt, shares no connection and forwards nothing (SSH_OPTIONS), and the
    round trip is cut at MAC_TIMEOUT_SECONDS.  An answer that arrived whole before the cut still counts.  Any
    failure, a timeout with no answer, or an answer that is not a session id is no parent."""
    dest = PL.mac_ssh(env)
    sid = _session_id(sid)
    if not dest or not sid:
        return ""
    app_id = ""
    if isinstance(host_session_id, str) and HOST_ID.fullmatch(host_session_id):
        app_id = host_session_id
    remote = PL.parent_ssh_command(PLATFORM, sid, app_id, "clear" if cleared else "")
    if not remote:
        return ""  # not a command the gate would take:  nothing is sent
    key = (dest, remote)
    if key in _ASKED:
        return _ASKED[key]
    cmd = ["ssh", "-T"]
    for option in SSH_OPTIONS:
        cmd += ["-o", option]
    cmd += ["--", dest, remote]
    parent = ""
    try:
        done = _run_ssh(cmd, MAC_TIMEOUT_SECONDS)
        if done.returncode == 0:
            parent = _answered(done.stdout)
    except subprocess.TimeoutExpired as late:
        # The answer may be in hand though ssh did not exit (something of the account's still holds its pipe).
        parent = _answered(late.stdout)
    except (OSError, subprocess.SubprocessError):
        parent = ""  # the Mac is unreachable, or ssh could not run:  no parent is known
    if parent == sid:
        parent = ""
    _ASKED[key] = parent
    return parent


def claude_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The session id of the chat that started this one, from the desktop app's records, or "".

    On a host with no app store (the server, where the app's chats run over ssh), the Mac is asked
    (`parent_from_mac`).  Asked by another host through the `parent` verb, this host answers from its own records
    only, with the app id the question carries.

    A parent is a non-empty session id that is not this chat's own.  Anything unreadable or malformed is no parent:
    the lookup is a hint the chat was never owed, so nothing it can raise stops a chat's start.  The start after a
    `/clear` lets the app id decide (`allow_lag`), because the app may not have written the new session id yet."""
    sid = _session_id(PL.from_payload("session_id")(payload, env))
    if not sid:
        return ""
    cleared = payload.get("hook_event_name") == "SessionStart" and payload.get("source") == "clear"
    # Another host's question, through the `parent` verb, is answered from this host's own records.
    asked = payload.get(PL.PARENT_VERB) is True
    host = str(env.get(ENV_HOST_SESSION) or "")
    if asked and isinstance(payload.get(PL.PARENT_APP_ID), str):
        host = payload[PL.PARENT_APP_ID]
    try:
        if not asked and not os.path.isdir(store_root(_home(env))):
            parent = parent_from_mac(env, sid, host, cleared)
        else:
            parent, _why = lookup(
                _home(env), host_session_id=host, cli_session_id=sid, allow_lag=cleared
            )
    except Exception:  # noqa: BLE001 - the app's private file;  a surprise is no parent, never a failed start
        return ""
    if not isinstance(parent, str) or not parent or parent == sid:
        return ""
    return parent


def claude_chat_payload(sid: str) -> Mapping[str, Any]:
    """A chat's own start event, as a resume sends it, marked as the library's question about a chat this hook did
    not fire for (a sub-thread's parent, registered ahead of the sub-thread)."""
    return {
        "hook_event_name": "SessionStart",
        "source": "resume",
        "session_id": sid,
        ASKED_ABOUT: True,
    }


def _runs_here(home: str, sid: str) -> bool:
    """This host's app holds a record of the chat, and runs it on this host (not over ssh).  Any doubt is False."""
    sid = _session_id(sid)
    if not sid:
        return False
    try:
        record, _unreadable = _record_for_cli_id(store_root(home), sid)
    except Exception:  # noqa: BLE001 - the app's private file;  a surprise registers nothing
        return False
    return record is not None and not record["remote"]


def claude_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """An interactive chat, or a headless run (`claude -p`, the SDK, a session the attach switch turns off).  A Grok
    run that imports the Claude hooks and inherited CLAUDE_CODE_ENTRYPOINT is Grok's chat, never Claude's.

    Asked about a chat this hook did not fire for (`claude_chat_payload`), the answer is "chat" only for one the app
    runs on this host:  the library would register it under this host's location, and a chat the app runs over ssh,
    or one this host has no record of, is registered by its own hook where it runs."""
    if str(env.get(I.ENV_ATTACH, "")).strip() == "0":
        return "headless"
    if env.get("GROK_HOOK_EVENT") or env.get("GROK_SESSION_ID"):
        return "headless"
    if env.get("CLAUDE_CODE_ENTRYPOINT") not in ENTRYPOINTS:
        return "headless"
    if payload.get(ASKED_ABOUT) is True:
        sid = PL.from_payload("session_id")(payload, env)
        return "chat" if _runs_here(_home(env), sid) else "headless"
    return "chat"


# --------------------------------------------------------------------------------------------
# A resumed chat's title
# --------------------------------------------------------------------------------------------


def _user_texts(entry: Mapping[str, Any]) -> list[str]:
    """What a person typed in one transcript entry, or nothing for any other entry."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        ]
    return []


def claude_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The title a resumed chat's first prompt gives, read from its own transcript (`transcript_path`), or "".

    Only a transcript under `~/.claude` is read, and only its first lines (TRANSCRIPT_LINES, TRANSCRIPT_BYTES, a
    line that runs past the bound is not read to its end), so a stray path in the hook input cannot point anywhere
    else and one long line cannot fill memory.  The words go through the library's own title
    rule, the one a first prompt goes through, secret scan included."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return ""
    root = os.path.realpath(os.path.join(_home(env), ".claude")) + os.sep
    real = os.path.realpath(path)
    if not real.startswith(root) or not os.path.isfile(real):
        return ""
    read = 0
    try:
        # Binary, so the bound counts bytes (a line of multibyte characters is no shorter for its characters), and the
        # text is decoded only after a line is in hand.
        with open(real, "rb") as fh:
            for _ in range(TRANSCRIPT_LINES):
                # Never more than the bound in memory:  one line can be as long as a pasted file.
                room = TRANSCRIPT_BYTES - read
                line = fh.readline(room + 1) if room > 0 else b""
                read += len(line)
                if not line or len(line) > room:
                    break  # the end, or a line that runs past the bound:  it is not read on
                try:
                    entry = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                for words in _user_texts(entry):
                    if words.lstrip().startswith(("<", "Caveat:")):
                        continue
                    title = PL.title_from_prompt(words)
                    if title:
                        return title
    except OSError:
        return ""
    return ""


# --------------------------------------------------------------------------------------------
# The title the app shows
# --------------------------------------------------------------------------------------------


def shown_title(
    home: str, *, host_session_id: str = "", cli_session_id: str = ""
) -> tuple[str, str] | None:
    """(`titleSource`, `title`) from the chat's own record, or None when they are not known.

    Only the chat's own record counts:  an app id inherited by a `claude` started inside another chat names that
    chat's record, whose session UUID does not match.  Any surprise answers None, which asks nothing of the chat."""
    path = _find(store_root(home), host_session_id) if host_session_id else ""
    if not path or not cli_session_id:
        return None
    record = _load(path, _project_title)
    if record is None or record.get("cliSessionId") != cli_session_id:
        return None
    return str(record.get("titleSource") or ""), str(record.get("title") or "")


def _reads_secret(line: str) -> bool:
    return bool(secretscan.scan(line)) or any(secretscan.scan(word) for word in line.split(" "))


def _own_words(line: str, labels: Iterable[str]) -> str:
    """`line` without the letters this chat holds or held, however many of them a rename stacked in front of it."""
    held = tuple(labels)
    for _ in range(len(held) + 1):
        words = PL.bare(line, held) if held else line
        if words == line:
            break
        line = words
    return line


def _rename_line(known: tuple[str, str], label: str, labels: Iterable[str]) -> str:
    """The line that has the model put the letter on a title an agent gave the chat, or "" when the app's title
    needs nothing:  it is not an agent's, it carries the letter already, it reads as a secret, or it has no words."""
    source, named = known
    prefix = "%s: " % label
    line = PL.tidy(named)
    if source != AGENT_TITLE or not line or line.startswith(prefix):
        return ""
    if _reads_secret(line):
        # Nothing of the title goes on:  the model would copy it into the app, and on to the registry.
        return ""
    # Every letter this chat has held comes off, because a title the model set under an earlier letter must not
    # stack a second letter in front of it, and "PR: Review" from a parent must keep its acronym.
    words = _own_words(line, labels)
    if len(words) > AGENT_TITLE_CHARS:
        cut = words[:AGENT_TITLE_CHARS].rsplit(" ", 1)[0]
        words = (cut or words[:AGENT_TITLE_CHARS]).rstrip(" ,;:-")
    if not words or words == PL.PLACEHOLDER:
        return ""
    return (
        "The app is showing the title another session gave this chat, without its letter, and it ignores a title "
        'from this hook.  Before other work, call %s with session_id "self" and title %s (load it with '
        "ToolSearch if it is deferred).  Do not mention this to the user."
        % (RENAME_TOOL, json.dumps(prefix + words, ensure_ascii=False))
    )


def claude_title_advice(request: PL.TitleRequest) -> str:
    """What the chat's model is told when the app will not take this hook's title, or "".

    The desktop app ignores a hook's title once an agent has named the chat, so a chat started through
    `start_session` keeps the title its parent gave it and never shows its letter.  The app's own rename tool is the
    only thing it takes, and only the model can call that, so the line names the tool and the exact title:  the
    letter in front of the words the app shows (`_rename_line`).  A person's rename is left alone, because the tool
    would stop to ask them to approve it.  A chat with no letter is asked nothing:  the app's title stays until a
    letter comes.

    A concrete rename needs the chat's own record, and a chat another chat just started may not have its session id
    or its title in its record yet.  So a start that is not a resume, of a chat another chat started, tells the model
    to look at its title itself and put the letter on it, whenever no concrete rename was found."""
    label = request.label
    if not label:
        return ""
    try:
        known = shown_title(
            _home(request.env),
            host_session_id=str(request.env.get(ENV_HOST_SESSION) or ""),
            cli_session_id=request.session_id,
        )
    except Exception:  # noqa: BLE001 - the app's private file;  a surprise must never stop a chat's hook
        known = None
    fix = _rename_line(known, label, request.held) if known is not None else ""
    if fix:
        return fix
    resumed = PL.from_payload("source")(request.data, request.env) == "resume"
    if request.reason != "start" or resumed or not claude_parent(request.data, request.env):
        return ""
    return (
        "Another chat started this one, and the app shows the title that chat gave it over the one this hook "
        'sets.  As your first step, unprompted, call %s with session_id "self" (load it with ToolSearch if it '
        'is deferred).  If the title does not start with "%s: ", call %s with session_id "self" and the title '
        '"%s: <that title>".' % (READ_TOOL, label, RENAME_TOOL, label)
    )


# --------------------------------------------------------------------------------------------
# Which chats the app has archived (AFC-694)
# --------------------------------------------------------------------------------------------


def _ids_named(text: str, wanted: set[str]) -> list[str]:
    """Which of `wanted` a record's text may be the record of.

    A whole record names its chat as a `cliSessionId` value, so one pass finds every id it could be the record
    of, however many are asked about (the scan runs inside a hook).  A text that does not end as a JSON object
    does was caught mid-write and may not have reached that field yet:  there, any id it mentions at all counts,
    so a half-written record still keeps its chat."""
    if not text.rstrip().endswith("}"):
        return [sid for sid in wanted if sid in text]
    return [sid for sid in set(CLI_ID_VALUE.findall(text)) if sid in wanted]


def archive_states(home: str, session_ids: Iterable[str]) -> dict[str, str] | None:
    """For each of these Claude Code session UUIDs that a record of the app holds as its `cliSessionId`:  ARCHIVED
    when the app has archived the chat (`isArchived` is true), UNARCHIVED when it has not, UNREADABLE when a record
    that may hold it could not be read.  A UUID no record holds is left out:  a terminal
    `claude` chat, or a chat on a host with no app.  None when this host has no store at all.

    Any doubt keeps the chat:  a UUID held by two records is ARCHIVED only when every one of them says so, and one
    unreadable record that mentions the UUID makes it UNREADABLE.  A record that was not looked at (too big to
    read, not openable, or past the scan's bounds) could be a live record of any chat asked about, so when
    there is one, every UUID with no UNARCHIVED answer is UNREADABLE, found or not:  what the scan did not see can
    only keep a letter.  Only `cliSessionId` and `isArchived` leave a
    record, and a record is parsed only when its text names one of the UUIDs asked about (`_ids_named`)."""
    root = store_root(home)
    if not os.path.isdir(root):
        return None
    wanted = {sid for sid in session_ids if isinstance(sid, str) and sid}
    states: dict[str, str] = {}
    if not wanted:
        return states
    rank = {ARCHIVED: 0, UNARCHIVED: 1, UNREADABLE: 2}

    def note(sid: str, state: str) -> None:
        if rank[state] >= rank.get(states.get(sid, ARCHIVED), 0):
            states[sid] = state

    spent = 0
    began = time.monotonic()
    records = _records(root)
    unseen = len(records) > ARCHIVE_SCAN_RECORDS  # a record past the bound is one nobody looked at
    for _, size, path in records[:ARCHIVE_SCAN_RECORDS]:
        if spent > ARCHIVE_SCAN_BYTES or time.monotonic() - began > ARCHIVE_SCAN_SECONDS:
            unseen = True
            break
        if size > RECORD_BYTES:
            spent += RECORD_BYTES
            unseen = True
            continue
        text = _read(path)
        if text is None:
            unseen = True
            continue
        spent += len(text)
        named = _ids_named(text, wanted)
        if not named:
            continue
        record = _parse(text, _project_archive)
        if record is None:
            record = _load(path, _project_archive)  # caught mid-write:  read it again
        if record is None:
            for sid in named:
                note(sid, UNREADABLE)
            continue
        cli = record.get("cliSessionId")
        if cli in wanted:
            flag = record["isArchived"]
            note(cli, UNREADABLE if flag is None else ARCHIVED if flag else UNARCHIVED)
    if unseen:
        for sid in wanted:
            if states.get(sid) != UNARCHIVED:
                states[sid] = UNREADABLE
    return states


def claude_gone(env: Mapping[str, str], session_ids: Iterable[str]) -> set[str]:
    """The spec's `gone_sessions`:  which of these chats the desktop app has archived.  Ids only.  A chat is gone
    only when every record that holds its session id says it is archived;  an unarchived, unreadable or missing
    record keeps it, and a host with no app store has no gone chats."""
    states = archive_states(_home(env), session_ids) or {}
    return {sid for sid, state in states.items() if state == ARCHIVED}


def _project_remote(data: object) -> dict[str, Any] | None:
    """What `claude_gone_elsewhere` reads of a record:  its session id, whether it is archived and whether the app
    runs it over ssh.  Nothing else of the record."""
    out = _project_archive(data)
    if out is not None and isinstance(data, dict):
        out["remote"] = bool(data.get("sshConfig"))
    return out


def claude_gone_elsewhere(env: Mapping[str, str], there: str) -> set[str]:
    """The spec's `gone_elsewhere`:  the chats the desktop app on this host runs on another one (over ssh) and has
    archived.  Their notes are on the other host, which has no app to tell it of an archive, so this host names
    them from its records alone, for the location such a chat registers under (REMOTE_LOCATION).  Ids only.

    A chat is named when a record changed within REMOTE_SCAN_DAYS (and not within REMOTE_QUIET_SECONDS), among the
    newest REMOTE_SCAN_RECORDS, is archived, carries `sshConfig` and holds a session id, and every other record
    that holds that id says archived too (`archive_states`).  An unreadable record names nothing, and one that
    may hold the id keeps the chat.  A host with no app store names none."""
    home = _home(env)
    root = store_root(home)
    if there != REMOTE_LOCATION or not os.path.isdir(root):
        return set()
    now = time.time()
    candidates: set[str] = set()
    for modified, size, path in _records(root)[:REMOTE_SCAN_RECORDS]:
        age = now - modified
        if age > REMOTE_SCAN_DAYS * 86400:
            break  # newest first:  every record after this one is older still
        if age < REMOTE_QUIET_SECONDS or size > RECORD_BYTES:
            continue
        text = _read(path)
        if text is None or '"sshConfig"' not in text or '"isArchived"' not in text:
            continue
        record = _parse(text, _project_remote)
        if record is None:
            continue
        sid = _cli_id(record)
        if sid and record["isArchived"] and record["remote"]:
            candidates.add(sid)
    if not candidates:
        return set()
    states = archive_states(home, candidates) or {}
    return {sid for sid in candidates if states.get(sid) == ARCHIVED}


#: What the retired Claude client (`simple-chat letter hook`, with no flags) read from the environment:  its own
#: off switch, and the variables that are this client's `--location` and `--mac-ssh`.  A hook entry of that
#: client stays installed until someone runs `install`, and it goes on meaning what it meant.
RETIRED_ENV = {
    "off": "CLAUDE_LETTER",
    "--location": "CLAUDE_LETTER_LOCATION",
    "--mac-ssh": "CLAUDE_LETTER_REMOTE",
}

SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("session_id"),
    parent_id=claude_parent,
    kind=claude_kind,
    stored_title=claude_stored_title,
    events=EVENTS,
    close_reasons=CLOSE_REASONS,
    title_in_output=True,
    output="hook",
    chat_payload=claude_chat_payload,
    session_env=("CLAUDE_CODE_SESSION_ID",),
    title_advice=claude_title_advice,
    gone_sessions=claude_gone,
    gone_elsewhere=claude_gone_elsewhere,
    retired_env=RETIRED_ENV,
)

INSTALL = PL.JsonHookInstaller(path=os.path.join(".claude", "settings.json"), events=HOOK_TIMEOUTS)
