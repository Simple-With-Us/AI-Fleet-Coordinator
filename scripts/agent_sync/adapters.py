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
the realpath of the claude binary that passed `simple-chat daemon test-wake --run` and was then
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

import datetime
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from . import live as L
from . import zulip as Z
from .config import MCODE_DEFAULT_MODEL, WAKE_MAX_BUDGET_USD, RoutineConfig
from .identity import WAKE_LAUNCHER
from .wakes import CONTRACT_PATH, parse_result, schema_text

WAKE_TIMEOUT = 240.0
MAX_TURNS = 4
NOTIFY_CUT = 120
ALLOWED_INIT_TOOLS = frozenset(
    {"StructuredOutput"}
)  # the harness's own output tool for --json-schema
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


def claude_argv(claude: str, model: str, seats: Iterable[str] | None = None) -> list[str]:
    """The fixed wake argv.  `claude` should be the pinned realpath (binary_pin), so the binary
    that runs is the one that was checked, even if a symlink moves in between.  `seats` is the live
    fleet tag set, the enum of the schema's route seat (wakes.schema_text)."""
    return [
        claude,
        "-p",
        "--safe-mode",
        "--restricted",
        "--settings",
        NO_HOOKS_SETTINGS,
        "--model",
        model,
        "--tools",
        "",
        "--strict-mcp-config",
        "--disallowedTools",
        "mcp__*",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--max-turns",
        str(MAX_TURNS),
        "--max-budget-usd",
        "%.2f" % WAKE_MAX_BUDGET_USD,
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        schema_text(seats),
        "--append-system-prompt-file",
        CONTRACT_PATH,
    ]


def claude_env(
    env: Mapping[str, str], home: str, wake_path: str, *, seat: str | None = None
) -> dict[str, str]:
    """`env -i` plus a fixed list:  every CLAUDE_CODE_* (the messaging socket and token), AGENT_*,
    ZULIP_* and ANTHROPIC_API_KEY are dropped, so the run uses the claude.ai login.

    With `seat` (a wake, or test-wake), the listener acts as the reference launcher
    (FLEET-PROTOCOL § Identity Rules › Launcher Contract):  it sets AGENT_LAUNCHER=agent-sync-wake,
    AGENT_LAUNCH_SEAT and AGENT_SEAT to the seat it is waking, and AGENT_SYNC_ATTACH=0, fresh, after
    every inherited identity variable was dropped.  The wake has no tools and posts nothing itself
    (the daemon posts its validated reply as the seat's bot), so this only keeps the seat explicit."""
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
                "AGENT_SYNC_ATTACH": "0",
            }
        )
    return out


def result_reason(result: Mapping[str, Any]) -> str:
    """Why a run ended in error, in the owner's own words when the provider gave them.

    claude returns the human-facing reason as the result text ("You've hit your weekly limit ·
    resets Oct 13 at 5pm (America/Chicago)"), so that text is the message.  `subtype` is the
    fallback for a run that failed without saying why, and it is stripped of control characters
    because provider output is not trusted to be printable."""
    text = result.get("result")
    if isinstance(text, str) and text.strip():
        return clean_banner(text, 160)
    return clean_banner(str(result.get("subtype") or "error"), 160)


class RunResult:
    def __init__(self) -> None:
        self.refused: str | None = (
            None  # why the run was refused (the init assertion, the model count)
        )
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
        self.init_tools: list[Any] | None = (
            None  # what the init event listed, for test-wake's record
        )
        self.killed = False
        self.limited = False  # the provider refused for a quota, not an internal fault

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

    def __init__(
        self,
        *,
        timeout: float = WAKE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        on_start: Callable[[subprocess.Popen[Any]], None] | None = None,
        on_exit: Callable[[subprocess.Popen[Any]], None] | None = None,
    ) -> None:
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
                    proc = subprocess.Popen(
                        list(argv),
                        cwd=cwd,
                        env=dict(env),
                        stdin=stdin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                        close_fds=True,
                    )
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
                if event.get("type") == "system" and str(event.get("subtype") or "").startswith(
                    "hook"
                ):
                    out.refused = "a hook ran in the wake (%s)" % clean_banner(
                        str(event.get("subtype")), 40
                    )
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
                out.cost_usd = (
                    float(cost)
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                    else WAKE_MAX_BUDGET_USD
                )
                # A rate-limited run returns a `result` with `is_error` and NO modelUsage at all
                # (verified Oct 11 2026 against claude 2.1.296 at its weekly cap:  exit 1, empty
                # stderr, ~5 KB of stdout whose result text is "You've hit your weekly limit ...
                # resets Oct 13 at 5pm").  The model-count assertion below would call that "modelUsage
                # shows 0 models, not 1", which is true but says nothing useful:  it names an
                # internal assertion instead of the cap that actually stopped the run, so the owner
                # sees a refusal that reads like a broken adapter.  It is named first, from the
                # result text, and the real message reaches the ledger, the banner and the owner.
                if out.result.get("is_error"):
                    out.error = result_reason(out.result)
                    # No model ran, so nothing was billed and nothing was answered.  That is NOT by
                    # itself proof of a quota: an authentication or configuration failure has the
                    # same empty shape.  It is reported as `limited` because the CONSEQUENCE is the
                    # same in every case that matters here — no marginal cost, and no fallback, since
                    # a second runtime would meet the same broken credential.  The WORDING is kept
                    # honest by quoting the provider rather than asserting a cause:  the banner says
                    # what the provider said, and the snapshot gate (which knows the real quota
                    # state) is what names "quota".
                    out.limited = not out.models
                elif len(out.models) != 1:
                    out.refused = "modelUsage shows %d models, not 1" % len(out.models)
                else:
                    out.parsed = parse_result(out.result)
        if out.result is None or out.killed:
            out.cost_usd = WAKE_MAX_BUDGET_USD  # killed or no result: debit the whole run's maximum
        return out


def init_tools_ok(tools: Any) -> bool:
    """The init event's tools:  empty, or only the harness's StructuredOutput tool."""
    return isinstance(tools, list) and all(
        isinstance(t, str) and t in ALLOWED_INIT_TOOLS for t in tools
    )


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


def pin_path(paths: L.SeatPaths) -> str:
    return paths.wake_pin


def binary_pin(claude: str) -> str:
    return os.path.realpath(claude)


def pin_state(paths: L.SeatPaths, claude: str | None) -> dict[str, Any]:
    """Why the wake pin does or does not match the binary (AFC-256).  `pinned()` is a plain bool and
    cannot tell a seat that was never pinned from one whose pin went stale, and the two need different
    answers from the owner:  the first is a fresh install waiting on manual test 3, the second means a
    claude auto-update moved the realpath and every wake since has been degrading to inbox-only.

    `why` is None when pinned, else `no_claude` (no binary to pin), `not_pinned` (nothing recorded yet)
    or `pin_stale` (a pin was recorded and the binary has since moved).  `pinned_claude` is the realpath
    the pin holds and `claude` the one on disk now, so the owner can be told what moved."""
    if not claude:
        return {"pinned": False, "why": "no_claude", "pinned_claude": None, "claude": None}
    current = binary_pin(claude)
    recorded = L.read_json(paths.wake_pin).get("claude")
    if not isinstance(recorded, str) or not recorded:
        return {"pinned": False, "why": "not_pinned", "pinned_claude": None, "claude": current}
    if recorded != current:
        return {"pinned": False, "why": "pin_stale", "pinned_claude": recorded, "claude": current}
    return {"pinned": True, "why": None, "pinned_claude": recorded, "claude": current}


def pinned(paths: L.SeatPaths, claude: str | None) -> bool:
    if not claude:
        return False
    return L.read_json(paths.wake_pin).get("claude") == binary_pin(claude)


def agents_pinned(paths: L.SeatPaths, claude: str | None) -> bool:
    """True when the pinned binary is `claude` and test-wake found its `agents --json` usable."""
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


def record_candidate(
    paths: L.SeatPaths, claude: str, now: float, *, agents_ok: bool, init_tools: list[Any] | None
) -> None:
    """A binary that passed `test-wake --run`, waiting for a person to read the result and pin it."""
    L.write_json(
        paths.wake_pin_candidate,
        {
            "claude": binary_pin(claude),
            "at": now,
            "agents_ok": bool(agents_ok),
            "init_tools": init_tools,
        },
    )


def pin_candidate(paths: L.SeatPaths, claude: str, now: float) -> str | None:
    """Pin the candidate `test-wake --run` left.  None on success, else why it was not pinned."""
    cand = L.read_json(paths.wake_pin_candidate)
    if not cand:
        return "no passing test-wake --run to pin; run that first"
    at = cand.get("at")
    if not isinstance(at, (int, float)) or now - float(at) > CANDIDATE_SECONDS or now < float(at):
        return "the last passing test-wake --run is over a day old; run it again"
    if cand.get("claude") != binary_pin(claude):
        return (
            "the claude binary changed since the test (%s, tested %s); run test-wake --run again"
            % (binary_pin(claude), cand.get("claude"))
        )
    record_pin(
        paths,
        claude,
        now,
        agents_ok=cand.get("agents_ok") is True,
        init_tools=cand.get("init_tools"),
    )
    try:
        os.unlink(paths.wake_pin_candidate)
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------------------------
# Muse headless wake
# --------------------------------------------------------------------------------------------
#
# The second local wake type (AFC-291).  `muse exec` replaces `claude -p`, chosen on technical merit
# (owner 2026-10-11:  cost is not the concern;  spam and cascade are).
#
#     muse exec --model muse-spark-1.3-contributor --reasoning-effort minimal
#               --prompt-file <prompt> --output-schema <schema>
#               --json --approval-mode never
#               --disable-shell --disable-write --disable-web-tools
#               --no-session-log --workspace <inert dir>
#               --max-model-steps N --no-foreign-personal-context
#
# Why this runtime:
#   * `--output-schema` shapes the final answer to a JSON schema, and that schema IS the wake
#     contract.  Constraint:  meta provider only, and meta is the default.
#   * The sandbox is ON by default;  `--disable-sandbox` turns it off.  The claude wake had to work
#     to reach an equivalent state.
#   * `--disable-shell`, `--disable-write` and `--no-session-log` are separate explicit switches, so
#     "read this topic, touch nothing" is stated rather than inferred.
#   * `--approval-mode never` cannot block on a prompt.
#
# Two runtime facts Gate 0 proved, both of which cost a run to learn:
#   * `--reasoning-effort none` DOES NOT EXIST on the meta provider.  `muse model-profile show
#     --effort none` accepts it and echoes `effort: none`, then a real run exits 2 with "not
#     supported with --provider meta; choose minimal|low|medium|high|xhigh|max|ultra".  The
#     model-profile tool reports a value the runner will not accept, so MUSE_REASONING_EFFORT is
#     `minimal`, the lowest tier that actually runs.
#   * The model id is `muse-spark-1.3-contributor`, NOT `muse-spark-1.3`.  The catalog carries three
#     variants and the plain one is 15x dearer.  `model` still comes from the seat's config so a
#     seat can be moved deliberately;  the default here is the contributor tier.
#
# The envelope differs from claude's stream-json:  muse emits MSP records, and the answer is the
# `text` of the `run.terminal.completed` record.  `--output-schema` makes that text the JSON object
# itself (Gate 0:  it round-tripped through wakes.validate with zero coercion, nested objects and
# enums included), so parse_muse_result reads it the same way parse_result reads a claude result.

MUSE_MAX_MODEL_STEPS = 8
MUSE_REASONING_EFFORT = "minimal"  # the lowest effort `muse exec` accepts on the meta provider
# `--output-schema` needs the meta provider (the default).  Naming it keeps the contract explicit.
MUSE_PROVIDER = "meta"
MUSE_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wake", "wake-schema.json"
)
# muse takes the prompt, the schema and the workspace by path;  the argv carries these slots and
# MuseRunner.run swaps in the private files it wrote, so the argv can be printed and diffed as-is.
MUSE_PROMPT_SLOT = "<prompt-file>"
MUSE_SCHEMA_SLOT = "<output-schema>"
MUSE_WORKSPACE_SLOT = "<workspace>"
MUSE_WAKE_POLICY_VERSION = "muse-chat-catalog-v2"


def muse_version_key(name: str) -> tuple[tuple[int, ...], str]:
    """Numeric order for ``muse-bin-1.4.10-R6427.1`` style names, so 1.4.10 sorts after 1.4.9."""
    return tuple(int(part) for part in re.findall(r"\d+", name)), name


#:  How long a wake-PATH scan is reused.  The daemon's status tick runs at most every two
#:  seconds, so the TTL has to OUTLIVE the tick:  a TTL of two seconds expires between two
#:  ticks, so every tick still pays the full scan and the memo never hits.  Ten seconds spans
#:  five ticks, so one scan serves them all, and a build that appears or disappears is still
#:  noticed within ten seconds (Kody, PR #880).
MUSE_SCAN_TTL_SECONDS = 10.0

#:  The last scan per wake PATH, with the monotonic time it was taken.  The daemon's seat
#:  threads share it, so the lock keeps the memo consistent between them.
_muse_scan_cache: dict[str, tuple[float, str | None]] = {}
_muse_scan_lock = threading.Lock()


def _versioned_muse_builds(directory: str) -> list[tuple[tuple[tuple[int, ...], str], str]]:
    """The executable ``muse-bin-*`` builds in one directory, with their version keys."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found: list[tuple[tuple[tuple[int, ...], str], str]] = []
    for name in names:
        if not name.startswith("muse-bin-"):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            # The listed name carries the version; a symlink target may not.
            found.append((muse_version_key(name), os.path.realpath(path)))
    return found


def _newest_versioned_muse(wake_path: str) -> str | None:
    """The newest versioned muse build in the FIRST PATH directory that carries one.

    PATH order is the trust order.  The scan stops at the first directory that has a versioned
    build, so a later directory cannot plant a higher-version build that outranks the first
    one:  on macOS /opt/homebrew/bin is group-writable, and the build that wins here runs with
    the daemon's privileges and ingests untrusted Zulip DMs (Kody, PR #880).
    """
    for directory in wake_path.split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        candidates = _versioned_muse_builds(directory)
        if candidates:
            return max(candidates)[1]
    return None


def newest_versioned_muse(wake_path: str) -> str | None:
    """The newest versioned muse build on the wake PATH, or None.

    The muse launcher replaces its versioned build on every release and DELETES the one it
    replaced, so a seat whose config names an absolute ``muse-bin-<version>`` path points at a
    file that no longer exists after the next release.  The launcher shim is deliberately not
    returned:  the wake runs the versioned build itself, so the pin names a real build.

    PATH order is the trust order:  the newest build of the FIRST PATH directory that carries one
    wins, so a later directory cannot outrank it (Kody, PR #880).

    The scan is memoized for a few seconds:  the daemon's status tick calls this once per muse
    seat every two seconds, and on macOS /usr/bin and /bin are the same directory, so an
    uncached scan reads ~7,000 dirents per seat per tick (Kody, PR #880).  The memo spans
    several ticks, so one scan serves them all, and it is revalidated on every hit, because the
    launcher DELETES the build it replaced and a warm memo must not serve a file that is gone.
    """
    now = time.monotonic()
    with _muse_scan_lock:
        cached = _muse_scan_cache.get(wake_path)
    if cached is not None and now - cached[0] < MUSE_SCAN_TTL_SECONDS:
        found = cached[1]
        # One stat is cheap and a directory listing is not, so the memoized answer is checked
        # before it is served:  a build the launcher deleted must not be handed back.
        if found is None or os.access(found, os.X_OK):
            return found
    found = _newest_versioned_muse(wake_path)
    with _muse_scan_lock:
        expired = [
            path
            for path, (taken, _) in _muse_scan_cache.items()
            if now - taken >= MUSE_SCAN_TTL_SECONDS
        ]
        for path in expired:
            _muse_scan_cache.pop(path, None)
        _muse_scan_cache[wake_path] = (now, found)
    return found


def resolve_muse(explicit: str | None, wake_path: str) -> str | None:
    """The muse binary:  the configured absolute path when it still exists, else the newest
    versioned build on the fixed wake PATH, else `muse` on that PATH.  Mirrors resolve_claude so a
    seat pins and runs the same resolved realpath.

    A configured absolute path that has been DELETED falls back to the newest versioned build
    rather than resolving to nothing.  The muse launcher replaces its versioned build on every
    release and deletes the one it replaced (AFC-491), so a config naming one absolute
    ``muse-bin-<version>`` path went dead on every release, and the seat read as healthy because a
    missing binary resolves to None, which reports no pin state at all.  The fallback keeps the
    seat on a real build, where the pin check reports `pin_stale` and the owner is told to re-pin
    it:  the wake is still held, because the pin is what proves the build's wake contract, and
    this way the hold is visible instead of silent.

    A RELATIVE path is refused rather than guessed:  it is a config error, not a moved binary.
    """
    if explicit:
        if not os.path.isabs(explicit):
            return None
        if os.access(explicit, os.X_OK):
            return explicit
        newer = newest_versioned_muse(wake_path)
        if newer:
            return newer
        found = shutil.which("muse", path=wake_path)
        return os.path.abspath(found) if found else None
    newer = newest_versioned_muse(wake_path)
    if newer:
        return newer
    found = shutil.which("muse", path=wake_path)
    return os.path.abspath(found) if found else None


def muse_argv(muse: str, model: str) -> list[str]:
    """The fixed wake argv.  `muse` should be the pinned realpath (binary_pin), so the build that
    runs is the build that was verified, even if the PATH symlink moves in between.

    The three path slots are filled in by MuseRunner.run against the wake dir;  the fleet tag set
    (the enum of the schema's route seat) travels in the schema file itself (wakes.schema_text),
    which is why it is not an argv parameter here."""
    return [
        muse,
        "exec",
        "-c",
        "run.toolset=[]",
        "--model",
        model,
        "--reasoning-effort",
        MUSE_REASONING_EFFORT,
        "--provider",
        MUSE_PROVIDER,
        "--prompt-file",
        MUSE_PROMPT_SLOT,
        "--output-schema",
        MUSE_SCHEMA_SLOT,
        "--json",
        "--approval-mode",
        "never",
        "--disable-shell",
        "--disable-write",
        "--disable-web-tools",
        "--disable-reminders",
        "--no-session-log",
        "--workspace",
        MUSE_WORKSPACE_SLOT,
        "--max-model-steps",
        str(MUSE_MAX_MODEL_STEPS),
        "--no-foreign-personal-context",
    ]


def muse_env(
    env: Mapping[str, str], home: str, wake_path: str, *, seat: str | None = None
) -> dict[str, str]:
    """`env -i` plus a fixed list, the same shape as claude_env.

    Every ANTHROPIC_*, CLAUDE_*, AGENT_*, ZULIP_* and MUSE_* variable is dropped, so the run uses
    the muse subscription login the daemon's user already has and never a second seat's identity.
    With `seat` (a wake, or test-wake) the listener is the reference launcher (FLEET-PROTOCOL
    Identity Rules):  it names the seat it is waking and clears every inherited identity variable
    first.  The wake has no tools and posts nothing itself (the daemon posts its validated reply as
    the seat's bot), so this only keeps the seat explicit."""
    user = env.get("USER") or env.get("LOGNAME") or os.path.basename(home)
    out = {
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "LANG": "en_US.UTF-8",
        "TMPDIR": env.get("TMPDIR") or "/tmp",
        "PATH": wake_path,
    }
    if seat:
        out.update(
            {
                "AGENT_LAUNCHER": WAKE_LAUNCHER,
                "AGENT_LAUNCH_SEAT": seat,
                "AGENT_SEAT": seat,
                "AGENT_SYNC_ATTACH": "0",
            }
        )
    return out


def parse_muse_records(text_lines: Iterable[str]) -> tuple[str | None, str | None, list[str]]:
    """(the final answer, why it is refused, the model ids seen) from muse's MSP records.

    Each stdout line is one record.  The answer is the `text` of `run.terminal.completed`; a terminal
    record that is not `completed` is a refusal that names its own reason.  `run.model.configured`
    names the model that was actually asked for, and it is recorded so test-wake can show it:  a
    wake that silently ran a different model than the seat configured is exactly the failure the pin
    exists to catch.  Returns one model id or several when the run switched models."""
    answer: str | None = None
    why: str | None = None
    models: list[str] = []
    for raw in text_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        payload_type = str(record.get("payload_type") or "")
        if payload_type == "run.model.configured" and isinstance(payload, dict):
            model = payload.get("model_id")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
        elif payload_type == "run.terminal.completed" and isinstance(payload, dict):
            answer = payload.get("text") if isinstance(payload.get("text"), str) else None
        elif payload_type.startswith("run.terminal.") and isinstance(payload, dict):
            # Any terminal record that is not `completed` is a refusal.  The name may sit in the
            # record's own `terminal` field, in the payload_type's suffix, or in `reason`, and a
            # bare reason with no terminal name still means the run did not complete.
            terminal = str(payload.get("terminal") or "") or payload_type[len("run.terminal.") :]
            reason = payload.get("reason")
            detail = clean_banner(str(reason), 120) if isinstance(reason, str) and reason else ""
            why = "%s (%s)" % (terminal, detail) if detail else terminal
    return answer, why, models


def parse_muse_result(text: str | None) -> Any:
    """The final answer text as JSON, or None.  `--output-schema` makes the terminal text the JSON
    object itself, so this is json.loads with the same leniency as the claude path (a fenced or
    prefixed block still parses) rather than a separate grammar."""
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


class MuseRunner:
    """Runs one headless muse wake.  Same lifecycle contract as ClaudeRunner:  `on_start`/`on_exit`
    bracket each child so the daemon can track every running child and kill each group on SIGTERM.

    Two structural differences from the claude run, both forced by the runtime:
      * The prompt goes in a FILE (`--prompt-file`), not on stdin, so it is written to the private
        wake dir and removed afterwards.
      * The schema goes in a FILE too, so it is materialised next to the prompt from the same
        in-memory source the claude wake passes as a string.  One schema, two transports."""

    def __init__(
        self,
        *,
        timeout: float = WAKE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        on_start: Callable[[subprocess.Popen[Any]], None] | None = None,
        on_exit: Callable[[subprocess.Popen[Any]], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.clock = clock
        self.on_start = on_start
        self.on_exit = on_exit

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        prompt: str,
        schema: str | None = None,
    ) -> RunResult:
        out = RunResult()
        try:
            with open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = fh.read()
        except (OSError, UnicodeError):
            out.error = "wake contract unavailable"
            return out
        L.private_dir(cwd)
        prompt_path = os.path.join(cwd, "prompt.md")
        schema_path = os.path.join(cwd, "schema.json")
        workspace = os.path.join(cwd, "workspace")
        L.private_dir(workspace)
        fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contract.rstrip() + "\n\n--- BEGIN DAEMON WAKE PROMPT ---\n" + prompt)
        # The schema is written from the same source claude passes inline, so the two runtimes can
        # never drift onto different contracts.
        fd = os.open(schema_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(schema if schema is not None else schema_text())
        # muse takes these three by path, and the paths are the wake dir's own private files, so
        # the argv is rebuilt here with the real paths in place of the placeholders.
        argv = [
            prompt_path
            if a == MUSE_PROMPT_SLOT
            else schema_path
            if a == MUSE_SCHEMA_SLOT
            else workspace
            if a == MUSE_WORKSPACE_SLOT
            else a
            for a in argv
        ]
        started = self.clock()
        proc: subprocess.Popen[Any] | None = None
        try:
            try:
                proc = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                out.error = "could not start muse (%s)" % (exc.strerror or type(exc).__name__)
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
            stdout_chunks: list[str] = []
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
                stdout_chunks.append(raw.decode("utf-8", "replace"))
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
            text, why, models = parse_muse_records(stdout_chunks)
            out.models = models
            if out.refused is None and out.error is None:
                if why:
                    out.error = why  # a terminal record that is not `completed` (a model refusal)
                elif text is None:
                    out.error = "no parseable result"
                elif out.exit not in (0, None):
                    out.error = "muse exited %s" % out.exit
                else:
                    out.result = {"text": text}
                    out.parsed = parse_muse_result(text)
        finally:
            if proc is not None and self.on_exit:
                self.on_exit(proc)
            for path in (prompt_path, schema_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            out.secs = round(self.clock() - started, 2)
        # A subscription-backed run has no per-run dollar amount, so there is nothing to reserve
        # against and nothing to debit:  cost_usd stays 0.  The count caps are what bound a seat.
        out.cost_usd = 0.0
        return out


def muse_wake_fingerprint() -> str | None:
    """Fingerprint the fixed Muse wake command and trusted inputs, or fail closed if unavailable.

    Binary path and model are tracked separately.  The argv fingerprint uses stable placeholders
    for those values while retaining every fixed flag and path slot; the policy version covers
    runner semantics that are not represented by argv.
    """
    try:
        with open(CONTRACT_PATH, "rb") as fh:
            contract = fh.read()
        # Ensure the bytes can be consumed by MuseRunner as UTF-8 before considering them pinnable.
        contract.decode("utf-8")
        schema = schema_text()
    except (OSError, UnicodeError, ValueError):
        return None
    command = json.dumps(
        {
            "policy_version": MUSE_WAKE_POLICY_VERSION,
            "argv": muse_argv("<muse>", "<model>"),
            "schema": schema,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(command)
    digest.update(b"\0")
    digest.update(contract)
    return digest.hexdigest()


def muse_pin_state(paths: L.SeatPaths, muse: str | None) -> dict[str, Any]:
    """Why the muse pin does or does not match the binary.  Same discipline as the claude pin
    (AFC-256), on the muse seat's own file so a claude pin can never stand in for a muse one.

    `why` is None when pinned, else `no_muse` (no binary to pin), `not_pinned` (nothing recorded
    yet), `pin_unverified` (a pin from before the fingerprint existed, so it vouches for nothing
    about this command), `pin_stale` (the binary moved, or the command, contract or schema changed
    under it), or `pin_unreadable` (the contract or schema could not be read, so nothing can be
    verified).  A seat whose muse pin has never been verified HOLDS and says why:  it never
    self-heals, because the pin records that a person checked this build's wake contract.  The
    last three are named apart because the remediation differs:  a moved binary and a changed
    contract both end in re-pinning, an unreadable contract does not, and only one of them is a
    moved binary."""
    if not muse:
        return {"pinned": False, "why": "no_muse", "pinned_muse": None, "muse": None}
    current = binary_pin(muse)
    pin = L.read_json(paths.wake_pin_muse)
    recorded = pin.get("muse")
    if not isinstance(recorded, str) or not recorded:
        return {"pinned": False, "why": "not_pinned", "pinned_muse": None, "muse": current}
    if recorded != current:
        return {"pinned": False, "why": "pin_stale", "pinned_muse": recorded, "muse": current}
    recorded_fingerprint = pin.get("fingerprint")
    if not isinstance(recorded_fingerprint, str) or not recorded_fingerprint:
        # The binary is the same, so this pin did verify SOMETHING;  it just predates the
        # fingerprint and says nothing about the current command, contract or schema.
        return {
            "pinned": False,
            "why": "pin_unverified",
            "pinned_muse": recorded,
            "muse": current,
        }
    current_fingerprint = muse_wake_fingerprint()
    if current_fingerprint is None:
        # The contract or the schema could not be read at all.  That is NOT a changed pin, and
        # the two need opposite advice:  a changed pin is fixed by re-pinning, whereas re-pinning
        # here re-reads the same unreadable file and produces the same refusal forever.
        return {
            "pinned": False,
            "why": "pin_unreadable",
            "pinned_muse": recorded,
            "muse": current,
        }
    if recorded_fingerprint != current_fingerprint:
        # A pin made before the command or the contract changed verified a wake this seat no
        # longer runs.
        return {"pinned": False, "why": "pin_stale", "pinned_muse": recorded, "muse": current}
    return {"pinned": True, "why": None, "pinned_muse": recorded, "muse": current}


def muse_pinned(paths: L.SeatPaths, muse: str | None) -> bool:
    return muse_pin_state(paths, muse)["pinned"]


#:  The remediation the operator needs for a muse pin that no longer matches.  No `--runtime` is
#:  named because a muse pin is only ever read for a seat whose `wake` is `muse`, which is exactly
#:  what `test-wake`'s default "auto" resolves to;  the fallback pin carries `--runtime fallback`
#:  because it is read for a seat whose wake is something else.
MUSE_PIN_REMEDIATION = (
    "`simple-chat daemon test-wake --seat %s --run`, then "
    "`simple-chat daemon test-wake --seat %s --pin`"
)

#:  A pin cannot be verified at all while the contract or the schema is unreadable, so the
#:  remediation is not a re-pin:  the same re-pin would re-read the same missing file and refuse
#:  again.
MUSE_PIN_UNREADABLE_REMEDIATION = (
    "restore the wake contract and result schema, then run "
    "`simple-chat daemon test-wake --seat %s --run` and `--pin`"
)

#:  A seat whose wake runtime is not installed at all holds every wake, so it must be red:  the
#:  remediation is to install the runtime, not to re-pin a binary that is not there.  The muse
#:  launcher removes the build it replaces, so this is what a config that named a deleted
#:  versioned path used to hit (AFC-491), and the seat read as healthy because a missing binary
#:  resolved to None and reported no pin state at all.
MUSE_NO_BINARY_REMEDIATION = (
    "install a muse build on the wake PATH or point seat.%s.muse at one, then run "
    "`simple-chat daemon test-wake --seat %s --run` and `--pin`"
)


def muse_no_binary_red_entry(seat: str) -> str:
    """The one-line red entry for a muse seat with no muse binary at all.

    It is a different fault from a stale pin:  there is nothing to re-pin until the runtime is
    installed, so the line must not read as "run --pin" against a missing binary.
    """
    return "no muse binary found, so %s wakes are inbox-only; %s" % (
        seat,
        MUSE_NO_BINARY_REMEDIATION % (seat, seat),
    )


def muse_configured_path_missing(explicit: str | None) -> bool:
    """True when the seat names an absolute muse path that is no longer there.

    The fallback then binds the seat to a build that appears nowhere in listener.toml, and the
    launcher deletes that build on the next release, so the seat breaks again at every release
    unless the operator repoints ``seat.X.muse`` at the build it is actually running
    (Kody, PR #880).
    """
    if not explicit or not os.path.isabs(explicit):
        return False
    return not os.access(explicit, os.X_OK)


def muse_repoint_red_entry(seat: str, resolved: str | None, configured: str | None) -> str:
    """The one-line red entry for a muse seat running a build its own config does not name.

    The seat works today, so the line must not read as a broken seat;  it must say that the
    CONFIG is what breaks at the next muse release, and name the build to point it at, because
    re-pinning alone records a build the launcher will delete (Kody, PR #880).
    """
    return (
        "wake is running muse %s, which seat.%s.muse does not name (it names %s); repoint "
        "seat.%s.muse to that build, or the next muse release will break this seat again"
        % (resolved or "<none>", seat, configured or "<none>", seat)
    )


def muse_pin_cause(pin: Mapping[str, Any]) -> str:
    """Why a muse pin no longer matches, in the operator's words.

    `muse_pin_state` returns `pin_stale` for two different causes, `pin_unverified` for a quieter
    third and `pin_unreadable` when the contract or schema cannot be read at all.  They read the
    same at a glance but are not the same fault:  a moved binary is a path that changed, while a
    changed command, contract or schema leaves the path alone, and an unreadable contract means
    the wake was probably never wrong at all.  Reporting the wrong one points the operator at a
    path that never moved, shows the same path twice, or sends them to re-pin something that
    cannot be re-pinned.
    """
    if pin.get("why") == "pin_unverified":
        return (
            "the pin predates the command fingerprint, so it verified nothing about the current "
            "muse wake"
        )
    if pin.get("why") == "pin_unreadable":
        return (
            "the wake contract or result schema could not be read, so the current muse wake "
            "cannot be verified at all (pinned %s)" % pin.get("pinned_muse")
        )
    if pin.get("muse") != pin.get("pinned_muse"):
        return "muse moved to %s (pinned %s)" % (pin.get("muse"), pin.get("pinned_muse"))
    return "the muse command, contract or schema changed (pinned %s)" % pin.get("pinned_muse")


def record_muse_pin(
    paths: L.SeatPaths,
    muse: str,
    now: float,
    *,
    model: str | None = None,
    fingerprint: str | None = None,
) -> None:
    L.write_json(
        paths.wake_pin_muse,
        {
            "muse": binary_pin(muse),
            "at": now,
            "model": model,
            "effort": MUSE_REASONING_EFFORT,
            "fingerprint": fingerprint if fingerprint is not None else muse_wake_fingerprint(),
        },
    )


def record_muse_candidate(
    paths: L.SeatPaths, muse: str, now: float, *, model: str | None = None
) -> None:
    """A binary that passed `test-wake --run`, waiting for a person to read the result and pin it."""
    L.write_json(
        paths.wake_pin_candidate_muse,
        {
            "muse": binary_pin(muse),
            "at": now,
            "model": model,
            "effort": MUSE_REASONING_EFFORT,
            "fingerprint": muse_wake_fingerprint(),
        },
    )


def muse_pin_candidate(paths: L.SeatPaths, muse: str, now: float) -> str | None:
    """Pin the candidate `test-wake --run` left.  None on success, else why it was not pinned."""
    cand = L.read_json(paths.wake_pin_candidate_muse)
    if not cand:
        return "no passing test-wake --run to pin; run that first"
    current_fingerprint = muse_wake_fingerprint()
    if current_fingerprint is None:
        # Re-running does not restore a missing contract:  it reproduces this same refusal, so the
        # advice names the read that failed rather than a re-test that cannot succeed.
        return (
            "the wake contract or result schema is unavailable, so the muse wake cannot be "
            "pinned; restore them, then run test-wake --run again"
        )
    recorded_fingerprint = cand.get("fingerprint")
    if (
        not isinstance(recorded_fingerprint, str)
        or not recorded_fingerprint
        or recorded_fingerprint != current_fingerprint
    ):
        return "the muse wake command or contract changed since the test; run test-wake --run again"
    at = cand.get("at")
    if not isinstance(at, (int, float)) or now - float(at) > CANDIDATE_SECONDS or now < float(at):
        return "the last passing test-wake --run is over a day old; run it again"
    if cand.get("muse") != binary_pin(muse):
        return (
            "the muse binary changed since the test (%s, tested %s); run test-wake --run again"
            % (binary_pin(muse), cand.get("muse"))
        )
    record_muse_pin(
        paths,
        muse,
        now,
        model=cand.get("model") if isinstance(cand.get("model"), str) else None,
        fingerprint=recorded_fingerprint,
    )
    try:
        os.unlink(paths.wake_pin_candidate_muse)
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------------------------
# Provider quota gate (CodeCaps)
# --------------------------------------------------------------------------------------------
#
# Jay 2026-10-11:  no reason to try to wake an agent that cannot run.  CodeCaps (the Mac menu-bar
# quota app) publishes what each provider has left;  a wake whose provider is spent is refused
# BEFORE the subprocess spawns, so an exhausted seat costs no run, no queue slot and no retry.
#
# The source is the Usage Monitor snapshot (docs/protocols/agent-sync-listener.md), read with the
# same rules the rest of the fleet uses for it:  a regular same-user file, no symlink, capped size,
# and a 15-minute expiry (the producer's 5-minute cadence plus two missed cycles).  A file that is
# missing, stale, malformed or simply has no window for this provider NEVER blocks a wake:  this is
# an optimisation, not a safety control, and a quota source that is down must not silence the fleet.

QUOTA_PATH = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Usage Monitor", "quota-windows.json"
)
QUOTA_MAX_AGE = 15 * 60
QUOTA_MAX_BYTES = 1024 * 1024
QUOTA_FORMAT = "usage-monitor-local-quotas"
QUOTA_NEVER_CAP_PROVIDERS = frozenset({"minimax", "muse"})


def _quota_file(path: str) -> dict[str, Any] | None:
    """The parsed snapshot, or None when it is not a fresh, regular, same-user file we can trust."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if (
        os.path.islink(path)
        or st.st_size <= 0
        or st.st_size > QUOTA_MAX_BYTES
        or not os.path.isfile(path)
    ):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != QUOTA_FORMAT:
        return None
    return data


def _quota_generated_at(value: Any) -> float | None:
    """The snapshot's `generatedAt` as a unix timestamp, or None when it is not one.

    The producer writes UTC with a trailing `Z`.  `strptime` parses that as LOCAL time, so
    subtracting `time.timezone` is wrong by an hour for an hour a year, and a reading one minute
    old would be judged stale (or a day-old one judged fresh) exactly when it matters.  `datetime`
    with an explicit UTC offset is the only correct reading, and `utcfromtimestamp` is deprecated."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def provider_quota(
    provider: str, now: float | None = None, path: str | None = None
) -> dict[str, Any]:
    """One provider's verdict from the CodeCaps snapshot.

    `known` False means the snapshot cannot speak for this provider (no file, stale, malformed, or
    no matching window), and the caller must NOT block:  an absent reading is not an exhausted one.
    `exhausted` is the producer's OWN verdict only (`isExhausted` or `skip`), never a derived 0%
    percentage:  a passed reset and an unknown remainder also render as 0%, and only one of those
    three means the provider refused to serve."""
    key = (provider or "").strip().casefold()
    unknown = {
        "known": False,
        "exhausted": False,
        "remaining": None,
        "reset_at": None,
        "reason": None,
        "source": "unknown",
    }
    if not key:
        return unknown
    data = _quota_file(path or QUOTA_PATH)
    if data is None:
        return unknown
    at = time.time() if now is None else float(now)
    generated = _quota_generated_at(data.get("generatedAt"))
    if generated is None:
        return unknown  # an undated snapshot cannot be aged, so it cannot be trusted
    if generated - at > QUOTA_MAX_AGE or at - generated > QUOTA_MAX_AGE:
        return unknown  # in the future by more than the window, or older than the window
    windows = data.get("windows")
    if not isinstance(windows, list):
        return unknown
    matched = 0
    exhausted = False
    remaining: float | None = None
    reset_at: str | None = None
    reason: str | None = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        names = {
            str(window.get("provider") or "").casefold(),
            str(window.get("providerKey") or "").casefold(),
        }
        if key not in names and not any(key in name for name in names if name):
            continue
        matched += 1
        if window.get("isExhausted") is True or window.get("skip") is True:
            exhausted = True
            why = window.get("fileSkipReason")
            reason = clean_banner(str(why), 160) if isinstance(why, str) and why else reason
        value = window.get("remainingPercent")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            remaining = float(value) if remaining is None else min(remaining, float(value))
        if isinstance(window.get("resetAt"), str) and not reset_at:
            reset_at = window["resetAt"]
    if not matched:
        return unknown
    return {
        "known": True,
        "exhausted": exhausted,
        "remaining": remaining,
        "reset_at": reset_at,
        "reason": reason,
        "source": "codecaps",
    }


def quota_block(provider: str, now: float | None = None, path: str | None = None) -> str | None:
    """Why this wake must not start, or None.  Only a fresh, positive exhaustion reading blocks;
    everything else returns None so a quota source that is down never silences the fleet."""
    if (provider or "").strip().casefold() in QUOTA_NEVER_CAP_PROVIDERS:
        return None  # muse runs on the MiniMax subscription the daemon's user already pays for
    state = provider_quota(provider, now, path)
    if state["known"] and state["exhausted"]:
        reset = state.get("reset_at")
        return "%s quota is exhausted%s" % (
            provider,
            (" until %s" % clean_banner(str(reset), 40)) if reset else "",
        )
    return None


def quota_red(provider: str, now: float | None = None, path: str | None = None) -> str | None:
    """The status page's RED line for a seat whose wake the quota gate stops, or None.

    It is the gate's own verdict (quota_block), never a second reading of the same file:  a provider the gate
    never caps (muse, minimax) shows no RED however its quota reads, because for those the reading is information
    and no wake is stopped.  The status used to read the quota itself and showed "wakes are not running" for a muse
    seat whose wakes the gate was letting through."""
    blocked = quota_block(provider, now, path)
    return "wakes are not running: the %s" % blocked if blocked else None


# --------------------------------------------------------------------------------------------
# mcode exec:  the fallback runtime (AFC-291)
# --------------------------------------------------------------------------------------------
#
#   mcode exec --prompt-mode work --permission off --output-format json
#             --model minimax/MiniMax-M3.1-Flash
#             --output-schema <schema> --max-steps N --timeout 4m --cwd <inert dir>
#             -o <last-message file> --prompt
#
# The same contract shape as muse:  `--output-schema` shapes the final answer, `--permission off`
# cannot block on a prompt, and Gate 0 proved the wake schema round-trips through it with zero
# coercion and a populated `board` object.  `-o` writes the final message to its own file, which is
# a cleaner transport than muse's MSP envelope:  the answer is read straight off disk.
#
# WHY IT IS THE FALLBACK AND NEVER THE PRIMARY (measured, Oct 11 2026):
#   * mcode has NO sandbox toggle.  muse sandboxes by default and needs --disable-sandbox to turn it
#     off;  mcode has no equivalent at all, so there is no switch to state the posture with.
#   * mcode has NO --disable-shell, --disable-write or --disable-web-tools.  `--permission off`
#     means "never ask", NOT "hold no tools".  An unattended wake that posts to a third-party cloud
#     wants the stronger statement, and only muse can make it.
#   * mcode cannot be HOME-isolated.  Pointing HOME at the inert workspace was tried and refused:
#     "Sign in to MiniMax to use Agent features", because the credential lives under HOME.  muse
#     drops every vendor and identity variable instead, so its run touches nothing but the wake.
# Both facts are verified behaviour, not documentation reading, and they are the whole reason the
# order is muse then mcode and never the reverse.

# The step budget, measured.  At 8 the wake contract does not fit:  mcode answers a four-message
# pending set in more steps than that and exits 7 with
# "mcode exec limit_exceeded: The run reached its step limit.", which the daemon reads as a failed
# fallback.  A failed fallback can never be pinned, so the seat keeps its old runtime forever.
# The same run at 16 steps exits 0 with the contract's answer.  The wall clock is still bounded by
# MCODE_TIMEOUT, so the extra steps cost time and nothing else.
MCODE_MAX_STEPS = 16
# The child's OWN deadline, and it must stay strictly under the runner's outer deadline
# (WAKE_TIMEOUT = 240s).  When the two are equal the daemon's group-kill, not mcode's clean
# timeout, is what ends a long fallback run:  the ledger then records "timed out after 240
# seconds" instead of mcode's own timeout, and a failing wake occupies the seat's serialized
# worker for the whole outer budget.  With the child under the outer deadline mcode's timeout
# fires first and the daemon's kill stays a backstop.
#
# 210s, not 180s:  the measured run takes about 145s, and the fallback is the LAST resort, so a
# timeout here means no answer at all rather than a slower answer.  The margin over the measured
# run is therefore worth more than the 30s it adds to a path that already spent the primary's
# 240s.  It is still 30s under the outer deadline, so the daemon's kill stays a backstop.
MCODE_TIMEOUT = "3m30s"
MCODE_PROMPT_MODE = "work"
MCODE_PERMISSION = "off"
# The fallback's PROMPT BOUND, and the reason the step budget is provable at all.  mcode has no
# sandbox and no tool-deny switch, so `--max-steps` is the ONLY bound on how much the fallback may
# act, and the step cost grows with the prompt:  the daemon can build a wake prompt with up to
# PROMPT_HISTORY (15) messages plus items_in_wake (20) held-back entries, while the budget is
# measured on a FOUR-message prompt.  An unbounded prompt would blow a budget that was never
# measured at that size, and would hand a misled model twice the live rounds before the cap
# stopped it.  The fallback therefore sees at most this many messages -- the trigger messages
# first, then the newest of the rest -- and no held-back entries, which makes MCODE_MAX_STEPS the
# smallest budget the contract provably needs for the prompt it is actually given.
MCODE_FALLBACK_MAX_MESSAGES = 4
MCODE_FALLBACK_MAX_HELD = 0
# Bump when the fallback command's behaviour changes in a way argv does not show, so an existing
# pin is re-verified by a person rather than trusted under the old policy.  v2: the prompt is
# bounded and the child's own timeout sits under the daemon's deadline, and neither of those
# shows in the argv.
MCODE_WAKE_POLICY_VERSION = "mcode-fallback-bounded-v2"
MCODE_PROMPT_SLOT = "<prompt-file>"
MCODE_SCHEMA_SLOT = "<output-schema>"
MCODE_CWD_SLOT = "<cwd>"
MCODE_LAST_SLOT = "<last-message>"


def resolve_mcode(explicit: str | None, wake_path: str) -> str | None:
    """The mcode binary:  the configured absolute path, else `mcode` on the fixed wake PATH."""
    if explicit:
        return explicit if os.path.isabs(explicit) and os.access(explicit, os.X_OK) else None
    found = shutil.which("mcode", path=wake_path)
    return os.path.abspath(found) if found else None


def mcode_argv(mcode: str, model: str = MCODE_DEFAULT_MODEL) -> list[str]:
    """The fixed fallback argv.  The schema, the inert cwd and the last-message file are path slots
    filled in by McodeRunner.run, so the argv prints and diffs as-is.

    The model is named on the command line (`--model provider/model`).  Without it mcode inherits
    MiniMax Code's own `defaultModel`, which is whatever that install happens to carry and can be
    an id the current catalog does not serve, in which case the run times out with no usage and no
    answer.  Naming the model here makes the backup runtime run on a model the catalog serves.

    The prompt arrives on STDIN, not as a positional argument:  mcode has no `--prompt-file`, and
    `--input -` is its documented stdin form.  Keeping the prompt off the command line also keeps it
    out of any process listing for the life of the run."""
    return [
        mcode,
        "exec",
        "--prompt-mode",
        MCODE_PROMPT_MODE,
        "--permission",
        MCODE_PERMISSION,
        "--output-format",
        "json",
        "--model",
        model,
        "--input",
        "-",
        "--output-schema",
        MCODE_SCHEMA_SLOT,
        "--max-steps",
        str(MCODE_MAX_STEPS),
        "--timeout",
        MCODE_TIMEOUT,
        "--cwd",
        MCODE_CWD_SLOT,
        "-o",
        MCODE_LAST_SLOT,
    ]


def mcode_env(
    env: Mapping[str, str], home: str, wake_path: str, *, seat: str | None = None
) -> dict[str, str]:
    """`env -i` plus a fixed list, the same shape as the other two.

    Unlike muse, HOME must stay the real one:  mcode reads its sign-in from there and refuses to run
    otherwise (verified).  That is a real difference in the lockdown, recorded rather than hidden."""
    user = env.get("USER") or env.get("LOGNAME") or os.path.basename(home)
    out = {
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "LANG": "en_US.UTF-8",
        "TMPDIR": env.get("TMPDIR") or "/tmp",
        "PATH": wake_path,
    }
    if seat:
        out.update(
            {
                "AGENT_LAUNCHER": WAKE_LAUNCHER,
                "AGENT_LAUNCH_SEAT": seat,
                "AGENT_SEAT": seat,
                "AGENT_SYNC_ATTACH": "0",
            }
        )
    return out


def parse_mcode_result(
    status: str | None, text: str | None, stderr: str = ""
) -> tuple[str | None, list[str]]:
    """(why the run failed, the model ids it names) from mcode's envelope and its last message.

    mcode's `exec.result` reports `status` ("succeeded", or whatever went wrong) and `model`;  the
    answer itself is the file `-o` wrote.  A failed status is returned as the reason, with the
    first line of stderr when there is one, so a fallback failure is legible rather than a blank."""
    models: list[str] = []
    if status and status != "succeeded":
        detail = clean_banner(stderr.splitlines()[0], 160) if stderr.splitlines() else ""
        return ("%s%s" % (status, " (%s)" % detail if detail else ""), models)
    return None, models


class McodeRunner:
    """Runs one headless mcode fallback.  Same lifecycle contract as the other two runners:  the
    child gets its own process group, is tracked while it runs, and is killed on SIGTERM."""

    def __init__(
        self,
        *,
        timeout: float = WAKE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        on_start: Callable[[subprocess.Popen[Any]], None] | None = None,
        on_exit: Callable[[subprocess.Popen[Any]], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.clock = clock
        self.on_start = on_start
        self.on_exit = on_exit

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        prompt: str,
        schema: str | None = None,
    ) -> RunResult:
        out = RunResult()
        L.private_dir(cwd)
        schema_path = os.path.join(cwd, "schema-mcode.json")
        workspace = os.path.join(cwd, "workspace-mcode")
        # A FRESH output file per attempt, and it is removed before the child starts.  A fixed name
        # that survives a crash is a replay hazard:  the next run writes nothing, exits non-zero or
        # dies, and the leftover valid answer from the PREVIOUS wake is read as this one's reply —
        # an old "action: board" would then be acted on again.  The name carries the pid and clock so
        # two attempts can never share it, and an unlink before launch makes "missing" mean "this
        # attempt produced nothing" rather than "something was there".
        last_path = os.path.join(
            cwd, "last-mcode-%d-%d.json" % (os.getpid(), int(self.clock() * 1000))
        )
        try:
            os.unlink(last_path)
        except OSError:
            pass
        L.private_dir(workspace)
        fd = os.open(schema_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(schema if schema is not None else schema_text())
        argv = [
            schema_path
            if a == MCODE_SCHEMA_SLOT
            else workspace
            if a == MCODE_CWD_SLOT
            else last_path
            if a == MCODE_LAST_SLOT
            else a
            for a in argv
        ]
        started = self.clock()
        proc: subprocess.Popen[Any] | None = None
        try:
            try:
                proc = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                out.error = "could not start mcode (%s)" % (exc.strerror or type(exc).__name__)
                return out
            if self.on_start:
                self.on_start(proc)
            try:
                stdout, stderr = proc.communicate(prompt.encode("utf-8"), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                out.error = "timed out after %d seconds" % self.timeout
                out.killed = True
                _kill_group(proc)
                stdout, stderr = proc.communicate(timeout=10)
            out.exit = proc.returncode
            out.stdout_len = len(stdout)
            out.stderr_len = len(stderr)
            err = stderr.decode("utf-8", "replace")
            out.stderr_sha = hashlib.sha256(stderr).hexdigest()
            status = None
            model = None
            envelopes = 0
            for line in stdout.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    envelope = json.loads(line)
                except ValueError:
                    continue
                if isinstance(envelope, dict) and envelope.get("type") == "exec.result":
                    envelopes += 1
                    status = (
                        envelope.get("status") if isinstance(envelope.get("status"), str) else None
                    )
                    found = envelope.get("model")
                    model = found if isinstance(found, str) and found else None
            text = None
            try:
                with open(last_path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                pass
            why, _ = parse_mcode_result(status, text, err)
            out.models = [model] if model else []
            # Acceptance is strict, and each clause closes a way a stale or foreign answer could be
            # taken for this run's:  a non-zero exit means the child failed whatever it left behind;
            # a missing or duplicated envelope means we cannot tell which turn produced the file.
            # Neither is recoverable by reading the file more carefully.
            if out.exit not in (0, None):
                out.error = "mcode exited %s" % out.exit
            elif envelopes != 1:
                out.error = "no result envelope (%d found)" % envelopes
            elif status is None:
                out.error = "result envelope named no status"
            elif why:
                out.error = "mcode %s" % why
            elif not text:
                out.error = "no parseable result"
            else:
                out.result = {"text": text}
                out.parsed = parse_muse_result(
                    text
                )  # the same JSON leniency;  the schema is the same
        finally:
            if proc is not None and self.on_exit:
                self.on_exit(proc)
            for path in (schema_path, last_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            out.secs = round(self.clock() - started, 2)
        out.cost_usd = 0.0  # subscription-backed, like the other two
        return out


def mcode_wake_fingerprint(mcode: str, model: str = MCODE_DEFAULT_MODEL) -> str | None:
    """Fingerprint the whole fallback command and the inputs it is validated against.

    Same discipline as `muse_wake_fingerprint`:  the argv is built with placeholders for the
    binary path and the model, so a flag or policy change invalidates a person-verified pin.  The
    model is carried separately because it is a per-seat value that lands on the command line, and
    a pin made under one model must not read as verified under another.

    The wake contract and the result schema are hashed too, because the fallback is validated
    against the same pair (`W.validate(result.parsed)` against `schema_text()`):  a pin that says
    nothing about them still reads as verified after either changes.  Returns None when they
    cannot be read, so the pin fails closed rather than vouching for a command whose contract
    could not be loaded.
    """
    try:
        with open(CONTRACT_PATH, "rb") as fh:
            contract = fh.read()
        # Ensure the bytes can be consumed as UTF-8 before considering them pinnable.
        contract.decode("utf-8")
        schema = schema_text()
    except (OSError, UnicodeError, ValueError):
        return None
    command = json.dumps(
        {
            "policy_version": MCODE_WAKE_POLICY_VERSION,
            "argv": mcode_argv("<mcode>", "<model>"),
            "model": model,
            "schema": schema,
            # The prompt bound is NOT in the argv, so it has to be hashed here or a pin made
            # under an unbounded prompt would still read as verified under a bounded one.
            "prompt_bound": {
                "max_messages": MCODE_FALLBACK_MAX_MESSAGES,
                "max_held": MCODE_FALLBACK_MAX_HELD,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(command)
    digest.update(b"\0")
    digest.update(contract)
    return digest.hexdigest()


def mcode_pin_state(
    paths: L.SeatPaths, mcode: str | None, model: str = MCODE_DEFAULT_MODEL
) -> dict[str, Any]:
    """Why the fallback pin does or does not match the command.  Same never-self-heal rule as the
    other two, on its own file, so a fallback is only trusted once a person verified it.

    `why` is None when pinned, else `no_mcode` (no binary to pin), `not_pinned` (nothing recorded),
    `pin_unverified` (a pin from before the fingerprint existed, so it vouches for nothing about
    this command), `pin_stale` (the binary moved, or the command or model changed under it), or
    `pin_unreadable` (the contract or schema could not be read, so nothing can be verified).
    These are reported apart because the remediation differs:  a moved binary and a changed model
    both end in re-pinning, an unreadable contract does not, and only one of them is a moved
    binary."""
    if not mcode:
        return {"pinned": False, "why": "no_mcode", "pinned_mcode": None, "mcode": None}
    current = binary_pin(mcode)
    pin = L.read_json(paths.wake_pin_mcode)
    recorded = pin.get("mcode")
    if not isinstance(recorded, str) or not recorded:
        return {"pinned": False, "why": "not_pinned", "pinned_mcode": None, "mcode": current}
    if recorded != current:
        return {"pinned": False, "why": "pin_stale", "pinned_mcode": recorded, "mcode": current}
    recorded_fingerprint = pin.get("fingerprint")
    if not isinstance(recorded_fingerprint, str) or not recorded_fingerprint:
        # The binary is the same, so this pin did verify SOMETHING;  it just predates the
        # fingerprint and says nothing about the model, the contract or the schema.
        return {
            "pinned": False,
            "why": "pin_unverified",
            "pinned_mcode": recorded,
            "mcode": current,
        }
    current_fingerprint = mcode_wake_fingerprint(mcode, model)
    if current_fingerprint is None:
        # The contract or the schema could not be read at all.  That is NOT a changed pin, and
        # the two need opposite advice:  a changed pin is fixed by re-pinning, whereas re-pinning
        # here re-reads the same unreadable file and produces the same refusal forever.  Folding
        # it into `pin_stale` points the operator at a command that is probably identical and at
        # a remediation that cannot succeed.
        return {
            "pinned": False,
            "why": "pin_unreadable",
            "pinned_mcode": recorded,
            "mcode": current,
        }
    if recorded_fingerprint != current_fingerprint:
        # A pin made before the model was named on the command, or under a different model, or
        # before the contract or schema changed, verified a command this seat no longer runs.
        return {"pinned": False, "why": "pin_stale", "pinned_mcode": recorded, "mcode": current}
    return {"pinned": True, "why": None, "pinned_mcode": recorded, "mcode": current}


def mcode_pinned(paths: L.SeatPaths, mcode: str | None, model: str = MCODE_DEFAULT_MODEL) -> bool:
    if not mcode:
        return False
    return bool(mcode_pin_state(paths, mcode, model)["pinned"])


#:  The remediation the operator needs for a fallback pin that no longer matches.  `--runtime
#:  fallback` is not decoration:  `test-wake` resolves the default "auto" to `seat_cfg.wake`, so
#:  without it a seat with wake = claude/muse and fallback = "mcode" would re-test and re-pin the
#:  PRIMARY runtime, leave this pin stale, and keep the fallback refused.
MCODE_PIN_REMEDIATION = (
    "`simple-chat daemon test-wake --seat %s --runtime fallback --run`, then "
    "`simple-chat daemon test-wake --seat %s --runtime fallback --pin`"
)

#:  A pin cannot be verified at all while the contract or the schema is unreadable, so the
#:  remediation is not a re-pin:  the same re-pin would re-read the same missing file and refuse
#:  again.  Both halves carry `--runtime fallback` for the same reason `MCODE_PIN_REMEDIATION`
#:  does (a bare `--pin` would re-pin the PRIMARY runtime and keep the fallback refused), so the
#:  string is safe to follow even when only the red line was copied.
MCODE_PIN_UNREADABLE_REMEDIATION = (
    "restore the wake contract and result schema, then run "
    "`simple-chat daemon test-wake --seat %s --runtime fallback --run` and "
    "`simple-chat daemon test-wake --seat %s --runtime fallback --pin`"
)


def mcode_pin_cause(pin: Mapping[str, Any]) -> str:
    """Why a fallback pin no longer matches, in the operator's words.

    `mcode_pin_state` returns `pin_stale` for two different causes, `pin_unverified` for a
    third, quieter one, and `pin_unreadable` when the contract or schema cannot be read at all.
    They are named apart here because they read the same at a glance but are not the same fault:
    a moved binary is a path that changed, while a changed model, contract or schema leaves the
    path alone, and an unreadable contract means the command was probably never wrong at all.
    Reporting the wrong one points the operator at a path that never moved, shows the same path
    twice, or sends them to re-pin something that cannot be re-pinned.
    """
    if pin.get("why") == "pin_unverified":
        return (
            "the pin predates the command fingerprint, so it verified nothing about the current "
            "fallback"
        )
    if pin.get("why") == "pin_unreadable":
        return (
            "the wake contract or result schema could not be read, so the current fallback "
            "cannot be verified at all (pinned %s)" % pin.get("pinned_mcode")
        )
    if pin.get("mcode") != pin.get("pinned_mcode"):
        return "mcode moved to %s (pinned %s)" % (pin.get("mcode"), pin.get("pinned_mcode"))
    return "the fallback command, model, contract or schema changed (pinned %s)" % pin.get(
        "pinned_mcode"
    )


def muse_pin_red_entry(pin: Mapping[str, Any], seat: str) -> str:
    """The one-line red entry for a seat whose muse pin no longer matches, in the operator's words.

    An unreadable contract is NOT fixed by re-pinning, so the line must not say "stale" (which
    reads as "run --pin") and must not prefix "run" before the remediation (which begins with
    "restore the wake contract").  Cause and remediation both live here so the wording is
    testable without standing up a daemon.
    """
    cause = muse_pin_cause(pin)
    remediation = (
        MUSE_PIN_UNREADABLE_REMEDIATION % seat
        if pin["why"] == "pin_unreadable"
        else MUSE_PIN_REMEDIATION % (seat, seat)
    )
    label = "unverifiable" if pin["why"] == "pin_unreadable" else "stale"
    verb = "" if pin["why"] == "pin_unreadable" else "run "
    return "wake pin is %s: %s; %s%s" % (label, cause, verb, remediation)


def mcode_pin_red_entry(pin: Mapping[str, Any], seat: str) -> str:
    """The one-line red entry for a seat whose fallback pin no longer matches, in the operator's
    words.  The shape mirrors `muse_pin_red_entry`:  unreadable contract is "unverifiable", the
    remediation starts with its own verb and is not prefixed by "run"."""
    cause = mcode_pin_cause(pin)
    remediation = (
        MCODE_PIN_UNREADABLE_REMEDIATION % (seat, seat)
        if pin["why"] == "pin_unreadable"
        else MCODE_PIN_REMEDIATION % (seat, seat)
    )
    label = "unverifiable" if pin["why"] == "pin_unreadable" else "stale"
    verb = "" if pin["why"] == "pin_unreadable" else "run "
    return "fallback pin is %s: %s; %s%s" % (label, cause, verb, remediation)


def record_mcode_pin(
    paths: L.SeatPaths, mcode: str, now: float, *, model: str | None = None
) -> None:
    L.write_json(
        paths.wake_pin_mcode,
        {
            "mcode": binary_pin(mcode),
            "at": now,
            "model": model,
            "fingerprint": mcode_wake_fingerprint(mcode, model or MCODE_DEFAULT_MODEL),
        },
    )


def record_mcode_candidate(
    paths: L.SeatPaths, mcode: str, now: float, *, model: str | None = None
) -> None:
    L.write_json(
        paths.wake_pin_candidate_mcode,
        {
            "mcode": binary_pin(mcode),
            "at": now,
            "model": model,
            "fingerprint": mcode_wake_fingerprint(mcode, model or MCODE_DEFAULT_MODEL),
        },
    )


def mcode_pin_candidate(
    paths: L.SeatPaths, mcode: str, now: float, *, model: str = MCODE_DEFAULT_MODEL
) -> str | None:
    cand = L.read_json(paths.wake_pin_candidate_mcode)
    if not cand:
        return "no passing test-wake --run to pin; run that first"
    at = cand.get("at")
    if not isinstance(at, (int, float)) or now - float(at) > CANDIDATE_SECONDS or now < float(at):
        return "the last passing test-wake --run is over a day old; run it again"
    if cand.get("mcode") != binary_pin(mcode):
        return (
            "the mcode binary changed since the test (%s, tested %s); run test-wake --run again"
            % (binary_pin(mcode), cand.get("mcode"))
        )
    current_fingerprint = mcode_wake_fingerprint(mcode, model)
    if current_fingerprint is None:
        return (
            "the wake contract or result schema could not be read, so the fallback cannot be "
            "pinned; fix that first"
        )
    if cand.get("fingerprint") != current_fingerprint:
        return (
            "the mcode command changed since the test (model, flags, contract or schema); "
            "run test-wake --run again"
        )
    record_mcode_pin(
        paths, mcode, now, model=cand.get("model") if isinstance(cand.get("model"), str) else None
    )
    try:
        os.unlink(paths.wake_pin_candidate_mcode)
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
# The hosted MCP Worker refuses a wake body over 64 KiB (src/wake.js WAKE_MAX_BODY).  A body with
# `held_back` stays far below it;  run_routine lists fewer items rather than ever cross it.
ROUTINE_MAX_BODY = 64 * 1024
# The hosted Worker's settled outcomes (docs/protocols/agent-sync-mcp.md section 3.12, item 12).
WORKER_OUTCOMES = frozenset(
    {"delivered", "no_subscriber", "channel_refused", "rejected", "gone", "not_live"}
)


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
        # reached the seat (routine_answer); None for any other answer.  The body itself is never kept.
        self.answer: dict[str, Any] | None = None


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
    no redirects (a redirect would re-send the key elsewhere), and an explicit timeout."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        retries: int = ROUTINE_RETRIES,
        backoff: Sequence[float] = ROUTINE_BACKOFF,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.sleep = sleep
        self.clock = clock
        self.retries = retries
        self.backoff = tuple(backoff)
        self.should_stop = should_stop

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
    """The fixed contract (agent-sync-wake/1) for one wake.  `row` is the trigger's seat-inbox
    row:  its owner flag is the daemon's (the owner's user id from a human Zulip app).  A DM
    trigger is marked `dm: true` and its `reply_to` is that DM's other members only, never a
    channel.  Channel, topic and sender name are Zulip data too, not instructions:  the top-level
    `channel`, `topic` and `sender_full_name` are escaped display copies, and only `reply_to`
    carries the exact names, for addressing.  `stream_id` is the trigger's Zulip stream id (an int;
    None for a DM or a row without one), so a receiver can hold the channel to its own allowlist by
    id, never by name:  the hosted MCP Worker refuses a channel wake without an allowlisted one.
    `held_back` (heldback.wake_summary) is added only when its count is above 0:  how many wakes
    the seat's budget held back, and the newest as metadata only, never a body (an additive field,
    so a body without it is unchanged)."""
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
        # `banners` overrides the instance flag for this one call.  A quota notice still needs the
        # once-per record, and must not pop a macOS banner or flip the shared flag other threads read.
        show = self.banners if banners is None else banners
        if show:
            self.runner(argv)
        return True


# --------------------------------------------------------------------------------------------
# Board filing (built, off by default:  board_per_day = 0)
# --------------------------------------------------------------------------------------------


def fleet_acronyms(repo_root: str = REPO_ROOT) -> set[str]:
    try:
        with open(os.path.join(repo_root, "fleet-apps.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            str(a["acronym"]).upper()
            for a in data.get("apps", [])
            if isinstance(a, dict) and a.get("acronym")
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"AFC"}


def board_app(topic: str, acronyms: set[str]) -> str:
    """The topic's leading app acronym when it is a fleet app, else AFC.

    `CT#2316 sentry` gives CT, and a Linear work topic `BF-123 reconnect loop` gives BF (the issue id
    names its team), so the filing keeps working after the cutover changed the topic format.
    """
    first = (topic.split() or [""])[0]
    first = re.split(r"[#-]", first, maxsplit=1)[0].upper()
    return first if first in acronyms else "AFC"


# The daemon files this board item with no agent session behind it, and `board file` needs a session id from every
# seat but the single-thread ones (AFC-361).  A label that is not a transcript id resolves to no session.
BOARD_SESSION_LABEL = "agent-sync-wake"


def board_argv(
    board: str,
    *,
    seat: str,
    title: str,
    desc: str,
    severity: str,
    app: str,
    trigger_id: int,
    url: str,
) -> list[str]:
    if severity not in ("P2", "P3"):
        raise ValueError("severity must be P2 or P3")
    if not re.fullmatch(r"[A-Z]{2,4}", app):
        raise ValueError("app must be a fleet acronym")
    return [
        board,
        "file",
        "--title=" + title,
        "--desc=" + desc,
        "--severity=" + severity,
        "--app=" + app,
        "--by",
        seat,
        "--env",
        "Mac",
        "--kind",
        "agent-report",
        "--uid",
        "zulip:%s:%d" % (seat, trigger_id),
        "--url",
        url,
        "--session",
        BOARD_SESSION_LABEL,
    ]


def run_board(
    argv: Sequence[str], env: Mapping[str, str], runner: Callable[..., Any] | None = None
) -> tuple[bool, str]:
    run = runner or subprocess.run
    try:
        done = run(
            list(argv), capture_output=True, text=True, timeout=60, env=dict(env), check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    return done.returncode == 0, "exit %d" % done.returncode
