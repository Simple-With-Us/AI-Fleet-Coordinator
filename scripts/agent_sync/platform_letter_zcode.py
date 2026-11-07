"""ZCode on `platform-letter` (OPS-1048, owner request Nov 7, 2026):  an Operator session letter, and the title
"XX: Title", on every ZCode chat, on the server (the ZCode server the desktop app drives) and on the Mac
(the desktop app's local chats).  The library (`platform_letter`) does the registering, the note, the retries and
the title flow; this file is what is ZCode's own.

How it fits together.
- ZCode keeps Claude-style hooks in `~/.zcode/cli/config.json` under `hooks.events`, and its payloads carry the
  Claude field names beside the native ones (`session_id` beside `sessionId`), so the library's default session-id
  accessor works as it is.  The events map onto the library's names:  `SessionStart` starts (its `source` values
  are the library's own:  startup, resume, clear, compact), `UserPromptSubmit` prompts, and `Stop` is the end
  event ZCode has instead of `SessionEnd` — it fires at the end of every turn, so it touches the registration and
  never closes one (`close_reasons` is empty):  a chat that is archived or deleted fires no hook, and `sweep`
  below closes those registrations from the store.
- The session store is ZCode's own SQLite table, `~/.zcode/cli/db/db.sqlite` (`session`:  id, parent_id, title,
  title_source, task_type, directory, time_archived, time_deleted).  The parent and the kind are read from it:
  a `subagent_child` row is a sub-thread of the row its `parent_id` names, and takes the pair after its parent's
  in the parent's row (parent CC, child CD), exactly as every other platform places them.  A session the store
  does not know yet registers as a chat and is named at its next prompt.
- The title is written straight into the store, as `title_source` `generated`, and never over a name the user
  gave (`title_source` `custom`) unless the name already carries the letter.
- The hook command is not `~/.local/bin/platform-letter` itself:  it is this adapter's wrapper
  (`scripts/zcode-letter-hook/platform-letter` in the checkout, copied to `~/.local/bin/zc-letter/platform-letter`
  by the install), because a hook process inherits no profile, and the registry's URL and signing key reach it
  only through that wrapper sourcing the host's shared registry env file first.  The wrapper is named
  `platform-letter` so the installer's idempotence matcher recognises its own entries.  It resolves the checkout
  at run time — the Mac's managed runtime first, then the server's fleet-cli `current`, then the integration
  tree — so a merge updates the hook with the checkout and a release move never leaves it pointing at a dead
  path.

Who takes a letter.  Every ZCode session the hooks see, except one a launcher started (`AGENT_LAUNCHER`, the
wake runs among them) and one the off switches name (`SESSION_LETTER=0`, `ZCODE_LETTER=0`).

Installing.  `platform-letter install --platform zcode [--location server|mac] [--check | --remove]` copies the
wrapper to `~/.local/bin/zc-letter/platform-letter` (0755) and adds this adapter's entries to
`~/.zcode/cli/config.json` `hooks.events`, idempotently, with the file backed up first.  Nothing else in the
config is touched, and there is no trust step:  ZCode runs the hooks a config file lists.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

from . import identity as I
from . import platform_letter as PL
from . import session_ids as SI

PLATFORM = "zcode"
SEAT = "ZC"
DISPLAY = "ZCode"
SOURCE = "the ZC seat's session-letter hook"
ENV_OFF = "ZCODE_LETTER"

STORE_SUBPATH = os.path.join(".zcode", "cli", "db", "db.sqlite")
WRAPPER_SUBPATH = os.path.join(".local", "bin", "zc-letter", "platform-letter")

#: ZCode has no SessionEnd:  Stop ends a turn, not a chat, so it never closes a registration.
EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "Stop": "end"}
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15, "Stop": 15}
CLOSE_REASONS: tuple[str, ...] = ()

_TASK_KINDS = {"interactive": "chat", "subagent_child": "subagent"}


def store_file(env: Mapping[str, str]) -> str:
    """The session store on this host, under the home directory."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    return os.path.join(home, STORE_SUBPATH)


def wrapper_path(env: Mapping[str, str] | None = None) -> str:
    """The stable wrapper path the installed hook entries name."""
    home = str((env or {}).get("HOME") or os.path.expanduser("~"))
    return os.path.join(home, WRAPPER_SUBPATH)


class _Store:
    """One read-only connection to the session store for the life of one hook, reused by every lookup."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.path = store_file(env)
        self._conn: sqlite3.Connection | None = None

    def row(self, sid: str) -> dict[str, Any] | None:
        if not sid or not os.path.isfile(self.path):
            return None
        if self._conn is None:
            try:
                self._conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=5)
            except sqlite3.Error:
                return None
        try:
            cur = self._conn.execute(
                "SELECT id, parent_id, title, title_source, task_type, directory,"
                " time_archived, time_deleted FROM session WHERE id = ?",
                (sid,),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        keys = (
            "id",
            "parent_id",
            "title",
            "title_source",
            "task_type",
            "directory",
            "time_archived",
            "time_deleted",
        )
        return dict(zip(keys, row))


def _sid_of(payload: Mapping[str, Any]) -> str:
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def zcode_session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return _sid_of(payload)


def zcode_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The parent chat's native id, from the store's `parent_id` (a sub-agent's row names it)."""
    row = _Store(env).row(_sid_of(payload))
    if row and isinstance(row.get("parent_id"), str):
        return row["parent_id"]
    return ""


def zcode_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """chat, subagent or headless, from the store's `task_type`; an unknown session is a chat."""
    row = _Store(env).row(_sid_of(payload))
    if row:
        return _TASK_KINDS.get(str(row.get("task_type") or ""), "headless")
    return "chat"


def zcode_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    row = _Store(env).row(_sid_of(payload))
    if row and isinstance(row.get("title"), str):
        return row["title"]
    return ""


def zcode_current_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The title as the chat shows it now:  what the payload says, else what the store holds."""
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return zcode_stored_title(payload, env)


def zcode_chat_payload(sid: str) -> Mapping[str, Any]:
    """The start payload a chat's own SessionStart would carry, so the library can enroll a
    parent that holds no live registration.  The store row is not read here:  a parent the
    store does not know yet is still enrolled, titleless, and named at its next prompt."""
    return {
        "hook_event_name": "SessionStart",
        "session_id": sid,
        "source": "startup",
        "cwd": "",
    }


def zcode_rename(request: Any) -> bool:
    """Put the lettered title on the chat:  one UPDATE in the store, `title_source` `generated`.

    A name the user gave (`title_source` `custom`) that does not already carry this letter
    is never written over:  the chat keeps the user's words, and the registry keeps the
    letter, so the two say the same chat in different words."""
    env = request.env
    sid = request.session_id
    title = str(request.title or "").strip()
    if not sid or not title:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", sid):
        return False
    path = store_file(env)
    if not os.path.isfile(path):
        return False
    row = _Store(env).row(sid)
    if row and row.get("title_source") == "custom":
        current = str(row.get("title") or "")
        if current and not current.startswith(str(request.label or "") + ":"):
            return True
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE session SET title = ?, title_source = 'generated',"
                    " time_title_updated = ? WHERE id = ?",
                    (title, int(time.time()), sid),
                )
                return cur.rowcount == 1
        finally:
            conn.close()
    except sqlite3.Error:
        return False


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("session_id"),
    parent_id=zcode_parent,
    kind=zcode_kind,
    current_title=zcode_current_title,
    stored_title=zcode_stored_title,
    events=EVENTS,
    close_reasons=CLOSE_REASONS,
    quiet_events=("Stop",),
    chat_payload=zcode_chat_payload,
    title_writer=zcode_rename,
    locations=("mac", "server"),
    env_off=ENV_OFF,
)


# --------------------------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------------------------


def _locate_events(doc: dict[str, Any]) -> dict[str, Any]:
    """Where ZCode keeps its hook events:  `hooks.events`, created when absent.  The `enabled`
    flag beside it belongs to the user's config and is never touched here."""
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


def zcode_entry(command: str, timeout: int, event: str) -> dict[str, Any]:
    """One hook entry.  The command is the stable wrapper path, not the bare `platform-letter`:
    a hook process inherits no profile, and the wrapper is what sources the registry env.
    The wrapper keeps the name `platform-letter`, which is what the installer's idempotence
    matcher checks, and the rest of the words (hook, --platform, --location, --remote) pass
    through unchanged."""
    parts = shlex.split(command)
    parts[0] = wrapper_path()
    return PL.command_entry(" ".join(parts), timeout, event)


WRAPPER_BODY = """#!/bin/sh
# The ZC seat's session-letter hook command (OPS-1048).  A hook process inherits no
# profile, so this wrapper is what sources the host's shared Operator registry env file
# (the URL and the signing key the hooks sign with), and then runs the checkout's real
# platform-letter.  The checkout is resolved at run time:  the Mac's managed fleet runtime
# first, then the server's fleet-cli current, then the host's integration tree, so a merge
# updates the hook with the checkout and a release move never leaves a dead path.
# Anything missing is a no-op:  a hook must never break a chat.
[ -f "$HOME/.local/bin/mm-registry-env" ] && . "$HOME/.local/bin/mm-registry-env"
real=
for candidate in \\
  "$HOME/apps/lanes/_managed/fleet/agent-sync-runtime/scripts/platform-letter" \\
  "$HOME/.local/share/fleet-cli/current/scripts/platform-letter" \\
  "$HOME/Code/AI-Fleet-Coordinator/scripts/platform-letter" \\
  /mnt/fleet-extra/Code/AI-Fleet-Coordinator/scripts/platform-letter; do
  if [ -f "$candidate" ]; then real="$candidate"; break; fi
done
[ -n "$real" ] || exit 0
exec "$real" "$@"
"""

INSTALL = PL.JsonHookInstaller(
    path=".zcode/cli/config.json",
    events=HOOK_TIMEOUTS,
    locate=_locate_events,
    build=zcode_entry,
)

WRAPPER_NOTE = (
    "the hook command is %s, a copy of the checkout's scripts/zcode-letter-hook/platform-letter"
    " that sources the host's shared registry env file and then runs the checkout's real"
    " platform-letter"
)


def _sync_wrapper(env: Mapping[str, str], stdout: TextIO) -> bool:
    """Copy this checkout's wrapper to the stable path (0755), creating the directory.  True
    when the stable copy exists and is current afterwards."""
    import tempfile

    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "zcode-letter-hook",
        "platform-letter",
    )
    target = wrapper_path(env)
    try:
        body = open(source, "rb").read()
    except OSError as exc:
        stdout.write(
            json.dumps({"error": "the wrapper copy in this checkout is unreadable (%s)" % type(exc).__name__})
            + "\n"
        )
        return False
    if os.path.isfile(target):
        try:
            if os.path.realpath(target) == os.path.realpath(source) or open(target, "rb").read() == body:
                return True
        except OSError:
            pass
    try:
        os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".letter-", suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o755)
        os.chmod(os.path.dirname(target), 0o755)
        os.replace(tmp, target)
        return True
    except OSError as exc:
        stdout.write(
            json.dumps({"error": "the wrapper could not be installed (%s)" % type(exc).__name__}) + "\n"
        )
        return False


def install(opts: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """`platform-letter install --platform zcode`:  the wrapper copy first, then the shared JSON
    installer.  `--remove` leaves the wrapper copy in place (it is a file the user keeps and
    may source from elsewhere) and takes out only this adapter's entries."""
    import io

    if opts.remove:
        return PL.install_json_hooks(INSTALL, opts, env, stdout)
    buf = io.StringIO()
    if not _sync_wrapper(env, buf):
        stdout.write(buf.getvalue())
        return 2
    code = PL.install_json_hooks(INSTALL, opts, env, buf)
    answer = PL._last_json(buf.getvalue())
    if answer is not None and "error" not in answer:
        answer["wrapper"] = wrapper_path(env)
        stdout.write(json.dumps(answer, sort_keys=True) + "\n")
        return code
    stdout.write(buf.getvalue())
    return code


def sweep_main(argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    """`sweep [--apply] [--db FILE]`:  close the registrations of sessions the store has
    archived or deleted (or dropped), which fire no hook of their own.  Without --apply it
    only reports."""
    import argparse

    parser = argparse.ArgumentParser(prog="platform-letter-zcode sweep")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default="")
    parser.add_argument("--location", default="server", choices=PL.LOCATIONS)
    args = parser.parse_args(argv)
    path = args.db or store_file(env)
    if not os.path.isfile(path):
        stdout.write(json.dumps({"error": "no ZCode session store at %s" % path}) + "\n")
        return 2
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        rows = conn.execute(
            "SELECT id FROM session WHERE time_archived IS NOT NULL OR time_deleted IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        stdout.write(json.dumps({"error": "the store could not be read (%s)" % type(exc).__name__}) + "\n")
        return 2
    gone = [r[0] for r in rows if isinstance(r[0], str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", r[0])]
    closed = []
    failed = []
    import io

    for sid in gone:
        if not args.apply:
            closed.append({"session_id": sid, "would_close": True})
            continue
        buf = io.StringIO()
        answer = PL.registry_main(
            ["close", "--platform", PLATFORM, "--location", args.location, "--session-id", sid],
            env=env,
            stdout=buf,
        )
        stdout.write(buf.getvalue())
        # registry_main writes its own JSON line; a nonzero code means the close did not land.
        if answer != 0:
            failed.append(sid)
        else:
            closed.append({"session_id": sid, "closed": True})
    stdout.write(
        json.dumps({"platform": PLATFORM, "found": len(gone), "closed": closed, "failed": failed})
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
    out.write("usage: python3 -m agent_sync.platform_letter_zcode sweep [--apply] [--db FILE]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
