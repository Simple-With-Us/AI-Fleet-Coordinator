"""Wake adapters and side channels for the listener daemon:  the Claude headless run, the remote
routine wake (`http`), the notify-owner banner and owner queue, and board filing.

Every subprocess gets a list argv, never a shell string, and message content is never executed.
The Claude run gets no tools:  it returns JSON, and the daemon validates it and posts.

    cd <state>/<SEAT>/wake            # empty, mode 700, never a repo
    env -i HOME USER LOGNAME LANG TMPDIR PATH ENABLE_CLAUDEAI_MCP_SERVERS=false CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1
      claude -p --safe-mode --restricted --settings '{"disableAllHooks":true}' --model sonnet --tools ""
        --strict-mcp-config --disallowedTools "mcp__*" --permission-mode dontAsk --permission-prompts none
        --no-session-persistence --max-turns 4 --max-budget-usd 0.25 --output-format stream-json --verbose
        --json-schema <schema> --append-system-prompt-file <package>/wake/wake-contract.md  < prompt.txt

The real guard is the stream check:  the `system` `init` event must list no MCP servers and no
tools other than the harness's own `StructuredOutput` (which `--json-schema` adds), and no
`system` hook event may appear at any point; otherwise the process group is killed and the wake
refused.  The final `result` must show exactly one model in `modelUsage`.  A run is pinned to
the realpath of the claude binary that passed `agent-sync daemon test-wake --run` and was then
confirmed with `--pin`; after an update changes it, wakes fall back to inbox-only until the test
passes again.  The pinned realpath is what runs, never the PATH symlink.

The `http` adapter (the server instance's Grok Bot personas) sends one fixed JSON body to a
remote routine webhook whose URL and sender key come from environment variables named in the
seat's config.  The body carries the trigger's metadata and a 2,000-character excerpt wrapped in
the untrusted-content markers; the routine itself replies in Zulip with the persona's own key.
2xx is accepted; a 5xx or a network failure is retried with backoff (3 retries, so at most 4
requests, the identical body each time); a 4xx or a redirect is never retried.  The URL, its
query string, the key and the signature are never logged.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import live as L
from . import zulip as Z
from .config import WAKE_MAX_BUDGET_USD, RoutineConfig
from .identity import WAKE_LAUNCHER
from .wakes import CONTRACT_PATH, parse_result, schema_text

WAKE_TIMEOUT = 240.0
MAX_TURNS = 4
NOTIFY_CUT = 120
ALLOWED_INIT_TOOLS = frozenset({"StructuredOutput"})  # the harness's own output tool for --json-schema
NO_HOOKS_SETTINGS = '{"disableAllHooks":true}'
CANDIDATE_SECONDS = 86400.0
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_wake_path(home: str) -> str:
    return "%s/.local/bin:/opt/homebrew/bin:/usr/bin:/bin" % home


# --------------------------------------------------------------------------------------------
# Claude headless wake
# --------------------------------------------------------------------------------------------

def resolve_claude(explicit: str | None, wake_path: str) -> str | None:
    """The claude binary:  the configured absolute path, else `claude` on the fixed wake PATH."""
    if explicit:
        return explicit if os.path.isabs(explicit) and os.access(explicit, os.X_OK) else None
    found = shutil.which("claude", path=wake_path)
    return os.path.abspath(found) if found else None


def claude_argv(claude: str, model: str) -> list[str]:
    """The fixed wake argv.  `claude` should be the pinned realpath (binary_pin), so the binary
    that runs is the one that was checked, even if a symlink moves in between."""
    return [claude, "-p", "--safe-mode", "--restricted", "--settings", NO_HOOKS_SETTINGS, "--model", model,
            "--tools", "", "--strict-mcp-config", "--disallowedTools", "mcp__*",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--no-session-persistence", "--max-turns", str(MAX_TURNS), "--max-budget-usd", "%.2f" % WAKE_MAX_BUDGET_USD,
            "--output-format", "stream-json", "--verbose", "--json-schema", schema_text(),
            "--append-system-prompt-file", CONTRACT_PATH]


def claude_env(env: Mapping[str, str], home: str, wake_path: str, *, seat: str | None = None) -> dict[str, str]:
    """`env -i` plus a fixed list:  every CLAUDE_CODE_* (the messaging socket and token), AGENT_*,
    ZULIP_* and ANTHROPIC_API_KEY are dropped, so the run uses the claude.ai login.

    With `seat` (a wake, or test-wake), the listener acts as the reference launcher
    (AGENT-SYNC § Identity Rules › Launcher Contract):  it sets AGENT_LAUNCHER=agent-sync-wake,
    AGENT_LAUNCH_SEAT and AGENT_SEAT to the seat it is waking, and AGENT_SYNC_ATTACH=0, fresh, after
    every inherited identity variable was dropped.  The wake has no tools and posts nothing itself
    (the daemon posts its validated reply as the seat's bot), so this only keeps the seat explicit."""
    user = env.get("USER") or env.get("LOGNAME") or os.path.basename(home)
    out = {"HOME": home, "USER": user, "LOGNAME": user, "LANG": "en_US.UTF-8",
           "TMPDIR": env.get("TMPDIR") or "/tmp", "PATH": wake_path,
           "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}
    if seat:
        out.update({"AGENT_LAUNCHER": WAKE_LAUNCHER, "AGENT_LAUNCH_SEAT": seat, "AGENT_SEAT": seat,
                    "AGENT_SYNC_ATTACH": "0"})
    return out


class RunResult:
    def __init__(self) -> None:
        self.refused: str | None = None  # why the run was refused (the init assertion, the model count)
        self.error: str | None = None  # is_error subtype, timeout, no result
        self.result: dict[str, Any] | None = None
        self.parsed: Any = None
        self.cost_usd = 0.0
        self.exit: int | None = None
        self.secs = 0.0
        self.models: list[str] = []
        self.stdout_len = 0
        self.stderr_len = 0
        self.stderr_sha = ""
        self.init_tools: list[Any] | None = None  # what the init event listed, for test-wake's record
        self.killed = False

    @property
    def ok(self) -> bool:
        return self.refused is None and self.error is None and self.result is not None


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class ClaudeRunner:
    """Runs one headless wake.  `on_start(proc)` and `on_exit(proc)` bracket each child, so the
    daemon can track every running child (several seats may wake at once) and kill each group on
    SIGTERM."""

    def __init__(self, *, timeout: float = WAKE_TIMEOUT, clock: Callable[[], float] = time.monotonic,
                 on_start: Callable[[subprocess.Popen[Any]], None] | None = None,
                 on_exit: Callable[[subprocess.Popen[Any]], None] | None = None) -> None:
        self.timeout = timeout
        self.clock = clock
        self.on_start = on_start
        self.on_exit = on_exit

    def run(self, argv: Sequence[str], env: Mapping[str, str], cwd: str, prompt: str) -> RunResult:
        out = RunResult()
        L.private_dir(cwd)
        prompt_path = os.path.join(cwd, "prompt.txt")
        fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        started = self.clock()
        proc: subprocess.Popen[Any] | None = None
        try:
            with open(prompt_path, "rb") as stdin:
                try:
                    proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=stdin, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
                except OSError as exc:
                    out.error = "could not start claude (%s)" % (exc.strerror or type(exc).__name__)
                    return out
            if self.on_start:
                self.on_start(proc)
            lines: queue.Queue[bytes | None] = queue.Queue()
            err_chunks: list[bytes] = []

            def pump_out() -> None:
                assert proc is not None and proc.stdout is not None
                for raw in proc.stdout:
                    lines.put(raw)
                lines.put(None)

            def pump_err() -> None:
                assert proc is not None and proc.stderr is not None
                err_chunks.append(proc.stderr.read())

            out_thread = threading.Thread(target=pump_out, daemon=True)
            out_thread.start()
            err_thread = threading.Thread(target=pump_err, daemon=True)
            err_thread.start()
            deadline = time.monotonic() + self.timeout
            saw_init = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    out.error = "timed out after %d seconds" % self.timeout
                    out.killed = True
                    _kill_group(proc)
                    break
                try:
                    raw = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if raw is None:
                    break
                out.stdout_len += len(raw)
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "system" and str(event.get("subtype") or "").startswith("hook"):
                    out.refused = "a hook ran in the wake (%s)" % clean_banner(str(event.get("subtype")), 40)
                    out.killed = True
                    _kill_group(proc)
                    break
                if event.get("type") == "system" and event.get("subtype") == "init":
                    saw_init = True
                    tools = event.get("tools")
                    out.init_tools = list(tools) if isinstance(tools, list) else None
                    if not init_tools_ok(tools) or event.get("mcp_servers") != []:
                        out.refused = "the init event listed tools or MCP servers"
                        out.killed = True
                        _kill_group(proc)
                        break
                elif event.get("type") == "result":
                    if not saw_init:
                        out.refused = "no init event before the result"
                        out.killed = True
                        _kill_group(proc)
                        break
                    out.result = event
            try:
                out.exit = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                out.exit = proc.wait(timeout=10)
            err_thread.join(timeout=2)
            out_thread.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not (out_thread.is_alive() or err_thread.is_alive()):
                    stream.close()
            err = b"".join(err_chunks)
            out.stderr_len = len(err)
            out.stderr_sha = hashlib.sha256(err).hexdigest()
        finally:
            if proc is not None and self.on_exit:
                self.on_exit(proc)
            try:
                os.unlink(prompt_path)
            except OSError:
                pass
            out.secs = round(self.clock() - started, 2)
        if out.refused is None and out.error is None:
            if out.result is None:
                out.error = "no parseable result"
            else:
                usage = out.result.get("modelUsage")
                out.models = sorted(usage) if isinstance(usage, dict) else []
                cost = out.result.get("total_cost_usd")
                out.cost_usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else WAKE_MAX_BUDGET_USD
                if len(out.models) != 1:
                    out.refused = "modelUsage shows %d models, not 1" % len(out.models)
                elif out.result.get("is_error"):
                    out.error = str(out.result.get("subtype") or "error")
                else:
                    out.parsed = parse_result(out.result)
        if out.result is None or out.killed:
            out.cost_usd = WAKE_MAX_BUDGET_USD  # killed or no result: debit the whole run's maximum
        return out


def init_tools_ok(tools: Any) -> bool:
    """The init event's tools:  empty, or only the harness's StructuredOutput tool."""
    return isinstance(tools, list) and all(isinstance(t, str) and t in ALLOWED_INIT_TOOLS for t in tools)


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
    """`claude agents --json`, stdin closed, 10 seconds at most.  stdout, or None on any failure."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False, env=dict(env),
                              cwd=cwd, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def pin_path(paths: L.SeatPaths) -> str:
    return paths.wake_pin


def binary_pin(claude: str) -> str:
    return os.path.realpath(claude)


def pinned(paths: L.SeatPaths, claude: str | None) -> bool:
    if not claude:
        return False
    return L.read_json(paths.wake_pin).get("claude") == binary_pin(claude)


def agents_pinned(paths: L.SeatPaths, claude: str | None) -> bool:
    """True when the pinned binary is `claude` and test-wake found its `agents --json` usable."""
    return pinned(paths, claude) and L.read_json(paths.wake_pin).get("agents_ok") is True


def record_pin(paths: L.SeatPaths, claude: str, now: float, *, agents_ok: bool = False,
               init_tools: list[Any] | None = None) -> None:
    L.write_json(paths.wake_pin, {"claude": binary_pin(claude), "at": now, "agents_ok": bool(agents_ok),
                                  "init_tools": init_tools})


def record_candidate(paths: L.SeatPaths, claude: str, now: float, *, agents_ok: bool,
                     init_tools: list[Any] | None) -> None:
    """A binary that passed `test-wake --run`, waiting for a person to read the result and pin it."""
    L.write_json(paths.wake_pin_candidate, {"claude": binary_pin(claude), "at": now, "agents_ok": bool(agents_ok),
                                            "init_tools": init_tools})


def pin_candidate(paths: L.SeatPaths, claude: str, now: float) -> str | None:
    """Pin the candidate `test-wake --run` left.  None on success, else why it was not pinned."""
    cand = L.read_json(paths.wake_pin_candidate)
    if not cand:
        return "no passing test-wake --run to pin; run that first"
    at = cand.get("at")
    if not isinstance(at, (int, float)) or now - float(at) > CANDIDATE_SECONDS or now < float(at):
        return "the last passing test-wake --run is over a day old; run it again"
    if cand.get("claude") != binary_pin(claude):
        return "the claude binary changed since the test (%s, tested %s); run test-wake --run again" % (
            binary_pin(claude), cand.get("claude"))
    record_pin(paths, claude, now, agents_ok=cand.get("agents_ok") is True, init_tools=cand.get("init_tools"))
    try:
        os.unlink(paths.wake_pin_candidate)
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------------------------
# Remote routine wake (http)
# --------------------------------------------------------------------------------------------

ROUTINE_CONTRACT = "agent-sync-wake/1"
ROUTINE_RETRIES = 3  # retries after the first request:  at most 4 requests per wake
ROUTINE_BACKOFF = (2.0, 5.0, 10.0)
ROUTINE_EXCERPT_LIMIT = 2000
ROUTINE_RESPONSE_CAP = 65536


class RoutineTarget:
    """A resolved routine:  the URL and key read from the environment, and how to send them.
    It is never logged and never written anywhere; `host` is the only part that is."""

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

    def header_value(self, body: bytes) -> str:
        """The auth header for these exact body bytes:  the key itself, `Bearer <key>`, or the
        hex HMAC-SHA256 of the raw body keyed with the sender key (after an optional prefix)."""
        if self.auth == "bearer":
            return "Bearer " + self.key
        if self.auth == "header":
            return self.key
        return self.signature_prefix + hmac.new(self.key.encode("utf-8"), body, hashlib.sha256).hexdigest()

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


def routine_url_problem(url: str) -> str | None:
    """Why a routine URL is refused, or None.  https only (plain http only to a loopback test
    server), a host, no user:password part and no control characters."""
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


def resolve_routine(cfg: RoutineConfig | None, env: Mapping[str, str]) -> tuple[RoutineTarget | None, str | None, str]:
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
        return None, "routine_not_configured", "environment variable%s not set: %s" % (
            "" if len(missing) == 1 else "s", ", ".join(missing))
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


class RoutineRunner:
    """Sends one wake body to a routine.  The body is serialized once by the caller, the header
    is computed over those bytes, and the identical bytes go out on every attempt.  No proxy and
    no redirects (a redirect would re-send the key elsewhere), and an explicit timeout."""

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 retries: int = ROUTINE_RETRIES, backoff: Sequence[float] = ROUTINE_BACKOFF,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.sleep = sleep
        self.clock = clock
        self.retries = retries
        self.backoff = tuple(backoff)
        self.should_stop = should_stop

    def deliver(self, target: RoutineTarget, body: bytes, *, idempotency_key: str) -> RoutineResult:
        out = RoutineResult()
        started = self.clock()
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                   "User-Agent": Z.USER_AGENT, "Idempotency-Key": idempotency_key,
                   target.header: target.header_value(body)}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Z._NoRedirect())
        for attempt in range(1, self.retries + 2):
            out.attempts = attempt
            status: int | None = None
            data = b""
            retry = False
            request = urllib.request.Request(target.url, data=body, method=target.method, headers=headers)
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
            if status is not None:
                out.history.append(status)
                if 200 <= status < 300:
                    out.accepted = True
                    out.error = None
                    break
                if status >= 500:
                    out.error, retry = "HTTP %d" % status, True
                elif 300 <= status < 400:
                    out.error = "HTTP %d (redirects are refused)" % status
                else:
                    out.error = "HTTP %d" % status
            if not retry or attempt > self.retries or (self.should_stop is not None and self.should_stop()):
                break
            self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        out.secs = round(self.clock() - started, 2)
        return out


def _hash_part(text: str) -> str:
    """Zulip's URL-hash encoding:  percent-encode, then `.` for `%` (and `.2E` for a dot)."""
    return urllib.parse.quote(text, safe="").replace(".", "%2E").replace("%", ".")


def zulip_link(realm: str, row: Mapping[str, Any]) -> str:
    """A link to the trigger message:  its topic for a channel message, its thread for a DM."""
    message_id = int(row.get("id") or 0)
    if row.get("type") == "private":
        ids = sorted(i for i in row.get("recipients") or [] if isinstance(i, int) and not isinstance(i, bool))
        return "%s/#narrow/dm/%s-dm/near/%d" % (realm, ",".join(str(i) for i in ids), message_id)
    channel = str(row.get("channel") or "")
    stream_id = row.get("stream_id")
    where = ("%d-%s" % (stream_id, _hash_part(channel)) if isinstance(stream_id, int) and not isinstance(stream_id, bool)
             else _hash_part(channel))
    return "%s/#narrow/channel/%s/topic/%s/near/%d" % (realm, where, _hash_part(str(row.get("topic") or "")), message_id)


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


def routine_body(*, seat: str, wake_id: str, trigger_ids: Iterable[int], row: Mapping[str, Any], bot_user_id: int | None,
                 realm: str, now: float, scrub: Callable[[str], str] = lambda text: text,
                 nonce: str | None = None) -> dict[str, Any]:
    """The fixed contract (agent-sync-wake/1) for one wake.  `row` is the trigger's seat-inbox
    row:  its owner flag is the daemon's (the owner's user id from a human Zulip app).  A DM
    trigger is marked `dm: true` and its `reply_to` is that DM's other members only, never a
    channel.  Channel, topic and sender name are Zulip data too, not instructions:  the top-level
    `channel`, `topic` and `sender_full_name` are escaped display copies, and only `reply_to`
    carries the exact names, for addressing.  `stream_id` is the trigger's Zulip stream id (an int;
    None for a DM or a row without one), so a receiver can hold the channel to its own allowlist by
    id, never by name:  the hosted MCP Worker refuses a channel wake without an allowlisted one."""
    dm = row.get("type") == "private"
    message_id = int(row["id"])
    members = sorted(i for i in row.get("recipients") or [] if isinstance(i, int) and not isinstance(i, bool))
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
    return {
        "contract": ROUTINE_CONTRACT,
        "seat": seat,
        "wake_id": wake_id,
        "message_id": message_id,
        "trigger_ids": [int(i) for i in trigger_ids],
        "dm": dm,
        "channel": channel,
        "topic": topic,
        "stream_id": (stream_id if not dm and isinstance(stream_id, int) and not isinstance(stream_id, bool)
                      and stream_id > 0 else None),
        "dm_recipient_ids": others if dm else [],
        "sender_user_id": sender_id if isinstance(sender_id, int) and not isinstance(sender_id, bool) else None,
        "sender_full_name": L.escape_line(str(row.get("sender") or ""), 100),
        "is_bot": bool(row.get("is_bot", True)),
        "owner": bool(row.get("owner")),
        "excerpt": routine_excerpt(scrub(str(row.get("content") or "")), nonce=nonce),
        "zulip_link": zulip_link(realm, row),
        "reply_to": ({"type": "direct", "to": others} if dm
                     else {"type": "stream", "channel": raw_channel, "topic": raw_topic}),
        "reply_prefix": "[%s·wake] re=%d" % (seat, message_id),
        "sent_at": int(now),
    }


def routine_bytes(body: Mapping[str, Any]) -> bytes:
    """Serialize once:  these bytes are signed and sent, unchanged on every retry."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


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
    return [osascript, "-e", "on run argv",
            "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e", "end run", "--", clean_banner(text), clean_banner(title)]


def _run_quiet(argv: Sequence[str]) -> None:
    try:
        subprocess.run(list(argv), capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


class Notifier:
    """A macOS banner plus an owner-queue entry.  Each `key` notifies at most once per `once_per`
    seconds (default a day), recorded in listener/notify.json so a restart does not repeat it."""

    def __init__(self, root: str, *, clock: Callable[[], float] = time.time,
                 runner: Callable[[Sequence[str]], None] | None = None, banners: bool = True,
                 osascript: str = "/usr/bin/osascript") -> None:
        self.root = root
        self.clock = clock
        self.runner = runner or _run_quiet
        self.banners = banners
        self.osascript = osascript
        self.sent: list[list[str]] = []
        self.lock = threading.Lock()

    def notify(self, seat: str, title: str, text: str, *, kind: str, key: str | None = None,
               once_per: float = 86400.0, note: str | None = None, trigger_ids: Sequence[int] = (),
               sender_id: Any = None, owner: bool = False, risk: str | None = None) -> bool:
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
                for old in [k for k, v in data.items() if isinstance(v, (int, float)) and now - v > 7 * 86400]:
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
                L.append_jsonl(paths.owner_queue, [{"seq": seq, "ts": now, "seat": seat, "kind": kind,
                                                    "title": clean_banner(title), "text": clean_banner(text, 300),
                                                    "note": note, "trigger_ids": list(trigger_ids),
                                                    "sender_id": sender_id, "owner": owner, "risk": risk}])
                data["next_seq"] = seq + 1
                L.write_json(meta, data)
            argv = osascript_argv(self.osascript, text, title)
            self.sent.append(argv)
        if self.banners:
            self.runner(argv)
        return True


# --------------------------------------------------------------------------------------------
# Board filing (built, off by default:  board_per_day = 0)
# --------------------------------------------------------------------------------------------

def fleet_acronyms(repo_root: str = REPO_ROOT) -> set[str]:
    try:
        with open(os.path.join(repo_root, "fleet-apps.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(a["acronym"]).upper() for a in data.get("apps", []) if isinstance(a, dict) and a.get("acronym")}
    except (OSError, ValueError, KeyError, TypeError):
        return {"AFC"}


def board_app(topic: str, acronyms: set[str]) -> str:
    """The topic's leading app acronym (`CT#2316 sentry` gives CT) when it is a fleet app, else AFC."""
    first = (topic.split() or [""])[0]
    first = first.split("#", 1)[0].upper()
    return first if first in acronyms else "AFC"


def board_argv(board: str, *, seat: str, title: str, desc: str, severity: str, app: str, trigger_id: int,
               url: str) -> list[str]:
    if severity not in ("P2", "P3"):
        raise ValueError("severity must be P2 or P3")
    if not re.fullmatch(r"[A-Z]{2,4}", app):
        raise ValueError("app must be a fleet acronym")
    return [board, "file", "--title=" + title, "--desc=" + desc, "--severity=" + severity, "--app=" + app,
            "--by", seat, "--env", "Mac", "--kind", "agent-report", "--uid", "zulip:%s:%d" % (seat, trigger_id),
            "--url", url]


def run_board(argv: Sequence[str], env: Mapping[str, str], runner: Callable[..., Any] | None = None) -> tuple[bool, str]:
    run = runner or subprocess.run
    try:
        done = run(list(argv), capture_output=True, text=True, timeout=60, env=dict(env), check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    return done.returncode == 0, "exit %d" % done.returncode
