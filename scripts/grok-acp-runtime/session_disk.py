#!/usr/bin/env python3
"""Disk helpers for driving a live Grok TUI session.

Any local agent (Claude, Cursor, Grok Bot, this TUI, …) can use these.
Does not print secrets.  Does not session/load a live chat.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSIONS_ROOT = Path.home() / ".grok" / "sessions"
ACTIVE_SESSIONS = Path.home() / ".grok" / "active_sessions.json"

JsonDict = dict[str, Any]

WORKING_PHASES = {
    "streaming_reasoning",
    "streaming",
    "tool_execution",
    "responding",
    "thinking",
}
NEEDS_INPUT_PHASES = {
    "permission_prompt",
    "ask_user",
    "needs_input",
    "blocked",
}


def find_session_dir(session_id: str) -> Path | None:
    if not session_id or "/" in session_id or "\\" in session_id:
        return None
    if not SESSIONS_ROOT.is_dir():
        return None
    matches = [p for p in SESSIONS_ROOT.glob("*/" + session_id) if p.is_dir()]
    return matches[0] if matches else None


def load_active() -> list[JsonDict]:
    if not ACTIVE_SESSIONS.is_file():
        return []
    try:
        raw = json.loads(ACTIVE_SESSIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[JsonDict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = item.get("session_id") or item.get("sessionId")
        if not sid:
            continue
        out.append({
            "sessionId": str(sid),
            "cwd": item.get("cwd"),
            "pid": item.get("pid"),
            "openedAt": item.get("opened_at") or item.get("openedAt"),
            "live": True,
        })
    return out


def _jsonl_last(path: Path, n: int = 200) -> list[JsonDict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[JsonDict] = []
    for line in lines[-n:]:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _parse_ts(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _attach_state_fields(row: JsonDict, state: JsonDict) -> None:
    row["turnState"] = state.get("turnState")
    row["phase"] = state.get("phase")
    if state.get("pendingTool"):
        row["pendingTool"] = state.get("pendingTool")
    else:
        row.pop("pendingTool", None)
    # Real turn activity counts as "not idle" even when summary.json lags.
    row["turnStartedAt"] = state.get("turnStartedAt")
    row["turnEndedAt"] = state.get("turnEndedAt")


def peek_summary(session_id: str) -> JsonDict:
    path = find_session_dir(session_id)
    if path is None:
        return {"ok": False, "error": "session dir not found", "sessionId": session_id}
    summary_path = path / "summary.json"
    if not summary_path.is_file():
        return {"ok": False, "error": "summary.json missing", "sessionId": session_id, "dir": str(path)}
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "summary.json parse failed: %s" % exc, "sessionId": session_id}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    state = turn_state(session_id)
    out: JsonDict = {
        "ok": True,
        "sessionId": session_id,
        "cwd": info.get("cwd"),
        "title": data.get("generated_title") or data.get("session_summary"),
        "text": data.get("last_turn_summary") or "",
        "recap": data.get("last_recap") or "",
        "updatedAt": data.get("updated_at") or data.get("last_active_at"),
        "source": "disk",
        "turnState": state.get("turnState"),
        "phase": state.get("phase"),
    }
    if state.get("pendingTool"):
        out["pendingTool"] = state.get("pendingTool")
    return out


def peek_tail(session_id: str, lines: int = 12) -> JsonDict:
    """Last assistant / thought / tool lines from updates.jsonl."""
    path = find_session_dir(session_id)
    if path is None:
        return {"ok": False, "error": "session dir not found", "sessionId": session_id}
    rows = _jsonl_last(path / "updates.jsonl", n=max(40, lines * 8))
    chunks: list[JsonDict] = []
    for obj in rows:
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        upd = params.get("update") if isinstance(params.get("update"), dict) else params
        kind = upd.get("sessionUpdate") or upd.get("session_update") or obj.get("method")
        content = upd.get("content") if isinstance(upd.get("content"), dict) else {}
        text = ""
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        title = upd.get("title") or upd.get("toolName") or ""
        if kind in {"agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "hook_execution"}:
            chunks.append({
                "kind": kind,
                "text": text[:500],
                "title": title,
                "ts": obj.get("timestamp"),
            })
    tail = chunks[-lines:]
    state = turn_state(session_id)
    out: JsonDict = {
        "ok": True,
        "sessionId": session_id,
        "turnState": state.get("turnState"),
        "phase": state.get("phase"),
        "tail": tail,
        "source": "updates.jsonl",
    }
    if state.get("pendingTool"):
        out["pendingTool"] = state.get("pendingTool")
    return out


def turn_state(session_id: str) -> JsonDict:
    """idle | working | needs-input | unknown.  From events.jsonl, not session/load."""
    path = find_session_dir(session_id)
    live_ids = {a["sessionId"] for a in load_active()}
    live = session_id in live_ids
    if path is None:
        return {
            "sessionId": session_id,
            "turnState": "unknown",
            "live": live,
            "phase": None,
            "pendingTool": None,
        }
    events = _jsonl_last(path / "events.jsonl", n=400)
    last_started = 0.0
    last_ended = 0.0
    phase = None
    phase_ts = 0.0
    pending_tool = None
    pending_ts = 0.0
    for ev in events:
        kind = ev.get("type")
        ts = _parse_ts(ev.get("ts"))
        if kind == "turn_started":
            last_started = max(last_started, ts)
        elif kind == "turn_ended":
            last_ended = max(last_ended, ts)
        elif kind == "phase_changed":
            phase = ev.get("phase")
            phase_ts = ts
        elif kind == "permission_requested":
            pending_tool = ev.get("tool_name") or ev.get("toolName")
            pending_ts = ts
        elif kind == "permission_resolved":
            pending_tool = None
            pending_ts = ts
    now = time.time()
    state = "idle"
    if pending_tool and (now - pending_ts) < 600:
        state = "needs-input"
    elif phase in NEEDS_INPUT_PHASES and (now - phase_ts) < 600:
        state = "needs-input"
    elif last_started > last_ended:
        state = "working"
    elif phase in WORKING_PHASES and (now - phase_ts) < 90:
        state = "working"
    elif not live:
        state = "idle"
    if state != "needs-input":
        pending_tool = None
    return {
        "sessionId": session_id,
        "turnState": state,
        "live": live,
        "phase": phase,
        "pendingTool": pending_tool,
        "turnStartedAt": last_started or None,
        "turnEndedAt": last_ended or None,
    }


def enrich_sessions(sessions: list[JsonDict]) -> list[JsonDict]:
    active = {a["sessionId"]: a for a in load_active()}
    merged: list[JsonDict] = []
    seen: set[str] = set()
    for s in sessions:
        sid = str(s.get("sessionId") or "")
        row = dict(s)
        if sid in active:
            row["live"] = True
            row["pid"] = active[sid].get("pid")
            row["openedAt"] = active[sid].get("openedAt")
            seen.add(sid)
        else:
            row.setdefault("live", False)
        if sid:
            st = turn_state(sid)
            _attach_state_fields(row, st)
            peek = peek_summary(sid)
            if peek.get("ok"):
                row.setdefault("title", peek.get("title"))
                row["lastTurnSummary"] = peek.get("text")
                row.setdefault("cwd", peek.get("cwd"))
        merged.append(row)
    for sid, a in active.items():
        if sid in seen:
            continue
        st = turn_state(sid)
        peek = peek_summary(sid)
        extra: JsonDict = {
            "sessionId": sid,
            "cwd": peek.get("cwd") or a.get("cwd"),
            "title": peek.get("title"),
            "updatedAt": peek.get("updatedAt") or a.get("openedAt"),
            "live": True,
            "pid": a.get("pid"),
            "openedAt": a.get("openedAt"),
            "lastTurnSummary": peek.get("text") if peek.get("ok") else None,
            "note": "in active_sessions.json; leader list missed it",
        }
        _attach_state_fields(extra, st)
        merged.append(extra)
    return merged


def prefix_prompt(text: str, from_name: str | None) -> str:
    name = (from_name or os.environ.get("AGENT_TAG") or os.environ.get("AGENT_SEAT") or "remote").strip()
    if not name:
        name = "remote"
    body = (text or "").strip()
    header = "[from: %s]" % name
    if body.startswith(header):
        return body
    return "%s %s" % (header, body)


def self_session_id() -> str:
    return (os.environ.get("GROK_SESSION_ID") or "").strip()


def is_self_session(session_id: str) -> bool:
    me = self_session_id()
    return bool(me) and session_id == me


# Loaded chats idle longer than this unload MCP via session/close.  Disk
# stays; /resume reloads tools.  Override with GROK_IDLE_UNLOAD_HOURS.
DEFAULT_IDLE_UNLOAD_SEC = 4 * 3600
# A "stub" is a session that was created and never took a real user turn
# (a client opened it and walked away).  It holds a full MCP set for nothing,
# so it gets a much shorter clock.  Override with GROK_IDLE_UNLOAD_STUB_MINUTES
# (0 turns the stub rule off).
DEFAULT_STUB_IDLE_SEC = 30 * 60


def updated_at_epoch(row: JsonDict) -> float:
    return _parse_ts(row.get("updatedAt") or row.get("updated_at"))


def _epoch(raw: Any) -> float:
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def last_activity_epoch(row: JsonDict) -> float:
    """Latest sign of life: summary updated_at or the last turn start/end."""
    return max(
        updated_at_epoch(row),
        _epoch(row.get("turnStartedAt")),
        _epoch(row.get("turnEndedAt")),
    )


def idle_age_seconds(row: JsonDict, now: float | None = None) -> float:
    ts = last_activity_epoch(row)
    if ts <= 0:
        return 0.0
    return max(0.0, (now if now is not None else time.time()) - ts)


def pid_alive(pid: Any) -> bool:
    """True when pid exists.  An unusable pid counts as alive (cannot tell)."""
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return True
    if n <= 0:
        return True
    try:
        os.kill(n, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def apply_pid_liveness(rows: list[JsonDict]) -> list[JsonDict]:
    """A row from active_sessions.json whose TUI pid is gone is not attached."""
    for row in rows:
        if row.get("live") and not pid_alive(row.get("pid")):
            row["live"] = False
            row["stalePid"] = True
    return rows


def count_real_user_turns(session_id: str) -> int | None:
    """Real (non-synthetic) user messages in chat_history.jsonl.

    Returns 0 for a session that only holds the system prompt plus synthetic
    reminders (a stub), 1 as soon as any real user message is seen (the scan
    stops there; histories reach 1 MB), and None when the file is missing or
    unreadable (unknown, so never treated as a stub).
    """
    path = find_session_dir(session_id)
    if path is None:
        return None
    hist = path / "chat_history.jsonl"
    if not hist.is_file():
        return None
    try:
        with hist.open("rb") as fh:
            for raw in fh:
                if b'"user"' not in raw[:64]:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "user":
                    continue
                if not obj.get("synthetic_reason"):
                    return 1
    except OSError:
        return None
    return 0


def annotate_user_turns(rows: list[JsonDict]) -> list[JsonDict]:
    """Set row["userTurns"] on loaded, unattached rows (the stub candidates)."""
    for row in rows:
        if row.get("live") or not row.get("loaded"):
            continue
        sid = str(row.get("sessionId") or "")
        if sid:
            row["userTurns"] = count_real_user_turns(sid)
    return rows


def is_stub_row(row: JsonDict) -> bool:
    """No attached client, zero real user turns, and no turn ever started."""
    if row.get("live"):
        return False
    if row.get("userTurns") != 0:
        return False
    return not _epoch(row.get("turnStartedAt"))


def unload_skip_reason(
    row: JsonDict,
    *,
    now: float | None = None,
    max_idle_sec: float = DEFAULT_IDLE_UNLOAD_SEC,
    self_id: str | None = None,
    stub_idle_sec: float | None = None,
) -> str | None:
    """None = this chat should session/close.  Else a skip reason.

    A chat is a candidate only when it holds MCP processes (row["loaded"],
    set from the process table) or a TUI is attached (row["live"]).
    Does not delete transcripts.  Unknown timestamps are skipped (not
    unloaded).  Working / needs-input / pendingTool / this TUI / a session
    with background processes outside the leader stay up.

    Clocks: a stub (see is_stub_row) unloads after stub_idle_sec when that is
    set; everything else uses max_idle_sec.
    """
    sid = str(row.get("sessionId") or "").strip()
    if not sid:
        return "no_id"
    me = self_id if self_id is not None else self_session_id()
    if me and sid == me:
        return "self"
    if not (row.get("live") or row.get("loaded")):
        return "not_loaded"
    state = str(row.get("turnState") or "unknown")
    if state in {"working", "needs-input"}:
        return "busy"
    if row.get("pendingTool"):
        return "pending_tool"
    if row.get("backgroundPids"):
        return "background_process"
    if last_activity_epoch(row) <= 0:
        return "no_timestamp"
    age = idle_age_seconds(row, now)
    if stub_idle_sec and float(stub_idle_sec) > 0 and is_stub_row(row):
        return None if age >= float(stub_idle_sec) else "fresh"
    if age < float(max_idle_sec):
        return "fresh"
    return None


def select_idle_unload(
    rows: list[JsonDict],
    *,
    now: float | None = None,
    max_idle_sec: float = DEFAULT_IDLE_UNLOAD_SEC,
    self_id: str | None = None,
    stub_idle_sec: float | None = None,
) -> list[JsonDict]:
    out: list[JsonDict] = []
    for row in rows:
        if unload_skip_reason(
            row,
            now=now,
            max_idle_sec=max_idle_sec,
            self_id=self_id,
            stub_idle_sec=stub_idle_sec,
        ) is None:
            out.append(row)
    return out


def poll_after_inject(
    session_id: str,
    before_started: float | None = None,
    timeout: float = 180.0,
    interval: float = 1.5,
) -> JsonDict:
    """Wait for a NEW turn after `before_started`, then that turn to end.

    Snapshot turnStartedAt before inject and pass it here.  Do not treat a
    pre-existing idle as the reply.  Returns on needs-input, or when
    turn_ended >= the new turn_started.
    """
    if before_started is None:
        before_started = float(turn_state(session_id).get("turnStartedAt") or 0.0)
    deadline = time.time() + max(1.0, timeout)
    saw_new = False
    while time.time() < deadline:
        st = turn_state(session_id)
        started = float(st.get("turnStartedAt") or 0.0)
        ended = float(st.get("turnEndedAt") or 0.0)
        if st.get("turnState") == "needs-input":
            peek = peek_summary(session_id)
            peek["await"] = "needs-input"
            peek["phase"] = st.get("phase")
            if st.get("pendingTool"):
                peek["pendingTool"] = st.get("pendingTool")
            peek["sawNewTurn"] = started > before_started
            return peek
        if started > before_started:
            saw_new = True
            if ended >= started:
                peek = peek_summary(session_id)
                peek["await"] = "idle"
                peek["sawNewTurn"] = True
                peek["turnStartedAt"] = started
                peek["turnEndedAt"] = ended
                return peek
        time.sleep(interval)
    peek = peek_summary(session_id)
    peek["await"] = "timeout"
    peek["ok"] = bool(peek.get("ok"))
    peek["sawNewTurn"] = saw_new
    if peek.get("ok"):
        peek["error"] = "no completed turn after inject (%ss)" % int(timeout)
    return peek


def poll_until_idle(session_id: str, timeout: float = 180.0, interval: float = 1.5) -> JsonDict:
    """If the session is working, wait until THIS turn ends.  If idle, return now.

    Post-inject callers should use poll_after_inject with a pre-inject snapshot.
    """
    st = turn_state(session_id)
    if st.get("turnState") == "needs-input":
        peek = peek_summary(session_id)
        peek["await"] = "needs-input"
        peek["phase"] = st.get("phase")
        if st.get("pendingTool"):
            peek["pendingTool"] = st.get("pendingTool")
        return peek
    if st.get("turnState") != "working":
        peek = peek_summary(session_id)
        peek["await"] = "idle"
        return peek
    before = float(st.get("turnEndedAt") or 0.0)
    return poll_after_inject(
        session_id,
        before_started=before,
        timeout=timeout,
        interval=interval,
    )


# timezone imported for fromisoformat fallbacks in older callers
_ = timezone
