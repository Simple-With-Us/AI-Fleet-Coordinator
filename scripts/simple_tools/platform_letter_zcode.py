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

from . import platform_letter as PL

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
    """One read-only connection to the session store for one lookup, closed by the context
    manager:  a hook run opens a handful of short-lived readers and leaves no descriptor
    behind.  Errors are the caller\'s empty answers, never exceptions a chat could see."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.path = store_file(env)
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> _Store:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _connect(self) -> bool:
        if self._conn is not None:
            return True
        if not os.path.isfile(self.path):
            return False
        try:
            self._conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=5)
            return True
        except sqlite3.Error:
            return False

    def row(self, sid: str) -> dict[str, Any] | None:
        if not sid or not self._connect():
            return None
        try:
            cur = self._conn.execute(
                "SELECT id, parent_id, title, title_source, task_type, directory,"
                " time_archived, time_deleted FROM session WHERE id = ?",
                (sid,),
            )
            fetched = cur.fetchone()
        except sqlite3.Error:
            return None
        if fetched is None:
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
        return dict(zip(keys, fetched))

    def subagent_ids(self, sids: list[str]) -> set[str]:
        """The ids among `sids` the store calls sub-agent children, in one query."""
        if not sids or not self._connect():
            return set()
        placeholders = ",".join("?" * len(sids))
        try:
            rows = self._conn.execute(
                "SELECT id FROM session WHERE task_type = 'subagent_child'"
                " AND id IN (%s)" % placeholders,
                sids,
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {str(r[0]) for r in rows if r and r[0]}


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
    with _Store(env) as store:
        row = store.row(_sid_of(payload))
    if row and isinstance(row.get("parent_id"), str):
        return row["parent_id"]
    return ""


def zcode_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """chat, subagent or headless, from the store's `task_type`; an unknown session is a chat."""
    with _Store(env) as store:
        row = store.row(_sid_of(payload))
    if row:
        return _TASK_KINDS.get(str(row.get("task_type") or ""), "headless")
    return "chat"


def zcode_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    with _Store(env) as store:
        row = store.row(_sid_of(payload))
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
    """Put the lettered title on the chat:  one UPDATE in the store, `title_source` `custom`.

    `custom`, not `generated`:  the app model-retitles a thread whose title it considers its
    own (`generated`) and wipes the letter prefix off it (seen live — a thread titled
    "DD: work on ..." came back as plain "Merge and Close Draft PRs"), while a custom title
    is a user rename the app leaves alone.  The letter hook's own rule keeps working on top:
    a custom title that already carries this letter is updated in place (new words, same
    letter).  A custom title without the letter is the hook's own earlier write when it is
    exactly the note's last applied title (label and words) — a later letter, such as a
    reissue after a recycle, replaces it — and anything else is a name a person chose, so
    the chat keeps the user's words and the registry keeps the letter.

    The write stays inside the hook's own budget while still beating a busy store:  two
    short attempts (4 s each, a quarter second apart), not one long one — a busy timeout
    alone was seen losing title writes under the live app (titles landed hours late, on a
    much later event's retry)."""
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
    with _Store(env) as store:
        row = store.row(sid)
    if row and row.get("title_source") == "custom":
        current = str(row.get("title") or "")
        if current and not current.startswith(str(request.label or "") + ":"):
            if not _hook_owned_title(env, sid, current):
                return True  # a person's rename:  the words stay
    for attempt in range(2):
        try:
            conn = sqlite3.connect(path, timeout=4)
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE session SET title = ?, title_source = 'custom',"
                        " time_title_updated = ? WHERE id = ?",
                        (title, int(time.time()), sid),
                    )
                    return cur.rowcount == 1
            finally:
                conn.close()
        except sqlite3.Error:
            if attempt == 0:
                time.sleep(0.25)
                continue
    return False


def _hook_owned_title(env: Mapping[str, str], sid: str, current: str) -> bool:
    """True when `current` is exactly the title the hook last applied to this chat (the
    note's label and words), so it is the hook's own `custom` write and a later letter may
    replace it.  A missing or unreadable note answers False:  without proof the name is
    treated as a person's, which is the safe side."""
    try:
        with open(PL.note_path(env, SEAT, sid), encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return False
    label = str(note.get("label") or "")
    words = str(note.get("title") or "")
    return bool(label and words and current == "%s: %s" % (label, words))


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
    through unchanged.  The wrapper path is derived from the command's own prefix, so the
    home the install ran under decides it, never the home of whichever process builds the
    entry later:  the wrapper sits in a `zc-letter` directory BESIDE the `platform-letter`
    the install command names (`<home>/.local/bin/zc-letter/platform-letter`) — one dirname
    up from it, not two."""
    parts = shlex.split(command)
    bin_dir = os.path.dirname(parts[0])  # <home>/.local/bin
    # the command is a shell-word list, so the rebuilt path is quoted again (a home with a space in it)
    parts[0] = shlex.quote(os.path.join(bin_dir, "zc-letter", "platform-letter"))
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
# The integration tree is host configuration:  the host names its volume root in
# FLEET_SERVER_VOLUME (the lane tools read the same variable), so this file carries no path.
vol_code="${FLEET_SERVER_VOLUME:-}/Code/AI-Fleet-Coordinator/scripts/platform-letter"
real=
for candidate in \\
  "$HOME/apps/lanes/_managed/fleet/simple-tools-runtime/scripts/platform-letter" \\
  "$HOME/.local/share/fleet-cli/current/scripts/platform-letter" \\
  "$HOME/Code/AI-Fleet-Coordinator/scripts/platform-letter" \\
  "$vol_code"; do
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
            json.dumps(
                {
                    "error": "the wrapper copy in this checkout is unreadable (%s)"
                    % type(exc).__name__
                }
            )
            + "\n"
        )
        return False
    if os.path.isfile(target):
        try:
            if (
                os.path.realpath(target) == os.path.realpath(source)
                or open(target, "rb").read() == body
            ):
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
            json.dumps({"error": "the wrapper could not be installed (%s)" % type(exc).__name__})
            + "\n"
        )
        return False


def install(opts: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """`platform-letter install --platform zcode`:  the wrapper copy first, then the shared JSON
    installer.  `--check` writes nothing anywhere:  it reports whether the wrapper copy and the
    hook entries are current, the way the shared installer's own check does.  `--remove` leaves
    the wrapper copy in place (it is a file the user keeps and may source from elsewhere) and
    takes out only this adapter's entries."""
    import io

    if opts.remove:
        return PL.install_json_hooks(INSTALL, opts, env, stdout)
    buf = io.StringIO()
    if opts.check:
        target = wrapper_path(env)
        source = os.path.join(
            os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
            "zcode-letter-hook",
            "platform-letter",
        )
        current = False
        try:
            current = (
                os.path.isfile(target) and open(target, "rb").read() == open(source, "rb").read()
            )
        except OSError:
            current = False
        code = PL.install_json_hooks(INSTALL, opts, env, buf)
        answer = PL._last_json(buf.getvalue())
        if answer is not None and "error" not in answer:
            answer["wrapper"] = target
            answer["wrapper_current"] = current
            stdout.write(json.dumps(answer, sort_keys=True) + "\n")
            return code if current else 1
        stdout.write(buf.getvalue())
        return code
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
    shape = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        rows = conn.execute(
            "SELECT id FROM session WHERE time_archived IS NOT NULL OR time_deleted IS NOT NULL"
        ).fetchall()
        every = conn.execute("SELECT id FROM session").fetchall()
        conn.close()
    except sqlite3.Error as exc:
        stdout.write(
            json.dumps({"error": "the store could not be read (%s)" % type(exc).__name__}) + "\n"
        )
        return 2
    gone = [r[0] for r in rows if isinstance(r[0], str) and shape.fullmatch(r[0])]
    # Registrations whose session row the store has dropped outright (a hard delete) fire no
    # flag to sweep on:  the live registrations answer what the store no longer knows about.
    # `known` is EVERY id the store holds, filtered by the same shape as `gone` — a known set
    # of only the archived/deleted ids would mark every active session dropped (Kody follow-up
    # on the sweep thread).  The history read asks for one high page because the API pages by
    # limit only:  more live registrations than the page means missed closes, never false ones.
    from . import registry_api as RA

    if RA.base_url(env):
        answer = RA.call(env, "history", {"platform": PLATFORM, "limit": 1000})
        history = answer.get("items") if isinstance(answer, dict) else None
        if isinstance(history, list):
            known = {r[0] for r in every if isinstance(r[0], str) and shape.fullmatch(r[0])}
            for row in history:
                sid = str((row or {}).get("native_session_id") or "")
                if (
                    shape.fullmatch(sid)
                    and not (row or {}).get("closed_at")
                    and str((row or {}).get("state") or "") != "closed"
                    and sid not in known
                ):
                    gone.append(sid)
    closed = []
    failed = []
    absent = []
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
        # A session the Operator never registered (or has already closed) is swept by this
        # very run:  the not-registered answer is the happy no-op, not a failure, so a sweep
        # re-run is idempotent.  Any other nonzero close is a failure.
        line = PL._last_json(buf.getvalue())
        text = str((line or {}).get("error") or "")
        if (
            answer != 0
            and PL._NOT_REGISTERED_MARK not in text
            and "no session with this identity" not in text
        ):
            stdout.write(buf.getvalue())
            failed.append(sid)
        elif answer != 0:
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
    out.write("usage: python3 -m simple_tools.platform_letter_zcode sweep [--apply] [--db FILE]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
