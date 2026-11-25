"""Adapters and side channels for the listener daemon:  the remote routine wake (`http`), the
notify-owner banner and owner queue, the `claude agents --json` lease-liveness probe, and the
OpenRouter transport the optional decider declares.

The listener starts no model (owner ruling, Nov 10, 2026):  a seat has a routine (`wake = "http"`)
or it only captures (`wake = "inbox"`).  The `claude`, `muse` and `openrouter` wake types and their
fallbacks are retired, and their runtimes were deleted with them.

The `http` adapter sends one fixed JSON body to a remote routine webhook whose URL and sender key
come from environment variables named in the seat's config.  The body carries the trigger's
metadata and a 2,000-character excerpt wrapped in the untrusted-content markers; the routine itself
replies in Zulip with the bot's own key.  2xx is accepted; a 5xx or a network failure is retried
with backoff (3 retries, so at most 4 requests, the identical body each time); a 4xx or a redirect
is never retried.  The URL, its query string, the key and the signature are never logged.

Every subprocess gets a list argv, never a shell string, and message content is never executed.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from . import live as L
from . import service_identity as SI
from . import zulip as Z
from .config import RoutineConfig
from .identity import WAKE_LAUNCHER

NOTIFY_CUT = 120
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_wake_path(home: str) -> str:
    return "%s/.local/bin:/opt/homebrew/bin:/usr/bin:/bin" % home


# --------------------------------------------------------------------------------------------
# Lease liveness:  `claude agents --json` (Daemon.agent_kinds)
# --------------------------------------------------------------------------------------------
#
# Not a wake:  the listener starts no model (owner ruling, Nov 10, 2026).  The daemon asks the
# pinned claude binary which of its processes are interactive sessions, so a `claude-code` lease
# is only alive while its process is one.  The pin file (`<SEAT>/wake-pin.json`, written by
# record_pin through `simple-chat daemon pin-claude`) says which binary that is and whether its listing
# was found usable (`agents_ok`).


def resolve_claude(explicit: str | None, wake_path: str) -> str | None:
    """The claude binary:  the configured absolute path, else `claude` on the fixed wake PATH."""
    if explicit:
        return explicit if os.path.isabs(explicit) and os.access(explicit, os.X_OK) else None
    found = shutil.which("claude", path=wake_path)
    return os.path.abspath(found) if found else None


def claude_env(
    env: Mapping[str, str], home: str, wake_path: str, *, seat: str | None = None
) -> dict[str, str]:
    """`env -i` plus a fixed list:  every CLAUDE_CODE_* (the messaging socket and token), AGENT_*,
    ZULIP_* and ANTHROPIC_API_KEY are dropped, so the run uses the claude.ai login.

    With `seat`, the listener acts as the reference launcher (FLEET-PROTOCOL § Seats and Identity):  it sets AGENT_LAUNCHER=simple-tools-wake, AGENT_LAUNCH_SEAT and AGENT_SEAT
    to the seat, and SIMPLE_TOOLS_ATTACH=0, fresh, after every inherited identity variable was
    dropped.  No production caller passes `seat` since the listener stopped starting models (owner
    ruling, Nov 10, 2026):  the lease-liveness probe passes none, and the branch stays with its
    tests (test_seat_precedence WakeLauncherTests)."""
    user = env.get("USER") or env.get("LOGNAME") or os.path.basename(home)
    out = {
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "LANG": "en_US.UTF-8",
        "TMPDIR": env.get("TMPDIR") or "/tmp",
        "PATH": wake_path,
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
    }
    if seat:
        out.update(
            {
                "AGENT_LAUNCHER": WAKE_LAUNCHER,
                "AGENT_LAUNCH_SEAT": seat,
                "AGENT_SEAT": seat,
                "SIMPLE_TOOLS_ATTACH": "0",
            }
        )
    return out


def parse_agents(text: str | None) -> list[dict[str, Any]] | None:
    """Rows of `claude agents --json`, or None when the output is not a listing this code knows."""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    rows = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return None
    return rows


def run_agents(argv: Sequence[str], env: Mapping[str, str], cwd: str) -> str | None:
    """`claude agents --json`, stdin closed, 10 seconds at most.  stdout, or None on any failure."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def binary_pin(claude: str) -> str:
    return os.path.realpath(claude)


def pinned(paths: L.SeatPaths, claude: str | None) -> bool:
    if not claude:
        return False
    return L.read_json(paths.wake_pin).get("claude") == binary_pin(claude)


def agents_pinned(paths: L.SeatPaths, claude: str | None) -> bool:
    """True when the pinned binary is `claude` and the pin says its `agents --json` is usable."""
    return pinned(paths, claude) and L.read_json(paths.wake_pin).get("agents_ok") is True


def record_pin(
    paths: L.SeatPaths,
    claude: str,
    now: float,
    *,
    agents_ok: bool = False,
    init_tools: list[Any] | None = None,
) -> None:
    L.write_json(
        paths.wake_pin,
        {
            "claude": binary_pin(claude),
            "at": now,
            "agents_ok": bool(agents_ok),
            "init_tools": init_tools,
        },
    )


# --------------------------------------------------------------------------------------------
# OpenRouter chat completions:  the transport the optional decider declares
# --------------------------------------------------------------------------------------------
#
#   POST https://openrouter.ai/api/v1/chat/completions      (one call, no stream, no retry, no tools)
#
# The listener starts no model (owner ruling, Nov 10, 2026):  the `openrouter` wake type and its
# fallback are retired, and nothing in the daemon calls this any more.  It stays for the optional
# decider (`[decider.consult_backup]`, config.DECIDER_OPENROUTER), the one caller its declaration
# selects, so the request shape, the answer reader and the key holder keep their tests.
#
# The caller's system message and prompt are the two messages, `response_format` is the strict
# json_schema the caller passes, and `provider.require_parameters` keeps OpenRouter from routing to
# a provider that cannot honor it.  Nothing in the request offers a tool, so the model can only
# return text, and the caller decides what to do with it.
#
# SECRETS.  The key is the Operator's own OpenRouter key, read from Fleet-OPS `/wake` at the moment of
# a call (service_identity.openrouter_api_key), hidden from every log line and scan the moment it is
# read, and sent in one `Authorization` header.  It is never an argument, a ledger field, a fixture
# or an exception message, and no response or request body is ever put in an error:  response
# bodies can echo the prompt, so a failure carries a fixed phrase and a status number only.


class RunResult:
    """What one OpenRouter call returned:  the parsed answer or why there is none, with counts only."""

    def __init__(self) -> None:
        self.refused: str | None = None  # why the run was refused before any call
        self.error: str | None = None  # an HTTP or network failure, a timeout, no result
        self.result: dict[str, Any] | None = None
        self.parsed: Any = None
        self.cost_usd = 0.0
        self.exit: int | None = None
        self.secs = 0.0
        self.models: list[str] = []
        self.stdout_len = 0
        self.stderr_len = 0
        self.stderr_sha = ""
        self.killed = False
        self.limited = False  # the provider refused for a quota, not an internal fault
        # The HTTP status of the call (None after a network failure) and the token counts the
        # response reported (counts only, never content).
        self.http_status: int | None = None
        self.usage: dict[str, int] | None = None

    @property
    def ok(self) -> bool:
        return self.refused is None and self.error is None and self.result is not None


def parse_muse_result(text: str | None) -> Any:
    """The answer text as JSON, or None:  json.loads with some leniency (a fenced or prefixed
    block still parses) rather than a separate grammar.  The name is the retired muse runtime's,
    whose answer this first read."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
    if "{" in body:
        body = body[body.find("{") :]
    try:
        return json.loads(body)
    except ValueError:
        return None


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_PATH = "/chat/completions"
OPENROUTER_TITLE = "Fleet listener wake"

# The hard overall deadline for one call, connect to last byte:  a call that has not answered in two
# minutes will not, and the caller should not wait on it.
OPENROUTER_TIMEOUT = 120.0
# The answer is one JSON object, so 4,096 output tokens is a generous bound for it plus whatever the
# model spends thinking, and it caps the output price of one call at 4,096 x $0.20 per million, under
# a tenth of a cent.  (It was sized on the retired wake result, about 4,000 characters of strings.)
OPENROUTER_MAX_TOKENS = 4096
OPENROUTER_TEMPERATURE = 0.2
# The lowest reasoning tier the runtime accepts.  `none` does not exist on the Meta provider (the muse
# runtime learned that with a run), so `minimal` is the floor;  `exclude` keeps the reasoning trace out
# of the response, which is only ever read for the answer and the token counts.
OPENROUTER_REASONING = {"effort": "minimal", "exclude": True}
OPENROUTER_SCHEMA_NAME = "wake_result"
OPENROUTER_RESPONSE_CAP = 256 * 1024
OPENROUTER_KEY_RETRY_SECONDS = 120.0
#: The status codes OpenRouter documents, in plain words.  The phrase is fixed text, so a failure
#: reason can never carry a response body.
OPENROUTER_HTTP_REASONS = {
    400: "request rejected",
    401: "key rejected",
    402: "credits exhausted",
    403: "forbidden or blocked by moderation",
    404: "no provider supports the request",
    408: "provider timed out",
    413: "request too large",
    422: "request unprocessable",
    429: "rate limited",
    502: "provider failed",
    503: "no provider available",
}


class OpenRouterUnavailable(Z.SimpleToolsError):
    """The OpenRouter key could not be read, so no call is made.  `reason` is a reason token or a
    short sentence naming the secret, never its value."""

    exit_code = 3

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OpenRouterKey:
    """The Operator's OpenRouter key, read from Fleet-OPS `/wake` when a call needs it.

    The read itself (service_identity.openrouter_api_key) goes through the fleet-secrets reader and
    gets its in-memory cache, its outage memory and its last-known-good fallback, so asking on every
    wake costs one vault read per cache window.  What this adds is the two things the daemon needs
    around it:  every value is handed to `hide` the moment it is read (so the log scrub and the secret
    scanner cover it for the rest of the process), and a failed read is remembered for
    `retry_seconds`, which lets the router thread ask `known_unavailable()` with no network and no
    thread, the way it asks the Operator's voice."""

    def __init__(
        self,
        env: Mapping[str, str],
        home: str,
        *,
        hide: Callable[[Iterable[str]], None] | None = None,
        fetch: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        retry_seconds: float = OPENROUTER_KEY_RETRY_SECONDS,
    ) -> None:
        self._env = dict(env)
        self._home = home
        self._hide = hide
        self._fetch = fetch
        self._clock = clock
        self._retry = float(retry_seconds)
        self._lock = threading.Lock()
        self._failed_until = -1e18
        self._failure = ""

    def get(self) -> str:
        """The key, or OpenRouterUnavailable.  Never logs or returns anything but the key itself."""
        with self._lock:
            if self._clock() < self._failed_until:
                raise OpenRouterUnavailable(self._failure)
        try:
            key = SI.openrouter_api_key(self._env, home=self._home, fetch=self._fetch)
        except Z.CredentialError as exc:
            reason = "the OpenRouter key is unavailable: %s" % exc
            with self._lock:
                self._failure = reason
                self._failed_until = self._clock() + self._retry
            raise OpenRouterUnavailable(reason) from None
        with self._lock:
            self._failure = ""
            self._failed_until = -1e18
        if self._hide is not None:
            # The bare value and the header form, so neither survives into a log line or a post.
            self._hide([key, "Bearer " + key])
        return key

    def known_unavailable(self) -> str | None:
        """The reason the last read failed while its window is still open, else None.  State only."""
        with self._lock:
            return self._failure if self._clock() < self._failed_until else None


def openrouter_url_problem(url: str) -> str | None:
    """Why an OpenRouter base URL is refused, or None.  https only (plain http only to a loopback test
    server), a host, no user:password part and no control character.  The production value is the
    module constant, so this guards the one injection point a test uses."""
    if any(ch in url for ch in "\r\n\x00\t "):
        return "it holds whitespace or a control character"
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return "it is not a valid URL"
    if parts.scheme not in ("https", "http"):
        return "it must be an https URL"
    if not host:
        return "it has no host"
    if parts.username is not None or parts.password is not None:
        return "it must not carry a user name or password"
    if parts.scheme == "http" and not Z._is_loopback(host.lower()):
        return "it must be https (plain http is accepted only for a loopback test server)"
    return None


# The same full slug shape the consult-backup declaration validates, so a malformed preset-like id
# never takes the preset branch (it keeps the plain request shape and fails validation elsewhere).
_OPENROUTER_PRESET_RE = re.compile(r"@preset/[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def openrouter_request_body(model: str, system: str, user: str, schema: str) -> dict[str, Any]:
    """The one request body, for the call and for the pin's fingerprint (with placeholders).

    No `tools`, no `tool_choice`, no `stream` and no plugin:  the model is offered nothing to call.
    For a plain model `provider.require_parameters` makes OpenRouter refuse a provider that cannot
    honor the strict `response_format` (or the reasoning setting) instead of quietly dropping it;  a
    preset-referencing model (`@preset/<slug>`) sends NO `provider` object, so the preset's own
    provider settings govern (see the comment below).  A preset model also sends "tools": [], which keeps the REQUEST tool-free but cannot
    disarm a tooled preset:  per the documented preset semantics request tools UNION with preset
    tools (a same-identity request tool overrides one preset entry, and an empty array overrides
    nothing), and the latest preset version is always served with no immutable version binding at
    inference.  No request field documented for this call removes a preset's tools, so that
    property rests on the preset's own zero-tool configuration, read by a person through an
    authenticated preset read (a management-key endpoint - it cannot live in this runtime without
    handing the runtime a workspace admin credential, and even a per-call read would race a
    dashboard edit).  The code-side backstops are "tool_choice": "none" and the response path:  a
    completion carrying a tool call is a fixed-phrase failure, so the consult settles as a hold and
    nothing is delivered.  Not verified:  whether "tool_choice": "none" also disables a preset's
    server tools.  Not covered:  any tool or plugin influence that leaves no tool call in the
    response (it is neither prevented nor detected here) - the named residual if a preset is ever
    changed to add tools or plugins.  A request that is tool-free is not thereby server tool-free."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": OPENROUTER_SCHEMA_NAME,
                "strict": True,
                "schema": json.loads(schema),
            },
        },
        "provider": {"require_parameters": True},
        "temperature": OPENROUTER_TEMPERATURE,
        "max_tokens": OPENROUTER_MAX_TOKENS,
        "reasoning": dict(OPENROUTER_REASONING),
        "stream": False,
    }
    if _OPENROUTER_PRESET_RE.fullmatch(model):
        body.pop("provider")
        # No `provider` object for a preset.  The owner chose the live preset (WhatsApp, Nov 7 2026,
        # 6:34 PM CT), so its provider block (order, fallbacks, exclusions, data collection) is his
        # dashboard's to set.  OpenRouter documents request-over-preset as a shallow merge, and
        # `provider` is one top-level field, so sending even `{"require_parameters": true}` is
        # expected to replace that whole block;  the Nov 7 two-call sample (sent with it) was served
        # by Nebius for the MiniMax model although the preset's order named only Meta and Minimax,
        # which fits a replaced block but does not prove one.  The cost of leaving it out:
        # `require_parameters` is no longer sent by this code, so refusing a provider that cannot
        # honor the strict `response_format` now depends on the preset's own provider setting.  The
        # answer is still parsed against the strict schema, so an ignored `response_format` ends as
        # a failed consult and a hold, not as a delivered answer.
        # Request tool-free is NOT server tool-free.  Request tools UNION with preset tools
        # (documented OpenRouter preset semantics), so the empty array cannot disarm a tooled
        # preset.  "tool_choice": "none" is meant to stop the model calling tools, but whether it
        # also covers a preset's server tools is unverified.  Only a returned tool call is detected
        # (the response path refuses it);  tool or plugin influence with no tool call is neither
        # prevented nor detected.  Server tool-free rests on the preset's own zero-tool,
        # zero-plugin configuration as read by a person, and a later edit to the live preset is
        # not seen by this code.
        body["tools"] = []
        body["tool_choice"] = "none"
    return body


def openrouter_http_reason(status: int) -> str:
    """A failure phrase for an HTTP status, from fixed text only (never from the body)."""
    detail = OPENROUTER_HTTP_REASONS.get(status)
    if detail is None:
        if 300 <= status < 400:
            detail = "redirects are refused"
        elif status >= 500:
            detail = "provider error"
    return "HTTP %d (%s)" % (status, detail) if detail else "HTTP %d" % status


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def openrouter_answer(data: bytes) -> tuple[str | None, dict[str, int], str | None, str | None]:
    """(the answer text, token counts, the model that served it, why there is no answer) from a 2xx body.

    Only an integer count, a model id and the message text are read.  A body that is not the
    chat-completions shape, a provider error carried in a 200, a tool call nobody offered, a refusal, an
    answer cut off at `max_tokens` and an empty answer are each a fixed phrase, and none puts a byte of
    the body in the reason."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, {}, None, "no parseable result"
    if not isinstance(obj, dict):
        return None, {}, None, "no parseable result"
    usage: dict[str, int] = {}
    raw_usage = obj.get("usage")
    if isinstance(raw_usage, dict):
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            count = _count(raw_usage.get(name))
            if count is not None:
                usage[name] = count
        details = raw_usage.get("completion_tokens_details")
        reasoning = _count(details.get("reasoning_tokens")) if isinstance(details, dict) else None
        if reasoning is not None:
            usage["reasoning_tokens"] = reasoning
    served = obj.get("model")
    served_model = clean_banner(served, 100) if isinstance(served, str) and served else None
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        error = obj.get("error")
        code = _count(error.get("code")) if isinstance(error, dict) else None
        if isinstance(error, dict):
            return (
                None,
                usage,
                served_model,
                "provider error%s" % (" (code %d)" % code if code is not None else ""),
            )
        return None, usage, served_model, "no parseable result"
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return None, usage, served_model, "no parseable result"
    if message.get("tool_calls") or message.get("function_call"):
        return None, usage, served_model, "the model returned a tool call (none were offered)"
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return None, usage, served_model, "the model refused"
    finish = choice.get("finish_reason")
    if finish == "length":
        return None, usage, served_model, "answer cut off at max_tokens"
    if finish == "content_filter":
        return None, usage, served_model, "answer blocked by a content filter"
    if finish == "error":
        return None, usage, served_model, "provider error mid-answer"
    content = message.get("content")
    if isinstance(content, list):  # content parts:  the text parts are the answer
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        return None, usage, served_model, "no parseable result"
    return content, usage, served_model, None


class OpenRouterRunner:
    """Makes one OpenRouter call and returns a RunResult.

    One attempt:  no retry loop, because a retry on a 429 or a 5xx would spend the caller's time
    on a provider that just said no.  No proxy (the standard library's http.client uses none), no
    redirect (a 3xx is a failure, so the key is never sent anywhere but the host in the URL), and a
    hard overall deadline:  the socket timeout bounds each step and a watchdog shuts the socket at
    the deadline, so a server that drips bytes cannot hold the caller past it.  `base_url` is for a
    test's loopback server;  production passes none.  The caller always states the system message:
    there is no default since the wake contract went with the retired wake types."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = OPENROUTER_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.clock = clock

    def run(
        self,
        model: str,
        prompt: str,
        schema: str,
        key: str,
        *,
        system: str,
    ) -> RunResult:
        out = RunResult()
        base = self.base_url if self.base_url is not None else OPENROUTER_BASE_URL
        problem = openrouter_url_problem(base)
        if problem:
            out.error = "the OpenRouter URL is refused: %s" % problem
            return out
        try:
            body = openrouter_request_body(model, system, prompt, schema)
        except ValueError:
            out.error = "the result schema is not valid JSON"
            return out
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        parts = urllib.parse.urlsplit(base)
        https = parts.scheme == "https"
        path = parts.path.rstrip("/") + OPENROUTER_PATH
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": Z.USER_AGENT,
            "X-Title": OPENROUTER_TITLE,
        }
        started = self.clock()
        fired = threading.Event()
        status: int | None = None
        data = b""
        too_large = False
        conn: http.client.HTTPConnection
        if https:
            conn = http.client.HTTPSConnection(
                parts.hostname or "",
                parts.port,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                parts.hostname or "", parts.port, timeout=self.timeout
            )

        def watchdog() -> None:
            fired.set()
            sock = conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(self.timeout, watchdog)
        timer.daemon = True
        timer.start()
        try:
            conn.request("POST", path, body=payload, headers=headers)
            response = conn.getresponse()
            status = int(response.status)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > OPENROUTER_RESPONSE_CAP:
                    too_large = True
                    break
                chunks.append(chunk)
            timer.cancel()
            if fired.is_set():
                # The watchdog shut the socket at the deadline.  http.client reports a connection
                # closed under a response with a Content-Length as a short read, not an error, so the
                # deadline is checked here rather than trusted to arrive as an exception.
                raise TimeoutError
            data = b"".join(chunks)
        except Exception as exc:  # noqa: BLE001 - any failure is a reason, never a traceback with a header
            if fired.is_set() or isinstance(exc, TimeoutError):
                out.error = "timed out after %g seconds" % self.timeout
                out.killed = True
            else:
                out.error = "network: %s" % type(exc).__name__
        finally:
            timer.cancel()
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            out.secs = round(self.clock() - started, 2)
        out.http_status = status
        out.stdout_len = len(data)
        out.stderr_sha = hashlib.sha256(data).hexdigest()
        if out.error is not None:
            return out
        if too_large:
            out.error = "response too large"
            return out
        if (
            status is None
        ):  # (no exception and no status cannot happen;  fail closed if it ever does)
            out.error = "no response"
            return out
        if not 200 <= status < 300:
            out.error = openrouter_http_reason(status)
            return out
        text, usage, served_model, why = openrouter_answer(data)
        out.usage = usage or None
        out.models = [served_model] if served_model else []
        if why:
            out.error = why
            return out
        out.result = {"text": text}
        out.parsed = parse_muse_result(text)
        if out.parsed is None:
            # A body that is not JSON at all.  It is a failed run, not an answer the validator has to
            # explain later.
            out.error = "no parseable result"
            out.result = None
            return out
        out.exit = 0
        return out


# --------------------------------------------------------------------------------------------
# Remote routine wake (http)
# --------------------------------------------------------------------------------------------

ROUTINE_CONTRACT = "simple-tools-wake/1"
ROUTINE_RETRIES = 3  # retries after the first request:  at most 4 requests per wake
ROUTINE_BACKOFF = (2.0, 5.0, 10.0)
ROUTINE_EXCERPT_LIMIT = 2000
ROUTINE_RESPONSE_CAP = 65536
# The hosted MCP Worker refuses a wake body over 64 KiB (src/wake.js WAKE_MAX_BODY).  A body with
# `held_back` stays far below it;  run_routine lists fewer items rather than ever cross it.
ROUTINE_MAX_BODY = 64 * 1024
# The hosted Worker's settled outcomes (docs/protocols/simple-tools-mcp.md section 3.12, item 12).
WORKER_OUTCOMES = frozenset(
    {"delivered", "no_subscriber", "channel_refused", "rejected", "gone", "not_live"}
)
# One read of the wake router's receipt (RoutineRunner.read_receipt) waits at most this long, or the
# routine's own timeout when that is shorter.  The router answers a receipt from a file, and a read
# that times out is simply read again on the daemon's schedule (wakes.receipt_next_read).
RECEIPT_TIMEOUT = 5.0
# The wake router's own path shapes (wake_router.SEAT_RE and WAKE_ID_RE):  a receipt URL is derived
# only for a seat and a wake id its `/receipt/<SEAT>/<id>` path accepts.
_ROUTER_SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")
_ROUTER_WAKE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RoutineTarget:
    """A resolved routine:  the URL and key read from the environment, and how to send them.
    The URL and key are never logged or written anywhere.  `host` is logged, and `origin`
    (host and port) is written in a router wake's `done` row as `receipt_routine.origin`."""

    def __init__(self, url: str, key: str, cfg: RoutineConfig) -> None:
        self.url = url
        self.key = key
        self.method = cfg.method
        self.header = cfg.header
        self.auth = cfg.auth
        self.timeout = cfg.timeout
        self.signature_prefix = cfg.signature_prefix

    def __repr__(self) -> str:
        return "RoutineTarget(host=%s, auth=%s)" % (self.host, self.auth)

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").lower()

    @property
    def origin(self) -> str:
        """`host:port`, the port empty when the URL names none:  where the key goes, without the
        path or query, which may hold a token.  A daemon restart checks it before it reads a
        receipt with a routine rebuilt from the ledger (daemon._sent_routine).  The URL passed
        routine_url_problem, so its port parses."""
        port = urllib.parse.urlsplit(self.url).port
        return "%s:%s" % (self.host, "" if port is None else port)

    def header_value(self, body: bytes) -> str:
        """The auth header for these exact body bytes:  the key itself, `Bearer <key>`, or the
        hex HMAC-SHA256 of the raw body keyed with the sender key (after an optional prefix)."""
        if self.auth == "bearer":
            return "Bearer " + self.key
        if self.auth == "header":
            return self.key
        return (
            self.signature_prefix
            + hmac.new(self.key.encode("utf-8"), body, hashlib.sha256).hexdigest()
        )

    def secrets(self) -> list[str]:
        """Values the daemon scrubs from every log line and error (routine_secret_values)."""
        return routine_secret_values(self.url, self.key)


def routine_secret_values(url: str, key: str = "") -> list[str]:
    """Every value to scrub for one routine:  the key and the whole URL, each as given and
    without wrapping quotes, the URL's query string, and its path when that is long enough to
    hold a token.  It never raises.  A URL that does not split (an unbalanced IPv6 bracket makes
    urlsplit raise ValueError) still hides the raw value and the text after its `?`, so a typo in
    an environment variable can neither stop the daemon nor leak the token."""
    found: list[str] = []
    for raw in (key or "", url or ""):
        for value in (raw, raw.strip(), Z._strip_quotes(raw)):
            if value not in found:
                found.append(value)
    clean = Z._strip_quotes(url or "")
    try:
        parts = urllib.parse.urlsplit(clean)
        query, path = parts.query, parts.path
    except ValueError:
        query, path = clean.partition("?")[2].partition("#")[0], ""
    found.append(query)
    if len(path) >= 8:
        found.append(path)
    return [value for value in found if value and len(value) >= 4]


# The Tailscale range is the RFC 6598 shared address space block.  It is built from numbers so the repo's
# private-address guard (test_fleet_skill_identity) does not read the rule itself as inventory.
_TAILNET = ipaddress.ip_network((ipaddress.IPv4Address((100 << 24) | (64 << 16)), 10))


def _is_tailnet(host: str) -> bool:
    """Whether the host is a Tailscale address (the RFC 6598 range), as an IP literal only.  A
    wake bridge that binds a tailnet address (the ZCode bridge does) is reached over the tailnet,
    which is encrypted, and from the server's own container without leaving the machine."""
    try:
        return ipaddress.ip_address(host) in _TAILNET
    except ValueError:
        return False


def routine_url_problem(url: str) -> str | None:
    """Why a routine URL is refused, or None.  https only (plain http only to a loopback test
    server or a Tailscale address), a host, no user:password part and no control characters."""
    if any(ch in url for ch in "\r\n\x00\t "):
        return "it holds whitespace or a control character"
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return "it is not a valid URL"
    if parts.scheme not in ("https", "http"):
        return "it must be an https URL"
    if not host:
        return "it has no host"
    if parts.username is not None or parts.password is not None:
        return "it must not carry a user name or password"
    if parts.scheme == "http" and not (Z._is_loopback(host.lower()) or _is_tailnet(host)):
        return (
            "it must be https (plain http is accepted only for a loopback test server "
            "or a Tailscale address)"
        )
    return None


def resolve_routine(
    cfg: RoutineConfig | None, env: Mapping[str, str]
) -> tuple[RoutineTarget | None, str | None, str]:
    """(target, reason code, detail).  The detail names environment variables, never values.
    Values lose wrapping quotes, as the Zulip credentials do (Z.env_credentials):  a synced
    `"https://..."` would otherwise be refused as not https, and a quoted key sent as
    `Bearer "<key>"` would get a 401 that is never retried."""
    if cfg is None:
        return None, "routine_not_configured", "no routine block"
    url = Z._strip_quotes(env.get(cfg.url_env) or "")
    key = Z._strip_quotes(env.get(cfg.key_env) or "")
    missing = [name for name, value in ((cfg.url_env, url), (cfg.key_env, key)) if not value]
    if missing:
        return (
            None,
            "routine_not_configured",
            "environment variable%s not set: %s"
            % ("" if len(missing) == 1 else "s", ", ".join(missing)),
        )
    if any(ch in key for ch in "\r\n\x00"):
        return None, "routine_key_refused", "%s holds a control character" % cfg.key_env
    problem = routine_url_problem(url)
    if problem:
        return None, "routine_url_refused", "the URL in %s is refused: %s" % (cfg.url_env, problem)
    return RoutineTarget(url, key, cfg), None, ""


class RoutineResult:
    def __init__(self) -> None:
        self.accepted = False
        self.status: int | None = None  # the last HTTP status, None after a network failure
        self.attempts = 0
        self.error: str | None = None
        self.history: list[Any] = []  # status or error class per attempt
        self.secs = 0.0
        self.response_len = 0
        self.response_sha = ""
        # The hosted MCP Worker's answer to an accepted wake, reduced to what says whether anything
        # reached the seat (routine_answer); None for any other answer.  The body itself is never
        # kept, except by a runner made with keep_response (`daemon test-wake`, never the daemon).
        self.answer: dict[str, Any] | None = None
        self.response: bytes | None = None


def routine_answer(data: bytes) -> dict[str, Any] | None:
    """The hosted MCP Worker's 2xx answer (spec 3.12, item 12) reduced to {delivered, paused,
    disabled, duplicate, outcome}, or None when the body is not that answer (a Grok Bot routine's,
    or anything else), which is then not read further.  Only an integer, booleans and one of the
    Worker's fixed outcome codes are kept, so nothing a routine writes reaches the ledger or a log."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
        return None
    if not any(key in obj for key in ("delivered", "paused", "disabled", "duplicate")):
        return None
    delivered = obj.get("delivered")
    outcome = obj.get("outcome")
    return {
        "delivered": delivered
        if isinstance(delivered, int) and not isinstance(delivered, bool) and delivered >= 0
        else None,
        "paused": obj.get("paused") is True,
        "disabled": obj.get("disabled") is True,
        "duplicate": obj.get("duplicate") is True,
        "outcome": outcome if isinstance(outcome, str) and outcome in WORKER_OUTCOMES else None,
    }


def receipt_url(url: str, seat: str, wake_id: str) -> str | None:
    """The wake router's receipt URL for one wake:  the routine URL with its final `/wake/<SEAT>`
    replaced by `/receipt/<SEAT>/<wake_id>` (wake_router.RECEIPT_PATH_RE), keeping any path before
    it (a router behind a prefix).  None when the URL does not end that way, carries a query or a
    fragment (the router's paths never do), or the seat or wake id is not one its path accepts.
    Any of the three that is not a string is refused the same way, never raised on."""
    if not (isinstance(url, str) and isinstance(seat, str) and isinstance(wake_id, str)):
        return None
    if not _ROUTER_SEAT_RE.fullmatch(seat) or not _ROUTER_WAKE_ID_RE.fullmatch(wake_id):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    suffix = "/wake/%s" % seat
    if not parts.netloc or parts.query or parts.fragment or not parts.path.endswith(suffix):
        return None
    path = parts.path[: -len(suffix)] + "/receipt/%s/%s" % (seat, wake_id)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class ReceiptRead:
    """One read of a wake router receipt.  `body` is the answer when it was a 200 holding one JSON
    object, else None;  `error` is a fixed word when no HTTP answer came (`timeout`, `network`,
    `request_refused`).  The answer is untrusted:  wakes.receipt_outcome decides what counts."""

    def __init__(
        self, status: int | None = None, body: Any = None, error: str | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.error = error


def _read_by(response: Any, deadline: float, clock: Callable[[], float]) -> bytes:
    """At most ROUTINE_RESPONSE_CAP bytes of a response body, read a piece at a time (each one
    socket read, bounded by the socket's own timeout), and TimeoutError once `clock` reaches
    `deadline`:  a server that sends a byte now and then cannot hold the read for long."""
    data = bytearray()
    while len(data) < ROUTINE_RESPONSE_CAP:
        if clock() >= deadline:
            raise TimeoutError("the response was not read by its deadline")
        piece = response.read1(min(16384, ROUTINE_RESPONSE_CAP - len(data)))
        if not piece:
            break
        data += piece
    return bytes(data)


def routine_surfaced(result: RoutineResult) -> bool:
    """Did this wake reach the seat, so the held-back items it listed count as surfaced?  Not when
    the routine did not take it.  A 2xx from a routine that is not the hosted Worker counts.  The
    Worker answers 202 for several outcomes that reached nobody (no subscriber, a channel off its
    allowlist, a refused or lapsed subscription, a paused seat, MCP_DISABLED), so from it only
    `delivered` of at least 1 counts.  A `duplicate` does not:  that wake was settled before, which
    does not say it was delivered, and listing an item twice is better than losing it."""
    if not result.accepted:
        return False
    answer = result.answer
    if answer is None:
        return True
    if answer["paused"] or answer["disabled"] or answer["duplicate"]:
        return False
    return isinstance(answer["delivered"], int) and answer["delivered"] >= 1


class RoutineRunner:
    """Sends one wake body to a routine.  The body is serialized once by the caller, the header
    is computed over those bytes, and the identical bytes go out on every attempt.  No proxy and
    no redirects (a redirect would re-send the key elsewhere), and an explicit timeout.
    `keep_response` keeps the last answer's bytes (at most ROUTINE_RESPONSE_CAP) on the result, for
    `daemon test-wake`, which shows the router's dry-run answer;  the daemon never sets it."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        retries: int = ROUTINE_RETRIES,
        backoff: Sequence[float] = ROUTINE_BACKOFF,
        should_stop: Callable[[], bool] | None = None,
        keep_response: bool = False,
    ) -> None:
        self.sleep = sleep
        self.clock = clock
        self.retries = retries
        self.backoff = tuple(backoff)
        self.should_stop = should_stop
        self.keep_response = keep_response

    def deliver(self, target: RoutineTarget, body: bytes, *, idempotency_key: str) -> RoutineResult:
        out = RoutineResult()
        started = self.clock()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": Z.USER_AGENT,
            "Idempotency-Key": idempotency_key,
            target.header: target.header_value(body),
        }
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Z._NoRedirect())
        for attempt in range(1, self.retries + 2):
            out.attempts = attempt
            status: int | None = None
            data = b""
            retry = False
            request = urllib.request.Request(
                target.url, data=body, method=target.method, headers=headers
            )
            try:
                with opener.open(request, timeout=target.timeout) as response:
                    status = int(response.status)
                    data = response.read(ROUTINE_RESPONSE_CAP)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    data = exc.read(ROUTINE_RESPONSE_CAP)
                except (OSError, http.client.HTTPException):
                    data = b""
                finally:
                    exc.close()
            except ValueError:
                out.error = "the request was refused before sending (bad URL or header value)"
                out.history.append("ValueError")
                break
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
                out.error = "network: %s" % ("timed out" if timed_out else type(reason).__name__)
                out.history.append("timeout" if timed_out else type(reason).__name__)
                retry = True
            out.status = status
            out.response_len = len(data)
            out.response_sha = hashlib.sha256(data).hexdigest()
            if self.keep_response:
                out.response = data
            if status is not None:
                out.history.append(status)
                if 200 <= status < 300:
                    out.accepted = True
                    out.error = None
                    out.answer = routine_answer(data)
                    break
                if status >= 500:
                    out.error, retry = "HTTP %d" % status, True
                elif 300 <= status < 400:
                    out.error = "HTTP %d (redirects are refused)" % status
                else:
                    out.error = "HTTP %d" % status
            if (
                not retry
                or attempt > self.retries
                or (self.should_stop is not None and self.should_stop())
            ):
                break
            self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        out.secs = round(self.clock() - started, 2)
        return out

    def read_receipt(
        self, target: RoutineTarget, url: str, *, timeout: float = RECEIPT_TIMEOUT
    ) -> ReceiptRead:
        """One GET of a wake router receipt (`url`, from receipt_url) with the routine's own header
        and key, as the wake was sent.  The same handling as `deliver`:  no proxy, no redirect (it
        would re-send the key elsewhere), a bounded timeout and at most ROUTINE_RESPONSE_CAP bytes.
        Only a 200's body is read, a piece at a time against a deadline `timeout` after the
        request began (_read_by), so a body sent slowly cannot hold the read much past it;  the
        status line and headers are bounded per socket read, as in `deliver`.  One request and no
        retry:  the daemon reads again on its own schedule.  It logs nothing."""
        headers = {
            "Accept": "application/json",
            "User-Agent": Z.USER_AGENT,
            target.header: target.header_value(b""),
        }
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Z._NoRedirect())
        request = urllib.request.Request(url, method="GET", headers=headers)
        wait = min(float(target.timeout), float(timeout))
        deadline = self.clock() + wait
        try:
            with opener.open(request, timeout=wait) as response:
                status = int(response.status)
                data = _read_by(response, deadline, self.clock) if status == 200 else b""
        except urllib.error.HTTPError as exc:
            exc.close()  # only a 200 is a receipt, so an error answer's body is never read
            return ReceiptRead(status=int(exc.code))
        except ValueError:
            return ReceiptRead(error="request_refused")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            return ReceiptRead(error="timeout" if timed_out else "network")
        body: Any = None
        if status == 200:
            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                body = None
        return ReceiptRead(status=status, body=body if isinstance(body, dict) else None)


def _hash_part(text: str) -> str:
    """Zulip's URL-hash encoding:  percent-encode, then `.` for `%` (and `.2E` for a dot)."""
    return urllib.parse.quote(text, safe="").replace(".", "%2E").replace("%", ".")


def zulip_link(realm: str, row: Mapping[str, Any]) -> str:
    """A link to the trigger message:  its topic for a channel message, its thread for a DM."""
    message_id = int(row.get("id") or 0)
    if row.get("type") == "private":
        ids = sorted(
            i for i in row.get("recipients") or [] if isinstance(i, int) and not isinstance(i, bool)
        )
        return "%s/#narrow/dm/%s-dm/near/%d" % (realm, ",".join(str(i) for i in ids), message_id)
    channel = str(row.get("channel") or "")
    stream_id = row.get("stream_id")
    where = (
        "%d-%s" % (stream_id, _hash_part(channel))
        if isinstance(stream_id, int) and not isinstance(stream_id, bool)
        else _hash_part(channel)
    )
    return "%s/#narrow/channel/%s/topic/%s/near/%d" % (
        realm,
        where,
        _hash_part(str(row.get("topic") or "")),
        message_id,
    )


def routine_excerpt(text: str, limit: int = ROUTINE_EXCERPT_LIMIT, nonce: str | None = None) -> str:
    """The trigger's body between nonce markers, escaped like every other untrusted body (marker
    text removed, control characters and line separators escaped), the whole string at most
    `limit` characters."""
    nonce = nonce or L.new_nonce()
    head = "%s nonce=%s\n" % (L.MARKER_BEGIN, nonce)
    tail = "\n%s nonce=%s" % (L.MARKER_END, nonce)
    room = limit - len(head) - len(tail)
    cut = room
    body = L.escape_body(text, cut)
    while len(body) > room and cut > 0:
        cut -= len(body) - room
        body = L.escape_body(text, max(cut, 0))
    return head + body[:room] + tail


def routine_body(
    *,
    seat: str,
    wake_id: str,
    trigger_ids: Iterable[int],
    row: Mapping[str, Any],
    bot_user_id: int | None,
    realm: str,
    now: float,
    scrub: Callable[[str], str] = lambda text: text,
    nonce: str | None = None,
    held_back: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The fixed contract (simple-tools-wake/1) for one wake.  `row` is the trigger's seat-inbox
    row:  its owner flag is the daemon's (the owner's user id from a human Zulip app).  A DM
    trigger is marked `dm: true` and its `reply_to` is that DM's other members only, never a
    channel.  Channel, topic and sender name are Zulip data too, not instructions:  the top-level
    `channel`, `topic` and `sender_full_name` are escaped display copies, and only `reply_to`
    carries the exact names, for addressing.  `stream_id` is the trigger's Zulip stream id (an int;
    None for a DM or a row without one), so a receiver can hold the channel to its own allowlist by
    id, never by name:  the hosted MCP Worker refuses a channel wake without an allowlisted one.
    `held_back` (heldback.wake_summary) is added only when its count is above 0:  how many wakes
    the seat's budget held back, and the newest as metadata only, never a body (an additive field,
    so a body without it is unchanged).  `letter` and `linked_by` are added only when the trigger row is
    an untagged reply to one of the seat's chats (class `reply`):  the chat's session letter, when it had
    one, and `"reply"`."""
    dm = row.get("type") == "private"
    message_id = int(row["id"])
    members = sorted(
        i for i in row.get("recipients") or [] if isinstance(i, int) and not isinstance(i, bool)
    )
    others = [i for i in members if i != bot_user_id]
    # The exact channel and topic go only into `reply_to`, which the routine needs for
    # addressing.  The top-level copies are display text, escaped like every other Zulip-derived
    # name (item_meta):  one line, marker text removed, U+2028 and U+2029 escaped, at most 100.
    raw_channel = str(row.get("channel") or "")
    raw_topic = str(row.get("topic") or "")
    channel = None if dm else L.escape_line(raw_channel, 100)
    topic = None if dm else L.escape_line(raw_topic, 100)
    sender_id = row.get("sender_id")
    stream_id = row.get("stream_id")
    body = {
        "contract": ROUTINE_CONTRACT,
        "seat": seat,
        "wake_id": wake_id,
        "message_id": message_id,
        "trigger_ids": [int(i) for i in trigger_ids],
        "dm": dm,
        "channel": channel,
        "topic": topic,
        "stream_id": (
            stream_id
            if not dm
            and isinstance(stream_id, int)
            and not isinstance(stream_id, bool)
            and stream_id > 0
            else None
        ),
        "dm_recipient_ids": others if dm else [],
        "sender_user_id": sender_id
        if isinstance(sender_id, int) and not isinstance(sender_id, bool)
        else None,
        "sender_full_name": L.escape_line(str(row.get("sender") or ""), 100),
        "is_bot": bool(row.get("is_bot", True)),
        "owner": bool(row.get("owner")),
        "excerpt": routine_excerpt(scrub(str(row.get("content") or "")), nonce=nonce),
        "zulip_link": zulip_link(realm, row),
        "reply_to": (
            {"type": "direct", "to": others}
            if dm
            else {"type": "stream", "channel": raw_channel, "topic": raw_topic}
        ),
        "reply_prefix": "[%s·wake] re=%d" % (seat, message_id),
        "sent_at": int(now),
    }
    if held_back and int(held_back.get("count") or 0) > 0:
        body["held_back"] = held_back
    if "reply" in (row.get("classes") or []):
        # The trigger is an untagged post that answers one of the seat's chats (owner, Nov 10 2026).  The
        # letter is that chat's, the router honours it like an explicit "@**Seat** AA" (the receiver's
        # `letter` field), and `linked_by` says the wake was picked by a reply and not by a letter in the
        # text.  Both are optional additive fields:  a trigger that carries a letter in its own text (an
        # explicit address) sends neither, and a receiver that ignores them still works.  The letter is
        # left out when the post the trigger answers carried none.
        letter = str(row.get("reply_target_letter") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9]{1,8}", letter):
            body["letter"] = letter
        body["linked_by"] = "reply"
    return body


def routine_bytes(body: Mapping[str, Any]) -> bytes:
    """Serialize once:  these bytes are signed and sent, unchanged on every retry."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


# --------------------------------------------------------------------------------------------
# notify-owner
# --------------------------------------------------------------------------------------------


def clean_banner(text: str, limit: int = NOTIFY_CUT) -> str:
    """Seat, sender and topic are attacker-controlled:  control characters go, the text is one
    line, and it is cut to 120 characters.  A body never reaches a banner."""
    text = _CONTROL_RE.sub(" ", str(text))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def osascript_argv(osascript: str, text: str, title: str) -> list[str]:
    return [
        osascript,
        "-e",
        "on run argv",
        "-e",
        "display notification (item 1 of argv) with title (item 2 of argv)",
        "-e",
        "end run",
        "--",
        clean_banner(text),
        clean_banner(title),
    ]


def _run_quiet(argv: Sequence[str]) -> None:
    try:
        subprocess.run(list(argv), capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


class Notifier:
    """A macOS banner plus an owner-queue entry.  Each `key` notifies at most once per `once_per`
    seconds (default a day), recorded in listener/notify.json so a restart does not repeat it."""

    def __init__(
        self,
        root: str,
        *,
        clock: Callable[[], float] = time.time,
        runner: Callable[[Sequence[str]], None] | None = None,
        banners: bool = True,
        osascript: str = "/usr/bin/osascript",
    ) -> None:
        self.root = root
        self.clock = clock
        self.runner = runner or _run_quiet
        self.banners = banners
        self.osascript = osascript
        self.sent: list[list[str]] = []
        self.lock = threading.Lock()

    def notify(
        self,
        seat: str,
        title: str,
        text: str,
        *,
        kind: str,
        key: str | None = None,
        once_per: float = 86400.0,
        note: str | None = None,
        trigger_ids: Sequence[int] = (),
        sender_id: Any = None,
        owner: bool = False,
        risk: str | None = None,
        banners: bool | None = None,
    ) -> bool:
        now = self.clock()
        key = key or "%s:%s" % (seat, kind)
        with self.lock:
            path = os.path.join(L.listener_dir(self.root), "notify.json")
            fresh = []

            def mutate(data: dict[str, Any]) -> None:
                last = data.get(key)
                if isinstance(last, (int, float)) and now - last < once_per:
                    return
                data[key] = now
                for old in [
                    k
                    for k, v in data.items()
                    if isinstance(v, (int, float)) and now - v > 7 * 86400
                ]:
                    data.pop(old, None)
                fresh.append(True)

            L.update_json(path, mutate)
            if not fresh:
                return False
            paths = L.SeatPaths(self.root, seat)
            meta = paths.owner_queue_meta
            with L.locked(meta):
                data = L.read_json(meta)
                seq = int(data.get("next_seq") or 1)
                L.append_jsonl(
                    paths.owner_queue,
                    [
                        {
                            "seq": seq,
                            "ts": now,
                            "seat": seat,
                            "kind": kind,
                            "title": clean_banner(title),
                            "text": clean_banner(text, 300),
                            "note": note,
                            "trigger_ids": list(trigger_ids),
                            "sender_id": sender_id,
                            "owner": owner,
                            "risk": risk,
                        }
                    ],
                )
                data["next_seq"] = seq + 1
                L.write_json(meta, data)
            argv = osascript_argv(self.osascript, text, title)
            self.sent.append(argv)
        # `banners` overrides the instance flag for this one call (tests use it):  the once-per record
        # is still kept, no macOS banner pops, and the shared flag other threads read is not flipped.
        show = self.banners if banners is None else banners
        if show:
            self.runner(argv)
        return True
