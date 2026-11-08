"""SERVER Operator identity only: a private scoped service token, never environment or subprocess arguments.

The normal credential path for the Operator on the server, after the existing server Notes
reader's reviewed pattern (notes_service_reader.py):  a scoped Infisical service token lives in
a custody-checked private config file, the reader verifies the token's metadata (id, name,
project, EXACT folder scope, read-only, valid expiry) against the pinned deployment on every
fetch, then reads only the Operator folder's three names and holds them in memory (600 s cache,
discarded before any refetch, never a fallback).  Infisical stays the sole source of truth;  no
value touches the environment, a file beyond this config, or a log line.  Absent config means
this host has no reader (the caller falls back to its own existing reader);  present but unsafe
NEVER falls back.

DEPLOYMENT_SHA256 commits the (project, token id, token name, api origin) tuple of the minted
token in source.  It ships EMPTY:  until the scoped token is minted (within the earned
creation grant, registered in the reference sheet - metadata only) and its commitment pinned
here by the reviewed install, the reader refuses every config as unqualified, so no loose token
can ever be used.
"""

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

FOLDER = "/zulip_operator"
NAMES = ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE")
KEY_CACHE_S = 600.0
# Pinned at token mint by the reviewed install;  empty refuses every deployment until then.
DEPLOYMENT_SHA256 = ""
MAX_BYTES = 256 * 1024
CONFIG_PARTS = (".config", "operator-identity", "reader.json")


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
    home = Path.home() if home is None else Path(home)
    fd = None
    try:
        # Prove absence before the no-follow custody walk. A symlinked ancestor
        # may lead to no optional config at all. lstat never follows a final
        # reader.json symlink, so a dangling config link still refuses below.
        try:
            os.lstat(home.joinpath(*CONFIG_PARTS))
        except FileNotFoundError:
            return None
        try:
            fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                # A home the walk cannot trust (a shared or root-owned scratch dir, say)
                # still means "no reader" when the config simply is not there;  only a
                # config that EXISTS under unsafe custody refuses.
                if not os.path.isfile(home.joinpath(*CONFIG_PARTS)):
                    return None
                raise ReaderError("reader-custody-unsafe")
        except FileNotFoundError:
            return None
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
            != {"schema", "service_token", "api_url", "project_id", "token_id", "token_name"}
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
        config["api_url"] = _origin(config["api_url"])
        pins = {name: config[name] for name in ("project_id", "token_id", "token_name", "api_url")}
        commitment = hashlib.sha256(
            json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if not DEPLOYMENT_SHA256 or commitment != DEPLOYMENT_SHA256:
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
        self.origin = config["api_url"]
        self._project = config["project_id"]
        self._token_id = config["token_id"]
        self._token_name = config["token_name"]
        self._check_custody = None
        self._lock = threading.Lock()
        self._cached = None
        # Match Infisical's service-token bearer contract, not universal auth.
        self._bearer = ".".join(config["service_token"].split(".")[:3])
        self._clock = clock
        self._monotonic = monotonic
        self._cache_started = 0.0
        self._cache_expiry = 0.0
        self._request = request or self._get

    def __repr__(self):
        return "OperatorIdentityReader(SERVER)"

    @property
    def project_id(self) -> str:
        return self._project

    def _get(self, path):
        # The pinned, validated origin from the config - never a hardcoded host:  the
        # deployment commitment covers it, so a different host is a different deployment.
        request = urllib.request.Request(
            self.origin + "/api" + path,
            method="GET",
            headers={
                "Authorization": "Bearer " + self._bearer,
                "Accept": "application/json",
                "User-Agent": "operator-identity-reader",
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
            self._cached = None
            self._cache_expiry = 0.0

    def credentials(self):
        """{name: value} for the three Operator names;  errors are ReaderError reason tokens."""
        with self._lock:
            if self._check_custody is not None:
                try:
                    self._check_custody()
                except ReaderError:
                    self._cached = None
                    self._cache_expiry = 0.0
                    raise
            now = self._clock()
            tick = self._monotonic()
            age = tick - self._cache_started
            if (
                self._cached is not None
                and 0 <= age < KEY_CACHE_S
                and now < self._cache_expiry - 30
            ):
                return dict(self._cached)
            # Expired cache is discarded before a fresh request, never a fallback.
            self._cached = None
            self._cache_expiry = 0.0
            values, expires = self._fetch_all()
            expiry_ts = expires.timestamp()
            if self._clock() >= expiry_ts - 30:
                raise ReaderError("reader-metadata-expired")
            self._cached = values
            self._cache_started = tick
            self._cache_expiry = expiry_ts
            return dict(values)

    def _fetch_all(self):
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
            or meta.get("scopes") != [{"environment": "prod", "secretPath": FOLDER}]
            or meta.get("permissions") != ["read"]
            or not valid_expiry
        ):
            raise ReaderError("reader-metadata-invalid")
        values = {}
        for name in NAMES:
            query = urllib.parse.urlencode(
                {
                    "workspaceId": self._project,
                    "environment": "prod",
                    "secretPath": FOLDER,
                    "type": "shared",
                    "include_imports": "false",
                    "expandSecretReferences": "false",
                }
            )
            answer = self._request("/v3/secrets/raw/" + name + "?" + query)
            row = answer.get("secret") if isinstance(answer, dict) else None
            required = {
                "workspace": self._project,
                "environment": "prod",
                "secretPath": FOLDER,
                "secretKey": name,
                "type": "shared",
                "secretValueHidden": False,
            }
            if not isinstance(row, dict) or any(row.get(k) != v for k, v in required.items()):
                raise ReaderError("reader-key-unverified")
            value = row.get("secretValue")
            if not isinstance(value, str) or not value:
                raise ReaderError("reader-key-unverified")
            values[name] = value
        return values, expires


_READERS: dict[str, Reader | None] = {}
_READERS_LOCK = threading.Lock()


def reader_for(home: Path | None = None):
    """The host's reader, memoized so its 600 s value cache survives across callers.

    Keyed by the home path;  the reader's own custody recheck on every use is what makes
    reuse safe - a changed or vanished config raises ReaderError, and the entry is then
    evicted so the next call re-reads the file.
    """
    key = str(Path.home() if home is None else Path(home))
    with _READERS_LOCK:
        if key in _READERS:
            return _READERS[key]
        reader = load(Path(key))
        if reader is not None:
            _READERS[key] = reader
        # Absence is never cached:  a valid config installed later must take effect in this
        # same process on the next call.
        return reader


def evict_reader(home: Path | None = None) -> None:
    key = str(Path.home() if home is None else Path(home))
    with _READERS_LOCK:
        _READERS.pop(key, None)
