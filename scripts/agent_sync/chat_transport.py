"""Existing-session delivery through Grok's shared leader, without a new agent owner."""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable

_RUNTIME = Path(__file__).resolve().parents[1] / "grok-acp-runtime"


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
    def _live_pid(pid: Any) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return False
        try:
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="],
                                    capture_output=True, text=True, timeout=3, check=False)
            return result.returncode == 0 and Path(result.stdout.strip()).name in {"grok", "grok-build"}
        except (OSError, subprocess.SubprocessError):
            return False

    def discover(self) -> list[dict[str, Any]]:
        """Read active-session metadata only.  Disk history alone never makes a chat wakeable."""
        rows = []
        for active in self.disk.load_active():
            sid = active.get("sessionId")
            cwd = active.get("cwd")
            if not isinstance(sid, str) or not sid or not isinstance(cwd, str) or not os.path.isabs(cwd):
                continue
            if not self._live_pid(active.get("pid")):
                continue
            state = self.disk.turn_state(sid)
            rows.append({"platform": "grok-leader", "host": socket.gethostname(),
                         "session_id": sid, "cwd": cwd, "pid": active["pid"],
                         "state": state.get("turnState", "unknown"),
                         "pending_permission": bool(state.get("pendingTool")),
                         "self": self.disk.is_self_session(sid)})
        return rows

    def preflight(self, binding: dict[str, str]) -> str:
        if binding.get("platform") != "grok-leader" or binding.get("host") != socket.gethostname():
            return "unsupported"
        for row in self.discover():
            if row["session_id"] == binding.get("session_id") and row["cwd"] == binding.get("cwd"):
                if row["self"] or row["pending_permission"] or row["state"] != "idle":
                    return "busy"
                return "ready"
        # A disconnected or unavailable chat can become available later; keep it pending.
        return "busy"

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
            return "unknown" if sent else "failed"
        finally:
            if client is not None:
                # This closes only our stdio transport, not the shared leader or chat.
                client.close()
