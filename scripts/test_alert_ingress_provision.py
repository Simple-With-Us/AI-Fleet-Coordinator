import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from fleet_infisical import SecretsError
from fleet_knobs.tests._fake import ScopedReadAPI, fixture_service_token

SPEC = importlib.util.spec_from_file_location(
    "alert_ingress_provision",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "alert-ingress", "provision-secrets.py"
    ),
)
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class FakeRead:
    """Stands in for the shared client's scoped read.

    The contract it reproduces:  one read answers with the names the folder holds,  and a
    name the folder does not hold is simply absent.
    """

    def __init__(self, answers, absent=()):
        self.answers = answers
        self.absent = set(absent)
        self.calls = []

    def __call__(self, project, path, env, *, token=None, names=None, **kwargs):
        self.calls.append((project, path, tuple(names or ())))
        found = {}
        for name in names or ():
            if name in self.absent:
                continue
            found[name] = self.answers[name]
        return found
class ServiceTokenTestCase(unittest.TestCase):
    def setUp(self):
        self.token_env = mock.patch.dict(
            os.environ,
            {
                provision.READ_TOKEN_AFC: "st." + "fixture-afc-read-token-value",
                provision.READ_TOKEN_FLEETOPS: "st." + "fixture-fleetops-read-token",
                provision.WRITE_TOKEN_AFC: "st." + "fixture-afc-write-token-value",
            },
            clear=True,
        )
        self.token_env.start()
        self.addCleanup(self.token_env.stop)
        project = mock.patch.object(
            provision.scoped_secrets,
            "infisical_project_id",
            lambda env, home, project="fleet-ops": "11111111-2222-3333-4444-555555555555",
        )
        project.start()
        self.addCleanup(project.stop)

    def read(self, answers, absent=()):
        """Patch the scoped read and return the fake."""
        fake = FakeRead(answers, absent)
        patcher = mock.patch.object(provision.scoped_secrets, "fetch_project", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class ScopedRead(ServiceTokenTestCase):
    """One scoped read, with the provisioner's own token for that project."""

    def test_one_name_is_read_as_itself(self):
        read = self.read({"ONLY_ONE": "the-value"})
        found = provision.scoped_values(["ONLY_ONE"], "fleet-ops", "/")
        self.assertEqual(found, {"ONLY_ONE": "the-value"})
        self.assertEqual(len(read.calls), 1)

    def test_several_names_come_from_one_read(self):
        read = self.read({"A": "one", "B": "two"})
        found = provision.scoped_values(["A", "B"], "fleet-ops", "/")
        self.assertEqual(found, {"A": "one", "B": "two"})
        self.assertEqual(len(read.calls), 1)

    def test_an_absent_name_is_left_out(self):
        read = self.read({"PRESENT": "the-value"}, absent=("GONE",))
        found = provision.scoped_values(["PRESENT", "GONE"], "fleet-ops", "/")
        self.assertEqual(found, {"PRESENT": "the-value"})
        self.assertEqual(len(read.calls), 1)


class BatchValues(ServiceTokenTestCase):
    """The environment first, one batched read second, an absence is an error."""

    def test_the_environment_comes_first(self):
        read = self.read({"A": "from-infisical"})
        with mock.patch.dict(os.environ, {"A": "from-the-environment"}):
            values = provision.batch_values(["A"], "fleet-ops", "/")
        self.assertEqual(values, {"A": "from-the-environment"})
        self.assertEqual(read.calls, [])

    def test_an_absent_name_is_an_error(self):
        self.read({}, absent=("MISSING",))
        with self.assertRaises(provision.ReadError) as caught:
            provision.batch_values(["MISSING"], "fleet-ops", "/")
        self.assertIn("MISSING is absent from fleet-ops/", caught.exception.reason)

    def test_three_names_are_one_export(self):
        # Fleet-OPS reads use this caller's distinct service token.
        read = self.read(
            {
                "CLOUDFLARE_FLEET_API_TOKEN": "the-token",
            }
        )
        values = provision.batch_values(
            ["CLOUDFLARE_FLEET_API_TOKEN"],
            "fleet-ops",
            "/",
        )
        self.assertEqual(len(read.calls), 1)
        self.assertEqual(values["CLOUDFLARE_FLEET_API_TOKEN"], "the-token")


class ServiceTokenWrites(ServiceTokenTestCase):
    """Writes use the caller's separate scoped token directly."""

    def test_the_values_come_from_the_caller(self):
        seen = {}

        def fake_api(method, url, token, data=None, timeout=35, write=None):
            seen["method"] = method
            seen["data"] = data
            return {"accessToken": "the-access-token"}

        metadata = {
            "workspace": provision.AFC,
            "projectId": provision.AFC,
            "permissions": ["read", "write"],
            "scopes": [{"environment": "prod", "secretPath": "/zulip"}],
            "expiresAt": "2099-01-01T00:00:00Z",
        }
        with (
            mock.patch.object(
                provision.scoped_secrets, "_api_json", return_value=(200, metadata)
            ) as verify,
            mock.patch.object(provision, "api", fake_api),
        ):
            provision.infisical_upsert(
                "st." + "fixture-afc-write-token-value", "INGRESS_SHARED_TOKEN", "secret"
            )
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[1], "/v2/service-token")
        self.assertEqual(
            verify.call_args.args[4][provision.scoped_secrets.ENV_WRITE_PATHS], "/zulip"
        )
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["data"]["secretValue"], "secret")

    def test_wrong_writer_scope_fails_before_write(self):
        metadata = {
            "workspace": provision.AFC,
            "projectId": provision.AFC,
            "permissions": ["read", "write"],
            "scopes": [{"environment": "prod", "secretPath": "/wrong"}],
            "expiresAt": "2099-01-01T00:00:00Z",
        }
        with mock.patch.object(provision.scoped_secrets, "_api_json", return_value=(200, metadata)):
            with mock.patch.object(provision, "api") as write:
                with self.assertRaisesRegex(provision.ReadError, "scope verification"):
                    provision.infisical_upsert(
                        "st." + "fixture-afc-write-token-value", "INGRESS_SHARED_TOKEN", "secret"
                    )
        write.assert_not_called()

    def test_an_absent_credential_is_refused(self):
        with self.assertRaises(provision.ReadError) as caught:
            provision.infisical_upsert("bad-token", "INGRESS_SHARED_TOKEN", "secret")
        self.assertEqual(
            caught.exception.reason, provision.WRITE_TOKEN_AFC + " is missing or invalid"
        )


if __name__ == "__main__":
    unittest.main()
