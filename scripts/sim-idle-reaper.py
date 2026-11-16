#!/usr/bin/env python3
"""Shut down iOS Simulators that have sat booted and idle (LaunchAgent com.jay.sim-idle-reaper).

Every seat's backstop for the owner rule "never end a turn with a simulator booted" (2026-10-09).
Claude Code sessions also shut down their own boots at turn end (scripts/hooks/sim-session-hook.py);
this job catches every other seat, crashed sessions, and boots no hook could attribute.

Each run lists the booted devices (`xcrun simctl list devices booted -j`) and SKIPS a device when
any of these holds, cheapest check first:
  1. an xcodebuild, xctest or simctl process is running, or one whose arguments name an
     .xctestrun file (`pgrep`, PIDs only, never command lines):  a build, test or
     `ios-debug --console` may be using it.  A `simctl spawn <udid> log stream` does not count:
     the Claude desktop app keeps one open for every booted device;
     An `xcodebuild -version` invocation (the Xcode health server's liveness probe) does not
     count either:  its argv is compared exactly and discarded, never logged or stored.  Nor
     does an xcodebuild PID that exited before its argv could be read;
  2. Simulator.app is running:  someone may be watching;
  2b. Bitrig.app (`Bitrig`) is running:  up to 1 booted simulator (the most recently seen) is
      exempted so Bitrig can keep its single shared simulator alive across projects;
  3. a Claude session marked it watched (the iOS Simulator panel) in the last 12 hours;
  4. it has been seen booted for less than --min-age-minutes (default 60).  simctl exposes no boot
     time, so the first time this job sees a device booted is kept in state.json;
  5. a User app (`simctl listapps`, ApplicationType User) is running in it (`simctl spawn <udid>
     launchctl list` shows UIKitApplication:<bundle> with a pid).  A failed check counts as running.
Otherwise it runs `xcrun simctl shutdown <udid>`:  never `shutdown all`, never erase or delete.
A load guard runs before the idle pass:  when the 1-minute load average reaches 800
(the box is heading toward the 926 seen 2026-10-14), every considered simulator
(`--udid` filter respected, in any letter case) is shut down unconditionally and one alert is
posted to Zulip #Alerts, topic sim-idle-reaper, as the MA bot via the simple-chat CLI, untagged
(house format `SEVERITY • service • what • link`, severity SEVERE), rate-limited to one post per
hour.
A post that may have reached Zulip (posted, timed out, or an unclear error) starts the hour;
only a post that provably never left (CLI missing, usage or credential error) does not.
TestFlight ships (com.jay.ios-ship-now, ship-testflight.sh) archive for generic/platform=iOS and
boot no simulator;  their xcodebuild still trips skip 1 while it runs.

One log line per decision.  A lock file makes concurrent runs exit at once.  It also tidies the
hook's state:  PreToolUse snapshots older than an hour, session files older than 24 hours, and
session entries for devices that are no longer booted.

  python3 ~/apps/sim-idle-reaper.py --dry-run --verbose
  python3 ~/apps/sim-idle-reaper.py --udid <UDID>            # only consider this device
  tail -f ~/Library/Logs/sim-idle-reaper.log

State:  ~/Library/Application Support/sim-idle-reaper/ (state.json, guard-alert.json, sessions/,
pre/).
Tracked copy:  AI-Fleet-Coordinator/scripts/sim-idle-reaper.py.  Live:  ~/apps/sim-idle-reaper.py.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("SIM_REAPER_STATE_DIR") or os.path.join(
    HOME, "Library", "Application Support", "sim-idle-reaper"
)
XCRUN = os.environ.get("SIM_REAPER_XCRUN") or "/usr/bin/xcrun"
PGREP = "/usr/bin/pgrep"
PLUTIL = "/usr/bin/plutil"
DEFAULT_MIN_AGE_MIN = 60
WATCHED_FRESH_SEC = 12 * 3600
SESSION_MAX_AGE_SEC = 24 * 3600
PRE_MAX_AGE_SEC = 3600
BUSY_EXACT = ("xcodebuild", "xctest", "simctl", "bitrig")
BUSY_PATTERN = "xctestrun"
LOG_STREAM_PATTERN = r"simctl spawn [^ ]+ log stream"  # passive streamer, not use
LOAD_GUARD_TRIP = 800.0  # 1-min loadavg: emergency-shutdown considered sims before load nears 900
GUARD_ALERT_COOLDOWN_SEC = 3600  # at most one #Alerts post per hour while the guard keeps tripping
# The fleet chat CLI;  it resolves the MA bot credential itself, never a file default.  An explicit
# SIM_REAPER_SIMPLE_CHAT wins, then simple-chat on PATH, then this default location.
SIMPLE_CHAT_ENV = "SIM_REAPER_SIMPLE_CHAT"
SIMPLE_CHAT = os.path.join(HOME, ".local", "bin", "simple-chat")
ALERT_CHANNEL = "Alerts"  # the realm stream's own name;  Zulip matches stream names in any case
ALERT_TOPIC = "sim-idle-reaper"  # #alerts topic = service name (docs/protocols/zulip-fleet-guide)
ALERT_SEVERITY = "SEVERE"  # one of the guide's SEVERE / MODERATE / INFO
ALERT_TIMEOUT_SEC = 60
# A resource issue that is not "almost out of space" tags both Housekeepers and Jay, and only a nearly full
# disk tags @all (owner, Oct 29 2026;  docs/HOUSEKEEPER.md, Alert Tiers).  Load is never a space
# issue, so the guard's post wakes the two Housekeepers and Jay.  The mentions ride on a second line, so
# the #Alerts line still starts with its severity;  name|id is the exact form Zulip resolves.
RESOURCE_MENTIONS = (
    "@**BF-Housekeeper|1216054** @**GB-Housekeeper|1215533** @**Jay Wedgeworth|1211974**"
)
# simple-chat exits 2 (usage) and 3 (credential) before anything is sent:  provably unsent.
CHAT_UNSENT_EXITS = (2, 3)
UDID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
REAPER_LOG = os.path.join(HOME, "Library", "Logs", "sim-idle-reaper.log")


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def out(line: str) -> None:
    print(f"{stamp()}  {line}", flush=True)


def run(args: list, timeout: float = 60, stdin_text: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, input=stdin_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return -1, ""


def list_booted() -> list[dict] | None:
    """[{udid, name, runtime}] or None when simctl fails."""
    rc, text = run([XCRUN, "simctl", "list", "devices", "booted", "-j"], timeout=120)
    if rc != 0:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    devs = []
    for runtime, items in (doc.get("devices") or {}).items():
        for d in items or []:
            if isinstance(d, dict) and d.get("state") == "Booted" and d.get("udid"):
                udid = d["udid"]
                if not isinstance(udid, str) or not UDID_RE.fullmatch(udid):
                    # Every simctl argv, the state file and the #Alerts post take this value:
                    # skip it and never echo it.
                    out("reaper invalid simulator UDID from simctl; skipping that device")
                    continue
                devs.append({"udid": udid, "name": d.get("name", ""), "runtime": runtime})
    return devs


def pgrep_pids(args: list) -> set[str] | None:
    """PIDs from pgrep (never command lines), or None when pgrep itself fails."""
    rc, text = run([PGREP] + args, timeout=10)
    if rc not in (0, 1):
        return None
    return {p for p in text.split() if p != str(os.getpid())} if rc == 0 else set()


def pgrep_exact(name: str) -> int:
    pids = pgrep_pids(["-x", name])
    return -1 if pids is None else len(pids)


def _not_a_build(pid: str) -> bool:
    """True when xcodebuild PID is not a build:  its argv is exactly `xcodebuild -version`, or it
    has already exited.

    The Xcode health server (~/apps/xcode-health/xcode-health-server.py) runs
    `xcodebuild -version` as a liveness probe; that is not a build and must not
    mark the machine busy.  argv is compared and discarded here — never logged
    or stored — so the "PIDs only, never command lines" posture holds elsewhere.
    The probe can exit between the `pgrep` scan and the `ps` call, and `ps` then shows
    nothing:  a PID that pgrep no longer lists is not a build either.  When pgrep itself
    fails, the PID stays busy.
    """
    rc, text = run(["/bin/ps", "-o", "command=", "-p", pid], timeout=10)
    if rc != 0 or not text.strip():
        alive = pgrep_pids(["-x", "xcodebuild"])
        return alive is not None and pid not in alive
    parts = text.strip().split()
    return len(parts) == 2 and os.path.basename(parts[0]) == "xcodebuild" and parts[1] == "-version"


def busy_processes() -> list[str]:
    """Processes that may be using a simulator, as name x count ('?' = could not tell).

    The Claude desktop app runs a passive `simctl spawn <udid> log stream` for every booted
    device for as long as it is open (found 2026-10-10), so those simctl processes are not
    counted:  otherwise this job could never shut anything down while the app runs.
    """
    hits = []
    for name in BUSY_EXACT:
        pids = pgrep_pids(["-x", name])
        if pids and name == "xcodebuild":
            # The Xcode health server's `xcodebuild -version` probe is not a build, and neither
            # is a PID that exited before ps could read it.
            pids = {p for p in pids if not _not_a_build(p)}
        if pids and name == "simctl":
            streams = pgrep_pids(["-f", LOG_STREAM_PATTERN])
            pids = pids - streams if streams is not None else pids
        if pids is None:
            hits.append(f"{name}?")
        elif pids:
            hits.append(f"{name}x{len(pids)}")
    pids = pgrep_pids(["-f", BUSY_PATTERN])
    if pids is None:
        hits.append(f"{BUSY_PATTERN}?")
    elif pids:
        hits.append(f"{BUSY_PATTERN}x{len(pids)}")
    return hits


def user_bundles(udid: str) -> set[str] | None:
    rc, text = run([XCRUN, "simctl", "listapps", udid], timeout=60)
    if rc != 0:
        return None
    rc, js = run([PLUTIL, "-convert", "json", "-o", "-", "-"], timeout=30, stdin_text=text)
    if rc != 0:
        return None
    try:
        apps = json.loads(js)
    except ValueError:
        return None
    return {
        bid
        for bid, info in apps.items()
        if isinstance(info, dict) and info.get("ApplicationType") == "User"
    }


def running_ui_apps(launchctl_text: str) -> set[str]:
    """Bundle ids of UIKitApplication jobs that have a pid in `launchctl list` output."""
    running = set()
    for line in launchctl_text.splitlines():
        parts = line.split("\t") if "\t" in line else line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, label = parts[0].strip(), parts[2].strip()
        if label.startswith("UIKitApplication:") and pid.isdigit():
            running.add(label[len("UIKitApplication:") :].split("[", 1)[0])
    return running


def user_app_running(udid: str) -> tuple[bool, str]:
    """(protect?, detail).  Any failure protects the device."""
    bundles = user_bundles(udid)
    if bundles is None:
        return True, "listapps failed"
    rc, text = run([XCRUN, "simctl", "spawn", udid, "launchctl", "list"], timeout=60)
    if rc != 0:
        return True, "launchctl list failed"
    live = sorted(running_ui_apps(text) & bundles)
    if live:
        return True, "user app running: " + ",".join(live)
    return False, f"{len(bundles)} user app(s) installed, none running"


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, type(default)) else default
    except Exception:
        return default


def write_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def watched_udids(now: float) -> set[str]:
    """Devices a Claude session marked watched (iOS Simulator panel) within WATCHED_FRESH_SEC."""
    d = os.path.join(STATE_DIR, "sessions")
    found: set[str] = set()
    try:
        names = os.listdir(d)
    except OSError:
        return found
    for fn in names:
        if not fn.endswith(".json"):
            continue
        doc = load_json(os.path.join(d, fn), {})
        for udid, t in (doc.get("watched") or {}).items():
            try:
                if now - float(t) < WATCHED_FRESH_SEC:
                    found.add(udid)
            except (TypeError, ValueError):
                continue
    return found


def tidy_hook_state(booted: set[str], now: float, dry: bool, verbose: bool) -> None:
    """Prune stale hook files.  Takes the hook's sessions.lock; skips quietly if it is busy."""
    pre = os.path.join(STATE_DIR, "pre")
    for fn in os.listdir(pre) if os.path.isdir(pre) else []:
        p = os.path.join(pre, fn)
        try:
            if now - os.path.getmtime(p) > PRE_MAX_AGE_SEC:
                if verbose:
                    out(f"tidy {'would remove' if dry else 'remove'} stale snapshot {fn}")
                if not dry:
                    os.remove(p)
        except OSError:
            pass
    sdir = os.path.join(STATE_DIR, "sessions")
    if not os.path.isdir(sdir) or dry:
        return
    try:
        lock = open(os.path.join(STATE_DIR, "sessions.lock"), "a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    try:
        for fn in os.listdir(sdir):
            p = os.path.join(sdir, fn)
            if not fn.endswith(".json"):
                continue
            try:
                if now - os.path.getmtime(p) > SESSION_MAX_AGE_SEC:
                    os.remove(p)
                    if verbose:
                        out(f"tidy removed session file {fn} (older than 24h)")
                    continue
            except OSError:
                continue
            doc = load_json(p, {})
            udids = doc.get("udids") or {}
            keep = {u: e for u, e in udids.items() if u in booted}
            if keep != udids:
                doc["udids"] = keep
                if keep or doc.get("watched"):
                    write_json(p, doc)
                else:
                    os.remove(p)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def bitrig_exempt_udid(booted: list[dict], seen: dict[str, float], bitrig_app: int) -> str | None:
    """When Bitrig is running, exempt up to 1 booted simulator (most recently seen)."""
    if not bitrig_app or not booted:
        return None
    chosen = max(booted, key=lambda d: (seen.get(d["udid"], 0.0), d["udid"]))
    return chosen["udid"]


def load_guard_tripped() -> float | None:
    """1-min loadavg when it has reached the emergency trip point, else None."""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        return None
    return load1 if load1 >= LOAD_GUARD_TRIP else None


def _chat_reason(stderr: str | None) -> str:
    """The last non-empty stderr line of simple-chat (its classified diagnosis), shortened."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "no detail"


def fleet_alert(content: str) -> str:
    """Post one message to Zulip #Alerts as the MA bot via the simple-chat CLI.

    simple-chat resolves the seat credential itself (no credential file default in
    this script) and owns HTTPS/redirect/User-Agent handling.  Never raises.  The post goes
    out with --no-tag, so the line in #Alerts starts with its severity as the guide's
    `SEVERITY • service • what • link` format requires;  Zulip still shows the MA bot as sender.

    Returns "ok" (posted), "unsent" (provably never sent:  the CLI could not start, or it
    exited with a usage or credential error) or "unknown" (anything else, such as a timeout,
    a network error or a refusal that may follow a send).  The post is never retried here.
    """
    chat = os.environ.get(SIMPLE_CHAT_ENV) or shutil.which("simple-chat") or SIMPLE_CHAT
    argv = [chat, "--as", "MA", "post", "--no-tag"]
    argv += ["--channel", ALERT_CHANNEL, "--topic", ALERT_TOPIC, content + "\n" + RESOURCE_MENTIONS]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=ALERT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        out(
            f"load-guard: fleet alert outcome unknown (no answer in {ALERT_TIMEOUT_SEC}s); "
            "counted as posted for the cooldown"
        )
        return "unknown"
    except (OSError, ValueError) as exc:  # the CLI never started
        out(f"load-guard: fleet alert not sent ({type(exc).__name__})")
        return "unsent"
    except Exception as exc:  # alerting must never break the guard itself
        out(
            f"load-guard: fleet alert outcome unknown ({type(exc).__name__}); "
            "counted as posted for the cooldown"
        )
        return "unknown"
    if p.returncode == 0:
        out("load-guard: fleet alert posted")
        return "ok"
    reason = _chat_reason(p.stderr)
    if p.returncode in CHAT_UNSENT_EXITS:
        out(f"load-guard: fleet alert not sent (rc={p.returncode}: {reason})")
        return "unsent"
    out(
        f"load-guard: fleet alert outcome unknown (rc={p.returncode}: {reason}); "
        "counted as posted for the cooldown"
    )
    return "unknown"


def _guard_alert_marker() -> str:
    # One fixed marker per machine, whatever --state names, so every run shares one cooldown.
    return os.path.join(STATE_DIR, "guard-alert.json")


def guard_alert_due() -> bool:
    """True when no guard alert was posted (or may have been) within the cooldown window."""
    try:
        last = float(load_json(_guard_alert_marker(), {}).get("last_alert", 0))
    except (TypeError, ValueError):
        return True
    return (time.time() - last) >= GUARD_ALERT_COOLDOWN_SEC


def guard_alert_recorded(outcome: str) -> None:
    write_json(_guard_alert_marker(), {"last_alert": time.time(), "outcome": outcome})


def decide(
    dev: dict,
    first_seen: float,
    now: float,
    busy: list[str],
    sim_app: int,
    watched: set[str],
    min_age_sec: float,
    check_apps=None,
    bitrig_exempt: set[str] | None = None,
) -> tuple[str, str]:
    """('skip' | 'shutdown', reason) for one booted device, cheapest check first."""
    if busy:
        return "skip", "busy: " + ",".join(busy)
    if sim_app:
        return "skip", "Simulator.app is open"
    if bitrig_exempt and dev["udid"] in bitrig_exempt:
        return "skip", "Bitrig is running (1 simulator exempt)"
    if dev["udid"] in watched:
        return "skip", "watched in the iOS Simulator panel"
    age = now - first_seen
    if age < min_age_sec:
        return "skip", f"booted-seen {int(age // 60)}m < {int(min_age_sec // 60)}m"
    protect, detail = (check_apps or user_app_running)(dev["udid"])
    if protect:
        return "skip", detail
    return "shutdown", f"idle, booted-seen {int(age // 60)}m, {detail}"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--dry-run", action="store_true", help="decide and log, shut nothing down, write no state"
    )
    ap.add_argument("--verbose", action="store_true", help="also log guard detail and housekeeping")
    ap.add_argument(
        "--udid", action="append", default=[], help="only consider this device (repeatable)"
    )
    ap.add_argument(
        "--min-age-minutes",
        type=float,
        default=float(os.environ.get("SIM_IDLE_REAPER_MIN_AGE_MIN") or DEFAULT_MIN_AGE_MIN),
    )
    ap.add_argument(
        "--state",
        default=os.path.join(STATE_DIR, "state.json"),
        help="first-seen state file (udid -> epoch)",
    )
    args = ap.parse_args(argv)
    mode = "dry-run" if args.dry_run else "live"
    for u in args.udid:
        if not UDID_RE.fullmatch(u):
            ap.error(f"--udid {u!r} is not a simulator UDID")
    # UDID_RE takes either case and simctl reports uppercase:  compare in one case.
    wanted = {u.upper() for u in args.udid}

    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "reaper.lock"), "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        out("reaper another run holds the lock; exiting")
        return 0

    booted = list_booted()
    if booted is None:
        out("reaper ERROR simctl list failed; nothing done")
        return 0
    now = time.time()
    state = load_json(args.state, {})
    seen = {}
    for d in booted:
        try:
            seen[d["udid"]] = float(state.get(d["udid"], now))
        except (TypeError, ValueError):
            seen[d["udid"]] = now
    if not args.dry_run:
        write_json(args.state, seen)  # drops devices that are no longer booted

    trip = load_guard_tripped()
    targets = [d for d in booted if not wanted or d["udid"].upper() in wanted]
    if trip is not None:
        if not targets:
            out(f"load-guard TRIP load1={trip:.0f}: no simulators considered, nothing to shut down")
        elif args.dry_run:
            out(
                "load-guard WOULD-TRIP load1=%.0f: would shut down %d considered sim(s): %s"
                % (trip, len(targets), ", ".join(f'{d["udid"]} "{d["name"]}"' for d in targets))
            )
        else:
            out(f"load-guard TRIP load1={trip:.0f}: shutting down {len(targets)} considered sim(s)")
            shut = []
            done = 0
            for d in targets:
                rc, _ = run([XCRUN, "simctl", "shutdown", d["udid"]], timeout=120)
                label = f'{d["udid"]} "{d["name"]}"'
                out(f"load-guard SHUTDOWN {label}: rc={rc}")
                shut.append(f"{label} rc={rc}")
                if rc == 0:
                    done += 1
                    seen.pop(d["udid"], None)
            write_json(args.state, seen)
            if guard_alert_due():
                what = (
                    f"load guard tripped: 1-min load {trip:.0f} >= {LOAD_GUARD_TRIP:.0f}, "
                    f"shut down {done} of {len(targets)} simulator(s): " + "; ".join(shut)
                )
                outcome = fleet_alert(f"{ALERT_SEVERITY} • {ALERT_TOPIC} • {what} • {REAPER_LOG}")
                if outcome != "unsent":  # it may be in #Alerts:  never post it twice in the hour
                    guard_alert_recorded(outcome)
            else:
                out("load-guard: alert cooldown active, skipping chat report")
            return 0  # emergency handled; skip the normal idle pass this run

    if args.verbose:
        out(f"reaper {mode}: {len(booted)} booted, {len(targets)} considered")
    if targets:
        busy = busy_processes()
        sim_app = pgrep_exact("Simulator")
        bitrig_app = max(pgrep_exact("Bitrig"), pgrep_exact("bitrig"), 0)
        exempt_udid = bitrig_exempt_udid(booted, seen, bitrig_app)
        bitrig_exempt = {exempt_udid} if exempt_udid else set()
        watched = watched_udids(now)
        for d in targets:
            action, reason = decide(
                d,
                seen[d["udid"]],
                now,
                busy,
                sim_app,
                watched,
                args.min_age_minutes * 60,
                bitrig_exempt=bitrig_exempt,
            )
            label = f'{d["udid"]} "{d["name"]}"'
            if action == "skip":
                out(f"reaper SKIP {label}: {reason}")
                continue
            if args.dry_run:
                out(f"reaper WOULD-SHUTDOWN {label}: {reason}")
                continue
            rc, _ = run([XCRUN, "simctl", "shutdown", d["udid"]], timeout=120)
            out(f"reaper SHUTDOWN {label}: {reason} rc={rc}")
            if rc == 0:
                seen.pop(d["udid"], None)
        if not args.dry_run:
            write_json(args.state, seen)
    tidy_hook_state({d["udid"] for d in booted}, now, args.dry_run, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
