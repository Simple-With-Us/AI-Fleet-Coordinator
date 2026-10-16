"""Which bots a stdio MCP connection or a CLI run on this host may act as (AFC-331,
docs/protocols/per-call-identity.md section 3).

The allowed set is every agent bot whose Zulip credential file is on this host:

    seats        every live row of fleet-apps.json `seats` and every `seatApps.extraSeats` key
    BF-*         every <secrets dir>/BF-<Role>-zuliprc whose name matches the BF-* pattern
    GB-*         every persona enabled in mcp/gb-identities.json, the one list the hosted Worker
                 imports as GB_IDENTITIES (GB-ENGINEER stays out until it is enabled there)

each kept only when its credential file (<secrets dir>/<FileCode>-zuliprc) exists.  The owner's own
account and the integration bots (BetterStack, Datadog, Linear, PagerDuty, Sentry) are never seats,
so they never enter the set, whatever files are on disk;  check_bot() still refuses a key that is
not the claimed identity's own bot when the credential is first used.

A launcher pin (AGENT_LAUNCH_SEAT) narrows the set to the pinned seat alone.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from . import identity as I
from . import zulip as Z

HERE = os.path.dirname(os.path.abspath(__file__))
GB_IDENTITIES_PATH = os.path.join(HERE, "mcp", "gb-identities.json")


def gb_identities() -> tuple[str, ...]:
    """The enabled Grok Bot personas, in table order."""
    with open(GB_IDENTITIES_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    enabled = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(enabled, list) or not all(isinstance(name, str) and name.startswith("GB-") for name in enabled):
        raise ValueError("%s: `enabled` must be a list of GB-* names" % GB_IDENTITIES_PATH)
    return tuple(enabled)


def agent_identity_problem(name: str, *, registry: I.SeatRegistry | None = None) -> str | None:
    """None when `name` (canonical) is an agent bot identity a connection may ever act as, else why not.
    Reads the registry;  a registry that cannot be read is a problem (fail closed)."""
    try:
        reg = registry if registry is not None else I.load_seat_registry()
        seat = I.strict_seat(name, registry=reg)
    except I.UnknownSeat as exc:
        return "%s is not an agent bot identity (%s)" % (exc.seat[:40], "a retired seat" if exc.retired else
                                                           "not a live seat, extra seat, BF-* or GB-* name")
    except I.SeatRegistryError as exc:
        return "the seat registry cannot be read:  %s" % exc
    if seat.startswith("GB-") and seat not in gb_identities():
        return "%s is not an enabled Grok Bot persona (scripts/agent_sync/mcp/gb-identities.json)" % seat
    return None


def secrets_dir(env: Mapping[str, str], home: Path) -> Path:
    """Where the credential files are:  the directory default_rc_path() looks in."""
    return Z.default_rc_path("X", env, home).parent


def candidates(env: Mapping[str, str], home: Path, *, registry: I.SeatRegistry | None = None) -> list[str]:
    """Every identity name the registry and the GB table allow, before the file check."""
    reg = registry if registry is not None else I.load_seat_registry(env=env)
    names = set(reg.live) | set(reg.extra) | set(gb_identities())
    directory = secrets_dir(env, home)
    try:
        entries = list(directory.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        seat = Z.seat_from_rc_path(entry)
        if seat and seat.startswith("BF-"):
            names.add(seat)  # agent_identity_problem() below keeps it only when it matches the BF-* pattern
    return sorted(name for name in names if agent_identity_problem(name, registry=reg) is None)


def allowed_identities(env: Mapping[str, str], home: Path, *,
                       registry: I.SeatRegistry | None = None) -> dict[str, Path]:
    """identity -> credential file, for every agent bot whose credential file is on this host.  An
    unreadable registry gives an empty set (the caller then allows its default identity alone)."""
    try:
        names = candidates(env, home, registry=registry)
    except (I.SeatRegistryError, OSError, ValueError):
        return {}
    found: dict[str, Path] = {}
    for name in names:
        path = Z.default_rc_path(name, env, home)
        if path.is_file():
            found[name] = path
    return found


def resolve_name(raw: object) -> str | None:
    """The canonical identity a caller's `identity` argument names (aliases resolved, upper-cased), or
    None when it is not a plain seat-shaped token."""
    if not isinstance(raw, str):
        return None
    return I.valid_seat(raw.strip())
