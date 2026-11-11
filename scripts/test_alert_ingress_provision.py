import importlib.util
import json
import os
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "alert_ingress_provision",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "alert-ingress", "provision-secrets.py"
    ),
)
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class FakeProc:
    """What `fleet-secrets get` hands back."""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeCLI:
    """Stands in for `fleet-secrets get NAME...`.

    The contract it reproduces is scripts/fleet_secrets/cli.py:133-136:  one
    name answers with the raw value and no wrapper, several answer with a JSON
    object.  A name the folder does not hold refuses the whole batch with
    exit 3 and names the missing ones on stderr.
    """

    def __init__(self, answers, absent=()):
        self.answers = answers
        self.absent = set(absent)
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        names = []
        path = ""
        after_get = argv[argv.index("get") + 1 :]
        for token in after_get:
            if token == "--path":
                break
            names.append(token)
        if "--path" in argv:
            path = argv[argv.index("--path") + 1]
        missing = [name for name in names if name in self.absent]
        if missing:
            return FakeProc(
                3,
                b"",
                "key {} is not in Infisical at {}".format(", ".join(missing), path).encode(),
            )
        values = {name: self.answers[name] for name in names}
        if len(names) == 1:
            return FakeProc(0, values[names[0]].encode())
        return FakeProc(0, json.dumps(values).encode())


class ServiceTokenTestCase(unittest.TestCase):
    def setUp(self):
        self.token_env = mock.patch.dict(
            os.environ,
            {
                provision.READ_TOKEN_AFC: "st." + "fixture-afc-read-token-value",
                provision.READ_TOKEN_FLEETOPS: "st." + "fixture-fleetops-read-token",
                provision.WRITE_TOKEN_AFC: "st." + "fixture-afc-write-token-value",
            },
            clear=False,
        )
        self.token_env.start()
        self.addCleanup(self.token_env.stop)


class OneNameBatch(ServiceTokenTestCase):
    """A batch of one answers with the raw value, not with a JSON object."""

    def test_one_name_is_read_as_itself(self):
        cli = FakeCLI({"ONLY_ONE": "the-value"})
        with mock.patch.object(provision.subprocess, "run", cli):
            found = provision.fleet_secrets_get_many(["ONLY_ONE"], "fleet-ops", "/")
        self.assertEqual(found, {"ONLY_ONE": "the-value"})

    def test_several_names_are_read_as_a_json_object(self):
        cli = FakeCLI({"A": "one", "B": "two"})
        with mock.patch.object(provision.subprocess, "run", cli):
            found = provision.fleet_secrets_get_many(["A", "B"], "fleet-ops", "/")
        self.assertEqual(found, {"A": "one", "B": "two"})

    def test_a_retry_that_leaves_one_name_still_reads(self):
        # One name of the batch is absent:  the CLI refuses the whole batch,
        # the absent name is dropped, and the retry is a one-name batch that
        # answers with the raw value.
        cli = FakeCLI({"PRESENT": "the-value"}, absent=("GONE",))
        with mock.patch.object(provision.subprocess, "run", cli):
            found = provision.fleet_secrets_get_many(["PRESENT", "GONE"], "fleet-ops", "/")
        self.assertEqual(found, {"PRESENT": "the-value"})
        self.assertEqual(len(cli.calls), 2)


class BatchValues(ServiceTokenTestCase):
    """The environment first, one batched read second, an absence is an error."""

    def test_the_environment_comes_first(self):
        cli = FakeCLI({"A": "from-infisical"})
        with mock.patch.object(provision.subprocess, "run", cli):
            with mock.patch.dict(os.environ, {"A": "from-the-environment"}):
                values = provision.batch_values(["A"], "fleet-ops", "/")
        self.assertEqual(values, {"A": "from-the-environment"})
        self.assertEqual(cli.calls, [])

    def test_an_absent_name_is_an_error(self):
        cli = FakeCLI({}, absent=("MISSING",))
        with mock.patch.object(provision.subprocess, "run", cli):
            with self.assertRaises(provision.ReadError) as caught:
                provision.batch_values(["MISSING"], "fleet-ops", "/")
        self.assertIn("MISSING is absent from fleet-ops/", caught.exception.reason)

    def test_three_names_are_one_export(self):
        # Fleet-OPS reads use this caller's distinct service token.
        cli = FakeCLI(
            {
                "CLOUDFLARE_FLEET_API_TOKEN": "the-token",
            }
        )
        with mock.patch.object(provision.subprocess, "run", cli):
            values = provision.batch_values(
                ["CLOUDFLARE_FLEET_API_TOKEN"],
                "fleet-ops",
                "/",
            )
        self.assertEqual(len(cli.calls), 1)
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
            "expiresAt": None,
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
            "expiresAt": None,
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
