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
registry, which is one file for the fleet.  The registry is on the Operator host, so
this asks it over SSH when the local copy is not there — one namespace, one allocator.

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
import sqlite3
import subprocess
import sys
import unicodedata

SEAT = "MM"
PLATFORM = "minimax"
OPERATOR_HOST = os.environ.get("MM_OPERATOR_HOST", "mac")
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TITLE_MAX = 120

# The Mac's checkout of AI-Fleet-Coordinator, on the Operator host and here.
REPO_REL = "AI-Fleet-Coordinator/scripts"


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
        return os.path.expanduser(override)
    candidate = os.path.expanduser("~/.agent-sync/operator/registry.sqlite3")
    return candidate if os.path.isfile(candidate) else ""


def repo_scripts_dir() -> str:
    """This host's checkout of the fleet scripts, where the registry CLI lives."""
    for candidate in (
        os.path.join(os.path.expanduser("~"), "Code", REPO_REL),
        os.path.join("/mnt/fleet-extra/Code", REPO_REL),
    ):
        if os.path.isdir(candidate):
            return candidate
    return ""


# ---- the registry, locally or over SSH --------------------------------------------


def registry_call(args: list[str], *, timeout: int = 25) -> dict:
    """Run the Operator's registry CLI and return its JSON answer.

    The registry is one file for the fleet, on the Operator host.  When this host has no
    copy, the call goes over SSH to that host, so the letter namespace stays one
    namespace.
    """
    local_db = registry_db()
    cmd = [sys.executable, "-m", "agent_sync.operator_registry"] + args
    if local_db:
        cmd += ["--db", local_db]
    scripts = repo_scripts_dir()
    if local_db and scripts:
        proc = subprocess.run(cmd, cwd=scripts, capture_output=True, text=True, timeout=timeout)
    else:
        remote = "cd ~/Code/%s && %s -m agent_sync.operator_registry %s" % (
            REPO_REL,
            "python3",
            " ".join(args),
        )
        if local_db:
            remote += " --db %s" % local_db
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", OPERATOR_HOST, remote],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    out = (proc.stdout or "").strip()
    if not out:
        return {"error": (proc.stderr or "no answer").strip()[:400]}
    try:
        return json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        return {"error": "unreadable answer: %s" % out[:200]}


def register(session_id: str, title: str = "") -> dict:
    """Ask the Operator for this session's letter.  Idempotent: the same session id
    always answers with the same letter."""
    args = [
        "register",
        "--platform",
        PLATFORM,
        "--location",
        location(),
        "--scope",
        "default",
        "--session-id",
        session_id,
        "--seat",
        SEAT,
        "--source",
        "the MM seat's session-letter hook",
    ]
    if title:
        args += ["--title", title]
    return registry_call(args)


def update_title(session_id: str, title: str) -> dict:
    """Carry the thread's own title into the registry, keyed on the session id."""
    return registry_call(
        ["update", "--platform", PLATFORM, "--session-id", session_id, "--title", title]
    )


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
    topic = re.sub(r"^\s*[A-Z]:\s*", "", topic)  # never double the prefix
    topic = " ".join(topic.split())
    if not topic:
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


# ---- the letter cache -------------------------------------------------------------


def letter_cache_dir() -> str:
    base = os.environ.get("MINIMAX_HOOKS_DATA") or os.path.join(data_dir(), "hook-state")
    path = os.path.join(base, "session-letters")
    os.makedirs(path, exist_ok=True)
    return path


def cache_letter(session_id: str, letter: str, topic: str = "") -> None:
    path = os.path.join(letter_cache_dir(), session_id + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"letter": letter, "topic": topic, "host": location()}, fh)


def cached_letter(session_id: str) -> str:
    path = os.path.join(letter_cache_dir(), session_id + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get("letter") or "")
    except (OSError, ValueError):
        return ""


def letter_for(session_id: str, topic: str = "") -> str:
    """The letter for a session:  the cache first, the Operator otherwise."""
    letter = cached_letter(session_id)
    if letter:
        return letter
    answer = register(session_id, topic)
    letter = str(answer.get("label") or "")
    if letter:
        cache_letter(session_id, letter, topic)
    return letter


# ---- the hooks --------------------------------------------------------------------


def read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def hook_session_start() -> int:
    """Give the session its letter, and tell the agent the two forms it must use."""
    payload = read_hook_input()
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0
    row = session_row(session_id) or {}
    if row.get("parent_session_id") or row.get("session_kind") not in (None, "conversation"):
        return 0  # a subthread rides on its parent's letter
    letter = letter_for(session_id, str(row.get("title") or ""))
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


def hook_pre_tool_use() -> int:
    """Block a post that does not state the letter."""
    payload = read_hook_input()
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0
    letter = cached_letter(session_id) or letter_for(session_id)
    if not letter:
        return 0
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
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
    else:
        for key in ("body", "text", "markdown", "title", "description", "content"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        if not text:
            return 0
    head = text.strip()[:400]
    if re.search(r"(^|\n)\s*%s[:.\s]" % re.escape(letter), head) or letter + ":" in head:
        return 0
    sys.stderr.write(
        "This post does not state the session letter %s.  State it in the first line "
        '(for example "%s: <what this post is about>") and send it again.' % (letter, letter)
    )
    return 2


def hook_stop() -> int:
    """Check the thread title, and carry it into the registry."""
    payload = read_hook_input()
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0
    row = session_row(session_id) or {}
    if row.get("parent_session_id") or row.get("session_kind") not in (None, "conversation"):
        return 0
    letter = cached_letter(session_id) or letter_for(session_id)
    if not letter:
        return 0
    title = str(row.get("title") or "")
    ok, why = title_ok(title, letter)
    if ok:
        update_title(session_id, title)  # the registry carries the thread's own title
        return 0
    active = bool(payload.get("stop_hook_active"))
    if not active:
        sys.stderr.write(
            "This thread is not titled in the seat's form: %s.  Rename it to "
            '"%s: <topic in English>" with the session-title tool, then finish.' % (why, letter)
        )
        return 2
    # Second pass: fix it here rather than leave a thread nobody can file.
    fallback = wanted_title(letter, title)
    set_title(session_id, fallback)
    update_title(session_id, fallback)
    sys.stderr.write("The thread was renamed to %r.\n" % fallback)
    return 0


# ---- the sweep --------------------------------------------------------------------


def sweep(apply: bool) -> int:
    """Name the sessions this host already has:  letter, English title, registry in step."""
    out = []
    for row in sessions(only_conversations=True):
        session_id = row["session_id"]
        title = str(row.get("title") or "")
        letter = letter_for(session_id, title)
        if not letter:
            out.append({"session_id": session_id, "error": "no letter"})
            continue
        wanted = wanted_title(letter, title)
        entry = {
            "session_id": session_id,
            "letter": letter,
            "current_title": title,
            "wanted_title": wanted,
            "english": is_english(title),
            "already_ok": title_ok(title, letter)[0],
        }
        if apply and not entry["already_ok"]:
            set_title(session_id, wanted)
            update_title(session_id, wanted)
            entry["applied"] = True
        out.append(entry)
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook-session-start")
    sub.add_parser("hook-pre-tool-use")
    sub.add_parser("hook-stop")
    sw = sub.add_parser("sweep")
    sw.add_argument("--apply", action="store_true")
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
        return sweep(args.apply)
    if args.command == "letter":
        print(json.dumps(register(args.session_id, args.title)))
        return 0
    if args.command == "rename":
        answer = register(args.session_id)
        letter = str(answer.get("label") or "")
        title = wanted_title(letter, args.title)
        set_title(args.session_id, title)
        update_title(args.session_id, title)
        print(json.dumps({"letter": letter, "title": title}))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
