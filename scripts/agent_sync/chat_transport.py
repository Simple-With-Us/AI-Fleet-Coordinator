"""Existing-session delivery through Grok's shared leader, without a new agent owner."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

_RUNTIME = Path(__file__).resolve().parents[1] / "grok-acp-runtime"
_DISCOVERY_TIMEOUT = 25.0  # leader-client's supported total RPC budget; list can take 14-22s.
_DISCOVERY_OUTER_TIMEOUT = _DISCOVERY_TIMEOUT + 2.0
_DISCOVERY_REFRESH_INTERVAL = 5.0
_DISCOVERY_STALE_AFTER = 30.0
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_DONE = threading.Event()
_DISCOVERY_DONE.set()
_DISCOVERY_SESSIONS: list[dict[str, Any]] | None = None
_DISCOVERY_UPDATED = 0.0
_DISCOVERY_LAST_ATTEMPT = 0.0
_DISCOVERY_ERROR: str | None = None
_DISCOVERY_RUNNING = False
_DISCOVERY_CURSOR: str | None = None
_DISCOVERY_SEEN_CURSORS: set[str] = set()
_DISCOVERY_CANDIDATE_SESSIONS: list[dict[str, Any]] | None = None
_DISCOVERY_COMPLETE = False
_DISCOVERY_MAX_PAGES = 20


def _module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _RUNTIME / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("Grok runtime helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GrokTransport:
    """Only the shared leader owns the target session.  No session/new, cancel or model change."""

    def __init__(self) -> None:
        self.disk = _module("agent_sync_grok_disk", "session_disk.py")
        self.leader = None

    @staticmethod
    def _read_leader_sessions(cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        command = [sys.executable, str(_RUNTIME / "leader-client.py"),
                   "--rpc-timeout", str(_DISCOVERY_TIMEOUT), "list"]
        if cursor:
            command.extend(["--cursor", cursor])
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=_DISCOVERY_OUTER_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Grok leader discovery failed") from exc
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Grok leader returned invalid discovery data") from exc
        if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Grok leader discovery unavailable")
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            raise RuntimeError("Grok leader returned invalid session list")
        next_cursor = payload.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            raise RuntimeError("Grok leader returned invalid pagination cursor")
        return [row for row in sessions if isinstance(row, dict)], next_cursor

    @classmethod
    def _refresh(cls) -> None:
        global _DISCOVERY_SESSIONS, _DISCOVERY_UPDATED, _DISCOVERY_ERROR, _DISCOVERY_RUNNING
        global _DISCOVERY_CURSOR, _DISCOVERY_CANDIDATE_SESSIONS, _DISCOVERY_COMPLETE
        with _DISCOVERY_LOCK:
            cursor = _DISCOVERY_CURSOR
        try:
            sessions, next_cursor = cls._read_leader_sessions(cursor)
        except Exception:
            with _DISCOVERY_LOCK:
                _DISCOVERY_ERROR = "leader list unavailable"
        else:
            with _DISCOVERY_LOCK:
                if next_cursor is not None and next_cursor in _DISCOVERY_SEEN_CURSORS:
                    _DISCOVERY_ERROR = "leader repeated pagination cursor"
                else:
                    if _DISCOVERY_CANDIDATE_SESSIONS is None:
                        _DISCOVERY_CANDIDATE_SESSIONS = []
                    known = {row.get("sessionId"): index for index, row in enumerate(_DISCOVERY_CANDIDATE_SESSIONS)
                             if isinstance(row.get("sessionId"), str)}
                    for row in sessions:
                        sid = row.get("sessionId")
                        if isinstance(sid, str) and sid in known:
                            _DISCOVERY_CANDIDATE_SESSIONS[known[sid]] = dict(row)
                        else:
                            _DISCOVERY_CANDIDATE_SESSIONS.append(dict(row))
                    _DISCOVERY_CURSOR = next_cursor
                    if next_cursor is not None:
                        _DISCOVERY_SEEN_CURSORS.add(next_cursor)
                        if _DISCOVERY_SESSIONS is None:
                            _DISCOVERY_SESSIONS = [dict(row) for row in _DISCOVERY_CANDIDATE_SESSIONS]
                            _DISCOVERY_UPDATED = time.monotonic()
                    else:
                        _DISCOVERY_SESSIONS = [dict(row) for row in _DISCOVERY_CANDIDATE_SESSIONS]
                        _DISCOVERY_CANDIDATE_SESSIONS = None
                        _DISCOVERY_UPDATED = time.monotonic()
                        _DISCOVERY_COMPLETE = True
                        _DISCOVERY_ERROR = None
        finally:
            with _DISCOVERY_LOCK:
                _DISCOVERY_RUNNING = False
                _DISCOVERY_DONE.set()

    @classmethod
    def _schedule_refresh(cls, *, force: bool) -> None:
        global _DISCOVERY_LAST_ATTEMPT, _DISCOVERY_RUNNING, _DISCOVERY_CURSOR
        global _DISCOVERY_SEEN_CURSORS, _DISCOVERY_CANDIDATE_SESSIONS, _DISCOVERY_COMPLETE
        now = time.monotonic()
        with _DISCOVERY_LOCK:
            if _DISCOVERY_RUNNING:
                return
            if not force and now - _DISCOVERY_LAST_ATTEMPT < _DISCOVERY_REFRESH_INTERVAL:
                return
            if _DISCOVERY_COMPLETE:
                # Start a new paginated inventory refresh without discarding the last good rows.
                _DISCOVERY_CURSOR = None
                _DISCOVERY_SEEN_CURSORS = set()
                _DISCOVERY_CANDIDATE_SESSIONS = []
                _DISCOVERY_COMPLETE = False
            _DISCOVERY_LAST_ATTEMPT = now
            _DISCOVERY_RUNNING = True
            _DISCOVERY_DONE.clear()
            thread = threading.Thread(target=cls._refresh, name="grok-leader-discovery", daemon=True)
            thread.start()

    def discover(self, *, force: bool = False, wait: bool = True) -> list[dict[str, Any]]:
        """List chats owned by the shared leader, then conservatively derive turn state.

        active_sessions.json tracks attached TUIs, not chats known to the leader, and is
        empty in leader mode.  It is therefore not an inventory or proof of promptability.
        A failed or malformed leader list is an error, never an empty-success fallback.
        """
        global _DISCOVERY_SESSIONS, _DISCOVERY_UPDATED, _DISCOVERY_LAST_ATTEMPT, _DISCOVERY_ERROR
        global _DISCOVERY_CURSOR, _DISCOVERY_SEEN_CURSORS, _DISCOVERY_CANDIDATE_SESSIONS, _DISCOVERY_COMPLETE
        if force:
            with _DISCOVERY_LOCK:
                if not _DISCOVERY_RUNNING:
                    _DISCOVERY_SESSIONS = None
                    _DISCOVERY_UPDATED = 0.0
                    _DISCOVERY_LAST_ATTEMPT = 0.0
                    _DISCOVERY_ERROR = None
                    _DISCOVERY_CURSOR = None
                    _DISCOVERY_SEEN_CURSORS = set()
                    _DISCOVERY_CANDIDATE_SESSIONS = []
                    _DISCOVERY_COMPLETE = False
        pages = 0
        while True:
            if pages >= _DISCOVERY_MAX_PAGES:
                break
            with _DISCOVERY_LOCK:
                complete = _DISCOVERY_COMPLETE
            # A completed warm cache needs no second refresh in the same call.  A newly
            # requested inventory may start a periodic refresh before returning it.
            if complete and pages > 0:
                break
            self._schedule_refresh(force=force or pages > 0)
            with _DISCOVERY_LOCK:
                complete = _DISCOVERY_COMPLETE
                running = _DISCOVERY_RUNNING
            if complete or pages >= _DISCOVERY_MAX_PAGES:
                break
            if not running and not wait:
                break
            if not wait:
                break
            _DISCOVERY_DONE.wait(_DISCOVERY_OUTER_TIMEOUT + 1.0)
            pages += 1
            with _DISCOVERY_LOCK:
                if _DISCOVERY_ERROR:
                    break
        with _DISCOVERY_LOCK:
            sessions = [dict(row) for row in _DISCOVERY_SESSIONS] if _DISCOVERY_SESSIONS is not None else None
            updated = _DISCOVERY_UPDATED
            error = _DISCOVERY_ERROR
        if sessions is None:
            if wait and error:
                raise RuntimeError(error)
            raise RuntimeError("Grok leader discovery pending")
        stale = error is not None or time.monotonic() - updated > _DISCOVERY_STALE_AFTER

        rows = []
        active_ids = {row.get("sessionId") for row in self.disk.load_active()
                      if isinstance(row, dict) and isinstance(row.get("sessionId"), str)}
        for session in sessions:
            if not isinstance(session, dict):
                continue
            sid = session.get("sessionId")
            cwd = session.get("cwd")
            if not isinstance(sid, str) or not sid:
                continue
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                cwd = None
            state = self.disk.turn_state(sid)
            status = state.get("turnState", "unknown")
            if status == "idle" and not state.get("turnEndedAt"):
                status = "unknown"  # A dead/missing TUI pid and empty history cannot prove idle.
            pending = bool(state.get("pendingTool")) or status == "needs-input"
            self_session = self.disk.is_self_session(sid)
            attached = sid in active_ids
            if stale:
                status = "unknown"
            rows.append({"platform": "grok-leader", "host": socket.gethostname(),
                         "session_id": sid, "cwd": cwd,
                         "title": session.get("title") if isinstance(session.get("title"), str) else "",
                         "updated_at": session.get("updatedAt"), "state": status,
                         "turn_ended_at": state.get("turnEndedAt"), "attached": attached,
                         "pending_permission": pending, "self": self_session, "stale": stale,
                         "wake": not stale and bool(cwd) and status == "idle" and bool(state.get("turnEndedAt"))
                                 and not pending and not self_session})
        return rows

    def preflight(self, binding: dict[str, str], *, force: bool = False) -> str:
        if binding.get("platform") != "grok-leader" or binding.get("host") != socket.gethostname():
            return "unsupported"
        try:
            rows = self.discover(force=force, wait=False)
        except Exception:
            return "busy"  # Discovery pending/unavailable is transient; leave the nudge queued.
        for row in rows:
            if row["session_id"] == binding.get("session_id") and row["cwd"] == binding.get("cwd"):
                if row.get("stale") or row.get("self") or row.get("pending_permission") or not row.get("wake"):
                    return "busy"
                return "ready"
        # A disconnected or unavailable chat can become available later; keep it pending.
        return "busy"

    def receipt(self, binding: dict[str, str], text: str) -> bool:
        """Confirm the exact nudge in the destination's real user history, never assistant output."""
        if binding.get("platform") != "grok-leader" or binding.get("host") != socket.gethostname():
            return False
        path = self.disk.find_session_dir(binding.get("session_id", ""))
        if path is None:
            return False
        try:
            with (path / "chat_history.jsonl").open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 2 * 1024 * 1024))
                if fh.tell():
                    fh.readline()  # Skip a partial first line in the bounded tail.
                for raw in fh:
                    try:
                        row = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(row, dict) or row.get("type") != "user" or row.get("synthetic_reason"):
                        continue
                    content = row.get("content")
                    if isinstance(content, list):
                        content = "".join(part.get("text", "") for part in content
                                          if isinstance(part, dict) and part.get("type") == "text"
                                          and isinstance(part.get("text"), str))
                    if content == text:
                        return True
        except OSError:
            return False
        return False

    def send(self, binding: dict[str, str], text: str, *,
             permit: Callable[[], bool] | None = None) -> str:
        # Repeat the check immediately before connecting.  A leader queues a racing prompt;
        # this adapter never cancels the current turn or passes an interrupt option.
        if self.preflight(binding) != "ready":
            return "deferred"
        if self.leader is None:
            self.leader = _module("agent_sync_grok_leader", "leader-client.py")
        client = None
        sent = False
        try:
            client = self.leader.LeaderStdio()
            self.leader._initialize(client, timeout=15)
            self.leader._resume(client, binding["session_id"], binding["cwd"], timeout=15)
            if self.preflight(binding) != "ready":
                return "deferred"
            if permit is not None and not permit():
                return "deferred"
            sent = True
            # request() correlates the JSON-RPC id.  Generic session notifications or silence
            # are not an acknowledgement.  Timeout is unknown, never an automatic retry.
            client.request("session/prompt", {"sessionId": binding["session_id"],
                           "prompt": [{"type": "text", "text": text}]}, timeout=30)
            return "accepted"
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError):
            if sent and self.receipt(binding, text):
                return "accepted"
            return "unknown" if sent else "failed"
        finally:
            if client is not None:
                # This closes only our stdio transport, not the shared leader or chat.
                client.close()


class ChatTransport:
    """Dispatch only through the registered existing-owner transport for this platform."""

    def __init__(self):
        self.adapters = {}

    def adapter(self, binding):
        platform = binding.get("platform")
        if platform not in self.adapters:
            if platform == "grok-leader":
                self.adapters[platform] = GrokTransport()
            elif platform == "cursor-cloud":
                from .cursor_chat_transport import CursorCloudTransport
                self.adapters[platform] = CursorCloudTransport()
            else:
                raise ValueError("unsupported chat platform")
        return self.adapters[platform]

    def preflight(self, binding):
        return self.adapter(binding).preflight(binding)

    def send(self, binding, text, *, permit=None):
        return self.adapter(binding).send(binding, text, permit=permit)

    def receipt(self, binding, text):
        return self.adapter(binding).receipt(binding, text)
