#!/usr/bin/env python3
"""mm_session_letter.py — the MM seat's session letter, and the thread title it belongs to.

One MiniMax Code conversation is one session, and the Operator issues it a letter.  The
letter is the tagged address Jay scans ("@**MiniMax** B"), so three things have to agree
and this file is where they agree:

  1. the thread is named in English and starts with the letter ("B: Memory sync");
  2. a Zulip post or a board item states the letter early, so a reply knows where it
     is going;
  3. the registry carries the same title the thread carries, found by the session id.

The store is MiniMax Code's own session table; the letter lives in the Operator
registry, which is one file for the fleet.  This host reads that file locally when the
deployment points at the shared store — there is no SSH fallback with a private alias.

Subthreads do not get a letter:  a session with a parent is part of its parent's
thread, and a session that is not a conversation (a task, a peek, a cron run) has no
thread to name.

Usage:
  mm_session_letter.py hook-session-start      # stdin: the hook JSON
  mm_session_letter.py hook-pre-tool-use       # stdin: the hook JSON
  mm_session_letter.py hook-stop               # stdin: the hook JSON
  mm_session_letter.py sweep [--apply]         # name the sessions this host already has
  mm_session_letter.py letter --session-id ID  # the letter for one session
  mm_session_letter.py rename --session-id ID --title "B: Topic"
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
import time
import unicodedata

from agent_sync import secretscan
from agent_sync.operator_registry import registry_db_path

SEAT = "MM"
PLATFORM = "minimax"
PROFILE_SCOPE = "default"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TITLE_MAX = 120
HOOK_INPUT_MAX = 1_048_576
HOOK_JSON_MAX_DEPTH = 32
HOOK_JSON_MAX_NODES = 10_000
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# The Mac's checkout of AI-Fleet-Coordinator, on the Operator host and here.
REPO_REL = "AI-Fleet-Coordinator/scripts"


class RegistryError(RuntimeError):
    """The Operator registry call failed; callers must not treat it as success."""


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
    return ["--location", location(), "--scope", PROFILE_SCOPE]


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
        code = "missing_tool" if getattr(exc, "errno", None) == 2 else "spawn_failed"
        return {"error": str(exc)[:400], "code": code}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.kill()
        proc.wait()
        return {"error": "the registry did not answer in time", "code": "timeout"}
    except OSError as exc:
        code = "spawn_failed"
        return {"error": str(exc)[:400], "code": code}
    out = (stdout or "").strip()
    if not out:
        err = (stderr or "no answer").strip()[:400]
        code = "nonzero" if proc.returncode else "empty"
        return {"error": err, "code": code}
    try:
        answer = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        return {"error": "unreadable answer: %s" % out[:200], "code": "bad_json"}
    if not isinstance(answer, dict):
        return {"error": "the registry answer was not an object", "code": "bad_shape"}
    if proc.returncode and "error" not in answer:
        answer = {"error": answer.get("error") or "registry exited %s" % proc.returncode}
    return answer


def _require_registry(answer: dict, *, needs_label: bool = False) -> dict:
    if answer.get("error"):
        raise RegistryError(str(answer["error"]))
    if needs_label:
        label = answer.get("label")
        if not isinstance(label, str) or not label.strip():
            raise RegistryError("the registry answer carried no letter")
    return answer


def register(session_id: str, title: str = "", space: str = "single") -> dict:
    """Ask the Operator for this session's letter.  Idempotent: the same session id
    always answers with the same letter."""
    args = [
        "register",
        "--platform",
        PLATFORM,
        *_registry_identity_args(),
        "--session-id",
        session_id,
        "--seat",
        SEAT,
        "--source",
        "the MM seat's session-letter hook",
    ]
    if space != "single":
        # Only the double space is asked for:  the single space is the default of every
        # registry CLI, so a conversation's registration still answers an older one.
        args += ["--letter-space", space]
    if title:
        _scan_title(title)
        args += ["--title", title]
    return registry_call(args)


def touch_session(session_id: str) -> dict:
    return registry_call(
        ["touch", "--platform", PLATFORM, *_registry_identity_args(), "--session-id", session_id]
    )


def update_title(session_id: str, title: str) -> dict:
    """Carry the thread topic into the registry (topic only — not the letter prefix)."""
    _scan_title(title)
    letter = cached_letter(session_id)
    topic = registry_topic_from_display(letter, title)
    return _require_registry(
        registry_call(
            [
                "update",
                "--platform",
                PLATFORM,
                *_registry_identity_args(),
                "--session-id",
                session_id,
                "--title",
                topic,
            ]
        )
    )


def close_letter(session_id: str, reason: str) -> dict:
    return _require_registry(
        registry_call(
            [
                "close",
                "--platform",
                PLATFORM,
                *_registry_identity_args(),
                "--session-id",
                session_id,
                "--reason",
                reason,
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


# ---- the title form ---------------------------------------------------------------


def is_english(text: str) -> bool:
    """True when the text carries no non-ASCII letter.  A Chinese title fails here."""
    for ch in text:
        if unicodedata.category(ch).startswith("L") and ord(ch) > 127:
            return False
    return True


def wanted_title(letter: str, topic: str) -> str:
    """The title form:  the letter, then the topic, in Title Case English."""
    topic = (topic or "").strip()
    if letter:
        prefix = letter + ":"
        if topic.startswith(prefix):
            topic = topic[len(prefix) :].lstrip()
    else:
        topic = re.sub(r"^\s*[A-Z]{1,2}:\s*", "", topic, count=1)
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


def letter_cache_dir() -> str:
    base = os.environ.get("MINIMAX_HOOKS_DATA") or os.path.join(data_dir(), "hook-state")
    path = os.path.join(base, "session-letters")
    for candidate in (base, path):
        if os.path.islink(candidate):
            raise ValueError("the letter cache path must not be a symlink")
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path):
        raise ValueError("the letter cache path must not be a symlink")
    return path


def cache_letter(session_id: str, letter: str, topic: str = "", space: str = "single") -> None:
    if letter not in LETTERS and len(letter) != 2:
        raise ValueError("invalid letter for cache")
    payload = {"letter": letter, "topic": topic, "host": location(), "space": space}
    path = os.path.join(letter_cache_dir(), session_id + ".json")
    fd, tmp = tempfile.mkstemp(dir=letter_cache_dir(), prefix=".letter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            # The bytes have to be on the disk before the name points at them, and the
            # directory entry has to be on the disk before anyone trusts the name.
            os.fchmod(fh.fileno(), 0o600)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(letter_cache_dir(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cached_letter(session_id: str) -> str:
    path = os.path.join(letter_cache_dir(), session_id + ".json")
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


def space_for(session_id: str) -> str:
    """The letter space a session belongs in:  single for a conversation, double for a
    subthread, so a subthread never takes the letter a conversation holds."""
    row = session_row(session_id) or {}
    if row.get("parent_session_id") or row.get("session_kind") == "task":
        return "double"
    return "single"


def letter_for(session_id: str, topic: str = "", space: str = "single") -> str:
    """The letter for a session:  the cache first, the Operator otherwise."""
    letter = cached_letter(session_id)
    if letter:
        try:
            touch_session(session_id)
        except RegistryError:
            pass
        return letter
    answer = _require_registry(register(session_id, topic, space), needs_label=True)
    letter = str(answer.get("label") or "")
    if letter:
        cache_letter(session_id, letter, topic, space)
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
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
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


def hook_session_start() -> int:
    """Give the session its letter, and tell the agent the two forms it must use."""
    payload = read_hook_input()
    session_id = hook_session_id(payload)
    if not session_id:
        return 0
    row = session_row(session_id) or {}
    space = space_for(session_id)
    try:
        letter = letter_for(session_id, str(row.get("title") or ""), space)
    except (RegistryError, ValueError):
        print(
            "Session letter: none yet (the Operator could not be reached).  Name this thread "
            "in English; if a letter arrives later, prefix the title with it."
        )
        return 0
    if not letter:
        print(
            "Session letter: none yet (the Operator could not be reached).  Name this thread "
            "in English; if a letter arrives later, prefix the title with it."
        )
        return 0
    print(
        "Session letter: %s.  Two rules for this session:\n"
        '1. Name this thread in English and start the title with the letter: "%s: <topic>".\n'
        "2. State the letter early in every Zulip post and every board item you file — the "
        'first line, for example "%s: what this post is about" — so a reply knows where it '
        "is going.\n"
        "The registry carries the same title under the session id %s."
        % (letter, letter, letter, session_id)
    )
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
    if row.get("parent_session_id"):
        return 0  # a subthread posts under its parent's letter
    if row.get("session_kind") not in (None, "conversation"):
        return 0
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    try:
        letter = cached_letter(session_id) or letter_for(session_id, "", space_for(session_id))
    except RegistryError:
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
    if row.get("parent_session_id") or row.get("session_kind") not in (None, "conversation"):
        return 0
    try:
        letter = cached_letter(session_id) or letter_for(session_id, "", space_for(session_id))
    except RegistryError:
        return 0
    if not letter:
        return 0
    title = str(row.get("title") or "")
    ok, why = title_ok(title, letter)
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
    if not set_title(session_id, fallback):
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


def is_live(row: dict, live_hours: int) -> bool:
    """A thread is live while it was touched inside the window."""
    created = row.get("created_at_ms") or 0
    try:
        age_hours = (time.time() * 1000 - float(created)) / 3600000.0
    except (TypeError, ValueError):
        return True
    return age_hours <= max(1, int(live_hours))


def sweep(apply: bool, live_hours: int = 36, topics_file: str = "") -> int:
    """Name the sessions this host already has:  letter, English title, registry in step."""
    topics: dict[str, str] = {}
    if topics_file:
        try:
            with open(topics_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("the topic file must be a JSON object")
            topics = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, ValueError, TypeError) as exc:
            print("cannot read the topic file: %s" % exc, file=sys.stderr)
            return 2
    out = []
    for row in sessions():
        kind = str(row.get("session_kind") or "")
        if kind not in ("conversation", "task"):
            continue
        title = str(row.get("title") or "")
        if looks_like_prompt(title):
            continue
        session_id = row["session_id"]
        space = "single" if kind == "conversation" else "double"
        try:
            letter = letter_for(session_id, title, space)
        except RegistryError as exc:
            out.append({"session_id": session_id, "error": str(exc)})
            continue
        if not letter:
            out.append({"session_id": session_id, "error": "no letter"})
            continue
        topic = topics.get(session_id) or (title if is_english(title) else "")
        if session_id in topics:
            try:
                _scan_title(str(topics[session_id]))
            except ValueError as exc:
                out.append({"session_id": session_id, "error": str(exc)})
                continue
        if not topic:
            out.append(
                {"session_id": session_id, "letter": letter, "error": "needs an English topic"}
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
            "live": is_live(row, live_hours),
        }
        if apply:
            if not already_ok:
                if not set_title(session_id, wanted):
                    entry["error"] = "could not rename in the session store"
                    out.append(entry)
                    continue
                try:
                    update_title(session_id, wanted)
                except (RegistryError, ValueError) as exc:
                    entry["error"] = str(exc)
                    out.append(entry)
                    continue
                entry["applied"] = True
            else:
                try:
                    update_title(session_id, title)
                    entry["registry_synced"] = True
                except (RegistryError, ValueError) as exc:
                    entry["registry_error"] = str(exc)
        out.append(entry)
    exit_code = 0
    if apply:
        answer = registry_call(
            [
                "sweep",
                "--platform",
                PLATFORM,
                "--seat",
                SEAT,
                "--stale-after-minutes",
                str(max(60, int(live_hours) * 60)),
            ]
        )
        if answer.get("error"):
            print(json.dumps({"sweep_error": answer["error"]}), file=sys.stderr)
            exit_code = 2
        else:
            print(json.dumps({"registry_sweep": answer.get("count", 0)}), file=sys.stderr)
    print(json.dumps(out, indent=2))
    return exit_code


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
        try:
            print(json.dumps(_require_registry(register(args.session_id, args.title), needs_label=True)))
        except RegistryError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        return 0
    if args.command == "rename":
        try:
            answer = _require_registry(register(args.session_id))
        except RegistryError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        letter = str(answer.get("label") or "")
        title = wanted_title(letter, args.title)
        if not set_title(args.session_id, title):
            print(json.dumps({"error": "could not rename in the session store"}))
            return 2
        try:
            update_title(args.session_id, title)
        except (RegistryError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps({"letter": letter, "title": title}))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
