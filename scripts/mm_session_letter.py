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
import shlex
import sqlite3
import subprocess
import sys
import time
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
            " ".join(shlex.quote(a) for a in args),
        )
        if local_db:
            remote += " --db %s" % shlex.quote(local_db)
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


def register(session_id: str, title: str = "", space: str = "single") -> dict:
    """Ask the Operator for this session's letter.  Idempotent: the same session id
    always answers with the same letter.

    The space is "single" for a main conversation and "double" for a subthread, so a
    subthread never takes the letter a conversation is holding.
    """
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
    if space != "single":
        # Only the double space is asked for:  the single space is the default of every
        # registry CLI, so a conversation's registration still answers an older one.
        args += ["--letter-space", space]
    if title:
        args += ["--title", title]
    return registry_call(args)


def update_title(session_id: str, title: str) -> dict:
    """Carry the thread's own title into the registry, keyed on the session id."""
    return registry_call(
        ["update", "--platform", PLATFORM, "--session-id", session_id, "--title", title]
    )


def close_letter(session_id: str, reason: str) -> dict:
    """End a letter's use.  The row stays, so the history keeps the window."""
    return registry_call(
        [
            "close",
            "--platform",
            PLATFORM,
            "--session-id",
            session_id,
            "--reason",
            reason,
        ]
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


def cache_letter(session_id: str, letter: str, topic: str = "", space: str = "single") -> None:
    path = os.path.join(letter_cache_dir(), session_id + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"letter": letter, "topic": topic, "host": location(), "space": space}, fh)


def cached_letter(session_id: str) -> str:
    path = os.path.join(letter_cache_dir(), session_id + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get("letter") or "")
    except (OSError, ValueError):
        return ""


def letter_for(session_id: str, topic: str = "", space: str = "single") -> str:
    """The letter for a session:  the cache first, the Operator otherwise."""
    letter = cached_letter(session_id)
    if letter:
        return letter
    answer = register(session_id, topic, space)
    letter = str(answer.get("label") or "")
    if letter:
        cache_letter(session_id, letter, topic, space)
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
    space = "single"
    if row.get("parent_session_id") or row.get("session_kind") == "task":
        space = "double"  # a subthread takes a double letter (owner 2026-10-28)
    letter = letter_for(session_id, str(row.get("title") or ""), space)
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
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0
    row = session_row(session_id) or {}
    if row.get("parent_session_id"):
        return 0  # a subthread posts under its parent's letter
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    letter = cached_letter(session_id) or letter_for(session_id)
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
    """Name the sessions this host already has:  letter, English title, registry in step.

    A main conversation takes a single letter and a subthread a double one.  A thread
    that is not live has its letter ended straight away, so the letter returns to the
    pool for the next thread and the registry keeps the window it was used in.
    """
    topics: dict[str, str] = {}
    if topics_file:
        try:
            with open(topics_file, encoding="utf-8") as fh:
                topics = json.load(fh)
        except (OSError, ValueError) as exc:
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
        letter = letter_for(session_id, title, space)
        if not letter:
            out.append({"session_id": session_id, "error": "no letter"})
            continue
        topic = topics.get(session_id) or (title if is_english(title) else "")
        if not topic:
            out.append(
                {"session_id": session_id, "letter": letter, "error": "needs an English topic"}
            )
            continue
        wanted = wanted_title(letter, topic)
        entry = {
            "session_id": session_id,
            "kind": kind,
            "letter": letter,
            "current_title": title,
            "wanted_title": wanted,
            "already_ok": title_ok(title, letter)[0],
            "live": is_live(row, live_hours),
        }
        if apply and not entry["already_ok"]:
            set_title(session_id, wanted)
            update_title(session_id, wanted)
            entry["applied"] = True
        if apply and not entry["live"]:
            # The thread is old:  its letter's use ends, and the history keeps the window.
            closed = close_letter(session_id, "stale and replaced by a newer session")
            entry["letter_ended"] = closed.get("outcome")
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
