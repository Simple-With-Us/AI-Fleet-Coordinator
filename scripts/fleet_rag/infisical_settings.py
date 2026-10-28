#!/usr/bin/env python3
"""Infisical sole-source-of-truth settings for the fleet's Python services.

Implements the fleet-wide canonical pattern (see repo-root INFISICAL.md):

1. **Load at startup.**  ``init_settings()`` fetches the full managed settings set from
   the app's Infisical project (universal-auth login -> access token ->
   GET /api/v3/secrets/raw) into an in-memory cache.  The credential is first match
   wins:  an ``INFISICAL_TOKEN`` service token in the environment is the bearer
   directly, with no universal-auth login on that path at all;  otherwise the machine
   identity is logged in.  When no Infisical credential is configured (local dev
   without the machine identity), the managed keys are populated from the process
   environment instead and a warning is logged -- the service keeps its old env-based
   behavior.  Missing *required* keys fail fast with a SettingsError naming the key and
   pointing at INFISICAL.md.
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

5. **Prod is the only environment.**  Infisical has one live environment per project,
   ``prod`` (owner decision 2026-10-10:  dev and staging are being deleted).  The default
   is prod and any other slug is refused:  ``init()``, ``set()`` and every fetch raise
   SettingsError before touching the network, ``refresh()`` logs the refusal and keeps the
   last-known-good cache, and ``configure()`` leaves the previously configured settings in
   place.  A stray ``INFISICAL_ENVIRONMENT=dev`` therefore fails loudly instead of reading a
   stale copy.

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
# The only Infisical environment the fleet reads or writes (see the module docstring).
PROD_ENVIRONMENT = "prod"
DEFAULT_REFRESH_SECONDS = 300.0
HTTP_TIMEOUT = 10.0
# Knob read from the cache itself, so the refresh cadence is tunable without a deploy.
REFRESH_INTERVAL_KEY = "SETTINGS_REFRESH_SECONDS"

# Identity env names, in priority order.  The fleet's shared automation machine
# identity is injected into the container environment on hosted services; local dev may
# also use the JSON identity file below.
IDENTITY_PREFIXES = ("INFISICAL_AUTOMATION", "INFISICAL_SHARED")
IDENTITY_FILE_ENV = "INFISICAL_MACHINE_IDENTITY"
# Fleet convention (see the Infisical SOT rollout brief): the machine identity JSON lives
# under ~/workspace/.secrets on agent machines; the Mac runs under `fleet-secrets run`.
IDENTITY_FILE_DEFAULT = pathlib.Path("~/workspace/.secrets/infisical-machine-identity.json")
# The service token a consumer binds in the environment, used as the bearer directly:
# no universal-auth login happens on that path.  First match wins, so it sits ahead of
# the machine identity.
ENV_TOKEN = "INFISICAL_TOKEN"
# The shape the fleet uses for a service token (the BotFleet reader's rule):  the st.
# prefix, 20 to 2048 characters, and no whitespace anywhere.
SERVICE_TOKEN_PREFIX = "st."
MIN_SERVICE_TOKEN_LENGTH = 20
MAX_SERVICE_TOKEN_LENGTH = 2048
# Where the bearer came from, so a 401 is answered the right way:  a machine identity
# can log in again, a service token cannot.
SOURCE_SERVICE_TOKEN = "service-token"
SOURCE_MACHINE_IDENTITY = "machine-identity"


class SettingsError(Exception):
    """Startup / write-through failure.  The message names the key and points at INFISICAL.md."""


def _is_service_token(token: str) -> bool:
    """Whether a value has the shape of an Infisical service token.

    The fleet's rule:  the st. prefix, 20 to 2048 characters, no whitespace.  A value
    that fails it is refused before it ever reaches the API."""
    return (
        token.startswith(SERVICE_TOKEN_PREFIX)
        and MIN_SERVICE_TOKEN_LENGTH <= len(token) <= MAX_SERVICE_TOKEN_LENGTH
        and not any(ch.isspace() for ch in token)
    )


def _service_token_401_message() -> str:
    """The error a 401 on the service-token path raises.

    A service token cannot be refreshed by a re-login, so the message names the
    variable and says what to check.  The token itself never appears in it."""
    return (
        f"Infisical rejected the {ENV_TOKEN} service token (status 401);  check that the "
        f"token is current, is scoped to this project and environment, and is bound as "
        f"{ENV_TOKEN}; see INFISICAL.md"
    )


class InfisicalSettings:
    """One app's settings set, cached in memory and backed by Infisical."""

    def __init__(
        self,
        *,
        project_id: str,
        environment: str = PROD_ENVIRONMENT,
        managed_keys: tuple[str, ...] = (),
        required_keys: tuple[str, ...] = (),
        secret_keys: tuple[str, ...] = (),
        api_base: str | None = None,
        identity_prefixes: tuple[str, ...] = IDENTITY_PREFIXES,
    ) -> None:
        self.project_id = project_id
        # Empty means "unset" (an INFISICAL_ENVIRONMENT= line), so it is prod; any other
        # slug is kept as given and refused at first use by _require_prod().
        self.environment = (environment or "").strip().lower() or PROD_ENVIRONMENT
        self.managed_keys = tuple(managed_keys)
        self.required_keys = tuple(required_keys)
        self.secret_keys = frozenset(secret_keys)
        self.api_base = (
            api_base or os.environ.get("INFISICAL_API_BASE") or INFISICAL_API_DEFAULT
        ).rstrip("/")
        self.identity_prefixes = identity_prefixes
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._access_token: str | None = None
        self._credential_source: str | None = None
        self._identity: tuple[str | None, str | None] | None = None
        self._refresh_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listeners: list = []

    # -- environment guard --------------------------------------------------

    def _environment_refusal(self) -> str | None:
        """Why this instance may not talk to Infisical, or None when it is prod.
        The slug is not a secret, so it is safe to name."""
        if self.environment == PROD_ENVIRONMENT:
            return None
        return (
            f"refusing Infisical environment {self.environment!r}: prod is the only "
            "environment (dev and staging are retired); unset INFISICAL_ENVIRONMENT "
            "or set it to prod; see INFISICAL.md"
        )

    def _require_prod(self) -> None:
        refusal = self._environment_refusal()
        if refusal:
            raise SettingsError(refusal)

    # -- identity -----------------------------------------------------------

    def _identity_from_env(self) -> tuple[str | None, str | None]:
        for prefix in self.identity_prefixes:
            cid = (os.environ.get(f"{prefix}_CLIENT_ID") or "").strip()
            csec = (os.environ.get(f"{prefix}_CLIENT_SECRET") or "").strip()
            if cid and csec:
                return cid, csec
        return None, None

    def _identity_from_file(self) -> tuple[str | None, str | None]:
        path = pathlib.Path(
            os.environ.get(IDENTITY_FILE_ENV, "") or IDENTITY_FILE_DEFAULT
        ).expanduser()
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

    # -- credential ---------------------------------------------------------

    def _service_token(self) -> str | None:
        """The INFISICAL_TOKEN service token, validated, or None when it is not set.

        A token that is set but unusable is refused here, before any API call, and its
        value never reaches an error message.  An empty or whitespace-only value counts
        as not set, so the next leg of the chain gets its turn."""
        raw = os.environ.get(ENV_TOKEN)
        if raw is None:
            return None
        token = raw.strip()
        if not token:
            return None
        if not _is_service_token(token):
            raise SettingsError(
                f"{ENV_TOKEN} is not a usable Infisical service token:  it must start with "
                f"the {SERVICE_TOKEN_PREFIX} prefix, carry no whitespace and be "
                f"{MIN_SERVICE_TOKEN_LENGTH} to {MAX_SERVICE_TOKEN_LENGTH} characters long; "
                "see INFISICAL.md"
            )
        return token

    def _credential(self) -> tuple[str | None, str]:
        """The bearer for the next Infisical call and where it came from, first match wins.

        The INFISICAL_TOKEN service token is the bearer directly:  no universal-auth
        login happens on that path.  The machine identity is logged in and its session
        token is the bearer;  a session token already held is reused.  With neither, the
        caller gets (None, "") and the module's own fail-closed behaviour applies.
        Neither value is ever logged."""
        token = self._service_token()
        if token:
            return token, SOURCE_SERVICE_TOKEN
        if self._access_token and self._credential_source == SOURCE_MACHINE_IDENTITY:
            return self._access_token, SOURCE_MACHINE_IDENTITY
        cid, _csec = self.identity()
        if cid:
            return self._login(), SOURCE_MACHINE_IDENTITY
        return None, ""

    # -- HTTP ---------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: dict | None = None, token: str | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api_base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
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
                + " in the environment and the machine-identity file); see INFISICAL.md"
            )
        status, payload = self._request(
            "POST", "/api/v1/auth/universal-auth/login", {"clientId": cid, "clientSecret": csec}
        )
        token = payload.get("accessToken") if status == 200 else None
        if not token:
            raise SettingsError(
                f"Infisical universal-auth login failed (status {status}); "
                "check the machine identity in INFISICAL.md"
            )
        self._access_token = token
        self._credential_source = SOURCE_MACHINE_IDENTITY
        return token

    def _fetch_all(self, token: str, source: str) -> dict[str, str]:
        self._require_prod()
        path = (
            "/api/v3/secrets/raw?workspaceId="
            + urllib.parse.quote(self.project_id)
            + "&environment="
            + urllib.parse.quote(self.environment)
        )
        status, payload = self._request("GET", path, token=token)
        if status == 401:
            if source == SOURCE_SERVICE_TOKEN:
                # A service token cannot be refreshed by a re-login, so there is
                # nothing to retry with.
                raise SettingsError(_service_token_401_message())
            # Token expired or rotated: one re-login, then retry once.
            token = self._login()
            status, payload = self._request("GET", path, token=token)
        if status != 200:
            raise SettingsError(
                f"Infisical secrets fetch failed (status {status}) for project "
                f"{self.project_id} environment {self.environment}; see INFISICAL.md"
            )
        out: dict[str, str] = {}
        for secret in payload.get("secrets", []):
            key = secret.get("secretKey")
            if key in self.managed_keys:
                out[key] = str(secret.get("secretValue") or "")
        self._access_token = token
        self._credential_source = source
        return out

    # -- lifecycle ----------------------------------------------------------

    def init(self) -> None:
        """Populate the cache at startup.  Fails fast on missing required keys.

        Infisical is authoritative; any managed key still missing after the fetch
        is backfilled from the process environment (the migration bridge -- logged
        by name so the fallback is always visible).  With no credential configured
        at all (no service token and no identity), every managed key comes from the
        environment and the service keeps its old env-only behavior.

        A non-prod environment is refused first, even with no identity configured, so
        a bad INFISICAL_ENVIRONMENT surfaces at startup rather than the day an identity
        is added.
        """
        self._require_prod()
        token, source = self._credential()
        if token:
            fetched = self._fetch_all(token, source)
        else:
            logger.warning(
                "infisical-settings: no Infisical credential (no service token and no "
                "machine identity); populating managed keys from the process environment"
            )
            fetched = {}
        backfilled = [k for k in self.managed_keys if k not in fetched and os.environ.get(k)]
        for k in backfilled:
            fetched[k] = os.environ[k]
        if backfilled:
            logger.warning(
                "infisical-settings: backfilled from environment (not in Infisical): "
                + ", ".join(backfilled)
            )
        with self._lock:
            self._cache = dict(fetched)
        missing = [k for k in self.required_keys if not self._cache.get(k)]
        if missing:
            raise SettingsError(
                "missing required setting(s): "
                + ", ".join(missing)
                + " -- add them to the Infisical project (see INFISICAL.md, "
                "key inventory) and restart"
            )
        logger.info(
            "infisical-settings: loaded %d managed key(s) from %s",
            len(self._cache),
            "Infisical" if token else "environment",
        )

    def get(self, key: str, default: str | None = None) -> str | None:
        """Memory-only read.  Never touches the network."""
        with self._lock:
            return self._cache.get(key, default)

    def present(self, key: str) -> bool:
        with self._lock:
            return bool(self._cache.get(key))

    def refresh(self) -> bool:
        """Re-fetch from Infisical.  On failure the last-known-good cache is kept."""
        refusal = self._environment_refusal()
        if refusal:
            logger.error(
                "infisical-settings: refresh refused (%s); keeping last-known-good "
                "cache of %d key(s)",
                refusal,
                len(self._cache),
            )
            return False
        try:
            token, source = self._credential()
            if not token:
                return False  # env-backed: nothing to refresh
            fetched = self._fetch_all(token, source)
        except SettingsError as e:
            # The message names the credential and what to check; it never carries a
            # value, so it is safe to log.
            logger.error(
                "infisical-settings: refresh failed (%s); keeping last-known-good "
                "cache of %d key(s)",
                e,
                len(self._cache),
            )
            return False
        except Exception as e:  # noqa: BLE001 - staleness beats an outage
            logger.error(
                "infisical-settings: refresh failed (%s); keeping last-known-good "
                "cache of %d key(s)",
                type(e).__name__,
                len(self._cache),
            )
            return False
        with self._lock:
            self._cache = dict(fetched)
        logger.info("infisical-settings: refreshed %d managed key(s)", len(fetched))
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
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
        self._require_prod()  # never write through to a non-prod environment
        if key not in self.managed_keys:
            raise SettingsError(
                f"refusing to write unmanaged key {key!r}; "
                "see INFISICAL.md for the managed key inventory"
            )
        token, source = self._credential()
        if not token:
            raise SettingsError(
                "no Infisical credential configured; cannot write through to the "
                f"source of truth (set {ENV_TOKEN} or the machine identity; "
                "see INFISICAL.md)"
            )

        def _write(method: str, bearer: str) -> int:
            status, _payload = self._request(
                method,
                "/api/v3/secrets/raw/" + urllib.parse.quote(key, safe=""),
                {
                    "workspaceId": self.project_id,
                    "environment": self.environment,
                    "secretPath": "/",
                    "secretValue": value,
                    "type": "shared",
                },
                token=bearer,
            )
            return status

        try:
            status = _write("PATCH", token)
            if status == 401:
                if source == SOURCE_SERVICE_TOKEN:
                    # A service token cannot be refreshed by a re-login, so there is
                    # nothing to retry with.
                    raise SettingsError(_service_token_401_message())
                # Token may have expired mid-flight: re-login once and retry.
                token = self._login()
                status = _write("PATCH", token)
            if status == 404:
                status = _write("POST", token)
        except SettingsError:
            raise
        except Exception as e:
            raise SettingsError(
                f"Infisical write for {key!r} failed ({type(e).__name__}); "
                "the save was rejected and the local cache was NOT updated"
            ) from e
        if status not in (200, 201):
            raise SettingsError(
                f"Infisical write for {key!r} failed (status {status}); "
                "the save was rejected and the local cache was NOT updated"
            )
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
            while not self._stop.wait(
                interval if interval is not None else self.refresh_interval()
            ):
                self.refresh()

        self._refresh_thread = threading.Thread(
            target=_loop, name="infisical-settings-refresh", daemon=True
        )
        self._refresh_thread.start()

    def stop_refresh(self) -> None:
        self._stop.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)
            self._refresh_thread = None

    # -- admin surface helpers ----------------------------------------------

    def export_env(self, keys: tuple[str, ...]) -> list[str]:
        """Mirror non-empty cached values into os.environ (for backend libraries that
        read the environment).  Returns the key names that were exported -- never values.

        The service token is never mirrored:  it is a credential, not a setting, so it
        stays out of the export path."""
        exported = []
        for key in keys:
            if key == ENV_TOKEN:
                continue
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
            if key in self.secret_keys or key == ENV_TOKEN:
                out[key] = {"set": bool(val), "value": "set" if val else "empty"}
            else:
                out[key] = {"set": bool(val), "value": val}
        return out


# --------------------------------------------------------------------------- module default

_default: InfisicalSettings | None = None


def configure(**kwargs) -> InfisicalSettings:
    """Install the module default.  A refused (non-prod) environment raises SettingsError
    and leaves the previously configured settings, cache included, exactly as they were."""
    global _default
    candidate = InfisicalSettings(**kwargs)
    candidate._require_prod()
    _default = candidate
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


def set(key: str, value: str) -> None:
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
