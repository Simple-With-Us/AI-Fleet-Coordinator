"""Bounded, read-only discovery of existing local and cloud agent chats.

Transcript parsing is deliberately allow-list based: only user and assistant text
blocks are surfaced.  Tool calls, tool results, reasoning, and arbitrary paths are
never returned by the public projection.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

MAX_FILES_PER_SOURCE = 160
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TAIL_BYTES = 192 * 1024
MAX_EXCERPT_CHARS = 2400
DEFAULT_TTL = 30.0
STALE_TTL = 300.0

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer [REDACTED]"),
)


def _default_scrub(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _safe_text(value: Any, *, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """Extract plain text from known text-only transcript shapes."""
    chunks: list[str] = []
    if isinstance(value, str):
        chunks.append(value)
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    elif isinstance(value, dict):
        if value.get("type") in {"text", "input_text", "output_text"} and isinstance(value.get("text"), str):
            chunks.append(value["text"])
    return "\n".join(part.strip() for part in chunks if part.strip())[:max_chars]


def _safe_read(path: Path, max_bytes: int = MAX_TAIL_BYTES) -> bytes:
    """Read a bounded tail without following a symlink."""
    try:
        if path.is_symlink() or not path.is_file():
            return b""
        st = path.stat(follow_symlinks=False)
        if st.st_size > MAX_FILE_BYTES:
            with path.open("rb") as fh:
                fh.seek(max(0, st.st_size - max_bytes))
                data = fh.read(max_bytes)
            if st.st_size > max_bytes:
                data = data.split(b"\n", 1)[-1]
            return data
        with path.open("rb") as fh:
            fh.seek(max(0, st.st_size - max_bytes))
            data = fh.read(max_bytes)
        if st.st_size > max_bytes:
            data = data.split(b"\n", 1)[-1]
        return data
    except OSError:
        return b""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    for raw in _safe_read(path).splitlines():
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _first_jsonl(path: Path) -> dict[str, Any] | None:
    """Read only the bounded first record, used for rollout metadata headers."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as fh:
            raw = fh.readline(32 * 1024)
        row = json.loads(raw)
        return row if isinstance(row, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _timestamp(*values: Any) -> float:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) / (1000 if value > 10_000_000_000 else 1)
        if isinstance(value, str):
            try:
                from datetime import datetime
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return 0.0


def _stable_id(host: str, platform: str, session_id: str) -> str:
    value = "\0".join((host, platform, session_id)).encode("utf-8", "replace")
    return hashlib.sha256(value).hexdigest()


def _within(path: Path, root: Path) -> bool:
    """Lexical containment check after rejecting symlink components."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return False
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


class ChatCatalog:
    """A short-lived cache of safe metadata and bounded user/assistant excerpts."""

    def __init__(self, home: str | os.PathLike[str] | None = None, root: str | os.PathLike[str] | None = None,
                 *, scrub: Callable[[str], str] | None = None, ttl: float = DEFAULT_TTL,
                 max_records: int = 24, host: str | None = None, grok_transport: Any = None,
                 cursor_transport: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.home = Path(home or root or Path.home()).expanduser()
        self.root = Path(root).expanduser() if root else self.home
        self.scrub = (lambda text: scrub(_default_scrub(text))) if scrub else _default_scrub
        self.ttl = max(1.0, min(float(ttl), 300.0))
        self.max_records = max(1, min(int(max_records), 1000))
        self.host = host or socket.gethostname()
        self._grok = grok_transport
        self._cursor = cursor_transport
        self._clock = clock
        self._records: dict[str, dict[str, Any]] = {}
        self._expires = 0.0
        self._last_scan = 0.0
        self._availability: dict[str, str] = {}
        self._missing_since: dict[str, float] = {}

    def _row(self, platform: str, seat: str, session_id: str, *, cwd: str | None, title: str,
             updated_at: float, status: str = "available", wake: bool = False,
             source: str = "", internal_path: str | None = None,
             excerpts: list[dict[str, str]] | None = None, host: str | None = None) -> dict[str, Any]:
        safe_cwd = cwd if isinstance(cwd, str) and (cwd.startswith("/") or
                                                    (platform == "cursor-cloud" and "://" in cwd)) else None
        if safe_cwd and platform == "cursor-cloud":
            safe_cwd = self.scrub(safe_cwd)[:500]
        safe_title = self.scrub(title or "Untitled chat")[:180]
        stable_host = host or self.host
        readable = status not in {"unavailable", "stale", "metadata-only"}
        return {"id": _stable_id(stable_host, platform, session_id), "platform": platform,
                "host": stable_host, "seat": seat, "session_id": session_id,
                "cwd": safe_cwd, "title": safe_title,
                "updated_at": updated_at or None, "status": status,
                "stale": status == "stale",
                "capabilities": {"read": readable, "wake": bool(wake),
                                 "wake_status": "available" if wake else "unsupported"},
                "_source": source, "_internal_path": internal_path, "_excerpts": excerpts or []}

    def _unavailable(self, platform: str, seat: str, reason: str) -> dict[str, Any]:
        self._availability[platform] = reason
        return self._row(platform, seat, "__unavailable__", cwd=None,
                         title=f"{platform} chat catalog unavailable", updated_at=0,
                         status="unavailable", source=reason)

    def _list_files(self, base: Path, *, recursive: bool = False) -> list[Path]:
        if not base.is_dir() or not _within(base, self.home):
            return []
        found: list[Path] = []
        def modified(path: Path) -> float:
            try:
                return path.stat(follow_symlinks=False).st_mtime
            except OSError:
                return 0.0
        if recursive:
            visited_dirs = 0
            for current, dirs, files in os.walk(base, followlinks=False):
                visited_dirs += 1
                if visited_dirs > MAX_FILES_PER_SOURCE * 32:
                    break
                dirs[:] = sorted((d for d in dirs if not (Path(current) / d).is_symlink()),
                                 key=lambda d: modified(Path(current) / d), reverse=True)
                file_paths = sorted((Path(current) / name for name in files if name.endswith(".jsonl")
                                     and not (Path(current) / name).is_symlink()),
                                    key=modified, reverse=True)
                for path in file_paths:
                    if _within(path, base) and path.is_file():
                        found.append(path)
                        if len(found) >= MAX_FILES_PER_SOURCE:
                            return found
        else:
            try:
                parents = sorted((p for p in base.iterdir() if p.is_dir() and not p.is_symlink()),
                                 key=modified, reverse=True)[:MAX_FILES_PER_SOURCE]
            except OSError:
                return []
            for parent in parents:
                try:
                    files = sorted((p for p in parent.iterdir() if p.suffix == ".jsonl" and p.is_file()
                                    and not p.is_symlink()), key=modified, reverse=True)
                except OSError:
                    continue
                found.extend(files[:MAX_FILES_PER_SOURCE - len(found)])
                if len(found) >= MAX_FILES_PER_SOURCE:
                    break
        return found

    def _parse_text_events(self, rows: list[dict[str, Any]], platform: str) -> list[dict[str, str]]:
        excerpts: list[dict[str, str]] = []
        for row in rows:
            role = None
            value: Any = None
            if platform == "claude":
                if row.get("type") in {"user", "assistant"}:
                    role, value = row.get("type"), row.get("message", {}).get("content") if isinstance(row.get("message"), dict) else row.get("content")
            elif platform == "codex":
                if row.get("type") == "response_item" and isinstance(row.get("payload"), dict):
                    payload = row["payload"]
                    if (payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}
                            and not (payload.get("role") == "assistant" and payload.get("channel") == "analysis")):
                        role, value = payload.get("role"), payload.get("content")
                elif row.get("type") in {"user", "assistant"}:
                    role, value = row.get("type"), row.get("content")
            elif row.get("type") in {"user", "assistant"}:
                role, value = row.get("type"), row.get("content")
            if role:
                text = self.scrub(_safe_text(value))
                if text:
                    excerpts.append({"role": role, "text": text})
        return excerpts[-8:]

    def _scan_claude(self) -> list[dict[str, Any]]:
        base = self.home / ".claude" / "projects"
        files = self._list_files(base)
        rows = []
        for path in files:
            events = _jsonl(path)
            if not events:
                continue
            sid = path.stem
            cwd = None
            title = "Claude chat " + sid[:12]
            updated = 0.0
            for event in events:
                if isinstance(event.get("cwd"), str):
                    cwd = event["cwd"]
                if event.get("type") in {"custom-title", "session_name"}:
                    title = _safe_text(event.get("customTitle") or event.get("name") or event.get("title"), max_chars=180) or title
                updated = max(updated, _timestamp(event.get("timestamp"), event.get("createdAt")))
            rows.append(self._row("claude", "CLAUDE", sid, cwd=cwd, title=title, updated_at=updated,
                                  source=str(path), internal_path=str(path),
                                  excerpts=self._parse_text_events(events, "claude")))
        return rows or [self._unavailable("claude", "CLAUDE", "no local project transcripts")]

    def _scan_codex(self) -> list[dict[str, Any]]:
        base = self.home / ".codex" / "sessions"
        files = self._list_files(base, recursive=True)
        rows = []
        for path in files:
            events = _jsonl(path)
            if not events:
                continue
            sid, cwd, title, updated = path.stem, None, "Codex chat " + path.stem[:12], 0.0
            header = _first_jsonl(path)
            ordered_events = ([header] if header else []) + events
            for event in ordered_events:
                if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                    meta = event["payload"]
                    sid = str(meta.get("id") or sid)
                    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else cwd
                    title = _safe_text(meta.get("title"), max_chars=180) or title
                updated = max(updated, _timestamp(event.get("timestamp"), event.get("ts")))
            rows.append(self._row("codex", "CODEX", sid, cwd=cwd, title=title, updated_at=updated,
                                  source=str(path), internal_path=str(path), excerpts=self._parse_text_events(events, "codex")))
        return rows or [self._unavailable("codex", "CODEX", "no local rollout transcripts")]

    def _scan_grok(self, *, wait: bool = False, force: bool = False) -> list[dict[str, Any]]:
        if self._grok is None:
            try:
                from .chat_transport import GrokTransport
                self._grok = GrokTransport()
            except Exception:
                return [self._unavailable("grok-leader", "GROK", "transport unavailable")]
        try:
            # Daemon catalog refreshes stay asynchronous.  The transport publishes a
            # snapshot only after its bounded page walk finishes, and state reads stay
            # inside the catalog cap.  One-shot callers and a forced rescan can wait.
            live = self._grok.discover(wait=wait, force=force, limit=MAX_FILES_PER_SOURCE)
        except Exception:
            return [self._unavailable("grok-leader", "GROK", "discovery failed")]
        rows = []
        for item in live[:MAX_FILES_PER_SOURCE]:
            sid, cwd = item.get("session_id"), item.get("cwd")
            if not isinstance(sid, str) or not sid:
                continue
            state = item.get("state")
            folder = self._find_grok_session(sid)
            title = _safe_text(item.get("title"), max_chars=180) or "Grok chat " + sid[:12]
            updated = _timestamp(item.get("updated_at"))
            excerpts: list[dict[str, str]] = []
            if folder:
                summary = self._read_json(folder / "summary.json")
                title = _safe_text(summary.get("generated_title") or summary.get("session_summary"), max_chars=180) or title
                info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
                updated = _timestamp(summary.get("updated_at") or summary.get("last_active_at"))
                if not cwd and isinstance(info.get("cwd"), str):
                    cwd = info["cwd"]
                excerpts = self._parse_text_events(_jsonl(folder / "chat_history.jsonl"), "grok-leader")
            if not isinstance(cwd, str) or not cwd.startswith("/"):
                cwd = None
            wake = (item.get("wake") is True and state == "idle" and bool(item.get("turn_ended_at")) and bool(cwd)
                    and not item.get("pending_permission") and not item.get("self"))
            rows.append(self._row("grok-leader", "GROK", sid, cwd=cwd, title=title,
                                  updated_at=updated,
                                  status="stale" if item.get("stale") else
                                  (state or "unknown") if folder else "metadata-only",
                                  wake=wake,
                                  source="grok-live", internal_path=str(folder) if folder else None,
                                  excerpts=excerpts))
        if not rows:
            self._availability["grok-leader"] = "no live existing leader chats"
            return [self._unavailable("grok-leader", "GROK", "no live existing leader chats")]
        return rows

    def _find_grok_session(self, sid: str) -> Path | None:
        if not sid or "/" in sid or "\\" in sid:
            return None
        base = self.home / ".grok" / "sessions"
        if not base.is_dir() or not _within(base, self.home):
            return None
        try:
            for group in base.iterdir():
                candidate = group / sid
                if group.is_symlink() or candidate.is_symlink():
                    continue
                if candidate.is_dir() and _within(candidate, base):
                    return candidate
        except OSError:
            pass
        return None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}

    def _scan_cursor(self) -> list[dict[str, Any]]:
        if self._cursor is None:
            try:
                from .cursor_chat_transport import CursorCloudTransport
                self._cursor = CursorCloudTransport()
            except Exception:
                return [self._unavailable("cursor-cloud", "CURSOR", "transport unavailable")]
        try:
            records = self._cursor.discover()
        except Exception:
            return [self._unavailable("cursor-cloud", "CURSOR", "discovery failed")]
        rows = []
        for record in records[:self.max_records]:
            sid = record.get("session_id") or record.get("agent_id")
            if not isinstance(sid, str) or not sid:
                continue
            state = str(record.get("state", "unknown")).lower()
            rows.append(self._row("cursor-cloud", "CURSOR", sid,
                                  cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                                  title=_safe_text(record.get("title"), max_chars=180) or "Cursor Cloud chat " + sid[:12],
                                  updated_at=_timestamp(record.get("updated_at")), status=state,
                                  wake=state in {"idle", "active", "busy"}, source="cursor-cloud",
                                  host=record.get("host") if isinstance(record.get("host"), str) else None))
        return rows or [self._unavailable("cursor-cloud", "CURSOR", "no discoverable existing chats")]

    def _scan_cursor_local(self) -> list[dict[str, Any]]:
        """Cursor ACP metadata is catalogued, but no local transcript reader is enabled."""
        base = self.home / ".cursor" / "acp-sessions"
        if not base.is_dir() or not _within(base, self.home):
            return [self._unavailable("cursor-local", "CURSOR", "no local ACP session store")]
        rows = []
        try:
            dirs = sorted((p for p in base.iterdir() if p.is_dir() and not p.is_symlink()),
                          key=lambda p: self._mtime(p / "meta.json"), reverse=True)[:MAX_FILES_PER_SOURCE]
        except OSError:
            return [self._unavailable("cursor-local", "CURSOR", "ACP metadata unavailable")]
        for directory in dirs:
            meta_path = directory / "meta.json"
            if not _within(meta_path, base):
                continue
            meta = self._read_json(meta_path)
            if not meta:
                continue
            try:
                updated = meta_path.stat(follow_symlinks=False).st_mtime
            except OSError:
                updated = 0.0
            rows.append(self._row("cursor-local", "CURSOR", directory.name,
                                  cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
                                  title=_safe_text(meta.get("title"), max_chars=180) or "Cursor local chat " + directory.name[:12],
                                  updated_at=updated, status="metadata-only", wake=False,
                                  source="cursor-acp-metadata", internal_path=None))
        return rows or [self._unavailable("cursor-local", "CURSOR", "no local ACP sessions")]

    def _scan_muse(self) -> list[dict[str, Any]]:
        """Read only the fixed Muse SQLite metadata columns, never prompt/search fields."""
        path = self.home / ".local/share/muse/session-index.db"
        if path.is_symlink() or not path.is_file() or not _within(path, self.home):
            return [self._unavailable("muse", "MC", "no Muse session index")]
        conn = None
        try:
            conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=0.25)
            cols = {str(row[1]).lower(): str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
            allowed = ("session_id", "id", "workspace", "title", "created_at", "updated_at",
                       "last_active_at", "createdatms", "updatedatms", "status")
            selected = [cols[name] for name in allowed if name in cols]
            lower_selected = {name.lower(): name for name in selected}
            id_column = lower_selected.get("session_id") or lower_selected.get("id")
            if id_column is None:
                return [self._unavailable("muse", "MC", "Muse session metadata schema unavailable")]
            ordering = next((lower_selected[name] for name in ("updated_at", "updatedatms", "last_active_at",
                                                                "created_at", "createdatms") if name in lower_selected), None)
            query = "SELECT " + ", ".join('"' + name.replace('"', '""') + '"' for name in selected) + ' FROM "sessions"'
            if ordering:
                query += ' ORDER BY "' + ordering.replace('"', '""') + '" DESC'
            query += " LIMIT ?"
            rows = []
            for values in conn.execute(query, (MAX_FILES_PER_SOURCE,)):
                meta = dict(zip(selected, values))
                sid = meta.get(id_column)
                if isinstance(sid, bool) or not isinstance(sid, (str, int)) or not str(sid):
                    continue
                session_key = str(sid)
                workspace_key, title_key = lower_selected.get("workspace"), lower_selected.get("title")
                rows.append(self._row("muse", "MC", session_key,
                                      cwd=meta.get(workspace_key) if workspace_key and isinstance(meta.get(workspace_key), str) else None,
                                      title=_safe_text(meta.get(title_key), max_chars=180) if title_key else "Muse chat " + session_key[:12],
                                      updated_at=_timestamp(*(meta.get(lower_selected.get(name)) for name in
                                                              ("updated_at", "last_active_at", "created_at",
                                                               "updatedatms", "createdatms"))),
                                      status="metadata-only", wake=False, source="muse-session-index"))
            return rows or [self._unavailable("muse", "MC", "no Muse sessions")]
        except (sqlite3.Error, OSError, ValueError):
            return [self._unavailable("muse", "MC", "Muse session index unavailable")]
        finally:
            if conn is not None:
                conn.close()

    def _minimax_dirs(self) -> list[Path]:
        base = self.home / ".minimax/v2/sessions"
        if not base.is_dir() or not _within(base, self.home):
            return []
        found: list[Path] = []
        visited = 0
        for current, dirs, files in os.walk(base, followlinks=False):
            visited += 1
            if visited > MAX_FILES_PER_SOURCE * 32:
                break
            here = Path(current)
            dirs[:] = sorted((name for name in dirs if not (here / name).is_symlink()), reverse=True)
            if "session_" in here.name and "manifest.json" in files:
                if _within(here / "manifest.json", base):
                    found.append(here)
                    if len(found) >= MAX_FILES_PER_SOURCE:
                        break
                dirs[:] = []
        return sorted(found, key=lambda p: self._mtime(p / "manifest.json"), reverse=True)

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat(follow_symlinks=False).st_mtime
        except OSError:
            return 0.0

    def _scan_minimax(self) -> list[dict[str, Any]]:
        dirs = self._minimax_dirs()
        if not dirs:
            return [self._unavailable("minimax", "MM", "no MiniMax Code session store")]
        rows = []
        for directory in dirs:
            manifest = self._read_json(directory / "manifest.json")
            sid = manifest.get("sessionId")
            if not isinstance(sid, str) or not sid:
                continue
            # Manifest schema is allowlisted to session identity and timestamps; `paths`
            # can contain internal store locations and is intentionally not projected.
            title = ""
            cwd = None
            updated = _timestamp(manifest.get("updatedAtMs"), manifest.get("createdAtMs"),
                                 manifest.get("updated_at"), manifest.get("created_at")) or self._mtime(directory / "manifest.json")
            messages = directory / "messages.jsonl"
            readable = messages.is_file() and not messages.is_symlink() and _within(messages, self.home / ".minimax/v2/sessions")
            rows.append(self._row("minimax", "MM", sid, cwd=cwd,
                                  title=title or "MiniMax Code chat", updated_at=updated,
                                  status="available" if readable else "metadata-only", wake=False,
                                  source="minimax-v2-metadata",
                                  internal_path=str(directory)))
        return rows

    def _scan_fx(self) -> list[dict[str, Any]]:
        base = self.home / ".fx/sessions"
        if not _within(base, self.home):
            return [self._unavailable("fx", "FX", "Fx session store unavailable")]
        index_path = base / "index.json"
        index = self._read_json(index_path)
        sessions = index.get("sessions")
        if not isinstance(sessions, list):
            return [self._unavailable("fx", "FX", "no Fx session index")]
        rows = []
        for item in sessions[:MAX_FILES_PER_SOURCE]:
            if not isinstance(item, dict):
                continue
            sid = item.get("id")
            if not isinstance(sid, str) or not sid:
                continue
            row_path = self.home / ".fx/sessions" / sid / "session.json"
            if not _within(row_path, self.home / ".fx/sessions"):
                continue
            meta = self._read_json(row_path)
            # The index contains identifiers only for selection; session.json is the metadata source.
            source = meta
            workspace = source.get("workspace_root") if isinstance(source.get("workspace_root"), str) else None
            title = _safe_text(source.get("title"), max_chars=180) or "Fx chat " + sid[:12]
            updated = _timestamp(source.get("updated_at"), source.get("last_active_at"),
                                 source.get("created_at")) or self._mtime(row_path)
            rows.append(self._row("fx", "FX", sid, cwd=workspace, title=title, updated_at=updated,
                                  status="metadata-only", wake=False, source="fx-session-metadata"))
        return rows or [self._unavailable("fx", "FX", "no Fx sessions")]

    def _scan(self, *, wait_grok: bool = False, force_grok: bool = False) -> None:
        self._availability = {}
        groups = [self._scan_claude(), self._scan_codex(), self._scan_grok(wait=wait_grok, force=force_grok), self._scan_cursor(),
                  self._scan_muse(), self._scan_minimax(), self._scan_fx()]
        # Explicit availability rows ensure unsupported/empty stores remain visible.
        groups.append(self._scan_cursor_local())
        rows = [row for group in groups for row in group]
        for platform, seat in (("antigravity", "AG"),):
            rows.append(self._unavailable(platform, seat, "adapter not available"))
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            unique[row["id"]] = row
        records = list(unique.values())
        availability = sorted((row for row in records if row["status"] == "unavailable"),
                              key=lambda row: row["platform"])
        sessions = [row for row in records if row["status"] != "unavailable"]
        keep_availability = availability[:min(len(availability), self.max_records)]
        session_limit = max(0, self.max_records - len(keep_availability))
        by_platform: dict[str, list[dict[str, Any]]] = {}
        for row in sessions:
            by_platform.setdefault(row["platform"], []).append(row)
        for rows_for_platform in by_platform.values():
            rows_for_platform.sort(key=lambda row: (row.get("updated_at") or 0, row["id"]), reverse=True)
        chosen_sessions: list[dict[str, Any]] = []
        platforms = sorted(by_platform)
        while platforms and len(chosen_sessions) < session_limit:
            next_platforms = []
            for platform in platforms:
                choices = by_platform[platform]
                if choices and len(chosen_sessions) < session_limit:
                    chosen_sessions.append(choices.pop(0))
                if choices:
                    next_platforms.append(platform)
            platforms = next_platforms
        chosen = chosen_sessions + keep_availability
        self._records = {row["id"]: row for row in chosen}
        self._last_scan = self._clock()
        self._expires = self._last_scan + self.ttl

    def discover(self, force: bool = False, *, wait_grok: bool = False) -> list[dict[str, Any]]:
        now = self._clock()
        if force or now >= self._expires:
            previous = self._records
            try:
                self._scan(wait_grok=wait_grok, force_grok=force)
            except Exception:
                if previous and now - self._last_scan <= STALE_TTL:
                    self._records = previous
                    for row in self._records.values():
                        row["status"] = "stale"
                        row["stale"] = True
                        row["capabilities"]["read"] = False
                        row["capabilities"]["wake"] = False
                        row["capabilities"]["wake_status"] = "stale"
                    self._expires = now + min(self.ttl, 10)
                else:
                    self._records = {}
                    self._availability["catalog"] = "scan failed"
            else:
                # Keep previous rows marked stale for one bounded interval when a source vanishes.
                if previous:
                    current_ids = set(self._records)
                    for ident in current_ids:
                        self._missing_since.pop(ident, None)
                    for ident, old in previous.items():
                        if ident not in current_ids and old.get("status") != "unavailable":
                            since = self._missing_since.setdefault(ident, now)
                            if now - since > STALE_TTL:
                                self._missing_since.pop(ident, None)
                                continue
                            old = dict(old)
                            old["status"] = "stale"
                            old["stale"] = True
                            old["capabilities"] = dict(old["capabilities"], read=False,
                                                        wake=False, wake_status="stale")
                            self._records[ident] = old
                if len(self._records) > self.max_records:
                    self._records = dict(list(self._records.items())[:self.max_records])
        return [self._public(row) for row in self._records.values()]

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        result = {key: row[key] for key in ("id", "platform", "host", "seat", "session_id", "cwd", "title",
                                            "updated_at", "status", "stale", "capabilities") if key in row}
        if row.get("status") == "unavailable" and row.get("_source"):
            result["error"] = row["_source"]
        elif row.get("status") == "stale":
            result["error"] = "catalog data is stale"
        return result

    def resolve(self, record_id: str) -> dict[str, Any] | None:
        """Resolve only an exact ID in this catalog's bounded in-memory cache."""
        if not isinstance(record_id, str):
            return None
        self.discover()
        row = self._records.get(record_id)
        return dict(row) if row is not None else None

    def excerpt(self, record_id: str, limit: int = 4) -> list[dict[str, str]]:
        row = self.resolve(record_id)
        if row is None:
            return []
        count = max(0, min(int(limit), 8))
        if not count or not row.get("capabilities", {}).get("read"):
            return []
        if row.get("platform") == "minimax":
            folder = Path(row.get("_internal_path") or "")
            base = self.home / ".minimax/v2/sessions"
            messages = folder / "messages.jsonl"
            if not _within(messages, base) or messages.is_symlink():
                return []
            excerpts: list[dict[str, str]] = []
            total_chars = 0
            for event in reversed(_jsonl(messages)):
                message = event.get("message") if isinstance(event.get("message"), dict) else None
                if not message or message.get("role") not in {"user", "assistant"}:
                    continue
                text = self.scrub(_safe_text(message.get("content"), max_chars=1800))
                if not text:
                    continue
                remaining = 6000 - total_chars
                if remaining <= 0:
                    break
                text = text[:remaining]
                excerpts.append({"role": message["role"], "text": text})
                total_chars += len(text)
                if len(excerpts) >= count:
                    break
            excerpts.reverse()
            row["_excerpts"] = excerpts
            self._records[record_id]["_excerpts"] = row["_excerpts"]
            return list(row["_excerpts"])
        if row.get("_source") == "cursor-cloud" and self._cursor is not None:
            try:
                source_rows = self._cursor.excerpt(row["session_id"], limit=count)
            except Exception:
                return []
            safe_rows = []
            for item in source_rows if isinstance(source_rows, list) else []:
                if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
                    text = self.scrub(_safe_text(item.get("text")))
                    if text:
                        safe_rows.append({"role": item["role"], "text": text})
            row["_excerpts"] = safe_rows[-count:]
            self._records[record_id]["_excerpts"] = row["_excerpts"]
        return [{"role": item["role"], "text": item["text"]} for item in row.get("_excerpts", [])[-count:]] if count else []

    def projection(self, record_id: str) -> dict[str, Any] | None:
        row = self.resolve(record_id)
        return self._public(row) if row is not None else None


Catalog = ChatCatalog
