"""Sentry Crons heartbeat:  the listener reports to Sentry that it is running.

Owner ask (Sat, Oct 10, 2026):  "have the listener report if it is running on Mac and Coolify to Sentry".
The daemon checks in to one Sentry cron monitor per instance, `agent-sync-listener-<instance>` (`mac` and
`server`), in the fleet-infra project.  A check-in goes out about every five minutes from inside the daemon
(no LaunchAgent, no cron, no SDK):  `ok` when the listener is healthy, `error` when it is degraded.  The
monitor is created by the first check-in (its `monitor_config` upserts), with a five-minute interval, a
ten-minute margin and a failure threshold of two, so a listener that goes silent (crashed, host off, stuck
in a restart loop) opens a Sentry issue instead of nobody noticing.

Degraded means the status the daemon already writes shows a problem:  an enabled seat is red (its
credentials are refused, or its queue has been down for 30 minutes), the owner is not pinned, or the config
has errors.  A degraded state that lasts two beats in a row also sends one warning event that names the reasons
(once per distinct problem, not once per beat), grouped into one issue per instance, so the alert says why.  A
single blip (a queue that failed one poll) sends an `error` check-in and no event.

Secrets:  the DSN comes from `AGENT_SYNC_SENTRY_DSN` or `SENTRY_FLEET_DSN` in the environment (the server:
Infisical `prod` `/zulip`, loaded at start by `server/infisical_env.py`), else from the same two names in
the chmod-600 file `~/.secrets/agent-sync.env` (the Mac;  only those two names are read from it).  The DSN
is never logged, never put in an error and never written to status.json.  With no DSN the heartbeat is off
and says so once.  A send never blocks the daemon:  it runs on its own short-lived thread, and a failure
backs off (30 seconds, doubling up to the five-minute interval) and logs at most once every 30 minutes.

Standard library only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from . import __version__

INTERVAL_SECONDS = 300.0
BACKOFF_FIRST_SECONDS = 30.0
FAILURE_LOG_SECONDS = 30 * 60.0
TIMEOUT_SECONDS = 10.0
ENV_DSN_NAMES = ("AGENT_SYNC_SENTRY_DSN", "SENTRY_FLEET_DSN")  # the first wins
ENV_FILE = "AGENT_SYNC_SENTRY_ENV_FILE"
ENV_OFF = "AGENT_SYNC_SENTRY_HEARTBEAT"       # 0, off, false or no turns it off
ENV_ENVIRONMENT = "AGENT_SYNC_SENTRY_ENVIRONMENT"
DEFAULT_ENVIRONMENT = "production"            # the fleet's one Sentry environment tag (sentry-fleet-integration plan)
DEFAULT_ENV_FILE = os.path.join(".secrets", "agent-sync.env")
MONITOR_PREFIX = "agent-sync-listener-"
REASON_LIMIT = 120
REASONS_SHOWN = 4

Send = Callable[[str, bytes, Mapping[str, str], float], int]


class Dsn:
    """A parsed Sentry DSN.  repr and str never show the key."""

    def __init__(self, scheme: str, host: str, project: str, key: str) -> None:
        self.scheme, self.host, self.project, self.key = scheme, host, project, key

    @property
    def envelope_url(self) -> str:
        return "%s://%s/api/%s/envelope/" % (self.scheme, self.host, self.project)

    def __repr__(self) -> str:
        return "Dsn(%s project %s)" % (self.host, self.project)

    __str__ = __repr__


def parse_dsn(text: str) -> Dsn | None:
    """https://<public key>@<host>/<project id>;  plain http only for a loopback host (a test server)."""
    try:
        parts = urllib.parse.urlsplit(text.strip())
        host = parts.hostname or ""
        port = parts.port
        key = parts.username or ""
    except ValueError:
        return None
    project = parts.path.strip("/")
    if not host or not key or not project.isdigit() or parts.password:
        return None
    if parts.scheme == "http":
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return None
    elif parts.scheme != "https":
        return None
    return Dsn(parts.scheme, "%s:%d" % (host, port) if port else host, project, key)


def read_env_file(path: str, names: tuple[str, ...] = ENV_DSN_NAMES) -> dict[str, str]:
    """Only the named keys from a KEY=value file (quotes stripped);  nothing else in it is read into memory."""
    found: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                name, sep, value = line.partition("=")
                name = name.strip()
                if sep and name in names and name not in found:
                    found[name] = value.strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass
    return found


def resolve_dsn(env: Mapping[str, str], home: str) -> str:
    """The DSN text, or ''.  Environment first, then the secrets file."""
    for name in ENV_DSN_NAMES:
        if (env.get(name) or "").strip():
            return env[name].strip()
    path = os.path.expanduser(env.get(ENV_FILE) or os.path.join(home, DEFAULT_ENV_FILE))
    file_values = read_env_file(path)
    for name in ENV_DSN_NAMES:
        if file_values.get(name):
            return file_values[name]
    return ""


def monitor_slug(instance: str) -> str:
    clean = "".join(c if c.isalnum() else "-" for c in (instance or "mac").lower()).strip("-")
    return MONITOR_PREFIX + (clean or "mac")


def degraded_reasons(status: Mapping[str, Any], scrub: Callable[[str], str] = lambda s: s) -> list[str]:
    """Short, scrubbed reasons the listener is not healthy.  [] means healthy."""
    def clip(text: Any) -> str:
        return scrub(" ".join(str(text).split()))[:REASON_LIMIT]

    reasons: list[str] = []
    for seat, info in sorted((status.get("seats") or {}).items()):
        red = (info or {}).get("red") or []
        if red:
            reasons.append("seat %s red: %s" % (seat, clip(red[0])))
    if not status.get("owner_user_id"):
        reasons.append("owner not pinned")
    errors = status.get("config_errors") or []
    if errors:
        reasons.append("%d config error(s): %s" % (len(errors), clip(errors[0])))
    return reasons


def reason_key(reasons: list[str]) -> tuple[str, ...]:
    """What makes two sets of reasons "the same problem":  the reasons with their numbers removed, so a seat that
    has been down for 31, then 36, then 41 minutes is one problem, not three."""
    return tuple(re.sub(r"\d+", "#", reason) for reason in reasons)


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(*, slug: str, instance: str, environment: str, ok: bool, reasons: list[str], send_event: bool,
                   now: float, version: str = __version__) -> bytes:
    """One Sentry envelope:  the cron check-in (with its monitor_config upsert), plus, when `send_event`,
    one warning event that names the reasons.  The DSN is not in it (the key travels in X-Sentry-Auth)."""
    items: list[tuple[dict[str, Any], dict[str, Any]]] = [({"type": "check_in"}, {
        "check_in_id": uuid.uuid4().hex,
        "monitor_slug": slug,
        "status": "ok" if ok else "error",
        "environment": environment,
        "monitor_config": {
            "schedule": {"type": "interval", "value": int(INTERVAL_SECONDS // 60), "unit": "minute"},
            "checkin_margin": 10,
            "max_runtime": 5,
            "timezone": "America/Chicago",
            "failure_issue_threshold": 2,
            "recovery_threshold": 1,
        },
    })]
    if send_event and reasons:
        shown = reasons[:REASONS_SHOWN]
        more = len(reasons) - len(shown)
        items.append(({"type": "event"}, {
            "event_id": uuid.uuid4().hex,
            "timestamp": _iso(now),
            "platform": "python",
            "level": "warning",
            "logger": "agent-sync.listener",
            "message": "agent-sync listener (%s) degraded: %s%s" % (instance, "; ".join(shown),
                                                                   "; +%d more" % more if more else ""),
            "environment": environment,
            "release": "agent-sync@%s" % version,
            "tags": {"app": "agent-sync-listener", "instance": instance},
            "fingerprint": ["agent-sync-listener", instance, "degraded"],
        }))
    lines = [json.dumps({"sent_at": _iso(now)})]
    for header, payload in items:
        body = json.dumps(payload)
        lines.append(json.dumps(dict(header, length=len(body.encode("utf-8")))))
        lines.append(body)
    return ("\n".join(lines) + "\n").encode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # the auth header never follows a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_send(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
    """POST and return the HTTP status.  Raises OSError (URLError, timeout, a refused connection) on a network
    failure.  A 4xx or 5xx is returned as a status, not raised."""
    req = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            resp.read(1024)
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code)


class SentryHeartbeat:
    """Called from the daemon's tick.  `tick(status)` is cheap:  it only builds and sends when a beat is due,
    and the send runs on its own thread.  `status` is a zero-argument callable returning the status dict, so
    a non-due tick costs nothing."""

    def __init__(self, *, env: Mapping[str, str], home: str, clock: Callable[[], float],
                 log: Callable[..., None], scrub: Callable[[str], str] = lambda s: s,
                 hide: Callable[[list[str]], None] | None = None, send: Send = http_send,
                 background: bool = True) -> None:
        self.env = env
        self.home = home
        self.now = clock
        self.log = log
        self.scrub = scrub
        self.hide = hide
        self.send = send
        self.background = background
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.next_at = 0.0                       # first beat on the first tick after start
        self.failures = 0
        self.last_failure_log = -1e18
        self.announced = ""                      # "", "on", "off" or "bad-dsn":  what the log already said
        self.seen_key: tuple[str, ...] = ()      # reason_key of the last delivered beat
        self.sent_key: tuple[str, ...] = ()      # reason_key Sentry already has an event for
        self.ever_ok = False
        self.last_ok_at: float | None = None
        self.hidden_dsn = ""

    # ---- configuration ---------------------------------------------------------------------
    def enabled(self) -> bool:
        return (self.env.get(ENV_OFF) or "").strip().casefold() not in ("0", "off", "false", "no")

    def _dsn(self) -> Dsn | None:
        if not self.enabled():
            self._announce("off", "sentry-heartbeat-off", why="disabled by %s" % ENV_OFF)
            return None
        text = resolve_dsn(self.env, self.home)
        if not text:
            self._announce("off", "sentry-heartbeat-off", why="no DSN (%s)" % " or ".join(ENV_DSN_NAMES))
            return None
        if text != self.hidden_dsn:
            self.hidden_dsn = text
            if self.hide:
                self.hide([text])
        dsn = parse_dsn(text)
        if dsn is None:
            self._announce("bad-dsn", "sentry-heartbeat-off", why="the DSN is not a valid https Sentry DSN")
            return None
        if self.hide:
            self.hide([dsn.key])  # the key alone is scrubbed too, in case an error quotes it
        return dsn

    def _announce(self, state: str, event: str, **fields: Any) -> None:
        if self.announced != state:
            self.announced = state
            self.log(event, **fields)

    # ---- the beat --------------------------------------------------------------------------
    def tick(self, status: Callable[[], Mapping[str, Any]]) -> bool:
        """True when a beat was started."""
        now = self.now()
        if now < self.next_at:
            return False
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
        dsn = self._dsn()
        if dsn is None:
            self.next_at = now + INTERVAL_SECONDS  # look again in five minutes:  the file may have gained the DSN
            return False
        snapshot = status()
        instance = str(snapshot.get("instance") or "mac")
        reasons = degraded_reasons(snapshot, self.scrub)
        slug = monitor_slug(instance)
        environment = (self.env.get(ENV_ENVIRONMENT) or DEFAULT_ENVIRONMENT).strip() or DEFAULT_ENVIRONMENT
        key = reason_key(reasons)
        send_event = bool(reasons) and key == self.seen_key and key != self.sent_key
        body = build_envelope(slug=slug, instance=instance, environment=environment, ok=not reasons, reasons=reasons,
                              send_event=send_event, now=now)
        headers = {"Content-Type": "application/x-sentry-envelope",
                   "X-Sentry-Auth": "Sentry sentry_version=7, sentry_client=agent-sync-listener/%s, sentry_key=%s"
                                    % (__version__, dsn.key)}
        # Provisional next time, so a slow send is not started twice;  the outcome sets the real one.
        self.next_at = now + INTERVAL_SECONDS
        job = (dsn.envelope_url, body, headers, now, slug, reasons, key, send_event)
        if self.background:
            thread = threading.Thread(target=self._deliver, args=job, name="sentry-heartbeat", daemon=True)
            with self.lock:
                self.thread = thread
            thread.start()
        else:
            self._deliver(*job)
        return True

    def wait(self, timeout: float = 5.0) -> None:
        with self.lock:
            thread = self.thread
        if thread is not None:
            thread.join(timeout)

    def _deliver(self, url: str, body: bytes, headers: Mapping[str, str], started: float, slug: str,
                 reasons: list[str], key: tuple[str, ...], sent_event: bool) -> None:
        failure = ""
        try:
            status = self.send(url, body, headers, TIMEOUT_SECONDS)
            if not 200 <= status < 300:
                failure = "HTTP %d" % status
        except (OSError, ValueError) as exc:     # URLError, timeouts and refused connections are OSErrors
            failure = type(exc).__name__
        except Exception as exc:                 # noqa: BLE001 - the heartbeat must never take the daemon down
            failure = type(exc).__name__
        with self.lock:
            if failure:
                self.failures += 1
                delay = min(BACKOFF_FIRST_SECONDS * (2 ** (self.failures - 1)), INTERVAL_SECONDS)
                self.next_at = started + delay
                first = self.failures == 1
                if first or started - self.last_failure_log >= FAILURE_LOG_SECONDS:
                    self.last_failure_log = started
                    self.log("sentry-heartbeat-failed", monitor=slug, error=self.scrub(failure),
                             attempts=self.failures, retry_in=int(delay))
                return
            recovered = self.failures > 0
            first_beat = not self.ever_ok
            self.failures = 0
            self.ever_ok = True
            self.last_ok_at = started
            self.next_at = started + INTERVAL_SECONDS
            self.seen_key = key
            if sent_event or not reasons:
                self.sent_key = key if reasons else ()
        if first_beat or recovered:
            self.log("sentry-heartbeat-ok", monitor=slug, state="degraded" if reasons else "ok",
                     recovered_after_failures=recovered)
