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

A local file only narrows.  `<state>/config.json` may lower a cap or turn delivery off, and whatever
would widen is ignored and named on stderr.  `local_host` and `claude_bin` are not settings but facts
about one host, which one Infisical value cannot hold, so they are still read from the file.

What is not per-seat.  The knob registry is flat, with one name per value, so a seat is not a
dimension of it.  The seats are a list of their own:  delivery runs for a seat only when
WAKE_ROUTER_DELIVERY_ENABLED is true AND WAKE_ROUTER_DELIVERY_SEATS names it, and an empty list
delivers for nobody.

`workers` is read when the router starts, so a new value needs a restart.  Every other setting
takes effect at the next refresh.  With no reader token, the router runs on the last-known-good
copy or else the defaults, and those have delivery off.

Python 3.11+, standard library only (fleet_knobs is too).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from fleet_knobs import knobs

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
HOST_FACTS = ("local_host", "claude_bin")
JOIN_SECONDS = 10  # longer than one fleet_knobs fetch can take


def _log(code: str) -> None:
    """Codes and knob names only:  never a value."""
    sys.stderr.write("wake-router: settings %s\n" % code)


def read(seat: str, defaults: Mapping[str, Any], local: Mapping[str, Any]) -> dict[str, Any]:
    """Every setting in `defaults`, from what `fleet_knobs` holds in this process, with `local` (only
    what the file sets) narrowing them."""
    values = dict(defaults)
    values.update({name: local[name] for name in HOST_FACTS if name in local})
    enabled = knobs.resolve(DELIVERY_ENABLED, local=local.get("delivery_enabled")).value
    values["delivery_enabled"] = enabled and seat in knobs.resolve(DELIVERY_SEATS).value
    for name, knob in INT_KNOBS.items():
        if name in defaults:
            values[name] = knobs.resolve(knob, local=local.get(name)).value
    if "retry_backoff" in defaults:
        wanted = local.get("retry_backoff")
        narrowing = None if wanted is None else ",".join(str(number) for number in wanted)
        text = knobs.resolve(RETRY_BACKOFF, local=narrowing).value
        values["retry_backoff"] = [int(part) for part in text.split(",")]
    if "mac_ssh" in defaults:
        values["mac_ssh"] = knobs.resolve(MAC_SSH, local=local.get("mac_ssh") or None).value or ""
    return values


def read_once(seat: str, defaults: Mapping[str, Any], local_path: Path) -> dict[str, Any]:
    """The settings for one short-lived process, as `wake-deliver` on the Mac is."""
    return read(seat, defaults, WR.read_local_config(local_path, defaults))


class WakeSettings(Mapping[str, Any]):
    """A router's settings, as the mapping it reads, kept current in the background.  Each refresh
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
        self._values: dict[str, Any] = {**defaults, "delivery_enabled": False}
        self._refreshing = threading.Lock()
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def _local(self) -> dict[str, Any]:
        return WR.read_local_config(self.local_path, self.defaults, required=self.required)

    def load(self) -> None:
        """Read at startup.  A bad local file or a refused environment stops the router here."""
        try:
            self._values = read(self.seat, self.defaults, self._local())
            self.interval = float(knobs.resolve(REFRESH_SECONDS).value)
        except knobs.KnobError as exc:
            raise SystemExit("wake-router: %s" % exc) from None
        report = knobs.report()
        if report["fetch"] != "ok":
            _log("stale %s" % report.get("fetch_reason", ""))
        for name, answer in report["knobs"].items():
            if answer.get("local_ignored"):
                _log("local-ignored %s" % name)

    def refresh(self) -> bool:
        """Read again, and answer whether that took.  Only a good fetch replaces what is held:  a
        failed one would otherwise answer with an old copy or the defaults, and an outage would turn
        delivery off, or back on."""
        with self._refreshing:
            try:
                local = self._local()
                knobs.refresh()
                values = read(self.seat, self.defaults, local)
                interval = float(knobs.resolve(REFRESH_SECONDS).value)
                report = knobs.report()
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - staleness beats a dead thread
                _log("refresh-error %s" % type(exc).__name__)
                return False
            if report["fetch"] != "ok":
                _log("refresh-kept-last-good %s" % report.get("fetch_reason", ""))
                return False
            self._values = values
            self.interval = interval
            return True

    def kick(self) -> None:
        """Refresh now.  Safe to call from a signal handler."""
        self._kick.set()

    def start_refresh(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self._kick.wait(self.interval)
                self._kick.clear()
                if not self._stop.is_set():
                    self.refresh()

        self._thread = threading.Thread(target=loop, name="wake-settings-refresh", daemon=True)
        self._thread.start()

    def stop_refresh(self) -> None:
        self._stop.set()
        self._kick.set()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_SECONDS)
            self._thread = None
