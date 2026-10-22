import importlib.util
import json
import os
import stat
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "fwh", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet-worker-health.py")
)
fwh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fwh)


class WorkerHealthReader(unittest.TestCase):
    def test_signature_matches_the_worker_scheme(self):
        body = fwh.request_body(1_800_000_000)
        self.assertEqual(
            json.loads(body), {"contract": "agent-sync-health/1", "sent_at": 1_800_000_000}
        )
        self.assertEqual(len(fwh.sign("k" * 40, body)), 64)
        self.assertNotEqual(fwh.sign("k" * 40, body), fwh.sign("j" * 40, body))

    def test_good_answer_keeps_seats_and_worst(self):
        doc = fwh.document(
            200,
            {
                "contract": "agent-sync-health/1",
                "worst": "warn",
                "bundle": "ok",
                "seats": [{"seat": "ECHO", "state": "warn"}],
            },
            0,
        )
        self.assertTrue(doc["reachable"])
        self.assertEqual(doc["worst"], "warn")
        self.assertEqual(doc["seats"][0]["seat"], "ECHO")

    def test_bad_answers_become_unknown_with_a_fixed_code(self):
        for status, code in (
            (0, "no_answer"),
            (401, "not_configured_or_refused"),
            (404, "not_configured_or_refused"),
            (503, "http_503"),
        ):
            doc = fwh.document(status, None, 0)
            self.assertFalse(doc["reachable"])
            self.assertEqual((doc["worst"], doc["error"]), ("unknown", code))
        self.assertFalse(fwh.document(200, {"contract": "other"}, 0)["reachable"])

    def test_read_key_survives_a_missing_binary(self):
        old_env, old_which = dict(os.environ), fwh.shutil.which
        os.environ.pop("HEALTH_HMAC_KEY", None)
        fwh.shutil.which = lambda name: "/nonexistent/fleet-secrets"
        try:
            self.assertEqual(fwh.read_key(), "")
        finally:
            fwh.shutil.which = old_which
            os.environ.clear()
            os.environ.update(old_env)

    def test_requests_carry_an_explicit_user_agent(self):
        self.assertTrue(fwh.USER_AGENT.startswith("fleet-worker-health/"))

    def test_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state", "worker-health.json")
            fwh.write_atomic(path, {"schemaVersion": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(json.load(open(path)), {"schemaVersion": 1})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["worker-health.json"])


if __name__ == "__main__":
    unittest.main()
