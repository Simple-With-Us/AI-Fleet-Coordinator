"""Offline tests. No process signals, service calls, credentials or writes."""

import contextlib
import itertools
import unittest

from git_relay.deploy_guard import freeze_failures, freeze_qualified
from git_relay.deploy_host import LinuxHost
from git_relay.deploy_supervisor import Supervisor
from git_relay.deploy_transaction import TransactionError

ID = {"pid": 123, "boot": "fixture", "start_ticks": "1"}


class Diagnostics(unittest.TestCase):
    def test_existing_guard_result_unchanged_for_6480_inputs(self):
        expected_values = [
            None,
            {},
            {"pid": 0},
            {"pid": True},
            ID,
            {"pid": 124},
            [],
            1,
            "x",
            {"pid": -1},
        ]
        count = 0
        for expected, before, after, tasks, children, pending, job in itertools.product(
            expected_values,
            [ID, {}, None],
            [ID, {}, None],
            [{123: "T"}, {123: "S"}, {123: "T", 124: "T"}, {}],
            [[], ["9"]],
            [0, 1, None],
            [False, True, None],
        ):
            prior = (
                isinstance(expected, dict)
                and expected == before == after
                and type(expected.get("pid")) is int
                and expected["pid"] > 0
                and tasks == {expected["pid"]: "T"}
                and children == []
                and pending == 0
                and job is False
            )
            self.assertEqual(
                freeze_qualified(expected, before, after, tasks, children, pending, job), prior
            )
            count += 1
        self.assertEqual(count, 6480)

    def test_multiple_failed_predicates_and_exact_success(self):
        self.assertTrue(freeze_qualified(ID, ID, ID, {123: "T"}, [], 0, False))
        self.assertEqual(
            freeze_failures(ID, ID, ID, {123: "T", 124: "T"}, [], 1, False),
            ["single_frozen_main_task", "no_pending"],
        )
        self.assertEqual(
            freeze_failures(ID, {}, ID, {123: "T"}, ["9"], 0, True),
            ["identity_unchanged", "no_children", "no_unit_job"],
        )
        self.assertIn(
            "valid_pid",
            freeze_failures({"pid": True}, {"pid": True}, {"pid": True}, {True: "T"}, [], 0, False),
        )

    def test_supervisor_reports_failed_frozen_observations_without_stop(self):
        class Host:
            frozen = False

            def lock(self):
                return contextlib.nullcontext()

            def preflight(self, rows):
                pass

            def process_identity(self):
                return ID

            def tasks_and_children(self, expected):
                return ({123: "T", 124: "T"} if self.frozen else {123: "S"}), []

            def pending_count(self):
                return int(self.frozen)

            def stop_job_exists(self):
                return False

            def arm_recovery(self, *args):
                pass

            def freeze(self, *args):
                self.frozen = True

            def recovery_is_armed(self):
                return False

            def stop_without_kill(self, *args):
                raise AssertionError("unsafe stop reached")

        with self.assertRaisesRegex(
            TransactionError, "freeze predicates=single_frozen_main_task,no_pending"
        ):
            Supervisor(Host(), None, []).run()

    def test_recovery_adapter_reports_and_reconciles_without_stop(self):
        class Host:
            frozen = False
            reconciled = False

            def confirmed_stopped(self, expected):
                return False

            def properties(self):
                return {"MainPID": "123", "ActiveState": "active"}

            def process_identity(self):
                return ID

            def tasks_and_children(self, expected):
                return ({123: "T", 124: "T"} if self.frozen else {123: "S"}), []

            def pending_count(self):
                return int(self.frozen)

            def sockets_idle(self, expected):
                return True

            def freeze(self, expected):
                self.frozen = True

            def stop_job_exists(self):
                return False

            def stop_without_kill(self, *args):
                raise AssertionError("unsafe stop reached")

            def reconcile_frozen_stop(self, expected):
                self.reconciled = True
                return "running"

        host = Host()
        with self.assertRaisesRegex(
            TransactionError, "busy recovery image; no unsafe stop"
        ) as caught:
            LinuxHost.stop_for_recovery(host)
        self.assertEqual(
            caught.exception.failed_predicates, ("single_frozen_main_task", "no_pending")
        )
        self.assertIn(
            "freeze predicates=single_frozen_main_task,no_pending", caught.exception.__notes__
        )
        self.assertTrue(host.reconciled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
