"""Where a wake router's settings come from (OPS-1048).

Infisical is the sole source of a tunable setting, so these are read through `fleet_knobs`, and each
one is a registered knob named below.  `fleet_knobs` was written for jobs that live a few seconds;
the router lives for weeks, so `WakeSettings` holds it to the service contract in AGENTS.md (§
Infisical sole source of truth), the one `fleet_rag/infisical_settings.py` implements for the recall
service:

  * Loaded at startup, before the port is bound.
  * Never fetched per wake:  a read of `WakeSettings` touches memory only.
  * Refreshed in the background (a daemon thread every WAKE_ROUTER_SETTINGS_REFRESH_SECONDS, and
    SIGHUP at once), and a refresh that fails keeps what the router was already running on.

`infisical_settings` itself does not fit, for three reasons.  Its environment backfill would let a
stray variable arm delivery, which is exactly what this may not do.  It has no notion of a local
file only narrowing.  And it is in `fleet_rag`, which the runtime bundle ships only in part, where
`fleet_knobs` already ships and is the registry `INFISICAL.md` is checked against.  There is no
write-through because the router has no admin save:  a value is changed with `fleet_knobs set`.

Delivery needs a read that worked.  `fleet_knobs` answers from its last-known-good copy when it
cannot fetch, and that copy may predate the owner turning delivery off.  So delivery is on only after
a successful fetch in this process, whatever the copy says.  Until the first one `confirmed` is
false, and the router leaves wakes pending rather than closing them as held (wake_router, Settings
not yet confirmed).  After it, a failed refresh keeps the values that fetch gave.

A local file only narrows.  `<state>/config.json` may lower a cap or turn delivery off, and whatever
would widen is ignored and named on stderr.  It is applied to the held values on every refresh
before the fetch, so it works as a brake during an outage too, and taking it away restores them.
`local_host` and `claude_bin` are not settings but facts about one host, which one Infisical value
cannot hold, so they are still read from the file.

What is not per-seat.  The knob registry is flat, with one name per value, so a seat is not a
dimension of it.  The seats are a list of their own:  delivery runs for a seat only when
WAKE_ROUTER_DELIVERY_ENABLED is true AND WAKE_ROUTER_DELIVERY_SEATS names it, and an empty list
delivers for nobody.  The numbers (workers, the delivery cap, retries) are the same knob for every
seat, and each seat's router applies them to itself:  four workers is four for each seat.

`workers` is read when the router starts, so a new value needs a restart.  Every other setting
takes effect at the next refresh.

Every seat in one process.  The service (`wake_service`) holds one `WakeSettings` per seat, each
with its own local file, and one `SettingsGroup` refreshes them together:  one thread, one SIGHUP,
and one fetch of the folder per refresh, because `fleet_knobs` keeps one cache for the whole
process and a refresh of it per seat would race.  One seat's broken local file holds that seat's
values and nobody else's.

One folder, one token.  Every knob here, and nothing else, is in the Infisical folder
/wake-router, so `fleet_knobs` fetches that folder alone and the router's read token is scoped to
prod:/wake-router only (a token scoped to "/" would reach every folder under it).  "A fetch that
worked" above means a fetch of that folder.  The service's token is `afc-wake-router-ro`.

The routine keys, one per seat.  The bearer the listener sends for a seat is that seat's secret
knob WAKE_ROUTER_<SEAT>_KEY in the same folder (KEY_KNOBS), the same value as the listener's
<SEAT>_ROUTINE_KEY, and never one key shared across seats.  Each is read at load and again on
every refresh, and held in memory only:  `fleet_knobs` never writes a secret anywhere and fails
it closed.  Only a refresh whose fetch worked may change a key, the same last-known-good rule as
the values (AGENTS.md, the service contract).  That fetch takes whatever it gives, so an unset or
malformed value drops the key and a revocation takes effect at the next refresh.  A failed fetch
or an error keeps the key already held in memory and logs the failure, so an Infisical outage
does not turn every seat away.  With no key at all (none read since start, or one revoked), that
seat refuses its requests with 503 and says why on /health until a refresh reads one;  every
other seat goes on with its own.  There is no key file.

Python 3.11+, standard library only (fleet_knobs is too).
"""

from __future__ import annotations

import os
import select
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from fleet_knobs import knobs
from fleet_knobs import values as knob_values
from fleet_knobs.registry import BY_NAME

from . import wake_router as WR

DELIVERY_ENABLED = "WAKE_ROUTER_DELIVERY_ENABLED"
DELIVERY_SEATS = "WAKE_ROUTER_DELIVERY_SEATS"
RETRY_BACKOFF = "WAKE_ROUTER_RETRY_BACKOFF"
MAC_SSH = "WAKE_ROUTER_MAC_SSH"
REFRESH_SECONDS = "WAKE_ROUTER_SETTINGS_REFRESH_SECONDS"
INT_KNOBS = {
    "workers": "WAKE_ROUTER_WORKERS",
    "max_deliveries": "WAKE_ROUTER_MAX_DELIVERIES",
    "max_attempts": "WAKE_ROUTER_MAX_ATTEMPTS",
    "busy_retry_seconds": "WAKE_ROUTER_BUSY_RETRY_SECONDS",
    "unreachable_retry_seconds": "WAKE_ROUTER_UNREACHABLE_RETRY_SECONDS",
    "max_age_seconds": "WAKE_ROUTER_MAX_AGE_SECONDS",
    "shutdown_wait_seconds": "WAKE_ROUTER_SHUTDOWN_WAIT_SECONDS",
    "resume_timeout_seconds": "WAKE_ROUTER_RESUME_TIMEOUT_SECONDS",
    "confirm_seconds": "WAKE_ROUTER_CONFIRM_SECONDS",
}
NARROWABLE = {"delivery_enabled": DELIVERY_ENABLED, **INT_KNOBS}
NOT_NARROWABLE = {"retry_backoff": RETRY_BACKOFF, "mac_ssh": MAC_SSH}
HOST_FACTS = ("local_host", "claude_bin")
JOIN_SECONDS = 10  # longer than one fleet_knobs fetch can take
# Each seat's routine key, a secret knob in the router's folder, one per seat and never shared.  A
# seat not named has none, so its router refuses every request.  A platform joining the service
# adds its seat here and its knob to fleet_knobs/registry.py.
KEY_KNOBS = {
    "CLAUDE": "WAKE_ROUTER_CLAUDE_KEY",
    "CODEX": "WAKE_ROUTER_CODEX_KEY",
    "MC": "WAKE_ROUTER_MC_KEY",
    "ZC": "WAKE_ROUTER_ZC_KEY",
}
# The one folder every setting above lives in;  a test keeps them, and the keys, all in it.
FOLDER = BY_NAME[DELIVERY_ENABLED].folder


def _log(code: str) -> None:
    """Codes and knob names only:  never a value."""
    sys.stderr.write("wake-router: settings %s\n" % code)


def _fetched() -> bool:
    """Whether this process's fetch of the router's folder worked, so its knobs came from
    Infisical and not from a copy or the defaults."""
    return knobs.folder_status(FOLDER)["fetch"] == "ok"


def _fetch_reason() -> str:
    return knobs.folder_status(FOLDER).get("fetch_reason", "")


def read_key(seat: str) -> tuple[str, str]:
    """(key, "") from the seat's secret knob, or ("", reason token).  Never raises and never
    names the value."""
    name = KEY_KNOBS.get(seat)
    if name is None:
        return "", "no-key-knob"
    try:
        key = knobs.resolve(name).value
    except knobs.SecretUnavailable as exc:
        return "", exc.reason
    except Exception as exc:  # noqa: BLE001 - no key is the answer, never a crash
        return "", "error-%s" % type(exc).__name__
    if not isinstance(key, str) or len(key) < WR.KEY_MIN_LENGTH:
        return "", "short"
    return key, ""


def from_knobs(seat: str, defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Every setting in `defaults` from what `fleet_knobs` holds in this process, before any local
    file.  Delivery is on only if that came from a fetch that worked."""
    values = dict(defaults)
    enabled = knobs.resolve(DELIVERY_ENABLED).value
    seats = knobs.resolve(DELIVERY_SEATS).value
    values["delivery_enabled"] = _fetched() and enabled and seat in seats
    for name, knob in INT_KNOBS.items():
        if name in defaults:
            values[name] = knobs.resolve(knob).value
    if "retry_backoff" in defaults:
        text = knobs.resolve(RETRY_BACKOFF).value
        values["retry_backoff"] = [int(part) for part in text.split(",")]
    if "mac_ssh" in defaults:
        values["mac_ssh"] = knobs.resolve(MAC_SSH).value or ""
    return values


def narrowed(base: Mapping[str, Any], local: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """`base` with a local file's values applied, and the knobs whose local value was ignored.  No
    fetch is involved, so this can run on values already held when Infisical cannot be reached."""
    values = dict(base)
    values.update({name: local[name] for name in HOST_FACTS if name in local})
    ignored = []
    for name, knob in NARROWABLE.items():
        if name not in base or name not in local:
            continue
        spec = BY_NAME[knob]
        valid, typed = knob_values.lenient(spec, local[name])
        if not valid:
            ignored.append(knob)
            continue
        values[name], _narrowed, widening = knob_values.narrow(spec, base[name], typed)
        if widening:
            ignored.append(knob)
    ignored += [
        knob
        for name, knob in NOT_NARROWABLE.items()
        if name in local and local[name] != base.get(name)
    ]
    return values, ignored


def read_once(seat: str, defaults: Mapping[str, Any], local_path: Path) -> dict[str, Any]:
    """The settings for one short-lived process, as `wake-deliver` on the Mac is."""
    local = WR.read_local_config(local_path, defaults)
    return narrowed(from_knobs(seat, defaults), local)[0]


def preflight() -> None:
    """Stop the service when this process may not read the knobs at all, such as in a non-prod
    environment.  That is true of every seat at once, so it is not one seat's fault to report."""
    try:
        knobs.resolve(REFRESH_SECONDS)
    except knobs.KnobError as exc:
        raise SystemExit("wake-router: %s" % exc) from None


class _Refresher:
    """The background refresh:  a daemon thread that calls `refresh` every `interval` seconds, and
    `kick`, safe in a signal handler, for one now."""

    interval: float = 300.0

    def __init__(self) -> None:
        self._refreshing = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._kick_write: int | None = None

    def refresh(self) -> bool:
        raise NotImplementedError

    def kick(self) -> None:
        """Refresh now.  This runs in a signal handler, where taking a lock can deadlock against the
        thread it interrupted, so all it does is write one byte to a pipe."""
        descriptor = self._kick_write
        if descriptor is None:
            return
        try:
            os.write(descriptor, b"\0")
        except OSError:
            pass  # the pipe is full, so a refresh is already coming, or it has just closed

    def start_refresh(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        os.set_blocking(write_end, False)

        def loop() -> None:
            while not self._stop.is_set():
                ready, _, _ = select.select([read_end], [], [], self.interval)
                if ready:
                    os.read(read_end, 4096)
                if not self._stop.is_set():
                    self.refresh()

        self._read_end, self._kick_write = read_end, write_end
        self._thread = threading.Thread(target=loop, name="wake-settings-refresh", daemon=True)
        self._thread.start()

    def stop_refresh(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self.kick()
        self._thread.join(timeout=JOIN_SECONDS)
        finished = not self._thread.is_alive()
        self._thread = None
        write_end, self._kick_write = self._kick_write, None
        os.close(write_end)
        if finished:  # a thread still in a refresh goes on to read this, so it keeps it
            os.close(self._read_end)


class WakeSettings(_Refresher, Mapping[str, Any]):
    """One seat's settings, as the mapping its router reads, kept current in the background.  Each
    change swaps in a whole new dict, so a reader never sees half of one."""

    def __init__(
        self,
        seat: str,
        defaults: Mapping[str, Any],
        local_path: Path,
        *,
        required: bool = False,
    ) -> None:
        super().__init__()
        self.seat = seat
        self.defaults = defaults
        self.local_path = local_path
        self.required = required
        self.interval = 300.0
        self._confirmed = False
        self._base: dict[str, Any] = {**defaults, "delivery_enabled": False}
        self._values: dict[str, Any] = dict(self._base)
        # (key, why), swapped whole like the values, so a request never sees half of one.
        self._key: tuple[str, str] = ("", "not-read")

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    @property
    def confirmed(self) -> bool:
        """Whether a fetch has worked in this process.  Until one has, nobody knows whether delivery
        should be on, which is not the same as knowing it is off."""
        return self._confirmed

    def routine_key(self) -> tuple[str, str]:
        """The router's `KeySource`:  the key the last read gave, or "" and why.  Memory only."""
        return self._key

    def _take_key(self) -> None:
        """Read the key again from what `fleet_knobs` fetched, and drop it if that fails.  Only
        `load` and a refresh whose fetch worked call this."""
        self._key = read_key(self.seat)
        if not self._key[0]:
            _log("key-unavailable %s seat=%s" % (self._key[1], self.seat))

    def _keep_key(self, why: str) -> None:
        """A refresh that did not fetch leaves the key as it was.  Names the seat and whether it
        holds a key, never the key."""
        held = "held" if self._key[0] else "none"
        _log("key-kept %s seat=%s key=%s" % (why, self.seat, held))

    def _local(self) -> dict[str, Any]:
        return WR.read_local_config(self.local_path, self.defaults, required=self.required)

    def load(self) -> None:
        """Read at startup.  A bad local file or a refused environment stops the router here."""
        try:
            local = self._local()
            base = from_knobs(self.seat, self.defaults)
            self.interval = float(knobs.resolve(REFRESH_SECONDS).value)
        except knobs.KnobError as exc:
            raise SystemExit("wake-router: %s" % exc) from None
        self._confirmed = _fetched()
        self._base = base
        self._values, ignored = narrowed(base, local)
        self._take_key()
        if not self._confirmed:
            _log("unconfirmed %s" % _fetch_reason())
        for knob in ignored:
            _log("local-ignored %s seat=%s" % (knob, self.seat))

    def _brake(self) -> dict[str, Any] | None:
        """Apply the local file to the values held, before any fetch and whatever it does:  this is
        the brake that works in an outage.  None when the file cannot be read, and then nothing
        held changes."""
        try:
            local = self._local()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - staleness beats a dead thread
            _log("refresh-error %s seat=%s" % (type(exc).__name__, self.seat))
            return None
        self._values = narrowed(self._base, local)[0]
        return local

    def _apply(self, local: Mapping[str, Any], fetched: bool, interval: float) -> bool:
        """Take what the shared fetch gave.  Only a good fetch replaces the values held:  a failed
        one would otherwise answer with an old copy or the defaults, and an outage would turn
        delivery off, or back on.  The routine key follows the same rule:  a good fetch takes
        whatever it gives, an empty key included, so a revocation takes effect, and a failed
        fetch or an error keeps the key held in memory."""
        if not fetched:
            self._keep_key("refresh-failed")
            return False
        try:
            base = from_knobs(self.seat, self.defaults)
        except Exception as exc:  # noqa: BLE001 - staleness beats a dead thread
            _log("refresh-error %s seat=%s" % (type(exc).__name__, self.seat))
            self._keep_key("refresh-error")
            return False
        self._take_key()
        self._base = base
        self._values = narrowed(base, local)[0]
        self.interval = interval
        self._confirmed = True
        return True

    def refresh(self) -> bool:
        """Apply the local file, read again, and answer whether the read took (refresh_together,
        for this seat alone)."""
        with self._refreshing:
            return refresh_together([self])


def refresh_together(members: Sequence[WakeSettings]) -> bool:
    """Refresh these seats' settings with one fetch of the folder, and answer whether every one of
    them took it.  Each seat's local brake goes first.  A seat whose local file cannot be read
    keeps what it held and is left out, and only that seat;  the fetch happens once for the rest,
    because `fleet_knobs` keeps one cache per process."""
    held = [(member, local) for member in members if (local := member._brake()) is not None]
    if not held:
        return False
    try:
        knobs.refresh()
        interval = float(knobs.resolve(REFRESH_SECONDS).value)
        fetched = _fetched()
        reason = _fetch_reason()
    except Exception as exc:  # noqa: BLE001 - staleness beats a dead thread
        _log("refresh-error %s" % type(exc).__name__)
        for member, _local in held:
            member._keep_key("refresh-error")  # no fetch worked, so nothing held changes
        return False
    if not fetched:
        _log("refresh-kept-last-good %s" % reason)
    took = [member._apply(local, fetched, interval) for member, local in held]
    return len(held) == len(members) and all(took)


class SettingsGroup(_Refresher):
    """Every seat's settings in one process, refreshed together:  one thread, one SIGHUP and one
    fetch per refresh (module docstring, Every seat in one process)."""

    def __init__(self, members: Sequence[WakeSettings]) -> None:
        super().__init__()
        self.members = list(members)

    @property
    def interval(self) -> float:  # every member reads the same knob
        return min((member.interval for member in self.members), default=300.0)

    def refresh(self) -> bool:
        with self._refreshing:
            return refresh_together(self.members)
