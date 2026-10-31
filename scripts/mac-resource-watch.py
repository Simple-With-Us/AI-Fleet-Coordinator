#!/usr/bin/env python3
"""Sample Mac disk / RAM / CPU and optionally clean + wake Housekeeper.

Launchd: com.jay.mac-resource-watch (every 5 min).
On-demand: python3 ~/apps/mac-resource-watch.py --once
Never prints webhook secrets.  Load BOTFLEET_HOUSEKEEPER_WEBHOOK_* from the environment, else
from Infisical Fleet-OPS /botfleet-housekeeper-webhook (`fleet-secrets get`), and, if present,
GROK_BOT_HOUSEKEEPER_WEBHOOK_* from /grok-bot-housekeeper-webhook, without echoing values.
No credential file is read (AFC-359).  BotFleet is the primary wake.  Grok Bot is optional.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import ctypes
import fcntl
import json
import math
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".claude-disk-janitor"
STATE_FILE = STATE_DIR / "resource-watch-state.json"
LOG_FILE = HOME / "Library" / "Logs" / "mac-resource-watch.log"
FLEET_SECRETS = Path(os.environ.get("FLEET_SECRETS") or HOME / ".local" / "bin" / "fleet-secrets")
BF_SECRET_FOLDER = "/botfleet-housekeeper-webhook"
GB_SECRET_FOLDER = "/grok-bot-housekeeper-webhook"
CLEANUP = HOME / "apps" / "mac-auto-cleanup.sh"
JANITOR = HOME / ".claude-disk-janitor" / "janitor.sh"
# The exit codes that mean the cleanup itself ran.  75 is the cleanup
# script's "the local cleanup ran, only the remote maintenance hop failed"
# code, so a failed hop is not a second cleanup on an already pruned Mac.
CLEANUP_EXIT_CLEANED = (0, 75)

# Live defaults, the same table the housekeeper skill carries: 25G free to warn,
# 15G to be critical, a 15G drop, swap at 90% AND 32G absolute, load1 at 65.
# They were retuned down deliberately: the old 80G warn line ran
# `mac-auto-cleanup.sh --pressure` every 5 minutes back to back, and two of three
# runs ended with LESS free disk than they started.
DISK_FREE_WARN_GB = float(os.environ.get("RESOURCE_DISK_WARN_GB", "25"))
DISK_FREE_CRIT_GB = float(os.environ.get("RESOURCE_DISK_CRIT_GB", "15"))
DISK_DROP_ALERT_GB = float(os.environ.get("RESOURCE_DISK_DROP_GB", "15"))
SWAP_USED_PCT = float(os.environ.get("RESOURCE_SWAP_USED_PCT", "90"))
# Absolute used swap, not the size of the store.  macOS grows the store on demand, so
# swap_used_pct sits near 90% whether 2G or 20G is actually paged out.
SWAP_USED_GB = float(os.environ.get("RESOURCE_SWAP_USED_GB", "32"))
LOAD1_THRESHOLD = float(os.environ.get("RESOURCE_LOAD1", "65"))
CPU_ENABLED = os.environ.get("RESOURCE_CPU", "1") not in {"0", "false", "no"}
COOLDOWN_SEC = int(os.environ.get("RESOURCE_COOLDOWN_SEC", "14400"))
# 2026-09-01 (Claude): cleanup had NO cooldown -- only the webhook did.  With disk
# hovering under the warn line this ran mac-auto-cleanup.sh --pressure every
# 5 min, back to back.  Measured: each run grew swap 3.6-7.3G (CleanMyMac purge +
# `optimize ram` force pages out) and 2 of 3 runs ended with LESS free disk than
# they started.  Cure was worse than the disease.  The cooldowns are 2h between
# cleanup runs, 30m when the disk is critical, and 4h for the webhook.
CLEAN_COOLDOWN_SEC = int(os.environ.get("RESOURCE_CLEAN_COOLDOWN_SEC", "7200"))
CLEAN_COOLDOWN_CRIT_SEC = int(os.environ.get("RESOURCE_CLEAN_COOLDOWN_CRIT_SEC", "1800"))
LOCK_FILE = STATE_DIR / "resource-watch.lock"
LOCK_STALE_SEC = int(os.environ.get("RESOURCE_LOCK_STALE_SEC", "1800"))
# The lock every reclaiming scheduler shares, so two of them never reclaim at once:
# mac-cleanup (4h), a sibling reclaimer's robotic vacuum (300s), the BotFleet routines, the
# resource webhook and a live bot thread.  It is a directory, taken with mkdir, and
# stale after 120 minutes -- the same lock and the same age mac-auto-cleanup.sh uses.
HOUSEKEEPER_LOCK = STATE_DIR / ".housekeeper.lock"
HOUSEKEEPER_LOCK_STALE_SEC = int(os.environ.get("RESOURCE_HOUSEKEEPER_LOCK_STALE_SEC", "7200"))

# macOS renamex_np flag: fail the rename when the destination already exists.
# This is the atomic no-replace primitive the housekeeper lock needs.
_RENAME_EXCL = 0x00000004
_RENAMEX_UNSET = object()
_RENAMEX_NP = _RENAMEX_UNSET

# The inode of the lock directory this process created.  The release only removes
# that directory, so a lock that was taken over as stale while this process was
# running is never dropped from under its new holder.
_HOUSEKEEPER_LOCK_INODE: int | None = None


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        raw = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(raw) > 800:
            LOG_FILE.write_text("\n".join(raw[-800:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.check_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def sample() -> dict:
    df = _run(["/bin/df", "-k", "/System/Volumes/Data"]) or _run(["/bin/df", "-k", "/"])
    disk_free_gb = disk_used_pct = 0.0
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            avail_k = float(parts[3])
            cap = parts[4].rstrip("%")
            disk_free_gb = avail_k / 1024 / 1024
            try:
                disk_used_pct = float(cap)
            except ValueError:
                disk_used_pct = 0.0
            break

    vm = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    swap_total_gb = swap_used_gb = 0.0
    m = re.search(r"total\s*=\s*([\d.]+)M.*?used\s*=\s*([\d.]+)M", vm)
    if m:
        swap_total_gb = float(m.group(1)) / 1024
        swap_used_gb = float(m.group(2)) / 1024
    swap_used_pct = (swap_used_gb / swap_total_gb * 100) if swap_total_gb else 0.0

    up = _run(["/usr/bin/uptime"])
    load1 = load5 = load15 = 0.0
    lm = re.search(r"load averages?:\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", up)
    if lm:
        load1, load5, load15 = (float(lm.group(i)) for i in (1, 2, 3))

    return {
        "at": int(time.time()),
        "disk_free_gb": round(disk_free_gb, 2),
        "disk_used_pct": round(disk_used_pct, 1),
        "swap_total_gb": round(swap_total_gb, 2),
        "swap_used_gb": round(swap_used_gb, 2),
        "swap_used_pct": round(swap_used_pct, 1),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
    }


def evaluate(s: dict, prev_free: float | None) -> list[dict]:
    hits: list[dict] = []
    free = s["disk_free_gb"]
    if free <= DISK_FREE_CRIT_GB:
        hits.append(
            {
                "metric": "disk_free_gb",
                "severity": "critical",
                "threshold": DISK_FREE_CRIT_GB,
                "cmp": "below",
                "value": free,
            }
        )
    elif free <= DISK_FREE_WARN_GB:
        hits.append(
            {
                "metric": "disk_free_gb",
                "severity": "warn",
                "threshold": DISK_FREE_WARN_GB,
                "cmp": "below",
                "value": free,
            }
        )
    if prev_free is not None:
        drop = prev_free - free
        if drop >= DISK_DROP_ALERT_GB:
            hits.append(
                {
                    "metric": "disk_drop_gb",
                    "severity": "warn",
                    "threshold": DISK_DROP_ALERT_GB,
                    "cmp": "above",
                    "value": round(drop, 2),
                }
            )
    # NOT `or swap_total_gb >= SWAP_TOTAL_GB`.  macOS grows the swap store on demand,
    # so once it passed 8G that disjunct was true forever and every tick reported a hit.
    # Absolute used-swap is the real signal; the store's size is not.
    if s["swap_used_pct"] >= SWAP_USED_PCT and s["swap_used_gb"] >= SWAP_USED_GB:
        hits.append(
            {
                "metric": "swap",
                "severity": "critical" if s["swap_used_pct"] >= 90 else "warn",
                "threshold": SWAP_USED_PCT,
                "cmp": "above",
                "value": s["swap_used_pct"],
                "swap_used_gb": s["swap_used_gb"],
                "swap_total_gb": s["swap_total_gb"],
            }
        )
    if CPU_ENABLED and s["load1"] >= LOAD1_THRESHOLD:
        hits.append(
            {
                "metric": "load_1m",
                "severity": "warn",
                "threshold": LOAD1_THRESHOLD,
                "cmp": "above",
                "value": s["load1"],
            }
        )
    return hits


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _fleet_secret(name: str, folder: str) -> str:
    """One Infisical value via `fleet-secrets get`, or "" when absent or unreachable.  Never logs it."""
    if not FLEET_SECRETS.is_file():
        return ""
    try:
        proc = subprocess.run(
            [str(FLEET_SECRETS), "get", name, "--path", folder],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _read_webhook(folder: str, url_key: str, secret_key: str) -> tuple[str, str]:
    """Environment first, then Infisical `fleet-secrets get` (folder)."""
    url = os.environ.get(url_key, "").strip() or _fleet_secret(url_key, folder)
    secret = os.environ.get(secret_key, "").strip() or _fleet_secret(secret_key, folder)
    return url, secret


def load_webhooks() -> list[tuple[str, str, str]]:
    """Return list of (name, url, secret).  Never print url/secret."""
    hooks: list[tuple[str, str, str]] = []
    bf_url, bf_secret = _read_webhook(
        BF_SECRET_FOLDER, "BOTFLEET_HOUSEKEEPER_WEBHOOK_URL", "BOTFLEET_HOUSEKEEPER_WEBHOOK_SECRET"
    )
    if bf_url and bf_secret:
        hooks.append(("BotFleet", bf_url, bf_secret))
    # Optional second wake;  no Infisical folder exists for it yet, so this is skipped unless the
    # environment or a future /grok-bot-housekeeper-webhook folder supplies both names.
    gb_url, gb_secret = _read_webhook(
        GB_SECRET_FOLDER, "GROK_BOT_HOUSEKEEPER_WEBHOOK_URL", "GROK_BOT_HOUSEKEEPER_WEBHOOK_SECRET"
    )
    if gb_url and gb_secret:
        hooks.append(("GrokBot", gb_url, gb_secret))
    return hooks


def _housekeeper_owner_path() -> Path:
    """The identity file that sits BESIDE the lock directory.

    It cannot live inside the lock directory: the lock is released with a
    plain rmdir, and rmdir refuses a directory that has anything in it.  A file
    inside the lock would make the release silently fail and leak the lock
    forever.  This is the same path an equivalent lock helper in a sibling
    repository uses, so the two read each other's owner file.
    """
    return HOUSEKEEPER_LOCK.with_name(HOUSEKEEPER_LOCK.name + ".owner")


def _housekeeper_guard_path() -> Path:
    """The shared guard over the housekeeper protocol:  a SIBLING of the lock.

    Every implementation of this protocol -- this watcher, the cleanup script
    in this repo (scripts/mac-auto-cleanup.sh) and an equivalent lock helper
    in a sibling repository -- serializes the critical sections of the lock
    with an exclusive flock on this ONE file:  the ownership validation, the
    stale takeover, the removal, the mkdir, the owner-file publication and
    every release path.

    It is never inside the lock directory, because the lock is released with a
    plain rmdir and rmdir refuses a directory that has anything in it.  It is
    never deleted either:  deleting it would split the flock, and two
    processes could each hold a different inode and both believe they hold the
    guard.
    """
    return HOUSEKEEPER_LOCK.with_name(HOUSEKEEPER_LOCK.name + ".guard")


# The guard serializes every implementation of this protocol against every
# other one, so it has to be a real inter-process lock and not an in-process
# one:  the owner file is shared state that any process can write, and the
# check and the unlink in the clear have to be atomic against another PROCESS
# too.
#
# The RLock serializes the threads of THIS process and is re-entrant, so a
# guarded section that calls another guarded function cannot deadlock on
# itself.  The flock is what serializes against OTHER processes.
_GUARD_LOCK = threading.RLock()
_GUARD_FD: int | None = None
_GUARD_DEPTH = 0


class _UnsafeStateDirError(OSError):
    """The state directory is not a real, non-symlink directory.

    It is an OSError so that the callers which already swallow OSError -- the
    read, the write and the clear of the owner record -- keep treating it as
    "the state is unusable", but it is its OWN type so a caller that has to
    tell this refusal apart from a genuine I/O failure can.  A permission
    error, a full disk or a cross-device rename says the setup is broken;
    this says the guard cannot be trusted, and only the second one means the
    cleanup must be skipped.
    """


@contextmanager
def _housekeeper_guard():
    """Hold the shared housekeeper guard for the duration of the block.

    The guard is released by closing the descriptor, and it is released on
    every path out of the block, exceptions included.
    """
    global _GUARD_FD, _GUARD_DEPTH
    with _GUARD_LOCK:
        if _GUARD_DEPTH == 0:
            path = _housekeeper_guard_path()
            parent = path.parent
            if parent.is_symlink():
                # A symlinked state directory can REDIRECT the shared guard:
                # the flock would then be taken on a file somewhere else
                # entirely, and two implementations would not be sharing one
                # guard.  Refuse it.
                raise _UnsafeStateDirError(
                    f"refusing a symlinked housekeeper state directory: {parent}"
                )
            if not parent.is_dir():
                # The state directory is PRIVATE:  the guard and the owner
                # record are shared state that must not be world readable.
                try:
                    parent.mkdir(mode=0o700, parents=True)
                except FileExistsError:
                    # Another process created it between the check and here:
                    # it is not ours to change -- but it still has to be a real
                    # directory and not a symlink.  The EEXIST may just as
                    # well have been a competing actor creating a LINK at
                    # this path, and carrying on would take the guard through
                    # the redirected path the check above refused.
                    if parent.is_symlink() or not parent.is_dir():
                        raise _UnsafeStateDirError(
                            f"refusing a symlinked housekeeper state directory: {parent}"
                        )
                else:
                    # mkdir's mode is masked by the umask, so the private
                    # mode is set explicitly.  The chmod goes through the
                    # opened directory and not through the path, so it cannot
                    # follow a symlink that appeared in the meantime --
                    # O_NOFOLLOW is what makes that true for the LAST path
                    # element, which a plain open would still follow.
                    dir_fd = os.open(
                        str(parent),
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        os.fchmod(dir_fd, 0o700)
                    finally:
                        os.close(dir_fd)
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
            _GUARD_FD = fd
        _GUARD_DEPTH += 1
        try:
            yield
        finally:
            _GUARD_DEPTH -= 1
            if _GUARD_DEPTH == 0 and _GUARD_FD is not None:
                fd, _GUARD_FD = _GUARD_FD, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


# The exact shape of an owner record.  The owner file is shared state that
# any process can write, so a record is either exactly this -- the key set,
# the types and the bounds of every value -- or it is not an owner record at
# all.
_OWNER_KEYS = frozenset({"pid", "host", "acquired_at", "lock", "start"})

# The start time of the holder's process, as the kernel reports it.  Readers
# parse this fixed C-locale civil timestamp only to recognize legacy local
# tokens across a timezone change; the bound also limits hostile input size.
_OWNER_START_MAX = 128
_OWNER_ACQUIRED_AT_FS_SKEW_SEC = 2.0
_OWNER_ACQUIRED_AT_FUTURE_SKEW_SEC = 60.0


def _parse_housekeeper_owner(raw: str) -> dict | None:
    """The owner payload in a raw owner file, or None when it is not usable.

    The owner file is shared state that any process can write, so its shape is
    checked rather than coerced.  int() on a pid that is not a number raises,
    and a raise here would escape the release path and leak the lock.

    The record has to match the declared schema EXACTLY:  the key set, the
    type of every value and the bounds of every value.  A record that is
    missing a key, carries an unknown key, or holds a value of the wrong type
    or out of bounds is not an owner record, and handing one to the
    lock-ownership logic would let a malformed file decide who holds the lock.
    A malformed record is rejected here, and this function never raises.
    """
    try:
        owner = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(owner, dict):
        return None
    if set(owner) != _OWNER_KEYS:
        # A missing key and an unknown key are the same failure:  the record
        # is not the record this protocol publishes.
        return None
    pid = owner["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    host = owner["host"]
    if not isinstance(host, str) or not host:
        return None
    acquired_at = owner["acquired_at"]
    if isinstance(acquired_at, bool) or not isinstance(acquired_at, (int, float)):
        return None
    try:
        finite = math.isfinite(acquired_at)
    except OverflowError:
        # A hundreds-of-digits int is too large to convert to a float:  it
        # is not a timestamp this protocol publishes.
        return None
    if not finite:
        # json accepts NaN and Infinity as literals, so a finite bound is a
        # real check and not a formality.
        return None
    lock = owner["lock"]
    if not isinstance(lock, str) or not lock:
        return None
    start = owner["start"]
    if not isinstance(start, str) or not start or len(start) > _OWNER_START_MAX:
        return None
    return owner


def _owner_acquired_at_is_sane(
    acquired_at: float, lock_mtime: float, owner_mtime: float, *, now: float | None = None
) -> bool:
    """Whether acquired_at agrees with the lock and owner file timestamps."""
    if not math.isfinite(acquired_at):
        return False
    now = time.time() if now is None else now
    skew = _OWNER_ACQUIRED_AT_FS_SKEW_SEC
    return (
        lock_mtime - skew <= acquired_at <= owner_mtime + skew
        and acquired_at <= now + _OWNER_ACQUIRED_AT_FUTURE_SKEW_SEC
    )


def _read_housekeeper_owner() -> dict | None:
    """Read the owner file and parse it, under the shared guard.

    None means the file is gone or cannot be read as an owner record.  A
    holder that cannot read the file cannot know whose it is, and deleting it
    would let a peer fall back to the age rule and take over a lock that is
    still held, so a caller that gets None leaves the file alone.
    """
    try:
        with _housekeeper_guard():
            try:
                raw = _housekeeper_owner_path().read_text(encoding="utf-8")
            except OSError:
                # A missing file is an OSError too:  there is no owner to
                # name, so the file is left alone.
                return None
            except ValueError:
                # A file that is not UTF-8 raises UnicodeDecodeError, which is
                # a ValueError and NOT an OSError.  It must not escape the
                # release path:  an owner file nobody can read is left alone
                # like any other malformed one.
                return None
            return _parse_housekeeper_owner(raw)
    except OSError:
        return None


def _process_start_token(pid: int, *, utc: bool = False) -> str:
    """The C-locale start time of the process a pid names.

    A pid alone does not name a process:  the kernel hands the same number to
    somebody else once the original exits, and a reused pid that is ALIVE
    looks exactly like a holder that is still running.  The start time tells
    the two apart.

    Readers compare exact UTC tokens directly and parse a differing token
    only as the fixed English civil form emitted under LC_ALL=C.  That lets a
    reader recognize a legacy local token after the host timezone changes.

    An empty token means the start time could not be read at all.  A caller
    that cannot tell the holder from a reused pid must not conclude that the
    holder is gone.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    if utc:
        # Readers normalize to UTC.  Writers keep the existing local token
        # until every reader in the fleet accepts both forms.
        env["TZ"] = "UTC0"
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _c_lstart_epoch(token: str) -> float | None:
    """Parse the fixed English civil timestamp emitted under LC_ALL=C."""
    match = re.fullmatch(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"( {1,2}[1-9]| [12][0-9]| 3[01]) "
        r"([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9]) ([0-9]{4})",
        token,
    )
    if match is None:
        return None
    weekday, month_name, day_text, hour, minute, second, year = match.groups()
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    try:
        civil = time.struct_time(
            (
                int(year),
                months.index(month_name) + 1,
                int(day_text),
                int(hour),
                int(minute),
                int(second),
                0,
                0,
                0,
            )
        )
        epoch = calendar.timegm(civil)
        parsed = time.gmtime(epoch)
        if parsed[:6] != civil[:6] or parsed.tm_wday != weekdays.index(weekday):
            return None
        return float(epoch)
    except (ValueError, OverflowError):
        return None


def _owner_start_matches_current(
    start: str,
    current_utc: str,
    acquired_at: float,
    lock_mtime: float,
    owner_mtime: float,
) -> bool | None:
    """Return same/different, or None when token evidence is unusable.

    Tokens more than 24 hours apart cannot name the same process under any
    timezone change, so they mean a reused pid even when acquired_at cannot
    be trusted.  Within that window, a pre-migration local-time token can
    equal a later process's UTC token, so string equality alone is not
    decisive:  acquired_at rules out a process that started after this owner
    record was published, and equality is only the fallback when the tokens
    or acquired_at cannot be checked.
    """
    exact = start == current_utc
    old_epoch = _c_lstart_epoch(start)
    current_epoch = _c_lstart_epoch(current_utc)
    if old_epoch is None or current_epoch is None:
        return True if exact else None
    if abs(old_epoch - current_epoch) > 24 * 60 * 60:
        return False
    if not _owner_acquired_at_is_sane(acquired_at, lock_mtime, owner_mtime):
        return True if exact else None
    if current_epoch > acquired_at:
        return False
    return True


def _pid_alive(pid: int) -> bool:
    """Whether a pid names a running process.

    Every failure answers "alive" except a pid the kernel says does not
    exist:  a permission error means the process is there and merely not
    ours to signal, and treating that as dead would take over a lock that a
    live holder still owns.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _housekeeper_owner_is_gone(lock_mtime: float) -> bool:
    """Whether the owner file proves the holder of this lock has exited.

    This is what makes a lock whose release failed recoverable in one run
    instead of one stale window:  the owner file survives a failed removal,
    so the next caller can see that the pid which held the lock is gone and
    take the lock over at once, rather than waiting out the full two hours
    for a lock nobody holds.

    False means DO NOT TRUST THE OWNER FILE, and the caller falls back to
    the age rule.  That covers every case where the file cannot speak for
    the lock that is actually on disk:

      * it is missing  --  the shell holder in scripts/mac-auto-cleanup.sh
        writes none at all and recovers by the age rule (see the protocol
        note there), so a missing file is an ordinary state, not a fault;
      * it is malformed  --  it is shared state any process can write;
      * it names another host  --  a pid is only meaningful where it ran;
      * it is OLDER than the lock directory it sits beside  --  then it
        describes an earlier holder, not the lock we are looking at;
      * it names a pid whose start time cannot be read  --  then it cannot
        tell the holder from a pid the kernel has since handed to somebody
        else, and a record that cannot say which is not evidence of
        anything.
    """
    owner = _read_housekeeper_owner()
    if owner is None:
        return False
    if owner.get("host") != socket.gethostname():
        return False
    try:
        owner_mtime = _housekeeper_owner_path().stat().st_mtime
    except OSError:
        return False
    if owner_mtime <= lock_mtime:
        return False
    pid = owner["pid"]
    if not _pid_alive(pid):
        return True
    # The pid is ALIVE.  It is the holder only when it is the SAME process:
    # the kernel hands a pid to somebody else once the original exits, and a
    # reused pid looks exactly like a live holder.  Believing it would leave
    # a dead holder's lock in place until the whole stale window expires.
    current = _process_start_token(pid, utc=True)
    if not current:
        # The start time could not be read:  the record cannot speak for the
        # lock, and the age rule decides.
        return False
    same_holder = _owner_start_matches_current(
        owner["start"], current, owner["acquired_at"], lock_mtime, owner_mtime
    )
    return same_holder is False


def _write_housekeeper_owner() -> None:
    """Record who holds the lock, in the shape the sibling lock reads.

    The pid is what makes a killed holder recoverable: the next process to ask
    for the lock can see that the pid is gone and take over at once instead of
    waiting out the whole stale window.  The owner file is only a hint, so
    failing to write it must not fail the acquire; the lock itself is the
    mutex.  It is written AFTER the lock directory, so its mtime is newer and
    a reader can tell that it describes the current holder.  The start time
    travels with the pid, because a pid the kernel has since handed to
    somebody else would otherwise look exactly like a live holder.

    The write is ATOMIC:  a temp file in the same directory is written,
    flushed and fsynced, renamed over the owner file, and the directory is
    fsynced too.  Writing the owner file in place truncates it first, so a
    crash or a power loss can leave partial JSON behind, and the owner record
    is the safety metadata the lock-ownership logic reads.

    The write is under the shared guard, because a writer that interleaves
    with a reader can leave a half-written file behind.
    """
    start = _process_start_token(os.getpid())
    if not start:
        # Without the start time the record cannot tell the holder from a
        # reused pid, so none is written:  a missing owner file is an
        # ordinary state -- the shell holder writes none at all -- and the
        # age rule decides.  The lock itself is the mutex.
        return
    payload = {
        "pid": os.getpid(),
        # The host must be a non-empty string:  that is the declared shape of
        # the record, and a record this process writes must be one it can
        # read back.
        "host": socket.gethostname() or "unknown",
        "acquired_at": time.time(),
        "lock": str(HOUSEKEEPER_LOCK),
        # The start time of THIS process, so a reader can tell the holder
        # from a pid the kernel has since handed to somebody else.
        "start": start,
    }
    try:
        with _housekeeper_guard():
            owner_path = _housekeeper_owner_path()
            tmp_path = owner_path.with_name(f".{owner_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                # The temp file is PRIVATE (0600):  the owner record names a
                # pid and a host and sits in a shared directory.
                fd = os.open(
                    str(tmp_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, owner_path)
                dir_fd = os.open(
                    str(owner_path.parent),
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                # The temp file is removed on EVERY path out of here, so a
                # failed write leaves no debris next to the owner file.
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def _clear_housekeeper_owner(only_pid: int | None = None) -> None:
    """Remove the owner file, optionally only when it names a given pid.

    The pid check keeps one holder from deleting the identity of another: if
    our lock was taken over as stale while we were still running, the file now
    names somebody else and we leave it alone.

    The check and the unlink are ONE decision, taken under the shared guard.
    Between a read and an unlink a contender can replace the owner file with
    its own, and the unlink would then delete the NEW holder's identity; with
    the owner file gone a later reader falls back to the age rule and can take
    over a lock that is genuinely held.
    """
    try:
        with _housekeeper_guard():
            _clear_housekeeper_owner_locked(only_pid)
    except OSError:
        pass


def _clear_housekeeper_owner_locked(only_pid: int | None) -> None:
    """The clear itself, with the owner lock already held."""
    owner_path = _housekeeper_owner_path()
    if only_pid is not None:
        owner = _read_housekeeper_owner()
        if owner is None:
            # Malformed state is not proof that the file describes us, so it is
            # left alone rather than deleted:  a holder that cannot read the
            # file cannot know whose it is, and deleting it would let a peer
            # fall back to the age rule and take over a lock that is still
            # held.
            return
        if owner["pid"] != only_pid:
            return
    try:
        owner_path.unlink()
    except OSError:
        pass


def _renamex_np():
    """macOS's renamex_np, or None where it does not exist."""
    global _RENAMEX_NP
    if _RENAMEX_NP is _RENAMEX_UNSET:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fn = libc.renamex_np
        except (OSError, AttributeError):
            _RENAMEX_NP = None
        else:
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            _RENAMEX_NP = fn
    return _RENAMEX_NP


def _rename_exclusive(src: Path, dst: Path) -> bool:
    """Rename src to dst, refusing to replace anything already at dst.

    os.rename is not that primitive.  On POSIX a rename onto an existing EMPTY
    directory SUCCEEDS and replaces it, and the housekeeper lock marker is an
    empty directory, so a plain rename can replace a third reclaimer's fresh
    lock while that reclaimer still believes it holds the lock and sweeps at
    the same time.

    macOS has renamex_np with RENAME_EXCL, which is the atomic no-replace
    rename: the move and the refusal are one decision, so there is no window
    between a check and a rename for a peer to slip into.  Elsewhere the
    fallback checks and then renames, which is strictly weaker but still
    refuses an occupied destination.
    """
    fn = _renamex_np()
    if fn is not None:
        if fn(os.fsencode(str(src)), os.fsencode(str(dst)), _RENAME_EXCL) == 0:
            return True
        return False
    try:
        if dst.exists() or dst.is_symlink():
            return False
        os.rename(src, dst)
        return True
    except OSError:
        return False


def _restore_housekeeper_lock(aside: Path) -> bool:
    """Put a moved-aside lock back without replacing anybody else's lock.

    os.rename onto an existing directory does NOT always fail.  It fails when
    the destination is a non-empty directory, but it SUCCEEDS when the
    destination is an empty one, and the lock marker is an empty directory.
    So a rename straight onto the canonical path replaces a third reclaimer's
    fresh lock, and that reclaimer goes on believing it holds the lock while
    this one sweeps at the same time.

    Checking that the canonical path is free and then renaming is not enough
    either: the check and the rename are two steps, so a third reclaimer can
    take the canonical path in between and be silently replaced by the move.
    The restore therefore makes the move and the no-replace check ONE decision,
    with the atomic no-replace rename.  When the canonical path is occupied
    there is nothing safe to do: the moved-aside copy stays where it is and
    the caller reports the failure, because the alternative is clobbering a
    live holder.
    """
    return _rename_exclusive(aside, HOUSEKEEPER_LOCK)


def _acquire_housekeeper_lock() -> bool:
    """Take the lock every reclaiming scheduler shares, so two never reclaim at once.

    The lock is the directory `~/.claude-disk-janitor/.housekeeper.lock`, taken with
    mkdir exactly as mac-auto-cleanup.sh takes it, and stale after 120 minutes.  A
    caller that already holds it exports HOUSEKEEPER_LOCK_OWNER=1; then the lock is
    neither taken nor released here -- the caller keeps it for the whole run.

    A stale lock is taken over by MOVING it aside.  The rename is atomic, so
    only one caller can win the takeover, while a stat-rmdir-mkdir takeover is
    not: two reclaimers can both judge the lock stale, one removes it and
    creates a fresh one, and the other then removes that fresh lock and creates
    its own, so both believe they hold it.

    The staleness is decided AT THE CANONICAL PATH, before the move.  Moving a
    live peer's lock out of the way leaves the canonical path briefly absent,
    and a third reclaimer can mkdir it and sweep at the same time -- the double
    sweep this lock exists to prevent.

    The staleness decision consults the owner file only to REJECT an early
    takeover:  an owner file that names a dead pid on this host, and is newer
    than the lock directory it sits beside, proves the lock is abandoned and
    is taken over at once.  Every other case -- no owner file, a malformed
    one, another host's pid, or one older than the lock -- falls back to the
    age rule, because the shell holder writes no owner file and recovers by
    age (scripts/mac-auto-cleanup.sh).  The owner file is only ever touched
    by the write, the read and the clear, and all three run under the shared
    guard, so the file cannot be half-written or replaced under a reader.

    The whole section runs under the shared guard, so a takeover in another
    implementation of this protocol cannot slip between this one's check and
    its mutation.
    """
    if os.environ.get("HOUSEKEEPER_LOCK_OWNER") == "1":
        return True
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_housekeeper_guard())
        except _UnsafeStateDirError as exc:
            # The guard cannot be trusted, and without it there is no mutual
            # exclusion, so the cleanup is skipped rather than run unguarded.
            log(f"housekeeper lock unavailable ({exc}); skipping cleanup")
            return False
        # Every OTHER OSError here is a genuine failure of this setup -- no
        # permission, no space, a read-only filesystem -- and NOT "a peer
        # holds the lock".  It propagates, because reporting it as a busy lock
        # would hide a broken install behind a log line for ever.
        try:
            return _acquire_housekeeper_lock_locked()
        except BaseException:
            # The mkdir may already have succeeded, so a failure from here on
            # can leave a lock directory behind.  run_cleanup returns before
            # its try/finally, so nothing else would release it, and every
            # peer would read it as a live lock until it goes stale:  the
            # machine would lose reclaim for the whole window, exactly when it
            # is under the disk pressure that triggered the run.  Drop the
            # lock this process created, then let the error through.
            _drop_housekeeper_lock_created_here()
            raise


def _drop_housekeeper_lock_created_here() -> None:
    """Drop the lock directory, but only the one this process created.

    A lock that was taken over as stale while this process was running is not
    ours to remove:  dropping it would delete the new holder's lock while it
    is still working.  The inode recorded immediately after the mkdir is what
    tells the two apart.
    """
    global _HOUSEKEEPER_LOCK_INODE
    if _HOUSEKEEPER_LOCK_INODE is None:
        # No mkdir of ours ever succeeded, so there is nothing of ours here.
        return
    inode, _HOUSEKEEPER_LOCK_INODE = _HOUSEKEEPER_LOCK_INODE, None
    if _lock_inode() != inode:
        # A peer already replaced it, so that one is theirs.
        return
    try:
        _clear_housekeeper_owner(only_pid=os.getpid())
    except OSError:
        pass
    try:
        HOUSEKEEPER_LOCK.rmdir()
    except OSError:
        # Not the empty marker we made:  a lock directory with something in
        # it is not ours to take.  Leaving it is the safe outcome -- it goes
        # stale on the age rule like any other lock.
        pass


def _acquire_housekeeper_lock_locked() -> bool:
    """The acquire itself, with the shared guard already held.

    The guard is held across the whole section:  the mkdir, the staleness
    check, the takeover, the removal and the owner-file publication.
    """
    global _HOUSEKEEPER_LOCK_INODE
    # The state directory holds the lock and the owner record, so it is
    # created PRIVATE.  The guard has usually created it already, and an
    # existing directory keeps its own permissions.
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(2):
        try:
            HOUSEKEEPER_LOCK.mkdir()
        except FileExistsError:
            pass
        else:
            # The inode is recorded BEFORE the owner record is written:  the
            # write can fail, and a failure after the mkdir still has to be
            # able to drop the lock this process just created.
            _HOUSEKEEPER_LOCK_INODE = _lock_inode()
            _write_housekeeper_owner()
            return True
        try:
            lock_mtime = HOUSEKEEPER_LOCK.stat().st_mtime
        except OSError:
            lock_mtime = time.time()
            age = 0.0
        else:
            age = time.time() - lock_mtime
        # A FRESH lock is a live peer's unless its owner file proves the
        # holder is gone:  that is the case a failed removal leaves behind,
        # and waiting out the whole window for it is the two-hour wedge this
        # lock's owner file exists to remove.
        if age < HOUSEKEEPER_LOCK_STALE_SEC and not _housekeeper_owner_is_gone(lock_mtime):
            log(f"housekeeper lock held by a peer ({int(age)}s old); skipping cleanup")
            return False
        # The lock must be EMPTY: it is a marker, not a workspace.  A lock
        # directory with something in it is not ours to take, and moving it
        # aside would displace a lock we do not own.
        try:
            if any(HOUSEKEEPER_LOCK.iterdir()):
                log("housekeeper lock has contents; skipping cleanup")
                return False
        except OSError:
            pass
        log(f"stealing stale housekeeper lock ({int(age)}s old)")
        aside = HOUSEKEEPER_LOCK.with_name(
            f"{HOUSEKEEPER_LOCK.name}.stale-{os.getpid()}.{uuid.uuid4().hex}"
        )
        try:
            os.rename(HOUSEKEEPER_LOCK, aside)
        except OSError:
            # Another caller won the race, or the holder released the lock
            # between the mkdir and the rename.
            return False
        # The moved object may have been replaced between the staleness check
        # and the rename; rename preserves mtime, so re-read its age.
        try:
            moved_mtime = aside.stat().st_mtime
        except OSError:
            moved_mtime = time.time()
        moved_age = time.time() - moved_mtime
        # The owner file has to be consulted HERE as well as at the gate
        # above:  this re-read would otherwise restore a lock we just proved
        # the holder had released, because the age it re-reads is the same
        # fresh age that the owner file has already explained.
        if moved_age < HOUSEKEEPER_LOCK_STALE_SEC and not _housekeeper_owner_is_gone(moved_mtime):
            # Fresh after all: it belongs to a live peer.  Put it back.
            if not _restore_housekeeper_lock(aside):
                # The copy stays where it is, so name it:  an orphaned copy
                # is invisible otherwise, and the copies accumulate.
                log(f"failed to restore the housekeeper lock from {aside}; skipping cleanup")
            log("housekeeper lock is live after all; skipping cleanup")
            return False
        try:
            aside.rmdir()
        except OSError:
            # Not the empty marker we made: a lock directory with something in
            # it is not ours to take.  Put it back.
            if not _restore_housekeeper_lock(aside):
                log(f"failed to restore the housekeeper lock from {aside}; skipping cleanup")
            log("housekeeper lock has contents; skipping cleanup")
            return False
        try:
            HOUSEKEEPER_LOCK.mkdir()
        except FileExistsError:
            # A peer took the canonical path in the window between the rename
            # and here.  We do not hold the lock.
            log("housekeeper lock taken by a peer during takeover; skipping cleanup")
            return False
        except OSError:
            log("failed to create the housekeeper lock")
            return False
        _HOUSEKEEPER_LOCK_INODE = _lock_inode()
        _write_housekeeper_owner()
        return True
    return False


def _lock_inode() -> int | None:
    """The inode of the lock directory, or None when it cannot be read."""
    try:
        return HOUSEKEEPER_LOCK.stat().st_ino
    except OSError:
        return None


def _release_housekeeper_lock() -> None:
    """Release the lock, but only the one this process created.

    A lock that was taken over as stale while this process was running is not
    ours to remove: removing it would drop the new holder's lock while it is
    still working.  The inode recorded at acquire time tells the two apart.

    The whole section runs under the shared guard, so the ownership check and
    the removal are one decision:  a takeover in another implementation of
    this protocol cannot replace the canonical lock between the inode check
    and the rmdir and have the exiting holder delete the NEW holder's lock.
    """
    if os.environ.get("HOUSEKEEPER_LOCK_OWNER") == "1":
        return
    try:
        with _housekeeper_guard():
            _release_housekeeper_lock_locked()
    except OSError as exc:
        # The release runs in run_cleanup's finally:  a refusal must not
        # mask the cleanup's own outcome.
        log(f"housekeeper lock release failed ({exc})")


def _release_housekeeper_lock_locked() -> None:
    """The release itself, with the shared guard already held.

    The lock directory goes first and the owner file second.  The other
    order strands the lock:  a lock directory that survives the removal with
    no owner file falls back to the age rule, so every implementation waits
    the full stale window for a lock that nobody holds.  That is a two-hour
    wedge on every run, and it is exactly what a failed removal used to
    leave behind.

    Removing the directory first is the safe order in the other direction
    too:  the worst case is an owner file naming a process that has gone,
    and the next caller takes that over at once instead of waiting.
    """
    global _HOUSEKEEPER_LOCK_INODE
    if _HOUSEKEEPER_LOCK_INODE is not None and _lock_inode() != _HOUSEKEEPER_LOCK_INODE:
        return
    removed = False
    try:
        HOUSEKEEPER_LOCK.rmdir()
        removed = True
    except OSError:
        pass
    if removed:
        _clear_housekeeper_owner(only_pid=os.getpid())
    _HOUSEKEEPER_LOCK_INODE = None


def run_cleanup(pressure: bool) -> int:
    # Hold the shared housekeeper lock while the reclaim runs: mac-cleanup, a
    # sibling reclaimer's vacuum, the BotFleet routines and a live bot thread
    # all take it, so two of them never reclaim at once.
    if not _acquire_housekeeper_lock():
        return 1
    try:
        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get(
            "PATH", ""
        )
        # We hold the housekeeper lock (taken above); mark ourselves the owner so the
        # child scripts do not deadlock against it and skip themselves.
        env["HOUSEKEEPER_LOCK_OWNER"] = "1"
        args = ["/bin/bash", str(CLEANUP)]
        if pressure:
            args.append("--pressure")
            env["MAC_CLEANUP_PRESSURE"] = "1"
        rc = 0
        if CLEANUP.is_file():
            try:
                rc = subprocess.call(args, env=env, timeout=900)
            except (subprocess.TimeoutExpired, OSError) as exc:
                log(f"cleanup timeout/error: {exc}")
                rc = 1
        if pressure and JANITOR.is_file():
            try:
                subprocess.call(["/bin/bash", str(JANITOR)], env=env, timeout=900)
            except (subprocess.TimeoutExpired, OSError) as exc:
                log(f"janitor timeout/error: {exc}")
        # mac-auto-cleanup.sh --pressure already runs `cleanmymac clean` when the CLI
        # exists.  Do not run a second full CleanMyMac pass here (Codex #156 P2).
        # Never `optimize ram` unless RESOURCE_ALLOW_RAM_OPTIMIZE=1.  It purges
        # resident pages into swapfiles on the disk we are trying to free.
        if pressure and os.environ.get("RESOURCE_ALLOW_RAM_OPTIMIZE") == "1":
            cmm = _run(["/usr/bin/which", "cleanmymac"]).strip() or "/opt/homebrew/bin/cleanmymac"
            if Path(cmm).is_file():
                subprocess.call([cmm, "optimize", "ram"], timeout=120)
        return rc
    finally:
        _release_housekeeper_lock()


def _event_name(hits: list[dict]) -> str:
    event = hits[0]["metric"]
    if event.startswith("disk"):
        return "resource.disk"
    if event.startswith("swap"):
        return "resource.ram"
    if event.startswith("load"):
        return "resource.cpu"
    return "resource.pressure"


def _webhook_started(raw: str, status: int) -> tuple[bool, str]:
    """202 is returned for accepted / ignored / captured / duplicate alike.

    Only a payload carrying runId (and not ignored/captured) actually starts a bot.
    Treating every 2xx as delivered set last_webhook_at and suppressed the next
    45 min on a no-op.
    """
    if not (200 <= status < 300):
        return False, f"http={status}"
    try:
        body = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return False, "unparseable body"
    if not isinstance(body, dict):
        return False, "non-object body"
    started = bool(body.get("runId")) and not body.get("ignored") and not body.get("captured")
    detail = str(body.get("reason") or body.get("status") or "")[:120]
    return started, detail


def fire_webhook(hits: list[dict], sample_data: dict, cleaned: bool) -> bool:
    hooks = load_webhooks()
    if not hooks:
        log("webhooks skipped: no valid urls/secrets found")
        return False
    event_name = _event_name(hits)
    payload = {
        "event": event_name,
        "task": (
            "Host resource pressure.  Run the Housekeeper cleanup playbook now.  "
            "Do not only report.  Regenerable caches and CleanMyMac are in scope."
        ),
        "hits": hits,
        "sample": sample_data,
        "cleanup_already_ran": cleaned,
        # NOTE: no `cleanmymac optimize ram`.  It purges resident pages into swapfiles
        # on the same APFS container as user data, turning RAM pressure into disk
        # consumption -- the opposite of the point.  Measured +3.6-7.3G swap per run.
        "playbook": [
            "python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook",
            "bash /Users/jay/apps/mac-auto-cleanup.sh --pressure",
            "bash /Users/jay/.claude-disk-janitor/janitor.sh",
            "cleanmymac clean --force",
        ],
        "never": [
            "cleanmymac optimize ram (converts RAM pressure into swapfile disk usage)",
            "deleting any git worktree, node_modules, or anything under ~/Library/CloudStorage",
            "bare `ps` or `pgrep -l/-fl` (both print argv, which carries live API keys here)",
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    any_started = False
    for name, url, secret in hooks:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
                # launchd receiver on :8800 historically read only x-webhook-event;
                # BotFleet origin/main (PR #78) also accepts x-botfleet-event.
                "X-BotFleet-Event": event_name,
                "X-Webhook-Event": event_name,
                "Idempotency-Key": f"resource-{sample_data['at']}-{event_name}-{name}",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=8, context=ssl.create_default_context()
            ) as resp:
                raw = resp.read(4096).decode("utf-8", "replace")
                status = resp.status
            started, detail = _webhook_started(raw, status)
            log(
                f"webhook {name} {event_name} http={status} started={started} "
                f"{('reason=' + detail) if detail and not started else ''} "
                f"hits={[h['metric'] for h in hits]}"
            )
            if started:
                any_started = True
        except urllib.error.HTTPError as exc:
            log(f"webhook {name} http={exc.code} (secret_len={len(secret)})")
        except Exception as exc:
            log(f"webhook {name} error: {type(exc).__name__}")
    return any_started


def acquire_lock() -> bool:
    """Single-flight.  A cleanup run can outlive the 5-min StartInterval."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        if LOCK_FILE.exists():
            age = now - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SEC:
                log(f"another run holds the lock ({int(age)}s old); skipping")
                return False
            log(f"stealing stale lock ({int(age)}s old)")
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError as exc:
        log(f"lock error: {type(exc).__name__}; proceeding")
        return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--clean", action="store_true", help="run cleanup even without a hit")
    p.add_argument(
        "--force", action="store_true", help="bypass the cleanup cooldown (manual use only)"
    )
    p.add_argument("--no-webhook", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not args.dry_run and not acquire_lock():
        return 0
    try:
        return _main_body(args)
    finally:
        if not args.dry_run:
            release_lock()


def _main_body(args) -> int:
    s = sample()
    state = load_state()
    prev_free = state.get("disk_free_gb")
    hits = evaluate(s, prev_free)
    log(
        f"free={s['disk_free_gb']}G used={s['disk_used_pct']}% "
        f"swap={s['swap_used_gb']}/{s['swap_total_gb']}G ({s['swap_used_pct']}%) "
        f"load={s['load1']}/{s['load5']}/{s['load15']} hits={[h['metric'] for h in hits] or 'none'}"
    )

    critical = any(h.get("severity") == "critical" for h in hits)
    pressure = critical or any(h["metric"] in {"disk_free_gb", "disk_drop_gb"} for h in hits)

    # Cleanup is EXPENSIVE (CleanMyMac, brew, npm, a walk over every worktree).  Rate
    # limit it independently of the webhook, and never clean for a swap/load-only hit --
    # deleting caches does not reduce swap, it just burns I/O and RAM and makes it worse.
    now = int(time.time())
    last_clean = int(state.get("last_cleanup_at") or 0)
    clean_cd = CLEAN_COOLDOWN_CRIT_SEC if critical else CLEAN_COOLDOWN_SEC
    disk_hit = any(h["metric"] in {"disk_free_gb", "disk_drop_gb"} for h in hits)
    should_clean = args.clean or disk_hit
    # `--clean` means "clean even without a hit", NOT "ignore the cooldown".  All four
    # BF-Housekeeper prompt surfaces invoke `--once --clean`, so letting it bypass the
    # rate limit reopened the every-5-minutes loop through the bot.  Only --force does.
    if should_clean and not args.force and (now - last_clean) < clean_cd:
        log(
            f"cleanup cooldown {clean_cd - (now - last_clean)}s remaining (last_free_delta={state.get('last_clean_delta_gb')})"
        )
        should_clean = False
    if bool(hits) and not disk_hit:
        log("pressure is swap/load only -- not a disk problem; skipping cache cleanup")

    cleaned = False
    if should_clean and not args.dry_run:
        free_before = s["disk_free_gb"]
        rc = run_cleanup(pressure=pressure or disk_hit)
        cleaned = rc in CLEANUP_EXIT_CLEANED
        s_after = sample()
        delta = round(s_after["disk_free_gb"] - free_before, 2)
        swap_delta = round(s_after["swap_used_gb"] - s["swap_used_gb"], 2)
        log(
            f"after_clean free={s_after['disk_free_gb']}G (delta {delta:+}G) "
            f"swap={s_after['swap_used_gb']}G (delta {swap_delta:+}G) load={s_after['load1']}"
        )
        state["last_cleanup_at"] = now
        state["last_clean_delta_gb"] = delta
        state["last_clean_swap_delta_gb"] = swap_delta
        # Counterproductive run: back off hard so we stop digging.
        if delta <= 0:
            state["last_cleanup_at"] = now + max(0, 3 * CLEAN_COOLDOWN_SEC - clean_cd)
            log(
                f"cleanup FREED NOTHING ({delta:+}G, swap {swap_delta:+}G) -- extending cooldown 3x"
            )
        s = s_after

    now = int(time.time())
    last_fire = int(state.get("last_webhook_at") or 0)
    fire = bool(hits) and not args.no_webhook and not args.dry_run
    if fire and (now - last_fire) < COOLDOWN_SEC:
        log(f"webhook cooldown {COOLDOWN_SEC - (now - last_fire)}s remaining")
        fire = False
    fired = False
    if fire:
        fired = fire_webhook(hits, s, cleaned)
        if fired:
            state["last_webhook_at"] = now

    state.update({"disk_free_gb": s["disk_free_gb"], "sample": s, "hits": hits, "ts": now})
    save_state(state)
    return 0 if not hits or cleaned or fired else 1


if __name__ == "__main__":
    sys.exit(main())
