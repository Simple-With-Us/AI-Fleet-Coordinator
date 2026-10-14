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

    @staticmethod
    def _merge_sessions(candidate: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> None:
        known = {row.get("sessionId"): index for index, row in enumerate(candidate)
                 if isinstance(row.get("sessionId"), str)}
        for row in sessions:
            sid = row.get("sessionId")
            if isinstance(sid, str) and sid in known:
                candidate[known[sid]] = dict(row)
            else:
                candidate.append(dict(row))

    @classmethod
    def _refresh(cls) -> None:
        """Walk every leader page, then publish once.

        A first page is not an inventory.  Callers keep the previous complete
        snapshot (or see pending) until this walk finishes, repeats a cursor,
        or reaches the page cap.
        """
        global _DISCOVERY_SESSIONS, _DISCOVERY_UPDATED, _DISCOVERY_ERROR, _DISCOVERY_RUNNING
        global _DISCOVERY_CURSOR, _DISCOVERY_SEEN_CURSORS, _DISCOVERY_CANDIDATE_SESSIONS, _DISCOVERY_COMPLETE
        candidate: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        error: str | None = None
        complete = False
        try:
            for _ in range(_DISCOVERY_MAX_PAGES):
                sessions, next_cursor = cls._read_leader_sessions(cursor)
                if next_cursor is not None and next_cursor in seen:
                    error = "leader repeated pagination cursor"
                    break
                cls._merge_sessions(candidate, sessions)
                if next_cursor is None:
                    complete = True
                    break
                seen.add(next_cursor)
                cursor = next_cursor
            else:
                error = "leader session list exceeded page cap"
        except Exception:
            error = "leader list unavailable"
            complete = False
        with _DISCOVERY_LOCK:
            publish_partial = (not complete and bool(candidate) and _DISCOVERY_SESSIONS is None
                               and error in ("leader repeated pagination cursor",
                                             "leader session list exceeded page cap"))
            if complete or publish_partial:
                _DISCOVERY_SESSIONS = candidate
                _DISCOVERY_UPDATED = time.monotonic()
            if complete:
                _DISCOVERY_ERROR = None
                _DISCOVERY_COMPLETE = True
            else:
                _DISCOVERY_ERROR = error or "leader list unavailable"
                _DISCOVERY_COMPLETE = False
            _DISCOVERY_CURSOR = None
            _DISCOVERY_SEEN_CURSORS = set()
            _DISCOVERY_CANDIDATE_SESSIONS = None
            _DISCOVERY_RUNNING = False
            _DISCOVERY_DONE.set()

    @classmethod
    def _schedule_refresh(cls, *, force: bool) -> None:
        global _DISCOVERY_LAST_ATTEMPT, _DISCOVERY_RUNNING, _DISCOVERY_COMPLETE
        now = time.monotonic()
        with _DISCOVERY_LOCK:
            if _DISCOVERY_RUNNING:
                return
            if not force and now - _DISCOVERY_LAST_ATTEMPT < _DISCOVERY_REFRESH_INTERVAL:
                return
            # A new walk keeps the last good rows until it publishes.  The page
            # interval applies to full refreshes, not to each cursor page.
            if _DISCOVERY_COMPLETE:
                _DISCOVERY_COMPLETE = False
            _DISCOVERY_LAST_ATTEMPT = now
            _DISCOVERY_RUNNING = True
            _DISCOVERY_DONE.clear()
            thread = threading.Thread(target=cls._refresh, name="grok-leader-discovery", daemon=True)
            thread.start()

    def discover(self, *, force: bool = False, wait: bool = True, limit: int = 160,
                 session_id: str | None = None) -> list[dict[str, Any]]:
        """List chats owned by the shared leader, then conservatively derive turn state.

        active_sessions.json tracks attached TUIs, not chats known to the leader, and is
        empty in leader mode.  It is therefore not an inventory or proof of promptability.
        A failed or malformed leader list is an error, never an empty-success fallback.
        One refresh walks up to 20 pages before publishing, so a partial page is never
        a fresh wakeable inventory.
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
        deadline = time.monotonic() + _DISCOVERY_MAX_PAGES * (_DISCOVERY_OUTER_TIMEOUT + 1.0)
        requested = False
        while True:
            # force applies only to the first schedule.  Later iterations just
            # observe the walk already in flight.
            self._schedule_refresh(force=force and not requested)
            requested = True
            with _DISCOVERY_LOCK:
                running = _DISCOVERY_RUNNING
                complete = _DISCOVERY_COMPLETE
                error = _DISCOVERY_ERROR
                have_sessions = _DISCOVERY_SESSIONS is not None
            if not running and (complete or error or have_sessions or not wait):
                break
            if not wait or time.monotonic() >= deadline:
                break
            _DISCOVERY_DONE.wait(deadline - time.monotonic())
        with _DISCOVERY_LOCK:
            sessions = [dict(row) for row in _DISCOVERY_SESSIONS] if _DISCOVERY_SESSIONS is not None else None
            updated = _DISCOVERY_UPDATED
            error = _DISCOVERY_ERROR
        if sessions is None:
            if wait and error:
                raise RuntimeError(error)
            raise RuntimeError("Grok leader discovery pending")
        stale = error is not None or time.monotonic() - updated > _DISCOVERY_STALE_AFTER

        # Limit/filter before touching disk.  turn_state reads the session event log, and
        # preflight only needs the one exact session selected by its durable binding.
        if session_id is not None:
            sessions = [row for row in sessions if row.get("sessionId") == session_id]
        else:
            sessions = sessions[:max(0, min(int(limit), 1000))]

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
        target_session_id = binding.get("session_id")
        if not isinstance(target_session_id, str) or not target_session_id:
            return "unsupported"
        try:
            rows = self.discover(force=force, wait=False, limit=1,
                                 session_id=target_session_id)
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
