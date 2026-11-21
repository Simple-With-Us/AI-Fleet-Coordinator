import importlib.util
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "fwh", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet-worker-health.py")
)
fwh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fwh)

import fleet_infisical


class WorkerHealthReader(unittest.TestCase):
    def test_signature_matches_the_worker_scheme(self):
        body = fwh.request_body(1_800_000_000)
        self.assertEqual(
            json.loads(body), {"contract": "simple-tools-health/1", "sent_at": 1_800_000_000}
        )
        self.assertEqual(len(fwh.sign("k" * 40, body)), 64)
        self.assertNotEqual(fwh.sign("k" * 40, body), fwh.sign("j" * 40, body))

    def test_good_answer_keeps_seats_and_worst(self):
        doc = fwh.document(
            200,
            {
                "contract": "simple-tools-health/1",
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

    def test_read_key_survives_a_missing_token(self):
        old_env = dict(os.environ)
        os.environ.pop("HEALTH_HMAC_KEY", None)
        try:
            with mock.patch.object(fleet_infisical, "fetch_scoped", return_value=("", "no-token")):
                # A refused store is not an absent value:  the source that must be
                # repaired is named.
                with self.assertRaises(RuntimeError) as caught:
                    fwh.read_key()
                self.assertIn("worker-health-ro", str(caught.exception))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_read_key_survives_a_missing_binary(self):
        old_env = dict(os.environ)
        os.environ.pop("HEALTH_HMAC_KEY", None)
        old_cli = fwh.INFISICAL_CLI
        fwh.INFISICAL_CLI = "/nonexistent/fleet_infisical_cli.py"
        try:
            self.assertEqual(fwh.read_key(), "")
        finally:
            fwh.INFISICAL_CLI = old_cli
            os.environ.clear()
            os.environ.update(old_env)

    def test_read_key_comes_from_the_environment_first(self):
        old_env = dict(os.environ)
        os.environ["HEALTH_HMAC_KEY"] = "from-the-environment"
        try:
            self.assertEqual(fwh.read_key(), "from-the-environment")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_read_key_uses_the_own_scoped_token(self):
        old_env = dict(os.environ)
        os.environ.pop("HEALTH_HMAC_KEY", None)
        asked = []

        def read(name, folder, account, env=None, **options):
            asked.append((name, folder, account))
            return ("from-the-scoped-token", "ok")

        try:
            with mock.patch.object(fleet_infisical, "fetch_scoped", read):
                self.assertEqual(fwh.read_key(), "from-the-scoped-token")
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        self.assertEqual(asked, [("HEALTH_HMAC_KEY", "/", "worker-health-ro")])

    def test_requests_carry_an_explicit_user_agent(self):
        self.assertTrue(fwh.USER_AGENT.startswith("fleet-worker-health/"))

    def test_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state", "worker-health.json")
            fwh.write_atomic(path, {"schemaVersion": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(json.load(open(path)), {"schemaVersion": 1})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["worker-health.json"])

    def test_refuses_a_symlinked_state_directory(self):
        # BACKEND-36:  the report goes where the readers look for it, or nowhere.
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            with self.assertRaises(OSError):
                fwh.write_atomic(os.path.join(link, "worker-health.json"), {"schemaVersion": 1})
            self.assertEqual(os.listdir(real), [])

    def test_a_folder_swapped_for_a_symlink_after_the_check_is_refused(self):
        # Kody, PR 1477:  the folder is opened with O_NOFOLLOW and used through that descriptor,
        # so passing the early check is not enough to write through a link.
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            with mock.patch.object(fwh.os.path, "islink", return_value=False):
                with self.assertRaises(OSError):
                    fwh.write_atomic(os.path.join(link, "worker-health.json"), {"schemaVersion": 1})
            self.assertEqual(os.listdir(real), [])

    def test_the_folder_is_fsynced_after_the_rename(self):
        # BACKEND-36:  the rename only survives a power cut once the folder itself is synced.
        synced = []
        real_fsync = os.fsync

        def record(fd):
            synced.append(os.fstat(fd).st_ino)
            return real_fsync(fd)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state", "worker-health.json")
            with mock.patch.object(fwh.os, "fsync", side_effect=record):
                fwh.write_atomic(path, {"schemaVersion": 1})
            self.assertIn(os.stat(os.path.dirname(path)).st_ino, synced)
            self.assertIn(os.stat(path).st_ino, synced)


if __name__ == "__main__":
    unittest.main()
