"""Safe existing-chat delivery for Cursor Cloud agents.

This adapter only follows up an existing Cloud Agent.  It never creates an
agent, cancels a run, or selects a model.  API timeouts after a send are
uncertain and are never retried automatically.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable

from pathlib import Path
import importlib.util


def _load_surfaces() -> Any:
    path = Path(__file__).resolve().parents[1] / "cursor_chat_surfaces.py"
    spec = importlib.util.spec_from_file_location("agent_sync_cursor_chat_surfaces", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cursor Cloud API helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_surfaces = _load_surfaces()
_AGENT_ID = re.compile(r"^bc-[A-Za-z0-9-]{1,120}$")
_MAX_PAGES = 2
_MAX_AGENTS = 200
_MAX_EXCERPT_CHARS = 1200


class _ApiFailure(Exception):
    def __init__(self, status: int | None = None, code: str | None = None) -> None:
        super().__init__("Cursor Cloud API request failed")
        self.status = status
        self.code = code


def _agent_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _AGENT_ID.fullmatch(value) else None


def _unwrap_agent(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("agent") if isinstance(payload.get("agent"), dict) else payload
    return candidate if isinstance(candidate, dict) else None


def _repo_url(agent: dict[str, Any]) -> str | None:
    repos = agent.get("repos")
    if isinstance(repos, list):
        for repo in repos:
            if isinstance(repo, dict) and isinstance(repo.get("url"), str) and repo["url"].strip():
                return repo["url"].strip()
    return None


def _binding_cwd(agent_id: str) -> str:
    return f"cursor-cloud://{agent_id}"


def _messages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows: Any = payload.get("messages") or payload.get("items")
    if not isinstance(rows, list) and isinstance(payload.get("conversation"), dict):
        nested = payload["conversation"]
        rows = nested.get("messages") or nested.get("items")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _message_text(row: dict[str, Any]) -> str:
    if _message_role(row) not in {"user", "human", "assistant"}:
        return ""
    value: Any = row.get("text") if isinstance(row.get("text"), str) else row.get("content", "")
    if isinstance(value, list):
        value = "".join(
            part.get("text", "") for part in value
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
            and isinstance(part.get("text"), str)
        )
    elif isinstance(value, dict):
        value = (value.get("text", "") if value.get("type") in {"text", "input_text", "output_text"}
                 else "")
    return value if isinstance(value, str) else ""


def _message_role(row: dict[str, Any]) -> str:
    value = row.get("role", row.get("speaker", row.get("type", "unknown")))
    return value.lower() if isinstance(value, str) else "unknown"


class CursorCloudTransport:
    """Cursor Cloud API adapter for cataloging and nudging existing agents."""

    platform = "cursor-cloud"
    host = "cursor-cloud"
    seat = "CURSOR"

    def __init__(self, surfaces: Any | None = None, *, timeout_seconds: int = 5,
                 max_agents: int = _MAX_AGENTS) -> None:
        self.surfaces = surfaces or _surfaces
        self.timeout_seconds = max(1, min(int(timeout_seconds), 30))
        self.max_agents = max(1, min(int(max_agents), _MAX_AGENTS))
        self.last_discovery_status = "not_started"

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None,
             *, timeout_seconds: int | None = None) -> dict[str, Any]:
        api_key = self.surfaces.load_api_key()
        if not api_key:
            raise _ApiFailure(code="missing_api_key")
        try:
            timeout = self.timeout_seconds if timeout_seconds is None else max(1, min(int(timeout_seconds), 30))
            payload = self.surfaces.api_request(method, path, api_key, body, timeout=timeout)
        except RuntimeError as exc:
            # The shared helper includes response text in its exception.  Inspect it only to
            # classify status/code; never pass it into logs, state, or an exception message.
            detail = str(exc)
            match = re.search(r"Cursor API (\d{3})", detail)
            status = int(match.group(1)) if match else None
            code = "agent_busy" if "agent_busy" in detail else None
            raise _ApiFailure(status, code) from None
        except Exception:
            raise _ApiFailure() from None
        return payload if isinstance(payload, dict) else {}

    def _detail(self, agent_id: str) -> dict[str, Any] | None:
        safe_id = _agent_id(agent_id)
        if safe_id is None:
            return None
        payload = self._api("GET", f"/v1/agents/{urllib.parse.quote(safe_id, safe='')}")
        agent = _unwrap_agent(payload)
        return agent if agent and agent.get("id") == safe_id else None

    def discover(self) -> list[dict[str, Any]]:
        """Return bounded existing-agent records and retain partial results on later errors."""
        agents: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        partial = False
        for _ in range(_MAX_PAGES):
            query: dict[str, str] = {"limit": "100"}
            if cursor:
                query["cursor"] = cursor
            try:
                payload = self._api("GET", "/v1/agents?" + urllib.parse.urlencode(query))
            except _ApiFailure:
                if not agents:
                    self.last_discovery_status = "unavailable"
                    raise
                partial = True
                break
            items = payload.get("items")
            if not isinstance(items, list):
                items = payload.get("agents", payload.get("data"))
            if not isinstance(items, list):
                partial = True
                break
            for item in items:
                if len(agents) >= self.max_agents:
                    partial = True
                    break
                if not isinstance(item, dict):
                    continue
                agent_id = _agent_id(item.get("id"))
                if not agent_id or item.get("status") == "ARCHIVED":
                    continue
                status = item.get("status")
                if status not in ("IDLE", "ACTIVE"):
                    continue
                agents.append({
                    "platform": self.platform,
                    "host": self.host,
                    "session_id": agent_id,
                    "cwd": _binding_cwd(agent_id),
                    "seat": self.seat,
                    "state": "idle" if status == "IDLE" else "busy",
                    "name": item.get("name") if isinstance(item.get("name"), str) else "",
                    "title": item.get("name") if isinstance(item.get("name"), str) else "",
                    "url": item.get("url") if isinstance(item.get("url"), str) else "",
                    "repo_url": _repo_url(item),
                    "updated_at": item.get("updatedAt"),
                })
            if len(agents) >= self.max_agents:
                partial = True
                break
            next_cursor = payload.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        self.last_discovery_status = "partial" if partial else "complete"
        return agents

    def find(self, session_id: str) -> dict[str, Any] | None:
        """One existing agent by id, in the same record shape `discover` returns.

        The detail endpoint is a single by-id GET, so a chat older than the bounded
        list window is still reachable without walking the agent list.  Only the
        statuses `discover` lists produce a record.
        """
        safe_id = _agent_id(session_id)
        if safe_id is None:
            return None
        try:
            agent = self._detail(safe_id)
        except _ApiFailure:
            return None
        if agent is None or agent.get("status") not in ("IDLE", "ACTIVE"):
            return None
        return {
            "platform": self.platform,
            "host": self.host,
            "session_id": safe_id,
            "cwd": _binding_cwd(safe_id),
            "seat": self.seat,
            "state": "idle" if agent.get("status") == "IDLE" else "busy",
            "name": agent.get("name") if isinstance(agent.get("name"), str) else "",
            "title": agent.get("name") if isinstance(agent.get("name"), str) else "",
            "url": agent.get("url") if isinstance(agent.get("url"), str) else "",
            "repo_url": _repo_url(agent),
            "updated_at": agent.get("updatedAt"),
        }

    def preflight(self, binding: dict[str, str]) -> str:
        if (binding.get("platform") != self.platform or binding.get("host") != self.host
                or not isinstance(binding.get("session_id"), str)):
            return "unsupported"
        if binding.get("cwd") != _binding_cwd(binding["session_id"]):
            return "unsupported"
        try:
            agent = self._detail(binding["session_id"])
        except _ApiFailure as exc:
            if exc.status == 404:
                return "unsupported"
            return "busy"
        if agent is None:
            return "unsupported"
        if agent.get("status") == "ARCHIVED":
            return "unsupported"
        if agent.get("status") != "IDLE":
            return "busy"
        return "ready"

    def excerpt(self, session_id: str, limit: int = 4) -> list[dict[str, str]]:
        """Return a small recent transcript excerpt with bounded text per message."""
        safe_id = _agent_id(session_id)
        if safe_id is None:
            return []
        bounded_limit = max(0, min(int(limit), 20))
        if bounded_limit == 0:
            return []
        try:
            payload = self._api(
                "GET", f"/v1/agents/{urllib.parse.quote(safe_id, safe='')}/conversation"
            )
        except _ApiFailure:
            return []
        rows = _messages(payload)[-bounded_limit:]
        return [{"role": _message_role(row), "text": _message_text(row)[-_MAX_EXCERPT_CHARS:]}
                for row in rows]

    def receipt(self, binding: dict[str, str], text: str) -> bool:
        """Match exactly one user message; latestRunId is never a delivery receipt."""
        if (binding.get("platform") != self.platform or binding.get("host") != self.host
                or _agent_id(binding.get("session_id")) is None or not isinstance(text, str) or not text):
            return False
        try:
            payload = self._api(
                "GET", f"/v1/agents/{urllib.parse.quote(binding['session_id'], safe='')}/conversation"
            )
        except _ApiFailure:
            return False
        matches = [row for row in _messages(payload)
                   if _message_role(row) in {"user", "human"} and _message_text(row) == text]
        return len(matches) == 1

    def send(self, binding: dict[str, str], text: str,
             permit: Callable[[], bool] | None = None) -> str:
        """Send one follow-up to an existing idle agent, with at-most-once uncertainty."""
        readiness = self.preflight(binding)
        if readiness == "busy":
            return "deferred"
        if readiness != "ready":
            return "failed"
        if permit is not None:
            try:
                if not permit():
                    return "deferred"
            except Exception:
                return "deferred"
        sent = False
        try:
            sent = True
            payload = self._api(
                "POST",
                f"/v1/agents/{urllib.parse.quote(binding['session_id'], safe='')}/runs",
                {"prompt": {"text": text}},
                timeout_seconds=5,
            )
            run = payload.get("run") if isinstance(payload.get("run"), dict) else payload
            run_id = run.get("id") if isinstance(run, dict) else None
            if isinstance(run_id, str) and run_id:
                response_agent_id = run.get("agentId") if isinstance(run, dict) else None
                if response_agent_id is not None and response_agent_id != binding["session_id"]:
                    return "unknown"
                return "accepted"
            return "accepted" if self.receipt(binding, text) else "unknown"
        except _ApiFailure as exc:
            if exc.status == 409 and exc.code == "agent_busy":
                return "deferred"
            if exc.status is not None and 400 <= exc.status < 500:
                return "failed"
            if not sent:
                return "failed"
            return "accepted" if self.receipt(binding, text) else "unknown"


_default_transport: CursorCloudTransport | None = None


def _default() -> CursorCloudTransport:
    global _default_transport
    if _default_transport is None:
        _default_transport = CursorCloudTransport()
    return _default_transport


def discover() -> list[dict[str, Any]]:
    return _default().discover()


def excerpt(session_id: str, limit: int = 4) -> list[dict[str, str]]:
    return _default().excerpt(session_id, limit)


def preflight(binding: dict[str, str]) -> str:
    return _default().preflight(binding)


def send(binding: dict[str, str], text: str, permit: Callable[[], bool] | None = None) -> str:
    return _default().send(binding, text, permit)


def receipt(binding: dict[str, str], text: str) -> bool:
    return _default().receipt(binding, text)
