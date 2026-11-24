import json
import tempfile
import unittest
from pathlib import Path

from zcode_native import NativeDelivery

B = dict(
    host="server",
    local=True,
    session_id="sess-fixture",
    workspace="/fixture",
    generation=1,
)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.folder = Path(self.tmp.name)
        self.calls = []
        self.failure = None
        self.status = "accepted"
        self.good = True

        def transport(req):
            self.calls.append(req)
            records = list(self.folder.glob("*.json"))
            assert len(records) == 1 and json.loads(records[0].read_text())["phase"] in (
                "dispatching",
                "dispatched",
            )
            if self.failure:
                raise self.failure
            return {
                "ack": dict(
                    commandId=req["command_id"],
                    status=self.status,
                    result=dict(
                        type="inputAccepted",
                        inputId=req["command_id"],
                        delivery="queue",
                    ),
                )
            }

        self.d = NativeDelivery(self.folder, transport, lambda b: self.good)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, **kw):
        return self.d.deliver(
            kw.pop("binding", B),
            kw.pop("wake", "new"),
            kw.pop("text", "fixture"),
            fresh=kw.pop("fresh", True),
        )

    def test_admission_is_not_confirmed(self):
        r = self.send()
        self.assertEqual(r["outcome"], "unknown")
        self.assertEqual(r["code"], "native-admitted-not-confirmed")

    def test_dispatch_once_then_query(self):
        self.send()
        self.send()
        self.assertEqual([x["action"] for x in self.calls], ["dispatch", "query"])

    def test_restart_query_only(self):
        self.send()
        self.d = NativeDelivery(self.folder, self.d.transport, lambda b: True)
        self.send()
        self.assertEqual(self.calls[-1]["action"], "query")

    def test_lost_ack_no_replay(self):
        self.failure = TimeoutError()
        self.send()
        self.failure = None
        self.send()
        self.assertEqual([x["action"] for x in self.calls], ["dispatch", "query"])

    def test_failed_ack_no_replay(self):
        self.status = "failed"
        self.send()
        self.send()
        self.assertEqual(self.calls[-1]["action"], "query")

    def test_changed_text_conflict(self):
        self.send()
        r = self.send(text="different")
        self.assertEqual(r["outcome"], "refused")
        self.assertEqual(len(self.calls), 1)

    def test_changed_generation_conflict(self):
        self.send()
        r = self.send(binding={**B, "generation": 2})
        self.assertEqual(r["outcome"], "refused")
        self.assertEqual(len(self.calls), 1)

    def test_changed_workspace_conflict(self):
        self.send()
        r = self.send(binding={**B, "workspace": "/other"})
        self.assertEqual(r["outcome"], "refused")
        self.assertEqual(len(self.calls), 1)

    def test_legacy_excluded(self):
        self.assertEqual(self.send(fresh=False)["code"], "native-legacy-attempt-excluded")
        self.assertEqual(self.calls, [])

    def test_mac_refused(self):
        self.assertEqual(self.send(binding={**B, "host": "mac"})["outcome"], "refused")
        self.assertEqual(self.calls, [])

    def test_remote_refused(self):
        self.assertEqual(self.send(binding={**B, "local": False})["outcome"], "refused")

    def test_stale_binding_refused(self):
        self.good = False
        self.assertEqual(self.send()["code"], "native-binding-changed")
        self.assertEqual(self.calls, [])

    def test_dispatching_crash_record_query_only(self):
        self.send()
        p = next(self.folder.glob("*.json"))
        r = json.loads(p.read_text())
        r["phase"] = "dispatching"
        p.write_text(json.dumps(r))
        self.send()
        self.assertEqual(self.calls[-1]["action"], "query")

    def test_query_failure_retains_uncertainty(self):
        self.send()
        self.failure = ConnectionError()
        r = self.send()
        self.assertEqual(r["code"], "native-query-unavailable")
        self.assertEqual(self.calls[-1]["action"], "query")

    def test_statuses_never_generic_success(self):
        for st in ["noop", "rejected", "stale", "failed", "duplicate"]:
            self.status = st
            r = self.send(wake=st)
            self.assertEqual(r["outcome"], "unknown")

    def test_wrong_ack_key(self):
        self.d.transport = lambda req: {"ack": {"commandId": "wrong", "status": "accepted"}}
        self.assertEqual(self.send()["code"], "native-unreconciled")

    def test_corrupt_record_never_dispatches(self):
        for n, bad in enumerate([{}, [], {"version": 1}, "invalid-json", {"identity": "wrong"}]):
            self.send(wake="corrupt" + str(n))
            from zcode_native import digest

            p = self.folder / (digest({"wake_key": "corrupt" + str(n)}) + ".json")
            p.write_text(bad if isinstance(bad, str) else json.dumps(bad))
            before = len(self.calls)
            r = self.send(wake="corrupt" + str(n))
            self.assertEqual(r["code"], "native-journal-corrupt")
            self.assertEqual(before, len(self.calls))

    def test_mutated_phase_refused(self):
        self.send()
        p = next(self.folder.glob("*.json"))
        r = json.loads(p.read_text())
        r["phase"] = "new"
        p.write_text(json.dumps(r))
        self.assertEqual(self.send()["code"], "native-journal-corrupt")

    def test_mutated_intent_refused(self):
        self.send()
        p = next(self.folder.glob("*.json"))
        r = json.loads(p.read_text())
        r["intent"]["text"] = "tampered"
        p.write_text(json.dumps(r))
        self.assertEqual(self.send()["code"], "native-journal-corrupt")

    def test_wrong_version_key_refused(self):
        for n, (k, v) in enumerate(
            [("version", 2), ("command_id", "wrong"), ("identity", "wrong")]
        ):
            wake = "field" + str(n)
            self.send(wake=wake)
            from zcode_native import digest

            p = self.folder / (digest({"wake_key": wake}) + ".json")
            r = json.loads(p.read_text())
            r[k] = v
            p.write_text(json.dumps(r))
            before = len(self.calls)
            self.assertEqual(self.send(wake=wake)["code"], "native-journal-corrupt")
            self.assertEqual(len(self.calls), before)

    def test_unsafe_journal_mode_refused(self):
        self.send()
        p = next(self.folder.glob("*.json"))
        p.chmod(0o644)
        self.assertEqual(self.send()["code"], "native-journal-corrupt")

    def test_save_failure_before_send(self):
        from unittest.mock import patch

        with patch("zcode_native._save", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(self.calls, [])

    def test_two_processes_only_one_dispatch(self):
        import multiprocessing
        import time

        counter = multiprocessing.get_context("fork").Value("i", 0)

        def work():
            def transport(req):
                if req["action"] == "dispatch":
                    with counter.get_lock():
                        counter.value += 1
                    time.sleep(0.1)
                return {}

            d = NativeDelivery(self.folder, transport, lambda b: True)
            d.deliver(B, "concurrent", "fixture", fresh=True)
            d.close()

        jobs = [multiprocessing.get_context("fork").Process(target=work) for _ in range(2)]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(5)
            self.assertEqual(job.exitcode, 0)
        self.assertEqual(counter.value, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
