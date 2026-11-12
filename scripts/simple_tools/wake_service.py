"""The one wake router service for every platform (OPS-1048;  owner rulings, Nov 11, 2026:  "the
router is supposed to be for every platform", and one router and one letter assigner for all).

    simple-chat wake-router serve [--bind ADDR] [--port N]

One process, one port (8794 on loopback by default), one HTTP server.  Each seat gets its own
`wake_router.Router`, with its own state folder, pending queue, workers and receipts, and
`wake_router.Seats` routes `/wake/<SEAT>`, `/receipt/<SEAT>/<id>`, `/receipt/<id>` (CLAUDE's),
`/health` and `/health/<SEAT>` to it.  Every guarantee of a single router holds for each seat:
the durable 202, never running a turn twice, per-chat order, the pick chain, the registry recheck
before delivery, holding on unconfirmed settings, the local file that only narrows, SIGHUP and the
test wake.

The one platform table.  `BUILDERS` is the only table of platforms, on both hosts.  The server
builds each seat's router from it, and `simple-chat wake-deliver` on the Mac looks a request's
`platform` up in it (`by_platform`).  A module is named only by this table, never by a request.
Each entry is a `Builder`:

    platform    the registry's platform name ("claude", "codex", "muse", "zcode",
                "grok-leader"), which is also what a delivery request names
    module      the module, by its constant name
    function    its server builder, called as function(env, settings) -> wake_router.Platform
    handler     its one-delivery handler on the Mac, called as handler(request, env=env) -> dict
    session_re  the platform's session-id pattern, checked on both hosts before any of the
                module's code runs

The module names its seat as `SEAT`, which must equal the key.  Its Platform must have the same
`platform` and the same session pattern as the entry.  It may define `DEFAULTS` (its settings,
which include every key of `wake_router.ROUTER_DEFAULTS`;  ROUTER_DEFAULTS when it has none),
`state_dir(env)` (its state folder;  `<state root>/<seat in lower case>` when it has none) and
`NEVER_RAN` (a frozenset of its own refusal codes that can only come before anything ran;  the
router adds them to its own, so a receipt says `delivered: false` for them).  Registering a
platform is one line here, and nothing else in the router changes.

Which seats.  Every registered seat is served, and delivery stays gated per seat by
WAKE_ROUTER_DELIVERY_ENABLED and WAKE_ROUTER_DELIVERY_SEATS.  Serving only the seats that knob
names would make arming a seat need a restart, since the seat set is fixed at start while the
knob is refreshed, and a seat not yet armed would answer its wakes with 404 instead of a pick and
a `held` receipt, so its test wakes could not be checked before anyone arms it.  This way arming
or disarming a seat is a knob change and a refresh.

A seat that cannot be served is only that seat.  A module that is not installed yet (its own
import fails as not found) shows as `not-installed` on /health/<SEAT> and does not make /health
503:  seats are registered here before their modules land.  A module that is there but broken
(its import raises, its `SEAT` disagrees, its builder is missing, raises or answers something
that is not its Platform, the shared local file is refused, or its router cannot start) is down
with a reason token and does count against /health.  Its requests are answered 503 either way,
and every other seat is served as usual.

The settings.  Each seat has a `WakeSettings` of its own, all on one local narrowing file,
`<state root>/config.json`, and one `SettingsGroup` refreshes them all with one fetch of the
Infisical folder /wake-router, on a timer and on SIGHUP.  The routine key (WAKE_ROUTER_KEY) and
the registry's address and key (SIMPLE_TOOLS_OPERATOR_REGISTRY_URL and _KEY) come from
the same folder;  nothing is read from an env file.  The service's read token,
`afc-wake-router-ro`, is scoped to prod:/wake-router and is the only thing in its environment file
(server/afc-wake-router.service).  A process that may not read knobs at all, such as one in a
non-prod environment, stops at start, since that is true of every seat.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from . import wake_router as WR
from . import wake_settings as WS

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8794
UUID_SESSION = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
NEVER_RAN_CODE = re.compile(r"[a-z0-9][a-z0-9:_-]{0,79}")


@dataclass(frozen=True)
class Builder:
    """One platform:  where its server Platform and its Mac-side delivery come from (module
    docstring, The one platform table)."""

    platform: str
    module: str
    function: str = "router_platform"
    handler: str = "deliver_request"
    session_re: str = UUID_SESSION
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", re.compile(self.session_re))

    @property
    def session_pattern(self) -> re.Pattern[str]:
        return self._compiled


#: Every platform, keyed by seat:  the only platform table, on both hosts.
BUILDERS: dict[str, Builder] = {
    "CLAUDE": Builder("claude", "simple_tools.claude_wake", "make_platform", "_deliver_claude"),
    "CODEX": Builder("codex", "simple_tools.codex_wake"),
    "MC": Builder("muse", "simple_tools.muse_wake"),
    "ZC": Builder("zcode", "simple_tools.zcode_wake", session_re=r"^sess_[A-Za-z0-9._-]{1,123}$"),
    # A live Grok TUI session id is a UUID, so the pattern is UUID_SESSION.
    "GROK": Builder("grok-leader", "simple_tools.grok_wake", session_re=UUID_SESSION),
}


def by_platform(name: object) -> Builder | None:
    """The entry whose platform a delivery request names, or None.  Only a str that is exactly
    a platform name in the table matches;  a seat tag, a module name or anything else does not."""
    if not isinstance(name, str):
        return None
    return next((entry for entry in BUILDERS.values() if entry.platform == name), None)


class SeatDown(Exception):
    """A seat that cannot be served, with a short reason token.  `installed` is False only when
    its module is not on this host yet."""

    def __init__(self, reason: str, *, installed: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.installed = installed


@dataclass(frozen=True)
class Prepared:
    """A seat whose module loaded and checked out, before its settings are read."""

    seat: str
    builder: Builder
    make: Callable[..., Any]
    defaults: Mapping[str, Any]
    state: Path
    never_ran: frozenset[str]


@dataclass(frozen=True)
class Served:
    """What the service runs:  each seat's router and settings, and the seats it cannot serve."""

    seats: WR.Seats
    settings: WS.SettingsGroup


def _log(text: str) -> None:
    """Seats and reason tokens only."""
    sys.stderr.write("wake-router: %s\n" % text)


def state_root(env: Mapping[str, str]) -> Path:
    home = str(env.get("HOME") or os.path.expanduser("~"))
    return Path(home) / ".local" / "state" / "wake-router"


def default_state(env: Mapping[str, str], seat: str) -> Path:
    return state_root(env) / seat.lower()


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


def _never_ran(module: ModuleType) -> frozenset[str]:
    codes = getattr(module, "NEVER_RAN", frozenset())
    if not isinstance(codes, (set, frozenset)) or not all(
        isinstance(code, str) and NEVER_RAN_CODE.fullmatch(code) for code in codes
    ):
        raise SeatDown("bad-never-ran")
    return frozenset(codes)


def prepare(
    seat: str,
    builder: Builder,
    env: Mapping[str, str],
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> Prepared:
    """Load one seat's module and check what it exports, or SeatDown with why."""
    if not WR.SEAT_RE.fullmatch(seat):
        raise SeatDown("bad-seat")
    try:
        module = importer(builder.module)
    except ModuleNotFoundError as exc:
        if exc.name == builder.module:  # the module itself is not there yet
            raise SeatDown(WR.NOT_INSTALLED, installed=False) from None
        raise SeatDown("import-error-ModuleNotFoundError") from None
    except Exception as exc:  # noqa: BLE001 - a module that fails to import is that seat's fault
        raise SeatDown("import-error-%s" % type(exc).__name__) from None
    if getattr(module, "SEAT", None) != seat:
        raise SeatDown("seat-mismatch")
    make = getattr(module, builder.function, None)
    if not callable(make):
        raise SeatDown("no-builder")
    return Prepared(
        seat, builder, make, _defaults(module), _state(module, env, seat), _never_ran(module)
    )


def build_seat(
    ready: Prepared,
    env: Mapping[str, str],
    local_path: Path,
    file_defaults: Mapping[str, Any],
) -> tuple[WR.Router, WS.WakeSettings]:
    """One seat's router and settings, or SeatDown with why."""
    seat, builder = ready.seat, ready.builder
    settings = WS.WakeSettings(seat, ready.defaults, local_path, file_defaults=file_defaults)
    try:
        settings.load()
    except SystemExit as exc:  # the local file, named on stderr
        _log("seat %s settings refused:  %s" % (seat, exc))
        raise SeatDown("settings-refused") from None
    try:
        platform = ready.make(env, settings)
    except Exception as exc:  # noqa: BLE001 - one seat's fault stays that seat's
        raise SeatDown("builder-error-%s" % type(exc).__name__) from None
    if not isinstance(platform, WR.Platform) or platform.seat != seat:
        raise SeatDown("builder-bad-platform")
    if platform.platform != builder.platform:
        raise SeatDown("platform-name-mismatch")
    if platform.target_re.pattern != builder.session_re:
        raise SeatDown("session-pattern-mismatch")
    # No decider is configured:  a wake the rules cannot link goes to the most recent live chat.
    router = WR.Router(
        platform,
        settings.registry_env(env),
        settings,
        ready.state,
        settings.routine_key,
        consult=None,
        never_ran=ready.never_ran,
    )
    return router, settings


def build(
    env: Mapping[str, str],
    builders: Mapping[str, Builder] | None = None,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> Served:
    """Every registered seat, each built on its own:  one that fails is reported down, one not
    installed yet is reported as such, and the others are served.  Every module loads first, so
    the one local file can be checked against every seat's settings at once."""
    table = BUILDERS if builders is None else builders
    down: dict[str, str] = {}
    absent: list[str] = []
    ready: list[Prepared] = []
    for seat in sorted(table):
        try:
            ready.append(prepare(seat, table[seat], env, importer=importer))
        except SeatDown as exc:
            if exc.installed:
                down[seat] = exc.reason
            else:
                absent.append(seat)
            _log("seat %s not served (%s)" % (seat, exc.reason))
    file_defaults: dict[str, Any] = {}
    for seat_ready in ready:
        file_defaults.update(seat_ready.defaults)
    local_path = state_root(env) / "config.json"
    routers: list[WR.Router] = []
    members: list[WS.WakeSettings] = []
    for seat_ready in ready:
        try:
            router, settings = build_seat(seat_ready, env, local_path, file_defaults)
        except SeatDown as exc:
            down[seat_ready.seat] = exc.reason
            _log("seat %s not served (%s)" % (seat_ready.seat, exc.reason))
            continue
        routers.append(router)
        members.append(settings)
    return Served(WR.Seats(routers, down, absent), WS.SettingsGroup(members))


def main(argv: list[str]) -> int:
    """`simple-chat wake-router serve`."""
    parser = argparse.ArgumentParser(prog="simple-chat wake-router")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--bind", default=DEFAULT_BIND)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    # The registry's address and key come from the knobs (WakeSettings.registry_env), never from
    # this environment or an env file.
    env = dict(os.environ)
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
