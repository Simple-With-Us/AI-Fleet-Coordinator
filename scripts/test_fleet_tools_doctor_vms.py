"""Tests for the doctor's VM pull:  python3 scripts/test_fleet_tools_doctor_vms.py"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("doctor", HERE / "fleet-tools-doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)

GOOD = {
    "schemaVersion": 1,
    "tool": "fleet-vm-status",
    "generatedAt": "2026-11-02T12:00:00Z",
    "hostLabel": "fixture-vm",
    "seats": ["MC"],
}


def ssh_runner(payload: bytes, returncode: int = 0, calls: list | None = None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, payload, b"")

    return run


class VmPullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "vm-hosts.json"
        self.state = root / "vms"

    def write_config(self, entries):
        self.config.write_text(json.dumps({"vms": entries}))

    def pull(self, runner):
        return doctor.pull_vms(config_path=self.config, state_dir=self.state, runner=runner)

    def test_missing_config_is_a_noop(self):
        calls = []
        result = self.pull(ssh_runner(b"{}", calls=calls))
        self.assertEqual(result, [])
        self.assertEqual(calls, [])
        self.assertFalse(self.state.exists())

    def test_good_report_lands_private(self):
        self.write_config([{"label": "fixture-vm", "destination": "vm-ro"}])
        result = self.pull(ssh_runner(json.dumps(GOOD).encode()))
        self.assertEqual([(r["label"], r["ok"]) for r in result], [("fixture-vm", True)])
        written = self.state / "fixture-vm.json"
        self.assertEqual(json.loads(written.read_text())["hostLabel"], "fixture-vm")
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)

    def test_ssh_is_batch_strict_and_sends_no_command(self):
        calls = []
        self.write_config([{"label": "fixture-vm", "destination": "vm-ro", "user": "puller"}])
        self.pull(ssh_runner(json.dumps(GOOD).encode(), calls=calls))
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        joined = " ".join(argv)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertEqual(argv[-1], "puller@vm-ro")

    def test_bad_label_is_refused_before_any_ssh(self):
        for label in ("../escape", "a/b", "..", "", "-oProxyCommand=x", "with space"):
            calls = []
            self.write_config([{"label": label, "destination": "vm-ro"}])
            result = self.pull(ssh_runner(json.dumps(GOOD).encode(), calls=calls))
            self.assertFalse(result[0]["ok"], label)
            self.assertEqual(calls, [], label)
            self.assertFalse((self.state / f"{label}.json").exists(), label)

    def test_bad_destination_is_refused_before_any_ssh(self):
        calls = []
        self.write_config([{"label": "ok", "destination": "-oProxyCommand=evil"}])
        result = self.pull(ssh_runner(json.dumps(GOOD).encode(), calls=calls))
        self.assertFalse(result[0]["ok"])
        self.assertEqual(calls, [])

    def test_oversize_answer_is_refused(self):
        self.write_config([{"label": "big", "destination": "vm-ro"}])
        payload = b"x" * (doctor.MAX_VM_STATUS_BYTES + 1)
        result = self.pull(ssh_runner(payload))
        self.assertFalse(result[0]["ok"])
        self.assertFalse((self.state / "big.json").exists())

    def test_bad_json_keeps_the_old_report(self):
        self.write_config([{"label": "fixture-vm", "destination": "vm-ro"}])
        self.state.mkdir(parents=True)
        old = self.state / "fixture-vm.json"
        old.write_text(json.dumps(GOOD))
        result = self.pull(ssh_runner(b"not json"))
        self.assertFalse(result[0]["ok"])
        self.assertEqual(json.loads(old.read_text()), GOOD)

    def test_wrong_tool_or_version_keeps_the_old_report(self):
        self.write_config([{"label": "fixture-vm", "destination": "vm-ro"}])
        self.state.mkdir(parents=True)
        old = self.state / "fixture-vm.json"
        old.write_text(json.dumps(GOOD))
        for bad in ({**GOOD, "tool": "fleet-server-status"}, {**GOOD, "schemaVersion": 2}):
            result = self.pull(ssh_runner(json.dumps(bad).encode()))
            self.assertFalse(result[0]["ok"])
            self.assertEqual(json.loads(old.read_text()), GOOD)

    def test_one_failed_vm_never_fails_the_others(self):
        self.write_config(
            [
                {"label": "down", "destination": "down-ro"},
                {"label": "fixture-vm", "destination": "vm-ro"},
            ]
        )

        def run(argv, **kwargs):
            if argv[-1].endswith("down-ro"):
                return subprocess.CompletedProcess(argv, 255, b"", b"unreachable")
            return subprocess.CompletedProcess(argv, 0, json.dumps(GOOD).encode(), b"")

        result = self.pull(run)
        self.assertEqual(
            [(r["label"], r["ok"]) for r in result], [("down", False), ("fixture-vm", True)]
        )
        self.assertTrue((self.state / "fixture-vm.json").exists())

    def test_ssh_timeout_is_a_failed_pull_not_a_crash(self):
        self.write_config([{"label": "slow", "destination": "vm-ro"}])

        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

        result = self.pull(run)
        self.assertFalse(result[0]["ok"])

    def test_duplicate_label_is_refused_before_the_second_ssh(self):
        calls = []
        self.write_config(
            [
                {"label": "fixture-vm", "destination": "vm-ro"},
                {"label": "fixture-vm", "destination": "other-ro"},
            ]
        )
        result = self.pull(ssh_runner(json.dumps(GOOD).encode(), calls=calls))
        self.assertEqual(
            [(r["label"], r["ok"]) for r in result], [("fixture-vm", True), ("fixture-vm", False)]
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads((self.state / "fixture-vm.json").read_text()), GOOD)

    def test_destination_with_an_at_sign_is_refused_before_ssh(self):
        calls = []
        self.write_config([{"label": "ok", "destination": "evil@vm-ro", "user": "puller"}])
        result = self.pull(ssh_runner(json.dumps(GOOD).encode(), calls=calls))
        self.assertFalse(result[0]["ok"])
        self.assertEqual(calls, [])

    def test_pulls_fetch_concurrently_in_config_order(self):
        labels = ["vm-a", "vm-b", "vm-c"]
        self.write_config([{"label": label, "destination": "vm-ro"} for label in labels])
        barrier = threading.Barrier(len(labels))

        def run(argv, **kwargs):
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                return subprocess.CompletedProcess(argv, 255, b"", b"serial")
            return subprocess.CompletedProcess(argv, 0, json.dumps(GOOD).encode(), b"")

        result = self.pull(run)
        self.assertEqual(
            [(r["label"], r["ok"]) for r in result], [(label, True) for label in labels]
        )

    def test_malformed_config_is_a_failed_pull_not_a_crash(self):
        self.config.write_text("[1, 2")
        result = self.pull(ssh_runner(b"{}"))
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["ok"])


if __name__ == "__main__":
    unittest.main()
