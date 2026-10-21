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
                "CREATE TABLE IF NOT EXISTS nonces (seat TEXT, nonce TEXT, expires REAL NOT NULL, PRIMARY KEY(seat,nonce))"
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def consume(self, seat, nonce, expires, now, capacity):
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM nonces WHERE expires < ?", (now,))
                if db.execute(
                    "SELECT 1 FROM nonces WHERE seat=? AND nonce=?", (seat, nonce)
                ).fetchone():
                    return "replay"
                if db.execute("SELECT count(*) FROM nonces").fetchone()[0] >= capacity:
                    return "capacity"
                db.execute("INSERT INTO nonces VALUES (?,?,?)", (seat, nonce, expires))
            return "ok"
        except (sqlite3.Error, OSError):
            raise LedgerError("replay storage unavailable") from None
