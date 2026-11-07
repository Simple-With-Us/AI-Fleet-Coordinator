"""Pinned finite operation runner. Source prep, not an installed activation.

The root-owned standalone entry point imports this reviewed package from a
fixed private path and supplies its compiled manifest digest. No network or
caller request may select a manifest, destination, package or command.
"""

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .deploy_host import LinuxHost
from .deploy_journal import Journal
from .deploy_recovery import Recovery, supervisor_unit
from .deploy_supervisor import Supervisor
from .deploy_transaction import Transaction, TransactionError, read_regular


def private_anchor(path):
    path = Path(path)
    for p in [path, *path.parents]:
        s = p.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise TransactionError("runner anchor unsafe")


def executor_identity(proc=Path("/proc"), pid=None):
    pid = os.getpid() if pid is None else pid
    p = proc / str(pid)
    return {
        "pid": pid,
        "start_ticks": (p / "stat").read_text().rsplit(")", 1)[1].split()[19],
        "exe": str((p / "exe").resolve(strict=True)),
        "cgroup": (p / "cgroup").read_text(),
        "boot": (proc / "sys/kernel/random/boot_id").read_text().strip(),
    }


def observe_executor(expected):
    try:
        current = executor_identity(pid=expected["pid"])
        return current if current == expected else None
    except (FileNotFoundError, ProcessLookupError):
        return None


def load_manifest(directory, digest):
    private_anchor(directory)
    raw, meta = read_regular(Path(directory) / "manifest.json")
    if meta["uid"] != 0 or meta["mode"] & 0o022 or hashlib.sha256(raw).hexdigest() != digest:
        raise TransactionError("manifest drift")
    doc = json.loads(raw)
    if set(doc) != {"config", "files", "runner", "unit"}:
        raise TransactionError("manifest shape")
    if doc["unit"] != "git-relay-patch-1429.service":
        raise TransactionError("wrong executor unit")
    seen = set()
    changes = []
    for index, source in enumerate(doc["files"]):
        if set(source) != {"root", "name", "before", "after", "meta"}:
            raise TransactionError("file row shape")
        key = source["root"] + "/" + source["name"]
        if key in seen or source["root"] not in {"code", "state"}:
            raise TransactionError("duplicate or unanchored manifest row")
        seen.add(key)
        # Transaction.target provides the same strict name checks before I/O.
        if "/" in source["name"] or source["name"] in {"", ".", ".."}:
            raise TransactionError("bad manifest name")
        raw, meta = read_regular(Path(directory) / f"{index}.candidate")
        if (
            meta["uid"] != 0
            or meta["mode"] & 0o022
            or hashlib.sha256(raw).hexdigest() != source["after"]
        ):
            raise TransactionError("candidate drift")
        if source["before"] == source["after"]:
            raise TransactionError("no-op image row refused")
        changes.append(dict(source, data=raw))
    if not changes:
        raise TransactionError("empty manifest")
    return doc, changes


class Runner:
    def __init__(self, directory, manifest_digest, proof):
        self.directory = Path(directory)
        self.doc, self.changes = load_manifest(directory, manifest_digest)
        self.rows = [{k: v for k, v in row.items() if k != "data"} for row in self.changes]
        old = {r["root"] + "/" + r["name"]: r["before"] for r in self.rows}
        new = {r["root"] + "/" + r["name"]: r["after"] for r in self.rows}
        self.host = LinuxHost(self.doc["config"], old, new, None, proof)
        self.journal = Journal(
            directory,
            self.rows,
            lambda: self.host.hashes(old),
            observe_executor,
            lambda: executor_identity()["boot"],
        )
        entry, entry_meta = read_regular(Path(self.doc["runner"]))
        if entry_meta["uid"] != 0 or entry_meta["mode"] & 0o022:
            raise TransactionError("unsafe entry point")
        self.recovery = Recovery(
            directory,
            self.doc["runner"],
            hashlib.sha256(entry).hexdigest(),
            self.host.command,
            self.journal,
            executor_identity,
        )
        self.host.recovery = self.recovery
        self.tx = Transaction(
            {"code": self.host.CODE, "state": self.host.STATE},
            self.directory / "backup",
            lambda: self.host.confirmed_stopped(None),
        )

    def verify_unit(self):
        required = supervisor_unit(self.doc["runner"])
        # systemd show renders time/exec properties differently. Verify parsed
        # values, not a launch command or caller attestation.
        raw = self.host.command(
            "systemctl",
            "show",
            self.doc["unit"],
            "--property=User,Type,TimeoutStartUSec,TimeoutStopUSec,KillMode,ControlGroup,ExecStopPost",
        )
        observed = dict(line.split("=", 1) for line in raw.splitlines())
        for key in ["User", "Type", "KillMode"]:
            if observed.get(key) != required[key]:
                raise TransactionError("executor unit drift")
        if [observed.get(k) for k in ["TimeoutStartUSec", "TimeoutStopUSec"]] != [
            "2min",
            "2min",
            "1min 30s",
        ]:
            raise TransactionError("executor deadline drift")
        identity = executor_identity()
        if identity["cgroup"].strip() != "0::" + observed.get("ControlGroup", ""):
            raise TransactionError("not executing in bounded unit")
        if "path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I " + self.doc[
            "runner"
        ] + " recover ;" not in observed.get("ExecStopPost", ""):
            raise TransactionError("recovery hook drift")

    def deploy(self):
        self.verify_unit()
        return Supervisor(self.host, self.tx, self.rows, changes=self.changes).run()

    def reconcile_commit(self):
        if self.tx.journal.exists() and self.tx.load(self.rows)["phase"] == "committed":
            if self.host.hashes(self.host.new_hashes) != self.host.new_hashes:
                raise TransactionError("committed image drift")
            if self.journal.read()["phase"] == "applied":
                self.journal.qualified()
            return True
        return False

    def guard(self):
        # ExecStartPre is already inside the service start job. Never issue
        # systemctl stop/start recursively from here. MainPID must be zero.
        if self.host.properties().get("MainPID") != "0":
            raise TransactionError("guard found live relay")
        if self.reconcile_commit():
            return "new"
        # During supervised start the executor holds the lock. The read-only
        # applied/new decision is safe; only recovery needs the exclusive lock.
        decision = self.journal.decision(
            self.host.old_hashes, self.host.new_hashes, time.monotonic()
        )
        if decision == "recover":
            with self.host.lock():
                # Re-evaluate after lock acquisition; another recovery may
                # already have committed or restored the durable inventory.
                if self.reconcile_commit():
                    return "new"
                self.restore_stopped(guard=True)
            return "old"
        if decision not in {"old", "new"}:
            raise TransactionError("start guard refuses image")
        return decision

    def restore_stopped(self, guard=False):
        if self.tx.journal.exists():
            original = self.tx.stopped
            try:
                if guard:
                    self.tx.stopped = lambda: self.host.properties().get("MainPID") == "0"
                self.tx.recover(self.rows)
            finally:
                self.tx.stopped = original
        elif self.host.hashes(self.host.old_hashes) != self.host.old_hashes:
            raise TransactionError("no backup and baseline differs")
        self.journal.allow_old(self.rows)

    def recover(self):
        with self.host.lock():
            if not self.journal.path.exists():
                return "not-armed"
            if self.reconcile_commit():
                self.recovery.finish()
                return "committed"
            # Before apply, or after a completed rollback, a live exact old
            # image requires no stop. Failed-exit cleanup must not interrupt
            # busy original handlers merely because a guard was armed.
            phase = self.journal.read()["phase"]
            if (
                phase in {"prepared", "restored"}
                and self.host.hashes(self.host.old_hashes) == self.host.old_hashes
            ):
                if phase == "prepared":
                    self.journal.aborted_without_writes()
                if self.host.properties().get("ActiveState") == "active":
                    # An executor may have died after freezing the unchanged
                    # original. Resume only its current exact process identity;
                    # no file writes or handler interruption are needed.
                    self.host.resume_if_same(self.host.process_identity())
                    if not self.host.prove_old_runtime(self.rows):
                        raise TransactionError("unchanged old runtime proof failed")
                    self.recovery.finish()
                    return "aborted"
            self.host.stop_for_recovery()
            if not self.host.confirmed_stopped(None):
                raise TransactionError("recovery requires stopped relay")
            self.restore_stopped()
            self.host.start()
            if not self.host.prove_old_runtime(self.rows):
                raise TransactionError("old runtime proof failed")
            self.recovery.finish()
            return "restored"
