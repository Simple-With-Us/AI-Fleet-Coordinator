"""Test double for the hosted gateway's `whoami`, used only by the stdio test harness.

It replaces the transport and the bearer file read, nothing else:  seat resolution, the bearer path,
the seat match and the exit-code mapping are the real mcp/bearer_gate.py.  `mode` is "ok" (the gateway
accepts the seat's own bearer), "revoked" (refused), "unavailable" (cannot confirm) or "other-seat"
(answers for a different seat).  Production code never imports this.
"""

from __future__ import annotations

from typing import Any, Callable

from agent_sync import fleet_bearer as F
from agent_sync.mcp import bearer_gate as BG

OTHER = "ZZSTUB"


class StubClient:
    def __init__(self, mode: Callable[[], str], path: Any, expected_seat: str | None = None, identity_env: Any = None):
        self._mode = mode
        self.path = path
        self.seat = expected_seat

    def rpc(self, method: str, params: dict[str, Any], write: bool = False) -> dict[str, Any]:
        mode = self._mode()
        if mode == "revoked":
            raise F.FleetError("not_authorized", "gateway HTTP 401")
        if mode == "unavailable":
            raise F.FleetError("unavailable", "gateway transport or response failed")
        seat = OTHER if mode == "other-seat" else self.seat
        return {"structuredContent": {"seat": seat, "authenticated_seat": seat}}


def stub_verify(mode: Callable[[], str]) -> Callable[[Any, Any], str]:
    def verify(rt: Any, args: Any) -> str:
        return BG.verify(rt, args, client_factory=lambda path, expected_seat=None, identity_env=None: StubClient(mode, path, expected_seat, identity_env))

    return verify
