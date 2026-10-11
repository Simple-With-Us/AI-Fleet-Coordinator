"""The backup health pass-through and the tolerant interface readers."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_facts import backup as K
from fleet_facts import interfaces as I
from fleet_facts.runner import Result

from .fakes import FakeRunner, out

HEALTH = {"schema": 1, "verdict": "warn", "reason": "last complete backup 14.0 hours ago.  @**all** [x](https://evil.example/)",
          "installed": True, "loaded": True, "not_ok_in_a_row": 1, "last_line": "run ok `x`", "warn": ["stale", "@all"],
          "disk_free_gb": 120.5}


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_backup(self):
        script = self.home / "apps" / "fleet-work-backup" / "fleet_work_backup.py"
        script.parent.mkdir(parents=True)
        script.write_text("# stand-in\n")


class Backup(Home):
    def test_passes_the_report_through_and_cleans_the_text(self):
        self.install_backup()
        runner = FakeRunner(lambda argv: out(json.dumps(HEALTH)))
        with mock.patch.object(K, "which", return_value=None):
            result = K.gather(runner)
        self.assertEqual(result["status"], "ok")
        report = result["data"]["report"]
        self.assertEqual(result["data"]["verdict"], "warn")
        self.assertEqual(report["not_ok_in_a_row"], 1)
        self.assertEqual(report["disk_free_gb"], 120.5)
        self.assertNotIn("@", report["reason"] + report["last_line"] + "".join(report["warn"]))
        self.assertNotIn("://", report["reason"])
        argv = runner.calls[0]["argv"]
        self.assertEqual(argv[-2:], ["health", "--json"])
        self.assertTrue(argv[1].endswith("fleet_work_backup.py"))

    def test_uses_the_installed_command_when_there_is_one(self):
        runner = FakeRunner(lambda argv: out(json.dumps(HEALTH)))
        with mock.patch.object(K, "which", return_value="/example/bin/fleet-work-backup"):
            K.gather(runner)
        self.assertEqual(runner.calls[0]["argv"], ["/example/bin/fleet-work-backup", "health", "--json"])

    def test_not_installed_is_absent(self):
        runner = FakeRunner(lambda argv: out("{}"))
        with mock.patch.object(K, "which", return_value=None):
            result = K.gather(runner)
        self.assertEqual(result["status"], "absent")
        self.assertEqual(runner.calls, [])

    def test_no_mac_is_absent_and_runs_nothing(self):
        self.install_backup()
        runner = FakeRunner(lambda argv: out("{}"))
        self.assertEqual(K.gather(runner, no_mac=True)["status"], "absent")
        self.assertEqual(runner.calls, [])

    def test_failures_are_failed(self):
        self.install_backup()
        with mock.patch.object(K, "which", return_value=None):
            for res in (Result(1, "", "x"), Result(0, "nope"), Result(0, "[]"), Result(0, "{}"), Result(None, reason="timed out after 120s")):
                self.assertEqual(K.gather(FakeRunner(lambda argv, r=res: r))["status"], "failed", res)


class Commands(Home):
    def test_a_missing_program_is_absent_not_an_error(self):
        runner = FakeRunner(lambda argv: out("{}"))
        with mock.patch.object(I, "which", return_value=None):
            for part in I.COMMANDS:
                self.assertEqual(I.gather(part, runner)["status"], "absent", part)
        self.assertEqual(runner.calls, [])

    def test_exact_commands(self):
        runner = FakeRunner(lambda argv: out(json.dumps({"n": 1})))
        with mock.patch.object(I, "which", side_effect=lambda name: "/x/" + name):
            for part in I.COMMANDS:
                I.gather(part, runner)
        self.assertEqual(runner.argvs, [
            ["/x/fleet-autopush", "status", "--json"],
            ["/x/agent-sync", "asks", "status", "--json"],
            ["/x/needs-owner", "list", "--all", "--json"],
            ["/x/consult", "status", "--all", "--json"]])

    def test_a_document_is_carried_through(self):
        with mock.patch.object(I, "which", return_value="/x/fleet-autopush"):
            result = I.gather("autopush", FakeRunner(lambda argv: out(json.dumps({"lanes": [1, 2]}))))
        self.assertEqual(result, {"status": "ok", "reason": None, "data": {"document": {"lanes": [1, 2]}}})

    def test_failures_are_failed(self):
        with mock.patch.object(I, "which", return_value="/x/consult"):
            self.assertEqual(I.gather("consult", FakeRunner(lambda a: out("", rc=3)))["status"], "failed")
            self.assertEqual(I.gather("consult", FakeRunner(lambda a: out("not json")))["status"], "failed")
            self.assertEqual(I.gather("consult", FakeRunner(lambda a: Result(None, reason="timed out after 60s")))["status"], "failed")
            big = out("[" + "1," * I.MAX_DOCUMENT_BYTES + "1]")
            self.assertEqual(I.gather("consult", FakeRunner(lambda a: big))["status"], "failed")

    def test_no_mac_is_absent_for_every_interface(self):
        runner = FakeRunner(lambda argv: out("{}"))
        for part in I.PARTS:
            self.assertEqual(I.gather(part, runner, no_mac=True)["reason"], "--no-mac", part)
        self.assertEqual(runner.calls, [])


class Report(Home):
    def test_report_latest_is_read_from_its_state_file(self):
        self.assertEqual(I.gather("report", FakeRunner(lambda a: out("")))["status"], "absent")
        path = I.report_path()
        self.assertEqual(path, self.home / "apps" / "claim-expiry" / "state" / "report-latest.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 1, "released": 0}))
        result = I.gather("report", FakeRunner(lambda a: out("")))
        self.assertEqual(result["data"]["document"], {"schema": 1, "released": 0})
        path.write_text("{broken")
        self.assertEqual(I.gather("report", FakeRunner(lambda a: out("")))["status"], "failed")


if __name__ == "__main__":
    unittest.main()
