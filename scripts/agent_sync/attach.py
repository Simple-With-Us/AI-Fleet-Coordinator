"""`agent-sync attach` and `agent-sync detach`:  how a session tells the listener daemon what it
owns, and how Zulip items reach that session.

    attach --hook EVENT          Claude Code hooks (session-start, prompt, stop, session-end)
    attach --rewake              the asyncRewake watcher: exit 2 with a batch to wake an idle session
    attach --drain [--replay N]  print undelivered items (or reprint the last N delivered)
    attach --wait [--timeout S]  block for one batch, then exit (any platform)
    attach [--topic T ...]       write or update the lease
    detach [--topic T | --all]   remove topics or the lease

The entry script dispatches here before `cli` is imported, so the hook path costs only a light
import (json, os, time, fcntl, tomllib, re, unicodedata).  argparse is imported only for the
forms that are not hooks, and subprocess only when a lease is first written (to read the
process start time with `ps`).

Zulip content is data.  Bodies reach model context only between nonce markers, through `claim`
(at most once per item); headlines carry no bodies.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Callable, Mapping, TextIO

from . import live as L

ENTRYPOINTS = ("cli", "claude-desktop")
HOOK_EVENTS = ("session-start", "prompt", "stop", "session-end")
HOOK_NAMES = {"session-start": "SessionStart", "prompt": "UserPromptSubmit", "stop": "Stop", "session-end": "SessionEnd"}
REWAKE_SECONDS = 86000  # just under the hook's 86400-second timeout
LEASE_APPEAR_SECONDS = 10.0
STDIN_LIMIT = 1 << 20
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "env", "timeout", "agent-sync", "python3", "python", "login", "sudo"}
# SessionEnd reasons after which the same Claude process keeps running (Claude Code 2.1.290 sends
# clear, resume, logout, prompt_input_exit and other):  the lease is keyed by the process, so it stays.
KEEP_LEASE_REASONS = ("clear", "resume")
END_MARKERS = ("ended.json", "returned.json")
_LEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")


class UsageProblem(Exception):
    """A bad --lease, --platform or --as value:  exit 2 with the message."""


def valid_lease_id(value: str) -> bool:
    return bool(_LEASE_ID_RE.fullmatch(value)) and ".." not in value


def clear_end_markers(paths: L.SeatPaths, lease_id: str) -> None:
    """A lease written again under an old id (/clear, detach --all then attach) must not inherit
    the old directory's ended.json or returned.json, or the daemon would treat it as dead.  Call
    with the lease lock held (inside update_lease), so a concurrent reap cannot interleave."""
    for name in END_MARKERS:
        try:
            os.unlink(os.path.join(paths.live_dir(lease_id), name))
        except OSError:
            pass


class Ctx:
    def __init__(self, env: Mapping[str, str], stdin: TextIO, stdout: TextIO, stderr: TextIO,
                 now: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.env = env
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.now = now
        self.sleep = sleep
        self.root = L.state_root(env)
        self.config, self.config_error = L.load_config(self.root)

    def out(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def err(self, text: str) -> None:
        self.stderr.write(text)
        self.stderr.flush()


# --------------------------------------------------------------------------------------------
# Process identity
# --------------------------------------------------------------------------------------------

def process_start(pid: int) -> int:
    """Start time of `pid` in epoch seconds from `ps -o lstart=`, or 0 when it cannot be read.
    A lease id carries it, so a reused pid never inherits an old lease."""
    import subprocess  # only when a lease is first written
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                              timeout=3, env={"LC_ALL": "C", "LANG": "C", "PATH": "/bin:/usr/bin"})
        text = " ".join(done.stdout.split())
        if done.returncode != 0 or not text:
            return 0
        return int(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def agent_root_pid(start: int | None = None) -> int:
    """The nearest ancestor that is not a shell or a wrapper:  the agent process itself."""
    import subprocess
    pid = start if start is not None else os.getppid()
    for _ in range(12):
        try:
            done = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=3, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
            parts = done.stdout.strip().split(None, 1)
        except (OSError, subprocess.SubprocessError):
            return pid
        if len(parts) < 2:
            return pid
        name = os.path.basename(parts[1].strip()).lstrip("-")
        if name not in SHELLS and not name.startswith("python"):
            return pid
        parent = int(parts[0]) if parts[0].isdigit() else 1
        if parent <= 1:
            return pid
        pid = parent
    return pid


def claude_pid(env: Mapping[str, str]) -> int:
    value = (env.get("CLAUDE_PID") or "").strip()
    return int(value) if value.isdigit() else os.getppid()


def in_claude_scope(env: Mapping[str, str]) -> bool:
    """Hooks attach only for interactive surfaces.  A `claude -p`, background or SDK run (any
    other CLAUDE_CODE_ENTRYPOINT) never writes a lease unless AGENT_SYNC_ATTACH=1."""
    return env.get("CLAUDE_CODE_ENTRYPOINT") in ENTRYPOINTS or env.get("AGENT_SYNC_ATTACH") == "1"


def read_hook_input(stream: TextIO) -> dict[str, Any]:
    try:
        raw = stream.read(STDIN_LIMIT)
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------------------------

def ensure_claude_lease(ctx: Ctx, paths: L.SeatPaths, pid: int, hook_input: Mapping[str, Any], *,
                        check_start: bool = False) -> str:
    """Find this process's lease or write a new one.  With `check_start` (a session that just
    started), a lease for the same pid with another start time belongs to a dead process that had
    the pid before; it is ended, not reused.  Otherwise an existing lease is reused without `ps`."""
    existing = L.find_lease(paths, "claude-code-%d-" % pid)
    if existing is not None and not check_start:
        if hook_input.get("session_id") or hook_input.get("cwd"):
            def refresh(lease: dict[str, Any]) -> None:
                if hook_input.get("session_id"):
                    lease["session_id"] = str(hook_input["session_id"])
                if hook_input.get("cwd"):
                    lease["cwd"] = str(hook_input["cwd"])
            L.update_lease(paths, existing, refresh)
        return existing
    start = process_start(pid)
    lease_id = "claude-code-%d-%d" % (pid, start)
    if existing is not None and existing != lease_id:
        end_lease(ctx, paths, existing)
    now = ctx.now()

    def mutate(lease: dict[str, Any]) -> None:
        clear_end_markers(paths, lease_id)  # this hook runs inside the live process, so the lease is not ended
        lease.setdefault("created", now)
        lease.setdefault("topics", [])
        lease.setdefault("posted", [])
        lease.setdefault("wakes_seen", len(L.read_jsonl(paths.wakes)))
        lease.update({"seat": paths.seat, "lease_id": lease_id, "platform": "claude-code", "pid": pid,
                      "pid_start": start, "mentions": True, "wake_capable": True, "heartbeat": now,
                      "entrypoint": ctx.env.get("CLAUDE_CODE_ENTRYPOINT")})
        if hook_input.get("session_id"):
            lease["session_id"] = str(hook_input["session_id"])
        if hook_input.get("cwd"):
            lease["cwd"] = str(hook_input["cwd"])
        lease.setdefault("permission_mode", None)

    L.update_lease(paths, lease_id, mutate)
    L.private_dir(paths.live_dir(lease_id))
    return lease_id


def end_lease(ctx: Ctx, paths: L.SeatPaths, lease_id: str) -> None:
    """Remove the lease.  The daemon returns undelivered items to the seat inbox when it reaps it."""
    if os.path.isdir(paths.live_dir(lease_id)):
        L.write_json(os.path.join(paths.live_dir(lease_id), "ended.json"), {"ts": ctx.now()})
    try:
        os.unlink(paths.lease(lease_id))
    except FileNotFoundError:
        pass


def generic_lease_id(ctx: Ctx, platform: str, explicit: str | None) -> tuple[str, int, int]:
    """(lease id, pid, pid start) for a non-Claude session."""
    pid = agent_root_pid()
    start = process_start(pid)
    if explicit:
        return explicit, pid, start
    session = (ctx.env.get("AGENT_SESSION") or "").strip()
    if session:
        import hashlib
        return "%s-s%s" % (platform, hashlib.sha256(session.encode()).hexdigest()[:12]), pid, start
    return "%s-%d-%d" % (platform, pid, start), pid, start


# --------------------------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------------------------

def _context(ctx: Ctx, event: str, text: str) -> None:
    if text:
        ctx.out(json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_NAMES[event], "additionalContext": text}}) + "\n")


def _counts(paths: L.SeatPaths, lease: Mapping[str, Any]) -> dict[str, int]:
    cursor = L.read_json(paths.local_cursor).get("seq")
    cursor = cursor if isinstance(cursor, int) else 0
    inbox = sum(1 for i in L.read_jsonl(paths.seat_inbox)
                if isinstance(i.get("seq"), int) and i["seq"] > cursor and not i.get("delivered_to"))
    surfaced = L.read_json(paths.owner_queue_meta).get("surfaced")
    surfaced = surfaced if isinstance(surfaced, int) else 0
    owner = sum(1 for i in L.read_jsonl(paths.owner_queue) if isinstance(i.get("seq"), int) and i["seq"] > surfaced)
    ledger = L.read_jsonl(paths.wakes)
    seen = lease.get("wakes_seen") if isinstance(lease.get("wakes_seen"), int) else len(ledger)
    outcomes = sum(1 for row in ledger[seen:] if row.get("state") in ("done", "failed"))
    return {"inbox": inbox, "owner_queue": owner, "wake_outcomes": outcomes, "ledger_lines": len(ledger)}


def hook(ctx: Ctx, event: str) -> int:
    if event not in HOOK_EVENTS:
        ctx.err("agent-sync attach: unknown hook event %r\n" % event)
        return 0  # a hook never blocks the session over its own arguments
    if not in_claude_scope(ctx.env):
        return 0
    seat = L.claude_seat(ctx.env, ctx.config)
    if seat is None:
        L.hook_log(ctx.root, "no-seat", hook=event)
        return 0
    hook_input = read_hook_input(ctx.stdin)
    paths = L.SeatPaths(ctx.root, seat)
    pid = claude_pid(ctx.env)
    if event == "session-end":
        existing = L.find_lease(paths, "claude-code-%d-" % pid)
        reason = str(hook_input.get("reason") or "")
        if existing and reason in KEEP_LEASE_REASONS:
            # /clear and /resume end a conversation, not the process:  the SessionStart that
            # follows rewrites session_id, and the lease keeps its topics and its inbox.
            L.hook_log(ctx.root, "session-end-kept", lease=existing, reason=reason)
        elif existing:
            end_lease(ctx, paths, existing)
        return 0
    lease_id = ensure_claude_lease(ctx, paths, pid, hook_input if event == "session-start" else {},
                                   check_start=event == "session-start" and hook_input.get("source") in (None, "", "startup"))
    now = ctx.now()
    if event == "session-start":
        lease = L.load_lease(paths, lease_id)
        counts = _counts(paths, lease)
        shown = {k: counts[k] for k in ("inbox", "owner_queue", "wake_outcomes")}
        quiet = hook_input.get("source") in ("resume", "compact", "clear") and lease.get("last_counts") == shown

        def mutate(data: dict[str, Any]) -> None:
            data["last_counts"] = shown
            data["wakes_seen"] = counts["ledger_lines"]

        L.update_lease(paths, lease_id, mutate)
        if any(shown.values()) and not quiet:
            parts = []
            if shown["inbox"]:
                parts.append("%d in the %s seat inbox (agent-sync inbox --local)" % (shown["inbox"], seat))
            if shown["owner_queue"]:
                parts.append("%d owner-queue item%s" % (shown["owner_queue"], "" if shown["owner_queue"] == 1 else "s"))
            if shown["wake_outcomes"]:
                parts.append("%d wake outcome%s (agent-sync wakes)" % (shown["wake_outcomes"],
                                                                    "" if shown["wake_outcomes"] == 1 else "s"))
            _context(ctx, event, "[agent-sync] " + "; ".join(parts) + ".")
        return 0
    if event == "stop":
        def touch(data: dict[str, Any]) -> None:
            data["heartbeat"] = now
            if hook_input.get("permission_mode"):
                data["permission_mode"] = str(hook_input["permission_mode"])
            tasks = hook_input.get("background_tasks")
            if isinstance(tasks, list):
                data["watcher_listed"] = any("attach --rewake" in json.dumps(t) for t in tasks)

        L.update_lease(paths, lease_id, touch)
        return 0
    # prompt
    prompt = str(hook_input.get("prompt") or "")
    from_rewake = L.REWAKE_HEADER in prompt[:4000]

    def on_prompt(data: dict[str, Any]) -> None:
        data["heartbeat"] = now
        if hook_input.get("session_id"):
            data["session_id"] = str(hook_input["session_id"])
        if hook_input.get("permission_mode"):
            data["permission_mode"] = str(hook_input["permission_mode"])
        if not from_rewake:
            data["last_prompt"] = now

    L.update_lease(paths, lease_id, on_prompt)
    parts: list[str] = []
    if L.rewake_verified(ctx.config) and not L.paused(ctx.root, seat):
        text, chosen = L.claim(paths, lease_id, max_chars=L.REWAKE_MAX_CHARS, per_message=L.REWAKE_PER_MESSAGE,
                               now=now, stale_seconds=L.stale_after(ctx.config),
                               select=lambda i: i.get("class") == "interrupt")
        if chosen:
            parts.append(text)
    heads = L.take_headlines(paths, lease_id)
    if heads:
        parts.append("[agent-sync] waiting in this session's Zulip inbox (bodies: agent-sync attach --drain):\n" + heads)
    _context(ctx, "prompt", "\n".join(parts))
    return 0


# --------------------------------------------------------------------------------------------
# Rewake watcher
# --------------------------------------------------------------------------------------------

def rewake(ctx: Ctx) -> int:
    """One per lease.  Blocks on the lease inbox; when interrupt items are pending and the live
    budget has room, claims them and exits 2 with the batch on stderr, which wakes the session.
    Exits 0 at once when another watcher holds the lease, when the session is not in scope, or
    when rewake has not been verified (manual test 1) on this machine."""
    import fcntl

    if not in_claude_scope(ctx.env):
        return 0
    seat = L.claude_seat(ctx.env, ctx.config)
    if seat is None or not L.rewake_verified(ctx.config):
        return 0
    paths = L.SeatPaths(ctx.root, seat)
    pid = claude_pid(ctx.env)
    poll = float(ctx.env.get("AGENT_SYNC_REWAKE_POLL") or 1.0)
    coalesce = float(ctx.env.get("AGENT_SYNC_REWAKE_COALESCE") or 3.0)
    started = ctx.now()
    lease_id = L.find_lease(paths, "claude-code-%d-" % pid)
    while lease_id is None and ctx.now() - started < LEASE_APPEAR_SECONDS:
        ctx.sleep(0.2)  # the session-start hook writes the lease in parallel
        lease_id = L.find_lease(paths, "claude-code-%d-" % pid)
    if lease_id is None:
        return 0
    L.private_dir(paths.live_dir(lease_id))
    fd = os.open(paths.rewake_lock(lease_id), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # a watcher is already armed for this lease
        L.write_json(os.path.join(paths.live_dir(lease_id), "rewake.json"), {"pid": os.getpid(), "started": started})
        limits = L.live_limits(ctx.config, seat)
        stale = L.stale_after(ctx.config)
        while ctx.now() - started < REWAKE_SECONDS:
            if not os.path.exists(paths.lease(lease_id)) or not L.pid_alive(pid):
                return 0
            now = ctx.now()
            pending = [i for i in L.pending_items(paths, lease_id) if i.get("class") == "interrupt"
                       and not (isinstance(i.get("ts"), (int, float)) and now - float(i["ts"]) > stale)]
            if pending and not L.paused(ctx.root, seat):
                ctx.sleep(coalesce)
                now = ctx.now()
                budget = L.read_json(paths.budget(lease_id))
                chosen_keys: dict[str, bool] = {}

                def fits(item: Mapping[str, Any]) -> bool:
                    if item.get("class") != "interrupt":
                        return False
                    key = L.topic_key(str(item.get("channel") or ""), str(item.get("topic") or ""))
                    if key in chosen_keys:
                        return True
                    if L.live_room(budget, key, bool(item.get("owner")), now, limits) is None:
                        chosen_keys[key] = bool(item.get("owner"))
                        return True
                    return False

                text, chosen = L.claim(paths, lease_id, max_chars=L.REWAKE_MAX_CHARS, per_message=L.REWAKE_PER_MESSAGE,
                                       now=now, stale_seconds=stale, select=fits, header=L.REWAKE_HEADER)
                if chosen:
                    owners = {L.topic_key(str(i.get("channel") or ""), str(i.get("topic") or ""))
                              for i in chosen if i.get("owner")}
                    keys = {L.topic_key(str(i.get("channel") or ""), str(i.get("topic") or "")) for i in chosen}
                    if owners:
                        L.record_turn(paths, lease_id, owners, True, now)
                    if keys - owners:
                        L.record_turn(paths, lease_id, keys - owners, False, now)
                    L.hook_log(ctx.root, "rewake-fired", seat=seat, lease=lease_id, items=len(chosen))
                    ctx.err(text + "\n")
                    return 2
            ctx.sleep(poll)
        return 0
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------------
# Drain, wait, attach, detach
# --------------------------------------------------------------------------------------------

def _parser(prog: str):
    import argparse

    parser = argparse.ArgumentParser(prog=prog, description=(
        "Lease topics for this session and receive the listener daemon's items for it.  Claude Code "
        "sessions get this through the agent-sync plugin's hooks; other platforms use --wait and --drain."))
    return parser


def _resolve_lease(ctx: Ctx, args: Any, *, create: bool) -> tuple[L.SeatPaths, str] | None:
    """Every value that becomes part of a path is checked first:  a seat name, a platform name and
    a lease id are plain tokens, never a path (`--lease ../../x` is refused)."""
    seat_raw = getattr(args, "as_seat", None) or ctx.env.get("AGENT_SEAT") or ""
    if not seat_raw and ctx.env.get("CLAUDE_PID"):
        seat_raw = L.claude_seat(ctx.env, ctx.config) or ""
    seat = seat_raw.strip().upper()
    if not seat:
        ctx.err("agent-sync attach: no seat; set AGENT_SEAT or pass --as NAME\n")
        return None
    if not _SEAT_RE.fullmatch(seat):
        raise UsageProblem("--as / AGENT_SEAT must be a seat name such as CLAUDE")
    paths = L.SeatPaths(ctx.root, seat)
    explicit = getattr(args, "lease", None) or (ctx.env.get("AGENT_LEASE") or "").strip() or None
    platform = getattr(args, "platform", None) or ctx.env.get("AGENT_PLATFORM") or None
    if explicit is not None and not valid_lease_id(explicit):
        raise UsageProblem("--lease / AGENT_LEASE must be letters, digits, dot, dash or underscore (at most 120), "
                           "never a path")
    if platform is not None and not _PLATFORM_RE.fullmatch(platform):
        raise UsageProblem("--platform / AGENT_PLATFORM must be lower-case letters, digits and dashes (at most 32)")
    if explicit and os.path.exists(paths.lease(explicit)):
        return paths, explicit
    if not explicit and (platform in (None, "claude-code")) and ctx.env.get("CLAUDE_PID") and in_claude_scope(ctx.env):
        lease_id = L.find_lease(paths, "claude-code-%d-" % claude_pid(ctx.env))
        if lease_id is None and create:
            lease_id = ensure_claude_lease(ctx, paths, claude_pid(ctx.env), {}, check_start=True)
        return (paths, lease_id) if lease_id else None
    platform = platform or "cli"
    lease_id, pid, start = generic_lease_id(ctx, platform, explicit)
    if os.path.exists(paths.lease(lease_id)):
        return paths, lease_id
    if not create:
        return None
    now = ctx.now()

    def mutate(lease: dict[str, Any]) -> None:
        if not lease:
            clear_end_markers(paths, lease_id)
        lease.update({"seat": seat, "lease_id": lease_id, "platform": platform, "pid": pid, "pid_start": start,
                      "created": now, "heartbeat": now, "topics": [], "posted": [], "mentions": False,
                      "wake_capable": False, "cwd": os.getcwd()})

    L.update_lease(paths, lease_id, mutate)
    L.private_dir(paths.live_dir(lease_id))
    return paths, lease_id


def cmd_attach(ctx: Ctx, argv: list[str]) -> int:
    parser = _parser("agent-sync attach")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hook", metavar="EVENT", choices=HOOK_EVENTS, help="run a Claude Code hook (%s)" % ", ".join(HOOK_EVENTS))
    mode.add_argument("--rewake", action="store_true", help="the asyncRewake watcher (exit 2 wakes an idle session)")
    mode.add_argument("--drain", action="store_true", help="print this session's undelivered items, then mark them delivered")
    mode.add_argument("--wait", action="store_true", help="block until one batch arrives, print it, exit 0 (exit 4 on timeout)")
    parser.add_argument("--replay", type=int, metavar="N", help="with --drain: reprint the last N delivered items")
    parser.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS", help="with --wait (default: %(default)s)")
    parser.add_argument("--topic", action="append", default=[], metavar="T", help="lease this topic (repeatable)")
    parser.add_argument("--channel", default="agent-sync", metavar="C", help="channel of --topic (default: %(default)s)")
    parser.add_argument("--mentions", action="store_true", help="also take @-mentions in unleased topics (interactive Claude only by default)")
    parser.add_argument("--platform", metavar="P", help="platform name for the lease (default: claude-code under Claude, else env AGENT_PLATFORM or cli)")
    parser.add_argument("--lease", metavar="ID", help="an existing lease id (default: env AGENT_LEASE, else derived)")
    parser.add_argument("--as", dest="as_seat", metavar="NAME", help="seat (default: env AGENT_SEAT)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.hook:
        return hook(ctx, args.hook)
    if args.rewake:
        return rewake(ctx)
    try:
        found = _resolve_lease(ctx, args, create=not args.drain)
    except UsageProblem as exc:
        ctx.err("agent-sync attach: %s\n" % exc)
        return 2
    if found is None:
        if args.drain:
            ctx.err("agent-sync attach: this session has no lease, so nothing is waiting for it\n")
            return 0
        return 2
    paths, lease_id = found
    now = ctx.now()
    if args.topic or args.mentions:
        def mutate(lease: dict[str, Any]) -> None:
            for topic in args.topic:
                L.add_topic(lease, args.channel.lstrip("#"), topic.strip(), None)
            if args.mentions:
                lease["mentions"] = True
            lease["heartbeat"] = now

        L.update_lease(paths, lease_id, mutate)
    stale = L.stale_after(ctx.config)
    if args.drain:
        if args.replay:
            text = L.replay(paths, lease_id, args.replay)
            ctx.out((text or "[agent-sync replay] nothing delivered yet") + "\n")
            return 0
        text, chosen = L.claim(paths, lease_id, max_chars=L.DRAIN_MAX_CHARS, per_message=L.DRAIN_PER_MESSAGE,
                               now=now, stale_seconds=stale, header="[agent-sync drain]")
        ctx.out((text or "[agent-sync drain] nothing pending") + "\n")
        return 0
    if args.wait:
        deadline = now + args.timeout
        beat = now
        while True:
            now = ctx.now()
            if L.pending_items(paths, lease_id):
                text, chosen = L.claim(paths, lease_id, max_chars=L.DRAIN_MAX_CHARS, per_message=L.DRAIN_PER_MESSAGE,
                                       now=now, stale_seconds=stale, header="[agent-sync wait]")
                L.update_lease(paths, lease_id, lambda d: d.update({"heartbeat": now, "wait_until": now + L.WAIT_LEASE_GRACE}))
                if text:
                    ctx.out(text + "\n")
                return 0
            if now >= deadline:
                L.update_lease(paths, lease_id, lambda d: d.update({"heartbeat": now, "wait_until": now + L.WAIT_LEASE_GRACE}))
                ctx.err("agent-sync attach: nothing arrived within %g seconds\n" % args.timeout)
                return 4
            if now - beat >= 60:
                beat = now
                L.update_lease(paths, lease_id, lambda d: d.update({"heartbeat": now, "wait_until": now + 120}))
            ctx.sleep(min(1.0, max(0.05, deadline - now)))
    ctx.out("lease %s\n" % lease_id)
    return 0


def cmd_detach(ctx: Ctx, argv: list[str]) -> int:
    parser = _parser("agent-sync detach")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--topic", action="append", metavar="T", help="stop leasing this topic (repeatable)")
    group.add_argument("--all", action="store_true", help="remove the lease; undelivered items go back to the seat inbox")
    parser.add_argument("--channel", default="agent-sync", metavar="C")
    parser.add_argument("--platform", metavar="P")
    parser.add_argument("--lease", metavar="ID")
    parser.add_argument("--as", dest="as_seat", metavar="NAME")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        found = _resolve_lease(ctx, args, create=False)
    except UsageProblem as exc:
        ctx.err("agent-sync detach: %s\n" % exc)
        return 2
    if found is None:
        ctx.err("agent-sync detach: this session has no lease\n")
        return 0
    paths, lease_id = found
    if args.all:
        end_lease(ctx, paths, lease_id)
        ctx.out("removed lease %s\n" % lease_id)
        return 0
    removed: list[str] = []

    def mutate(lease: dict[str, Any]) -> None:
        for topic in args.topic:
            if L.remove_topic(lease, args.channel.lstrip("#"), topic.strip()):
                removed.append(topic)

    L.update_lease(paths, lease_id, mutate)
    ctx.out("".join("detached #%s > %s\n" % (L.escape_line(args.channel), L.escape_line(t)) for t in removed)
            or "no such topic on lease %s\n" % lease_id)
    return 0


def main(argv: list[str] | None = None, *, command: str = "attach", env: Mapping[str, str] | None = None,
         stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None,
         now: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> int:
    ctx = Ctx(os.environ if env is None else env, stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr,
              now or time.time, sleep or time.sleep)
    argv = list(sys.argv[2:] if argv is None else argv)
    try:
        if command == "detach":
            return cmd_detach(ctx, argv)
        if len(argv) >= 2 and argv[0] == "--hook":  # the fast path: no argparse
            return hook(ctx, argv[1])
        if argv == ["--rewake"]:
            return rewake(ctx)
        return cmd_attach(ctx, argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 - a hook must never take the session down
        L.hook_log(ctx.root, "error", command=command, error=type(exc).__name__)
        ctx.err("agent-sync %s: internal error: %s\n" % (command, type(exc).__name__))
        return 1 if command != "attach" or not argv or argv[0] not in ("--hook", "--rewake") else 0
