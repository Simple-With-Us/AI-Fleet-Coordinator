"""The bar:  what may reach Jay through `needs-owner file`.

A decision waits on Jay only when it needs his identity, his credential, his money, a legal act, an
irreversible approval or a decision only he can make.  Everything else the seat decides, asks a peer in
Side-Chats, or posts to Pending-for-Jay.  The filing seat names which of the six it is with `--why-owner`;
the class is stored in the block (`Why owner:`) and the knobs decide how hard the bar is:

  NEEDS_OWNER_BAR              off (the default, today's behaviour exactly), warn (file it, record the class,
                               print one warning line) or enforce (refuse a new filing without a class)
  NEEDS_OWNER_SEAT_DAILY_MAX   most filings one seat may make in 24 hours (0, the default, means no cap);
                               it applies in warn (a warning) and enforce (a refusal), never in off

Both knobs are read once, through `fleet_knobs` (the tests pass a fake reader).  A knob that cannot be read, or
holds anything but one of the three modes, is `off`:  the bar never stops a filing it cannot be sure about.
A Grok Bot mirror (`--mirror-of`) is exempt:  the Grok bot already opened its own OWNER topic, so there is
nothing left to keep from Jay.  So is an item that already exists:  a rerun of it is looked up first and behaves
exactly as in `off` (it needs no class and is never counted), so only a NEW filing meets the bar.

The cap counts this host's own filings, kept in `~/apps/needs-owner/filings/filings.json` (mode 0600, seat
names, times and identifiers only).  A closed item still counts, because the filer removes the label when it
closes one and Linear alone would forget it.  A rerun that files nothing new is never counted or refused.

The count is a read-modify-write, so a `Slot` makes the check, the Linear filing and the record one critical
section under an exclusive advisory lock (`flock` on `filings.lock` beside the ledger, mode 0600), and two
filings racing for the last slot cannot both pass.  The lock dies with its process, so nothing is left to expire,
and a filing that fails records nothing.  A lock that cannot be taken (no `fcntl`, the file cannot be opened, or
the wait of 60 seconds runs out) fails OPEN in warn (the filing goes ahead, with one warning line) and CLOSED in
enforce when a cap is set (nothing is filed, and the refusal says why).  With no cap there is nothing the lock
would protect, so enforce fails open too.  Off never takes it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

try:
    import fcntl
except ImportError:  # not POSIX:  every lock is then a lock failure
    fcntl = None

from . import ledger
from .block import WHY_RE
from .errors import Refusal

MODE_KNOB = "NEEDS_OWNER_BAR"
CAP_KNOB = "NEEDS_OWNER_SEAT_DAILY_MAX"

OFF, WARN, ENFORCE = "off", "warn", "enforce"
MODES = (OFF, WARN, ENFORCE)

# The six things that need Jay (identity, credential, money, a legal act, an irreversible approval, a decision only he
# can make).  What each one means is in docs/protocols/needs-owner.md, "What May Reach Jay";  a test keeps the two equal.
CLASSES = ("identity", "credential", "money", "legal", "irreversible", "policy")
# A class word as stored.  The block reader checks the shape and not this list, so an item filed under a class
# that is later retired still reads.
CLASS_RE = WHY_RE

WINDOW_SECONDS = 24 * 3600
KEEP_SECONDS = 2 * 24 * 3600
MAX_ROWS = 500
FILINGS_DIR = "filings"
FILINGS_FILE = "filings.json"
LOCK_FILE = "filings.lock"
LOCK_WAIT_SECONDS = (
    60.0  # how long a filing waits for another one on this host (a filing is a few seconds)
)
LOCK_POLL_SECONDS = 0.05
SCHEMA = 1

POINTER = (
    "everything else is yours to decide, or goes to Side-Chats (bot to bot, visible to Jay) or to "
    "Pending-for-Jay (one updated post per bot, for what only the owner has to act on)"
)


@dataclass(frozen=True)
class Policy:
    mode: str = OFF
    cap: int = 0

    @property
    def on(self) -> bool:
        return self.mode != OFF


# ---- the knobs ------------------------------------------------------------------------------------


def _fleet_knobs() -> object:
    from fleet_knobs import knobs  # the stable copy carries it;  a copy without it reads `off`

    return knobs


def read_policy(knobs: object | None = None) -> Policy:
    """The bar's mode and cap.  Anything that cannot be read, or is not one of the three modes, is `off`."""
    try:
        reader = knobs if knobs is not None else _fleet_knobs()
        mode = reader.get(MODE_KNOB)
    except Exception:  # noqa: BLE001 - an unreadable knob never stops a filing
        return Policy()
    if mode not in (WARN, ENFORCE):
        return Policy()
    try:
        raw = reader.get(CAP_KNOB)
    except Exception:  # noqa: BLE001
        return Policy(mode)
    cap = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
    return Policy(mode, cap)


# ---- the class --------------------------------------------------------------------------------------


def _message(problem: str) -> str:
    return (
        "%s;  needs-owner is only for what needs Jay's identity, credential, money, a legal act, an irreversible "
        "approval or a decision only he can make (--why-owner %s);  %s"
        % (problem, "|".join(CLASSES), POINTER)
    )


def check_class(mode: str, given: str | None) -> tuple[str | None, str | None]:
    """(the class to store, a warning line), or a Refusal.

    Off looks at nothing.  Warn stores a good class and warns about a missing or unknown one (the filing goes
    ahead without a class).  Enforce refuses a missing or unknown class.  The answer never repeats the text
    that was given, because a seat may have typed anything."""
    if mode not in (WARN, ENFORCE):
        return None, None
    word = (given or "").strip().lower()
    if word in CLASSES:
        return word, None
    problem = (
        "no --why-owner class was given"
        if not word
        else "--why-owner is not one of the six classes"
    )
    if mode == ENFORCE:
        raise Refusal(_message(problem))
    return None, _message(problem)


# ---- the daily cap ------------------------------------------------------------------------------------


def _path(environ: Mapping[str, str]) -> str:
    home = environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, "apps", "needs-owner", FILINGS_DIR, FILINGS_FILE)


def _rows(environ: Mapping[str, str]) -> list[dict]:
    data = ledger.read_json(_path(environ))
    rows = data.get("rows") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def filings_in_window(environ: Mapping[str, str], seat: str, now: float) -> int:
    """How many items `seat` filed from this host in the last 24 hours."""
    count = 0
    for row in _rows(environ):
        at = row.get("t")
        if (
            row.get("seat") == seat
            and isinstance(at, (int, float))
            and not isinstance(at, bool)
            and 0 <= now - at < WINDOW_SECONDS
        ):
            count += 1
    return count


def record_filing(environ: Mapping[str, str], seat: str, identifier: str, now: float) -> None:
    """Remember one new filing.  A write that fails is ignored:  the bar never blocks a filing that landed."""
    try:
        rows = [
            r
            for r in _rows(environ)
            if isinstance(r.get("t"), (int, float)) and 0 <= now - r["t"] < KEEP_SECONDS
        ]
        rows.append({"seat": seat, "t": now, "id": identifier})
        path = _path(environ)
        ledger.private_dir(os.path.dirname(path))
        ledger.write_private(
            path, json.dumps({"schema": SCHEMA, "rows": rows[-MAX_ROWS:]}, sort_keys=True)
        )
    except OSError:
        pass


def check_cap(policy: Policy, environ: Mapping[str, str], seat: str, now: float) -> str | None:
    """A warning line when `seat` is at its daily cap in warn mode, a Refusal in enforce, else None."""
    if not policy.on or policy.cap <= 0:
        return None
    used = filings_in_window(environ, seat, now)
    if used < policy.cap:
        return None
    message = (
        "%s has filed %d needs-owner items in the last 24 hours (the daily cap is %d);  hold this one, "
        "decide it yourself or post it to Pending-for-Jay" % (seat, used, policy.cap)
    )
    if policy.mode == ENFORCE:
        raise Refusal(message)
    return message


def wall(clock: object | None) -> float:
    """Now as seconds since the epoch:  the caller's clock (a function giving an aware datetime) or the real one."""
    if callable(clock):
        return float(clock().timestamp())
    return time.time()


# ---- the lock ---------------------------------------------------------------------------------------------


def _lock_path(environ: Mapping[str, str]) -> str:
    return os.path.join(os.path.dirname(_path(environ)), LOCK_FILE)


class FilingsLock:
    """An exclusive advisory `flock` on the lock file beside the ledger (mode 0600).

    `acquire` returns None when it holds the lock, else a reason word (`no_flock`, `cannot_open`, `flock_failed` or
    `timeout`) and holds nothing.  It waits up to LOCK_WAIT_SECONDS for another holder, polling, so a holder that
    dies releases it by dying."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = environ
        self.fd: int | None = None

    def acquire(self) -> str | None:
        if fcntl is None:
            return "no_flock"
        path = _lock_path(self.environ)
        try:
            ledger.private_dir(os.path.dirname(path))
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return "cannot_open"
        try:
            os.fchmod(fd, 0o600)  # a file made earlier under another umask is brought to 0600
        except OSError:
            pass
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return "timeout"
                time.sleep(LOCK_POLL_SECONDS)
            except OSError:
                os.close(fd)
                return "flock_failed"
            else:
                self.fd = fd
                return None

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)  # closing the descriptor drops the lock whatever the unlock said


class Slot:
    """One new filing's place in the seat's daily count:  the lock, the cap check and the record in one section.

        with Slot(policy, environ, seat, clock, warn) as slot:
            line = slot.check()          # a warning line (warn), or a Refusal (enforce), when the seat is at its cap
            ... file the item ...
            slot.record(identifier)      # only when the item was really filed

    Nothing is held in off.  See the module docstring for what a lock that cannot be taken does."""

    def __init__(
        self,
        policy: Policy,
        environ: Mapping[str, str],
        seat: str,
        clock: object | None,
        warn: Callable[[str], None],
    ) -> None:
        self.policy = policy
        self.environ = environ
        self.seat = seat
        self.clock = clock
        self.warn = warn
        self.lock: FilingsLock | None = None

    def __enter__(self) -> Slot:
        if not self.policy.on:
            return self
        lock = FilingsLock(self.environ)
        reason = lock.acquire()
        if reason is None:
            self.lock = lock
            return self
        if self.policy.mode == ENFORCE and self.policy.cap > 0:
            raise Refusal(
                "the daily cap cannot be checked safely (could not take the filings lock: %s), so nothing was "
                "filed;  try again in a moment, or post it to Pending-for-Jay" % reason
            )
        self.warn(
            "could not take the filings lock (%s);  filing without it, so this filing may not be counted exactly"
            % reason
        )
        return self

    def check(self) -> str | None:
        return check_cap(self.policy, self.environ, self.seat, wall(self.clock))

    def record(self, identifier: str) -> None:
        if self.policy.on:
            record_filing(self.environ, self.seat, identifier, wall(self.clock))

    def __exit__(self, *exc: object) -> None:
        if self.lock is not None:
            self.lock.release()
            self.lock = None
