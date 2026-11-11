"""The one wake router service for every platform (OPS-1048;  owner ruling, Nov 11, 2026:  "the
router is supposed to be for every platform").

    simple-chat wake-router serve [--bind ADDR] [--port N]

One process, one port (8794 on loopback by default), one HTTP server.  Each seat gets its own
`wake_router.Router`, with its own state folder, pending queue, workers, bearer and receipts, and
`wake_router.Seats` routes `/wake/<SEAT>`, `/receipt/<SEAT>/<id>`, `/receipt/<id>` (CLAUDE's),
`/health` and `/health/<SEAT>` to it.  Every guarantee of a single router holds for each seat:
the durable 202, never running a turn twice, per-chat order, the pick chain, the registry recheck
before delivery, holding on unconfirmed settings, the local file that only narrows, SIGHUP and the
test wake.

Which seats.  `BUILDERS` is a fixed table in code of every platform that can be served, keyed by
seat.  Every registered seat is served, and delivery is still gated per seat by
WAKE_ROUTER_DELIVERY_ENABLED and WAKE_ROUTER_DELIVERY_SEATS.  Serving only the seats that knob
names would make arming a seat need a restart, since the seat set is fixed at start while the
knob is refreshed, and a seat not yet armed would answer its wakes with 404 instead of a pick and
a `held` receipt, so its test wakes could not be checked before anyone arms it.  This way arming
or disarming a seat is a knob change and a refresh.

Registering a platform is one line in `BUILDERS`:

    "ZC": Builder("simple_tools.zcode_wake", "router_platform"),

The module names its seat as `SEAT`, which must equal the key.  The function is called as
`function(env, settings)`, where `settings` is the seat's own `WakeSettings`, and returns a
`wake_router.Platform` for that seat.  The module may define `DEFAULTS` (its settings, which
include every key of `wake_router.ROUTER_DEFAULTS`;  ROUTER_DEFAULTS when it has none) and
`state_dir(env)` (its state folder;  `~/.local/state/wake-router/<seat in lower case>` when it has
none).  A seat also needs its bearer knob, WAKE_ROUTER_<SEAT>_KEY in `wake_settings.KEY_KNOBS` and
`fleet_knobs/registry.py`;  without one it is served and refuses every request with 503.

A module is imported once, at start, and only by the constant name in this table, so nothing from
a request ever reaches an import.  A seat whose module is missing, whose `SEAT` disagrees, whose
local settings file is refused, whose builder raises or answers something that is not its
Platform, or whose router cannot start is not served, and only that seat:  `/health` names it
with a reason token and its requests are answered 503.  Every other seat is served as usual.

The settings.  Each seat has a `WakeSettings` of its own (its local file is `<state>/config.json`)
and one `SettingsGroup` refreshes them all with one fetch of the Infisical folder /wake-router,
on a timer and on SIGHUP.  The service's read token, `afc-wake-router-ro`, is scoped to
prod:/wake-router and is the only thing in its environment file (server/afc-wake-router.service).
A process that may not read knobs at all, such as one in a non-prod environment, stops at start,
since that is true of every seat.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import wake_router as WR
from . import wake_settings as WS

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8794


@dataclass(frozen=True)
class Builder:
    """Where one seat's Platform comes from:  a module, by its constant name, and the function in
    it that builds the Platform."""

    module: str
    function: str = "make_platform"


#: Every platform the service can serve, keyed by seat.  A platform joins with one line (module
#: docstring, Registering a platform).
BUILDERS: dict[str, Builder] = {
    "CLAUDE": Builder("simple_tools.claude_wake"),
    # Muse Code (PR #1815 adds muse_wake.router_platform).  Until that lands the import fails, and
    # only MC is down, with the reason on /health.
    "MC": Builder("simple_tools.muse_wake", "router_platform"),
}


class SeatDown(Exception):
    """A seat that cannot be served, with a short reason token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Served:
    """What the service runs:  each seat's router, each seat's settings, and the seats it cannot
    serve with why."""

    seats: WR.Seats
    settings: WS.SettingsGroup


def _log(text: str) -> None:
    """Seats and reason tokens only."""
    sys.stderr.write("wake-router: %s\n" % text)


def default_state(env: Mapping[str, str], seat: str) -> Path:
    home = str(env.get("HOME") or os.path.expanduser("~"))
    return Path(home) / ".local" / "state" / "wake-router" / seat.lower()


def _defaults(module: ModuleType) -> Mapping[str, Any]:
    defaults = getattr(module, "DEFAULTS", WR.ROUTER_DEFAULTS)
    if not isinstance(defaults, Mapping) or not set(WR.ROUTER_DEFAULTS) <= set(defaults):
        raise SeatDown("bad-defaults")
    return defaults


def _state(module: ModuleType, env: Mapping[str, str], seat: str) -> Path:
    locate = getattr(module, "state_dir", None)
    if locate is None:
        return default_state(env, seat)
    try:
        return Path(locate(env))
    except Exception as exc:  # noqa: BLE001 - one seat's fault stays that seat's
        raise SeatDown("state-error-%s" % type(exc).__name__) from None


def build_seat(
    seat: str,
    builder: Builder,
    env: Mapping[str, str],
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> tuple[WR.Router, WS.WakeSettings]:
    """One seat's router and settings, or SeatDown with why."""
    if not WR.SEAT_RE.fullmatch(seat):
        raise SeatDown("bad-seat")
    try:
        module = importer(builder.module)
    except ImportError:
        raise SeatDown("platform-not-installed") from None
    except Exception as exc:  # noqa: BLE001 - a module that fails to import is that seat's fault
        raise SeatDown("import-error-%s" % type(exc).__name__) from None
    if getattr(module, "SEAT", None) != seat:
        raise SeatDown("seat-mismatch")
    make = getattr(module, builder.function, None)
    if not callable(make):
        raise SeatDown("no-builder")
    state = _state(module, env, seat)
    settings = WS.WakeSettings(seat, _defaults(module), state / "config.json")
    try:
        settings.load()
    except SystemExit as exc:  # its local file, named on stderr:  this seat only
        _log("seat %s settings refused:  %s" % (seat, exc))
        raise SeatDown("settings-refused") from None
    try:
        platform = make(env, settings)
    except Exception as exc:  # noqa: BLE001 - one seat's fault stays that seat's
        raise SeatDown("builder-error-%s" % type(exc).__name__) from None
    if not isinstance(platform, WR.Platform) or platform.seat != seat:
        raise SeatDown("builder-bad-platform")
    # No decider is configured:  a wake the rules cannot link goes to the most recent live chat.
    router = WR.Router(platform, env, settings, state, settings.routine_key, consult=None)
    return router, settings


def build(
    env: Mapping[str, str],
    builders: Mapping[str, Builder] | None = None,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> Served:
    """Every registered seat, each built on its own:  one that fails is reported down and the
    others are served."""
    routers: list[WR.Router] = []
    members: list[WS.WakeSettings] = []
    down: dict[str, str] = {}
    table = BUILDERS if builders is None else builders
    for seat in sorted(table):
        try:
            router, settings = build_seat(seat, table[seat], env, importer=importer)
        except SeatDown as exc:
            down[seat] = exc.reason
            _log("seat %s not served (%s)" % (seat, exc.reason))
            continue
        routers.append(router)
        members.append(settings)
    return Served(WR.Seats(routers, down), WS.SettingsGroup(members))


def main(argv: list[str]) -> int:
    """`simple-chat wake-router serve`."""
    from . import platform_letter as PL

    parser = argparse.ArgumentParser(prog="simple-chat wake-router")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--bind", default=DEFAULT_BIND)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    env = dict(PL.with_registry_env(dict(os.environ)))
    WS.preflight()
    served = build(env)
    group = served.settings
    previous = signal.signal(signal.SIGHUP, lambda _signum, _frame: group.kick())
    group.start_refresh()
    try:
        return WR.serve(served.seats, args.bind, args.port)
    finally:
        group.stop_refresh()
        signal.signal(signal.SIGHUP, previous)
