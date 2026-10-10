"""The listener daemon:  one Zulip event queue per configured seat, routing at zero tokens, live
leases, seat inboxes, the wake ledger, budgets, the Claude headless wake and notify-owner.

Threads in production (`Daemon.run`):  one poller per seat bot, the router on the main thread
(it also runs `tick`), and one wake worker per seat whose adapter is `claude` or `http`.  Tests
drive the same code step by step:  `connect_seat`, `pump`, `tick` and `run_jobs`, with a fake clock.

Two instances run this code (owner decision, Thu, Oct 8):  `mac` holds the CLAUDE seat (live
delivery and the tool-less claude wake), `server` holds the Grok Bot personas (environment
credentials and the remote routine wake).  A daemon whose config holds a seat that the seat
partition does not give to its instance (another instance's seat, a `none` seat, or one the
partition does not list) refuses to start (config.partition_errors).

The daemon never spends a model token while idle.  A token is spent only on a headless wake,
which starts only for a seat-inbox item the deterministic prefilter marks wake-worthy, inside the
seat's budgets, with a pinned claude binary, when the kill switch is off.

Message content is data.  It is stored in inbox files and handed to a model only between nonce
markers.  It is never executed and never logged:  listener.log holds ids, seats, topics,
senders, decisions and costs, and adapter output only as length and SHA-256.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import __version__
from . import adapters as A
from . import config as C
from . import heartbeat as HB
from . import live as L
from . import router as R
from . import secretscan
from . import wakes as W
from . import zulip as Z
from .cli import format_time, seat_tag_for

EVENT_TYPES = ["message", "update_message", "user_topic", "realm_user"]
FLEET_ROLES = (300, 400)  # moderator and member (owner decision 2026-10-07); owner 100 and admin 200 refused
BACKOFF = (5.0, 30.0, 60.0, 120.0)
DEAD_QUEUE_SECONDS = 180.0
DOWN_NOTIFY_SECONDS = 30 * 60.0
CLOCK_JUMP_SECONDS = 120.0
TOPIC_BACKFILL_CAP = 200
PAGE = 100
MAX_PAGES = 20
RING_LIMIT = 5000
CURSOR_FLUSH_SECONDS = 1.0
GRACE_SECONDS = 600.0
LIVE_DIR_KEEP = 86400.0
AGENTS_CACHE_SECONDS = 30.0
LEASE_HEARTBEAT_LIMIT = 15 * 60.0
POST_SPACING = 3.0
SEAT_INBOX_ROTATE_BYTES = 1 << 20
SEAT_INBOX_ROTATE_SECONDS = 7 * 86400
LOG_ROTATE_BYTES = 5 << 20
LOG_KEEP = 3
PROBE_CHANNEL = "sandbox"
PROBE_TOPIC = "listener probe"
PAUSE_COMMAND = "agent-sync pause"
PLUGIN_MANIFEST = os.path.join(A.REPO_ROOT, "plugins", "agent-sync", ".claude-plugin", "plugin.json")
# A process's environment is fixed when it starts:  `daemon reload` re-reads listener.toml only.
ENV_RESTART_HINT = ("environment variables are read once, when the listener starts:  restart it (on the server, "
                    "restart or redeploy the app) after adding or changing one; agent-sync daemon reload re-reads "
                    "only listener.toml")


class Clock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class RoleRefused(Z.AgentSyncError):
    exit_code = 3


def role_refused(person: Mapping[str, Any]) -> bool:
    """True for an admin or owner bot (or an unknown role):  the listener refuses those keys."""
    return bool(person.get("is_admin") or person.get("is_owner") or person.get("role") not in FLEET_ROLES)


def role_refusal(seat: str, role: Any) -> str:
    return ("the %s bot has role %s; the listener accepts only moderator (300) and member (400) bots and refuses "
            "admin and owner keys" % (seat, role))


# --------------------------------------------------------------------------------------------
# Logging (JSON lines, scrubbed, rotated)
# --------------------------------------------------------------------------------------------

class JsonLog:
    """listener.log, and (AGENT_SYNC_LOG_STDOUT=1, the server container) the same scrubbed line
    on stdout, written under the same lock."""

    def __init__(self, path: str, scrub: Callable[[str], str], tee: Any = None) -> None:
        self.path = path
        self.scrub = scrub
        self.lock = threading.Lock()
        self.tee = tee

    def write(self, event: str, **fields: Any) -> None:
        row = {"ts": round(time.time(), 3), "event": event}
        row.update(fields)
        text = self.scrub(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)) + "\n"
        with self.lock:
            try:
                L.private_dir(os.path.dirname(self.path))
                if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_ROTATE_BYTES:
                    for n in range(LOG_KEEP - 1, 0, -1):
                        older = "%s.%d" % (self.path, n)
                        if os.path.exists(older):
                            os.replace(older, "%s.%d" % (self.path, n + 1))
                    os.replace(self.path, self.path + ".1")
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, text.encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                pass
            if self.tee is not None:
                try:
                    self.tee.write(text)
                    self.tee.flush()
                except (OSError, ValueError):
                    pass


# --------------------------------------------------------------------------------------------
# Per-seat state
# --------------------------------------------------------------------------------------------

class Ring:
    def __init__(self, limit: int = RING_LIMIT) -> None:
        self.items: collections.OrderedDict[int, None] = collections.OrderedDict()
        self.limit = limit

    def __contains__(self, item: object) -> bool:
        return item in self.items

    def add(self, item: int) -> None:
        self.items[item] = None
        while len(self.items) > self.limit:
            self.items.popitem(last=False)


class SeatRunner:
    def __init__(self, daemon: "Daemon", cfg: C.SeatConfig) -> None:
        self.daemon = daemon
        self.cfg = cfg
        self.seat = cfg.seat
        self.paths = L.SeatPaths(daemon.root, cfg.seat)
        self.creds: Z.Credentials | None = None
        self.client: Z.ZulipClient | None = None
        self.me: R.SeatIdentity | None = None
        self.queue: Z.EventQueue | None = None
        cursor = L.read_json(self.paths.cursor).get("last_id")
        self.cursor: int | None = cursor if isinstance(cursor, int) else None
        self.floor: int = self.cursor or 0
        self.cursor_flushed = self.cursor
        self.cursor_flushed_at = 0.0
        self.ring = Ring()
        self.users: dict[int, dict[str, Any]] = {}
        self.users_version = 0  # bumped on every change to `users`, so the fleet-bot set is cached
        self.fleet_cache: tuple[Any, set[int]] | None = None
        self.streams: dict[int, str] = {}
        self.muted: set[str] = set()
        self.followed: set[str] = set()
        self.connected = False
        self.error: str | None = None
        self.fatal = False  # a credential or role problem: retried slowly, never spun
        self.failures = 0
        self.down_since: float | None = None
        self.next_attempt = 0.0
        self.wall_offset: float | None = None
        self.last_event_wall: float | None = None
        self.jobs: collections.deque[W.Pending] = collections.deque()
        self.jobs_cond = threading.Condition()
        self.running: W.Pending | None = None
        self.last_post = -1e9
        self.ledger = W.Ledger(self.paths.wakes)
        self.crashes: dict[int, int] = {}
        self.stop = threading.Event()  # set when a reload removes the seat:  its threads exit
        self.threads: dict[str, threading.Thread] = {}

    # ---- connection -------------------------------------------------------------------------
    def load_credentials(self) -> None:
        # One file per seat, or (creds = "env", server instance) the variables the seat names:
        # never ZULIP_RC or the CLI's triple, so seats cannot alias one bot.  The realm is never
        # taken from the credential:  its site must match AGENT_SYNC_REALM (or the default).
        try:
            creds = C.seat_credentials(self.cfg, self.daemon.env, self.daemon.secrets_dir)
        except Z.CredentialError as exc:
            if self.cfg.creds == "env" and "not set" in str(exc):
                raise Z.CredentialError("%s; %s" % (exc, ENV_RESTART_HINT)) from None
            raise
        Z.verify_realm(creds, self.daemon.realm)
        self.creds = creds
        self.daemon.hide(creds.email, creds.key)
        self.client = Z.ZulipClient(creds, self.daemon.realm, timeout=self.daemon.http_timeout,
                                    events_timeout=self.daemon.events_timeout, sleep=self.daemon.clock.sleep)

    def connect(self) -> list[dict[str, Any]]:
        """users/me and the role check, subscriptions, register, then the backfill.  Returns the
        backfilled messages (routing is the caller's job)."""
        if self.client is None:
            self.load_credentials()
        assert self.client is not None and self.creds is not None
        me = self.client.get("users/me")
        role = me.get("role")
        if str(me.get("email") or "").casefold() != self.creds.email.casefold():
            raise RoleRefused("users/me for seat %s answered as a different user; refusing it" % self.seat)
        if role_refused(me):
            raise RoleRefused(role_refusal(self.seat, role))
        # A seat holds only its own bot.  The partition is checked by seat name, so a section
        # under another name (bot = "GB-Compiler" in [seat.CLAUDE], or email_env and key_env
        # pointed at another seat's variables) would otherwise hold a bot that another seat or
        # the other instance holds too.  users/me says whose key this really is.
        tag = seat_tag_for(me)
        if tag != self.seat:
            raise RoleRefused("the credential for seat %s belongs to the bot of seat %s (users/me); a seat holds "
                              "only its own bot, so it is refused" % (self.seat, tag))
        self.me = R.SeatIdentity(self.seat, int(me["user_id"]), str(me.get("full_name") or ""), self.creds.email)
        for sub in self.client.get("users/me/subscriptions").get("subscriptions") or []:
            if isinstance(sub.get("stream_id"), int):
                self.streams[sub["stream_id"]] = str(sub.get("name") or "")
        old = self.queue
        if old is not None:
            old.close()
        self.queue = Z.EventQueue(self.client, None, EVENT_TYPES, clock=self.daemon.clock.monotonic)
        self.queue.register()
        state = self.queue.register_state
        for person in state.get("realm_users") or []:
            if isinstance(person, dict) and isinstance(person.get("user_id"), int):
                self.users[person["user_id"]] = person
        self.users_version += 1
        self.muted.clear()
        self.followed.clear()
        for entry in state.get("user_topics") or []:
            self.apply_user_topic(entry)
        self.wall_offset = self.daemon.clock.time() - self.daemon.clock.monotonic()
        self.connected = True
        self.error = None
        self.fatal = False
        if self.cursor is None:
            newest = state.get("max_message_id")
            self.cursor = int(newest) if isinstance(newest, int) and newest >= 0 else 0
            self.floor = self.cursor
            self.flush_cursor(force=True)
            return []
        self.floor = self.cursor
        return self.backfill()

    def fetch(self, narrow: list[dict[str, Any]], *, anchor: Any, before: int = 0, after: int = 0,
              include: bool = False) -> list[dict[str, Any]]:
        assert self.client is not None
        result = self.client.get("messages", {"narrow": narrow, "anchor": anchor, "num_before": before,
                                              "num_after": after, "include_anchor": include, "apply_markdown": False})
        return list(result.get("messages") or [])

    def backfill(self) -> list[dict[str, Any]]:
        """DMs, mentions and the owner's posts are paged from the cursor until exhausted.  Each
        leased or followed topic is fetched newest first, capped at 200 with a gap marker.
        Unleased chatter is never fetched."""
        with self.daemon.backfill_lock:  # queues register in parallel; backfill is serialized across bots
            return self._backfill()

    def _backfill(self) -> list[dict[str, Any]]:
        floor = self.floor
        found: dict[int, dict[str, Any]] = {}
        narrows = [[{"operator": "is", "operand": "dm"}], [{"operator": "is", "operand": "mentioned"}]]
        owner = self.daemon.config.owner_user_id
        if owner:
            narrows.append([{"operator": "sender", "operand": owner}])
        for narrow in narrows:
            anchor = floor
            for _ in range(MAX_PAGES):
                page = self.fetch(narrow, anchor=anchor, after=PAGE)
                for message in page:
                    if isinstance(message.get("id"), int) and message["id"] > floor:
                        found[message["id"]] = message
                if len(page) < PAGE:
                    break
                anchor = max(m["id"] for m in page)
        self.gaps: list[dict[str, Any]] = []
        for channel, topic in self.daemon.backfill_topics(self):
            page = self.fetch([{"operator": "channel", "operand": channel}, {"operator": "topic", "operand": topic}],
                              anchor="newest", before=TOPIC_BACKFILL_CAP)
            newer = [m for m in page if isinstance(m.get("id"), int) and m["id"] > floor]
            for message in newer:
                found[message["id"]] = message
            if len(newer) >= TOPIC_BACKFILL_CAP:
                self.gaps.append({"channel": channel, "topic": topic, "oldest": min(m["id"] for m in newer)})
        return [found[i] for i in sorted(found)]

    def apply_user_topic(self, entry: Mapping[str, Any]) -> None:
        channel = self.streams.get(entry.get("stream_id"), "")
        key = L.topic_key(channel, str(entry.get("topic_name") or ""))
        policy = entry.get("visibility_policy")
        self.muted.discard(key)
        self.followed.discard(key)
        if policy == 1:
            self.muted.add(key)
        elif policy == 3:
            self.followed.add(key)

    def advance(self, message_id: int) -> None:
        if self.cursor is None or message_id > self.cursor:
            self.cursor = message_id

    def flush_cursor(self, *, force: bool = False) -> None:
        now = self.daemon.clock.monotonic()
        if self.cursor is None or self.cursor == self.cursor_flushed:
            return
        if not force and now - self.cursor_flushed_at < CURSOR_FLUSH_SECONDS:
            return
        L.write_json(self.paths.cursor, {"last_id": self.cursor})
        self.cursor_flushed = self.cursor
        self.cursor_flushed_at = now

    def is_bot_map(self) -> dict[int, bool]:
        return {uid: bool(u.get("is_bot")) for uid, u in self.users.items()}

    def disconnect(self) -> None:
        self.connected = False
        if self.queue is not None:
            problem = self.queue.close()
            if problem:
                self.daemon.log.write("queue-delete-failed", seat=self.seat)


# --------------------------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------------------------

class Daemon:
    def __init__(self, *, env: Mapping[str, str] | None = None, root: str | None = None, home: str | None = None,
                 clock: Clock | None = None, notifier: A.Notifier | None = None,
                 claude_runner: A.ClaudeRunner | None = None, wake_path: str | None = None,
                 agents_runner: Callable[[Sequence[str]], str | None] | None = None,
                 board_runner: Callable[..., Any] | None = None, board_bin: str | None = None,
                 start_time: Callable[[int], int] | None = None, http_timeout: float = 30.0,
                 events_timeout: float | None = None, stderr: Any = None, stdout: Any = None,
                 routine_runner: A.RoutineRunner | None = None, partition_file: str | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.home = home or self.env.get("HOME") or os.path.expanduser("~")
        self.root = root or L.state_root(self.env)
        self.clock = clock or Clock()
        self.realm = Z.realm_url(self.env)
        self.secrets_dir = os.path.expanduser(self.env.get(Z.ENV_SECRETS_DIR) or os.path.join(self.home, ".secrets", "Zulip"))
        self.hidden: list[str] = []
        self.hidden_lock = threading.Lock()
        self.stdout = stdout or sys.stdout
        tee = self.stdout if self.env.get("AGENT_SYNC_LOG_STDOUT", "").strip().casefold() in ("1", "true", "yes") else None
        self.log = JsonLog(os.path.join(L.logs_dir(self.root), "listener.log"), self.scrub, tee=tee)
        self.stderr = stderr or sys.stderr
        self.notifier = notifier or A.Notifier(self.root, clock=self.clock.time)
        self.banners_allowed = self.notifier.banners  # the config can only turn banners off (the server has none)
        self.claude_runner = claude_runner or A.ClaudeRunner(on_start=self._track_child, on_exit=self._untrack_child)
        self.wake_path = wake_path or A.default_wake_path(self.home)
        self.agents_runner = agents_runner or self._run_agents
        self.board_runner = board_runner
        self.board_bin = board_bin or shutil.which("board", path=self.wake_path) or "board"
        self.start_time = start_time or _process_start
        self.http_timeout = http_timeout
        self.events_timeout = events_timeout
        self.stop = threading.Event()
        self.routine_runner = routine_runner or A.RoutineRunner(sleep=self.clock.sleep, clock=self.clock.monotonic,
                                                                should_stop=self.stop.is_set)
        self.config_path = L.config_path(self.root, self.env)
        self.partition_file = partition_file or C.partition_path(self.env)
        # Eligible senders' tags (owner 2026-10-09):  every partition seat plus FLEET_SEATS, refreshed
        # with the partition on every check, so a seat added to the partition is eligible without a re-init.
        self.fleet_tags: frozenset[str] = C.fleet_tags(None)
        self.partition: dict[str, str] = {}  # seat -> instance, from the last partition check (route checks)
        self.config = C.load(self.root, self.config_path)
        # The seat partition:  a config that holds a seat this instance may not hold never gets a
        # queue; run() refuses to start (no flock, no thread, no register).
        self.refusal = self.partition_check(self.config)
        if self.refusal:
            self.config.seats = {}
        self.reload_refused: list[str] = []
        self.seats: dict[str, SeatRunner] = {}
        self.coalescer = W.Coalescer(window=self.config.coalesce_seconds, max_window=self.config.coalesce_max_seconds,
                                     owner_window=self.config.owner_coalesce_seconds, new_id=self._new_wake_id)
        self.loopguard = W.LoopGuard(os.path.join(L.listener_dir(self.root), "loopguard.json"))
        self.global_ring = Ring()
        self.agents_cache: tuple[float, dict[int, str] | None] = (-1e9, None)
        self.start_cache: dict[int, tuple[float, int]] = {}
        self.children: set[subprocess.Popen[Any]] = set()  # every running wake child, one per waking seat
        self.child_lock = threading.Lock()
        self.router_queue: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        self.backfill_lock = threading.Lock()
        self.started_at = self.clock.time()
        self.last_tick = {"reap": -1e9, "status": -1e9, "grace": -1e9, "prune": -1e9}
        self.config_notified = False
        self.reload_requested = False
        self.acronyms = A.fleet_acronyms()
        # Reports to Sentry Crons that this instance is running (heartbeat.py);  off with no DSN.
        self.heartbeat = HB.SentryHeartbeat(env=self.env, home=self.home, clock=self.clock.time, log=self.log.write,
                                            scrub=self.scrub, hide=self.hide_values)
        self._apply_config()
        # A listener that already holds the state directory owns its ledgers and cursors:  a
        # second daemon (a container waiting to take over on a redeploy, or a stray manual run)
        # must not mark that listener's running wake failed or reload its queued wakes.  run()
        # rebuilds everything from disk once it holds the lock.
        self.deferred = self.lock_held_elsewhere()
        if not self.deferred:
            self._recover_ledgers()

    # ---- secrecy -------------------------------------------------------------------------------
    def hide(self, email: str, key: str) -> None:
        with self.hidden_lock:
            for value in secretscan.basic_forms(email, key):
                if value and value not in self.hidden:
                    self.hidden.append(value)

    def hide_values(self, values: Iterable[str]) -> None:
        """Scrub these exact values (a routine URL, its query string, its key) from every log
        line, error and status, like a bot key."""
        with self.hidden_lock:
            for value in values:
                if value and len(value) >= 4 and value not in self.hidden:  # never redact a 1-3 character string
                    self.hidden.append(value)
            self.hidden.sort(key=len, reverse=True)  # the whole URL before the query inside it

    def scrub(self, text: str) -> str:
        with self.hidden_lock:
            hidden = list(self.hidden)
        for value in hidden:
            text = text.replace(value, "[redacted]")
        return text

    def install_excepthooks(self) -> None:
        """Uncaught exceptions in any thread are logged and printed scrubbed of every loaded key
        and its base64 form, never as a raw traceback."""
        def report(where: str, exc_type: Any, exc: Any) -> None:
            text = self.scrub("%s: %s" % (getattr(exc_type, "__name__", "Error"), exc))
            self.log.write("uncaught", where=where, error=text)
            try:
                self.stderr.write("agent-sync daemon: %s: %s\n" % (where, text))
                self.stderr.flush()
            except (OSError, ValueError):
                pass

        sys.excepthook = lambda t, e, tb: report("main", t, e)
        threading.excepthook = lambda args: report(getattr(args.thread, "name", "thread"), args.exc_type, args.exc_value)

    # ---- config ------------------------------------------------------------------------------
    def partition_check(self, cfg: C.Config) -> list[str]:
        """Why this config must not run here (the seat partition), or []."""
        partition, error = C.load_partition(self.partition_file)
        if error:
            return [error + "; cannot check the seat partition, so no seat is held"]
        self.fleet_tags = C.fleet_tags(partition)
        self.partition = dict(partition or {})
        env_instance = (self.env.get(C.ENV_INSTANCE) or "").strip().casefold() or None
        return C.partition_errors(cfg, partition, partition_file=self.partition_file, env_instance=env_instance)

    def routine(self, runner: "SeatRunner") -> tuple[A.RoutineTarget | None, str | None, str]:
        """The seat's routine, resolved from the environment the daemon started with.  A reload
        re-reads listener.toml (so it picks up changed variable names or auth settings), but the
        values come from the process environment, which is fixed at start:  only a restart (on
        the server, restarting or redeploying the container) picks up a new or changed URL or
        key.  Its secrets are hidden before anything can log them."""
        target, why, detail = A.resolve_routine(runner.cfg.routine, self.env)
        if target is not None:
            self.hide_values(target.secrets())
        elif why == "routine_not_configured" and "not set" in detail:
            detail = "%s; %s" % (detail, ENV_RESTART_HINT)
        return target, why, detail

    def _apply_config(self) -> None:
        cfg = self.config
        self.notifier.banners = self.banners_allowed and cfg.notify_banners
        for seat_cfg in list(cfg.seats.values()) + list(cfg.disabled.values()):
            # Routine secrets are hidden as soon as the config names them, before any wake.
            if seat_cfg.routine is None:
                continue
            # A refused routine (a malformed URL included) still hides whatever its variables
            # hold.  routine_secret_values never raises, so a typo in a URL variable cannot stop
            # the daemon at start, on a reload or on a rebuild.
            target, _, _ = A.resolve_routine(seat_cfg.routine, self.env)
            if target is not None:
                self.hide_values(target.secrets())
                continue
            self.hide_values(A.routine_secret_values(self.env.get(seat_cfg.routine.url_env) or "",
                                                     self.env.get(seat_cfg.routine.key_env) or ""))
        if cfg.errors:
            self.log.write("config-errors", errors=cfg.errors)
            if not self.config_notified:
                self.config_notified = True
                self.notifier.notify(cfg.claude_seat or "LISTENER", "agent-sync listener", "config problem: %d issue%s; "
                                     "see agent-sync status" % (len(cfg.errors), "" if len(cfg.errors) == 1 else "s"),
                                     kind="config")
        self.coalescer.window = cfg.coalesce_seconds
        self.coalescer.max_window = cfg.coalesce_max_seconds
        self.coalescer.owner_window = cfg.owner_coalesce_seconds
        for seat, seat_cfg in cfg.seats.items():
            if seat in self.seats:
                self.seats[seat].cfg = seat_cfg
            else:
                self.seats[seat] = SeatRunner(self, seat_cfg)
        for seat in [s for s in self.seats if s not in cfg.seats]:
            gone = self.seats.pop(seat)
            gone.stop.set()  # its poller and wake worker exit instead of reconnecting
            gone.disconnect()

    def reload(self) -> None:
        new = C.load(self.root, self.config_path)
        refused = self.partition_check(new)
        if refused:
            # Never a runner for a seat this instance may not hold:  the running config stays.
            self.reload_refused = refused
            self.log.write("reload-refused", errors=refused)
            self.notifier.notify(self.config.claude_seat or "LISTENER", "agent-sync listener",
                                 "reload refused: the config holds a seat this instance may not hold; see agent-sync "
                                 "status",
                                 kind="partition", key="partition-reload")
            return
        self.reload_refused = []
        self.config = new
        self.config_notified = False
        self._apply_config()
        self.log.write("reloaded", seats=sorted(self.seats), errors=len(self.config.errors))

    def _recover_ledgers(self) -> None:
        now = self.clock.time()
        for runner in self.seats.values():
            reload, crashed = runner.ledger.recover(now)
            for view in reload:
                pending = W.Pending.from_view(view)
                if view.get("state") == "accepted":
                    runner.jobs.append(pending)
                else:
                    self.coalescer.restore(pending)
            for view in crashed:
                self.log.write("wake-crashed", seat=runner.seat, wake_id=view.get("wake_id"))
                if view.get("owner"):
                    self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                         "a wake for your message stopped mid-run and was not re-run",
                                         kind="wake-crashed", key="crash:%s" % view.get("wake_id"))

    def lock_path(self) -> str:
        return os.path.join(L.listener_dir(self.root), "daemon.lock")

    def lock_held_elsewhere(self) -> bool:
        """True while another listener holds daemon.lock (a shared probe, released at once)."""
        import fcntl

        try:
            fd = os.open(self.lock_path(), os.O_RDONLY)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def rebuild_from_disk(self) -> None:
        """After waiting for another listener:  drop every runner built while it ran (their
        cursors are stale) and every wake it had pending, then read the config, the cursors and
        the ledgers again, as that listener left them when it stopped."""
        for runner in self.seats.values():
            runner.stop.set()
        self.seats = {}
        self.coalescer.pending.clear()
        self.config = C.load(self.root, self.config_path)
        self.refusal = self.partition_check(self.config)
        if self.refusal:
            self.config.seats = {}
        self.reload_refused = []
        self.config_notified = False
        self._apply_config()
        self._recover_ledgers()
        self.deferred = False
        self.log.write("rebuilt-after-wait", seats=sorted(self.seats))

    def _new_wake_id(self) -> str:
        return "w%d-%s" % (int(self.clock.time()), os.urandom(4).hex())

    # ---- liveness ------------------------------------------------------------------------------
    def _run_agents(self, argv: Sequence[str]) -> str | None:
        return A.run_agents(argv, A.claude_env(self.env, self.home, self.wake_path), self.home)

    def agent_kinds(self) -> dict[int, str] | None:
        """pid -> kind from `claude agents --json`, cached 30 seconds.  None when the command is not
        available or its output cannot be read; liveness then rests on the pid and its start time.

        It runs only the pinned realpath, only when `test-wake` found `agents --json` usable on
        that binary (`agents_ok` in the pin), and never while a pause covers the Claude seat, so
        the daemon never starts an unchecked claude binary on its own."""
        at, cached = self.agents_cache
        now = self.clock.monotonic()
        if now - at < AGENTS_CACHE_SECONDS:
            return cached
        result: dict[int, str] | None = None
        seat = self.config.claude_seat or "CLAUDE"
        claude = A.resolve_claude(self._claude_cfg_path(), self.wake_path)
        paths = L.SeatPaths(self.root, seat)
        if claude and not L.paused(self.root, seat, wakes=True) and A.agents_pinned(paths, claude):
            rows = A.parse_agents(self.agents_runner([A.binary_pin(claude), "agents", "--json"]))
            if rows is not None:
                result = {int(r["pid"]): str(r.get("kind") or r.get("type") or "").casefold() for r in rows
                          if isinstance(r.get("pid"), int) and not isinstance(r.get("pid"), bool)}
        self.agents_cache = (now, result)
        return result

    def _claude_cfg_path(self) -> str | None:
        seat = self.config.seats.get(self.config.claude_seat or "CLAUDE")
        return seat.claude if seat else None

    def _start_matches(self, pid: int, expected: Any) -> bool:
        if not isinstance(expected, int) or expected == 0:
            return True  # unknown at write time; the pid check stands alone
        now = self.clock.monotonic()
        at, value = self.start_cache.get(pid, (-1e9, 0))
        if now - at > AGENTS_CACHE_SECONDS:
            value = self.start_time(pid)
            self.start_cache[pid] = (now, value)
        return value == 0 or abs(value - expected) <= 2

    def lease_alive(self, paths: L.SeatPaths, lease: Mapping[str, Any]) -> tuple[bool, str]:
        if not lease:
            return False, "no lease"
        lease_id = str(lease.get("lease_id") or "")
        if os.path.exists(os.path.join(paths.live_dir(lease_id), "ended.json")):
            return False, "ended"
        now = self.clock.time()
        pid = lease.get("pid")
        if not L.pid_alive(pid):
            return False, "pid gone"
        if isinstance(lease.get("wait_until"), (int, float)) and lease["wait_until"] > now:
            # The grace bridges the gap between one `attach --wait` and the next for a live
            # agent; it never keeps a lease alive after the agent process itself is gone.
            if not self._start_matches(int(pid), lease.get("pid_start")):
                return False, "pid reused"
            return True, "wait grace"
        if not self._start_matches(int(pid), lease.get("pid_start")):
            return False, "pid reused"
        if lease.get("platform") == "claude-code":
            # Positive evidence only:  the lease dies when the listing shows this pid with another
            # kind (a background or -p run).  A pid missing from it, or a listing in a shape this
            # code does not know, leaves the pid and start-time checks to decide.
            kinds = self.agent_kinds()
            kind = kinds.get(pid) if kinds else None
            if kind and kind != "interactive":
                return False, "not an interactive claude (%s)" % kind[:20]
            return True, "ok"
        beat = lease.get("heartbeat")
        if not isinstance(beat, (int, float)) or now - beat > LEASE_HEARTBEAT_LIMIT:
            return False, "no heartbeat"
        return True, "ok"

    def lease_views(self, runner: SeatRunner) -> list[R.LeaseView]:
        now = self.clock.time()
        limits = runner.cfg.live
        views: list[R.LeaseView] = []
        for lease_id in L.list_lease_ids(runner.paths):
            lease = L.load_lease(runner.paths, lease_id)
            alive, _ = self.lease_alive(runner.paths, lease)
            if not alive:
                continue
            budget = L.read_json(runner.paths.budget(lease_id))
            platform = str(lease.get("platform") or "")
            claude = platform == "claude-code"

            def room(key: str, owner: bool, budget: Mapping[str, Any] = budget, claude: bool = claude) -> str | None:
                return L.live_room(budget, key, owner, now, limits) if claude else None

            capable = (claude and lease.get("wake_capable") is not False and self.config.rewake_verified
                       and not L.paused(self.root, runner.seat) and L.watcher_alive(runner.paths, lease_id))
            views.append(R.LeaseView(lease_id, platform=platform, topics=L.lease_topics(lease, now).keys(),
                                     mentions=bool(lease.get("mentions")), wake_capable=capable,
                                     posted=[p for p in lease.get("posted") or [] if isinstance(p, int)],
                                     prefix=L.session_prefix(runner.seat, lease.get("session_id")),
                                     last_prompt=float(lease.get("last_prompt") or 0),
                                     created=float(lease.get("created") or 0), room=room))
        return views

    def backfill_topics(self, runner: SeatRunner) -> list[tuple[str, str]]:
        now = self.clock.time()
        found: dict[str, tuple[str, str]] = {}
        for lease_id in L.list_lease_ids(runner.paths):
            for entry in L.lease_topics(L.load_lease(runner.paths, lease_id), now).values():
                channel, topic = str(entry.get("channel") or ""), str(entry.get("topic") or "")
                if channel and topic:
                    found[L.topic_key(channel, topic)] = (channel, topic)
        for key in list(runner.followed):
            channel, _, topic = key.partition("\u0000")
            found.setdefault(key, (channel, topic))
        return list(found.values())

    # ---- routing -------------------------------------------------------------------------------
    def eligible_ids(self, runner: SeatRunner) -> set[int]:
        """The eligible senders besides the owner:  every fleet bot in the seat's realm user list
        (router.fleet_bot_ids over the partition's tags, owned by the owner), plus the optional
        eligible_user_ids pins.  Cached until the user list, the tags or the owner pin changes."""
        key = (runner.users_version, self.fleet_tags, self.config.owner_user_id)
        if runner.fleet_cache is None or runner.fleet_cache[0] != key:
            runner.fleet_cache = (key, R.fleet_bot_ids(runner.users, self.fleet_tags, self.config.owner_user_id))
        return set(self.config.eligible_user_ids) | runner.fleet_cache[1]

    def context(self, runner: SeatRunner) -> R.Context:
        cfg = self.config
        return R.Context(owner_user_id=cfg.owner_user_id, owner_clients=cfg.owner_clients,
                         eligible_user_ids=self.eligible_ids(runner), is_bot=runner.is_bot_map(), now=self.clock.time(),
                         stale_after=cfg.stale_after, presence=cfg.presence, muted=runner.muted,
                         followed=runner.followed)

    def handle_events(self, runner: SeatRunner, events: Iterable[Mapping[str, Any]]) -> None:
        for event in events:
            kind = event.get("type")
            try:
                if kind == "message" and isinstance(event.get("message"), dict):
                    self.route_message(runner, event["message"])
                elif kind == "update_message":
                    self.handle_update(runner, event)
                elif kind == "user_topic":
                    runner.apply_user_topic(event)
                elif kind == "realm_user":
                    person = event.get("person") or {}
                    uid = person.get("user_id")
                    if isinstance(uid, int):
                        runner.users_version += 1
                        if event.get("op") == "remove":
                            runner.users.pop(uid, None)
                        else:
                            entry = runner.users.setdefault(uid, {})
                            entry.update(person)
                            if "role" in person and "is_admin" not in person:  # Zulip sends only the changed field
                                entry["is_admin"] = person["role"] in (100, 200)
                                entry["is_owner"] = person["role"] == 100
                        if runner.me is not None and uid == runner.me.user_id and event.get("op") != "remove" \
                                and role_refused(runner.users[uid]):
                            # The seat's own bot was promoted to admin or owner:  drop the queue now,
                            # not when it next expires (owner decision 2026-10-07).
                            self._refuse(runner, role_refusal(runner.seat, runner.users[uid].get("role")))
            except Exception as exc:  # one bad event must never stop the main loop or the batch
                self.log.write("event-error", seat=runner.seat, type=str(kind)[:40], id=event.get("id"),
                               error=self.scrub(type(exc).__name__ + ": " + str(exc)))
        runner.flush_cursor()

    def route_backfill(self, runner: SeatRunner, messages: list[dict[str, Any]]) -> None:
        for message in sorted(messages, key=lambda m: m.get("id", 0)):
            self.route_message(runner, message)
        for gap in getattr(runner, "gaps", []):
            for view in self.lease_views(runner):
                if L.topic_key(gap["channel"], gap["topic"]) in view.topics:
                    L.append_live(runner.paths, view.lease_id, [{
                        "kind": "gap", "class": "passive", "channel": gap["channel"], "topic": gap["topic"],
                        "id": gap["oldest"], "ts": self.clock.time(), "sender": "agent-sync",
                        "content": "Older messages were not fetched; use agent-sync read --since with an older id."}])
        runner.gaps = []
        runner.flush_cursor(force=True)

    def route_message(self, runner: SeatRunner, message: Mapping[str, Any]) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, int) or runner.me is None:
            return
        if message_id <= runner.floor or message_id in runner.ring:
            return
        runner.ring.add(message_id)
        self._note_probe(runner, message)
        done: set[Any] = set()  # side effects that completed, so a retry never repeats them
        for attempt in range(3):
            try:
                self._route(runner, message, done)
                break
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop routing
                runner.crashes[message_id] = runner.crashes.get(message_id, 0) + 1
                self.log.write("route-error", seat=runner.seat, id=message_id, attempt=attempt + 1,
                               error=self.scrub(type(exc).__name__ + ": " + str(exc)))
        else:
            self._append_seat_inbox(runner, [self._quarantine_item(message)])
            self.log.write("quarantined", seat=runner.seat, id=message_id)
        runner.advance(message_id)

    def _quarantine_item(self, message: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": message.get("id"), "kind": "message", "quarantined": True, "channel": Z.message_channel(message),
                "topic": Z.message_topic(message), "sender_id": message.get("sender_id"),
                "sender": message.get("sender_full_name"), "ts": message.get("timestamp"),
                "content": message.get("content"), "classes": []}

    def _note_probe(self, runner: SeatRunner, message: Mapping[str, Any]) -> None:
        if Z.message_channel(message).casefold() == PROBE_CHANNEL and Z.message_topic(message).casefold() == PROBE_TOPIC:
            path = os.path.join(L.listener_dir(self.root), "probe.json")
            L.update_json(path, lambda d: d.__setitem__(runner.seat, (list(d.get(runner.seat) or []) + [message["id"]])[-20:]))

    def make_item(self, runner: SeatRunner, message: Mapping[str, Any], c: R.Classes, d: R.Decision) -> dict[str, Any]:
        ts = message.get("timestamp")
        recipients = [r.get("id") for r in message.get("display_recipient") or [] if isinstance(r, dict)] if c.dm else []
        return {"kind": "message", "id": message.get("id"), "type": "private" if c.dm else "stream",
                "channel": c.channel, "topic": c.topic, "stream_id": message.get("stream_id"), "recipients": recipients,
                "sender_id": message.get("sender_id"), "sender": message.get("sender_full_name"),
                "sender_email": message.get("sender_email"), "is_bot": c.sender_is_bot, "owner": c.owner,
                "owner_api": c.owner_api, "ts": ts, "time": format_time(float(ts or 0)),
                "content": message.get("content"), "flags": list(message.get("flags") or []), "classes": c.labels(),
                "stale": c.stale, "wake_worthy": d.wake_worthy, "wake_owner": d.wake_owner,
                "routed_at": self.clock.time()}

    def _route(self, runner: SeatRunner, message: Mapping[str, Any], done: set[Any] | None = None) -> None:
        """Route one message.  `done` is shared by route_message's retries:  each append and the
        wake offer is recorded there once it succeeds, so a retry after a partial failure resumes
        instead of delivering a second copy."""
        assert runner.me is not None
        done = set() if done is None else done
        now = self.clock.time()
        views = self.lease_views(runner)
        c, d = R.route(message, runner.me, self.context(runner), views)
        first_seen = message["id"] not in self.global_ring
        if first_seen:
            # The note comes before the ring mark:  a note that raises is retried, and a later
            # failure never counts the same wake reply twice.
            # A DM thread without the owner can never be reset by him, so its count is rolling.
            rolling = c.dm and str(self.config.owner_user_id) not in c.topic.split(",")
            # A `·route` post counts like a wake reply (owner 2026-10-10), though it still wakes its target.
            counts = c.wake_tag or c.route_tag
            self.loopguard.note(c.key, wake_reply=counts, owner=c.owner, rolling=rolling, now=now)
            self.global_ring.add(message["id"])
            if c.owner_api:
                self.notifier.notify(self.config.claude_seat or runner.seat, "agent-sync: owner account",
                                     "a post from your account came from an API client (%s), treated as not you"
                                     % A.clean_banner(str(message.get("client") or "?"), 40),
                                     kind="owner-api", key="owner-api")
        if c.owner and not c.dm:
            for view in views:
                if c.key in view.topics:
                    L.reset_streak(runner.paths, view.lease_id, c.key)
        self.log.write("route", seat=runner.seat, id=message["id"], channel=c.channel if not c.dm else "DM",
                       topic=L.escape_line(c.topic, 60) if not c.dm else "", sender=message.get("sender_id"),
                       classes=c.labels(), decision=d.describe())
        if c.owner and c.dm and str(message.get("content") or "").strip().casefold() == PAUSE_COMMAND:
            self.pause(seat=None, wakes_only=False, by="owner-dm", message_id=message["id"])
            return
        item = self.make_item(runner, message, c, d)
        for lease_id, klass, kind in d.lease_items:
            if ("live", lease_id) in done:
                continue
            row = dict(item, **{"class": klass, "kind": kind})
            L.append_live(runner.paths, lease_id, [row])
            done.add(("live", lease_id))
        for lease_id, reason in d.throttled:
            if reason == "per_topic":
                continue  # routine spacing:  the item is a passive headline, no banner and no throttle line
            if self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                    "live budget reached (%s); the topic is passive in that session" % reason,
                                    kind="live-budget", key="live:%s:%s" % (lease_id, c.key)):
                L.append_live(runner.paths, lease_id, [{
                    "kind": "throttle", "class": "passive", "channel": c.channel, "topic": c.topic,
                    "id": message["id"], "ts": now, "sender": "agent-sync",
                    "content": "Live budget reached (%s): this topic is passive here until the budget resets." % reason}])
        if d.seat_inbox:
            row = dict(item, delivered_to=d.delivered_to)
            if "inbox" not in done:
                self._append_seat_inbox(runner, [row])
                done.add("inbox")
            if d.wake and not d.delivered_to and "wake" not in done:
                self.offer_wake(runner, row, owner=d.wake == "owner")
                done.add("wake")

    def _append_seat_inbox(self, runner: SeatRunner, rows: list[dict[str, Any]]) -> None:
        paths = runner.paths
        with L.locked(paths.seat_inbox_meta):
            meta = L.read_json(paths.seat_inbox_meta)
            seq = int(meta.get("next_seq") or 1)
            now = self.clock.time()
            try:
                size = os.path.getsize(paths.seat_inbox)
            except OSError:
                size = 0
            started = meta.get("file_started")
            if size and (size > SEAT_INBOX_ROTATE_BYTES or
                         (isinstance(started, (int, float)) and now - started > SEAT_INBOX_ROTATE_SECONDS)):
                os.replace(paths.seat_inbox, paths.seat_inbox + ".1")
                size = 0
            if not size:
                meta["file_started"] = now
            numbered = []
            for row in rows:
                numbered.append(dict(row, seq=seq))
                seq += 1
            L.append_jsonl(paths.seat_inbox, numbered)
            meta["next_seq"] = seq
            L.write_json(paths.seat_inbox_meta, meta)

    def handle_update(self, runner: SeatRunner, event: Mapping[str, Any]) -> None:
        """A topic rename (propagate_mode change_all) rewrites the seat's leases and CLI cursors.
        A content-only edit is ignored and never delivered."""
        new_topic = event.get("subject", event.get("topic"))
        old_topic = event.get("orig_subject", event.get("orig_topic"))
        stream_id = event.get("stream_id")
        new_stream = event.get("new_stream_id")
        if event.get("propagate_mode") != "change_all" or (new_topic is None and new_stream is None):
            return
        old_channel = runner.streams.get(stream_id, "")
        new_channel = runner.streams.get(new_stream, old_channel) if new_stream is not None else old_channel
        old_topic = str(old_topic if old_topic is not None else new_topic or "")
        new_topic = str(new_topic if new_topic is not None else old_topic)
        if not old_channel:
            return
        old_key = L.topic_key(old_channel, old_topic)
        renamed = 0
        for lease_id in L.list_lease_ids(runner.paths):
            def mutate(lease: dict[str, Any]) -> None:
                nonlocal renamed
                for entry in lease.get("topics") or []:
                    if isinstance(entry, dict) and L.topic_key(str(entry.get("channel") or ""),
                                                              str(entry.get("topic") or "")) == old_key:
                        entry["channel"], entry["topic"] = new_channel, new_topic
                        renamed += 1

            L.update_lease(runner.paths, lease_id, mutate)
        try:
            sessions = os.listdir(runner.paths.seat_dir)
        except OSError:
            sessions = []
        for name in sessions:
            state_path = os.path.join(runner.paths.seat_dir, name, "state.json")
            if not os.path.isfile(state_path):
                continue

            def rename_cursor(data: dict[str, Any]) -> None:
                cursors = data.get("cursors")
                if isinstance(cursors, dict) and old_key in cursors:
                    value = cursors.pop(old_key)
                    new_key = L.topic_key(new_channel, new_topic)
                    cursors[new_key] = max(value, cursors.get(new_key, value)) if isinstance(value, int) else value

            L.update_json(state_path, rename_cursor)
        self.log.write("rename", seat=runner.seat, leases=renamed)

    # ---- wakes ---------------------------------------------------------------------------------
    def offer_wake(self, runner: SeatRunner, item: Mapping[str, Any], *, owner: bool) -> None:
        now = self.clock.time()
        key = L.topic_key(str(item.get("channel") or ""), str(item.get("topic") or ""))
        if runner.cfg.wake not in C.WORKER_WAKES:
            if owner:
                self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                     "a message from you is waiting in the %s seat inbox (no headless wake)" % runner.seat,
                                     kind="inbox-owner", key="inbox-owner:%s:%s" % (runner.seat, key), once_per=3600)
            return
        if self.loopguard.blocked(key, now):
            self.log.write("wake-blocked", seat=runner.seat, id=item.get("id"), reason="loop_guard")
            return
        if owner and item.get("stale"):
            for view in runner.ledger.wakes().values():
                if view.get("topic_key") == key and view.get("stale") and isinstance(view.get("ts"), (int, float)) \
                        and now - float(view["ts"]) < W.DAY:
                    self.log.write("wake-blocked", seat=runner.seat, id=item.get("id"), reason="stale_once")
                    return
        pending = self.coalescer.offer(runner.seat, item, owner, now)
        if item.get("stale"):
            pending.stale = True
        runner.ledger.append(pending.row("queued", now))

    def finalize(self, pending: W.Pending) -> None:
        now = self.clock.time()
        runner = self.seats.get(pending.seat)
        if runner is None:
            return
        reason = self._wake_block(runner, pending, now)
        if reason:
            self._drop(runner, pending, reason, now)
            return
        lease_id = self.takeover(runner, pending)
        if lease_id:
            self._hand_to_lease(runner, pending, lease_id)
            runner.ledger.append(pending.row("skipped", now, reason="leased", lease=lease_id))
            return
        with runner.jobs_cond:
            if len(runner.jobs) >= W.FIFO_LIMIT:
                overflow = True
            else:
                overflow = False
                runner.ledger.append(pending.row("accepted", now, reserve_usd=runner.cfg.wake_max_usd,
                                                 adapter=runner.cfg.wake))
                runner.jobs.append(pending)
                runner.jobs_cond.notify_all()
        if overflow:
            runner.ledger.append(pending.row("dropped", now, reason="overflow"))
            self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat, "wake queue full; the message is in the "
                                 "seat inbox", kind="wake-overflow", key="wake:%s:overflow" % runner.seat)

    def _drop(self, runner: SeatRunner, pending: W.Pending, reason: str, now: float) -> None:
        runner.ledger.append(pending.row("dropped", now, reason=reason))
        self.log.write("wake-dropped", seat=runner.seat, wake_id=pending.wake_id, reason=reason)
        if reason != "stale":
            self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                 "a wake was not run (%s); the message is in the seat inbox" % reason,
                                 kind="wake-" + reason, key="wake:%s:%s" % (runner.seat, reason))

    def _pinned_claude(self, runner: SeatRunner) -> tuple[str | None, str | None]:
        """(the realpath to run, why not).  Resolved once, so the pin check and the argv agree."""
        claude = A.resolve_claude(runner.cfg.claude, self.wake_path)
        if not claude:
            return None, "no_claude"
        real = A.binary_pin(claude)
        if not A.pinned(runner.paths, real):
            return None, "not_pinned"
        return real, None

    def _wake_block(self, runner: SeatRunner, pending: W.Pending, now: float) -> str | None:
        """Why this wake must not run now, or None.  Checked when the quiet window ends and again
        just before the run (a restored or long-queued wake gets the same checks)."""
        if self.config.owner_user_id == 0:
            return "owner_not_pinned"
        if runner.fatal:
            return "seat_refused"  # a credential or role problem:  nothing is posted with that key
        if L.paused(self.root, runner.seat, wakes=True):
            return "paused"
        if runner.cfg.wake == "claude":
            _, why = self._pinned_claude(runner)
            if why:
                return why
        elif runner.cfg.wake == "http":
            _, why, _ = self.routine(runner)
            if why:
                return why
        else:
            return "inbox_only"
        if self.loopguard.blocked(pending.key, now):
            return "loop_guard"
        if not pending.owner and pending.trigger_ts is not None and now - pending.trigger_ts > self.config.stale_after:
            return "stale"  # a stale non-owner item never wakes, even after a restart
        return W.budget_block(runner.ledger.wakes(), runner.cfg.budget, owner=pending.owner, key=pending.key,
                              now=now, exclude=pending.wake_id, run_max=runner.cfg.wake_max_usd)

    def takeover(self, runner: SeatRunner, pending: W.Pending) -> str | None:
        """A live session that took over:  a lease on the topic, or for a mention trigger, a
        wake-capable claude-code lease with mentions.  A lease the trigger came back from (grace
        release or reap) never takes it again, so the headless wake runs instead of looping."""
        skip = set(pending.skip_leases)
        views = [v for v in self.lease_views(runner) if v.lease_id not in skip]
        if pending.type == "stream":
            on_topic = [v for v in views if pending.key in v.topics]
            if on_topic:
                return max(on_topic, key=lambda v: (v.last_prompt, v.created)).lease_id
        mention = [v for v in views if v.platform == "claude-code" and v.mentions and v.wake_capable]
        if mention:
            return max(mention, key=lambda v: (v.last_prompt, v.created)).lease_id
        return None

    def _hand_to_lease(self, runner: SeatRunner, pending: W.Pending, lease_id: str) -> None:
        """The triggers go to the lease that took over, one row per id:  ids the lease already
        holds are skipped, the grace period starts now, and the seat-inbox rows are marked
        delivered_to the lease.  Rows are read from the seat inbox and its rotated copy."""
        now = self.clock.time()
        present = {i.get("id") for i in L.read_jsonl(runner.paths.live_inbox(lease_id)) if i.get("kind") == "message"}
        wanted = [i for i in pending.trigger_ids if i not in present]
        found = self._seat_inbox_rows(runner, wanted)
        rows = []
        for message_id in wanted:
            if message_id in found:
                row = {k: v for k, v in found[message_id].items()
                       if k not in ("seq", "class", "delivered_to", "released_from", "returned_from")}
                row.update({"class": "interrupt", "kind": "message", "routed_at": now, "handed_from": pending.wake_id})
                rows.append(row)
        L.append_live(runner.paths, lease_id, rows)
        self._mark_seat_inbox(runner, [r["id"] for r in rows], lease_id)

    def _seat_inbox_rows(self, runner: SeatRunner, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """The newest seat-inbox message row for each wanted id:  the current file first, then the
        rotated copy only for ids still missing, so a row in the current file always wins."""
        wanted = {i for i in ids if isinstance(i, int)}
        found: dict[int, dict[str, Any]] = {}
        for path in (runner.paths.seat_inbox, runner.paths.seat_inbox + ".1"):
            missing = wanted - set(found)
            if not missing:
                break
            newest: dict[int, dict[str, Any]] = {}
            for row in L.read_jsonl(path):
                message_id = row.get("id")
                if isinstance(message_id, int) and message_id in missing \
                        and row.get("kind", "message") == "message" and not row.get("quarantined"):
                    newest[message_id] = row  # a later line (a requeued copy) wins
            found.update(newest)
        return found

    def _mark_seat_inbox(self, runner: SeatRunner, ids: list[int], lease_id: str) -> None:
        """Mark seat-inbox rows delivered_to a lease (an atomic rewrite under the inbox lock)."""
        if not ids:
            return
        wanted = set(ids)
        with L.locked(runner.paths.seat_inbox_meta):
            rows = L.read_jsonl(runner.paths.seat_inbox)
            if not any(r.get("id") in wanted and not r.get("delivered_to") for r in rows):
                return
            for row in rows:
                if row.get("id") in wanted and not row.get("delivered_to"):
                    row["delivered_to"] = lease_id
            tmp = runner.paths.seat_inbox + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            os.replace(tmp, runner.paths.seat_inbox)

    def _track_child(self, proc: subprocess.Popen[Any]) -> None:
        """A wake child started.  Under the lock, so a child either lands in shutdown's snapshot
        or sees the stop flag here and is killed at once."""
        with self.child_lock:
            self.children.add(proc)
            stopping = self.stop.is_set()
        if stopping:
            _kill_group(proc)

    def _untrack_child(self, proc: subprocess.Popen[Any]) -> None:
        with self.child_lock:
            self.children.discard(proc)

    def run_jobs(self, seat: str, *, limit: int | None = None) -> int:
        """Run queued wakes of one seat on this thread (tests, and the worker loop)."""
        runner = self.seats[seat]
        count = 0
        while limit is None or count < limit:
            with runner.jobs_cond:
                if not runner.jobs:
                    break
                pending = runner.jobs.popleft()
                runner.running = pending
            try:
                self.run_wake(runner, pending)
            finally:
                runner.running = None
            count += 1
        return count

    def history(self, runner: SeatRunner, pending: W.Pending) -> list[dict[str, Any]]:
        if pending.type == "private":
            others = [i for i in pending.recipients if i != (runner.me.user_id if runner.me else None)]
            narrow: list[dict[str, Any]] = [{"operator": "dm", "operand": others}]
        else:
            narrow = [{"operator": "channel", "operand": pending.channel}, {"operator": "topic", "operand": pending.topic}]
        return sorted(runner.fetch(narrow, anchor="newest", before=W.PROMPT_HISTORY), key=lambda m: m.get("id", 0))

    def owner_of(self, message: Mapping[str, Any]) -> bool:
        return (self.config.owner_user_id > 0 and message.get("sender_id") == self.config.owner_user_id
                and str(message.get("client") or "") in self.config.owner_clients)

    def run_wake(self, runner: SeatRunner, pending: W.Pending) -> None:
        now = self.clock.time()
        lease_id = self.takeover(runner, pending)
        if lease_id:
            self._hand_to_lease(runner, pending, lease_id)
            runner.ledger.append(pending.row("skipped", now, reason="leased", lease=lease_id))
            return
        # Everything is checked again just before the run:  the pause, the pin, the loop guard,
        # staleness, and the budgets with every other accepted wake reserved at its maximum.
        reason = self._wake_block(runner, pending, now)
        if reason:
            self._drop(runner, pending, reason, now)
            return
        if runner.cfg.wake == "http":
            self.run_routine(runner, pending, now)
            return
        claude, why = self._pinned_claude(runner)
        if claude is None:
            self._drop(runner, pending, why or "not_pinned", now)
            return
        try:
            history = self.history(runner, pending)
        except Z.AgentSyncError as exc:
            runner.ledger.append(pending.row("failed", now, reason="history: %s" % self.scrub(str(exc)), cost_usd=0.0))
            return
        board_on = runner.cfg.budget.get("board_per_day", 0) > 0
        users = runner.users
        prompt = W.build_prompt(seat=runner.seat, pending=pending, history=history,
                                owner_user_id=self.config.owner_user_id,
                                is_bot=lambda uid: bool(users.get(uid, {}).get("is_bot", True)),
                                owner_of=self.owner_of, format_time=format_time, board_enabled=board_on,
                                route=self.route_gate(runner, pending))
        runner.ledger.append(pending.row("started", now, reserved_usd=C.WAKE_MAX_BUDGET_USD, adapter="claude"))
        argv = A.claude_argv(claude, runner.cfg.model, self.fleet_tags)
        result = self.claude_runner.run(argv, A.claude_env(self.env, self.home, self.wake_path, seat=runner.seat),
                                        runner.paths.wake_dir, prompt)
        self.log.write("wake-run", seat=runner.seat, wake_id=pending.wake_id, exit=result.exit, secs=result.secs,
                       cost=result.cost_usd, refused=result.refused, error=result.error, models=result.models,
                       stdout_len=result.stdout_len, stderr_len=result.stderr_len, stderr_sha256=result.stderr_sha)
        done = self.clock.time()
        if result.refused or result.error:
            runner.ledger.append(pending.row("failed", done, reason=result.refused or result.error,
                                             cost_usd=result.cost_usd, exit=result.exit, secs=result.secs))
            if result.refused or pending.owner:
                self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                     "a wake %s: %s" % ("was refused" if result.refused else "failed",
                                                        A.clean_banner(result.refused or result.error or "", 60)),
                                     kind="wake-failed", key="wake-failed:%s" % pending.wake_id, once_per=0)
            return
        obj, why = W.validate(result.parsed)
        if why:
            self.log.write("wake-invalid", seat=runner.seat, wake_id=pending.wake_id, reason=why)
        outcome = self.act(runner, pending, obj, history=history)
        runner.ledger.append(pending.row("done", self.clock.time(), cost_usd=result.cost_usd, exit=result.exit,
                                         secs=result.secs, action=obj["action"], risk=obj.get("risk"),
                                         coerced=obj.get("coerced"), invalid=why, **outcome))

    def trigger_row(self, runner: SeatRunner, pending: W.Pending) -> dict[str, Any] | None:
        """The trigger a routine wake is about:  the newest owner trigger when the owner is among
        them, else the newest trigger.  Read from the seat inbox (and its rotated copy); fetched
        from Zulip and classified again only when both have rotated past it."""
        ids = pending.owner_ids or pending.trigger_ids
        if not ids:
            return None
        message_id = ids[-1]
        row = self._seat_inbox_rows(runner, [message_id]).get(message_id)
        if row is not None:
            return row
        if runner.client is None or runner.me is None:
            return None
        message = runner.client.get("messages/%d" % message_id, {"apply_markdown": False}).get("message") or {}
        if not message:
            return None
        c, d = R.route(message, runner.me, self.context(runner), [])
        return self.make_item(runner, message, c, d)

    def run_routine(self, runner: SeatRunner, pending: W.Pending, now: float) -> None:
        """The `http` wake:  one fixed JSON body to the seat's routine (a Grok Bot routine
        webhook).  The routine replies in Zulip itself; the daemon posts nothing for this seat."""
        target, why, detail = self.routine(runner)
        if target is None:
            self.log.write("routine-unready", seat=runner.seat, wake_id=pending.wake_id, reason=why, detail=detail)
            self._drop(runner, pending, why or "routine_not_configured", now)
            return
        cost = runner.cfg.wake_max_usd
        try:
            row = self.trigger_row(runner, pending)
        except Z.AgentSyncError as exc:
            runner.ledger.append(pending.row("failed", now, reason="trigger: %s" % self.scrub(str(exc)), cost_usd=0.0,
                                             adapter="http"))
            return
        if row is None or not isinstance(row.get("id"), int):
            runner.ledger.append(pending.row("failed", now, reason="trigger not found", cost_usd=0.0, adapter="http"))
            return
        body = A.routine_body(seat=runner.seat, wake_id=pending.wake_id, trigger_ids=pending.trigger_ids, row=row,
                              bot_user_id=runner.me.user_id if runner.me else None, realm=self.realm, now=now,
                              scrub=self.scrub)
        data = A.routine_bytes(body)
        runner.ledger.append(pending.row("started", now, reserved_usd=cost, adapter="http", message_id=row["id"],
                                         dm=body["dm"]))
        result = self.routine_runner.deliver(target, data, idempotency_key=pending.wake_id)
        self.log.write("wake-routine", seat=runner.seat, wake_id=pending.wake_id, host=target.host,
                       method=target.method, auth=target.auth, accepted=result.accepted, status=result.status,
                       attempts=result.attempts, history=result.history, secs=result.secs, error=result.error,
                       body_len=len(data), response_len=result.response_len, response_sha256=result.response_sha)
        done = self.clock.time()
        if result.accepted:
            runner.ledger.append(pending.row("done", done, cost_usd=cost, adapter="http", action="routine",
                                             http_status=result.status, attempts=result.attempts, secs=result.secs,
                                             message_id=row["id"], dm=body["dm"]))
            return
        reason = self.scrub(result.error or "not accepted")
        runner.ledger.append(pending.row("failed", done, reason=reason, cost_usd=cost, adapter="http",
                                         http_status=result.status, attempts=result.attempts, secs=result.secs,
                                         message_id=row["id"], dm=body["dm"]))
        status = result.status
        if status is not None and 400 <= status < 500:
            # A 4xx is a contract or key problem:  say so once a day, whoever triggered it.
            self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                 "the %s routine refused a wake (HTTP %d); check the routine URL, key and format"
                                 % (runner.seat, status), kind="routine-refused", key="routine:%s:%d" % (runner.seat, status))
        if pending.owner:
            self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                 "a wake for your message did not reach the %s routine (%s); it is in the seat inbox"
                                 % (runner.seat, A.clean_banner(reason, 60)),
                                 kind="wake-failed", key="wake-failed:%s" % pending.wake_id, once_per=0)

    def act(self, runner: SeatRunner, pending: W.Pending, obj: Mapping[str, Any], *,
            history: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """Carry out a validated result:  post the reply (mentions neutralized, secret-scanned,
        tagged), file the board item when allowed, and send owner notes through notify-owner.  A
        note about a peer request, or any request screened uncertain or high, is also sent to the
        owner as a Zulip DM from the seat's own bot (`dm_owner`)."""
        outcome: dict[str, Any] = {"posted_id": None, "board_uid": None, "note": False}
        trigger = pending.trigger_ids[-1] if pending.trigger_ids else 0
        reply = obj.get("reply") if obj.get("action") in ("reply", "board", "escalate") else None
        lease_id = self.takeover(runner, pending)
        if lease_id and reply:
            source = self._seat_inbox_rows(runner, [trigger]).get(trigger) or {}
            L.append_live(runner.paths, lease_id, [{
                "kind": "draft", "class": "passive", "id": trigger, "channel": pending.channel, "topic": pending.topic,
                "type": pending.type, "sender": "%s wake (draft, not posted)" % runner.seat, "ts": self.clock.time(),
                "owner": False, "trigger_owner": pending.owner, "trigger_sender_id": source.get("sender_id"),
                "content": reply}])
            outcome["draft_to"] = lease_id
            reply = None
        if reply:
            text = W.compose_reply(runner.seat, trigger, reply)
            found = secretscan.scan(text, self.hidden)
            if W.loud_mention(text):  # never reached after neutralize_mentions; refuse rather than notify
                outcome["refused"] = "a mention survived neutralizing"
            elif found:
                outcome["refused"] = "secret scanner: %s" % found
                self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat, "a wake reply was refused by the "
                                     "secret scanner", kind="wake-secret", key="secret:%s" % pending.wake_id, once_per=0)
            else:
                outcome["posted_id"] = self.post(runner, pending, text)
        board = obj.get("board")
        if obj.get("action") == "board" and isinstance(board, dict):
            outcome.update(self.file_board(runner, pending, board, trigger))
        note = obj.get("owner_note")
        if obj.get("action") == "escalate" or (isinstance(note, str) and note.strip()):
            outcome["note"] = True
            where = "a DM" if pending.type == "private" else "#%s > %s" % (pending.channel, pending.topic)
            risk = obj.get("risk") if obj.get("risk") in W.RISKS else None
            self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat, "a note for you about %s" % where,
                                 kind="owner_note", key="note:%s" % pending.wake_id, once_per=0,
                                 note=note if isinstance(note, str) else None, trigger_ids=pending.trigger_ids,
                                 owner=pending.owner, risk=risk)
            if not pending.owner or risk in ("uncertain", "high"):
                dm_note = note if isinstance(note, str) else ""
                route = obj.get("route")
                gate = self.route_gate(runner, pending) if isinstance(route, dict) else None
                if gate:  # a route that can never pass (a peer-only batch, a DM) is named in the note too
                    dm_note = ("%s  (Route to %s dropped: %s.)" % (dm_note.strip(), route.get("seat"), gate)).strip()
                outcome.update(self.dm_owner(runner, pending, dm_note, risk, history))
            if pending.owner and not outcome["posted_id"] and not lease_id:
                outcome["posted_id"] = self.post(runner, pending, W.compose_reply(runner.seat, trigger, W.OWNER_ACK))
        # The route comes last, after the reply:  two posts, because a `·wake`-tagged post never wakes
        # anyone and the route post must wake its target.
        route = obj.get("route")
        if isinstance(route, dict):
            outcome["route"] = self.handle_route(runner, pending, obj, route, history=history, lease_id=lease_id)
        elif obj.get("route_invalid"):
            outcome["route"] = {"seat": None, "status": "dropped", "why": str(obj["route_invalid"])}
            self.note_route_dropped(runner, pending, None, outcome["route"]["why"])
        return outcome

    # ---- route suggestions (owner 2026-10-10) ---------------------------------------------------
    def route_gate(self, runner: SeatRunner, pending: W.Pending) -> str | None:
        """Why this wake may not route at all, or None.  Known before the run, so the prompt header
        tells the model, and checked again after it.  Only an owner trigger (Jay, from a human Zulip
        app) in a channel topic can route:  a peer-only batch never can, so no peer fans out through
        the seat, and a DM never can."""
        if not (self.config.route_enabled and runner.cfg.route_enabled):
            return "disabled"
        if pending.type == "private":
            return "dm_trigger"
        if not pending.owner_ids:
            return "no_owner_trigger"
        if pending.route_tagged:
            return "route_trigger"  # a `·route` post never leads to another route
        return None

    def route_mode(self, seat: str) -> tuple[str | None, str | None]:
        """(how the target hears the page, why it cannot).  A seat this listener holds:  live session
        (a live lease), headless wake, routine wake, or inbox only.  A seat the other listener
        instance holds:  that listener (its config is not visible here; it captures the page in the
        seat inbox at least).  A seat no listener holds (`none` in the partition, or not listed) has
        no wake path the daemon can vouch for, so it is refused."""
        cfg = self.config.seats.get(seat)
        if cfg is not None:
            target = self.seats.get(seat)
            if target is not None and self.lease_views(target):
                return "live session", None
            return {"claude": "headless wake", "http": "routine wake"}.get(cfg.wake, "inbox only"), None
        assigned = self.partition.get(seat)
        if assigned in C.INSTANCES and assigned != self.config.instance:
            return "%s listener" % assigned, None
        if assigned == self.config.instance:
            return None, "not_held"  # this instance's seat, but its config holds no queue for it
        return None, "no_listener"

    def trigger_messages(self, runner: SeatRunner, pending: W.Pending,
                         history: Iterable[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
        """Trigger id -> its message (content and sender), from the seat inbox first, then history."""
        found: dict[int, Mapping[str, Any]] = dict(self._seat_inbox_rows(runner, pending.trigger_ids))
        for message in history:
            if isinstance(message, Mapping) and message.get("id") in pending.trigger_ids:
                found.setdefault(int(message["id"]), message)
        return found

    def route_check(self, runner: SeatRunner, pending: W.Pending, obj: Mapping[str, Any], seat: str,
                    triggers: Mapping[int, Mapping[str, Any]], lease_id: str | None,
                    ) -> tuple[str | None, dict[str, Any] | None, str | None]:
        """(why the route is dropped, the target bot, how it hears the page).  Deterministic:  the gate,
        no live session took the wake over, no risky screen, a known fleet seat that is not this seat,
        an active fleet bot, a wake path, not a trigger's sender, not already @-mentioned in a trigger,
        and the route budget."""
        gate = self.route_gate(runner, pending)
        if gate:
            return gate, None, None
        if lease_id:
            return "live_session", None, None  # the session holds the draft and can page the seat itself
        if obj.get("risk") in ("high", "uncertain"):
            return "risk_screened", None, None
        if seat not in self.fleet_tags:
            return "unknown_seat", None, None
        if seat == runner.seat:
            return "self", None, None
        bots = R.fleet_bots_by_seat(runner.users, self.fleet_tags, self.config.owner_user_id).get(seat) or []
        if not bots:
            return "bot_inactive", None, None
        mode, why = self.route_mode(seat)
        if why:
            return why, None, None
        if any(i not in triggers for i in pending.trigger_ids):
            return "trigger_unreadable", None, None
        bot_ids = {int(b["user_id"]) for b in bots}
        for message in triggers.values():
            if message.get("sender_id") in bot_ids:
                return "sender", None, None
            visible = R.outside_code(str(message.get("content") or "")).casefold()  # Zulip matches names without case
            for bot in bots:
                name = str(bot.get("full_name") or "").casefold()
                if name and ("@**%s**" % name in visible or "@**%s|%d**" % (name, int(bot["user_id"])) in visible):
                    return "already_mentioned", None, None
        block = W.route_budget_block(runner.ledger.wakes(), runner.cfg.budget, key=pending.key, now=self.clock.time())
        if block:
            return block, None, None
        return None, bots[0], mode

    def handle_route(self, runner: SeatRunner, pending: W.Pending, obj: Mapping[str, Any], route: Mapping[str, Any],
                     *, history: Iterable[Mapping[str, Any]], lease_id: str | None) -> dict[str, Any]:
        """Check a route suggestion and, when it passes, post `[SEAT·route→TARGET]` in the trigger's
        topic with one live mention (the daemon's, of the target bot), then tell the owner who was
        paged and why.  A dropped route is recorded and reaches the owner as a note."""
        seat = str(route.get("seat") or "").upper()
        reason = str(route.get("reason") or "")
        info: dict[str, Any] = {"seat": seat, "status": "dropped", "why": None}
        history = list(history)
        triggers = self.trigger_messages(runner, pending, history)
        why, bot, mode = self.route_check(runner, pending, obj, seat, triggers, lease_id)
        if why is None and bot is not None:
            owner_trigger = pending.owner_ids[-1]
            owner_text = str((triggers.get(owner_trigger) or {}).get("content") or "")
            link = A.zulip_link(self.realm, {"id": owner_trigger, "type": pending.type, "recipients": pending.recipients,
                                             "channel": pending.channel, "topic": pending.topic,
                                             "stream_id": pending.stream_id})
            text = W.compose_route(runner.seat, seat, str(bot.get("full_name") or seat), int(bot["user_id"]),
                                   owner_trigger, reason, owner_text, link)
            found = secretscan.scan(text, self.hidden)
            if W.loud_mentions(text) != 1:
                why = "mention_check"  # never reached:  the composer neutralizes every other mention
            elif found:
                why = "secret_scanner"
            else:
                posted = self.post(runner, pending, text)
                if posted is None:
                    info.update(status="failed", why="post_failed", mode=mode)
                    self.log.write("route-failed", seat=runner.seat, wake_id=pending.wake_id, target=seat)
                    return info
                info.update(status="posted", why=None, mode=mode, posted_id=posted, target_id=int(bot["user_id"]),
                            re=owner_trigger)
                self.log.write("route-posted", seat=runner.seat, wake_id=pending.wake_id, target=seat, mode=mode,
                               posted_id=posted)
                info.update(self.note_route_posted(runner, pending, seat, mode or "", reason, owner_trigger, posted))
                return info
        info["why"] = why
        self.log.write("route-dropped", seat=runner.seat, wake_id=pending.wake_id, target=seat, reason=why)
        self.note_route_dropped(runner, pending, seat, why or "dropped")
        return info

    def note_route_dropped(self, runner: SeatRunner, pending: W.Pending, seat: str | None, why: str) -> None:
        """A dropped route reaches the owner queue (and a Mac banner) as a note, never as a Zulip post."""
        where = "a DM" if pending.type == "private" else "#%s > %s" % (pending.channel, pending.topic)
        target = A.clean_banner(seat or "an invalid route", 40)
        self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                             "a route to %s was dropped (%s) in %s" % (target, A.clean_banner(why, 40), where),
                             kind="route_dropped", key="route:%s" % pending.wake_id, once_per=0,
                             note="route to %s dropped: %s" % (target, A.clean_banner(why, 60)),
                             trigger_ids=pending.trigger_ids, owner=pending.owner)

    def note_route_posted(self, runner: SeatRunner, pending: W.Pending, seat: str, mode: str, reason: str,
                          owner_trigger: int, posted: int) -> dict[str, Any]:
        """Tell the owner who was paged and why:  a `[SEAT·note]` DM from the seat's own bot (the same
        channel as escalation notes) and the owner queue.  Never raises; a failure is recorded."""
        where = "#%s > %s" % (A.clean_banner(pending.channel, 60), A.clean_banner(pending.topic, 80))
        self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat, "paged %s for you in %s" % (seat, where),
                             kind="route", key="route:%s" % pending.wake_id, once_per=0,
                             note="routed to %s (%s): %s" % (seat, mode, A.clean_banner(reason, W.LIMITS["route_reason"])),
                             trigger_ids=pending.trigger_ids, owner=pending.owner)
        owner_id = self.config.owner_user_id
        if owner_id <= 0 or runner.client is None or runner.fatal:
            return {"owner_dm": None, "owner_dm_error": "owner dm skipped"}
        link = A.zulip_link(self.realm, {"id": posted, "type": pending.type, "recipients": pending.recipients,
                                         "channel": pending.channel, "topic": pending.topic,
                                         "stream_id": pending.stream_id})
        text = W.neutralize_mentions("\n".join([
            "[%s\u00b7note] re=%d" % (runner.seat, owner_trigger),
            "Routed: %s (%s)" % (seat, mode),
            "Where: %s" % where,
            "```quote",
            A.clean_banner(reason.replace("`", "'"), W.LIMITS["route_reason"]),
            "```",
            "Route post: %s" % link,
        ]))
        if secretscan.scan(text, self.hidden):
            self.log.write("owner-dm-refused", seat=runner.seat, wake_id=pending.wake_id, reason="secret scanner")
            return {"owner_dm": None, "owner_dm_error": "secret scanner"}
        wait = POST_SPACING - (self.clock.monotonic() - runner.last_post)
        if wait > 0:
            self.clock.sleep(wait)
        try:
            result = runner.client.post("messages", {"type": "direct", "to": [owner_id], "content": text})
        except Z.AgentSyncError as exc:
            reason_text = self.scrub(str(exc))
            self.log.write("owner-dm-failed", seat=runner.seat, wake_id=pending.wake_id, error=reason_text)
            return {"owner_dm": None, "owner_dm_error": reason_text}
        finally:
            runner.last_post = self.clock.monotonic()
        message_id = result.get("id")
        return {"owner_dm": int(message_id) if isinstance(message_id, int) else None}

    def owner_dm_text(self, runner: SeatRunner, pending: W.Pending, note: str, risk: str | None,
                      history: Iterable[Mapping[str, Any]]) -> str:
        """The owner DM for an escalated request:  who asked, where, the risk, the responder's note
        and a link to the trigger.  Sender, place and link are the daemon's own fields; the note is
        model output derived from untrusted text, so it sits in a quote block with backticks and
        mentions removed.  The tag is `[SEAT·note]`, not `·wake`, so the router never takes the DM for
        a wake reply."""
        peers = [i for i in pending.trigger_ids if i not in pending.owner_ids]
        trigger = (peers or pending.trigger_ids or [0])[-1]
        senders = {m.get("id"): m for m in history if isinstance(m, Mapping)}
        sender = senders.get(trigger) or self._seat_inbox_rows(runner, [trigger]).get(trigger) or {}
        name = A.clean_banner(str(sender.get("sender_full_name") or sender.get("sender") or "a peer"), 60)
        sender_id = sender.get("sender_id")
        bot = runner.users.get(sender_id, {}).get("is_bot") if isinstance(sender_id, int) else None
        where = "a DM" if pending.type == "private" else "#%s > %s" % (
            A.clean_banner(pending.channel, 60), A.clean_banner(pending.topic, 80))
        link = A.zulip_link(self.realm, {"id": trigger, "type": pending.type, "recipients": pending.recipients,
                                         "channel": pending.channel, "topic": pending.topic,
                                         "stream_id": pending.stream_id})
        quoted = A.clean_banner(note.replace("`", "'"), W.LIMITS["owner_note"]) if note.strip() else "(no note)"
        text = "\n".join([
            "[%s\u00b7note] re=%d" % (runner.seat, trigger),
            "From: %s%s" % (name, " (bot)" if bot else ""),
            "Where: %s" % where,
            "Risk: %s" % (risk or "not screened"),
            "```quote",
            quoted,
            "```",
            "Message: %s" % link,
        ])
        return W.neutralize_mentions(text)

    def dm_owner(self, runner: SeatRunner, pending: W.Pending, note: str, risk: str | None,
                 history: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Send the owner a Zulip DM as the seat's own bot.  It rides on a wake that already passed
        the seat's budgets, so it is at most one DM per wake, and it never raises:  a refusal or a
        failure is logged and recorded in the ledger row, and the banner and owner queue still hold
        the note."""
        owner_id = self.config.owner_user_id
        if owner_id <= 0 or runner.client is None or runner.fatal:
            self.log.write("owner-dm-skipped", seat=runner.seat, wake_id=pending.wake_id,
                           reason="no_owner_id" if owner_id <= 0 else "seat_unavailable")
            return {"owner_dm": None, "owner_dm_error": "owner dm skipped"}
        text = self.owner_dm_text(runner, pending, note, risk, history)
        found = secretscan.scan(text, self.hidden)
        withheld = None
        if found:
            # Every decline must reach the owner, so a note the scanner flags is withheld, not the DM.
            withheld = "secret scanner: %s" % found
            self.log.write("owner-dm-note-withheld", seat=runner.seat, wake_id=pending.wake_id, reason=withheld)
            text = self.owner_dm_text(runner, pending, "(withheld by the secret scanner; it is in the owner queue)",
                                      risk, history)
            if secretscan.scan(text, self.hidden):  # the sender or place was the problem:  say nothing at all
                self.log.write("owner-dm-refused", seat=runner.seat, wake_id=pending.wake_id, reason="secret scanner")
                return {"owner_dm": None, "owner_dm_error": withheld}
        wait = POST_SPACING - (self.clock.monotonic() - runner.last_post)
        if wait > 0:
            self.clock.sleep(wait)
        try:
            result = runner.client.post("messages", {"type": "direct", "to": [owner_id], "content": text})
        except Z.AgentSyncError as exc:
            reason = self.scrub(str(exc))
            self.log.write("owner-dm-failed", seat=runner.seat, wake_id=pending.wake_id, error=reason)
            return {"owner_dm": None, "owner_dm_error": reason}
        finally:
            runner.last_post = self.clock.monotonic()
        message_id = result.get("id")
        sent: dict[str, Any] = {"owner_dm": int(message_id) if isinstance(message_id, int) else None}
        if withheld:
            sent["owner_dm_note_withheld"] = withheld
        return sent

    def post(self, runner: SeatRunner, pending: W.Pending, text: str) -> int | None:
        """A channel trigger is answered in its topic; a DM trigger only by DM to the original
        recipient set, never in a channel.  Posts are about 3 seconds apart."""
        assert runner.client is not None and runner.me is not None
        if runner.fatal:
            self.log.write("post-refused", seat=runner.seat, wake_id=pending.wake_id, reason="seat_refused")
            return None
        wait = POST_SPACING - (self.clock.monotonic() - runner.last_post)
        if wait > 0:
            self.clock.sleep(wait)
        try:
            if pending.type == "private":
                to = [i for i in pending.recipients if isinstance(i, int) and i != runner.me.user_id]
                if not to:
                    return None
                result = runner.client.post("messages", {"type": "direct", "to": to, "content": text})
            else:
                result = runner.client.post("messages", {"type": "stream", "to": pending.channel,
                                                         "topic": pending.topic, "content": text})
        except Z.AgentSyncError as exc:
            self.log.write("post-failed", seat=runner.seat, wake_id=pending.wake_id, error=self.scrub(str(exc)))
            return None
        finally:
            runner.last_post = self.clock.monotonic()
        message_id = result.get("id")
        return int(message_id) if isinstance(message_id, int) else None

    def file_board(self, runner: SeatRunner, pending: W.Pending, board: Mapping[str, Any], trigger: int) -> dict[str, Any]:
        block = W.board_block(runner.ledger.wakes(), runner.cfg.budget, self.clock.time())
        if block:
            return {"board_skipped": block}
        app = A.board_app(pending.topic, self.acronyms)
        url = "%s/#narrow/channel/%s/near/%d" % (self.realm, int(pending.stream_id or 0), int(trigger))
        argv = A.board_argv(self.board_bin, seat=runner.seat, title=str(board["title"]), desc=str(board["desc"]),
                            severity=str(board["severity"]), app=app, trigger_id=trigger, url=url)
        ok, detail = A.run_board(argv, A.claude_env(self.env, self.home, self.wake_path), self.board_runner)
        return {"board_uid": "zulip:%s:%d" % (runner.seat, trigger) if ok else None, "board_detail": detail,
                "board": dict(board) if ok else None}

    # ---- periodic work -------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock.time()
        if self.reload_requested:
            self.reload_requested = False
            self.reload()
        for pending in self.coalescer.take_due(now):
            self.finalize(pending)
        if now - self.last_tick["reap"] >= 10:
            self.last_tick["reap"] = now
            for runner in self.seats.values():
                self.reap(runner)
        if now - self.last_tick["grace"] >= 30:
            self.last_tick["grace"] = now
            for runner in self.seats.values():
                self.release_grace(runner)
        if now - self.last_tick["prune"] >= W.DAY:
            self.last_tick["prune"] = now
            for runner in self.seats.values():
                runner.ledger.prune(now)
        for runner in self.seats.values():
            runner.flush_cursor()
        if now - self.last_tick["status"] >= 2:
            self.last_tick["status"] = now
            self.write_status()
        try:
            self.heartbeat.tick(self.status)
        except Exception as exc:  # noqa: BLE001 - a monitoring report must never stop the router
            self.heartbeat.next_at = now + 60.0
            self.log.write("sentry-heartbeat-error", error=self.scrub(type(exc).__name__))

    def _requeue(self, runner: SeatRunner, items: list[dict[str, Any]], source: str, lease_id: str) -> None:
        """Items a dead or silent lease never took go back through steps 6 and 7."""
        rows = []
        for item in items:
            if item.get("kind", "message") != "message":
                continue
            classes = set(item.get("classes") or [])
            key = L.topic_key(str(item.get("channel") or ""), str(item.get("topic") or ""))
            if not (classes & {"direct", "dm", "fleet", "owner", "wildcard"}) and key not in runner.followed:
                continue
            row = {k: v for k, v in item.items() if k not in ("seq", "class")}
            row[source] = lease_id
            rows.append(row)
        if rows:
            self._append_seat_inbox(runner, rows)
        for row in rows:
            if row.get("wake_worthy"):
                self.offer_wake(runner, row, owner=bool(row.get("wake_owner")))

    def reap(self, runner: SeatRunner) -> None:
        now = self.clock.time()
        ids = set(L.list_lease_ids(runner.paths)) | set(L.iter_live_dirs(runner.paths))
        for lease_id in sorted(ids):
            live_dir = runner.paths.live_dir(lease_id)
            returned = os.path.join(live_dir, "returned.json")
            lease = L.load_lease(runner.paths, lease_id)
            if os.path.exists(returned):
                at = L.read_json(returned).get("ts")
                created = lease.get("created")
                if lease and (not isinstance(at, (int, float)) or
                              (isinstance(created, (int, float)) and float(created) > float(at))):
                    # The lease was written again after it was reaped (a re-attach with the same
                    # id):  the old marker no longer applies.
                    try:
                        os.unlink(returned)
                    except OSError:
                        pass
                else:
                    if not lease and isinstance(at, (int, float)) and now - at > LIVE_DIR_KEEP:
                        shutil.rmtree(live_dir, ignore_errors=True)
                    continue
            alive, why = self.lease_alive(runner.paths, lease)
            if alive:
                continue
            with L.locked(runner.paths.lease(lease_id)):
                # A hook may have written the lease again since it was read (SessionStart after
                # /clear, a re-attach):  leave it for the next round, which sees the new file.
                if L.load_lease(runner.paths, lease_id) != lease:
                    continue
                if lease and why == "ended" and not os.path.exists(os.path.join(live_dir, "ended.json")):
                    continue
                items = L.release(runner.paths, lease_id, field="returned")
                L.write_json(returned, {"ts": now, "reason": why, "items": len(items)})
                try:
                    os.unlink(runner.paths.lease(lease_id))
                except FileNotFoundError:
                    pass
            self._requeue(runner, items, "returned_from", lease_id)
            self.log.write("lease-reaped", seat=runner.seat, lease=lease_id, reason=why, returned=len(items))

    def release_grace(self, runner: SeatRunner) -> None:
        """A wake-worthy item a lease that cannot wake has not taken in 10 minutes is unowned."""
        now = self.clock.time()
        capable = {v.lease_id for v in self.lease_views(runner) if v.wake_capable}
        for lease_id in L.list_lease_ids(runner.paths):
            if lease_id in capable:
                continue
            def due(item: Mapping[str, Any]) -> bool:
                return (item.get("class") == "interrupt" and bool(item.get("wake_worthy"))
                        and isinstance(item.get("routed_at"), (int, float)) and now - float(item["routed_at"]) >= GRACE_SECONDS)

            released = L.release(runner.paths, lease_id, select=due, field="released")
            if released:
                self._requeue(runner, released, "released_from", lease_id)
                self.log.write("grace-released", seat=runner.seat, lease=lease_id, items=len(released))

    # ---- pause ---------------------------------------------------------------------------------
    def pause(self, *, seat: str | None, wakes_only: bool, by: str, message_id: int | None = None) -> None:
        set_pause(self.root, seat, wakes_only=wakes_only, by=by, now=self.clock.time(), message_id=message_id)
        self.log.write("paused", seat=seat or "*", wakes_only=wakes_only, by=by)
        self.notifier.notify(self.config.claude_seat or (seat or "LISTENER"), "agent-sync listener",
                             "paused by %s; resume with agent-sync daemon resume" % by, kind="paused",
                             key="paused:%s" % (message_id or by), once_per=0)

    # ---- status --------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        now = self.clock.time()
        seats: dict[str, Any] = {}
        for seat, runner in self.seats.items():
            views = runner.ledger.wakes()
            day = [v for v in views.values() if isinstance((v.get("times") or {}).get("started"), (int, float))
                   and now - v["times"]["started"] < W.DAY]
            claude = A.resolve_claude(runner.cfg.claude, self.wake_path) if runner.cfg.wake == "claude" else None
            routine = None
            if runner.cfg.wake == "http":
                target, why, detail = self.routine(runner)
                routine = {"ready": target is not None, "why": why, "detail": self.scrub(detail),
                           "host": target.host if target else None, "method": runner.cfg.routine.method
                           if runner.cfg.routine else None, "auth": runner.cfg.routine.auth if runner.cfg.routine else None}
            leases = []
            for lease_id in L.list_lease_ids(runner.paths):
                lease = L.load_lease(runner.paths, lease_id)
                alive, why = self.lease_alive(runner.paths, lease)
                leases.append({"lease_id": lease_id, "platform": lease.get("platform"), "alive": alive, "why": why,
                               "topics": len(L.lease_topics(lease, now)),
                               "watcher": L.watcher_alive(runner.paths, lease_id),
                               "pending": len(L.pending_items(runner.paths, lease_id))})
            red = []
            if runner.error:
                red.append(runner.error)
            if runner.down_since is not None and now - runner.down_since > DOWN_NOTIFY_SECONDS:
                red.append("down for %d minutes" % ((now - runner.down_since) // 60))
            seats[seat] = {
                "bot": runner.cfg.bot, "creds": runner.cfg.creds, "connected": runner.connected, "cursor": runner.cursor,
                "last_event": runner.last_event_wall, "wake": runner.cfg.wake, "routine": routine,
                "pinned": A.pinned(runner.paths, claude) if claude else None, "claude": claude,
                "wakes_24h": len(day), "cost_24h": W.spent_today(views.values(), now),
                "routes_24h": W.routes_in(views.values(), now),
                "route_enabled": bool(self.config.route_enabled and runner.cfg.route_enabled),
                "usd_per_day": runner.cfg.budget["usd_per_day"], "budget": runner.cfg.budget, "live": runner.cfg.live,
                "jobs": len(runner.jobs), "running": runner.running.wake_id if runner.running else None,
                "paused": L.paused(self.root, seat), "wakes_paused": L.paused(self.root, seat, wakes=True),
                "leases": leases, "red": red}
        return {"pid": os.getpid(), "version": __version__, "started": self.started_at, "updated": now,
                "instance": self.config.instance, "disabled": sorted(self.config.disabled),
                "config_errors": [self.scrub(e) for e in self.config.errors + self.refusal + self.reload_refused],
                "owner_user_id": self.config.owner_user_id,
                "rewake_verified": self.config.rewake_verified, "pause": L.read_json(pause_path(self.root)),
                "seats": seats}

    def write_status(self) -> None:
        try:
            L.write_json(os.path.join(L.listener_dir(self.root), "status.json"), self.status())
        except OSError:
            pass

    # ---- seat threads --------------------------------------------------------------------------
    def connect_seat(self, seat: str) -> bool:
        """Connect one seat and route its backfill on this thread (tests and the poller)."""
        runner = self.seats[seat]
        messages = self._connect(runner)
        if messages is None:
            return False
        self.route_backfill(runner, messages)
        return True

    def _connect(self, runner: SeatRunner) -> list[dict[str, Any]] | None:
        try:
            messages = runner.connect()
        except (Z.CredentialError, RoleRefused) as exc:
            self._refuse(runner, str(exc))
            return None
        except (Z.NetworkError, Z.ApiError, OSError) as exc:
            self._backoff(runner, exc)
            return None
        runner.failures = 0
        runner.down_since = None
        self.log.write("connected", seat=runner.seat, backfill=len(messages))
        return messages

    def _refuse(self, runner: SeatRunner, text: str) -> None:
        """A credential or role problem:  no queue, retried slowly (every 5 minutes), never spun."""
        if runner.connected:
            runner.disconnect()
        runner.connected = False
        runner.fatal = True
        runner.error = self.scrub(text)
        runner.next_attempt = self.clock.monotonic() + 300
        self.log.write("seat-refused", seat=runner.seat, error=runner.error)
        self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat, A.clean_banner(runner.error),
                             kind="seat-refused")

    def _backoff(self, runner: SeatRunner, exc: BaseException) -> None:
        runner.connected = False
        runner.error = self.scrub(str(exc))
        delay = BACKOFF[min(runner.failures, len(BACKOFF) - 1)]
        runner.failures += 1
        runner.next_attempt = self.clock.monotonic() + delay
        if runner.down_since is None:
            runner.down_since = self.clock.time()
        self.log.write("seat-error", seat=runner.seat, error=runner.error, retry=delay)
        if self.clock.time() - runner.down_since > DOWN_NOTIFY_SECONDS:
            self.notifier.notify(runner.seat, "agent-sync %s" % runner.seat,
                                 "the %s queue has been down for 30 minutes" % runner.seat, kind="seat-down")

    def pump(self, seat: str, timeout: float | None = None) -> int:
        """One long poll for a seat, then route what came (tests).  Returns the number of events."""
        runner = self.seats[seat]
        events = self._poll(runner, timeout)
        if events is None:
            return 0
        self.handle_events(runner, events)
        return len(events)

    def _poll(self, runner: SeatRunner, timeout: float | None) -> list[dict[str, Any]] | None:
        if not runner.connected or runner.queue is None:
            return None
        wait = timeout if timeout is not None else (self.events_timeout or (runner.queue.longpoll_timeout or 90.0) + 10.0)
        try:
            events = runner.queue.poll_events(wait)
        except Z.QueueExpired:
            runner.connected = False
            runner.next_attempt = 0.0
            self.log.write("queue-expired", seat=runner.seat)
            return None
        except (Z.NetworkError, Z.ApiError, OSError) as exc:
            self._backoff(runner, exc)
            return None
        if events:
            runner.last_event_wall = self.clock.time()
        offset = self.clock.time() - self.clock.monotonic()
        if runner.wall_offset is not None and abs(offset - runner.wall_offset) > CLOCK_JUMP_SECONDS:
            self.log.write("clock-jump", seat=runner.seat)
            runner.disconnect()
            runner.next_attempt = 0.0
            return events
        last = runner.queue.last_event_at
        if last is not None and self.clock.monotonic() - last > DEAD_QUEUE_SECONDS:
            self.log.write("dead-queue", seat=runner.seat)
            runner.disconnect()
            runner.next_attempt = 0.0
        return events

    def _stopping(self, runner: SeatRunner) -> bool:
        return self.stop.is_set() or runner.stop.is_set()

    def seat_loop(self, runner: SeatRunner) -> None:
        self.stop.wait(random.uniform(0.0, 2.0))  # jitter, so the seats do not register at the same instant
        while not self._stopping(runner):
            if not runner.connected:
                wait = runner.next_attempt - self.clock.monotonic()
                if wait > 0:
                    self.stop.wait(min(wait, 1.0))
                    continue
                messages = self._connect(runner)
                if messages is not None:
                    self.router_queue.put(("backfill", runner, messages))
                continue
            events = self._poll(runner, None)
            if events:
                self.router_queue.put(("events", runner, events))
        if runner.stop.is_set() and runner.connected:
            runner.disconnect()  # removed while a connect was in flight:  delete that queue too

    def worker_loop(self, runner: SeatRunner) -> None:
        while not self._stopping(runner):
            with runner.jobs_cond:
                if not runner.jobs:
                    runner.jobs_cond.wait(timeout=1.0)
                    continue
            self.run_jobs(runner.seat, limit=1)

    def refuse_start(self) -> int:
        """The seat partition failed:  say why on stderr and in the log, notify once a day, and
        exit before the flock, any thread or any register.  Nothing writes status.json, so a
        container health check fails too."""
        for problem in self.refusal:
            self.stderr.write("agent-sync daemon: refusing to start: %s\n" % self.scrub(problem))
        try:
            self.stderr.flush()
        except (OSError, ValueError):
            pass
        self.log.write("refused-start", errors=self.refusal)
        self.notifier.notify(self.config.claude_seat or "LISTENER", "agent-sync listener",
                             "refused to start: the seat partition check failed; see the listener log",
                             kind="partition", key="partition-start")
        return 2

    def run(self, *, wait_lock: bool = False) -> int:
        """Foreground daemon (what launchd and the container run).  Single instance by flock; a
        bad config never exits (it would re-register every queue every 30 seconds under
        KeepAlive), except the seat partition, which refuses to start.  `wait_lock` (the server
        container) waits for a running listener on the same state volume to stop instead of
        exiting, so a rolling redeploy hands over without two queues per bot."""
        import fcntl

        if self.refusal:
            return self.refuse_start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda s, f: self.stop.set())
            signal.signal(signal.SIGINT, lambda s, f: self.stop.set())
            signal.signal(signal.SIGHUP, lambda s, f: setattr(self, "reload_requested", True))
        L.private_dir(L.listener_dir(self.root))
        lock_fd = os.open(self.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        waiting = False
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if waiting and self.stop.is_set():
                    os.close(lock_fd)  # stopped while waiting for the other listener:  nothing to clean up
                    return 0
                if not wait_lock:
                    self.stderr.write("agent-sync daemon: another listener is already running\n")
                    os.close(lock_fd)
                    return 1
                if not waiting:
                    waiting = True
                    self.stderr.write("agent-sync daemon: another listener holds the state directory; waiting for it "
                                      "to stop\n")
                    self.log.write("waiting-for-lock", pid=os.getpid())
                self.stop.wait(1.0)
        if waiting or self.deferred:
            self.rebuild_from_disk()
            if self.refusal:  # the config changed while this listener waited
                os.close(lock_fd)
                return self.refuse_start()
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode())
        self.install_excepthooks()
        self.log.write("started", pid=os.getpid(), seats=sorted(self.seats), errors=len(self.config.errors),
                       instance=self.config.instance)
        try:
            while not self.stop.is_set():
                # Threads belong to a runner object, not a seat name:  a seat removed by a reload
                # keeps its old threads only until they see runner.stop, and a seat added back
                # gets a new runner with fresh threads.
                for seat, runner in list(self.seats.items()):
                    for role, target, wanted in (("poll", self.seat_loop, True),
                                                 ("wake", self.worker_loop, runner.cfg.wake in C.WORKER_WAKES)):
                        thread = runner.threads.get(role)
                        if wanted and (thread is None or not thread.is_alive()):
                            thread = threading.Thread(target=target, args=(runner,), name="%s-%s" % (role, seat),
                                                      daemon=True)
                            runner.threads[role] = thread
                            thread.start()
                try:
                    kind, source, payload = self.router_queue.get(timeout=1.0)
                except queue.Empty:
                    kind, source = "", None
                runner = source if kind and isinstance(source, SeatRunner) else None
                if runner is not None and self.seats.get(runner.seat) is not runner:
                    runner = None  # events from a removed seat's old queue are never routed
                if runner is not None and kind == "backfill":
                    self.route_backfill(runner, payload)
                elif runner is not None and kind == "events":
                    self.handle_events(runner, payload)
                self.tick()
        finally:
            self.shutdown()
            os.close(lock_fd)
        return 0

    def shutdown(self) -> None:
        self.stop.set()
        with self.child_lock:
            children = list(self.children)
        for child in children:
            _kill_group(child)
        for runner in self.seats.values():
            runner.disconnect()
            runner.flush_cursor(force=True)
        self.write_status()
        self.log.write("stopped")


# --------------------------------------------------------------------------------------------
# Pause file and helpers shared with the CLI
# --------------------------------------------------------------------------------------------

def pause_path(root: str) -> str:
    return os.path.join(L.listener_dir(root), "pause.json")


def set_pause(root: str, seat: str | None, *, wakes_only: bool, by: str, now: float, message_id: int | None = None) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data[seat or "*"] = {"wakes_only": wakes_only, "by": by, "ts": now, "message_id": message_id}

    L.update_json(pause_path(root), mutate)


def clear_pause(root: str, seat: str | None) -> bool:
    removed = []

    def mutate(data: dict[str, Any]) -> None:
        if seat is None:
            removed.extend(data)
            data.clear()
        elif seat in data:
            removed.append(seat)
            data.pop(seat)

    L.update_json(pause_path(root), mutate)
    return bool(removed)


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    """SIGKILL a wake child's process group (it runs in its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def _process_start(pid: int) -> int:
    from .attach import process_start

    return process_start(pid)


def plugin_version() -> str | None:
    return L.read_json(PLUGIN_MANIFEST).get("version")
