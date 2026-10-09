"""Shared pieces for the listener tests:  a fake clock, a fake `claude` executable, config
writing, and a Daemon wired to the fake Zulip server.

The fake claude is a small Python script written into a temp bin directory with an absolute
shebang (the wake runs it with `env -i`, so it cannot read test variables).  It reads its
behaviour from a control file baked into it and can dump its argv, environment, cwd and stdin.
No test ever runs a real claude, osascript, board or launchctl:  the daemon gets the temp bin
directory as its wake PATH, a notifier whose runner only records argv, and an injected
start-time function.
"""
from __future__ import annotations

import base64
import io
import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

from agent_sync import adapters as A
from agent_sync import live as L
from agent_sync.daemon import Daemon
from agent_sync.tests.fake_zulip import EPOCH
from agent_sync.tests.harness import Harness

BASE_CONFIG = {
    "owner": 12,
    "eligible": [10, 11, 13, 14, 15],
}

FAKE_CLAUDE = r'''#!{python}
import json, os, sys, time
CONTROL = {control!r}
DUMP = {dump!r}
with open(CONTROL) as fh:
    control = json.load(fh)
argv = sys.argv[1:]
if argv[:2] == ["agents", "--json"]:
    print(json.dumps(control.get("agents", [])))
    sys.exit(0)
stdin = sys.stdin.read()
with open(DUMP, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": stdin}}) + "\n")
mode = control.get("mode", "ok")
# --json-schema adds the harness's own StructuredOutput tool, so a real init lists it.
init = {{"type": "system", "subtype": "init", "tools": control.get("tools", ["StructuredOutput"]), "mcp_servers": [],
        "model": "claude-sonnet"}}
if mode == "tool":
    init["tools"] = ["StructuredOutput", "Bash"]
if mode == "mcp":
    init["mcp_servers"] = [{{"name": "gmail", "status": "connected"}}]
if mode != "noinit":
    print(json.dumps(init), flush=True)
if mode == "hook":
    print(json.dumps({{"type": "system", "subtype": "hook_response", "hook_name": "Stop"}}), flush=True)
if mode in ("tool", "mcp", "hang", "hook"):
    time.sleep(control.get("hang", 30))
result = {{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": control.get("cost", 0.03),
          "modelUsage": {{"claude-sonnet": {{"inputTokens": 1}}}}, "num_turns": 1,
          "structured_output": control.get("output")}}
if mode == "two_models":
    result["modelUsage"]["claude-haiku"] = {{"inputTokens": 1}}
if mode == "error":
    result.update({{"is_error": True, "subtype": "error_max_budget_usd", "structured_output": None}})
if mode == "text":
    result["structured_output"] = None
    result["result"] = json.dumps(control.get("output"))
print(json.dumps(result), flush=True)
sys.stderr.write("some stderr noise\n")
'''


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now
        self.mono = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(max(0.0, seconds))

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.mono += seconds

    def jump_wall(self, seconds: float) -> None:
        """Wall time moves, monotonic does not (a sleeping laptop)."""
        self.now += seconds


def reply_output(text: str = "On it.  The cutover is in review.", **extra: Any) -> dict[str, Any]:
    out = {"action": "reply", "reply": text, "board": None, "owner_note": None, "risk": None}
    out.update(extra)
    return out


class ListenerHarness(Harness):
    heartbeat = 0.2

    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock(EPOCH + 1000)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.control = self.tmp / "claude-control.json"
        self.dump = self.tmp / "claude-dump.jsonl"
        self.set_claude(mode="ok", output=reply_output())
        self.claude = self.bin / "claude"
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable, control=str(self.control), dump=str(self.dump)))
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        self.banners: list[list[str]] = []
        self.starts: dict[int, int] = {}
        self.root = str(self.state_dir)
        self.state_dir.mkdir(exist_ok=True)
        self.extra_keys: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        # Every byte written under the state dir, and everything the fake claude saw, is scanned too.
        for path in self.state_dir.rglob("*"):
            if path.is_file():
                try:
                    self.transcript.append(path.read_text(errors="replace"))
                except OSError:
                    pass
        if self.dump.exists():
            self.transcript.append(self.dump.read_text())
        everything = "\n".join(self.transcript)
        for email, key in self.extra_keys:
            token = base64.b64encode(("%s:%s" % (email, key)).encode()).decode()
            self.assertNotIn(key, everything, "a second bot key reached the output or the state files")
            self.assertNotIn(token, everything, "a second bot's base64 auth token reached the output or the state files")
        super().tearDown()

    # ---- helpers ------------------------------------------------------------------------------
    def set_claude(self, **control: Any) -> None:
        """Control the fake claude.  `claude agents --json` lists this test process as an
        interactive session unless `agents` is given."""
        control.setdefault("agents", [{"pid": os.getpid(), "kind": "interactive"}])
        self.control.write_text(json.dumps(control))

    def dumps(self) -> list[dict[str, Any]]:
        if not self.dump.exists():
            return []
        return [json.loads(line) for line in self.dump.read_text().splitlines() if line.strip()]

    def write_config(self, *, seats: str | None = None, owner: int = 12, eligible: list[int] | None = None,
                     extra_daemon: str = "", rewake: bool = False, wake: str = "claude", budget: str = "",
                     live: str = "", coalesce: float = 20, owner_coalesce: float = 5) -> None:
        eligible = BASE_CONFIG["eligible"] if eligible is None else eligible
        text = textwrap.dedent("""\
            [daemon]
            owner_user_id = {owner}
            eligible_user_ids = {eligible}
            stale_after_minutes = 120
            coalesce_seconds = {coalesce}
            coalesce_max_seconds = 90
            owner_coalesce_seconds = {owner_coalesce}
            {extra}

            [platform.claude-code]
            seat = "CLAUDE"
            rewake_verified = {rewake}

            """).format(owner=owner, eligible=json.dumps(eligible), extra=extra_daemon, coalesce=coalesce,
                        owner_coalesce=owner_coalesce,
                        rewake="true" if rewake else "false")
        if seats is None:
            seats = textwrap.dedent("""\
                [seat.CLAUDE]
                bot = "Claude"
                wake = "{wake}"
                model = "sonnet"
                {budget}
                {live}
                """).format(wake=wake, budget=("budget = { %s }" % budget) if budget else "",
                            live=("live = { %s }" % live) if live else "")
        (self.state_dir / "listener.toml").write_text(text + seats)

    def daemon(self, **kw: Any) -> Daemon:
        notifier = A.Notifier(self.root, clock=self.clock.time, runner=self.banners.append)
        options: dict[str, Any] = dict(env=self.env(), root=self.root, home=str(self.home), clock=self.clock,
                                       notifier=notifier, wake_path=str(self.bin),
                                       start_time=lambda pid: self.starts.get(pid, 0), http_timeout=5.0,
                                       events_timeout=1.0, stderr=io.StringIO())
        options.update(kw)
        daemon = Daemon(**options)
        self.addCleanup(daemon.shutdown)
        return daemon

    def pin(self, seat: str = "CLAUDE", *, agents_ok: bool = True) -> None:
        A.record_pin(L.SeatPaths(self.root, seat), str(self.claude), self.clock.time(), agents_ok=agents_ok,
                     init_tools=["StructuredOutput"])

    def seat_paths(self, seat: str = "CLAUDE") -> L.SeatPaths:
        return L.SeatPaths(self.root, seat)

    def inbox(self, seat: str = "CLAUDE") -> list[dict[str, Any]]:
        return L.read_jsonl(self.seat_paths(seat).seat_inbox)

    def ledger(self, seat: str = "CLAUDE") -> list[dict[str, Any]]:
        return L.read_jsonl(self.seat_paths(seat).wakes)

    def write_lease(self, lease_id: str, *, seat: str = "CLAUDE", platform: str = "claude-code", pid: int | None = None,
                    topics: list[tuple[str, str]] = (), mentions: bool = True, posted: list[int] = (),
                    session_id: str | None = None, **extra: Any) -> None:
        paths = self.seat_paths(seat)
        lease = {"seat": seat, "lease_id": lease_id, "platform": platform, "pid": pid or os.getpid(), "pid_start": 0,
                 "topics": [{"channel": c, "topic": t, "expires": None} for c, t in topics], "mentions": mentions,
                 "wake_capable": platform == "claude-code", "posted": list(posted), "heartbeat": self.clock.time(),
                 "created": self.clock.time(), "session_id": session_id}
        lease.update(extra)
        L.write_json(paths.lease(lease_id), lease)
        L.private_dir(paths.live_dir(lease_id))

    def live_items(self, lease_id: str, seat: str = "CLAUDE") -> list[dict[str, Any]]:
        return L.read_jsonl(self.seat_paths(seat).live_inbox(lease_id))

    def pump_until(self, daemon: Daemon, predicate: Any, seat: str = "CLAUDE", rounds: int = 20) -> None:
        for _ in range(rounds):
            if predicate():
                return
            daemon.pump(seat, timeout=1.0)
        self.assertTrue(predicate(), "the condition never held")


def fake_watcher(paths: L.SeatPaths, lease_id: str) -> int:
    """Hold the lease's rewake lock like a live watcher would; returns the fd to close."""
    import fcntl

    L.private_dir(paths.live_dir(lease_id))
    fd = os.open(paths.rewake_lock(lease_id), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def mode_of(path: Path | str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
