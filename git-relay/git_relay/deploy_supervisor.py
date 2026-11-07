"""Finite transaction orchestration over a host adapter, no service of its own.

The production adapter must be root-owned, path/hash pinned and independently
reviewed. Its exclusive systemd execution unit has RuntimeMaxSec and ExecStopPost
recovery, plus the durable existing-service start guard. This module does not
install units, access private policy, execute SSH or supply owner authority.
"""

from __future__ import annotations

import time

from .deploy_guard import freeze_qualified
from .deploy_transaction import TransactionError


class Supervisor:
    def __init__(self, host, transaction, rows, timeout=90, clock=time.monotonic, changes=None):
        self.host = host
        self.transaction = transaction
        self.rows = rows
        self.timeout = timeout
        self.changes = changes
        self.clock = clock
        self.deadline = 0

    def check(self):
        if self.clock() >= self.deadline:
            raise TransactionError("deployment deadline exceeded")

    def run(self):
        self.deadline = self.clock() + self.timeout
        frozen = False
        stopped = False
        applying = False
        committed = False
        # Context manager holds flock for the entire process. Adapter refuses
        # conflicting lock, source/config drift or insufficient backup capacity.
        with self.host.lock():
            self.host.preflight(self.rows)
            self.check()
            # Before freezing, bind timeout/failed-exit recovery to durable
            # root-owned inventory and install start guard with original config
            # byte-for-byte backed up. No module or policy change yet.
            expected = self.host.process_identity()
            try:
                self.host.arm_recovery(self.rows, self.deadline)
                frozen = True
                self.host.freeze(expected)
                self.check()
                before = self.host.process_identity()
                states, children = self.host.tasks_and_children(expected)
                pending = self.host.pending_count()
                after = self.host.process_identity()
                if not freeze_qualified(
                    expected, before, after, states, children, pending, self.host.stop_job_exists()
                ):
                    raise TransactionError("active or changed relay; abort")
                # Freeze blocks accepts and Python handlers. Single exact task
                # means no live request thread; child check rules out minter.
                self.host.stop_without_kill(expected)
                self.check()
                if not self.host.confirmed_stopped(expected):
                    raise TransactionError("stop not confirmed")
                frozen = False
                stopped = True
                applying = True
                if self.changes is not None:
                    prepared = self.transaction.prepare(self.changes)
                    if prepared["rows"] != self.rows:
                        raise TransactionError("prepared inventory differs from pinned rows")
                self.transaction.apply(self.rows)
                self.check()
                # Start guard allows only this exact bounded executor and new
                # complete file inventory. No reboot may use that exception.
                self.host.allow_new_start(self.rows)
                self.host.start()
                stopped = False
                self.check()
                if not self.host.prove_new_runtime(expected, self.rows):
                    raise TransactionError("new runtime proof failed")
                self.transaction.commit(
                    self.rows, lambda: self.host.prove_new_runtime(expected, self.rows)
                )
                committed = True
                self.host.mark_qualified()
                self.host.finish_recovery()
                return "committed"
            except BaseException:
                if not self.host.recovery_is_armed():
                    # No durable journal means arm never happened. Preserve the
                    # original failure rather than masking it with cleanup.
                    raise
                if committed:
                    # Durable file journal is already committed after both
                    # runtime proofs. Failed-exit recovery reconciles the
                    # supervisor journal; never pretend this is an old image.
                    raise
                if frozen:
                    # Resume only the exact frozen PID/start/boot identity.
                    self.host.resume_if_same(expected)
                if not applying and not stopped:
                    # An aborted busy freeze changed no service image. Resume
                    # the original process without stopping its live handlers.
                    self.host.mark_aborted()
                    if self.host.confirmed_stopped(None):
                        self.host.start()
                        if not self.host.prove_old_runtime(self.rows):
                            raise TransactionError("aborted old runtime unverified") from None
                    self.host.finish_recovery()
                    raise
                if not stopped:
                    # Adapter uses the same exact-request-free freeze proof for
                    # a new-image rollback; never SIGTERM an active handler.
                    self.host.stop_for_recovery()
                if not self.host.confirmed_stopped(None):
                    raise TransactionError("recovery stop unconfirmed; guard stays armed") from None
                self.transaction.recover(self.rows)
                self.host.allow_old_start(self.rows)
                self.host.start()
                if not self.host.prove_old_runtime(self.rows):
                    raise TransactionError("old runtime unverified; recovery stays armed") from None
                self.host.finish_recovery()
                raise
