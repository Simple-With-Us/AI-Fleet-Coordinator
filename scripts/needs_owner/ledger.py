"""The fallback notifier's own files:  the state, the caps, the lock and the file for CLAUDE's own items.

Everything lives under `~/apps/needs-owner/state/` (mode 0700, HOME from the caller's environment):

  notify-state.json      send times (for the caps), once-only marks, replies whose note is still due, the pause, retries,
                         the Grok Bot requests already mirrored, the topics already read, the messages already judged.
                         Mode 0600, ids and numbers only.
  notify.lock            one run at a time.  A lock older than 30 minutes, or whose process is gone, is taken over.
  pending-CLAUDE.json    replies to items CLAUDE filed itself.  A post that mentions yourself wakes nobody, so the
                         CLAUDE session-start skill reads this file instead.  Ids and links only, never an answer's words.

The files hold no question, option, answer or message text and no credential.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import time
from typing import Callable, Iterator, Mapping

SCHEMA = 1
STATE_FILE = "notify-state.json"
LOCK_FILE = "notify.lock"
PENDING_CLAUDE_FILE = "pending-CLAUDE.json"
LOCK_STALE_SECONDS = 30 * 60
SENT_KEEP_SECONDS = 2 * 24 * 3600
ONCE_KEEP_SECONDS = 30 * 24 * 3600
MAX_KEYS = 500
PENDING_CLAUDE_MAX = 50

DEFAULT_CAPS = {"urgent_per_hour": 3, "per_hour": 6, "per_day": 30}


def state_dir(environ: Mapping[str, str]) -> str:
    home = environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, "apps", "needs-owner", "state")


def private_dir(path: str) -> None:
    """`path` and its missing parents exist, and the last one is mode 0700."""
    os.makedirs(path, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def write_private(path: str, text: str) -> None:
    """Atomically replace `path` with `text` (a temp file in the same directory, mode 0600, then rename)."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: str):
    """The parsed JSON in `path`, or None when it is missing, unreadable or not JSON."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ---- the lock ------------------------------------------------------------------------------------

class Lock:
    """One notifier run at a time.  `acquire` returns False when another run holds a fresh lock."""

    def __init__(self, path: str, stale_seconds: int = LOCK_STALE_SECONDS, wall: Callable[[], float] = time.time) -> None:
        self.path = path
        self.stale = stale_seconds
        self.wall = wall
        self.held = False

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def _holder_gone(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = handle.readline().strip()
            if not raw:
                return False  # just created, the process id not written yet:  the age rule decides
            pid = int(raw)
        except (OSError, ValueError):
            return False  # unreadable:  the age rule decides
        return not self._alive(pid)

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    age = self.wall() - os.stat(self.path).st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale or self._holder_gone():
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(self.path)
                    continue
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("%d\n" % os.getpid())
            self.held = True
            return True
        return False

    def touch(self) -> None:
        """Keep a long run's lock fresh."""
        if self.held:
            with contextlib.suppress(OSError):
                os.utime(self.path, None)

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            self.held = False


# ---- the state -----------------------------------------------------------------------------------

def _empty() -> dict:
    return {"schema": SCHEMA, "sent": [], "once": {}, "notes": {}, "pause": {"on": False, "msg": 0, "at": 0.0},
            "retry": {}, "mirror": {}, "topics": {}, "decided": {}, "stream_id": None, "last_run": 0.0}


class State:
    """The notifier's JSON state.  A file that cannot be read starts a fresh one (the caps then restart too,
    which is the only cost:  a repeated note is held back by its once-only mark in Linear's own records)."""

    def __init__(self, directory: str, now: Callable[[], float]) -> None:
        self.directory = directory
        self.path = os.path.join(directory, STATE_FILE)
        self.now = now
        self.data = _empty()
        self.reset = False
        self.dirty = False

    def load(self) -> None:
        loaded = read_json(self.path)
        if isinstance(loaded, dict) and loaded.get("schema") == SCHEMA:
            base = _empty()
            for key, value in loaded.items():
                if key in base and (base[key] is None or isinstance(value, type(base[key]))):
                    base[key] = value
            self.data = base
        elif os.path.exists(self.path):
            self.reset = True

    def save(self) -> None:
        self._prune()
        private_dir(self.directory)
        write_private(self.path, json.dumps(self.data, sort_keys=True, indent=1))
        self.dirty = False

    def _prune(self) -> None:
        now = self.now()
        d = self.data
        d["sent"] = [s for s in d["sent"] if isinstance(s, dict) and now - float(s.get("t", 0)) < SENT_KEEP_SECONDS]
        d["once"] = {k: t for k, t in d["once"].items() if now - float(t) < ONCE_KEEP_SECONDS}
        for key in ("mirror", "topics", "decided", "retry"):
            if len(d[key]) > MAX_KEYS:
                d[key] = dict(list(d[key].items())[-MAX_KEYS:])

    # ---- sends and caps --------------------------------------------------------------------
    def sent_within(self, seconds: float, *, urgent: bool | None = None) -> int:
        now = self.now()
        return sum(1 for s in self.data["sent"] if now - float(s.get("t", 0)) < seconds
                   and (urgent is None or bool(s.get("u")) == urgent))

    def record_send(self, kind: str, ident: str, urgent: bool = False) -> None:
        self.data["sent"].append({"t": self.now(), "k": kind, "i": ident, "u": bool(urgent)})
        self.dirty = True

    # ---- once-only marks --------------------------------------------------------------------
    def seen_once(self, key: str) -> bool:
        return key in self.data["once"]

    def mark_once(self, key: str) -> None:
        self.data["once"][key] = self.now()
        self.dirty = True

    # ---- notes still due --------------------------------------------------------------------
    def add_note(self, ident: str, kind: str, ref: str, option: int | None) -> None:
        self.data["notes"][ident] = {"kind": kind, "id": ref, "option": option, "since": self.now(), "tries": 0, "next": 0.0}
        self.dirty = True

    def drop_note(self, ident: str) -> None:
        if self.data["notes"].pop(ident, None) is not None:
            self.dirty = True

    # ---- the pause Jay asked for in an OWNER topic ----------------------------------------------
    PAUSE_SECONDS = 24 * 3600

    def message_pause(self) -> bool:
        pause = self.data["pause"]
        return bool(pause.get("on")) and self.now() - float(pause.get("at", 0)) < self.PAUSE_SECONDS

    def set_pause(self, on: bool, message_id: int) -> None:
        self.data["pause"] = {"on": bool(on), "msg": int(message_id), "at": self.now()}
        self.dirty = True

    # ---- retries -------------------------------------------------------------------------------
    def backoff(self, ident: str) -> float:
        return float(self.data["retry"].get(ident, {}).get("next", 0.0))

    def failed(self, ident: str) -> int:
        row = self.data["retry"].setdefault(ident, {"fails": 0, "next": 0.0})
        row["fails"] = int(row.get("fails", 0)) + 1
        row["next"] = self.now() + min(3600.0, 300.0 * (2 ** (row["fails"] - 1)))
        self.dirty = True
        return row["fails"]

    def succeeded(self, ident: str) -> None:
        if self.data["retry"].pop(ident, None) is not None:
            self.dirty = True


# ---- caps ----------------------------------------------------------------------------------------

def read_caps(raw: object) -> dict[str, int]:
    """The knob's caps as three counts, each a whole number from 0 up.  A missing or odd entry takes the default."""
    caps = dict(DEFAULT_CAPS)
    if isinstance(raw, Mapping):
        for name in caps:
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                caps[name] = value
    return caps


class Caps:
    """Every Zulip post the notifier makes counts.  `urgent` posts also count against their own hourly cap, and
    the hourly and daily caps cover all of them."""

    def __init__(self, state: State, limits: Mapping[str, int]) -> None:
        self.state = state
        self.limits = limits

    def room(self, urgent: bool = False) -> str | None:
        """None when a post may go out, else the cap that is full:  per_day, per_hour or urgent_per_hour."""
        if self.state.sent_within(24 * 3600) >= self.limits["per_day"]:
            return "per_day"
        if self.state.sent_within(3600) >= self.limits["per_hour"]:
            return "per_hour"
        if urgent and self.state.sent_within(3600, urgent=True) >= self.limits["urgent_per_hour"]:
            return "urgent_per_hour"
        return None


# ---- the file for CLAUDE's own items ----------------------------------------------------------------

def write_pending_claude(directory: str, rows: list[dict], now: dt.datetime) -> None:
    """The replies waiting for a CLAUDE session, newest last.  `rows` hold ids, links and numbers only."""
    private_dir(directory)
    document = {"schema": SCHEMA, "updated_at": now.isoformat(timespec="seconds"), "items": rows[-PENDING_CLAUDE_MAX:]}
    write_private(os.path.join(directory, PENDING_CLAUDE_FILE), json.dumps(document, indent=1, sort_keys=True))


def read_pending_claude(directory: str) -> list[dict]:
    document = read_json(os.path.join(directory, PENDING_CLAUDE_FILE))
    rows = document.get("items") if isinstance(document, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


@contextlib.contextmanager
def held(lock: Lock) -> Iterator[bool]:
    got = lock.acquire()
    try:
        yield got
    finally:
        lock.release()
