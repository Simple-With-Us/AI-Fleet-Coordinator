"""Own-bearer gate for `simple-chat mcp`, the local stdio tools.

The server starts for one seat only, and only after that seat's own bearer has been read from its
private file and the hosted gateway has answered `whoami` with the same seat.  The bearer file is
`~/.secrets/fleet-tools/<seat>` (the place the assigned stdio connectors read), never an environment
value, a launcher claim or another seat's file: a launcher or flag can only restrict which seat is
asked for, and a seat that is not the bearer's own is refused.  There is no unnamed-seat mode and no
enumeration of the identities this host can reach.  The bearer is never passed on, logged or put in
an error.  Nothing here changes any other command.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import fleet_bearer as F
from .. import identity as I

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


class Refused(Exception):
    """`exit_code` is the startup exit code: 2 a bad seat name, 3 refused, 6 the gateway cannot confirm."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def bearer_path(home: Path, seat: str) -> Path:
    stem = seat.lower()
    if not SEAT_FILE.fullmatch(stem):
        raise Refused("%r is not a seat name this server can authenticate" % seat, 2)
    return home.joinpath(*BEARER_DIR, stem)


def verify(rt: Any, args: Any, *, client_factory: Any = None) -> str:
    """The seat whose own bearer the hosted gateway just confirmed.  Raises Refused, never a bearer."""
    resolved = I.resolve_seat(
        rt.env,
        flag=getattr(args, "as_seat", None),
        default=getattr(args, "default_seat", None),
        use_tag=False,
    )
    if resolved.problem:
        raise Refused(resolved.problem, 3)
    if not resolved.seat:
        raise Refused(
            "no seat named:  pass --default-seat (or --as) so the seat's own bearer can be checked;  "
            "nothing is served without one",
            3,
        )
    seat = I.canonical_seat(resolved.seat)
    home = Path(rt.home) if getattr(rt, "home", None) else Path.home()
    path = bearer_path(home, seat)
    factory = client_factory or F.Client
    try:
        client = bound(factory(path, expected_seat=seat, identity_env=rt.env))
        who = client.rpc("tools/call", {"name": "whoami", "arguments": {}}).get("structuredContent")
    except F.FleetError as exc:
        code = 3 if exc.code in ("not_configured", "not_authorized", "invalid_argument") else 6
        raise Refused("%s's own bearer was not accepted (%s)" % (seat, exc.code), code) from None
    if (
        client.seat != seat
        or not isinstance(who, dict)
        or who.get("authenticated_seat") != seat
        or who.get("seat") != seat
    ):
        raise Refused("the gateway's identity answer did not match %s's own bearer" % seat, 3)
    return seat
