"""Zulip access for agent-sync: seat and credential loading, a urllib-only HTTP client, event queues.

Credential order (first hit wins, nothing is merged):
  1. --rc PATH
  2. env ZULIP_RC=PATH
  3. <secrets dir>/<Title>-zuliprc  (default ~/.secrets/Zulip, override with AGENT_SYNC_SECRETS_DIR)
  4. env ZULIP_EMAIL + ZULIP_API_KEY + ZULIP_SITE  (cloud seats with no file)
A .env file, ./zuliprc and ~/.zuliprc are never read.

The API key lives only in `Credentials.key` (hidden from repr) and in the Authorization header.
Every error message that leaves this module is scrubbed of the key and of the base64 token, so
a server that echoes the header cannot leak it into stdout, stderr or an exception string.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import base64
import configparser
import http.client
import ipaddress
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "REALM", "DEFAULT_CHANNEL", "MAX_TOPIC_LENGTH", "USER_AGENT",
    "AgentSyncError", "UsageError", "CredentialError", "ApiError", "NetworkError", "QueueExpired",
    "Credentials", "normalise_seat", "credential_file_name", "seat_from_rc_path", "session_tag",
    "realm_url", "resolve_credentials", "read_zuliprc", "env_credentials", "verify_realm",
    "ZulipClient", "EventQueue", "message_channel", "message_topic",
]

REALM = "https://simplewithus.zulipchat.com"
ENV_REALM = "AGENT_SYNC_REALM"
ENV_SECRETS_DIR = "AGENT_SYNC_SECRETS_DIR"
DEFAULT_CHANNEL = "agent-sync"
MAX_TOPIC_LENGTH = 60
USER_AGENT = "agent-sync/1 (fleet)"

# Seat name -> credential file stem, for names the Title Case default would get wrong.  The stems
# are the ones scripts/zulip_provision_bots.py writes (its ROSTER), which is where the files come from.
# MM and MC are fleet-apps.json tags whose bots are named MiniMax and Muse Code.  MA (Muse Assist) and
# GROK-BOT have no provisioned bot yet, so they are not listed; add them when the files exist.
# File stems follow the seat tag (owner 2026-10-07): split on hyphens, parts of two letters or
# fewer stay upper case, longer parts are Title Case.  CLAUDE -> Claude, GROK-BUILD -> Grok-Build,
# BF-BUILDER -> BF-Builder, AG/FX/MM/MC/MA stay as they are.  Add an override only for a file that
# breaks the rule.
SEAT_FILE_OVERRIDES: dict[str, str] = {
    "GROK": "Grok-Build",  # GROK posts as grok-build-bot@ (owner 2026-10-08)
}
SHORT_PART_MAX = 2

MAX_RATE_LIMIT_RETRIES = 3
RETRY_AFTER_CAP = 30.0
GET_RETRY_BACKOFF = (1.0, 3.0)
GATEWAY_STATUSES = frozenset({502, 503, 504})  # the proxy in front of Zulip restarting or timing out

_SEAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_LOOPBACK_NAMES = {"localhost"}


class AgentSyncError(Exception):
    """Base error.  `exit_code` is what the CLI returns."""

    exit_code = 1


class UsageError(AgentSyncError):
    exit_code = 2


class CredentialError(AgentSyncError):
    exit_code = 3


class ApiError(AgentSyncError):
    """Zulip answered with an error.  `code` is Zulip's machine code (e.g. BAD_EVENT_QUEUE_ID)."""

    exit_code = 5

    def __init__(self, msg: str, *, code: str | None = None, status: int | None = None,
                 data: Mapping[str, Any] | None = None) -> None:
        super().__init__("%s [%s]" % (msg, code) if code else msg)
        self.msg = msg
        self.code = code
        self.status = status
        self.data = dict(data or {})


class NetworkError(AgentSyncError):
    """No usable answer.  `timeout` is True for a read or connect timeout.  `maybe_sent` is True
    when the request may have reached the server (a timeout or reset on a non-GET request)."""

    exit_code = 6

    def __init__(self, msg: str, *, timeout: bool = False, maybe_sent: bool = False) -> None:
        super().__init__(msg)
        self.timeout = timeout
        self.maybe_sent = maybe_sent


class QueueExpired(Exception):
    """The event queue is gone on the server (BAD_EVENT_QUEUE_ID).  Register a new one."""


# --------------------------------------------------------------------------------------------
# Seat and session identity
# --------------------------------------------------------------------------------------------

def normalise_seat(name: str) -> str:
    """Upper-case a seat name, refusing anything that is not a plain token (it names a directory)."""
    cleaned = (name or "").strip()
    if not _SEAT_RE.match(cleaned):
        raise UsageError("seat name %r is not valid; use letters, digits, hyphen or underscore (e.g. CLAUDE)" % cleaned)
    return cleaned.upper()


def credential_file_name(seat: str) -> str:
    """CLAUDE -> Claude-zuliprc, BF-BUILDER -> BF-Builder-zuliprc, MM -> MM-zuliprc.  Parts of
    SHORT_PART_MAX letters or fewer stay upper case; longer parts are Title Case."""
    seat = seat.upper()
    stem = SEAT_FILE_OVERRIDES.get(seat)
    if stem is None:
        parts = seat.split("-")
        stem = "-".join(part if len(part) <= SHORT_PART_MAX else part[:1].upper() + part[1:].lower()
                        for part in parts)
    return stem + "-zuliprc"


def seat_from_rc_path(path: str | os.PathLike[str]) -> str | None:
    """Derive a seat from a file named <Seat>-zuliprc, or None when the name does not say.  The
    inverse of credential_file_name: MM-zuliprc is the seat MM, BF-Builder-zuliprc is BF-BUILDER."""
    name = Path(path).name
    if name.lower().endswith("-zuliprc") and len(name) > len("-zuliprc"):
        stem = name[: -len("-zuliprc")]
        try:
            seat = normalise_seat(stem)
        except UsageError:
            return None
        for known, known_stem in SEAT_FILE_OVERRIDES.items():
            if known_stem.upper() == seat:
                return known
        return seat
    return None


def session_tag(session_id: str | None) -> str | None:
    """First 8 characters of the id with hyphens removed, lowercase.  Only [a-z0-9] survive,
    because the tag names a state directory."""
    if not session_id:
        return None
    cleaned = re.sub(r"[^a-z0-9]", "", session_id.replace("-", "").lower())[:8]
    return cleaned or None


# --------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    email: str
    key: str = field(repr=False)
    site: str
    source: str  # a file path, or "env"


def realm_url(env: Mapping[str, str]) -> str:
    return (env.get(ENV_REALM) or REALM).strip().rstrip("/")


def _normalise_site(site: str) -> str:
    site = site.strip().rstrip("/")
    if "://" not in site:
        site = "https://" + site
    return site


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def read_zuliprc(path: Path) -> Credentials:
    """Parse a zuliprc ([api] email, key, site).  Refuses a group- or world-accessible file.
    Parser errors are replaced by a message naming only the path: configparser quotes the
    offending line, and that line may be the key."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise CredentialError("credential file not found: %s" % path) from None
    except OSError as exc:
        raise CredentialError("cannot stat credential file %s (%s)" % (path, exc.strerror or "error")) from None
    if not stat.S_ISREG(st.st_mode):
        raise CredentialError("credential path is not a regular file: %s" % path)
    if st.st_mode & 0o077:
        raise CredentialError(
            "credential file %s is accessible to group or other (mode %04o); run: chmod 600 %s"
            % (path, stat.S_IMODE(st.st_mode), path))
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except (configparser.Error, UnicodeDecodeError, OSError):
        raise CredentialError(
            "cannot parse %s: expected an INI file with an [api] section holding email, key and site" % path) from None
    if not parser.has_section("api"):
        raise CredentialError("%s has no [api] section" % path)
    values = {name: _strip_quotes(parser.get("api", name, fallback="")) for name in ("email", "key", "site")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise CredentialError("%s [api] section is missing: %s" % (path, ", ".join(missing)))
    return Credentials(email=values["email"], key=values["key"], site=_normalise_site(values["site"]), source=str(path))


def env_credentials(env: Mapping[str, str], *, email_env: str, key_env: str, site_env: str,
                    label: str) -> Credentials:
    """One seat's credentials from the environment, under the variable names its config gives
    (the listener's server instance, where Infisical syncs each bot's email and key into the
    container).  A missing variable is named in the error; a value never is."""
    values = {name: _strip_quotes(env.get(name) or "") for name in (email_env, key_env, site_env)}
    missing = [name for name in (email_env, key_env, site_env) if not values[name]]
    if missing:
        raise CredentialError("seat %s: environment variable%s not set: %s" % (
            label, "" if len(missing) == 1 else "s", ", ".join(missing)))
    if any(ch in values[key_env] for ch in "\r\n\x00"):
        raise CredentialError("seat %s: %s holds a control character; refusing it" % (label, key_env))
    return Credentials(email=values[email_env], key=values[key_env], site=_normalise_site(values[site_env]),
                       source="env %s" % key_env)


def resolve_credentials(env: Mapping[str, str], *, rc_arg: str | None, seat: str | None,
                        home: Path | None = None) -> Credentials:
    """Apply the credential order.  An explicit --rc or ZULIP_RC that cannot be read is an error;
    it never falls through to the next source."""
    home = home if home is not None else Path(env.get("HOME") or os.path.expanduser("~"))
    if rc_arg:
        return read_zuliprc(Path(rc_arg).expanduser())
    if env.get("ZULIP_RC"):
        return read_zuliprc(Path(env["ZULIP_RC"]).expanduser())
    tried: list[str] = []
    if seat:
        secrets_dir = Path(env.get(ENV_SECRETS_DIR) or (home / ".secrets" / "Zulip")).expanduser()
        candidate = secrets_dir / credential_file_name(seat)
        tried.append(str(candidate))
        if candidate.exists():
            return read_zuliprc(candidate)
    triple = {name: (env.get(name) or "").strip() for name in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE")}
    if all(triple.values()):
        return Credentials(email=triple["ZULIP_EMAIL"], key=triple["ZULIP_API_KEY"],
                           site=_normalise_site(triple["ZULIP_SITE"]), source="env")
    missing = [name for name, value in triple.items() if not value]
    where = ("; looked for %s" % ", ".join(tried)) if tried else ""
    raise CredentialError("no Zulip credentials found%s; or set ZULIP_EMAIL, ZULIP_API_KEY and ZULIP_SITE "
                          "(missing: %s), or pass --rc PATH" % (where, ", ".join(missing)))


def _host_key(url: str) -> tuple[str, int | None, str]:
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None)
    except ValueError:
        return ("", None, "")
    return (host, port, parts.scheme)


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def verify_realm(creds: Credentials, realm: str) -> None:
    """Refuse credentials whose site host is not the realm host, and plain http to a non-loopback host."""
    c_host, c_port, _ = _host_key(creds.site)
    r_host, r_port, r_scheme = _host_key(realm)
    if not r_host or not c_host:
        raise CredentialError("could not read a host from the realm or from the credential site (%s)" % creds.source)
    if (c_host, c_port) != (r_host, r_port):
        raise CredentialError(
            "credential site host %s (bot %s, from %s) does not match the realm host %s; refusing to send it there"
            % (c_host, creds.email, creds.source, r_host))
    if r_scheme != "https" and not _is_loopback(r_host):
        raise CredentialError("refusing to send credentials over %s to %s; the realm must be https" % (r_scheme, r_host))


# --------------------------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------------------------

def encode_params(params: Mapping[str, Any] | None) -> str:
    """Form-encode.  Strings go through unchanged; bools, ints and floats become Zulip's
    textual forms; lists and dicts are JSON-encoded (narrow, subscriptions, event_types)."""
    pairs: list[tuple[str, str]] = []
    for name, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        pairs.append((name, text))
    return urllib.parse.urlencode(pairs)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would re-send the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class ZulipClient:
    """Minimal Zulip REST client.  Thread-safe: each request builds its own urllib objects."""

    def __init__(self, creds: Credentials, realm: str, *, timeout: float = 30.0, events_timeout: float = 100.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.creds = creds
        self.realm = realm.rstrip("/")
        self.base = self.realm + "/api/v1/"
        self.timeout = timeout
        self.events_timeout = events_timeout
        self.sleep = sleep
        token = base64.b64encode(("%s:%s" % (creds.email, creds.key)).encode("utf-8")).decode("ascii")
        self._auth = "Basic " + token
        self._hidden = [value for value in (creds.key, token) if value]
        host = _host_key(self.realm)[0]
        handlers: list[Any] = [_NoRedirect()]
        if _is_loopback(host):
            handlers.insert(0, urllib.request.ProxyHandler({}))  # a proxy must not see local traffic
        self._opener = urllib.request.build_opener(*handlers)

    # ---- secrecy ------------------------------------------------------------------------
    def scrub(self, text: str) -> str:
        for hidden in self._hidden:
            text = text.replace(hidden, "[redacted]")
        return text

    # ---- core ---------------------------------------------------------------------------
    def request(self, method: str, path: str, params: Mapping[str, Any] | None = None, *,
                timeout: float | None = None, long_poll: bool = False) -> dict[str, Any]:
        """One API call.  429 is retried (Retry-After, at most 3 times).  GET also retries
        network failures and a 502, 503 or 504 twice (1s, then 3s); a long poll does not retry a
        timeout, because a quiet queue is normal.  Non-GET requests are never retried on a
        network failure or a gateway error: a POST /messages that timed out may have posted."""
        method = method.upper()
        query = encode_params(params)
        url = self.base + path.lstrip("/")
        data: bytes | None = None
        if method == "GET":
            if query:
                url += "?" + query
        elif method == "DELETE":
            # Zulip reads DELETE parameters from the body; the query string is sent too so a
            # server variant that only looks there still sees the queue id.
            if query:
                url += "?" + query
                data = query.encode("utf-8")
        else:
            data = query.encode("utf-8")
        headers = {"Authorization": self._auth, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        rate_retries = 0
        net_retries = 0
        while True:
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(request, timeout=timeout if timeout is not None else self.timeout) as response:
                    body = response.read()
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = exc.read()
                except (OSError, http.client.HTTPException):
                    body = b""
                finally:
                    exc.close()
                if status == 429 and rate_retries < MAX_RATE_LIMIT_RETRIES:
                    rate_retries += 1
                    self.sleep(self._retry_after(exc.headers, body))
                    continue
                if method == "GET" and status in GATEWAY_STATUSES and net_retries < len(GET_RETRY_BACKOFF):
                    self.sleep(GET_RETRY_BACKOFF[net_retries])
                    net_retries += 1
                    continue
                raise self._api_error(status, body) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                is_timeout = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
                retryable = (method == "GET" and net_retries < len(GET_RETRY_BACKOFF)
                             and not (long_poll and is_timeout))
                if retryable:
                    self.sleep(GET_RETRY_BACKOFF[net_retries])
                    net_retries += 1
                    continue
                refused = isinstance(reason, ConnectionRefusedError)
                detail = "timed out" if is_timeout else self.scrub(str(reason) or type(reason).__name__)
                raise NetworkError(
                    "%s %s failed: %s%s" % (method, path, detail,
                                            "; Zulip may or may not have received it" if method != "GET" and not refused else ""),
                    timeout=is_timeout, maybe_sent=(method != "GET" and not refused)) from None
            return self._parse(status, body)

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.request("GET", path, params, **kw)

    def post(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.request("POST", path, params, **kw)

    def patch(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.request("PATCH", path, params, **kw)

    def delete(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.request("DELETE", path, params, **kw)

    # ---- helpers ------------------------------------------------------------------------
    def _retry_after(self, headers: Any, body: bytes) -> float:
        value: Any = None
        try:
            value = headers.get("Retry-After") if headers is not None else None
        except Exception:  # noqa: BLE001 - a malformed header object is not worth failing over
            value = None
        if value is None:
            try:
                value = json.loads(body.decode("utf-8")).get("retry-after")
            except (ValueError, AttributeError, UnicodeDecodeError):
                value = None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = 1.0
        return max(0.0, min(seconds, RETRY_AFTER_CAP))

    def _api_error(self, status: int, body: bytes) -> ApiError:
        data: dict[str, Any] = {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, UnicodeDecodeError):
            pass
        msg = self.scrub(str(data.get("msg") or "HTTP %d" % status))
        code = data.get("code")
        # The code goes into the exception text too, so it is scrubbed like the message.
        return ApiError(msg, code=self.scrub(str(code)) if code else None, status=status, data=data)

    def _parse(self, status: int, body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Zulip returned a response that is not JSON (HTTP %d)" % status, status=status) from None
        if not isinstance(parsed, dict):
            raise ApiError("Zulip returned an unexpected response shape (HTTP %d)" % status, status=status)
        if parsed.get("result") == "error":
            raise self._api_error(status, body)
        return parsed


# --------------------------------------------------------------------------------------------
# Messages and event queues
# --------------------------------------------------------------------------------------------

def message_channel(message: Mapping[str, Any]) -> str:
    recipient = message.get("display_recipient")
    return recipient if isinstance(recipient, str) else "DM"


def message_topic(message: Mapping[str, Any]) -> str:
    return str(message.get("subject") if message.get("subject") is not None else message.get("topic") or "")


class EventQueue:
    """One Zulip event queue.  `narrow` is the pair-shaped list [["channel", C], ["topic", T]]
    (or None for everything the bot receives).  Messages come back with `flags` copied in from
    the event, so callers see the same shape as GET /messages.

    `event_types` defaults to ["message"] (the CLI's `wait` and `listen`).  The listener daemon
    registers more types and reads every event with `poll_events`.  `register_state` keeps the
    register response (initial user and topic state, the long-poll timeout), and `last_event_at`
    is the monotonic time of the last event of any kind, heartbeats included."""

    def __init__(self, client: ZulipClient, narrow: list[list[str]] | None,
                 event_types: list[str] | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.narrow = narrow
        self.event_types = list(event_types or ["message"])
        self.queue_id: str | None = None
        self.last_event_id: int = -1
        self.register_state: dict[str, Any] = {}
        self.clock = clock
        self.last_event_at: float | None = None

    def register(self) -> None:
        params: dict[str, Any] = {"event_types": self.event_types, "apply_markdown": False}
        if self.narrow is not None:
            params["narrow"] = self.narrow
        result = self.client.post("register", params)
        self.queue_id = str(result["queue_id"])
        self.last_event_id = int(result.get("last_event_id", -1))
        self.register_state = result
        self.last_event_at = self.clock()

    @property
    def longpoll_timeout(self) -> float | None:
        """The server's long-poll timeout from the register response, when it gave one."""
        value = self.register_state.get("event_queue_longpoll_timeout_seconds")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None

    def poll_events(self, timeout: float) -> list[dict[str, Any]]:
        """Block up to `timeout` seconds; return every event except heartbeats, as received.
        Message events get the event's `flags` copied into the message.  Raises QueueExpired."""
        if self.queue_id is None:
            raise QueueExpired()
        try:
            result = self.client.get("events", {"queue_id": self.queue_id, "last_event_id": self.last_event_id},
                                     timeout=max(0.05, timeout), long_poll=True)
        except ApiError as exc:
            if exc.code == "BAD_EVENT_QUEUE_ID":
                self.queue_id = None
                raise QueueExpired() from None
            raise
        except NetworkError as exc:
            if exc.timeout:
                return []
            raise
        events: list[dict[str, Any]] = []
        raw = result.get("events", [])
        if raw:
            self.last_event_at = self.clock()
        for event in raw:
            event_id = event.get("id")
            if isinstance(event_id, int):
                self.last_event_id = max(self.last_event_id, event_id)
            if event.get("type") == "heartbeat":
                continue
            if event.get("type") == "message" and isinstance(event.get("message"), dict):
                message = dict(event["message"])
                message["flags"] = list(event.get("flags") or message.get("flags") or [])
                event = {**event, "message": message}
            events.append(event)
        return events

    def poll(self, timeout: float) -> list[dict[str, Any]]:
        """Block up to `timeout` seconds for events.  Returns the message events (possibly none).
        Raises QueueExpired when the server has dropped the queue."""
        return [event["message"] for event in self.poll_events(timeout)
                if event.get("type") == "message" and isinstance(event.get("message"), dict)]

    def close(self) -> str | None:
        """Best-effort DELETE of the queue; never raises.  Returns a one-line problem (never the
        key) when the delete failed, or None.  A queue the server already dropped is not a problem."""
        queue_id, self.queue_id = self.queue_id, None
        if queue_id is None:
            return None
        try:
            self.client.delete("events", {"queue_id": queue_id}, timeout=5.0)
        except ApiError as exc:
            return None if exc.code == "BAD_EVENT_QUEUE_ID" else str(exc)
        except (AgentSyncError, OSError) as exc:
            return str(exc)
        return None
