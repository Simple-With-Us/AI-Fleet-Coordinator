import importlib.util
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

SPEC = importlib.util.spec_from_file_location(
    "fwh", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet-worker-health.py")
)
fwh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fwh)


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

    def test_synced_key_transport_precedes_any_provider_access(self):
        reader = mock.Mock(side_effect=AssertionError("must not read"))
        self.assertEqual(fwh.read_key({fwh.SECRET_NAME: " synced-test-value "}, reader), "synced-test-value")
        reader.assert_not_called()

    def test_no_generic_writer_other_consumer_or_missing_binding_fallback(self):
        own = "st." + secrets.token_urlsafe(32)
        for missing in (fwh.READ_BINDING, fwh.PROJECT_BINDING):
            env = {
                fwh.READ_BINDING: own, fwh.PROJECT_BINDING: "project",
                "INFISICAL_TOKEN": own, "INFISICAL_WRITE_TOKEN": own,
                "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": own,
                "INFISICAL_FLEET_OPS_PROJECT_ID": "other-project",
            }
            env.pop(missing)
            reader = mock.Mock(side_effect=AssertionError("must not read"))
            self.assertEqual(fwh.read_key(env, reader), "")
            reader.assert_not_called()

    def test_direct_read_is_named_exact_folder_bounded_and_has_no_ambient_environment(self):
        own = "st." + secrets.token_urlsafe(32)
        reader = mock.Mock(return_value={fwh.SECRET_NAME: "health-test-value", "UNRELATED": "ignored"})
        env = {fwh.READ_BINDING: own, fwh.PROJECT_BINDING: "project",
               "INFISICAL_WRITE_TOKEN": "ambient-writer", "FLEET_SECRETS_API": "https://wrong.invalid/api",
               "INFISICAL_READ_PATHS": "/unrelated"}
        with mock.patch("subprocess.run", side_effect=AssertionError("no app subprocess")):
            self.assertEqual(fwh.read_key(env, reader), "health-test-value")
        reader.assert_called_once_with("project", "/", {}, token=own,
                                       names=(fwh.SECRET_NAME,), timeout=20.0)

    def test_provider_failure_or_missing_value_is_closed_without_output_or_subprocess(self):
        import contextlib
        import io
        env = {fwh.READ_BINDING: "st." + secrets.token_urlsafe(32), fwh.PROJECT_BINDING: "project"}
        for value in ({}, {fwh.SECRET_NAME: None}, RuntimeError("provider-private-text")):
            reader = mock.Mock(side_effect=value) if isinstance(value, Exception) else mock.Mock(return_value=value)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), mock.patch(
                "subprocess.run", side_effect=AssertionError("no fallback")
            ):
                self.assertEqual(fwh.read_key(env, reader), "")
            self.assertEqual(output.getvalue(), "")

    def test_actual_shared_verifier_reads_only_the_named_root_secret(self):
        client = fwh._shared_client()
        project = "00000000-0000-4000-8000-000000000042"
        own = "st." + secrets.token_urlsafe(32)
        env = {fwh.READ_BINDING: own, fwh.PROJECT_BINDING: project}
        grant = {"workspace": project, "permissions": ["read"],
                 "scopes": [{"environment": "prod", "secretPath": "/"}],
                 "expiresAt": "2099-01-01T00:00:00Z"}
        calls = []

        def api(method, endpoint, supplied, body, source, **kwargs):
            calls.append(endpoint)
            self.assertEqual((method, supplied, body, source), ("GET", own, None, {}))
            if endpoint == "/v2/service-token":
                return 200, grant
            parsed = urlsplit(endpoint)
            self.assertEqual(parsed.path, "/v3/secrets/raw/HEALTH_HMAC_KEY")
            query = parse_qs(parsed.query)
            for key, expected in (("workspaceId", project), ("environment", "prod"),
                                  ("secretPath", "/"), ("recursive", "false"),
                                  ("include_imports", "false"), ("expandSecretReferences", "false")):
                self.assertEqual(query[key], [expected])
            return 200, {"secret": {"secretKey": fwh.SECRET_NAME, "secretValue": "health-test-value",
                                    "type": "shared", "workspace": project,
                                    "environment": "prod", "secretPath": "/"}}

        with mock.patch.object(client, "_api_json", side_effect=api):
            self.assertEqual(fwh.read_key(env), "health-test-value")
            self.assertEqual(len(calls), 2)
            for change in (
                {"workspace": "wrong-project"},
                {"permissions": ["read", "write"]},
                {"scopes": [{"environment": "prod", "secretPath": "/zulip"}]},
                {"scopes": [{"environment": "prod", "secretPath": "/"},
                            {"environment": "prod", "secretPath": "/unrelated"}]},
                {"expiresAt": None},
            ):
                original = dict(grant)
                grant.update(change)
                calls.clear()
                self.assertEqual(fwh.read_key(env), "")
                self.assertEqual(calls, ["/v2/service-token"])
                grant.clear()
                grant.update(original)

    def test_preloaded_client_from_another_origin_is_refused_before_use(self):
        foreign = types.SimpleNamespace(__file__="/other-runtime/fleet_infisical.py")
        foreign.fetch_project = mock.Mock(side_effect=AssertionError("must not use other origin"))
        env = {fwh.READ_BINDING: "st." + secrets.token_urlsafe(32), fwh.PROJECT_BINDING: "project"}
        with mock.patch.dict(sys.modules, fleet_infisical=foreign):
            self.assertEqual(fwh.read_key(env), "")
        foreign.fetch_project.assert_not_called()

    def test_isolated_payload_loads_only_its_sibling_and_missing_client_keeps_synced_transport(self):
        with tempfile.TemporaryDirectory() as root:
            payload = os.path.join(root, "payload")
            hostile = os.path.join(root, "hostile")
            os.makedirs(payload)
            os.makedirs(hostile)
            entry = os.path.join(payload, "fleet-worker-health.py")
            source = os.path.dirname(os.path.abspath(__file__))
            shutil.copy(os.path.join(source, "fleet-worker-health.py"), entry)
            client = os.path.join(payload, "fleet_infisical.py")
            shutil.copy(os.path.join(source, "fleet_infisical.py"), client)
            with open(os.path.join(hostile, "fleet_infisical.py"), "w") as handle:
                handle.write("raise RuntimeError('unreviewed module executed')\n")
            code = (
                "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
                "assert m['_shared_client']().__file__==sys.argv[2]; print('own-sibling')"
            )
            env = {"PATH": os.defpath, "PYTHONPATH": hostile}
            out = subprocess.run([sys.executable, "-I", "-c", code, entry, client],
                                 env=env, cwd=hostile, capture_output=True, text=True, timeout=5)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, "own-sibling"), out.stderr)
            os.unlink(client)
            code = (
                "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
                "assert m['read_key']({'HEALTH_HMAC_KEY':'synced-test-value'})=='synced-test-value'; "
                "assert m['read_key']({'FLEET_WORKER_HEALTH_INFISICAL_READ_TOKEN':'test-only',"
                "'FLEET_WORKER_HEALTH_INFISICAL_PROJECT_ID':'test-project'})==''"
            )
            out = subprocess.run([sys.executable, "-I", "-c", code, entry],
                                 env=env, cwd=hostile, capture_output=True, text=True, timeout=5)
            self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)

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
