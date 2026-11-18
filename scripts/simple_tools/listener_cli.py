"""CLI commands for the listener:  `daemon ...`, `status`, `wakes` and `inbox --local`.

These run without a Zulip login except where they must talk to Zulip (`daemon init`,
`status --probe`; `daemon test-wake` fetches nothing from Zulip but sends the seat's router one
dry-run wake).  None of them runs launchctl.  `rt` is the CLI's Runtime (env, streams, home, sleep).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import adapters as A
from . import chat_nudges as CN
from . import chat_transport as CT
from . import config as C
from . import heldback as HELD
from . import identity as I
from . import launchd, systemd
from . import listener_records as LR
from . import live as L
from . import wakes as W
from . import zulip as Z

# Seat tags (from seat_tag_for, which reads the bot's email) whose bots `daemon init` pins.  The pins are
# an optional extra:  since the owner ruling of 2026-10-09 the daemon treats every fleet bot as eligible
# at runtime (router.fleet_bot_ids over config.fleet_tags:  every partition seat, GB bots and BF role
# bots included), so a new seat needs no re-init.  Webhook bots and unknown senders stay out.
FLEET_SEATS = C.FLEET_SEATS
SERVER_SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server", "listener.toml")


def _home(rt: Any) -> str:
    return str(rt.home) if rt.home else (rt.env.get("HOME") or os.path.expanduser("~"))


def _root(rt: Any) -> str:
    if rt.env.get(L.ENV_STATE_DIR):
        return L.state_root(rt.env)
    return os.path.join(_home(rt), ".simple-tools")


def _config_path(rt: Any) -> str:
    """The listener.toml the daemon reads (SIMPLE_TOOLS_CONFIG, else <state root>/listener.toml)."""
    return L.config_path(_root(rt), rt.env)


def _load(rt: Any) -> C.Config:
    return C.load(_root(rt), _config_path(rt))


def _secrets_dir(rt: Any) -> str | None:
    """Only the explicit SIMPLE_TOOLS_SECRETS_DIR override;  seats read Infisical /zulip (AFC-359)."""
    return Z.override_secrets_dir(rt.env)


def _seat(rt: Any, args: argparse.Namespace, cfg: C.Config | None = None) -> str | None:
    """The seat a local command shows (identity.resolve_seat):  a launcher's AGENT_LAUNCH_SEAT, and
    a refusal when --seat, --as, AGENT_SEAT or AGENT_TAG names another or the launcher set none;
    else --seat, --as, AGENT_SEAT, AGENT_TAG, --default-seat; and only then, with no launcher, the
    `[platform.claude-code]` seat (the Claude Code platform default for the hooks and these local
    views, never for posting)."""
    flag = getattr(args, "seat", None) or getattr(args, "as_seat", None)
    resolved = I.resolve_seat(rt.env, flag=flag, default=getattr(args, "default_seat", None))
    if resolved.problem:
        raise Z.CredentialError(resolved.problem)
    raw = resolved.seat
    if not raw:
        cfg = cfg or _load(rt)
        raw = cfg.claude_seat
    return Z.normalise_seat(raw) if raw else None


def _when(ts: Any) -> str:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "never"
    from .cli import format_time

    return format_time(float(ts))


def daemon_running(root: str) -> int | None:
    """The daemon's pid when it holds daemon.lock, else None."""
    path = os.path.join(L.listener_dir(root), "daemon.lock")
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            text = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def installed_plugin_version(home: str) -> str | None:
    data = L.read_json(os.path.join(home, ".claude", "plugins", "installed_plugins.json"))
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else data
    entry = plugins.get("simple-chat@afc") if isinstance(plugins, dict) else None
    if isinstance(entry, list) and entry:
        entry = entry[-1]
    return entry.get("version") if isinstance(entry, dict) else None


# --------------------------------------------------------------------------------------------
# daemon run, install, uninstall, pause, resume, reload
# --------------------------------------------------------------------------------------------


def _check_defer_scope(seat: str | None, defer: bool) -> None:
    """AFC-652:  --defer is fleet-wide drain semantics (wakes defer, broadcasts park, the
    Linear hook gates);  a per-seat marker would arm the hook and defer one seat's wakes
    without parking broadcasts, so the CLI refuses the combination."""
    if defer and seat is not None:
        raise Z.UsageError("--defer marks a fleet-wide drain window; it cannot take --seat")


def cmd_daemon(rt: Any, args: argparse.Namespace) -> int:
    sub = args.daemon_command
    root = _root(rt)
    if sub == "chats":
        # Bindings target GROK but are local config; resolve the caller's own identity explicitly
        # without a Claude platform default or any credential exchange with the target bot.
        caller = I.resolve_seat(rt.env, flag=getattr(args, "as_seat", None), default=None)
        if caller.problem or not caller.seat:
            raise Z.CredentialError(
                caller.problem or "daemon chats requires an explicit caller seat"
            )
        if args.chats_action in ("catalog", "peek"):
            from .chat_catalog import ChatCatalog

            catalog = ChatCatalog(home=_home(rt), max_records=getattr(args, "limit", 120))
            # This is a one-shot CLI process, unlike the daemon's nonblocking catalog refresh.
            # Wait for the bounded leader lookup so `catalog`/`peek` can actually inspect Grok.
            rows = catalog.discover(wait_grok=True)
            if args.chats_action == "catalog":
                rt.out(json.dumps(rows, ensure_ascii=True, indent=2) + "\n")
            else:
                if not catalog.resolve(args.chat_id):
                    raise Z.UsageError("chat id must exactly match a discovered chat")
                rt.out(
                    json.dumps(catalog.excerpt(args.chat_id, limit=4), ensure_ascii=True, indent=2)
                    + "\n"
                )
            return 0
        if not getattr(args, "target_seat", None) or Z.normalise_seat(args.target_seat) not in (
            "GROK",
            "CURSOR",
            "CODEX",
        ):
            raise Z.UsageError("daemon chats requires --target-seat GROK, CURSOR or CODEX")
        registry = CN.ChatNudges(root, Z.normalise_seat(args.target_seat), recover=False)
        action = args.chats_action
        if action == "list":
            if Z.normalise_seat(args.target_seat) == "CODEX":
                from .chat_catalog import ChatCatalog

                rows = [
                    row
                    for row in ChatCatalog(home=_home(rt), max_records=120).discover()
                    if row.get("platform") == "codex"
                ]
            elif Z.normalise_seat(args.target_seat) == "CURSOR":
                from .cursor_chat_transport import CursorCloudTransport

                rows = CursorCloudTransport().discover()
            else:
                rows = CT.GrokTransport().discover()
            rt.out(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
            return 0
        if action == "status":
            snapshot = registry.snapshot()
            counts = {
                state: sum(1 for item in snapshot["items"] if item.get("status") == state)
                for state in ("pending", "dispatching", "accepted", "unknown", "cancelled")
            }
            counts["blocked"] = sum(
                1
                for item in snapshot["items"]
                if item.get("status") in ("pending", "unknown") and item.get("hold_reason")
            )
            rt.out(json.dumps({"counts": counts, **snapshot}, ensure_ascii=False, indent=2) + "\n")
            return 0
        if action == "unbind":
            removed = registry.unbind(args.channel, args.topic)
            rt.out("unbound\n" if removed else "no binding\n")
            return 0
        if action == "retry":
            released = registry.retry(args.item_id, time.time())
            rt.out(
                "hold released; delivery gates will be rechecked\n"
                if released
                else "not retryable (requires a pending held item)\n"
            )
            return 0 if released else 1
        if action == "cancel":
            cancelled = registry.cancel(args.item_id, time.time())
            rt.out(
                "cancelled\n"
                if cancelled
                else "not cancellable (missing, accepted, or dispatching)\n"
            )
            return 0 if cancelled else 1
        if action == "bind":
            if Z.normalise_seat(args.target_seat) != "GROK":
                raise Z.UsageError(
                    "manual binding currently requires GROK; Cursor Cloud and Codex use catalog selection"
                )
            host = socket.gethostname()
            target = next(
                (
                    row
                    for row in CT.GrokTransport().discover(limit=1, session_id=args.session_id)
                    if row.get("platform") == "grok-leader"
                    and row.get("host") == host
                    and row.get("session_id") == args.session_id
                    and row.get("cwd") == args.cwd
                    and not row.get("self")
                ),
                None,
            )
            if target is None:
                raise Z.UsageError(
                    "session id and cwd must exactly match a discovered existing Grok chat"
                )
            binding = registry.bind(
                args.channel, args.topic, "grok-leader", host, args.session_id, args.cwd
            )
            rt.out(json.dumps(binding, ensure_ascii=False) + "\n")
            return 0
    if sub == "run":
        from .daemon import Daemon

        env = dict(rt.env)
        env.setdefault("HOME", _home(rt))
        return Daemon(env=env, root=root, home=_home(rt), stderr=rt.stderr, stdout=rt.stdout).run(
            wait_lock=bool(getattr(args, "wait_lock", False))
        )
    if sub in ("install", "uninstall"):
        if sys.platform == "darwin":
            service = launchd
        elif sys.platform.startswith("linux"):
            service = systemd
        else:
            raise Z.UsageError("daemon installation supports only macOS and Linux")
    if sub == "install":
        return service.install(
            rt.env,
            _home(rt),
            rt.stdout,
            dry_run=args.dry_run,
            python=args.python,
            program=args.bin,
            **({"err": rt.stderr} if service is systemd else {}),
        )
    if sub == "uninstall":
        return service.uninstall(_home(rt), rt.stdout, dry_run=args.dry_run)
    if sub == "status":
        return cmd_status(rt, args)
    if sub == "pause":
        from .daemon import set_pause

        seat = Z.normalise_seat(args.seat) if args.seat else None
        _check_defer_scope(seat, args.defer)
        set_pause(
            root, seat, wakes_only=args.wakes_only, by="cli", now=time.time(), defer=args.defer
        )
        rt.out(
            "paused %s%s; resume with: simple-chat daemon resume%s\n"
            % (
                seat or "every seat",
                " (headless wakes only)" if args.wakes_only else " (wakes and live interrupts)",
                " --seat " + seat if seat else "",
            )
        )
        return 0
    if sub == "resume":
        from .daemon import clear_pause

        seat = Z.normalise_seat(args.seat) if args.seat else None
        rt.out(
            ("resumed %s\n" % (seat or "every seat"))
            if clear_pause(root, seat)
            else "nothing was paused\n"
        )
        return 0
    if sub == "reload":
        pid = daemon_running(root)
        if not pid or pid < 0:
            rt.err("simple-chat: the listener is not running")
            return 1
        os.kill(pid, signal.SIGHUP)
        rt.out("sent SIGHUP to the listener (pid %d)\n" % pid)
        return 0
    if sub == "init":
        return cmd_init(rt, args)
    if sub == "test-wake":
        return cmd_test_wake(rt, args)
    if sub == "check-config":
        return cmd_check_config(rt, args)
    raise Z.UsageError("unknown daemon command %r" % sub)


# --------------------------------------------------------------------------------------------
# check-config
# --------------------------------------------------------------------------------------------


def cmd_check_config(rt: Any, args: argparse.Namespace) -> int:
    """Would the listener take this candidate listener.toml?  The listener's own TOML read, its
    from_dict and its seat partition check run on the candidate and on the current file, so a tool
    that writes the file (Muster's Wake & Listener panel) can refuse a bad edit before it lands and
    does not have to copy the rules.  It reads two files and changes nothing.

    `ok` means the candidate parses, the partition refuses nothing in it, and it brings no error the
    current file does not already have.  An existing soft error on another seat does not block an
    edit;  an existing partition refusal does, because the listener would refuse any reload of it.

    `--partition` names the partition to judge by.  The running listener can read another checkout's
    copy than this command's, and that copy is the one that can refuse the file, so a tool that knows
    it (the status's `partition_file`) passes it.

    `warnings` are the candidate's schema notes (a deprecated seat setting that still works, a seat key
    nothing reads).  They are printed and reported, and they never change `ok` or the exit code."""
    root = _root(rt)
    current = _config_path(rt)
    partition_file = (
        os.path.expanduser(args.partition) if args.partition else C.partition_path(rt.env)
    )
    partition, partition_problem = C.load_partition(partition_file)
    env_instance = (rt.env.get(C.ENV_INSTANCE) or "").strip().casefold() or None

    def judge(path: str) -> tuple[list[str], list[str], list[str]]:
        cfg = C.load(root, path)
        if partition_problem:
            refused = [partition_problem + "; cannot check the seat partition, so no seat is held"]
        else:
            refused = C.partition_errors(
                cfg, partition, partition_file=partition_file, env_instance=env_instance
            )
        return list(cfg.errors), refused, list(cfg.warnings)

    raw, parse_error = L.load_config(root, args.file)
    if parse_error is None and not raw:
        parse_error = "%s holds no settings" % args.file
    errors: list[str] = []
    refused: list[str] = []
    warnings: list[str] = []
    new_errors: list[str] = []
    if parse_error is None:
        errors, refused, warnings = judge(args.file)
        before_errors, before_refused, _before_warnings = judge(current)
        before = set(before_errors) | set(before_refused)
        new_errors = [e for e in errors + refused if e not in before]
    ok = parse_error is None and not new_errors and not refused
    if getattr(args, "json", False):
        rt.out(
            json.dumps(
                {
                    "ok": ok,
                    "candidate": args.file,
                    "current": current,
                    "partition_file": partition_file,
                    "parse_error": parse_error,
                    "errors": errors,
                    "new_errors": new_errors,
                    "refused": refused,
                    "warnings": warnings,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        rt.out("the listener would %s %s\n" % ("take" if ok else "not take", args.file))
        for problem in ([parse_error] if parse_error else []) + refused + new_errors:
            rt.out("  %s\n" % problem)
        for note in warnings:
            rt.out("  warning: %s\n" % note)
    return 0 if ok else 1


# --------------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------------


def _wake_caps_text(budget: Any) -> str:
    """The wake count caps a status line shows:  `cap 40/day, 6/hour`, with `no limit` for a cap the
    seat leaves unset, and `no cap` when neither is set.  An unset cap is no limit (owner, Nov 10,
    2026), so it is never shown as 0, which would read as a pause."""
    caps = budget if isinstance(budget, dict) else {}

    def one(name: str) -> str:
        value = caps.get(name)
        return "no limit" if value is None else str(int(value))

    if caps.get("wakes_per_day") is None and caps.get("wakes_per_hour") is None:
        return "no cap"
    return "cap %s/day, %s/hour" % (one("wakes_per_day"), one("wakes_per_hour"))


def _wake_loop_status_line(status: dict[str, Any]) -> str | None:
    enabled = status.get("wake_loop_prevention")
    if type(enabled) is not bool:
        return None
    return "wake loop prevention: " + ("ON" if enabled else "OFF (topic loop guard bypassed)")


def _gate_status_lines(root: str, seat: str, now: float, *, ledger: bool) -> list[str]:
    """The model gate's status line for one seat (phase 1): `model starts 24h N (avoidable M)` from the
    seat's own wake ledger.  The ledger is read from its END, down to the window, so the cost is the day
    and not the 90 days a ledger keeps.  Read-only, and never a reason for `status` to fail.  The line is
    printed only when the ledger could be read."""
    lines: list[str] = []
    if ledger:
        try:
            rows = W.rows_since(L.SeatPaths(root, seat).wakes, now - W.DAY)
            if rows is not None:
                lines.append(W.gate_line(W.gate_counts(rows, now, W.DAY).get(seat)))
        except Exception:  # noqa: BLE001
            pass
    return lines


def cmd_status(rt: Any, args: argparse.Namespace) -> int:
    root = _root(rt)
    home = _home(rt)
    status = L.read_json(os.path.join(L.listener_dir(root), "status.json"))
    pid = daemon_running(root)
    repo_version = L.read_json(
        os.path.join(A.REPO_ROOT, "plugins", "simple-chat", ".claude-plugin", "plugin.json")
    ).get("version")
    info = {
        "running": bool(pid),
        "pid": pid if pid and pid > 0 else None,
        "status": status,
        "plugin": {"repo": repo_version, "installed": installed_plugin_version(home)},
        "launch_agent": os.path.exists(launchd.plist_path(home)),
    }
    if sys.platform.startswith("linux"):
        info["systemd_user_service"] = systemd.status(home)
    probe_result: dict[str, Any] | None = None
    if getattr(args, "probe", False):
        probe_result = run_probe(rt, args, root, bool(pid))
        info["probe"] = probe_result
    if getattr(args, "json", False):
        rt.out(json.dumps(info, ensure_ascii=False, indent=2, default=str) + "\n")
        return 0 if probe_result is None or probe_result.get("ok") else 1
    lines = [
        "listener: %s%s"
        % ("running" if pid else "not running", " (pid %d)" % pid if pid and pid > 0 else "")
    ]
    if "systemd_user_service" in info:
        service_info = info["systemd_user_service"]
        lines.append(
            "systemd user service: %s; state: %s"
            % (
                "installed" if service_info["installed"] else "not installed",
                service_info["active_state"],
            )
        )
    plugin_line = "plugin: repo %s, installed %s" % (
        repo_version or "?",
        info["plugin"]["installed"] or "no",
    )
    if sys.platform == "darwin":
        plugin_line = (
            "LaunchAgent: %s; " % ("installed" if info["launch_agent"] else "not installed")
            + plugin_line
        )
    lines.append(plugin_line)
    if status:
        lines.append(
            "status updated %s; instance %s; owner pinned: %s; rewake verified: terminal %s, Desktop %s"
            % (
                _when(status.get("updated")),
                status.get("instance") or "mac",
                "yes" if status.get("owner_user_id") else "NO",
                "yes" if status.get("rewake_verified_cli", status.get("rewake_verified")) else "no",
                "yes"
                if status.get("rewake_verified_desktop", status.get("rewake_verified"))
                else "no",
            )
        )
        nudges = status.get("chat_nudges")
        if isinstance(nudges, dict):
            # Accept the older flat status snapshot during managed-runtime upgrades.
            by_seat = {"GROK": nudges} if "bindings" in nudges else nudges
            for seat, counts in sorted(by_seat.items()):
                if isinstance(counts, dict):
                    lines.append(
                        "%s chat nudges: %s bindings, %s pending, %s blocked, %s unknown, %s accepted"
                        % (
                            seat,
                            counts.get("bindings", 0),
                            counts.get("pending", 0),
                            counts.get("blocked", 0),
                            counts.get("unknown", 0),
                            counts.get("accepted", 0),
                        )
                    )
        pause = status.get("pause") or {}
        if pause:
            lines.append(
                "paused: %s"
                % ", ".join(
                    "%s%s%s"
                    % (
                        k,
                        " (wakes only)" if (v or {}).get("wakes_only") else "",
                        " (drain)" if (v or {}).get("defer") else "",
                    )
                    for k, v in sorted(pause.items())
                )
            )
        for error in status.get("config_errors") or []:
            lines.append("RED config: %s" % error)
        for seat, why in sorted((status.get("skipped_seats") or {}).items()):
            lines.append("%-12s NOT LOADED  nothing is captured for this seat" % seat)
            for problem in why or []:
                lines.append("  RED %s" % problem)
        for seat, s in sorted((status.get("seats") or {}).items()):
            routine = s.get("routine") or {}
            if s.get("wake") == "claude":
                pin = s.get("pin") or {}
                # A stale pin is not a seat that was never set up, and the difference is the whole point
                # of AFC-256:  say which one this is rather than a bare NOT pinned.
                state = (
                    " (pinned)"
                    if s.get("pinned")
                    else (
                        " (NOT pinned: %s)" % pin.get("why") if pin.get("why") else " (NOT pinned)"
                    )
                )
            elif s.get("wake") == "http":
                state = (
                    " (routine ready, %s)" % routine.get("host")
                    if routine.get("ready")
                    else " (routine NOT ready: %s)" % (routine.get("detail") or routine.get("why"))
                )
            elif s.get("wake") == "openrouter":
                # No binary, so the pin is on the model and the request shape.
                pin = s.get("pin") or {}
                pin_text = (
                    "pinned"
                    if s.get("pinned")
                    else "NOT pinned: %s" % (pin.get("why") or "not_pinned")
                )
                state = " (%s, %s)" % (s.get("model"), pin_text)
            else:
                state = ""
            fallback = s.get("fallback") or {}
            if fallback:
                fallback_pin = fallback.get("pin") or {}
                state += "  fallback %s (%s, %s)%s" % (
                    fallback.get("runtime"),
                    fallback.get("model"),
                    "pinned"
                    if fallback.get("pinned")
                    else "NOT pinned: %s" % (fallback_pin.get("why") or "not_pinned"),
                    " ANSWERING IN PLACE OF THE WAKE (%s)" % fallback.get("primary_hold")
                    if fallback.get("stands_in")
                    else "",
                )
            routes = ""
            if s.get("wake") == "claude":
                routes = "  routes 24h %s%s" % (
                    s.get("routes_24h", 0),
                    "" if s.get("route_enabled", True) else " (routing off)",
                )
            lines.append(
                "%-12s %s  last event %s  cursor %s  wake %s%s  wakes 24h %s (%s)%s"
                % (
                    seat,
                    "connected" if s.get("connected") else "DOWN",
                    _when(s.get("last_event")),
                    s.get("cursor"),
                    s.get("wake"),
                    state,
                    s.get("wakes_24h"),
                    _wake_caps_text(s.get("budget")),
                    routes,
                )
            )
            for gate_text in _gate_status_lines(
                root,
                seat,
                time.time(),
                ledger=s.get("wake") != "inbox",  # an inbox seat starts no model
            ):
                lines.append("  " + gate_text)
            for red in s.get("red") or []:
                lines.append("  RED %s" % red)
            held = _held_line(s.get("held_back"))
            if held:
                lines.append("  " + held)
            for lease in s.get("leases") or []:
                lines.append(
                    "  lease %s  %s  topics %s  watcher %s  pending %s"
                    % (
                        lease.get("lease_id"),
                        "live" if lease.get("alive") else "dead (%s)" % lease.get("why"),
                        lease.get("topics"),
                        "yes" if lease.get("watcher") else "no",
                        lease.get("pending"),
                    )
                )
        loop_status = _wake_loop_status_line(status)
        if loop_status:
            lines.append(loop_status)
        for held_key in status.get("loop_guard") or []:
            # A bot DM has no owner to post, so its count rolls and clears itself;  a topic counts
            # until the owner posts.  Saying the wrong one would send someone to wait for a reset
            # that cannot come.
            lines.append(
                "loop guard: %d wake replies in #%s > %s (%s); %s"
                % (
                    int(held_key.get("count") or 0),
                    L.escape_line(str(held_key.get("channel") or "")),
                    L.escape_line(str(held_key.get("topic") or "")),
                    "rolling, so it clears itself"
                    if held_key.get("rolling")
                    else "counts until you post",
                    "the messages are in the seat inboxes"
                    if held_key.get("rolling")
                    else "an owner post there resets it, and the messages are in the seat inboxes",
                )
            )
        if status.get("disabled"):
            lines.append(
                "disabled seats (enabled = false, no queue): %s" % ", ".join(status["disabled"])
            )
    else:
        lines.append("no status yet (the daemon writes listener/status.json every few seconds)")
    try:
        seat = _seat(rt, args)
    except (
        Z.CredentialError
    ) as exc:  # a launched session with no seat, or a re-pin:  no seat inbox shown
        seat = None
        lines.append("seat inbox: not shown (%s)" % exc)
    if seat:
        paths = L.SeatPaths(root, seat)
        cursor = L.read_json(paths.local_cursor).get("seq") or 0
        waiting = sum(
            1
            for i in L.read_jsonl(paths.seat_inbox)
            if (i.get("seq") or 0) > cursor and not i.get("delivered_to")
        )
        lines.append("%s seat inbox: %d unread (simple-chat inbox --local)" % (seat, waiting))
    if probe_result is not None:
        lines.append(
            "probe: %s"
            % ("pass" if probe_result.get("ok") else "FAIL (%s)" % probe_result.get("reason"))
        )
    rt.out("\n".join(lines) + "\n")
    return 0 if probe_result is None or probe_result.get("ok") else 1


def run_probe(rt: Any, args: argparse.Namespace, root: str, running: bool) -> dict[str, Any]:
    """Post in #sandbox > listener probe and pass when the daemon routes that id within the timeout."""
    if not running:
        return {"ok": False, "reason": "the listener is not running"}
    try:
        seat = _seat(rt, args)
    except Z.CredentialError as exc:
        return {"ok": False, "reason": str(exc)}
    if not seat:
        return {"ok": False, "reason": "no seat; pass --seat"}
    cfg = _load(rt)
    seat_cfg = cfg.seats.get(seat)
    if seat_cfg is None:
        return {"ok": False, "reason": "seat %s is not an enabled seat in listener.toml" % seat}
    creds = C.seat_credentials(seat_cfg, rt.env, _secrets_dir(rt))
    realm = Z.realm_url(rt.env)
    Z.verify_realm(creds, realm)
    client = Z.ZulipClient(creds, realm, timeout=rt.timeout, sleep=rt.sleep)
    result = client.post(
        "messages",
        {
            "type": "stream",
            "to": "sandbox",
            "topic": "listener probe",
            "content": "[%s] listener probe %s" % (seat, os.urandom(3).hex()),
        },
    )
    message_id = result.get("id")
    deadline = time.monotonic() + float(getattr(args, "probe_timeout", 30.0) or 30.0)
    path = os.path.join(L.listener_dir(root), "probe.json")
    while time.monotonic() < deadline:
        if message_id in (L.read_json(path).get(seat) or []):
            return {"ok": True, "id": message_id}
        time.sleep(0.25)
    return {
        "ok": False,
        "reason": "message %s was not routed within the timeout" % message_id,
        "id": message_id,
    }


# --------------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------------


def _rewrite_pins(text: str, owner: int, eligible: list[int]) -> str:
    owner_line = "owner_user_id = %d" % owner
    eligible_line = "eligible_user_ids = [%s]" % ", ".join(str(i) for i in eligible)
    text, n1 = re.subn(r"(?m)^owner_user_id\s*=\s*[^#\n]*", owner_line + "  ", text, count=1)
    text, n2 = re.subn(
        r"(?m)^eligible_user_ids\s*=\s*\[[^\]]*\][^#\n]*", eligible_line + "  ", text, count=1
    )
    if not n1 or not n2:
        raise Z.UsageError(
            "could not find owner_user_id and eligible_user_ids in the [daemon] section; "
            "set them by hand: %s and %s" % (owner_line, eligible_line)
        )
    return text


def _init_credentials(
    rt: Any, args: argparse.Namespace, cfg: C.Config, seat: str, secrets_dir: str | None
) -> Z.Credentials:
    """The credentials `daemon init` reads the user list with, in this order:  --rc, ZULIP_RC, the
    seat's own source in listener.toml (enabled or not:  an env seat's email_env and key_env, else
    its bot's Infisical /zulip keys), and for a seat the config does not list, the CLI's order
    (ZULIP_EMAIL, ZULIP_API_KEY and ZULIP_SITE, then Infisical /zulip).  An explicit --rc or
    ZULIP_RC that cannot be read is an error, never a fall-through."""
    rc_arg = getattr(args, "rc", None)
    if rc_arg:
        return Z.read_zuliprc(Path(rc_arg).expanduser())
    if rt.env.get("ZULIP_RC"):
        return Z.read_zuliprc(Path(rt.env["ZULIP_RC"]).expanduser())
    seat_cfg = cfg.seats.get(seat) or cfg.disabled.get(seat)
    if seat_cfg is not None:
        return C.seat_credentials(seat_cfg, rt.env, secrets_dir)
    return Z.resolve_credentials(rt.env, rc_arg=None, seat=seat, home=Path(_home(rt)))


def cmd_init(rt: Any, args: argparse.Namespace) -> int:
    from .cli import seat_tag_for

    root = _root(rt)
    config_path = _config_path(rt)
    secrets_dir = _secrets_dir(rt)
    server = (rt.env.get(C.ENV_INSTANCE) or "").strip().casefold() == "server"
    if not os.path.exists(config_path):
        if server:
            with open(SERVER_SAMPLE, encoding="utf-8") as fh:
                launchd.write_private(config_path, fh.read())
        else:
            launchd.write_private(config_path, launchd.sample_config(secrets_dir, rt.env))
        rt.out("wrote the sample config %s\n" % config_path)
    cfg = C.load(root, config_path)
    # The bot that reads the user list:  --seat, then --as, the Claude seat, else the first enabled
    # seat (the server instance has no Claude seat).
    default = (
        cfg.claude_seat
        if cfg.claude_seat in cfg.seats
        else (sorted(cfg.seats) or [cfg.claude_seat or "CLAUDE"])[0]
    )
    seat = Z.normalise_seat(
        getattr(args, "seat", None) or getattr(args, "as_seat", None) or default or "CLAUDE"
    )
    creds = _init_credentials(rt, args, cfg, seat, secrets_dir)
    realm = Z.realm_url(rt.env)
    Z.verify_realm(creds, realm)
    client = Z.ZulipClient(creds, realm, timeout=rt.timeout, sleep=rt.sleep)
    # The #410 bot check, whatever the source:  the key must be the --seat's own bot, so a stray --rc
    # or ZULIP_RC cannot read the realm as another seat.
    problem = I.check_bot(client.get("users/me"), seat, creds.email)
    if problem:
        raise Z.CredentialError(problem)
    rt.out("reading the user list as %s (%s)\n" % (seat, creds.source))
    members = list(client.get("users").get("members") or [])
    owners = [
        u
        for u in members
        if u.get("role") == 100 and not u.get("is_bot") and u.get("is_active", True)
    ]
    if len(owners) != 1:
        raise Z.UsageError(
            "expected exactly one human realm owner (role 100), found %d; pin owner_user_id by hand"
            % len(owners)
        )
    owner = owners[0]
    eligible = sorted(
        (seat_tag_for(u), int(u["user_id"]))
        for u in members
        if u.get("is_bot") and u.get("is_active", True) and seat_tag_for(u) in FLEET_SEATS
    )
    rt.out(
        "owner: user id %s, %s (%s)\n"
        % (owner["user_id"], owner.get("full_name"), owner.get("email"))
    )
    rt.out(
        "eligible seat bots: %s\n"
        % (", ".join("%s=%d" % pair for pair in eligible) or "none found")
    )
    in_infisical = frozenset() if secrets_dir else Z.infisical_seats(rt.env, Path(_home(rt)))
    present = [
        s
        for s in launchd.READER_SEATS + launchd.NO_READER
        if (
            os.path.isfile(os.path.join(secrets_dir, Z.credential_file_name(s)))
            if secrets_dir
            else s in in_infisical
        )
    ]
    rt.out(
        "credentials present (not enabled by this command): %s\n" % (", ".join(present) or "none")
    )
    by_email = {str(u.get("email") or "").casefold(): u for u in members}
    for name, seat_cfg in sorted(cfg.seats.items()):
        try:
            seat_creds = C.seat_credentials(seat_cfg, rt.env, secrets_dir)
            Z.verify_realm(seat_creds, realm)
        except Z.CredentialError as exc:
            rt.out("  %s: %s\n" % (name, exc))
            continue
        user = by_email.get(seat_creds.email.casefold()) or {}
        role = user.get("role")
        ok_role = role in (300, 400)
        try:
            seat_client = Z.ZulipClient(seat_creds, realm, timeout=rt.timeout, sleep=rt.sleep)
            subs = {
                s.get("name")
                for s in seat_client.get("users/me/subscriptions").get("subscriptions") or []
            }
        except Z.SimpleToolsError:
            subs = set()
        rt.out(
            "  %s: %s ok, role %s%s, subscribed to #%s: %s\n"
            % (
                name,
                "environment credentials"
                if seat_cfg.creds == "env"
                else (
                    "file mode" if seat_cfg.creds == "file" and secrets_dir else "Infisical /zulip"
                ),
                role,
                "" if ok_role else " (REFUSED: the listener takes moderator or member bots only)",
                Z.DEFAULT_CHANNEL,
                "yes"
                if Z.DEFAULT_CHANNEL.casefold() in {str(s).casefold() for s in subs}
                else "NO",
            )
        )
    if not args.yes:
        rt.out("Pin these in %s? Type yes: " % config_path)
        answer = rt.stdin.readline().strip().casefold()
        if answer != "yes":
            rt.out("not changed\n")
            return 1
    with open(config_path, encoding="utf-8") as fh:
        text = fh.read()
    text = _rewrite_pins(text, int(owner["user_id"]), [i for _, i in eligible])
    fd = os.open(config_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(config_path + ".tmp", config_path)
    rt.out("pinned owner_user_id and eligible_user_ids in %s\n" % config_path)
    return 0


# --------------------------------------------------------------------------------------------
# test-wake
# --------------------------------------------------------------------------------------------


#: `daemon test-wake` sends its body only to the one shared wake router, because a routine
#: that is not the router may not know the test flag and would really wake:  a Grok Bot
#: routine webhook, JET's hosted MCP Worker, and the ZCode and Antigravity wake bridges.  The
#: path alone proves nothing (the two bridges serve the same `/wake/<SEAT>` path with the same
#: contract and never read `test`), so three things must hold before the key or the body
#: leaves:  the routine sends the router's one bearer (`key_env` is config.ROUTER_BEARER_ENV
#: with `auth = "bearer"`), its URL path is the router's path for the seat, and an
#: unauthenticated `GET /health/<SEAT>` on that host answers as the router
#: (wake_router.Seats.seat_health;  both bridges answer 404 there).
#: The router's wake path and health path for one seat (wake_router.WAKE_PATH_RE, HEALTH_PATH_RE).
TEST_WAKE_ROUTER_PATH = "/wake/%s"
TEST_WAKE_HEALTH_PATH = "/health/%s"
TEST_WAKE_CHANNEL = "sandbox"
TEST_WAKE_TOPIC = "AFC listener test-wake"
#: The router answer's fields `daemon test-wake` shows (wake_router.Router.test_answer).
TEST_WAKE_ANSWER_FIELDS = (
    "test",
    "status",
    "wake_id",
    "receipt_id",
    "seat",
    "letter",
    "letter_live",
    "picked",
    "picked_letter",
    "picked_generation",
    "session_id",
    "host",
    "thread_link",
    "reason",
    "candidates",
    "link_note",
    "delivered",
    "error",
)
_SCALARS = (str, int, float, bool, type(None))
#: What the command tells a person whose shell does not hold the routine's variables.
TEST_WAKE_ENV_HINT = (
    "this command reads the routine's variables from its own environment, not from the running "
    'listener;  on the server run it through the entrypoint, docker exec "$C" '
    "/app/scripts/simple_tools/server/entrypoint.sh daemon test-wake --seat SEAT"
)


def _json_object(data: bytes | None) -> dict[str, Any] | None:
    """`data` parsed as one JSON object, else None.  A body nested past the parser's depth is
    not an object either."""
    try:
        parsed = json.loads((data or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _router_health(target: A.RoutineTarget, seat: str) -> tuple[dict[str, Any] | None, str]:
    """(the router's `GET /health/<SEAT>` answer at the routine's host, "") or (None, why).  No key
    is sent, no proxy is used and no redirect is followed, within the routine's timeout and reading
    at most ROUTINE_RESPONSE_CAP bytes (a 503 answer's body too, since an unhealthy router still
    answers for its seat).  It counts only when it is a JSON object whose `seat` is this seat and
    whose `healthy` is a boolean (wake_router.Seats.seat_health);  the wake bridges answer 404
    there.  `why` is "router_unreachable" when the host gave no HTTP answer (refused, timed out,
    cut off), else "not_the_router":  a redirect, a 404 or any other answer."""
    try:
        parts = urllib.parse.urlsplit(target.url)
    except ValueError:
        return None, "not_the_router"
    url = "%s://%s%s" % (parts.scheme, parts.netloc, TEST_WAKE_HEALTH_PATH % seat)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Z._NoRedirect())
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json", "User-Agent": Z.USER_AGENT}
    )
    data = b""
    try:
        with opener.open(request, timeout=target.timeout) as response:
            data = response.read(A.ROUTINE_RESPONSE_CAP)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 503:
                data = exc.read(A.ROUTINE_RESPONSE_CAP)
        except (OSError, http.client.HTTPException):
            data = b""
        finally:
            exc.close()
    except ValueError:
        return None, "not_the_router"
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return None, "router_unreachable"
    found = _json_object(data)
    if found is None or found.get("seat") != seat or not isinstance(found.get("healthy"), bool):
        return None, "not_the_router"
    return found, ""


def _test_wake_body(
    seat: str, wake_id: str, topic: str, letter: str | None, realm: str, now: float
) -> dict[str, Any]:
    """The body the daemon would send for one mention of `seat` in #sandbox (adapters.routine_body,
    the same builder `Daemon.run_routine` calls), with `"test": true`.  The message id is 0:  no
    Zulip message stands behind a test.  `letter` is the router's explicit letter field."""
    row = {
        "id": 0,
        "type": "stream",
        "channel": TEST_WAKE_CHANNEL,
        "topic": topic,
        "sender": "simple-chat daemon test-wake",
        "is_bot": True,
        "owner": False,
        "content": "A test wake for %s from `simple-chat daemon test-wake`:  a dry run, so the "
        "router picks a chat and delivers nothing." % seat,
    }
    body = A.routine_body(
        seat=seat,
        wake_id=wake_id,
        trigger_ids=[0],
        row=row,
        bot_user_id=None,
        realm=realm,
        now=now,
    )
    if letter:
        body["letter"] = letter
    body["test"] = True
    return body


def cmd_test_wake(rt: Any, args: argparse.Namespace) -> int:
    """Send the seat's router one dry-run wake (`"test": true`) and show its answer.  The router
    picks a chat as it would for a real wake and delivers nothing.  The body is built, signed and
    sent by the code the daemon's own `http` wake uses (adapters.routine_body, routine_bytes,
    resolve_routine and RoutineRunner), once and without a retry, and only after the endpoint is
    shown to be the router (the checks above TEST_WAKE_ROUTER_PATH).  Exit 0 needs the router's dry-run
    answer, a picked chat, and with `--letter` that letter picked.  Nothing is written on the
    listener's side:  no ledger row, no inbox item and no pin (the router appends one test receipt
    to its own wakes.jsonl), and the daemon need not be running."""
    cfg = _load(rt)
    seat = Z.normalise_seat(args.seat)
    seat_cfg = cfg.seats.get(seat)
    if seat_cfg is None:
        raise Z.UsageError("seat %s is not a loaded seat in %s" % (seat, _config_path(rt)))
    routine = seat_cfg.routine
    if routine is None:
        raise Z.UsageError(
            "seat %s has no routine in %s (wake = %s), so it has no router to test"
            % (seat, _config_path(rt), seat_cfg.wake)
        )
    letter = (args.letter or "").strip().upper() or None
    if letter is not None and not re.fullmatch(r"[A-Z]{1,2}", letter):
        raise Z.UsageError("--letter must be one or two letters, such as AA")
    topic = args.topic or TEST_WAKE_TOPIC
    report: dict[str, Any] = {
        "seat": seat,
        "routine": {
            "url_env": routine.url_env,
            "key_env": routine.key_env,
            "auth": routine.auth,
            "method": routine.method,
        },
    }

    def finish(passed: bool, line: str) -> int:
        report["passed"] = passed
        if getattr(args, "json", False):
            rt.out(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        else:
            rt.out(line + "\n")
        return 0 if passed else 1

    if routine.key_env != C.ROUTER_BEARER_ENV:
        # This command sends only to a routine that names the router's bearer:  a wake bridge or
        # a single-threaded seat's routine names its own key variable.
        report["error"] = "not_the_router"
        return finish(
            False,
            "FAIL: the %s routine's key variable is %s, not the wake router's %s;  this command "
            "sends only to a routine that names the router's bearer, so nothing was sent"
            % (seat, routine.key_env, C.ROUTER_BEARER_ENV),
        )
    if routine.auth != "bearer":
        # The router checks `Authorization: Bearer <key>` only, and config keeps the router's
        # bearer to that one role (config._refuse_shared_env).
        report["error"] = "not_the_router"
        return finish(
            False,
            'FAIL: the %s routine names %s with auth = "%s";  the wake router takes it only as '
            'auth = "bearer", so nothing was sent' % (seat, routine.key_env, routine.auth),
        )
    target, why, detail = A.resolve_routine(routine, rt.env)
    if target is None:
        # resolve_routine's detail names the variables, never their values.
        if why == "routine_not_configured" and "not set" in detail:
            detail = "%s;  %s" % (detail, TEST_WAKE_ENV_HINT)
        report["error"] = "%s: %s" % (why, detail)
        return finish(
            False,
            "FAIL: the %s routine cannot be resolved in this command's environment (%s): %s"
            % (seat, why, detail),
        )
    hidden = sorted(target.secrets(), key=len, reverse=True)

    def scrub(text: str) -> str:
        for value in hidden:
            text = text.replace(value, "[redacted]")
        return text

    def shown(value: Any) -> Any:
        """A kept answer value, scrubbed:  a list keeps its scalar items only."""
        if isinstance(value, str):
            return scrub(value)
        if isinstance(value, list):
            return [scrub(v) if isinstance(v, str) else v for v in value if isinstance(v, _SCALARS)]
        return value

    report["routine"]["host"] = target.host
    try:
        path = urllib.parse.urlsplit(target.url).path
    except ValueError:
        path = ""
    if path != TEST_WAKE_ROUTER_PATH % seat:
        # The path is not printed:  a routine URL's path may hold a token (routine_secret_values).
        report["error"] = "not_the_router"
        return finish(
            False,
            "FAIL: the URL in %s is not the wake router's %s path, so nothing was sent:  a routine "
            "that is not the router may not know the test flag and would really wake %s"
            % (routine.url_env, TEST_WAKE_ROUTER_PATH % "<SEAT>", seat),
        )
    health, why = _router_health(target, seat)
    if health is None:
        report["error"] = why
        what = (
            "got no answer (the router may be down)"
            if why == "router_unreachable"
            else "did not answer as the wake router"
        )
        return finish(
            False,
            "FAIL: GET %s at the host in %s %s, so nothing was sent"
            % (TEST_WAKE_HEALTH_PATH % seat, routine.url_env, what),
        )
    delivery = health.get("delivery")
    report["router_health"] = {
        "healthy": health["healthy"],
        "delivery": delivery if isinstance(delivery, bool) else None,
        "reason": shown(health["reason"]) if isinstance(health.get("reason"), str) else None,
    }
    now = time.time()
    wake_id = "test-wake-%s-%s" % (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
        os.urandom(4).hex(),
    )
    body = _test_wake_body(seat, wake_id, topic, letter, Z.realm_url(rt.env), now)
    data = A.routine_bytes(body)
    hidden.extend(value for value in (target.header_value(data),) if len(value) >= 4)
    hidden.sort(key=len, reverse=True)
    report["request"] = body
    result = A.RoutineRunner(retries=0, keep_response=True).deliver(
        target, data, idempotency_key=wake_id
    )
    report["http_status"] = result.status
    parsed = _json_object(result.response)
    answer: dict[str, Any] = {}
    if parsed is not None:
        # The answer is the router's own text:  only its dry-run fields are kept, as scalars or
        # lists of scalars, and each string is scrubbed of every value this command holds.
        answer = {
            key: shown(parsed[key])
            for key in TEST_WAKE_ANSWER_FIELDS
            if key in parsed and isinstance(parsed[key], (*_SCALARS, list))
        }
    report["answer"] = answer
    dry_run = (
        result.accepted
        and answer.get("test") is True
        and answer.get("delivered") is False
        and answer.get("wake_id") == wake_id
    )
    picked = dry_run and bool(answer.get("session_id"))
    addressed = letter is None or answer.get("picked_letter") == letter
    passed = dry_run and picked and addressed
    report["dry_run"] = dry_run
    report["picked"] = picked
    report["addressed"] = dry_run and addressed
    if not result.accepted:
        report["error"] = scrub(result.error or "not accepted")
    lines = [
        "test-wake %s:  %s to the router in %s (host %s), %s with %s"
        % (seat, routine.method, routine.url_env, target.host, routine.auth, routine.key_env),
        "router health:  healthy %s;  delivery:  %s"
        % (
            "yes" if health["healthy"] else "no",
            "on"
            if delivery is True
            else "off, a real wake would be held"
            if delivery is False
            else "unknown",
        ),
        "wake_id: %s  channel: %s  topic: %s  letter: %s"
        % (wake_id, TEST_WAKE_CHANNEL, topic, letter or "none"),
        ("HTTP %d" % result.status if result.status is not None else "no HTTP answer")
        + ("" if result.accepted else " (%s)" % report["error"]),
    ]
    for key in TEST_WAKE_ANSWER_FIELDS:
        if key in answer and key not in ("test", "wake_id"):
            lines.append("  %s: %s" % (key, json.dumps(answer[key], ensure_ascii=False)))
    if passed:
        lines.append(
            "PASS: the router answered the dry run (picked %s, reason %s) and delivered nothing"
            % (answer.get("picked_letter") or answer.get("picked"), answer.get("reason") or "none")
        )
    elif dry_run and not picked:
        lines.append(
            "FAIL: the router answered the dry run and picked no chat (reason %s), so a real wake "
            "would reach nobody" % (answer.get("reason") or "none")
        )
    elif dry_run:
        lines.append(
            "FAIL: the router picked letter %s, not the addressed %s (letter_live %s)"
            % (
                answer.get("picked_letter") or "none",
                letter,
                json.dumps(answer.get("letter_live")),
            )
        )
    elif result.accepted:
        lines.append(
            "FAIL: the routine answered %s but not as the router's dry run (test: true, "
            "delivered: false, this wake_id)" % result.status
        )
    else:
        lines.append("FAIL: the router did not accept the dry run")
    return finish(passed, "\n".join(lines))


# --------------------------------------------------------------------------------------------
# wakes and inbox --local
# --------------------------------------------------------------------------------------------


def _age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 60:
        return "under a minute"
    minutes = int(seconds // 60)
    return "%dm" % minutes if minutes < 60 else "%dh%02dm" % (minutes // 60, minutes % 60)


def _held_line(summary: Any) -> str | None:
    """`held back 3 (oldest 42m; per_topic_per_hour 1, wakes_per_hour 2)`, or None when nothing is
    held back (heldback.summary)."""
    if not isinstance(summary, dict) or not (summary.get("count") or summary.get("evicted")):
        return None
    reasons = ", ".join("%s %d" % (k, v) for k, v in sorted((summary.get("reasons") or {}).items()))
    parts = ["oldest %s" % _age(summary.get("oldest_age"))] if summary.get("count") else []
    if reasons:
        parts.append(reasons)
    if summary.get("overflow"):
        parts.append("%d in overflow" % summary["overflow"])
    if summary.get("evicted"):
        parts.append("%d more lost past the caps" % summary["evicted"])
    return "held back %d (%s)" % (int(summary.get("count") or 0), "; ".join(parts))


def cmd_wakes(rt: Any, args: argparse.Namespace) -> int:
    root = _root(rt)
    seat = _seat(rt, args)
    if not seat:
        raise Z.UsageError("no seat; pass --seat or set AGENT_SEAT")
    paths = L.SeatPaths(root, seat)
    views = W.Ledger(paths.wakes).wakes()
    since = time.time() - float(args.since) * 3600 if args.since else 0
    rows = sorted(
        (v for v in views.values() if float(v.get("ts") or 0) >= since),
        key=lambda v: float(v.get("ts") or 0),
    )
    if getattr(args, "json", False):
        for view in rows:
            rt.out(json.dumps(view, ensure_ascii=False, default=str) + "\n")
        return 0
    held = _held_line(HELD.summary(paths, time.time()))
    if not rows:
        rt.out("no wakes for %s\n%s" % (seat, held + "\n" if held else ""))
        return 0
    for view in rows:
        where = (
            "DM"
            if view.get("type") == "private"
            else "#%s > %s"
            % (
                L.escape_line(str(view.get("channel") or "")),
                L.escape_line(str(view.get("topic") or "")),
            )
        )
        notes = (" catch-up" if view.get("catch_up") else "") + (
            " by %s" % view["by"] if view.get("state") == "surfaced" and view.get("by") else ""
        )
        rt.out(
            "%s  %-9s %s  triggers %s%s%s  %s%s\n"
            % (
                _when(view.get("ts")),
                view.get("state"),
                where,
                ",".join(str(i) for i in view.get("trigger_ids") or []),
                " (owner)" if view.get("owner") else "",
                notes,
                "action %s " % view["action"] if view.get("action") else "",
                "(%s)" % view["reason"] if view.get("reason") else "",
            )
        )
    if held:
        rt.out("%s now:  %s\n" % (seat, held))
    return 0


def cmd_inbox_local(rt: Any, args: argparse.Namespace) -> int:
    root = _root(rt)
    seat = _seat(rt, args)
    if not seat:
        raise Z.UsageError("no seat; set AGENT_SEAT or pass --as NAME")
    paths = L.SeatPaths(root, seat)
    cursor = L.read_json(paths.local_cursor).get("seq")
    cursor = cursor if isinstance(cursor, int) else 0
    items = [
        i
        for i in L.read_jsonl(paths.seat_inbox)
        if isinstance(i.get("seq"), int) and i["seq"] > cursor
    ]
    shown = [i for i in items if not i.get("delivered_to")][-args.limit :]
    surfaced = L.read_json(paths.owner_queue_meta).get("surfaced")
    surfaced = surfaced if isinstance(surfaced, int) else 0
    notes = [
        n
        for n in L.read_jsonl(paths.owner_queue)
        if isinstance(n.get("seq"), int) and n["seq"] > surfaced
    ]
    if getattr(args, "json", False):
        for row in notes:
            rt.out(json.dumps({"owner_queue": row}, ensure_ascii=False) + "\n")
        for row in shown:
            rt.out(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        if notes:
            note_items = [
                {
                    "id": n.get("trigger_ids"),
                    "kind": "owner-queue",
                    "class": "passive",
                    "channel": n.get("kind"),
                    "topic": n.get("title"),
                    "sender": "simple-chat daemon",
                    "time": _when(n.get("ts")),
                    "owner": bool(n.get("owner")),
                    "trigger_sender_id": n.get("sender_id"),
                    "content": "%s%s%s"
                    % (
                        n.get("text") or "",
                        ("\nrisk: " + str(n["risk"])) if n.get("risk") else "",
                        ("\nnote: " + n["note"]) if n.get("note") else "",
                    ),
                }
                for n in notes
            ]
            rt.out(
                "[simple-tools owner queue] %d item%s\n%s\n"
                % (
                    len(notes),
                    "" if len(notes) == 1 else "s",
                    L.wrap_block(note_items, L.new_nonce(), 600),
                )
            )
        if shown:
            rt.out(
                "[simple-chat inbox] %d item%s for %s\n%s\n%s\n%s\n"
                % (
                    len(shown),
                    "" if len(shown) == 1 else "s",
                    seat,
                    L.owner_line(shown),
                    L.SCREEN_LINE,
                    L.wrap_block(shown, L.new_nonce(), L.DRAIN_PER_MESSAGE),
                )
            )
        if not notes and not shown:
            rt.err("simple-chat: nothing new in the %s seat inbox" % seat)
    if not args.peek:
        if items:
            L.update_json(
                paths.local_cursor, lambda d: d.__setitem__("seq", max(i["seq"] for i in items))
            )
        if notes:
            L.update_json(
                paths.owner_queue_meta,
                lambda d: d.__setitem__("surfaced", max(n["seq"] for n in notes)),
            )
    return 0


# --------------------------------------------------------------------------------------------
# listener:  this seat's own records (AFC-330 part B)
# --------------------------------------------------------------------------------------------


def _fenced(rt: Any, title: str, items: list[dict[str, Any]], per_message: int = 600) -> None:
    """Items with Zulip-authored text, inside the listener's own untrusted fence."""
    rows = [
        dict(
            i,
            content=i.get("body") or "",
            id=i.get("id") if i.get("id") is not None else i.get("seq"),
        )
        for i in items
    ]
    rt.out("%s\n%s\n%s\n" % (title, L.SCREEN_LINE, L.wrap_block(rows, L.new_nonce(), per_message)))


def cmd_listener(rt: Any, args: argparse.Namespace) -> int:
    """Read and act on the listener's records for the caller's own seat only (the seat this session
    resolves:  AGENT_LAUNCH_SEAT, --as, AGENT_SEAT, then the Claude seat).  No Zulip login."""
    root = _root(rt)
    seat = _seat(
        rt,
        argparse.Namespace(
            as_seat=getattr(args, "as_seat", None), default_seat=getattr(args, "default_seat", None)
        ),
    )
    if not seat:
        raise Z.UsageError("no seat; set AGENT_SEAT or pass --as NAME")
    action = args.listener_action
    as_json = bool(getattr(args, "json", False))

    def emit(view: Any, text: str | None = None) -> int:
        if as_json or text is None:
            rt.out(json.dumps(view, ensure_ascii=False, default=str) + "\n")
        else:
            rt.out(text)
        return 0

    if action == "status":
        view = LR.status_view(root, seat)
        lines = [
            "seat %s%s" % (seat, " (paused)" if view["paused"] else ""),
            "seat inbox:   %d pending after cursor %d"
            % (view["inbox_pending"], view["inbox_cursor"]),
            "owner queue:  %d not surfaced" % view["owner_queue_pending"],
            "wakes (24h):  %d %s"
            % (view["wakes_24h"], ", ".join("%s %d" % kv for kv in view["wakes_by_state"].items())),
            "held back:    %d (%d lost past the caps)"
            % (view["held_back"], view["held_back_lost"]),
            "leases:       %d" % view["leases"],
            "chat nudges:  %d pending" % view["nudges_pending"],
        ]
        return emit(view, "\n".join(lines) + "\n")
    if action == "inbox":
        view = LR.inbox_view(
            root, seat, since_seq=args.since_seq, limit=args.limit, include_delivered=args.delivered
        )
        if as_json:
            return emit(view)
        if not view["items"]:
            rt.err(
                "simple-chat: nothing in the %s seat inbox after seq %s"
                % (seat, args.since_seq or view["cursor"])
            )
            return 0
        _fenced(
            rt,
            "[simple-chat listener inbox] %d item%s for %s (cursor %d, next_seq %s;  ack with "
            "simple-chat listener ack --seq %s)"
            % (
                len(view["items"]),
                "" if len(view["items"]) == 1 else "s",
                seat,
                view["cursor"],
                view["next_seq"],
                view["next_seq"],
            ),
            view["items"],
            L.DRAIN_PER_MESSAGE,
        )
        return 0
    if action == "pending-inbox":
        view = LR.pending_inbox_view(root, seat, after_seq=args.after_seq, limit=args.limit)
        if as_json:
            return emit(view)
        if not view["items"]:
            if view["count"]:
                rt.out(
                    "simple-chat: %d wake-worthy item%s pending for %s after seq %d "
                    "(latest_seq %d;  --limit 0 shows none)\n"
                    % (
                        view["count"],
                        "" if view["count"] == 1 else "s",
                        seat,
                        view["after_seq"],
                        view["latest_seq"],
                    )
                )
                return 0
            rt.err(
                "simple-chat: no wake-worthy items pending for %s after seq %d (latest_seq %d)"
                % (seat, view["after_seq"], view["latest_seq"])
            )
            return 0
        _fenced(
            rt,
            "[simple-chat listener pending-inbox] %d of %d pending item%s for %s after seq %d "
            "(latest_seq %d;  read-only, no cursor moved)"
            % (
                len(view["items"]),
                view["count"],
                "" if view["count"] == 1 else "s",
                seat,
                view["after_seq"],
                view["latest_seq"],
            ),
            view["items"],
            L.DRAIN_PER_MESSAGE,
        )
        return 0
    if action == "ack":
        return emit(
            {"seat": seat, "cursor": LR.ack_inbox(root, seat, args.seq)},
            "seat inbox cursor for %s is now %d\n"
            % (seat, LR.inbox_cursor(LR.seat_paths(root, seat))),
        )
    if action == "owner-queue":
        view = LR.owner_queue_view(root, seat, all_notes=args.all, limit=args.limit)
        if as_json or not view["items"]:
            if not view["items"] and not as_json:
                rt.err("simple-chat: nothing in the %s owner queue" % seat)
                return 0
            return emit(view)
        _fenced(
            rt,
            "[simple-tools owner queue] %d note%s for %s (%d not surfaced)"
            % (len(view["items"]), "" if len(view["items"]) == 1 else "s", seat, view["pending"]),
            view["items"],
        )
        return 0
    if action == "surface":
        value = LR.mark_surfaced(root, seat, args.seq)
        return emit(
            {"seat": seat, "surfaced": value},
            "owner queue for %s surfaced up to %d\n" % (seat, value),
        )
    if action == "wakes":
        view = LR.wakes_view(root, seat, since_hours=args.since, state=args.state, limit=args.limit)
        lines = [
            "%s  %-9s %s  triggers %s%s%s\n"
            % (
                _when(w.get("ts")),
                w.get("state"),
                "DM"
                if w.get("dm")
                else "#%s > %s"
                % (
                    L.escape_line(str(w.get("channel") or "")),
                    L.escape_line(str(w.get("topic") or "")),
                ),
                ",".join(str(i) for i in w.get("trigger_ids") or []),
                " (owner)" if w.get("owner") else "",
                "  (%s)" % L.escape_line(str(w["reason"])) if w.get("reason") else "",
            )
            for w in view["items"]
        ]
        return emit(view, "".join(lines) or "no wakes for %s\n" % seat)
    if action == "held-back":
        view = LR.held_back_view(root, seat, limit=args.limit)
        lines = [
            "%s  %s  %s  %s\n"
            % (
                i["message_id"],
                i["reason"],
                "DM"
                if i["dm"]
                else "#%s > %s"
                % (
                    L.escape_line(str(i.get("channel") or "")),
                    L.escape_line(str(i.get("topic") or "")),
                ),
                L.escape_line(str(i.get("sender_full_name") or "")),
            )
            for i in view["items"]
        ]
        return emit(
            view,
            "".join(lines)
            + "%d held back, %d lost past the caps\n" % (view["count"], view["lost"]),
        )
    if action == "clear-held":
        cleared = LR.clear_held(root, seat, args.id)
        return emit(
            {"seat": seat, "cleared": cleared},
            "cleared %d held-back entr%s\n" % (cleared, "y" if cleared == 1 else "ies"),
        )
    if action == "leases":
        view = LR.leases_view(root, seat)
        lines = [
            "%s  pending %d  topics %d%s\n"
            % (
                l["lease_id"],
                l["pending"],
                len(l["topics"]),
                "" if l["watcher_alive"] else "  (no watcher)",
            )
            for l in view["items"]
        ]
        return emit(view, "".join(lines) or "no leases for %s\n" % seat)
    if action in ("live", "drain"):
        view = (
            LR.live_view(root, seat, args.lease, limit=args.limit)
            if action == "live"
            else LR.drain(root, seat, args.lease)
        )
        if as_json:
            return emit(view)
        if not view["items"]:
            rt.err("simple-chat: nothing pending in lease %s" % view["lease_id"])
            return 0
        _fenced(
            rt,
            "[simple-chat listener %s] %d item%s from lease %s"
            % (
                action,
                len(view["items"]),
                "" if len(view["items"]) == 1 else "s",
                view["lease_id"],
            ),
            view["items"],
            L.DRAIN_PER_MESSAGE,
        )
        return 0
    if action == "nudges":
        view = LR.nudges_view(root, seat)
        lines = [
            "%s  %s  #%s > %s%s\n"
            % (
                i.get("id"),
                i.get("status"),
                L.escape_line(str(i.get("channel") or "")),
                L.escape_line(str(i.get("topic") or "")),
                "  (%s)" % L.escape_line(str(i["hold_reason"])) if i.get("hold_reason") else "",
            )
            for i in view["items"]
        ]
        return emit(
            view,
            "".join(lines)
            + "%d binding%s\n" % (len(view["bindings"]), "" if len(view["bindings"]) == 1 else "s"),
        )
    raise Z.UsageError("unknown listener action")


# --------------------------------------------------------------------------------------------
# Parser wiring (called from cli.build_parser)
# --------------------------------------------------------------------------------------------


def add_parsers(
    sub: Any, common: argparse.ArgumentParser, verbs: frozenset[str] | None = None
) -> None:
    """Register the listener/daemon verbs.  `verbs` keeps them out of a command that does not own
    them (today only `simple-chat` does); a verb outside it is registered into a null parser."""

    def add(name: str, summary: str, parent: Any = sub) -> argparse.ArgumentParser:
        if verbs is not None and parent is sub and name not in verbs:
            from . import cli  # local: cli imports this module

            return cli._NullParser()
        return parent.add_parser(name, parents=[common], help=summary, description=summary)

    if verbs is None or "attach" in verbs:
        sub.add_parser(
            "attach",
            add_help=False,
            help="lease topics for this session; hooks, rewake, --drain and --wait "
            "(see simple-chat attach --help)",
        )
    if verbs is None or "detach" in verbs:
        sub.add_parser(
            "detach",
            add_help=False,
            help="remove topics or this session's lease (see simple-chat detach --help)",
        )

    p = add("daemon", "the always-on listener: run, install, status, pause and resume")
    dsub = p.add_subparsers(dest="daemon_command", metavar="ACTION", required=True)
    q = add(
        "run",
        "run the listener in the foreground (what the LaunchAgent and the server container run)",
        dsub,
    )
    q.add_argument(
        "--wait-lock",
        action="store_true",
        help="wait for a listener already holding the state directory to stop, instead of exiting "
        "(the server container, for rolling redeploys)",
    )
    q = add(
        "install",
        "write the platform service and sample listener.toml; prints activation commands",
        dsub,
    )
    q.add_argument("--dry-run", action="store_true", help="only print what would change")
    q.add_argument(
        "--python",
        metavar="PATH",
        default=None,
        help="python 3.11+ for the service (default: %s)"
        % (systemd.DEFAULT_PYTHON if sys.platform.startswith("linux") else launchd.DEFAULT_PYTHON),
    )
    q.add_argument(
        "--bin", metavar="PATH", help="the simple-chat program (default: ~/.local/bin/simple-chat)"
    )
    q = add("uninstall", "remove the platform service; prints stop commands", dsub)
    q.add_argument("--dry-run", action="store_true", help="only print what would change")
    q = add(
        "status",
        "per bot: connected, last event, cursor; leases, wakes, budgets, pause, plugin",
        dsub,
    )
    _status_args(q)
    q = add("pause", "kill switch: stop wakes and live interrupts (capture continues)", dsub)
    q.add_argument("--seat", metavar="S", help="one seat (default: every seat)")
    q.add_argument("--wakes-only", action="store_true", help="stop headless wakes only")
    q.add_argument(
        "--defer",
        action="store_true",
        help="mark this pause as a qualified drain window: wakes defer instead of dropping, "
        "broadcast effects park durably, and the Linear hook refuses new events (AFC-652; "
        "fleet-wide only, cannot combine with --seat)",
    )
    q = add("resume", "lift a pause", dsub)
    q.add_argument("--seat", metavar="S", help="one seat (default: every pause)")
    add("reload", "re-read listener.toml (SIGHUP to the running listener)", dsub)
    q = add(
        "check-config",
        "would the listener take this candidate listener.toml?  Runs its own read, rules and seat "
        "partition check, and changes nothing",
        dsub,
    )
    q.add_argument("--file", metavar="PATH", required=True, help="the candidate file to check")
    q.add_argument(
        "--partition",
        metavar="PATH",
        help="the seat partition to judge by (default: this checkout's copy)",
    )
    q = add("chats", "discover, inspect, or manage existing-chat Zulip nudges", dsub)
    csub = q.add_subparsers(dest="chats_action", metavar="ACTION", required=True)
    c = csub.add_parser("catalog", parents=[common])
    c.add_argument("--limit", type=int, default=120)
    c = csub.add_parser("peek", parents=[common])
    c.add_argument("--chat-id", required=True)
    for action in ("list", "status"):
        c = csub.add_parser(action, parents=[common])
        c.add_argument("--target-seat", required=True, metavar="GROK|CURSOR|CODEX")
    c = csub.add_parser("bind", parents=[common])
    c.add_argument("--target-seat", required=True, metavar="GROK")
    c.add_argument("--channel", required=True)
    c.add_argument("--topic", required=True)
    c.add_argument("--session-id", required=True)
    c.add_argument("--cwd", required=True)
    c = csub.add_parser("unbind", parents=[common])
    c.add_argument("--target-seat", required=True, metavar="GROK|CURSOR|CODEX")
    c.add_argument("--channel", required=True)
    c.add_argument("--topic", required=True)
    c = csub.add_parser("retry", parents=[common])
    c.add_argument("--target-seat", required=True, metavar="GROK|CURSOR|CODEX")
    c.add_argument("--item-id", required=True)
    c = csub.add_parser("cancel", parents=[common])
    c.add_argument("--target-seat", required=True, metavar="GROK|CURSOR|CODEX")
    c.add_argument("--item-id", required=True)
    q = add("init", "pin owner_user_id and eligible_user_ids in listener.toml (asks first)", dsub)
    q.add_argument(
        "--seat",
        metavar="S",
        help="the bot to read the user list with (default: the Claude seat); its "
        "credentials come from --rc, ZULIP_RC, then its listener.toml source "
        "(enabled or not), and must be that seat's own bot",
    )
    q.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    q = add(
        "test-wake",
        'send the seat\'s router one dry-run wake ("test": true) and show its pick;  nothing is '
        "delivered, and the listener's side writes nothing (the router logs a test receipt).  "
        "It sends only when the routine's key variable is WAKE_ROUTER_KEY with auth = bearer, "
        "its URL path is "
        "/wake/<SEAT> and GET /health/<SEAT> there answers as the router.  Exit 0 needs the "
        "router's dry-run answer and a picked chat",
        dsub,
    )
    q.add_argument("--seat", metavar="S", required=True, help="a seat with a routine")
    q.add_argument(
        "--letter",
        metavar="XX",
        help="the session letter the wake addresses (optional;  without it the router picks as "
        "for an unaddressed mention);  with it, exit 0 also needs that letter picked",
    )
    q.add_argument(
        "--topic", metavar="T", help="the topic of the test wake (default: %s)" % TEST_WAKE_TOPIC
    )

    p = add("status", "listener status (same as daemon status)")
    _status_args(p)

    p = add(
        "listener",
        "this seat's own listener records:  status, inbox, pending-inbox, ack, owner-queue, surface, wakes, "
        "held-back, clear-held, leases, live, drain, nudges (no Zulip login)",
    )
    lsub = p.add_subparsers(dest="listener_action", metavar="ACTION", required=True)
    add("status", "counts:  seat inbox, owner queue, wakes, held back, leases, nudges, pause", lsub)
    q = add(
        "inbox",
        "the seat inbox after the acknowledged cursor (bodies are untrusted data); never moves it",
        lsub,
    )
    q.add_argument(
        "--since-seq", dest="since_seq", type=int, metavar="N", help="items after this seq instead"
    )
    q.add_argument("--limit", type=int, default=20, metavar="N")
    q.add_argument(
        "--delivered", action="store_true", help="also show items already delivered to a session"
    )
    q = add(
        "pending-inbox",
        "wake-worthy seat-inbox items after --after-seq (bodies are untrusted data); never moves any cursor",
        lsub,
    )
    q.add_argument(
        "--after-seq",
        dest="after_seq",
        type=int,
        default=0,
        metavar="N",
        help="items after this seq",
    )
    q.add_argument("--limit", type=int, default=20, metavar="N")
    q = add("ack", "move the seat-inbox cursor forward to --seq (never back)", lsub)
    q.add_argument("--seq", type=int, required=True, metavar="N")
    q = add("owner-queue", "notes the listener queued for the owner, after the surfaced mark", lsub)
    q.add_argument("--all", action="store_true", help="every note, surfaced ones included")
    q.add_argument("--limit", type=int, default=20, metavar="N")
    q = add("surface", "mark the owner queue surfaced up to --seq", lsub)
    q.add_argument("--seq", type=int, required=True, metavar="N")
    q = add("wakes", "the wake ledger, with filters", lsub)
    q.add_argument(
        "--since",
        type=float,
        default=24.0,
        metavar="HOURS",
        help="the last HOURS hours (default 24)",
    )
    q.add_argument(
        "--state", metavar="S", help="only wakes in this state (surfaced, dropped, held_back, ...)"
    )
    q.add_argument("--limit", type=int, default=30, metavar="N")
    q = add("held-back", "wakes held back for the budget, oldest first", lsub)
    q.add_argument("--limit", type=int, default=50, metavar="N")
    q = add("clear-held", "clear one held-back entry by its message id", lsub)
    q.add_argument("--id", type=int, required=True, metavar="MSGID")
    add("leases", "this seat's leases and their live-inbox backlog", lsub)
    q = add("live", "the undelivered items of one lease's live inbox (never marks them)", lsub)
    q.add_argument("--lease", required=True, metavar="ID", help="a lease id or its prefix")
    q.add_argument("--limit", type=int, default=50, metavar="N")
    q = add(
        "drain",
        "take every undelivered item of one lease's live inbox (marks them delivered)",
        lsub,
    )
    q.add_argument("--lease", required=True, metavar="ID", help="a lease id or its prefix")
    add("nudges", "this seat's chat bindings and nudge queue", lsub)

    p = add("wakes", "list the wake ledger")
    p.add_argument(
        "--seat", metavar="S", help="seat (default: env AGENT_SEAT, then the Claude seat)"
    )
    p.add_argument("--since", type=float, metavar="HOURS", help="only the last HOURS hours")


def _status_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--probe",
        action="store_true",
        help="post in #sandbox > listener probe and check it is routed",
    )
    p.add_argument("--seat", metavar="S", help="seat for --probe and the inbox count")
    p.add_argument(
        "--probe-timeout", type=float, default=30.0, metavar="SECONDS", help=argparse.SUPPRESS
    )
