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
delivers for nobody.

`workers` is read when the router starts, so a new value needs a restart.  Every other setting
takes effect at the next refresh.

Python 3.11+, standard library only (fleet_knobs is too).
"""

from __future__ import annotations

import os
import select
import sys
import threading
from collections.abc import Iterator, Mapping
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


def _log(code: str) -> None:
    """Codes and knob names only:  never a value."""
    sys.stderr.write("wake-router: settings %s\n" % code)


def _fetched() -> bool:
    """Whether this process's knobs came from Infisical, and not from a copy or the defaults."""
    return knobs.report()["fetch"] == "ok"


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


class WakeSettings(Mapping[str, Any]):
    """A router's settings, as the mapping it reads, kept current in the background.  Each change
    swaps in a whole new dict, so a reader never sees half of one."""

    def __init__(
        self,
        seat: str,
        defaults: Mapping[str, Any],
        local_path: Path,
        *,
        required: bool = False,
    ) -> None:
        self.seat = seat
        self.defaults = defaults
        self.local_path = local_path
        self.required = required
        self.interval = 300.0
        self._confirmed = False
        self._base: dict[str, Any] = {**defaults, "delivery_enabled": False}
        self._values: dict[str, Any] = dict(self._base)
        self._refreshing = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._kick_write: int | None = None

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
        if not self._confirmed:
            _log("unconfirmed %s" % knobs.report().get("fetch_reason", ""))
        for knob in ignored:
            _log("local-ignored %s" % knob)

    def refresh(self) -> bool:
        """Apply the local file, read again, and answer whether the read took.  Only a good fetch
        replaces the values held:  a failed one would otherwise answer with an old copy or the
        defaults, and an outage would turn delivery off, or back on."""
        with self._refreshing:
            try:
                local = self._local()
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - staleness beats a dead thread
                _log("refresh-error %s" % type(exc).__name__)
                return False
            # First, and whatever the fetch does:  this is the brake that works in an outage.
            self._values = narrowed(self._base, local)[0]
            try:
                knobs.refresh()
                base = from_knobs(self.seat, self.defaults)
                interval = float(knobs.resolve(REFRESH_SECONDS).value)
                fetched = _fetched()
                reason = knobs.report().get("fetch_reason", "")
            except Exception as exc:  # noqa: BLE001 - staleness beats a dead thread
                _log("refresh-error %s" % type(exc).__name__)
                return False
            if not fetched:
                _log("refresh-kept-last-good %s" % reason)
                return False
            self._base = base
            self._values = narrowed(base, local)[0]
            self.interval = interval
            self._confirmed = True
            return True

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
