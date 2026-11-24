import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

root = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "native_fixture_package",
    root / "__init__.py",
    submodule_search_locations=[str(root)],
)
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from native_fixture_package.native_integration import Driver

B = {
    "host": "server",
    "local": True,
    "session_id": "sess-fixture",
    "workspace": "/fixture",
    "generation": 1,
}


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.driver = Driver(Path(self.tmp.name), 99, "node", "/fixture")
        self.actions = []
        self.target = SimpleNamespace(
            session_id=B["session_id"], host="server", letter="II", generation=1
        )
        p = Path(self.tmp.name)
        (p / "pending").mkdir()
        (p / "done").mkdir()
        r = {
            "wake": {
                "wake_id": "new",
                "credential_scope": "review-alerts",
                "required_session": B["session_id"],
                "require_generation": 1,
                "require_letter": "II",
                "message_id": 123,
            },
            "pick": {"target": vars(self.target)},
            "attempts": 1,
            "in_flight": True,
            "accepted_at": 100,
        }
        f = p / "pending/new.json"
        f.write_text(json.dumps(r))
        f.chmod(0o600)

    def tearDown(self):
        self.driver.journal.close()
        self.tmp.cleanup()

    def test_interactive_binding_then_one_send_query(self):
        original = subprocess.Popen
        code = """const readline=require('readline');const rl=readline.createInterface({input:process.stdin});let req;rl.on('line',s=>{const x=JSON.parse(s);if(!req){req=x;console.log(JSON.stringify({verify:1,binding:req.binding}))}else if(x.ok){console.log(JSON.stringify({result:{ack:{commandId:req.command_id,status:'accepted',result:{type:'inputAccepted',inputId:req.command_id,delivery:'queue'}}}}));rl.close()}else process.exit(1)})"""

        def popen(argv, **kw):
            self.actions.append(argv)
            return original(["node", "-e", code], **kw)

        with patch(
            "native_fixture_package.native_integration.subprocess.Popen",
            side_effect=popen,
        ):
            r = self.driver(
                B,
                "new",
                "fixture",
                fresh=True,
                target=self.target,
                verify=lambda b: True,
            )
            self.assertEqual(r["outcome"], "unknown")
            r = self.driver.reconcile_only(B, "new", "fixture", verify=lambda b: True)
            self.assertEqual(r["outcome"], "unknown")
        self.assertEqual(len(self.actions), 2)

    def test_generation_race_fails_closed(self):
        original = subprocess.Popen
        count = 0
        code = """const r=require('readline').createInterface({input:process.stdin});let req;r.on('line',s=>{const x=JSON.parse(s);if(!req){req=x;console.log(JSON.stringify({verify:1,binding:req.binding}))}else {console.log(JSON.stringify({error:'refused'}));r.close()}})"""

        def verify(b):
            nonlocal count
            count += 1
            return count < 3

        with patch(
            "native_fixture_package.native_integration.subprocess.Popen",
            side_effect=lambda argv, **kw: original(["node", "-e", code], **kw),
        ):
            r = self.driver(
                B, "new", "fixture", fresh=True, target=self.target, verify=verify
            )
        self.assertEqual(r["outcome"], "unknown")

    def test_partial_response_eof_unknown(self):
        original = subprocess.Popen
        with patch(
            "native_fixture_package.native_integration.subprocess.Popen",
            side_effect=lambda argv, **kw: original(
                [
                    "node",
                    "-e",
                    "process.stdin.once('data',()=>{process.stdout.write('{');process.exit()})",
                ],
                **kw,
            ),
        ):
            r = self.driver(
                B,
                "new",
                "fixture",
                fresh=True,
                target=self.target,
                verify=lambda b: True,
            )
        self.assertEqual(r["outcome"], "unknown")

    def test_boolean_without_ledger_target_cannot_dispatch(self):
        r = self.driver(B, "new", "fixture", fresh=True, verify=lambda b: True)
        self.assertEqual(r["code"], "native-legacy-attempt-excluded")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class EvidenceIntegration(Integration):
    def test_target_binding_mismatch_cannot_dispatch(self):
        self.target.session_id = "other"
        r = self.driver(
            B, "new", "fixture", fresh=True, target=self.target, verify=lambda b: True
        )
        self.assertEqual(r["code"], "native-legacy-attempt-excluded")

    def test_durable_alert_id_pass_through(self):
        self.driver.journal.transport = lambda req: {}
        with patch(
            "native_fixture_package.native_integration.reconcile",
            return_value={"native_input": "persisted", "owning_alert": "fetched"},
        ) as read:
            result = self.driver(
                B, "new", "fixture", target=self.target, verify=lambda b: True
            )
        self.assertEqual(read.call_args.args[-1], 123)
        files = list((Path(self.tmp.name) / "native-evidence").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(
            json.loads(files[0].read_text())["evidence"]["owning_alert"], "fetched"
        )
        self.assertEqual(result["outcome"], "unknown")

    def test_report_persistence_failure_does_not_resend(self):
        calls = []
        self.driver.journal.transport = lambda req: calls.append(req["action"]) or {}
        with patch(
            "native_fixture_package.native_integration.persist",
            side_effect=OSError("full"),
        ):
            a = self.driver(
                B, "new", "fixture", target=self.target, verify=lambda b: True
            )
            b = self.driver.reconcile_only(B, "new", "fixture", verify=lambda b: True)
        self.assertEqual(calls, ["dispatch", "query"])
        self.assertEqual(a["evidence_persistence"], "unverified")
