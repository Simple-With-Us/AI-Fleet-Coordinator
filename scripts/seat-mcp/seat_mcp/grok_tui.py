"""List live Grok TUI sessions and spawn leader-client prompt jobs.

Does not print secrets.  Does not start a second grok-acp serve.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import GROK_ACP_PYTHON, GROK_DRIVE, GROK_LEADER_CLIENT

JsonDict = dict[str, Any]
ACTIVE_SESSIONS = Path("/Users/jay/.grok/active_sessions.json")


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


def merge_live(sessions: list[JsonDict]) -> list[JsonDict]:
    active = {a["sessionId"]: a for a in load_active()}
    merged: list[JsonDict] = []
    seen: set[str] = set()
    for s in sessions:
        sid = str(s.get("sessionId") or "")
        row = dict(s)
        if sid and sid in active:
            row["live"] = True
            row["pid"] = active[sid].get("pid")
            row["openedAt"] = active[sid].get("openedAt")
            seen.add(sid)
        else:
            row.setdefault("live", False)
        merged.append(row)
    for sid, a in active.items():
        if sid not in seen:
            merged.append({
                "sessionId": sid,
                "cwd": a.get("cwd"),
                "title": None,
                "updatedAt": a.get("openedAt"),
                "live": True,
                "pid": a.get("pid"),
                "openedAt": a.get("openedAt"),
                "note": "in active_sessions.json; leader list missed it",
            })
    return merged


def _parse_helper(stdout: str, stderr: str) -> JsonDict:
    src = (stdout or "").strip() or (stderr or "").strip()
    if not src:
        return {"ok": False, "error": "empty helper output"}
    try:
        data = json.loads(src)
    except json.JSONDecodeError:
        start = src.find("{")
        try:
            data = json.loads(src[start:] if start >= 0 else src)
        except json.JSONDecodeError:
            return {"ok": False, "error": src[:800]}
    if not isinstance(data, dict):
        return {"ok": False, "error": src[:800]}
    return data


def run_leader(argv: list[str], timeout: float) -> JsonDict:
    py = str(GROK_ACP_PYTHON if GROK_ACP_PYTHON.is_file() else "python3")
    helper = GROK_DRIVE if GROK_DRIVE.is_file() else GROK_LEADER_CLIENT
    cmd = [py, str(helper), *argv]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "leader helper timed out"}
    except OSError as exc:
        return {"ok": False, "error": "leader helper spawn failed:  %s" % exc}
    data = _parse_helper(proc.stdout, proc.stderr)
    if proc.returncode != 0 and data.get("ok") is not False:
        data = dict(data)
        data["ok"] = False
        data.setdefault("exitCode", proc.returncode)
    return data


def list_sessions(cwd: str | None = None) -> JsonDict:
    argv = ["list"]
    if cwd:
        argv.extend(["--cwd", cwd])
    data = run_leader(argv, timeout=25.0)
    sessions = merge_live(data.get("sessions") or [])
    out: JsonDict = {
        "ok": bool(data.get("ok")) or bool(sessions),
        "count": len(sessions),
        "sessions": sessions,
    }
    if data.get("error") or data.get("leaderError"):
        out["leaderError"] = data.get("error") or data.get("leaderError")
        if sessions:
            out["partial"] = True
            out["ok"] = True
    return out


def peek_session(session_id: str, cwd: str) -> JsonDict:
    return run_leader(
        ["peek", "--session-id", session_id, "--cwd", cwd],
        timeout=50.0,
    )
