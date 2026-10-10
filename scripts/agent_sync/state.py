"""Per-session cursor and posted-id ledger, plus the seat-wide inbox cursor.

Layout (root is $AGENT_SYNC_STATE_DIR, else ~/.agent-sync):

  <root>/<SEAT>/<session-tag or "nosession">/state.json
        {"cursors": {"<channel>\\u0000<topic>": last_id}, "posted": [message ids, newest 500]}
  <root>/<SEAT>/inbox.json
        {"cursor": id}

Directories are created with mode 700 and files with mode 600; a seat or session directory that
already exists with a wider mode and belongs to this user is tightened to 700 on the next write
(the root is left alone: it may be a shared or user-chosen directory).  Every write is atomic (a temp file
in the same directory, then os.replace) and happens under an advisory file lock, because one
session can run `listen` in a Monitor while it also posts from the shell.  Every read goes to
disk, so a long-running `listen` sees ids that another process of the same session just posted.
Cursors only ever move forward.  Sessions never share a state.json because the session tag
differs; the inbox file is shared by every session of a seat, and the lock keeps even that free
of lost updates.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:  # POSIX only; on anything else the in-process lock is all there is
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

__all__ = ["State", "state_root", "NO_SESSION", "POSTED_LIMIT", "cursor_key"]

NO_SESSION = "nosession"
POSTED_LIMIT = 500
ENV_STATE_DIR = "AGENT_SYNC_STATE_DIR"

_THREAD_LOCK = threading.RLock()


def state_root(env: Mapping[str, str], home: Path | None = None) -> Path:
    override = env.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
    home = home if home is not None else Path(env.get("HOME") or os.path.expanduser("~"))
    return home / ".agent-sync"


def cursor_key(channel: str, topic: str) -> str:
    """Channel names and topics are case-insensitive in Zulip, so the key is too."""
    return "%s\u0000%s" % (channel.casefold(), topic.casefold())


def make_private_dir(path: Path) -> None:
    """Create `path` and any missing parents, each with mode 700.  os.makedirs(mode=...) would
    apply the mode to the leaf only."""
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)


def tighten_private_dir(path: Path) -> None:
    """Make an existing directory that this user owns private (700).  Best effort: a directory
    that belongs to someone else, or one that cannot be changed, is left as it is."""
    try:
        st = os.stat(path)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    make_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on <path>.lock, held across a read-modify-write."""
    make_private_dir(path.parent)
    with _THREAD_LOCK:
        if fcntl is None:  # pragma: no cover
            yield
            return
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _update(path: Path, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _locked(path):
        data = _read_json(path)
        mutate(data)
        _write_json_atomic(path, data)
        return data


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class State:
    """Cursor and ledger store for one seat and one session tag."""

    def __init__(self, root: Path, seat: str, tag: str | None) -> None:
        self.root = Path(root)
        self.seat = seat
        self.tag = tag
        self.seat_dir = self.root / seat
        self.session_dir = self.seat_dir / (tag or NO_SESSION)
        self.path = self.session_dir / "state.json"
        self.inbox_path = self.seat_dir / "inbox.json"

    def _update(self, path: Path, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        make_private_dir(path.parent)
        for directory in (self.seat_dir, path.parent):  # the seat and session directories, never the root
            tighten_private_dir(directory)
        return _update(path, mutate)

    # ---- per-session cursors ---------------------------------------------------------------
    def cursor(self, channel: str, topic: str) -> int | None:
        cursors = _read_json(self.path).get("cursors")
        if not isinstance(cursors, dict):
            return None
        return _as_int(cursors.get(cursor_key(channel, topic)))

    def advance_cursor(self, channel: str, topic: str, message_id: int) -> None:
        """Move the cursor forward to `message_id`; a smaller id never moves it back."""
        key = cursor_key(channel, topic)

        def mutate(data: dict[str, Any]) -> None:
            cursors = data.get("cursors")
            if not isinstance(cursors, dict):
                cursors = {}
                data["cursors"] = cursors
            current = _as_int(cursors.get(key))
            if current is None or message_id > current:
                cursors[key] = int(message_id)

        self._update(self.path, mutate)

    # ---- posted ledger --------------------------------------------------------------------
    def posted(self) -> set[int]:
        ledger = _read_json(self.path).get("posted")
        if not isinstance(ledger, list):
            return set()
        return {item for item in ledger if _as_int(item) is not None}

    def record_posted(self, message_id: int) -> None:
        def mutate(data: dict[str, Any]) -> None:
            ledger = data.get("posted")
            ids = {item for item in ledger if _as_int(item) is not None} if isinstance(ledger, list) else set()
            ids.add(int(message_id))
            data["posted"] = sorted(ids)[-POSTED_LIMIT:]

        self._update(self.path, mutate)

    # ---- seat-wide inbox cursor -------------------------------------------------------------
    def inbox_cursor(self) -> int | None:
        return _as_int(_read_json(self.inbox_path).get("cursor"))

    def advance_inbox(self, message_id: int) -> None:
        def mutate(data: dict[str, Any]) -> None:
            current = _as_int(data.get("cursor"))
            if current is None or message_id > current:
                data["cursor"] = int(message_id)

        self._update(self.inbox_path, mutate)
