"""SERVER Notes only: a private service token, never environment or subprocess arguments."""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KEY = "OPERATOR_NOTES_SERVER_KEY"
API = "https://app.infisical.com/api"
KEY_CACHE_S = 600.0
# Reviewed deployment metadata commitment; no private inventory values in source.
DEPLOYMENT_SHA256 = "c043de10c9d1185093f564e825d1c624e7a22681e4c07ca7362a4078a4b61e6d"
MAX_BYTES = 256 * 1024
CONFIG_PARTS = (".config", "operator-notes", "reader.json")


class ReaderError(Exception):
    """A short reason only; never credentials, response bodies or private config."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate")
        result[name] = value
    return result


def _origin(value):
    if not isinstance(value, str) or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value
    ):
        raise ReaderError("reader-config-invalid")
    try:
        url = urllib.parse.urlsplit(value)
        url.port
    except ValueError:
        raise ReaderError("reader-config-invalid") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        raise ReaderError("reader-config-invalid")
    return "https://" + url.netloc


def load(home: Path | None = None):
    """Absent means this host has no reader; present but unsafe never falls back."""
    home = Path.home() if home is None else home
    fd = None
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ReaderError("reader-custody-unsafe")
        for index, part in enumerate(CONFIG_PARTS):
            final = index == len(CONFIG_PARTS) - 1
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (0 if final else os.O_DIRECTORY),
                    dir_fd=fd,
                )
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ReaderError("reader-custody-unsafe")
            if index == 1 and stat.S_IMODE(info.st_mode) != 0o700:
                raise ReaderError("reader-custody-unsafe")
            if final:
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 16384
                ):
                    raise ReaderError("reader-custody-unsafe")
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise ReaderError("reader-config-invalid")
        config = json.loads(raw, object_pairs_hook=_pairs)
        if (
            not isinstance(config, dict)
            or set(config)
            != {
                "schema",
                "service_token",
                "operator_api_url",
                "project_id",
                "token_id",
                "token_name",
            }
            or type(config["schema"]) is not int
            or config["schema"] != 1
        ):
            raise ReaderError("reader-config-invalid")
        token = config["service_token"]
        if (
            not isinstance(token, str)
            or not token.startswith("st.")
            or len(token.split(".")) != 4
            or any(not p for p in token.split("."))
            or any(c.isspace() for c in token)
        ):
            raise ReaderError("reader-config-invalid")
        for name in ("project_id", "token_id"):
            if not isinstance(config[name], str) or not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", config[name]
            ):
                raise ReaderError("reader-config-invalid")
        if not isinstance(config["token_name"], str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,63}", config["token_name"]
        ):
            raise ReaderError("reader-config-invalid")
        config["operator_api_url"] = _origin(config["operator_api_url"])
        pins = {
            name: config[name]
            for name in ("project_id", "token_id", "token_name", "operator_api_url")
        }
        commitment = hashlib.sha256(
            json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if commitment != DEPLOYMENT_SHA256:
            raise ReaderError("reader-deployment-unqualified")
        reader = Reader(config)
        fingerprint = hashlib.sha256(raw).digest()

        def check_custody():
            current = load(home)
            if current is None or current._fingerprint != fingerprint:
                raise ReaderError("reader-config-changed")

        reader._fingerprint = fingerprint
        reader._check_custody = check_custody
        return reader
    except ReaderError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise ReaderError("reader-config-invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


class Reader:
    def __init__(self, config, *, clock=time.time, monotonic=time.monotonic, request=None):
        self.origin = config["operator_api_url"]
        self._project = config["project_id"]
        self._token_id = config["token_id"]
        self._token_name = config["token_name"]
        self._check_custody = None
        self._lock = threading.Lock()
        self._cached_key = None
        # Match Infisical's service-token bearer contract, not universal auth.
        self._bearer = ".".join(config["service_token"].split(".")[:3])
        self._clock = clock
        self._monotonic = monotonic
        self._cache_started = 0.0
        self._cache_expiry = 0.0
        self._request = request or self._get

    def __repr__(self):
        return "NotesServiceReader(SERVER)"

    def _get(self, path):
        request = urllib.request.Request(
            API + path,
            method="GET",
            headers={
                "Authorization": "Bearer " + self._bearer,
                "Accept": "application/json",
                "User-Agent": "operator-notes-reader",
            },
        )
        # No ambient proxy credentials or redirects, and no inherited custom headers.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                if response.status != 200:
                    raise ReaderError("reader-api-failed")
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ReaderError("reader-api-oversized")
            return json.loads(raw, object_pairs_hook=_pairs)
        except ReaderError:
            raise
        except (
            OSError,
            http.client.HTTPException,
            urllib.error.URLError,
            ValueError,
            UnicodeError,
        ):
            raise ReaderError("reader-api-failed") from None

    def forget(self):
        with self._lock:
            self._cached_key = None
            self._cache_expiry = 0.0

    def key(self):
        with self._lock:
            if self._check_custody is not None:
                try:
                    self._check_custody()
                except ReaderError:
                    self._cached_key = None
                    self._cache_expiry = 0.0
                    raise
            now = self._clock()
            tick = self._monotonic()
            age = tick - self._cache_started
            if (
                self._cached_key is not None
                and 0 <= age < KEY_CACHE_S
                and now < self._cache_expiry - 30
            ):
                return self._cached_key
            # Expired cache is discarded before a fresh request, never a fallback.
            self._cached_key = None
            self._cache_expiry = 0.0
            value, expires = self._fetch_key()
            # Network time consumes the TTL; UTC expiry is checked independently.
            if self._clock() >= expires - 30:
                raise ReaderError("reader-metadata-expired")
            self._cached_key = value
            self._cache_started = tick
            self._cache_expiry = expires
            return value

    def _fetch_key(self):
        meta = self._request("/v2/service-token")
        if not isinstance(meta, dict):
            raise ReaderError("reader-metadata-invalid")
        try:
            raw_expiry = meta["expiresAt"]
            if not isinstance(raw_expiry, str):
                raise ValueError("expiry")
            expires = dt.datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            valid_expiry = expires.tzinfo is not None and expires.timestamp() > self._clock() + 30
        except (KeyError, TypeError, ValueError, OverflowError):
            valid_expiry = False
        if (
            meta.get("_id") != self._token_id
            or meta.get("id") != self._token_id
            or meta.get("name") != self._token_name
            or meta.get("workspace") != self._project
            or meta.get("scopes") != [{"environment": "prod", "secretPath": "/operator"}]
            or meta.get("permissions") != ["read"]
            or not valid_expiry
        ):
            raise ReaderError("reader-metadata-invalid")
        query = urllib.parse.urlencode(
            {
                "workspaceId": self._project,
                "environment": "prod",
                "secretPath": "/operator",
                "type": "shared",
                "include_imports": "false",
                "expandSecretReferences": "false",
            }
        )
        answer = self._request("/v3/secrets/raw/" + KEY + "?" + query)
        row = answer.get("secret") if isinstance(answer, dict) else None
        required = {
            "workspace": self._project,
            "environment": "prod",
            "secretPath": "/operator",
            "secretKey": KEY,
            "type": "shared",
            "secretValueHidden": False,
        }
        if not isinstance(row, dict) or any(row.get(k) != v for k, v in required.items()):
            raise ReaderError("reader-key-unverified")
        if type(row.get("secretValueHidden")) is not bool:
            raise ReaderError("reader-key-unverified")
        value = row.get("secretValue")
        if (
            not isinstance(value, str)
            or not 32 <= len(value) <= 4096
            or "${" in value
            or any(c.isspace() for c in value)
        ):
            raise ReaderError("reader-key-unverified")
        return value, expires.timestamp()
