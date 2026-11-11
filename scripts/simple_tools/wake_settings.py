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

One local file, and it only narrows.  The service reads one file, `<state root>/config.json`
(wake_service), and its top-level keys narrow every seat:  it may lower a cap or turn delivery
off, and whatever would widen is ignored and named on stderr.  `delivery_seats`, a list of seat
tags, can only remove seats:  a seat it leaves out does not deliver, and naming a seat the knob
does not name adds nothing.  A key no seat knows, or a value of the wrong type, refuses the file,
as before.  It is applied to the held values on every refresh before the fetch, so it works as a
brake during an outage too, and taking it away restores them.  `local_host` and `claude_bin` are
not settings but facts about one host, which one Infisical value cannot hold, so they are still
read from the file, by the seats that have them.

What is not per-seat.  The knob registry is flat, with one name per value, so a seat is not a
dimension of it.  The seats are a list of their own:  delivery runs for a seat only when
WAKE_ROUTER_DELIVERY_ENABLED is true AND WAKE_ROUTER_DELIVERY_SEATS names it, and an empty list
delivers for nobody.  The numbers (workers, the delivery cap, retries) are the same knob for every
seat, and each seat's router applies them to itself:  four workers is four for each seat.

`workers` is read when the router starts, so a new value needs a restart.  Every other setting
takes effect at the next refresh.

Every seat in one process.  The service (`wake_service`) holds one `WakeSettings` per seat, all on
the same local file, and one `SettingsGroup` refreshes them together:  one thread, one SIGHUP, and
one fetch of the folder per refresh, because `fleet_knobs` keeps one cache for the whole process
and a refresh of it per seat would race.

One folder, one token.  Every knob here, and nothing else, is in the Infisical folder
/wake-router, so `fleet_knobs` fetches that folder alone and the router's read token is scoped to
prod:/wake-router only (a token scoped to "/" would reach every folder under it).  "A fetch that
worked" above means a fetch of that folder.  The service's token is `afc-wake-router-ro`.

One routine key.  The bearer the listener sends for every seat is the one secret knob
WAKE_ROUTER_KEY in the same folder.  It is read at load and again on every refresh, and held in
memory only:  `fleet_knobs` never writes a secret anywhere and fails it closed.  Only a refresh
whose fetch worked may change it, the same last-known-good rule as the values (AGENTS.md, the
service contract).  That fetch takes whatever it gives, so an unset or malformed value drops the
key and a revocation takes effect at the next refresh.  A failed fetch or an error keeps the key
already held in memory and logs the failure, so an Infisical outage does not turn the seats away.
With no key at all (none read since start, or one revoked), every seat refuses its requests with
503 and says why on /health until a refresh reads one.  There is no key file.

The registry's address and key.  The router reads the Operator's registry over its API
(registry_api), and the address and signing key come from this folder too:  the setting
WAKE_ROUTER_REGISTRY_URL and the secret WAKE_ROUTER_REGISTRY_KEY, held by the same rule as the
routine key.  `RegistryEnv` hands them to registry_api under the names it reads
(SIMPLE_TOOLS_OPERATOR_REGISTRY_URL and _KEY) and drops any registry address or key the process
inherited, so no env file and no ssh remote is ever a fallback.  With no address or no key, every
registry call answers `not_configured`:  a pick is a bounded retry, a delivery's recheck is
`failed` with the code `registry-not-configured`, and /health says so.

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

from . import registry_api
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
# The local file's one key that is not a setting:  seat tags, and it can only remove seats.
LOCAL_SEATS = "delivery_seats"
JOIN_SECONDS = 10  # longer than one fleet_knobs fetch can take
# The one routine key every seat's path is checked against.
KEY_KNOB = "WAKE_ROUTER_KEY"
# Where the registry is and the key its calls are signed with.
REGISTRY_URL = "WAKE_ROUTER_REGISTRY_URL"
REGISTRY_KEY = "WAKE_ROUTER_REGISTRY_KEY"
# The names registry_api reads the address and key under, which no inherited value may supply.
REGISTRY_NAMES = (registry_api.URL_ENV, registry_api.KEY_ENV, registry_api.KEY_REF_ENV)
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


def read_secret(name: str, minimum: int = 1) -> tuple[str, str]:
    """(value, "") from a secret knob, or ("", reason token).  Never raises and never names the
    value."""
    try:
        value = knobs.resolve(name).value
    except knobs.SecretUnavailable as exc:
        return "", exc.reason
    except Exception as exc:  # noqa: BLE001 - no value is the answer, never a crash
        return "", "error-%s" % type(exc).__name__
    if not isinstance(value, str) or len(value) < minimum:
        return "", "short"
    return value, ""


def read_key() -> tuple[str, str]:
    """(key, "") from WAKE_ROUTER_KEY, or ("", reason token)."""
    return read_secret(KEY_KNOB, WR.KEY_MIN_LENGTH)


def read_registry() -> tuple[str, str, str]:
    """(address, key, "") for the registry, or with "" where one is missing and a reason token."""
    try:
        url = str(knobs.resolve(REGISTRY_URL).value or "").strip()
    except Exception as exc:  # noqa: BLE001 - no address is the answer, never a crash
        return "", "", "url-error-%s" % type(exc).__name__
    key, why = read_secret(REGISTRY_KEY)
    if not url:
        return "", key, "url-unset"
    return url, key, ("key-" + why) if why else ""


def registry_overlay(env: Mapping[str, str], url: str, key: str) -> dict[str, str]:
    """`env` with the registry's address and key under registry_api's names, and with any
    address or key it carried dropped, so nothing else is ever a fallback."""
    out = {name: value for name, value in env.items() if name not in REGISTRY_NAMES}
    if url:
        out[registry_api.URL_ENV] = url
    if key:
        out[registry_api.KEY_ENV] = key
    return out


def registry_env_once(env: Mapping[str, str]) -> dict[str, str]:
    """The registry env for one short-lived process, as `wake-deliver` on the Mac is.  A process
    that may not read the knobs, or cannot reach them, gets no address and no key, so its
    registry calls answer `not_configured`."""
    try:
        url, key, _why = read_registry()
    except Exception:  # noqa: BLE001 - not configured, never a crash
        url, key = "", ""
    return registry_overlay(env, url, key)


class RegistryEnv(Mapping[str, str]):
    """A router's env:  `base`, and the registry's address and key as its settings hold them now,
    read at every lookup so a refresh reaches the next registry call."""

    def __init__(self, base: Mapping[str, str], settings: WakeSettings) -> None:
        self._base = dict(base)
        self._settings = settings

    def _view(self) -> dict[str, str]:
        url, key = self._settings.registry()
        return registry_overlay(self._base, url, key)

    def __getitem__(self, name: str) -> str:
        return self._view()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._view())

    def __len__(self) -> int:
        return len(self._view())


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


def narrowed(
    base: Mapping[str, Any], local: Mapping[str, Any], seat: str = ""
) -> tuple[dict[str, Any], list[str]]:
    """`base` with a local file's values applied, and the knobs whose local value was ignored.  No
    fetch is involved, so this can run on values already held when Infisical cannot be reached.
    A `delivery_seats` that leaves this seat out turns its delivery off;  it never turns one on."""
    values = dict(base)
    values.update({name: local[name] for name in HOST_FACTS if name in local and name in base})
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
    if LOCAL_SEATS in local and "delivery_enabled" in values and seat not in local[LOCAL_SEATS]:
        values["delivery_enabled"] = False
    ignored += [
        knob
        for name, knob in NOT_NARROWABLE.items()
        if name in local and local[name] != base.get(name)
    ]
    return values, ignored


def read_local(
    path: Path, defaults: Mapping[str, Any], *, required: bool = False
) -> dict[str, Any]:
    """The local file, checked against `defaults` and the seat list (wake_router.read_local_config,
    with its refusals)."""
    return WR.read_local_config(path, defaults, required=required, seat_lists=(LOCAL_SEATS,))


def read_once(seat: str, defaults: Mapping[str, Any], local_path: Path) -> dict[str, Any]:
    """The settings for one short-lived process, as `wake-deliver` on the Mac is."""
    local = read_local(local_path, defaults)
    return narrowed(from_knobs(seat, defaults), local, seat)[0]


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
        file_defaults: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.seat = seat
        self.defaults = defaults
        self.local_path = local_path
        self.required = required
        # What the local file may name:  every seat's settings when the file is shared.
        self.file_defaults = defaults if file_defaults is None else file_defaults
        self.interval = 300.0
        self._confirmed = False
        self._base: dict[str, Any] = {**defaults, "delivery_enabled": False}
        self._values: dict[str, Any] = dict(self._base)
        # (key, why) and (address, key), each swapped whole, so a request never sees half of one.
        self._key: tuple[str, str] = ("", "not-read")
        self._registry: tuple[str, str] = ("", "")

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

    def registry(self) -> tuple[str, str]:
        """The registry's (address, key) as the last good read gave them;  "" where missing."""
        return self._registry

    def registry_env(self, base: Mapping[str, str]) -> RegistryEnv:
        """`base` as the router's env:  the registry's address and key from these settings."""
        return RegistryEnv(base, self)

    def _take_key(self) -> None:
        """Read the routine key and the registry's address and key again from what `fleet_knobs`
        fetched, and drop whichever fails.  Only `load` and a refresh whose fetch worked call
        this."""
        self._key = read_key()
        if not self._key[0]:
            _log("key-unavailable %s seat=%s" % (self._key[1], self.seat))
        url, key, why = read_registry()
        self._registry = (url, key)
        if why:
            _log("registry-not-configured %s seat=%s" % (why, self.seat))

    def _keep_key(self, why: str) -> None:
        """A refresh that did not fetch leaves the routine key and the registry's as they were.
        Names the seat and whether a key is held, never a key."""
        held = "held" if self._key[0] else "none"
        _log("key-kept %s seat=%s key=%s" % (why, self.seat, held))

    def _local(self) -> dict[str, Any]:
        return read_local(self.local_path, self.file_defaults, required=self.required)

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
        self._values, ignored = narrowed(base, local, self.seat)
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
        self._values = narrowed(self._base, local, self.seat)[0]
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
        self._values = narrowed(base, local, self.seat)[0]
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
