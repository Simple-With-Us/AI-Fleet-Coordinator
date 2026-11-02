#!/usr/bin/env python3
"""mm_session_letter.py — the MM seat's session letter, and the thread title it belongs to.

One MiniMax Code conversation is one session, and the Operator issues it a letter.  The
letter is the tagged address Jay scans ("@**MiniMax** AA"), so three things have to agree
and this file is where they agree:

  1. the thread is named in English and starts with the letter ("AA: Memory sync");
  2. a Zulip post or a board item states the letter early, so a reply knows where it
     is going;
  3. the registry carries the same title the thread carries, found by the session id.

The store is MiniMax Code's own session table; the letter lives in the Operator
registry, which is one file for the fleet.  This host reads that file locally when the
deployment points at the shared store — there is no SSH fallback with a private alias.

A thread and its child both hold a letter, and they are placed the way every other platform
places them (owner ruling, Nov 1 2026):  a main chat takes the next doubled pair in order
(AA, BB ... ZZ) and a child takes the first free pair after its parent's in the same row
(parent CC: CD, CE;  parent XX: XY, XZ, XA, XB).  A session that is neither a conversation nor
a child of one (a cron run) has no thread to name.

Usage:
  mm_session_letter.py hook-session-start      # stdin: the hook JSON
  mm_session_letter.py hook-pre-tool-use       # stdin: the hook JSON
  mm_session_letter.py hook-stop               # stdin: the hook JSON
  mm_session_letter.py sweep [--apply]         # name the sessions this host already has
  mm_session_letter.py letter --session-id ID  # the letter for one session
  mm_session_letter.py rename --session-id ID --title "AA: Topic"
  mm_session_letter.py migrate-pairs          # move the live sessions into the pair space
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterator

from agent_sync import secretscan
from agent_sync.operator_registry import (
    letter_space_of,
    pairs_after,
    registry_db_path,
)

SEAT = "MM"
PLATFORM = "minimax"
PROFILE_SCOPE = "default"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# The letter space every MiniMax session holds its letter in (owner ruling, Nov 1 2026).
# A MiniMax thread reads like a Claude one ("AA: Topic"), so it takes a pair:  a main chat
# takes the next doubled pair in order (AA, BB ... ZZ) and a child takes the first free pair
# after its parent's in the same row (parent CC: CD, CE;  parent XX: XY, XZ, XA, XB).
LETTER_SPACE = "double"
LETTER_PICK = "primary"
TITLE_MAX = 120
HOOK_INPUT_MAX = 1_048_576
HOOK_JSON_MAX_DEPTH = 32
HOOK_JSON_MAX_NODES = 10_000
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# The Mac's checkout of AI-Fleet-Coordinator, on the Operator host and here.
REPO_REL = "AI-Fleet-Coordinator/scripts"


class RegistryError(RuntimeError):
    """The Operator registry call failed; callers must not treat it as success.

    The code and the outcome travel with the error, so a caller can tell a registry
    that was out of reach from one that refused, and a write whose outcome is unknown
    from one that never happened.
    """

    def __init__(self, message: str, code: str = "", outcome: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.outcome = outcome


# The statuses a shell expects:  124 for a command that hung, 127 for a command that
# is not there.  Automation reads these, so a registry that is out of reach has to be
# distinguishable from one that refused.
REGISTRY_EXIT_CODES = {"timeout": 124, "missing_tool": 127}

# A registry call that timed out, or answered something unreadable, may have applied
# the write before the answer was lost.  Those outcomes are unknown rather than
# failed, so a caller checks the state instead of assuming the write never happened.
UNKNOWN_OUTCOME_CODES = ("timeout", "io_error", "nonzero", "empty", "bad_json", "bad_shape")

# The bound on a registry error field:  long enough for any message the CLI writes,
# short enough that a runaway answer cannot flood a hook's stderr.
REGISTRY_FIELD_MAX = 2000


# ---- where things live ------------------------------------------------------------


def data_dir() -> str:
    return os.environ.get("MINIMAX_DATA_DIR") or os.path.expanduser("~/.minimax")


def store_db() -> str:
    return os.path.join(data_dir(), "v2", "sqlite", "runtime-state.sqlite")


def is_mac() -> bool:
    return os.path.isdir("/Users/jay") or os.uname().nodename.startswith("jay-mac")


def location() -> str:
    """The fleet location, never an OS hostname."""
    return "mac" if is_mac() else "server"


def registry_db() -> str:
    """The local registry file, or "" when this host keeps no copy."""
    override = os.environ.get("AGENT_SYNC_OPERATOR_REGISTRY")
    if override:
        path = os.path.expanduser(override)
        return path if os.path.isfile(path) else ""
    root = os.environ.get("AGENT_SYNC_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".agent-sync"
    )
    path = registry_db_path(root, os.environ)
    return path if os.path.isfile(path) else ""


def repo_scripts_dir() -> str:
    """This host's checkout of the fleet scripts, where the registry CLI lives.

    The checkout is host configuration, so the host names it and this file never
    writes a path:  the code root comes from the environment, the fleet's code root
    sits beside its apps root, the Mac has its own layout, and a checkout running
    this script is its own parent.
    """
    candidates = []
    code_root = os.environ.get("FLEET_CODE_ROOT", "").strip()
    if code_root:
        candidates.append(os.path.join(code_root, "AI-Fleet-Coordinator", "scripts"))
    apps_root = os.environ.get("FLEET_APPS_ROOT", "").strip()
    if apps_root:
        candidates.append(
            os.path.join(os.path.dirname(apps_root), "Code", "AI-Fleet-Coordinator", "scripts")
        )
    candidates.append(
        os.path.join(os.path.expanduser("~"), "Code", "AI-Fleet-Coordinator", "scripts")
    )
    # A checkout running this script:  the scripts sit beside it.
    candidates.append(os.path.dirname(os.path.realpath(__file__)))
    for candidate in candidates:
        if os.path.isdir(candidate) and os.path.isfile(
            os.path.join(candidate, "agent_sync", "operator_registry.py")
        ):
            return candidate
    return ""


def _scan_title(text: str) -> None:
    """Refuse a title that carries a credential, with a stable code and no value.

    The code is the same one the Operator's registry answers with, so a caller
    reading either side sees one name for the refusal.  Neither the secret nor any
    part of it is ever put in the message.
    """
    if secretscan.scan(text or ""):
        raise ValueError("secret_detected")


# ---- the registry, locally only ---------------------------------------------------


def _registry_identity_args() -> list[str]:
    return ["--location=" + location(), "--scope=" + PROFILE_SCOPE]


def _json_within_limits(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > HOOK_JSON_MAX_NODES or depth > HOOK_JSON_MAX_DEPTH:
            return False
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return True


def _raw_json_too_deep(raw: str) -> bool:
    """True when the raw JSON text nests brackets deeper than the hook limit.

    A cheap scan of the text before json.loads:  it counts the brackets that sit
    outside a string, so a payload nested far past the limit is refused without the
    parser ever recursing through it.  It only refuses what _json_within_limits would
    refuse anyway, since a value inside HOOK_JSON_MAX_DEPTH brackets is never deeper.
    """
    if raw.count("{") + raw.count("[") <= HOOK_JSON_MAX_DEPTH:
        return False  # too few brackets to nest past the limit:  nothing to walk
    depth = 0
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > HOOK_JSON_MAX_DEPTH:
                return True
        elif ch in "}]" and depth:
            depth -= 1
    return False


def _interrupt_on_signal(signum, frame):
    """Turn an interruption into an exception, so the child is stopped on the way out."""
    raise KeyboardInterrupt("interrupted by signal %s" % signum)


def _install_signal_handlers() -> dict:
    """Catch the signals that would end this process without unwinding.

    SIGINT already raises KeyboardInterrupt on its own;  SIGTERM does not, and the
    default action for it kills the process outright, leaving the registry child
    running on its own.  A handler that cannot be installed, which is the case off
    the main thread, is simply left out of the returned map.
    """
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _interrupt_on_signal)
        except (ValueError, OSError):
            continue
    return previous


def _restore_signal_handlers(previous: dict) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            continue


def _kill_process_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child and everything it started."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop the child and its group:  a polite stop first, then a hard one."""
    _kill_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc, signal.SIGKILL)
        proc.wait()


def registry_call(args: list[str], *, timeout: int = 25) -> dict:
    """Run the Operator's registry CLI and return its JSON answer."""
    local_db = registry_db()
    scripts = repo_scripts_dir()
    if not local_db or not scripts:
        return {
            "error": "the operator registry is not available on this host",
            "code": "no_registry",
        }
    cmd = [sys.executable, "-m", "agent_sync.operator_registry"] + args + ["--db", local_db]
    previous = _install_signal_handlers()
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=scripts,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            # A missing executable is errno 2;  it is a different failure from a spawn
            # that failed for any other reason, and it has its own exit status.
            code = "missing_tool" if getattr(exc, "errno", None) == 2 else "spawn_failed"
            return {"error": str(exc)[:400], "code": code}
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc, signal.SIGKILL)
            proc.kill()
            proc.wait()
            return {
                "error": "the registry did not answer in time",
                "code": "timeout",
                "outcome": "outcome_unknown",
            }
        except OSError as exc:
            # The child started, so it may have committed its write before the pipe
            # failed:  this is an unknown outcome, not a call that never happened.
            _kill_process_group(proc, signal.SIGKILL)
            proc.kill()
            proc.wait()
            return {"error": str(exc)[:400], "code": "io_error", "outcome": "outcome_unknown"}
    except BaseException:
        # This process was interrupted.  The child sits in its own process group, so
        # it would outlive us unless it is stopped here.
        if proc is not None:
            _terminate_process_group(proc)
        raise
    finally:
        _restore_signal_handlers(previous)
    out = (stdout or "").strip()
    if not out:
        err = (stderr or "no answer").strip()[:400]
        code = "nonzero" if proc.returncode else "empty"
        return {"error": err, "code": code, "outcome": "outcome_unknown"}
    try:
        answer = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        return {
            "error": "unreadable answer: %s" % out[:200],
            "code": "bad_json",
            "outcome": "outcome_unknown",
        }
    if not isinstance(answer, dict):
        return {
            "error": "the registry answer was not an object",
            "code": "bad_shape",
            "outcome": "outcome_unknown",
        }
    if proc.returncode and "error" not in answer:
        answer = {"error": answer.get("error") or "registry exited %s" % proc.returncode}
    return answer


def _require_registry(answer: dict, *, needs_label: bool = False) -> dict:
    """A registry answer that is not a success is a failure, never a result.

    The failure carries the registry's own code and outcome, so the caller can map
    it to an exit status and can tell a write whose outcome is unknown from one that
    never happened.
    """
    if not isinstance(answer, dict):
        raise RegistryError("the registry answer was not an object", "bad_shape", "outcome_unknown")
    # Every field is checked for its type before anything reads it:  a field of the
    # wrong type is a malformed answer, never coerced into one that looks valid.
    for name in ("error", "code", "outcome", "label"):
        if name in answer and answer[name] is not None:
            value = answer[name]
            if not isinstance(value, str) or len(value) > REGISTRY_FIELD_MAX:
                raise RegistryError(
                    f"the registry answer has a malformed {name} field",
                    "bad_shape",
                    "outcome_unknown",
                )
    if "error" in answer and answer["error"] is not None:
        # An error key is a failure even when its text is empty:  a falsey error is
        # never read as a success.
        raise RegistryError(
            answer["error"] or "the registry reported an error with no message",
            answer.get("code") or "",
            answer.get("outcome") or "",
        )
    if needs_label:
        label = answer.get("label")
        if not isinstance(label, str) or not label.strip():
            raise RegistryError("the registry answer carried no letter", "bad_shape")
    return answer


def _registry_exit_code(exc: RegistryError) -> int:
    """The status this registry failure gets on the command line."""
    return REGISTRY_EXIT_CODES.get(getattr(exc, "code", "") or "", 2)


def _report_registry_fault(exc: Exception) -> None:
    """Say on stderr why a hook went on without a letter.

    A hook that exits non-zero blocks the tool call or the session, and a registry
    that is missing must not stop the seat working, so the hooks keep exit 0.  The
    distinct code goes to stderr instead:  an operator reading it can tell a missing
    executable (127) and a hang (124) from an outage.  A registry that is not on this
    host at all is a configuration state, not a fault of this call, so it stays quiet.
    """
    code = str(getattr(exc, "code", "") or "")
    if code == "no_registry":
        return
    shown = code or "refused"
    if code in REGISTRY_EXIT_CODES:
        shown += ", status %d" % REGISTRY_EXIT_CODES[code]
    sys.stderr.write("Operator registry fault (%s): %s\n" % (shown, exc))


def register(session_id: str, title: str = "", parent_session_id: str = "") -> dict:
    """Ask the Operator for this session's letter.  Idempotent: the same session id
    always answers with the same letter.

    The pair space and the main-chat order are asked for by name, and the parent is named
    when there is one, so the Operator places a child in its parent's row.
    """
    args = [
        "register",
        "--platform=" + PLATFORM,
        *_registry_identity_args(),
        "--session-id=" + session_id,
        "--seat=" + SEAT,
        "--source=the MM seat's session-letter hook",
        "--letter-space=" + LETTER_SPACE,
        "--letter-pick=" + LETTER_PICK,
    ]
    if parent_session_id:
        args += ["--parent-session-id=" + parent_session_id]
    if title:
        _scan_title(title)
        args += ["--title=" + title]
    return _require_registry(registry_call(args))


def relabel(session_id: str, parent_session_id: str = "", force: bool = False) -> dict:
    """Move a live session's letter into the pair space, in place.

    A session id always answers with the letter its registry record holds, so a session that
    was lettered under the old single-letter rule cannot be moved by registering it again:
    the registry's own relabel path is what moves it, and it keeps the old letter in the
    history.
    """
    args = [
        "relabel",
        "--platform=" + PLATFORM,
        *_registry_identity_args(),
        "--session-id=" + session_id,
        "--seat=" + SEAT,
        "--source=the MM seat's session-letter hook (the pair migration)",
        "--letter-space=" + LETTER_SPACE,
        "--letter-pick=" + LETTER_PICK,
    ]
    if parent_session_id:
        args += ["--parent-session-id=" + parent_session_id]
    if force:
        args += ["--force"]
    return _require_registry(registry_call(args))


def touch_session(session_id: str) -> dict:
    return _require_registry(
        registry_call(
            [
                "touch",
                "--platform=" + PLATFORM,
                *_registry_identity_args(),
                "--session-id=" + session_id,
            ]
        )
    )


def update_title(session_id: str, title: str, directories: list[str] | None = None) -> dict:
    """Carry the thread topic into the registry (topic only — not the letter prefix)."""
    _scan_title(title)
    letter = cached_letter(session_id, directories)
    topic = registry_topic_from_display(letter, title)
    return _require_registry(
        registry_call(
            [
                "update",
                "--platform=" + PLATFORM,
                *_registry_identity_args(),
                "--session-id=" + session_id,
                # The single --flag=VALUE form:  a topic that starts with "-" is still
                # the title, never an option the child parses.
                "--title=" + topic,
            ]
        )
    )


def registry_topic_from_display(letter: str, title: str) -> str:
    text = (title or "").strip()
    if letter and text.startswith(letter + ":"):
        return text[len(letter) + 1 :].lstrip()
    return text


# ---- the MiniMax Code session store ----------------------------------------------


def sessions(only_conversations: bool = False) -> list[dict]:
    """The sessions this host knows about."""
    db = store_db()
    if not os.path.isfile(db):
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, title, session_kind, parent_session_id, agent_name,"
            " runtime, workspace_dir, archived, visibility, created_at_ms"
            " FROM local_runtime_sessions"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        title = str(item.get("title") or "")
        if title:
            try:
                _scan_title(title)
            except ValueError:
                item["title"] = ""
        if only_conversations and (
            item.get("parent_session_id") or item.get("session_kind") != "conversation"
        ):
            continue
        out.append(item)
    return out


def session_row(session_id: str) -> dict | None:
    for item in sessions():
        if item["session_id"] == session_id:
            return item
    return None


def set_title(session_id: str, title: str) -> bool:
    """Write the thread's title into the MiniMax Code store."""
    _scan_title(title)
    db = store_db()
    if not os.path.isfile(db):
        return False
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute(
                "UPDATE local_runtime_sessions SET title = ? WHERE session_id = ?",
                (title, session_id),
            )
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def apply_title(session_id: str, letter: str, topic: str) -> str:
    """Put "<letter>: <topic>" on the thread and carry the topic to the registry.

    The title in the seat's form is written in the session store and the topic
    (never the prefix) goes to the Operator registry.  A thread that already
    carries the letter is left as it is, so a call is safe to repeat.  Returns
    the title now in place, or "" when there is no topic yet or nothing could
    be written:  a hook never fails a session over a title.
    """
    topic = (topic or "").strip()
    if not letter or not topic:
        return ""
    title = wanted_title(letter, topic)
    if topic != title:
        try:
            if not set_title(session_id, title):
                return ""
        except ValueError:
            # The title carried a credential:  the refusal names the problem and
            # never the value.
            return ""
    try:
        update_title(session_id, title)
    except RegistryError:
        # The thread carries the title in the store;  the registry copy is a
        # second copy, and the sweep carries it later.
        pass
    except ValueError:
        # The title carried a credential.  Nothing is printed back, because
        # the caller prints what this returns.
        return ""
    return title


# ---- the title form ---------------------------------------------------------------


def is_english(text: str) -> bool:
    """True when the text carries no non-ASCII letter.  A Chinese title fails here."""
    for ch in text:
        if unicodedata.category(ch).startswith("L") and ord(ch) > 127:
            return False
    return True


def placeholder_title(letter: str) -> str:
    """The title a thread carries while it has no topic of its own yet."""
    return "%s: Session %s" % (letter, letter)


# The letter shapes a thread title carries:  a doubled pair (AA, the shape this platform
# gives every thread now) and a single capital (the shape it gave them before).  Two
# capitals that are not a pair are not a letter:  a topic that starts with an acronym
# ("AI: the model") keeps it.
DOUBLED_PAIR_RE = re.compile(r"^([A-Z]{2}):[ ]*")
SINGLE_LETTER_RE = re.compile(r"^[A-Z]:[ ]*")


def topic_without_letter(title: str, letters: tuple[str, ...] = ()) -> str:
    """The topic in a title, without the letter prefix a thread already carries.

    A thread that is re-lettered keeps its topic, so the old prefix is dropped before the
    new one is put in front:  "A: Fix the sweep" under the letter "AA" is "AA: Fix the
    sweep", never "AA: A: Fix the sweep".

    Only the letters the thread actually holds are dropped, and the shapes a thread title
    carries:  a doubled pair, and the single capital this platform used before it moved to
    pairs.  Two capitals that are not a pair are an acronym, so a topic that starts with one
    keeps it.
    """
    text = (title or "").strip()
    for letter in letters:
        prefix = letter + ":"
        if letter and text.startswith(prefix):
            return text[len(prefix) :].strip()
    match = DOUBLED_PAIR_RE.match(text)
    if match and match.group(1)[0] == match.group(1)[1]:
        return text[match.end() :].strip()
    match = SINGLE_LETTER_RE.match(text)
    if match:
        return text[match.end() :].strip()
    return text


def wanted_title(letter: str, topic: str, previous_letters: tuple[str, ...] = ()) -> str:
    """The title form:  the letter, then the topic, in Title Case English."""
    topic = topic_without_letter(topic, tuple(previous_letters) + (letter,))
    topic = " ".join(topic.split())
    if not topic or not is_english(topic):
        topic = "Session " + (letter or "?")
    return "%s: %s" % (letter, topic[:TITLE_MAX])


def title_ok(title: str, letter: str) -> tuple[bool, str]:
    """Is the title in the form the seat uses?  Returns (ok, why)."""
    if not title:
        return False, "the thread has no title"
    if not title.startswith(letter + ":"):
        return False, "it does not start with the session letter %r" % (letter + ":")
    if not is_english(title):
        return False, "it is not in English"
    return True, "ok"


def first_line_states_letter(text: str, letter: str) -> bool:
    head = (text or "").strip()
    if not head:
        return False
    first = head.split("\n", 1)[0].strip()
    return bool(re.match(r"^%s:" % re.escape(letter), first))


# ---- the letter cache -------------------------------------------------------------


def hooks_data_root() -> str:
    """Where the runtime keeps its hook scopes.

    The runtime (MiniMax Code v2) runs a hook with its scope's own directory in
    MINIMAX_HOOKS_DATA:  the user scope is <data>/v2/hook-data/user and a project
    scope is <data>/v2/hook-data/projects/<hash>.  The pre-v2 layout kept the same
    directory under <data>/hook-state, which is now the last place a letter is read
    from, never the first.
    """
    return os.path.join(data_dir(), "v2", "hook-data")


def _refuse_symlink(path: str) -> None:
    """A cache path that is a symlink is refused, never followed.

    The refusal is a RegistryError with its own code, so every hook and the sweep,
    which already handle RegistryError, refuse it cleanly instead of failing with a
    traceback.  It applies to every scope, not only to the primary cache:  a
    planted symlink must not carry a read or a write outside the data directory.
    """
    if os.path.islink(path):
        raise RegistryError("the letter cache path must not be a symlink", "unsafe_cache")


def _refuse_symlink_chain(root: str, *parts: str) -> str:
    """The path from the root down, with every component refused when it is a link.

    A link one level up is followed by the kernel, so a check of the leaf alone is
    not enough:  a planted link at any level of the chain would carry a read or a
    write out of the data directory.  The walk is lexical, the way
    chat_catalog._within does it.
    """
    current = root
    _refuse_symlink(current)
    for part in parts:
        current = os.path.join(current, part)
        _refuse_symlink(current)
    return current


def _project_scope_dirs() -> list[str]:
    """The letter-cache directories of the project scopes, in a stable order.

    The listing is not remembered:  a listing remembered against the scope root's
    mtime went stale inside one tick, so a scope that appeared after the first
    listing was invisible until the tick changed.  The callers that ask once per
    session enumerate once and pass the list on, so the listing costs nothing there.
    """
    projects = _refuse_symlink_chain(data_dir(), "v2", "hook-data", "projects")
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        names = []
    return [
        _refuse_symlink_chain(data_dir(), "v2", "hook-data", "projects", name, "session-letters")
        for name in names
    ]


def _scope_dirs() -> Iterator[str]:
    """The letter-cache directories of the runtime's scopes, the user scope first."""
    yield _refuse_symlink_chain(data_dir(), "v2", "hook-data", "user", "session-letters")
    for directory in _project_scope_dirs():
        yield directory


def _legacy_cache_dir() -> str:
    """The pre-v2 letter cache:  read as a fallback, never written to."""
    return _refuse_symlink_chain(data_dir(), "hook-state", "session-letters")


def letter_cache_dirs() -> list[str]:
    """Every letter-cache directory this host may hold, in the order it is read.

    A hook knows its scope:  the runtime hands it that scope in MINIMAX_HOOKS_DATA,
    and it reads that scope alone.  A shell knows no scope -- the migration, the
    sweep and an operator command all run outside the runtime -- so it reads the user
    scope, then the project scopes, then the pre-v2 directory.  A session lettered
    under one scope is then still found from another, which is what the migration
    needs when it runs from a plain shell.

    The caller that asks once per session enumerates once and passes the list on,
    so the listing is not repeated for every session.
    """
    override = os.environ.get("MINIMAX_HOOKS_DATA")
    if override:
        # The override is the scope the runtime names, and it is refused on the read
        # path too:  letter_cache_dir refuses that base for a write.
        return [_refuse_symlink_chain(override, "session-letters")]
    directories = list(_scope_dirs())
    directories.append(_legacy_cache_dir())
    return directories


def letter_cache_dir() -> str:
    """The letter cache directory.  An unsafe path is a RegistryError with its own
    code, so every hook and the sweep, which already handle RegistryError, refuse it
    cleanly instead of failing with a traceback."""
    override = os.environ.get("MINIMAX_HOOKS_DATA")
    if override:
        base = _refuse_symlink_chain(override)
    else:
        root = hooks_data_root()
        base = _refuse_symlink_chain(data_dir(), "v2", "hook-data", "user")
        legacy = os.path.join(data_dir(), "hook-state")
        if (
            not os.path.isdir(base)
            and not os.path.isdir(os.path.join(root, "projects"))
            and os.path.isdir(legacy)
        ):
            # A pre-v2 runtime:  it has no v2 scope at all, so the old layout is the
            # live one.  A host with a project scope is a v2 host, even when the user
            # scope has not been created yet, and a letter written to the old layout
            # there is one no hook ever reads.
            base = _refuse_symlink_chain(data_dir(), "hook-state")
    path = os.path.join(base, "session-letters")
    for candidate in (base, path):
        _refuse_symlink(candidate)
    os.makedirs(path, mode=0o700, exist_ok=True)
    _refuse_symlink(path)
    return path


def cache_letter(
    session_id: str,
    letter: str,
    topic: str = "",
    space: str = LETTER_SPACE,
    directories: list[str] | None = None,
) -> None:
    if not (
        letter in LETTERS or (len(letter) == 2 and letter[0] in LETTERS and letter[1] in LETTERS)
    ):
        raise ValueError("invalid letter for cache")
    payload = {"letter": letter, "topic": topic, "host": location(), "space": space}
    home = _cache_home(session_id, directories)
    path = os.path.join(home, session_id + ".json")
    fd, tmp = tempfile.mkstemp(dir=home, prefix=".letter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            # The bytes have to be on the disk before the name points at them, and the
            # directory entry has to be on the disk before anyone trusts the name.
            os.fchmod(fh.fileno(), 0o600)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _cached_letter_in(directory: str, session_id: str) -> str:
    path = os.path.join(directory, session_id + ".json")
    _refuse_symlink(directory)
    _refuse_symlink(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    letter = str(data.get("letter") or "")
    if len(letter) == 1 and letter in LETTERS:
        return letter
    if len(letter) == 2 and letter[0] in LETTERS and letter[1] in LETTERS:
        return letter
    return ""


def cached_letter(session_id: str, directories: list[str] | None = None) -> str:
    """The letter this host cached for a session, or "".

    Every scope is read, in the order letter_cache_dirs gives, so a shell finds a
    session lettered under a project scope and a hook finds the letter its own
    scope wrote.  A cache path that is a symlink is refused with the same code the
    primary cache uses, and is never followed.

    The caller that asks once per session passes the list it enumerated, so the
    scopes are listed once for the whole run rather than once per session.
    """
    for directory in directories if directories is not None else letter_cache_dirs():
        letter = _cached_letter_in(directory, session_id)
        if letter:
            return letter
    return ""


def _cache_home(session_id: str, directories: list[str] | None = None) -> str:
    """The directory a session's letter is written to.

    The scope that already holds the file keeps it, so a letter does not split into
    two copies that drift apart;  otherwise the primary directory takes it.
    """
    if os.environ.get("MINIMAX_HOOKS_DATA"):
        return letter_cache_dir()
    for directory in directories if directories is not None else list(_scope_dirs()):
        if os.path.isfile(os.path.join(directory, session_id + ".json")):
            return directory
    return letter_cache_dir()


def parent_for_row(row: dict) -> str:
    """The session that started this one, as the Operator knows it, or "" for a main chat.

    It is the parent's own session id, so the Operator places the child in its parent's row:
    the first free pair after the parent's, in the same row (parent CC: CD, CE;  parent XX:
    XY, XZ, XA, XB).  It reads the row the caller already fetched, so a hook reads the session
    table and scans its titles once per call."""
    parent = str(row.get("parent_session_id") or "").strip()
    return parent if SESSION_ID_RE.fullmatch(parent) else ""


def letter_for(
    session_id: str,
    topic: str = "",
    parent_session_id: str = "",
    *,
    strict_touch: bool = False,
    directories: list[str] | None = None,
) -> str:
    """The letter for a session:  the cache first, the Operator otherwise.

    With strict_touch, a touch that did not land raises RegistryError:  the sweep is
    a batch job whose caller keeps the failed session for a retry, so it has to see
    the failure.  A hook keeps the cached letter instead, below.

    The caller that asks once per session passes the scopes it enumerated, so the
    listing is not repeated for every session.
    """
    letter = cached_letter(session_id, directories)
    if letter:
        try:
            touch_session(session_id)
        except RegistryError as exc:
            if strict_touch:
                raise
            # The letter came from the cache and it is still this session's, so a
            # touch that did not land is not a reason to lose it.  It is a reason to
            # say so:  the sweep reads last_seen_at, and a stale stamp can end a
            # live letter.  A registry that is not on this host at all is a
            # configuration state rather than a failure of this call, so it stays
            # quiet.
            if getattr(exc, "code", "") != "no_registry":
                sys.stderr.write("Could not stamp the session as seen: %s\n" % exc)
        return letter
    answer = _require_registry(register(session_id, topic, parent_session_id), needs_label=True)
    letter = str(answer.get("label") or "")
    if letter:
        cache_letter(session_id, letter, topic, LETTER_SPACE, directories)
    return letter


# ---- the hooks --------------------------------------------------------------------


def read_hook_input() -> dict:
    try:
        raw_bytes = sys.stdin.buffer.read(HOOK_INPUT_MAX + 1)
    except (OSError, ValueError):
        return {}
    if len(raw_bytes) > HOOK_INPUT_MAX:
        return {}
    if not raw_bytes.strip():
        return {}
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    if _raw_json_too_deep(raw):
        # Refused on the text itself, so the parser never recurses through it.
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # The size and the bracket depth are bounded before parsing;  whatever still
        # exhausts the parser's recursion is refused here, the rest by
        # _json_within_limits.
        return {}
    if not isinstance(data, dict) or not _json_within_limits(data):
        return {}
    return data


def hook_session_id(payload: dict) -> str:
    value = payload.get("session_id")
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if SESSION_ID_RE.fullmatch(text) else ""


NO_LETTER = (
    "Session letter: none yet (the Operator could not be reached).  Name this thread "
    "in English; if a letter arrives later, prefix the title with it."
)


def hook_session_start() -> int:
    """Give the session its letter, title the thread, and tell the agent the forms it must use."""
    payload = read_hook_input()
    session_id = hook_session_id(payload)
    if not session_id:
        return 0
    row = session_row(session_id) or {}
    parent = parent_for_row(row)
    try:
        letter = letter_for(session_id, str(row.get("title") or ""), parent)
    except (RegistryError, ValueError) as exc:
        _report_registry_fault(exc)
        print(NO_LETTER)
        return 0
    if not letter:
        print(NO_LETTER)
        return 0
    title = apply_title(session_id, letter, str(row.get("title") or ""))
    if title:
        third = (
            '3. The thread is titled "%s" now.  Rename it with the session-title tool to '
            '"%s: <a short topic in English>" once you know the work, and do it before you finish.\n'
            % (title, letter)
        )
    else:
        # A title that was refused, or none at all:  a title that was never
        # written is never printed back.
        third = (
            "3. The thread has no usable title yet.  Rename it with the session-title tool to "
            '"%s: <a short topic in English>" once you know the work, and do it before you finish.\n'
            % (letter,)
        )
    message = (
        (
            "Session letter: %s.  Three rules for this session:\n"
            '1. Name this thread in English and start the title with the letter: "%s: <topic>".\n'
            "2. State the letter early in every Zulip post and every board item you file — the "
            'first line, for example "%s: what this post is about" — so a reply knows where it '
            "is going.\n" % (letter, letter, letter)
        )
        + third
        + ("The registry carries the same title under the session id %s." % session_id)
    )
    print(message)
    return 0


# The tools whose text lands in front of Jay or another seat.
POSTING_TOOLS = ("zulip_", "linear_", "notes_")


def hook_pre_tool_use() -> int:
    """Block a post that does not state the letter."""
    payload = read_hook_input()
    session_id = hook_session_id(payload)
    if not session_id:
        return 0
    row = session_row(session_id) or {}
    if row.get("session_kind") not in (None, "conversation", "task"):
        return 0
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    try:
        letter = cached_letter(session_id) or letter_for(session_id, "", parent_for_row(row))
    except (RegistryError, ValueError) as exc:
        _report_registry_fault(exc)
        return 0
    if not letter:
        return 0
    text = ""
    if tool in ("bash", "shell"):
        command = str(tool_input.get("command") or "")
        if not re.search(r"simple-chat\s+(post|reply)|simple-board\s+file", command):
            return 0
        text = command
        match = re.search(r"--body-file\s+(\S+)", command)
        if match:
            try:
                with open(match.group(1), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = command
    elif tool.startswith(POSTING_TOOLS):
        for key in ("body", "text", "markdown", "title", "description", "content"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        if not text:
            return 0
    else:
        return 0  # not a posting tool:  the letter does not apply
    if first_line_states_letter(text, letter):
        return 0
    sys.stderr.write(
        "This post does not state the session letter %s.  State it in the first line "
        '(for example "%s: <what this post is about>") and send it again.' % (letter, letter)
    )
    return 2


def hook_stop() -> int:
    """Check the thread title, and carry it into the registry."""
    payload = read_hook_input()
    session_id = hook_session_id(payload)
    if not session_id:
        return 0
    row = session_row(session_id)
    if row is None:
        return 0
    if row.get("session_kind") not in (None, "conversation", "task"):
        return 0
    try:
        letter = cached_letter(session_id) or letter_for(session_id, "", parent_for_row(row))
    except (RegistryError, ValueError) as exc:
        _report_registry_fault(exc)
        return 0
    if not letter:
        return 0
    title = str(row.get("title") or "")
    ok, why = title_ok(title, letter)
    if ok and title.strip() == placeholder_title(letter):
        ok, why = False, "it still carries the placeholder topic"
    if ok:
        try:
            update_title(session_id, title)
        except (RegistryError, ValueError) as exc:
            sys.stderr.write("Could not update the registry: %s\n" % exc)
            return 2
        return 0
    active = bool(payload.get("stop_hook_active"))
    if not active:
        sys.stderr.write(
            "This thread is not titled in the seat's form: %s.  Rename it to "
            '"%s: <topic in English>" with the session-title tool, then finish.' % (why, letter)
        )
        return 2
    fallback = wanted_title(letter, title)
    try:
        renamed = set_title(session_id, fallback)
    except ValueError as exc:
        # The title carries a credential:  the refusal names the problem and never
        # the value.
        sys.stderr.write("Could not rename the thread: %s\n" % exc)
        return 2
    if not renamed:
        sys.stderr.write("Could not rename the thread in the session store.\n")
        return 2
    try:
        update_title(session_id, fallback)
    except (RegistryError, ValueError) as exc:
        sys.stderr.write("Could not update the registry: %s\n" % exc)
        return 2
    sys.stderr.write("The thread was renamed to %r.\n" % fallback)
    return 0


# ---- the sweep --------------------------------------------------------------------

# A wake session's title is the prompt it was woken with, not a thread anybody reads.
# Those sessions get no letter:  a letter is the address of a thread.
PROMPT_PREFIXES = (
    "you are ",
    "reply with",
    "respond with",
    "reply to",
    "say ok",
    "ping",
    "pong",
    "run exactly",
    "run this",
    "run ps",
    "run echo",
    "run the",
    "use the write tool",
    "use the read tool",
    "do not call any tool",
    "apply ",
    "explain ",
    "telemetry!",
    "muster ",
    "watch-",
    "daemon header",
    "<",
    "botfleet-ok",
)


def looks_like_prompt(title: str) -> bool:
    """True when the title is a wake prompt rather than a thread name."""
    text = title.strip().lower()
    if not text or text == "main":
        return True
    return text.startswith(PROMPT_PREFIXES)


def _failure_entry(session_id: str, exc: RegistryError) -> dict:
    """One session's registry failure, with the code and the outcome kept."""
    code = str(getattr(exc, "code", "") or "")
    outcome = str(getattr(exc, "outcome", "") or "")
    if not outcome:
        outcome = "outcome_unknown" if code in UNKNOWN_OUTCOME_CODES else "failed"
    return {"session_id": session_id, "error": str(exc), "code": code, "outcome": outcome}


def _sweep_exit_code(failed: list[dict]) -> int:
    """The sweep's status from the failures it kept.

    A missing registry tool is 127 and a registry that hung is 124, the statuses a
    shell uses, so automation can tell those apart from a refusal.  The missing tool
    wins over the hang:  every later call would fail the same way.  Any other
    failure is 2.
    """
    if not failed:
        return 0
    codes = {e.get("code") for e in failed}
    for code in ("missing_tool", "timeout"):
        if code in codes:
            return REGISTRY_EXIT_CODES[code]
    return 2


def _registry_ended_rows(answer: dict) -> list[dict]:
    """The rows the registry sweep ended, checked for their shape before anything reads them.

    Each row carries native_session_id, label and reason.  A row of the wrong type, or a
    field that is not a bounded string, is a malformed answer, never coerced into one.
    """
    ended = answer.get("ended", [])
    malformed = RegistryError(
        "the registry sweep answer has a malformed ended field", "bad_shape", "outcome_unknown"
    )
    if not isinstance(ended, list):
        raise malformed
    for row in ended:
        if not isinstance(row, dict):
            raise malformed
        for name in ("native_session_id", "label", "reason"):
            value = row.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > REGISTRY_FIELD_MAX:
                raise malformed
    return ended


def _fold_registry_ended(out: list[dict], ended: list[dict]) -> None:
    """Put the registry's own sweep result into the entries.

    A session this host listed gets the registry's word on its entry;  a letter the
    registry ended for a session this host no longer lists gets an entry of its own, so
    the output says everything the sweep ended and not only a count.
    """
    by_session = {e["session_id"]: e for e in out if e.get("session_id")}
    for row in ended:
        native = row.get("native_session_id")
        if not native:
            continue  # a row that names no session has nothing to attach to, and no entry of its own to make
        info = {"label": str(row.get("label") or ""), "reason": str(row.get("reason") or "")}
        entry = by_session.get(native)
        if entry is not None:
            entry["registry_ended"] = info
        else:
            out.append({"session_id": native, "registry_ended": info})


def sweep(apply: bool, live_hours: int = 36, topics_file: str = "") -> int:
    """Name the sessions this host already has:  letter, English title, registry in step."""
    topics: dict[str, str] = {}
    if topics_file:
        try:
            with open(topics_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("the topic file must be a JSON object")
            if not all(
                isinstance(key, str)
                and SESSION_ID_RE.fullmatch(key) is not None
                and isinstance(value, str)
                and len(value) <= TITLE_MAX
                for key, value in loaded.items()
            ):
                raise ValueError(
                    "the topic file must map valid session IDs to bounded string titles"
                )
            topics = dict(loaded)
        except (OSError, ValueError, TypeError) as exc:
            print("cannot read the topic file: %s" % exc, file=sys.stderr)
            return 2
    out = []
    # The scopes are enumerated once for the whole sweep, so the listing is not
    # repeated for every session.
    directories = letter_cache_dirs()
    for row in sessions():
        kind = str(row.get("session_kind") or "")
        if kind not in ("conversation", "task"):
            continue
        session_id = row["session_id"]
        title = str(row.get("title") or "")
        # A title that reads like a wake prompt is not a thread name, but the topic
        # file names one of them on purpose, so the operator's own list rescues it.
        if looks_like_prompt(title) and session_id not in topics:
            continue
        try:
            letter = letter_for(
                session_id,
                title,
                parent_for_row(row),
                strict_touch=True,
                directories=directories,
            )
        except RegistryError as exc:
            out.append(_failure_entry(session_id, exc))
            continue
        except ValueError as exc:
            out.append({"session_id": session_id, "error": str(exc), "outcome": "failed"})
            continue
        if not letter:
            out.append({"session_id": session_id, "skipped": "no letter"})
            continue
        topic = topics.get(session_id) or (title if is_english(title) else "")
        if session_id in topics:
            try:
                _scan_title(topics[session_id])
            except ValueError:
                out.append(
                    {
                        "session_id": session_id,
                        "error": "topic_secret_detected",
                    }
                )
                continue
        if not topic:
            # Not a failure:  a session titled in another language is healthy and
            # simply waits for a topic from the topic file.
            out.append(
                {"session_id": session_id, "letter": letter, "skipped": "needs an English topic"}
            )
            continue
        wanted = wanted_title(letter, topic)
        already_ok = title_ok(title, letter)[0]
        entry = {
            "session_id": session_id,
            "kind": kind,
            "letter": letter,
            "current_title": title,
            "wanted_title": wanted,
            "already_ok": already_ok,
        }
        if apply:
            if not already_ok:
                try:
                    renamed = set_title(session_id, wanted)
                except ValueError as exc:
                    entry["error"] = str(exc)
                    out.append(entry)
                    continue
                if not renamed:
                    entry["error"] = "could not rename in the session store"
                    out.append(entry)
                    continue
                try:
                    update_title(session_id, wanted, directories)
                except RegistryError as exc:
                    entry.update(_failure_entry(session_id, exc))
                    out.append(entry)
                    continue
                except ValueError as exc:
                    entry["error"] = str(exc)
                    out.append(entry)
                    continue
                entry["applied"] = True
            else:
                try:
                    update_title(session_id, title)
                    entry["registry_synced"] = True
                except RegistryError as exc:
                    failure = _failure_entry(session_id, exc)
                    entry["registry_error"] = failure.pop("error")
                    entry.update(failure)
                except ValueError as exc:
                    entry["registry_error"] = str(exc)
        out.append(entry)
    # A sweep that could not handle every session is not a success:  the caller sees
    # a nonzero status and a count on stderr, and the entries say which ones failed.
    failed = [e for e in out if e.get("error") or e.get("registry_error")]
    exit_code = _sweep_exit_code(failed)
    skipped = sum(1 for e in out if e.get("skipped"))
    if failed or skipped:
        print(
            json.dumps(
                {
                    "sessions_failed": len(failed),
                    "sessions_outcome_unknown": sum(
                        1 for e in failed if e.get("outcome") == "outcome_unknown"
                    ),
                    "sessions_skipped": skipped,
                }
            ),
            file=sys.stderr,
        )
    if apply:
        # The registry sweep converges:  it ends, in one write transaction, only the
        # letters that are stale and replaced, so a rerun finds them ended and does
        # nothing more.
        answer = registry_call(
            [
                "sweep",
                "--platform=" + PLATFORM,
                "--seat=" + SEAT,
                "--stale-after-minutes=" + str(max(60, int(live_hours) * 60)),
            ]
        )
        try:
            answer = _require_registry(answer)
            count = answer.get("count", 0)
            if not isinstance(count, int) or isinstance(count, bool):
                raise RegistryError(
                    "the registry sweep answer has a malformed count",
                    "bad_shape",
                    "outcome_unknown",
                )
            ended = _registry_ended_rows(answer)
        except RegistryError as exc:
            error_code = exc.code
            # The registry classifies the failure itself;  the list of codes is the
            # fallback for an answer that came from somewhere else.
            outcome = exc.outcome
            if not outcome:
                outcome = "outcome_unknown" if error_code in UNKNOWN_OUTCOME_CODES else "failed"
            exit_code = REGISTRY_EXIT_CODES.get(error_code, 2)
            print(json.dumps(out, indent=2))
            print(
                json.dumps(
                    {
                        "sweep_error": "operator registry unavailable",
                        "sweep_outcome": outcome,
                        "code": error_code,
                    }
                ),
                file=sys.stderr,
            )
            return exit_code
        _fold_registry_ended(out, ended)
        print(json.dumps({"registry_sweep": count}), file=sys.stderr)
    print(json.dumps(out, indent=2))
    return exit_code


def migrate_pairs() -> int:
    """Move this host's live MiniMax sessions into the pair space.

    A MiniMax thread is a pair (owner ruling, Nov 1 2026), and a session id always answers
    with the letter its registry record already holds, so the sessions that were lettered
    under the old single-letter rule are moved by the registry's own relabel path.

    Only a session that is not where the rule puts it is moved:  a session that already
    holds a pair keeps it, and so does a child that already holds a pair in its parent's
    row.  So the migration is idempotent:  a second run finds nothing to move and changes
    no letter, no generation and no reference.  A main chat goes first, because a child is
    placed after its parent's pair.

    The letters are read from every scope the runtime keeps, so the migration finds the
    live ones when it runs from a plain shell, where the runtime names no scope.
    """
    rows = [
        row for row in sessions() if str(row.get("session_kind") or "") in ("conversation", "task")
    ]
    # The scopes are enumerated once for the whole migration, so the listing is not
    # repeated for every session.
    directories = letter_cache_dirs()
    mains = sorted(
        (row for row in rows if not parent_for_row(row)),
        key=lambda row: int(row.get("created_at_ms") or 0),
    )
    children = sorted(
        (row for row in rows if parent_for_row(row)),
        key=lambda row: int(row.get("created_at_ms") or 0),
    )
    letters: dict[str, str] = {}
    out = []
    for row in mains + children:
        session_id = row["session_id"]
        parent = parent_for_row(row)
        title = str(row.get("title") or "")
        try:
            current = cached_letter(session_id, directories)
        except RegistryError as exc:
            # A cache path that is a symlink is refused here too, so the migration
            # reports the refusal rather than reading outside the data directory.
            out.append(_failure_entry(session_id, exc))
            continue
        if not current:
            # This host never lettered it, so there is no letter here to move.
            continue
        parent_letter = letters.get(parent, "")
        in_a_pair = letter_space_of(current) == LETTER_SPACE
        out_of_place = not in_a_pair
        if not out_of_place and parent_letter and current not in pairs_after(parent_letter):
            # A child that holds a pair but not in its parent's row is placed again.
            out_of_place = True
        if not out_of_place:
            letters[session_id] = current
            out.append({"session_id": session_id, "letter": current, "outcome": "unchanged"})
            continue
        try:
            answer = relabel(session_id, parent, force=in_a_pair)
        except RegistryError as exc:
            code = str(getattr(exc, "code", "") or "")
            if code == "closed_session":
                # The session is over:  its letter ended with it, and there is nothing to move.
                out.append({"session_id": session_id, "letter": current, "outcome": "closed"})
                continue
            if code == "not_registered":
                # The registry has never seen this session, so it holds no letter to move.
                # It is registered here, and the cache is not asked:  the cache still holds
                # the old single letter, and letter_for would hand that back.
                try:
                    answer = _require_registry(
                        register(session_id, title, parent), needs_label=True
                    )
                except (RegistryError, ValueError) as inner:
                    out.append({"session_id": session_id, "error": str(inner), "outcome": "failed"})
                    continue
                letter = str(answer.get("label") or "")
                if not letter:
                    out.append({"session_id": session_id, "skipped": "no letter"})
                    continue
                letters[session_id] = letter
                out.append({"session_id": session_id, "letter": letter, "outcome": "registered"})
                try:
                    cache_letter(session_id, letter, title, LETTER_SPACE, directories)
                except RegistryError as exc:
                    # The registry holds the letter, so the migration is done even
                    # when the cache refuses to write it.
                    out[-1]["cache_error"] = str(exc)
                continue
            out.append(_failure_entry(session_id, exc))
            continue
        letter = str(answer.get("label") or "")
        if not letter:
            out.append({"session_id": session_id, "skipped": "no letter"})
            continue
        letters[session_id] = letter
        try:
            cache_letter(session_id, letter, title, LETTER_SPACE, directories)
        except RegistryError as exc:
            # The registry holds the letter, so the migration is done even when the
            # cache refuses to write it.
            cache_error = str(exc)
        else:
            cache_error = ""
        entry = {
            "session_id": session_id,
            "letter": letter,
            "previous": answer.get("previous", ""),
            "outcome": answer.get("outcome", ""),
            "placed_after": answer.get("placed_after", ""),
        }
        topic = topic_without_letter(title, (current,))
        if topic and is_english(topic):
            wanted = wanted_title(letter, topic)
            try:
                if set_title(session_id, wanted):
                    update_title(session_id, wanted, directories)
                    entry["title"] = wanted
            except (RegistryError, ValueError) as exc:
                entry["error"] = str(exc)
        if cache_error:
            entry["cache_error"] = cache_error
        if cache_error:
            entry["cache_error"] = cache_error
        out.append(entry)
    failed = [e for e in out if e.get("error")]
    print(json.dumps(out, indent=2))
    return _sweep_exit_code(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook-session-start")
    sub.add_parser("hook-pre-tool-use")
    sub.add_parser("hook-stop")
    sw = sub.add_parser("sweep")
    sw.add_argument("--apply", action="store_true")
    sw.add_argument("--live-hours", type=int, default=36)
    sw.add_argument("--topics-file", default="")
    lt = sub.add_parser("letter")
    lt.add_argument("--session-id", required=True)
    lt.add_argument("--title", default="")
    rn = sub.add_parser("rename")
    rn.add_argument("--session-id", required=True)
    rn.add_argument("--title", required=True)
    mp = sub.add_parser(
        "migrate-pairs",
        help="move this host's live MiniMax sessions into the pair space",
    )
    args = parser.parse_args(argv)
    if args.command == "hook-session-start":
        return hook_session_start()
    if args.command == "hook-pre-tool-use":
        return hook_pre_tool_use()
    if args.command == "hook-stop":
        return hook_stop()
    if args.command == "sweep":
        return sweep(args.apply, args.live_hours, args.topics_file)
    if args.command == "letter":
        # The parent is named here too, so a child lettered through this path lands in its
        # parent's row and not in the main-chat order.
        parent = parent_for_row(session_row(args.session_id) or {})
        try:
            print(
                json.dumps(
                    _require_registry(
                        register(args.session_id, args.title, parent), needs_label=True
                    )
                )
            )
        except RegistryError as exc:
            print(json.dumps({"error": str(exc)}))
            return _registry_exit_code(exc)
        except ValueError as exc:
            # The title carried a credential:  the refusal names the problem and
            # never the value.
            print(json.dumps({"error": str(exc)}))
            return 2
        return 0
    if args.command == "migrate-pairs":
        return migrate_pairs()
    if args.command == "rename":
        parent = parent_for_row(session_row(args.session_id) or {})
        try:
            answer = _require_registry(register(args.session_id, "", parent))
        except RegistryError as exc:
            print(json.dumps({"error": str(exc)}))
            return _registry_exit_code(exc)
        letter = str(answer.get("label") or "")
        title = wanted_title(letter, args.title)
        try:
            renamed = set_title(args.session_id, title)
        except ValueError as exc:
            # The title carried a credential:  the refusal names the problem and
            # never the value.
            print(json.dumps({"error": str(exc)}))
            return 2
        if not renamed:
            print(json.dumps({"error": "could not rename in the session store"}))
            return 2
        try:
            update_title(args.session_id, title)
        except RegistryError as exc:
            print(json.dumps({"error": str(exc)}))
            return _registry_exit_code(exc)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps({"letter": letter, "title": title}))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
