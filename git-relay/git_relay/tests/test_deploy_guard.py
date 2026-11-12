import copy
import unittest

from git_relay.deploy_guard import freeze_qualified, start_decision


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.executor = {
            "pid": 123,
            "start_ticks": "456",
            "exe": "/usr/bin/python3",
            "cgroup": "/system.slice/patch.service",
            "boot": "boot-a",
        }
        self.old = {"a": "old", "policy": "old"}
        self.new = {"a": "new", "policy": "new"}

    def decide(self, phase, **kw):
        args = {
            "phase": phase,
            "executor": self.executor,
            "observed_executor": self.executor,
            "observed_boot": "boot-a",
            "current_hashes": self.new,
            "old_hashes": self.old,
            "new_hashes": self.new,
        }
        args.update(kw)
        return start_decision(**args)

    def test_reboot_executor_loss_reuse_and_timeout_require_recovery(self):
        for phase in ["prepared", "applying", "restoring"]:
            self.assertEqual(self.decide(phase), "recover")
        self.assertEqual(self.decide("applied"), "new")
        self.assertEqual(self.decide("applied", observed_executor=None), "recover")
        self.assertEqual(self.decide("applied", observed_boot="boot-b"), "recover")
        for key in self.executor:
            other = copy.deepcopy(self.executor)
            other[key] = "changed"
            self.assertEqual(self.decide("applied", observed_executor=other), "recover")
        self.assertEqual(self.decide("applied", executor={}), "refuse")

    def test_mixed_images_unknown_phase_fail_closed(self):
        mixed = {"a": "new", "policy": "old"}
        for phase in ["applied", "committed", "restored"]:
            self.assertEqual(self.decide(phase, current_hashes=mixed), "refuse")
        self.assertEqual(self.decide("unexpected"), "refuse")
        self.assertEqual(self.decide("committed"), "new")
        self.assertEqual(self.decide("restored", current_hashes=self.old), "old")

    def test_freeze_refuses_inflight_handler_child_pending_pid_drift_and_stop_job(self):
        args = {
            "expected": self.executor,
            "before": self.executor,
            "after": self.executor,
            "task_states": {123: "T"},
            "children": [],
            "pending": 0,
            "stop_job": False,
        }
        self.assertTrue(freeze_qualified(**args))
        for update in [
            {"task_states": {123: "T", 124: "T"}},
            {"task_states": {123: "S"}},
            {"children": [125]},
            {"pending": 1},
            {"after": {}},
            {"stop_job": True},
        ]:
            self.assertFalse(freeze_qualified(**{**args, **update}))
