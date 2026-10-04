#!/usr/bin/env python3
"""Infisical sole-source-of-truth settings for the fleet's Python services.

Implements the fleet-wide canonical pattern (see repo-root INFISICAL.md):

1. **Load at startup.**  ``init_settings()`` fetches the full managed settings set from
   the app's Infisical project (universal-auth login -> access token ->
   GET /api/v3/secrets/raw) into an in-memory cache.  When no Infisical identity is
   configured (local dev without the machine identity), the managed keys are populated
   from the process environment instead and a warning is logged -- the service keeps
   its old env-based behavior.  Missing *required* keys fail fast with a SettingsError
   naming the key and pointing at INFISICAL.md.
2. **Never fetch per-request.**  ``get()`` reads memory only.  All runtime reads come
   from the cache; no per-request (or per-tick, per-event) Infisical call exists.
3. **Background refresh.**  ``start_refresh()`` runs a daemon thread that re-fetches on
   an interval (default 300 s, tunable via the ``SETTINGS_REFRESH_SECONDS`` key inside
   Infisical itself) plus on-demand ``refresh_now()`` (the server's SIGHUP handler).
   Refresh failures log loudly but keep serving the last-known-good cache.
4. **Write-through on admin save.**  ``set()`` writes the new value to Infisical FIRST
   (POST/PATCH /api/v3/secrets/raw) and only then updates the local cache.  A failed
   Infisical write raises SettingsError and the cache is left untouched -- cache and
   Infisical never diverge silently.

Stdlib only (urllib).  No secret VALUES are ever logged; only key names.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import urllib.error
import urllib.request

logger = logging.getLogger("infisical_settings")

INFISICAL_API_DEFAULT = "https://app.infisical.com"
DEFAULT_REFRESH_SECONDS = 300.0
HTTP_TIMEOUT = 10.0
# Knob read from the cache itself, so the refresh cadence is tunable without a deploy.
REFRESH_INTERVAL_KEY = "SETTINGS_REFRESH_SECONDS"

# Identity env names, in priority order.  The fleet's shared automation machine
# identity ("global api keys" holds these on the Mac) is injected into the container
# environment on hosted services; local dev may also use the JSON handoff file.
IDENTITY_PREFIXES = ("INFISICAL_AUTOMATION", "INFISICAL_SHARED")
IDENTITY_FILE_ENV = "INFISICAL_MACHINE_IDENTITY"
# Fleet convention (see the Infisical SOT rollout brief): the machine identity JSON lives
# under ~/workspace/.secrets on agent machines; the Mac uses ~/.secrets/global-api-keys.
IDENTITY_FILE_DEFAULT = pathlib.Path("~/workspace/.secrets/infisical-machine-identity.json")


class SettingsError(Exception):
    """Startup / write-through failure.  The message names the key and points at INFISICAL.md."""


class InfisicalSettings:
    """One app's settings set, cached in memory and backed by Infisical."""

    def __init__(self, *, project_id: str, environment: str = "dev",
                 managed_keys: tuple[str, ...] = (), required_keys: tuple[str, ...] = (),
                 secret_keys: tuple[str, ...] = (), api_base: str | None = None,
                 identity_prefixes: tuple[str, ...] = IDENTITY_PREFIXES) -> None:
        self.project_id = project_id
        self.environment = environment
        self.managed_keys = tuple(managed_keys)
        self.required_keys = tuple(required_keys)
        self.secret_keys = frozenset(secret_keys)
        self.api_base = (api_base or os.environ.get("INFISICAL_API_BASE")
                         or INFISICAL_API_DEFAULT).rstrip("/")
        self.identity_prefixes = identity_prefixes
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._access_token: str | None = None
        self._identity: tuple[str | None, str | None] | None = None
        self._refresh_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listeners: list = []

    # -- identity -----------------------------------------------------------

    def _identity_from_env(self) -> tuple[str | None, str | None]:
        for prefix in self.identity_prefixes:
            cid = (os.environ.get(f"{prefix}_CLIENT_ID") or "").strip()
            csec = (os.environ.get(f"{prefix}_CLIENT_SECRET") or "").strip()
            if cid and csec:
                return cid, csec
        return None, None

    def _identity_from_file(self) -> tuple[str | None, str | None]:
        path = pathlib.Path(os.environ.get(IDENTITY_FILE_ENV, "") or IDENTITY_FILE_DEFAULT).expanduser()
        if not path.is_file():
            return None, None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None, None
        for prefix in self.identity_prefixes:
            cid = str(data.get(f"{prefix}_CLIENT_ID") or "").strip()
            csec = str(data.get(f"{prefix}_CLIENT_SECRET") or "").strip()
            if cid and csec:
                return cid, csec
        return None, None

    def identity(self) -> tuple[str | None, str | None]:
        """(client_id, client_secret); resolved once and cached.  Values never logged."""
        if self._identity is None:
            cid, csec = self._identity_from_env()
            if not cid:
                cid, csec = self._identity_from_file()
            self._identity = (cid, csec)
        return self._identity

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 token: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api_base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8") or "{}")
            except (OSError, ValueError):
                payload = {}
            return e.code, payload

    def _login(self) -> str:
        cid, csec = self.identity()
        if not cid:
            raise SettingsError(
                "no Infisical identity configured (checked "
                + ", ".join(f"{p}_CLIENT_ID" for p in self.identity_prefixes)
                + " in the environment and the machine-identity file); see INFISICAL.md")
        status, payload = self._request(
            "POST", "/api/v1/auth/universal-auth/login",
            {"clientId": cid, "clientSecret": csec})
        token = payload.get("accessToken") if status == 200 else None
        if not token:
            raise SettingsError(
                f"Infisical universal-auth login failed (status {status}); "
                "check the machine identity in INFISICAL.md")
        self._access_token = token
        return token

    def _fetch_all(self, token: str) -> dict[str, str]:
        path = ("/api/v3/secrets/raw?workspaceId=" + urllib.parse.quote(self.project_id)
                + "&environment=" + urllib.parse.quote(self.environment))
        status, payload = self._request("GET", path, token=token)
        if status == 401:
            # Token expired or rotated: one re-login, then retry once.
            token = self._login()
            status, payload = self._request("GET", path, token=token)
        if status != 200:
            raise SettingsError(
                f"Infisical secrets fetch failed (status {status}) for project "
                f"{self.project_id} environment {self.environment}; see INFISICAL.md")
        out: dict[str, str] = {}
        for secret in payload.get("secrets", []):
            key = secret.get("secretKey")
            if key in self.managed_keys:
                out[key] = str(secret.get("secretValue") or "")
        self._access_token = token
        return out

    # -- lifecycle ----------------------------------------------------------

    def init(self) -> None:
        """Populate the cache at startup.  Fails fast on missing required keys.

        Infisical is authoritative; any managed key still missing after the fetch
        is backfilled from the process environment (the migration bridge -- logged
        by name so the fallback is always visible).  With no identity configured
        at all, every managed key comes from the environment and the service keeps
        its old env-only behavior.
        """
        cid, _csec = self.identity()
        if cid:
            token = self._login()
            fetched = self._fetch_all(token)
        else:
            logger.warning("infisical-settings: no machine identity; "
                           "populating managed keys from the process environment")
            fetched = {}
        backfilled = [k for k in self.managed_keys
                      if k not in fetched and os.environ.get(k)]
        for k in backfilled:
            fetched[k] = os.environ[k]
        if backfilled:
            logger.warning("infisical-settings: backfilled from environment (not in Infisical): "
                           + ", ".join(backfilled))
        with self._lock:
            self._cache = dict(fetched)
        missing = [k for k in self.required_keys if not self._cache.get(k)]
        if missing:
            raise SettingsError(
                "missing required setting(s): " + ", ".join(missing)
                + " -- add them to the Infisical project (see INFISICAL.md, "
                "key inventory) and restart")
        logger.info("infisical-settings: loaded %d managed key(s) from %s",
                    len(self._cache), "Infisical" if cid else "environment")

    def get(self, key: str, default: str | None = None) -> str | None:
        """Memory-only read.  Never touches the network."""
        with self._lock:
            return self._cache.get(key, default)

    def present(self, key: str) -> bool:
        with self._lock:
            return bool(self._cache.get(key))

    def refresh(self) -> bool:
        """Re-fetch from Infisical.  On failure the last-known-good cache is kept."""
        cid, _csec = self.identity()
        if not cid:
            return False                      # env-backed: nothing to refresh
        try:
            token = self._access_token or self._login()
            fetched = self._fetch_all(token)
        except Exception as e:                # noqa: BLE001 - staleness beats an outage
            logger.error("infisical-settings: refresh failed (%s); keeping last-known-good "
                         "cache of %d key(s)", type(e).__name__, len(self._cache))
            return False
        with self._lock:
            self._cache = dict(fetched)
        logger.info("infisical-settings: refreshed %d managed key(s)", len(fetched))
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:                 # noqa: BLE001 - a listener must not break refresh
                logger.exception("infisical-settings: refresh listener failed")
        return True

    def refresh_now(self) -> bool:
        """On-demand refresh (SIGHUP handler, admin action)."""
        return self.refresh()

    # -- write-through ------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        """Write-through: Infisical FIRST, then the local cache.

        Persists via PATCH /api/v3/secrets/raw/{key} (POST when the key does not
        exist yet, i.e. 404), mirroring the fleet's TypeScript pilot.  Raises
        SettingsError when the Infisical write fails; the cache is left untouched
        so the two can never diverge silently.
        """
        if key not in self.managed_keys:
            raise SettingsError(f"refusing to write unmanaged key {key!r}; "
                                "see INFISICAL.md for the managed key inventory")
        cid, _csec = self.identity()
        if not cid:
            raise SettingsError(
                "no Infisical identity configured; cannot write through to the "
                "source of truth (see INFISICAL.md)")

        def _write(method: str) -> int:
            status, _payload = self._request(
                method, "/api/v3/secrets/raw/" + urllib.parse.quote(key, safe=""),
                {"workspaceId": self.project_id, "environment": self.environment,
                 "secretPath": "/", "secretValue": value, "type": "shared"},
                token=self._access_token or self._login())
            return status

        try:
            status = _write("PATCH")
            if status == 401:
                # Token may have expired mid-flight: re-login once and retry.
                self._login()
                status = _write("PATCH")
            if status == 404:
                status = _write("POST")
        except SettingsError:
            raise
        except Exception as e:                # noqa: BLE001 - network down: reject the save
            raise SettingsError(
                f"Infisical write for {key!r} failed ({type(e).__name__}); "
                "the save was rejected and the local cache was NOT updated") from e
        if status not in (200, 201):
            raise SettingsError(
                f"Infisical write for {key!r} failed (status {status}); "
                "the save was rejected and the local cache was NOT updated")
        # Infisical accepted the write: only now does the cache change.
        with self._lock:
            self._cache[key] = value
        logger.info("infisical-settings: wrote key %r through to Infisical", key)

    # -- refresh thread -----------------------------------------------------

    def refresh_interval(self) -> float:
        raw = self.get(REFRESH_INTERVAL_KEY, "")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_REFRESH_SECONDS
        return val if val > 0 else DEFAULT_REFRESH_SECONDS

    def add_refresh_listener(self, fn) -> None:
        self._listeners.append(fn)

    def start_refresh(self, interval: float | None = None) -> None:
        """Daemon thread; idempotent.  `interval` overrides the Infisical-tuned cadence."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval if interval is not None else self.refresh_interval()):
                self.refresh()

        self._refresh_thread = threading.Thread(target=_loop, name="infisical-settings-refresh",
                                                daemon=True)
        self._refresh_thread.start()

    def stop_refresh(self) -> None:
        self._stop.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)
            self._refresh_thread = None

    # -- admin surface helpers ----------------------------------------------

    def export_env(self, keys: tuple[str, ...]) -> list[str]:
        """Mirror non-empty cached values into os.environ (for backend libraries that
        read the environment).  Returns the key names that were exported -- never values."""
        exported = []
        for key in keys:
            val = self.get(key)
            if val:
                os.environ[key] = val
                exported.append(key)
        return exported

    def admin_listing(self) -> dict[str, dict]:
        """Key inventory for the admin endpoint: names only, values masked for secrets."""
        with self._lock:
            keys = list(self.managed_keys)
            cache = dict(self._cache)
        out: dict[str, dict] = {}
        for key in keys:
            val = cache.get(key, "")
            if key in self.secret_keys:
                out[key] = {"set": bool(val), "value": "set" if val else "empty"}
            else:
                out[key] = {"set": bool(val), "value": val}
        return out


# --------------------------------------------------------------------------- module default

_default: InfisicalSettings | None = None


def configure(**kwargs) -> InfisicalSettings:
    global _default
    _default = InfisicalSettings(**kwargs)
    return _default


def _require() -> InfisicalSettings:
    if _default is None:
        raise SettingsError("infisical settings not configured; call configure() at startup")
    return _default


def init_settings() -> None:
    _require().init()


def get(key: str, default: str | None = None) -> str | None:
    if _default is None:
        return default
    return _default.get(key, default)


def present(key: str) -> bool:
    return _default is not None and _default.present(key)


def set(key: str, value: str) -> None:  # noqa: A001 - write-through setter, shadows builtin deliberately
    _require().set(key, value)


def refresh_now() -> bool:
    return _default.refresh() if _default else False


def start_refresh(interval: float | None = None) -> None:
    if _default:
        _default.start_refresh(interval)


def stop_refresh() -> None:
    if _default:
        _default.stop_refresh()


def export_env(keys: tuple[str, ...]) -> list[str]:
    return _default.export_env(keys) if _default else []


def add_refresh_listener(fn) -> None:
    if _default:
        _default.add_refresh_listener(fn)


def admin_listing() -> dict[str, dict]:
    return _default.admin_listing() if _default else {}


def reset_for_tests() -> None:
    """Drop the module default (tests only)."""
    global _default
    _default = None
