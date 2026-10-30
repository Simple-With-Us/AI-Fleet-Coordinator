"""Own-bearer proof for the local `fleet-recall-mcp.py` server.

The seat is named by the launcher (AGENT_LAUNCH_SEAT, else AGENT_SEAT or AGENT_TAG), and a call is served
only when that seat's own bearer, read from its private file `~/.secrets/fleet-tools/<seat>`, is accepted
by the hosted gateway (`whoami` answers for the same seat).  This is checked on every tools/call, so a
revoked or replaced bearer stops the next call.  The environment never supplies a bearer, and a launcher
or argument can only restrict the seat, never choose another one.  The seat the call acts as is the
bearer's:  recall_contribute is stored under it and a search is logged under it.  The bearer is never
logged or returned, and errors carry no secret.

The corpus and public-service credentials the server already used stay as they were:  this module only
decides whether, and as whom, a call may run.  An in-process fake corpus (FLEET_RECALL_FAKE) holds no real
data and is not gated.  `fleet_bearer.py` is the FleetLink CLI's client, vendored byte-for-byte (a test
compares it with scripts/agent_sync/fleet_bearer.py when that file is present).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import fleet_bearer as F
from .core import FleetRagError

# The per-seat bearer files the assigned stdio connectors and the fleet CLI read;  no environment or
# other store supplies a bearer.
BEARER_DIR = (".secrets", "fleet-tools")
SEAT_FILE = re.compile(r"[a-z][a-z0-9-]{1,19}")


# Longest the gateway may hold one whoami before the call is refused.  It is the HTTP connection's socket
# timeout, so it bounds connect and each read separately, not the total time.  Nothing is cached: every
# call still asks the gateway, and a timeout refuses the call.
GATEWAY_TIMEOUT_SECONDS = 4


class BoundedTransport(F.GatewayTransport):
    def open(self, request, timeout=40):
        return super().open(request, timeout=min(timeout, GATEWAY_TIMEOUT_SECONDS))


def bound(client):
    """Cap the real client's gateway timeout;  a double without the real transport is left alone."""
    if isinstance(getattr(client, "_transport", None), F.GatewayTransport):
        client._transport.close()
        client._transport = BoundedTransport()
    return client


HINTS = ("AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG")

# The client class;  tests replace it with a double for the gateway.
client_factory: Callable[..., Any] = F.Client


def launcher_seat(env: Mapping[str, str]) -> str:
    """The one seat the launcher names, canonical, or '' when none.  Conflicting names are refused."""
    seats = {F.canonical_hint(env.get(name, "")) for name in HINTS} - {""}
    if len(seats) > 1:
        raise FleetRagError("the launcher names conflicting seats;  nothing was served")
    if env.get("AGENT_LAUNCHER") and not env.get("AGENT_LAUNCH_SEAT"):
        raise FleetRagError("the launcher did not bind a seat;  nothing was served")
    return next(iter(seats), "")


def bearer_path(home: Path, seat: str) -> Path:
    stem = seat.lower()
    if not SEAT_FILE.fullmatch(stem):
        raise FleetRagError("%r is not a seat name this server can authenticate" % seat)
    return home.joinpath(*BEARER_DIR, stem)


def verify(env: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    """The seat whose own bearer the gateway just accepted.  Raises FleetRagError, never a bearer."""
    env = os.environ if env is None else env
    seat = launcher_seat(env)
    if not seat:
        raise FleetRagError(
            "no seat named (AGENT_SEAT or the launcher's seat):  the seat's own bearer cannot be checked;  "
            "nothing was served"
        )
    path = bearer_path(home or Path.home(), seat)
    try:
        client = bound(client_factory(path, expected_seat=seat, identity_env=env))
        who = client.rpc("tools/call", {"name": "whoami", "arguments": {}}).get("structuredContent")
    except F.FleetError as exc:
        raise FleetRagError(
            "%s's own bearer was not accepted (%s);  nothing was served" % (seat, exc.code)
        ) from None
    if (
        client.seat != seat
        or not isinstance(who, dict)
        or who.get("authenticated_seat") != seat
        or who.get("seat") != seat
    ):
        raise FleetRagError(
            "the gateway's identity answer did not match %s's own bearer;  nothing was served"
            % seat
        )
    return seat
