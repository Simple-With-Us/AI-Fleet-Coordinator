import contextlib
import unittest

from git_relay.deploy_supervisor import Supervisor
from git_relay.deploy_transaction import TransactionError


class FakeHost:
    def __init__(self, fail=None, busy=False):
        self.events = []
        self.fail = fail
        self.busy = busy
        self.stopped = False
        self.ident = {
            "pid": 42,
            "start_ticks": "100",
            "exe": "python",
            "cgroup": "relay",
            "boot": "a",
        }
        self.armed = False

    @contextlib.contextmanager
    def lock(self):
        self.events.append("lock")
        yield
        self.events.append("unlock")

    def step(self, name):
        self.events.append(name)
        if self.fail == name:
            raise RuntimeError(name)

    def preflight(self, rows):
        self.step("preflight")

    def arm_recovery(self, rows, deadline):
        self.step("arm")
        self.armed = True

    def recovery_is_armed(self):
        return self.armed

    def process_identity(self):
        return self.ident

    def freeze(self, ident):
        self.step("freeze")

    def tasks_and_children(self, ident):
        return ({42: "T", 43: "T"} if self.busy else {42: "T"}, [])

    def pending_count(self):
        return 0

    def stop_job_exists(self):
        return False

    def stop_without_kill(self, ident):
        self.step("stop")
        self.stopped = True

    def confirmed_stopped(self, ident):
        return self.stopped

    def allow_new_start(self, rows):
        self.step("allow_new")

    def start(self):
        self.step("start")
        self.stopped = False

    def prove_new_runtime(self, expected, rows):
        self.step("prove_new")
        return True

    def mark_qualified(self):
        self.step("qualified")

    def mark_aborted(self):
        self.step("aborted")

    def finish_recovery(self):
        self.step("finish")
        self.armed = False

    def resume_if_same(self, expected):
        self.step("resume_same")

    def stop_for_recovery(self):
        self.step("recovery_stop")
        self.stopped = True

    def allow_old_start(self, rows):
        self.step("allow_old")

    def prove_old_runtime(self, rows):
        self.step("prove_old")
        return True


class FakeTransaction:
    def __init__(self, host):
        self.host = host

    def apply(self, rows):
        assert self.host.stopped
        self.host.step("apply")

    def commit(self, rows, qualified):
        assert qualified()
        self.host.step("commit")

    def recover(self, rows):
        assert self.host.stopped
        self.host.step("recover")


class SupervisorTests(unittest.TestCase):
    def test_happy_order(self):
        h = FakeHost()
        self.assertEqual(Supervisor(h, FakeTransaction(h), []).run(), "committed")
        for first, second in [
            ("arm", "freeze"),
            ("stop", "apply"),
            ("apply", "start"),
            ("prove_new", "commit"),
            ("commit", "finish"),
        ]:
            self.assertLess(h.events.index(first), h.events.index(second))
        self.assertFalse(h.armed)

    def test_failure_at_each_mutation_restores_under_guard(self):
        for fail in ["apply", "allow_new", "prove_new", "commit"]:
            with self.subTest(fail=fail):
                h = FakeHost(fail=fail)
                with self.assertRaises(RuntimeError):
                    Supervisor(h, FakeTransaction(h), []).run()
                self.assertIn("recover", h.events)
                self.assertIn("prove_old", h.events)
                self.assertFalse(h.armed)

    def test_busy_freeze_never_applies(self):
        h = FakeHost(busy=True)
        with self.assertRaises(TransactionError):
            Supervisor(h, FakeTransaction(h), []).run()
        self.assertNotIn("apply", h.events)
        self.assertIn("resume_same", h.events)
        self.assertNotIn("recovery_stop", h.events)
        self.assertNotIn("recover", h.events)

    def test_recovery_failure_retains_guard(self):
        h = FakeHost(fail="recovery_stop")

        def fail_apply(rows):
            h.stopped = False
            raise RuntimeError("apply failed")

        transaction = FakeTransaction(h)
        transaction.apply = fail_apply
        with self.assertRaises(RuntimeError):
            Supervisor(h, transaction, []).run()
        self.assertTrue(h.armed)
        self.assertNotIn("finish", h.events)

    def test_no_journal_cleanup_before_arm(self):
        for fail in ["preflight", "arm"]:
            h = FakeHost(fail=fail)
            with self.assertRaisesRegex(RuntimeError, fail):
                Supervisor(h, FakeTransaction(h), []).run()
            self.assertNotIn("aborted", h.events)
            self.assertNotIn("finish", h.events)

    def test_abort_window_after_stop_restarts_verifies_old_and_only_then_disarms(self):
        for fail_proof in [False, True]:
            h = FakeHost()
            original_stop = h.stop_without_kill

            def stop(expected, original_stop=original_stop):
                original_stop(expected)
                raise RuntimeError("deadline after successful stop")

            h.stop_without_kill = stop
            if fail_proof:
                h.prove_old_runtime = lambda rows: False
            with self.subTest(fail_proof=fail_proof), self.assertRaises(RuntimeError):
                Supervisor(h, FakeTransaction(h), []).run()
            self.assertNotIn("apply", h.events)
            self.assertLess(h.events.index("aborted"), h.events.index("start"))
            self.assertEqual(h.armed, fail_proof)
            if not fail_proof:
                self.assertLess(h.events.index("start"), h.events.index("prove_old"))
                self.assertLess(h.events.index("prove_old"), h.events.index("finish"))
