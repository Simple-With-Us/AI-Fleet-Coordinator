"""Root-owned finite recovery wiring for an existing systemd service.

Prepare source only. Not a daemon and not an installer entry point. A reviewed
runner supplies exact source/config inventory and calls this under its lock.
The installed start guard calls the same pinned runner with `guard`; its
failed-exit recovery calls `recover`. No strings come from a caller request.
"""

import hashlib
import os
import stat
from pathlib import Path

from .deploy_transaction import TransactionError, atomic, read_regular, sync_dir


class Recovery:
    DROPIN = Path("/etc/systemd/system/git-relay.service.d/95-patch-transaction.conf")

    def __init__(
        self, directory, runner, runner_hash, command, journal, executor_identity, root=Path("/")
    ):
        self.directory = Path(directory)
        self.runner = Path(runner)
        self.runner_hash = runner_hash
        self.command = command
        self.journal = journal
        self.executor_identity = executor_identity
        self.root = Path(root)
        self.dropin = self.root / str(self.DROPIN).lstrip("/")
        self.original = None

    def is_armed(self):
        return self.journal.path.exists()

    def preflight(self, rows):
        if os.getuid() != 0:
            raise TransactionError("recovery must run as root")
        for p in [self.directory, self.runner.parent, self.dropin.parent]:
            s = p.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
                raise TransactionError("recovery anchor not root owned/private")
        data, meta = read_regular(self.runner)
        if (
            meta["uid"] != 0
            or meta["mode"] & 0o022
            or hashlib.sha256(data).hexdigest() != self.runner_hash
        ):
            raise TransactionError("recovery runner drift")
        # Absolute fixed path, no systemd expansion, whitespace or shell syntax.
        if any(
            c not in "/._-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            for c in str(self.runner)
        ):
            raise TransactionError("runner path unsafe for unit")
        if self.dropin.exists() or self.dropin.is_symlink():
            raise TransactionError("transaction drop-in already exists")
        if not rows:
            raise TransactionError("empty deployment inventory")

    def arm(self, rows, deadline):
        # Persist before guard publication. Runner `guard` refuses all pending
        # phases except applied/new under the exact still-live executor identity.
        self.journal.arm(rows, self.executor_identity(), deadline)
        text = "[Service]\nExecStartPre=+/usr/bin/python3 -I " + str(self.runner) + " guard\n"
        atomic(self.dropin, text.encode(), {"uid": 0, "gid": 0, "mode": 0o644})
        self.command("systemctl", "daemon-reload")
        # No separate timer is needed: root runner is itself launched under
        # finite RuntimeMaxSec and ExecStopPost=...recover. Its unit is verified
        # independently before this method may run.
        self.journal.guard_installed()

    def allow_new(self, rows):
        self.journal.allow_new(rows, self.executor_identity())

    def allow_old(self, rows):
        self.journal.allow_old(rows)

    def finish(self):
        if not self.journal.safe_to_finish():
            raise TransactionError("recovery cannot be disarmed")
        try:
            current, meta = read_regular(self.dropin)
        except FileNotFoundError:
            # ExecStopPost runs on success too; a crash after durable unlink
            # must be safely repeatable without re-creating any guard.
            self.command("systemctl", "daemon-reload")
            self.journal.finished()
            return
        expected = (
            "[Service]\nExecStartPre=+/usr/bin/python3 -I " + str(self.runner) + " guard\n"
        ).encode()
        if current != expected or meta["uid"] != 0:
            raise TransactionError("start guard changed; refusing removal")
        self.dropin.unlink()
        sync_dir(self.dropin.parent)
        self.command("systemctl", "daemon-reload")
        self.journal.finished()


def supervisor_unit(runner):
    """Transient finite unit properties, checked against actual systemd state."""
    runner = str(runner)
    if not runner.startswith("/") or any(
        c not in "/._-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        for c in runner
    ):
        raise TransactionError("unsafe runner path")
    return {
        "Type": "oneshot",
        "User": "root",
        "TimeoutStartSec": "120s",
        "RuntimeMaxSec": "120s",
        "TimeoutStopSec": "90s",
        "KillMode": "control-group",
        "ExecStopPost": "/usr/bin/python3 -I " + runner + " recover",
        "UMask": "0077",
    }
