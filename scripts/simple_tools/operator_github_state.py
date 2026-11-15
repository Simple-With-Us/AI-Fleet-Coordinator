"""Private persistent replay ledger. Mount parent directory on durable storage."""

import contextlib
import math
import os
import sqlite3
import stat
import time
from pathlib import Path


class LedgerError(Exception):
    pass


class NonceLedger:
    INITIALIZED_NAME = "retained.initialized"
    INITIALIZED_BYTES = b"retained-ledger-v1\n"
    MAX_RESPONSE_BYTES = 64 * 1024
    MAX_RETAINED_ROWS = 4096
    MAX_DATABASE_BYTES = 128 * 1024 * 1024
    _RETAINED_STATES = frozenset(
        {"pending", "ok", "refused", "outcome_unknown", "unknown", "replay"}
    )

    def __init__(self, directory, *, retain_unsettled=False):
        self.retain_unsettled = retain_unsettled
        self.directory = Path(os.path.abspath(directory))
        self.path = self.directory / "nonces.sqlite3"
        self._identity = None
        self._initializing = True
        try:
            parent = self._private_parent(create=True)
            try:
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                if retain_unsettled:
                    initialized = self._retained_marker(parent)
                    try:
                        fd = os.open(self.path.name, flags, dir_fd=parent)
                        created = False
                    except FileNotFoundError:
                        # Never reset a directory with initialization or prior-lock evidence.
                        if initialized or os.listdir(parent):
                            raise LedgerError("replay storage unavailable") from None
                        self._retained_marker(parent, create=True)
                        fd = os.open(
                            self.path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
                        )
                        created = True
                else:
                    try:
                        fd = os.open(
                            self.path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
                        )
                        created = True
                    except FileExistsError:
                        fd = os.open(self.path.name, flags, dir_fd=parent)
                        created = False
                try:
                    s = os.fstat(fd)
                    if not self._private_file(s):
                        raise LedgerError("private replay file required")
                    if retain_unsettled and not created and s.st_size == 0:
                        raise LedgerError("replay storage unavailable")
                    self._identity = (s.st_dev, s.st_ino)
                    if retain_unsettled and created:
                        # Commit the new file's inode and name before any caller can send a write.
                        os.fsync(fd)
                        os.fsync(parent)
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
            with contextlib.closing(self.connect()) as db, db:
                if retain_unsettled and not created:
                    self._validate_retained(db)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS nonces (seat TEXT, nonce TEXT, expires REAL NOT NULL, outcome TEXT NOT NULL, response TEXT, PRIMARY KEY(seat,nonce))"
                )
                if retain_unsettled:
                    self._validate_retained(db)
            if retain_unsettled:
                parent = self._private_parent()
                try:
                    # Valid existing databases acquire the same evidence before any dispatch.
                    self._retained_marker(parent, create=True)
                finally:
                    os.close(parent)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise LedgerError("replay storage unavailable") from None
        finally:
            self._initializing = False

    @staticmethod
    def _private_file(info):
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077
            and info.st_nlink == 1
        )

    def _retained_marker(self, parent, *, create=False):
        try:
            fd = os.open(
                self.INITIALIZED_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        except FileNotFoundError:
            if not create:
                return False
            fd = os.open(
                self.INITIALIZED_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
            try:
                if os.write(fd, self.INITIALIZED_BYTES) != len(self.INITIALIZED_BYTES):
                    raise LedgerError("replay storage unavailable")
                os.fsync(fd)
                os.fsync(parent)
            finally:
                os.close(fd)
            return True
        try:
            info = os.fstat(fd)
            if (
                not self._private_file(info)
                or info.st_size != len(self.INITIALIZED_BYTES)
                or os.read(fd, len(self.INITIALIZED_BYTES) + 1) != self.INITIALIZED_BYTES
            ):
                raise LedgerError("replay storage unavailable")
            return True
        finally:
            os.close(fd)

    def _private_parent(self, *, create=False):
        """The retained journal never traverses a symlink or an untrusted writable ancestor."""
        parts = self.directory.parts
        if len(parts) < 2:
            raise LedgerError("private replay directory required")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(parts[0], flags)
        try:
            for index, part in enumerate(parts[1:], 1):
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                final = index == len(parts) - 1
                sticky_root = not final and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if (
                    info.st_uid not in (0, os.getuid())
                    or info.st_mode & 0o022
                    and not sticky_root
                    or final
                    and (info.st_uid != os.getuid() or info.st_mode & 0o077)
                ):
                    raise LedgerError("private replay directory required")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check_custody(self):
        parent = self._private_parent()
        try:
            if (
                self.retain_unsettled
                and not self._initializing
                and not self._retained_marker(parent)
            ):
                raise LedgerError("replay storage unavailable")
            for name in (
                self.path.name,
                self.path.name + "-journal",
                self.path.name + "-wal",
                self.path.name + "-shm",
            ):
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError:
                    if name != self.path.name:
                        continue
                    raise
                try:
                    info = os.fstat(fd)
                    if not self._private_file(info):
                        raise LedgerError("private replay file required")
                    if name == self.path.name and (info.st_dev, info.st_ino) != self._identity:
                        raise LedgerError("replay storage unavailable")
                    if info.st_size > self.MAX_DATABASE_BYTES:
                        raise LedgerError("replay storage unavailable")
                finally:
                    os.close(fd)
        finally:
            os.close(parent)

    @contextlib.contextmanager
    def serialized(self, *, timeout=5.0):
        """Hold one protected process lock across journal transitions and an external mutation."""
        if (
            not self.retain_unsettled
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 150
        ):
            raise LedgerError("private replay lock unavailable")
        import fcntl

        parent, fd = None, None
        locked = entered = False
        try:
            parent = self._private_parent()
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open("journal.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                created = True
            except FileExistsError:
                fd = os.open("journal.lock", flags, dir_fd=parent)
                created = False
            if not self._private_file(os.fstat(fd)):
                raise LedgerError("private replay lock unavailable")
            if created:
                os.fsync(fd)
                os.fsync(parent)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LedgerError("private replay lock unavailable") from None
                    time.sleep(min(0.025, remaining))
            # Refuse replacement of the lock name while this descriptor was waiting.
            named = os.open(
                "journal.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                current, held = os.fstat(named), os.fstat(fd)
                if not self._private_file(current) or (current.st_dev, current.st_ino) != (
                    held.st_dev,
                    held.st_ino,
                ):
                    raise LedgerError("private replay lock unavailable")
            finally:
                os.close(named)
            self._check_custody()
            entered = True
            yield
        except (OSError, sqlite3.Error):
            if entered:
                raise
            raise LedgerError("private replay lock unavailable") from None
        finally:
            if fd is not None:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            if parent is not None:
                os.close(parent)

    def _validate_retained(self, db):
        """Validate shape and bounds before loading any opaque response text."""
        count = db.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM nonces LIMIT ?)",
            (self.MAX_RETAINED_ROWS + 1,),
        ).fetchone()[0]
        if count > self.MAX_RETAINED_ROWS:
            raise LedgerError("replay storage unavailable")
        columns = db.execute("PRAGMA table_info(nonces)").fetchall()
        expected = [
            ("seat", "TEXT", 0, 1),
            ("nonce", "TEXT", 0, 2),
            ("expires", "REAL", 1, 0),
            ("outcome", "TEXT", 1, 0),
            ("response", "TEXT", 0, 0),
        ]
        if [(row[1], row[2].upper(), row[3], row[5]) for row in columns] != expected:
            raise LedgerError("replay storage unavailable")
        objects = db.execute(
            "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if objects != [("table", "nonces", "nonces")]:
            raise LedgerError("replay storage unavailable")
        invalid = db.execute(
            "SELECT 1 FROM nonces WHERE typeof(seat)!='text' OR length(CAST(seat AS BLOB)) NOT BETWEEN 1 AND 256 "
            "OR typeof(nonce)!='text' OR length(CAST(nonce AS BLOB)) NOT BETWEEN 1 AND 256 "
            "OR typeof(expires) NOT IN ('integer','real') "
            "OR typeof(outcome)!='text' OR outcome NOT IN ('pending','ok','refused','outcome_unknown','unknown','replay') "
            "OR typeof(response) NOT IN ('null','text') OR length(CAST(response AS BLOB))>? LIMIT 1",
            (self.MAX_RESPONSE_BYTES,),
        ).fetchone()
        if invalid or any(
            not math.isfinite(row[0]) for row in db.execute("SELECT expires FROM nonces")
        ):
            raise LedgerError("replay storage unavailable")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise LedgerError("replay storage unavailable")

    def _check_input(self, seat, nonce, response):
        if not self.retain_unsettled:
            return
        try:
            valid = all(
                isinstance(value, str) and 1 <= len(value.encode("utf-8")) <= 256
                for value in (seat, nonce)
            )
            valid = valid and (
                response is None
                or isinstance(response, str)
                and len(response.encode("utf-8")) <= self.MAX_RESPONSE_BYTES
            )
        except UnicodeError:
            valid = False
        if not valid:
            raise LedgerError("replay storage unavailable")

    def connect(self):
        db = None
        try:
            if self.retain_unsettled:
                self._check_custody()
            db = sqlite3.connect(self.path, timeout=5)
            db.execute("PRAGMA synchronous=FULL")
            if self.retain_unsettled and not self._initializing:
                self._validate_retained(db)
            return db
        except (sqlite3.Error, OSError, LedgerError):
            if db is not None:
                db.close()
            raise LedgerError("replay storage unavailable") from None

    def begin(self, seat, nonce, expires, now, capacity, *, response=None):
        """Claim a nonce as pending, or report/claim the recorded repeat.

        Returns "fresh", "capacity", ("repeat", outcome, response), or
        ("repeat-claimed", "ok", response).  A "pending"/"outcome_unknown"/
        "replay" repeat means an attempt's result is not known, so the caller
        must refuse rather than risk a duplicate mint.  A "repeat-claimed" row
        has been atomically re-claimed by THIS call inside the same BEGIN
        IMMEDIATE transaction:  the caller owns it and must settle, restore or
        release it, so two concurrent replays can never both mint.
        """
        self._check_input(seat, nonce, response)
        if self.retain_unsettled and (
            any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (expires, now)
            )
            or type(capacity) is not int
            or not 0 <= capacity <= self.MAX_RETAINED_ROWS
        ):
            raise LedgerError("replay storage unavailable")
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if self.retain_unsettled:
                    db.execute(
                        "DELETE FROM nonces WHERE expires < ? AND outcome IN ('ok','refused')",
                        (now,),
                    )
                else:
                    db.execute("DELETE FROM nonces WHERE expires < ?", (now,))
                row = db.execute(
                    "SELECT outcome, response FROM nonces WHERE seat=? AND nonce=?",
                    (seat, nonce),
                ).fetchone()
                if row:
                    if row[0] == "ok" and row[1]:
                        claimed = db.execute(
                            "UPDATE nonces SET outcome='replay' "
                            "WHERE seat=? AND nonce=? AND outcome='ok'",
                            (seat, nonce),
                        )
                        if claimed.rowcount == 1:
                            return ("repeat-claimed", "ok", row[1])
                        return ("repeat", "replay", None)
                    return ("repeat", row[0], row[1])
                if db.execute("SELECT count(*) FROM nonces").fetchone()[0] >= capacity:
                    return "capacity"
                db.execute(
                    "INSERT INTO nonces VALUES (?,?,?,'pending',?)",
                    (seat, nonce, expires, response),
                )
            return "fresh"
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def settle(self, seat, nonce, outcome, response=None):
        """Record the final outcome of a begun nonce: ok or outcome_unknown."""
        self._check_input(seat, nonce, response)
        if self.retain_unsettled and (
            not isinstance(outcome, str) or outcome not in self._RETAINED_STATES - {"replay"}
        ):
            raise LedgerError("replay storage unavailable")
        try:
            with contextlib.closing(self.connect()) as db, db:
                changed = db.execute(
                    "UPDATE nonces SET outcome=?, response=? WHERE seat=? AND nonce=?",
                    (outcome, response, seat, nonce),
                )
                if self.retain_unsettled and changed.rowcount != 1:
                    raise LedgerError("replay storage unavailable")
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def release(self, seat, nonce):
        """Free a nonce after a definite refusal, so a corrected request may retry."""
        if self.retain_unsettled:
            raise LedgerError("retained replay rows cannot be released")
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute("DELETE FROM nonces WHERE seat=? AND nonce=?", (seat, nonce))
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def lookup(self, seat, nonce):
        """Read a recorded outcome without claiming or creating a request."""
        self._check_input(seat, nonce, None)
        try:
            with contextlib.closing(self.connect()) as db:
                return db.execute(
                    "SELECT outcome, response FROM nonces WHERE seat=? AND nonce=?", (seat, nonce)
                ).fetchone()
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def has_unsettled(self, namespace, excluding_nonce):
        """Only a boolean; callers serialize this check with mutations under their folder lock."""
        self._check_input(namespace, excluding_nonce, None)
        try:
            with contextlib.closing(self.connect()) as db:
                row = db.execute(
                    "SELECT 1 FROM nonces WHERE seat=? AND nonce!=? "
                    "AND outcome IN ('pending','outcome_unknown','unknown','replay') LIMIT 1",
                    (namespace, excluding_nonce),
                ).fetchone()
                return row is not None
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def lookup_unsettled(self, namespace):
        """One retained recovery identity; caller must hold the shared namespace lock."""
        if not self.retain_unsettled:
            raise LedgerError("retained replay storage required")
        self._check_input(namespace, "recovery", None)
        try:
            with contextlib.closing(self.connect()) as db:
                rows = db.execute(
                    "SELECT nonce,outcome,response FROM nonces WHERE seat=? "
                    "AND outcome IN ('pending','outcome_unknown','unknown','replay') LIMIT 2",
                    (namespace,),
                ).fetchall()
                if len(rows) > 1:
                    raise LedgerError("replay storage unavailable")
                return rows[0] if rows else None
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def lookup_identity(self, nonce):
        """Bind one retained identity across namespaces without creating another registry."""
        if not self.retain_unsettled:
            raise LedgerError("retained replay storage required")
        self._check_input("identity", nonce, None)
        try:
            with contextlib.closing(self.connect()) as db:
                rows = db.execute(
                    "SELECT seat,outcome,response FROM nonces WHERE nonce=? LIMIT 2", (nonce,)
                ).fetchall()
                if len(rows) > 1:
                    raise LedgerError("replay storage unavailable")
                return rows[0] if rows else None
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def validate_retained_rows(self, expected_expiry):
        """Caller validates its bounded metadata and immutable expiry before begin can prune."""
        if not self.retain_unsettled:
            raise LedgerError("retained replay storage required")
        try:
            with contextlib.closing(self.connect()) as db:
                for namespace, nonce, expires, outcome, response in db.execute(
                    "SELECT seat,nonce,expires,outcome,response FROM nonces LIMIT ?",
                    (self.MAX_RETAINED_ROWS,),
                ):
                    expected = expected_expiry(namespace, nonce, outcome, response)
                    if (
                        type(expected) not in (int, float)
                        or not math.isfinite(expected)
                        or expires != expected
                    ):
                        raise LedgerError("replay storage unavailable")
        except Exception:
            raise LedgerError("replay storage unavailable") from None
