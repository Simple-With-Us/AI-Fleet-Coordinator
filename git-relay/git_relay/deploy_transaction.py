"""Small durable file transaction for a stopped service.

This module does not stop, start or authorize any service. The root-owned
supervisor must prevent service start throughout prepare/apply/recover, bound
executor lifetime, and run recovery on failed exit and on boot before service
start. All paths and expected hashes come from its reviewed pinned manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


class TransactionError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TransactionError("not a single regular file")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read()
        return data, {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
    finally:
        os.close(fd)


def atomic(path, data, meta):
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".patch-transaction-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchown(handle.fileno(), meta["uid"], meta["gid"])
            os.fchmod(handle.fileno(), meta["mode"])
            os.fsync(handle.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()
            sync_dir(temp.parent)


class Transaction:
    """An anchored journal of exact files, never arbitrary journal destinations.

    Caller holds a process-exclusive deployment lock and a stopped-service guard.
    Directory anchors must have been owner/mode/symlink checked by the supervisor.
    A torn operation is recovered from exact before/new hashes, never guessed.
    """

    def __init__(self, roots, backup, stopped):
        self.roots = {k: Path(v) for k, v in roots.items()}
        self.backup = Path(backup)
        self.stopped = stopped
        self.journal = self.backup / "journal.json"

    def target(self, row):
        if set(row) != {"root", "name", "before", "after", "meta"}:
            raise TransactionError("invalid manifest row")
        name = row["name"]
        if (
            row["root"] not in self.roots
            or not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
        ):
            raise TransactionError("unanchored target")
        return self.roots[row["root"]] / name

    def write_journal(self, doc):
        atomic(
            self.journal,
            json.dumps(doc, sort_keys=True).encode(),
            {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o600},
        )

    def prepare(self, changes):
        if not self.stopped() or self.backup.exists():
            raise TransactionError("service not stopped or backup exists")
        rows = []
        before_images = []
        seen = set()
        for source in changes:
            row = {k: source[k] for k in ["root", "name", "before", "after", "meta"]}
            path = self.target(row)
            if str(path) in seen:
                raise TransactionError("duplicate target")
            seen.add(str(path))
            if sha(source["data"]) != row["after"]:
                raise TransactionError("new hash mismatch")
            try:
                old, meta = read_regular(path)
            except FileNotFoundError:
                old, meta = None, row["meta"]
            if (sha(old) if old is not None else None) != row["before"] or meta != row["meta"]:
                raise TransactionError("baseline drift")
            rows.append(row)
            before_images.append(old)
        self.backup.mkdir(mode=0o700)
        sync_dir(self.backup.parent)
        for index, (row, old, source) in enumerate(zip(rows, before_images, changes)):
            if old is not None:
                atomic(
                    self.backup / f"{index}.before",
                    old,
                    {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o600},
                )
            atomic(
                self.backup / f"{index}.after",
                source["data"],
                {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o600},
            )
        doc = {"phase": "prepared", "rows": rows}
        self.write_journal(doc)
        return doc

    def load(self, expected_rows):
        doc = json.loads(read_regular(self.journal)[0])
        if set(doc) != {"phase", "rows"} or doc["rows"] != expected_rows:
            raise TransactionError("journal differs from pinned manifest")
        for row in expected_rows:
            self.target(row)
        return doc

    def apply(self, expected_rows, checkpoint=lambda index: None):
        doc = self.load(expected_rows)
        if doc["phase"] != "prepared" or not self.stopped():
            raise TransactionError("not prepared/stopped")
        doc["phase"] = "applying"
        self.write_journal(doc)
        for index, row in enumerate(expected_rows):
            if not self.stopped():
                raise TransactionError("service became active")
            path = self.target(row)
            try:
                current = sha(read_regular(path)[0])
            except FileNotFoundError:
                current = None
            if current != row["before"]:
                raise TransactionError("target drift")
            data = read_regular(self.backup / f"{index}.after")[0]
            if sha(data) != row["after"]:
                raise TransactionError("staged source drift")
            atomic(path, data, row["meta"])
            checkpoint(index)
        doc["phase"] = "applied"
        self.write_journal(doc)

    def recover(self, expected_rows):
        doc = self.load(expected_rows)
        if doc["phase"] in {"committed", "restored"}:
            return doc["phase"]
        if doc["phase"] not in {"prepared", "applying", "applied"} or not self.stopped():
            raise TransactionError("recovery needs confirmed stopped service")
        # Validate ALL target/before images before restoring any file.
        for index, row in enumerate(expected_rows):
            try:
                current = sha(read_regular(self.target(row))[0])
            except FileNotFoundError:
                current = None
            if current not in {row["before"], row["after"]}:
                raise TransactionError("unexpected target; no destructive recovery")
            if (
                row["before"] is not None
                and sha(read_regular(self.backup / f"{index}.before")[0]) != row["before"]
            ):
                raise TransactionError("backup drift")
        for index, row in enumerate(expected_rows):
            path = self.target(row)
            if row["before"] is None:
                if path.exists():
                    # Only a manifest-created file with the exact new hash.
                    if sha(read_regular(path)[0]) != row["after"]:
                        raise TransactionError("new file drift")
                    path.unlink()
                    sync_dir(path.parent)
            else:
                atomic(path, read_regular(self.backup / f"{index}.before")[0], row["meta"])
        doc["phase"] = "restored"
        self.write_journal(doc)
        return "restored"

    def commit(self, expected_rows, qualified):
        doc = self.load(expected_rows)
        if doc["phase"] != "applied" or not qualified():
            raise TransactionError("runtime proof missing")
        for row in expected_rows:
            if sha(read_regular(self.target(row))[0]) != row["after"]:
                raise TransactionError("target drift before commit")
        doc["phase"] = "committed"
        self.write_journal(doc)
