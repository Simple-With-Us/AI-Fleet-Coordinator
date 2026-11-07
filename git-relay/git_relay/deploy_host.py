"""Finite Linux host primitives for existing git-relay deployment.

No CLI entry point: the reviewed root-owned transaction runner must supply
pinned inventory, durable journal, unit guard and recovery command first.
Every subprocess is bounded; this adapter never executes shell input.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from .deploy_guard import freeze_qualified
from .deploy_transaction import TransactionError, read_regular


class LinuxHost:
    UNIT = "git-relay.service"
    CODE = Path("/opt/git-relay/git-relay/git_relay")
    STATE = Path("/var/lib/private/git-relay")
    HEALTH = "http://127.0.0.1:8471/healthz"

    def __init__(
        self,
        expected_config,
        old_hashes,
        new_hashes,
        recovery,
        proof,
        run=subprocess.run,
        proc=Path("/proc"),
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.expected_config = expected_config
        self.old_hashes, self.new_hashes = old_hashes, new_hashes
        self.recovery, self.proof = recovery, proof
        self.run_command, self.proc, self.clock, self.sleep = run, proc, clock, sleep

    def command(self, *args):
        out = self.run_command(list(args), capture_output=True, text=True, timeout=20, check=False)
        if out.returncode:
            raise TransactionError("host command failed: " + args[0])
        return out.stdout.strip()

    def properties(self):
        out = self.command(
            "systemctl",
            "show",
            self.UNIT,
            "--property=MainPID,ActiveState,SubState,Restart,WatchdogUSec",
        )
        return dict(line.split("=", 1) for line in out.splitlines())

    def process_identity(self):
        state = self.properties()
        pid = int(state["MainPID"])
        if pid <= 0 or state["ActiveState"] != "active":
            raise TransactionError("relay not active")
        p = self.proc / str(pid)
        # /proc/PID/stat comm may include spaces or parentheses.
        ticks = (p / "stat").read_text().rsplit(")", 1)[1].split()[19]
        exe = str((p / "exe").resolve(strict=True))
        group = (p / "cgroup").read_text()
        if "git-relay.service" not in group or not exe.startswith("/usr/bin/python3"):
            raise TransactionError("unexpected relay process")
        return {
            "pid": pid,
            "start_ticks": ticks,
            "exe": exe,
            "cgroup": group,
            "boot": (self.proc / "sys/kernel/random/boot_id").read_text().strip(),
        }

    def tasks_and_children(self, expected):
        tasks, children = {}, []
        for p in (self.proc / str(expected["pid"]) / "task").iterdir():
            rows = dict(
                line.split(":", 1)
                for line in (p / "status").read_text().splitlines()
                if ":" in line
            )
            tasks[int(p.name)] = rows["State"].strip()[0]
            children.extend((p / "children").read_text().split())
        return tasks, children

    def pending_count(self):
        doc = json.loads(read_regular(self.STATE / "outcomes.json")[0])
        if not isinstance(doc, dict) or any(not isinstance(r, dict) for r in doc.values()):
            raise TransactionError("invalid outcome journal")
        return sum(row.get("state") == "pending" for row in doc.values())

    def stop_job_exists(self):
        rows = self.command("systemctl", "list-jobs", "--no-legend", "--plain")
        return any(self.UNIT in line for line in rows.splitlines())

    @contextlib.contextmanager
    def lock(self):
        path = Path("/run/lock/git-relay-patch.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != 0:
                raise TransactionError("deployment lock not root owned")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransactionError("deployment lock is busy") from None
            yield
        finally:
            os.close(fd)

    def hashes(self, expected):
        out = {}
        for key in expected:
            root, name = key.split("/", 1)
            if root not in {"code", "state"} or "/" in name or name in {"", ".", ".."}:
                raise TransactionError("unanchored hash target")
            path = (self.CODE if root == "code" else self.STATE) / name
            try:
                raw = read_regular(path)[0]
                out[key] = hashlib.sha256(raw).hexdigest()
            except FileNotFoundError:
                out[key] = None
        return out

    def preflight(self, rows):
        if os.getuid() != 0:
            raise TransactionError("root-owned supervisor required")
        for p in [self.CODE, self.STATE]:
            if p.is_symlink() or not p.is_dir() or p.stat().st_mode & 0o022:
                raise TransactionError("unsafe deployment anchor")
        config = Path("/etc/git-relay/config.env")
        if hashlib.sha256(read_regular(config)[0]).hexdigest() != self.expected_config:
            raise TransactionError("service config drift")
        if self.hashes(self.old_hashes) != self.old_hashes:
            raise TransactionError("source or policy drift")
        if self.properties().get("WatchdogUSec") != "0":
            raise TransactionError("active watchdog incompatible with freeze")
        if self.stop_job_exists():
            raise TransactionError("unit job already active")
        self.recovery.preflight(rows)

    def recovery_is_armed(self):
        return self.recovery.is_armed()

    def arm_recovery(self, rows, deadline):
        self.recovery.arm(rows, deadline)

    def freeze(self, expected):
        if self.process_identity() != expected:
            raise TransactionError("relay identity changed before freeze")
        os.kill(expected["pid"], signal.SIGSTOP)
        deadline = self.clock() + 2
        while self.clock() < deadline:
            if self.process_identity() != expected:
                raise TransactionError("relay identity changed while freezing")
            tasks, _children = self.tasks_and_children(expected)
            if tasks and all(s == "T" for s in tasks.values()):
                return
            self.sleep(0.02)
        raise TransactionError("freeze timeout")

    def resume_if_same(self, expected):
        try:
            if self.process_identity() == expected:
                os.kill(expected["pid"], signal.SIGCONT)
        except (FileNotFoundError, ProcessLookupError, TransactionError):
            pass

    def confirmed_stopped(self, expected):
        p = self.properties()
        return (
            p.get("MainPID") == "0"
            and p.get("ActiveState") == "inactive"
            and (expected is None or not (self.proc / str(expected["pid"])).exists())
        )

    def stop_without_kill(self, expected):
        # Caller has established the frozen process has no handler or child.
        # Do not resume a frozen accept loop until default SIGTERM is proven
        # pending. systemctl --no-block alone does not guarantee delivery before
        # SIGCONT and otherwise opens an accept/new-handler race.
        if self.process_identity() != expected:
            raise TransactionError("relay changed before termination")
        status = dict(
            line.split(":", 1)
            for line in (self.proc / str(expected["pid"]) / "status").read_text().splitlines()
            if ":" in line
        )
        term_bit = 1 << (signal.SIGTERM - 1)
        if (int(status["SigCgt"].strip(), 16) | int(status["SigIgn"].strip(), 16)) & term_bit:
            raise TransactionError("SIGTERM is not default termination")
        os.kill(expected["pid"], signal.SIGTERM)
        status = dict(
            line.split(":", 1)
            for line in (self.proc / str(expected["pid"]) / "status").read_text().splitlines()
            if ":" in line
        )
        if not ((int(status["SigPnd"].strip(), 16) | int(status["ShdPnd"].strip(), 16)) & term_bit):
            raise TransactionError("default SIGTERM not queued; keep frozen")
        # systemd may itself send SIGCONT after SIGTERM. Only request its stop
        # job after the default-termination signal is proven queued.
        self.command("systemctl", "stop", "--no-block", self.UNIT)
        self.resume_if_same(expected)
        deadline = self.clock() + 10
        while self.clock() < deadline:
            if self.confirmed_stopped(expected):
                return
            self.sleep(0.05)
        raise TransactionError("service stop timeout")

    def stop_for_recovery(self):
        if self.confirmed_stopped(None):
            return
        expected = self.process_identity()
        self.freeze(expected)
        tasks, children = self.tasks_and_children(expected)
        if not freeze_qualified(
            expected,
            self.process_identity(),
            self.process_identity(),
            tasks,
            children,
            self.pending_count(),
            self.stop_job_exists(),
        ):
            self.resume_if_same(expected)
            raise TransactionError("busy recovery image; no unsafe stop")
        self.stop_without_kill(expected)

    def allow_new_start(self, rows):
        self.recovery.allow_new(rows)

    def allow_old_start(self, rows):
        self.recovery.allow_old(rows)

    def start(self):
        self.command("systemctl", "start", self.UNIT)

    def mark_qualified(self):
        self.recovery.journal.qualified()

    def mark_aborted(self):
        self.recovery.journal.aborted_without_writes()

    def finish_recovery(self):
        self.recovery.finish()

    def runtime(self, expected, prior):
        identity = self.process_identity()
        if prior is not None and identity == prior:
            return False
        if self.hashes(expected) != expected:
            return False
        # Health is only one signal. Separate proof must perform actual own-App
        # negative patch validation and read-only action against the new process.
        with urlopen(self.HEALTH, timeout=3) as response:
            healthy = response.status == 200 and json.load(response) == {"ok": True}
        return healthy and self.proof(identity, expected) and self.process_identity() == identity

    def prove_new_runtime(self, prior, rows):
        return self.runtime(self.new_hashes, prior)

    def prove_old_runtime(self, rows):
        return self.runtime(self.old_hashes, None)
