"""Private persistent replay ledger. Mount parent directory on durable storage."""

import contextlib
import os
import sqlite3
import stat
from pathlib import Path


class LedgerError(Exception):
    pass


class NonceLedger:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        s = self.directory.lstat()
        if (
            not stat.S_ISDIR(s.st_mode)
            or s.st_uid != os.getuid()
            or stat.S_IMODE(s.st_mode) & 0o077
        ):
            raise LedgerError("private replay directory required")
        self.path = self.directory / "nonces.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if (
                not stat.S_ISREG(s.st_mode)
                or s.st_uid != os.getuid()
                or stat.S_IMODE(s.st_mode) & 0o077
                or s.st_nlink != 1
            ):
                raise LedgerError("private replay file required")
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS nonces (seat TEXT, nonce TEXT, expires REAL NOT NULL, outcome TEXT NOT NULL, response TEXT, PRIMARY KEY(seat,nonce))"
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def begin(self, seat, nonce, expires, now, capacity):
        """Claim a nonce as pending, or report/claim the recorded repeat.

        Returns "fresh", "capacity", ("repeat", outcome, response), or
        ("repeat-claimed", "ok", response).  A "pending"/"outcome_unknown"/
        "replay" repeat means an attempt's result is not known, so the caller
        must refuse rather than risk a duplicate mint.  A "repeat-claimed" row
        has been atomically re-claimed by THIS call inside the same BEGIN
        IMMEDIATE transaction:  the caller owns it and must settle, restore or
        release it, so two concurrent replays can never both mint.
        """
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
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
                    "INSERT INTO nonces VALUES (?,?,?,'pending',NULL)", (seat, nonce, expires)
                )
            return "fresh"
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def settle(self, seat, nonce, outcome, response=None):
        """Record the final outcome of a begun nonce: ok or outcome_unknown."""
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE nonces SET outcome=?, response=? WHERE seat=? AND nonce=?",
                    (outcome, response, seat, nonce),
                )
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None

    def release(self, seat, nonce):
        """Free a nonce after a definite refusal, so a corrected request may retry."""
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute("DELETE FROM nonces WHERE seat=? AND nonce=?", (seat, nonce))
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None
