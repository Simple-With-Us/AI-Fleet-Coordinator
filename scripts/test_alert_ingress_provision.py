import contextlib
import importlib.util
import io
import os
import sys
import unittest
import uuid
from unittest import mock

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
        self.calls.append((project, path, tuple(names or ()), dict(kwargs)))
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
            lambda env, home, project="fleet-ops": str(uuid.uuid4()),
        )
        project.start()
        self.addCleanup(project.stop)
        # The provisioner reads its token from its own store,  and the store is the
        # only source it may use:  the environment's copy never stands in for it.  The
        # store is stubbed here so a test never reads the host's own credential store.
        store = mock.patch.object(
            provision.scoped_secrets,
            "token_from_store",
            return_value=("st." + "fixture-store-read-token-value", "ok"),
        )
        store.start()
        self.addCleanup(store.stop)

    def read(self, answers, absent=()):
        """Patch the scoped read and return the fake."""
        fake = FakeRead(answers, absent)
        patcher = mock.patch.object(provision.scoped_secrets, "bounded_fetch_project", fake)
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


class StoreFirst(ServiceTokenTestCase):
    """The store is the authority for the token;  the environment only carries a copy."""

    def test_the_store_token_is_the_one_the_read_runs_under(self):
        seen = []

        def fake_store(account, env=None, home=None):
            seen.append(account)
            return ("st." + "fixture-store-read-token-value", "ok")

        read = self.read({"A": "one"})
        with mock.patch.object(provision.scoped_secrets, "token_from_store", fake_store):
            found = provision.scoped_values(["A"], "fleet-ops", "/zulip")
        self.assertEqual(found, {"A": "one"})
        self.assertEqual(seen, ["alert-ingress-fleetops-ro"])
        self.assertEqual(len(read.calls), 1)

    def test_an_empty_store_fails_closed_even_when_the_environment_carries_a_copy(self):
        # The owner's ruling on OPS-1101:  the environment's copy is never used in
        # place of the store,  so a store with no token fails closed.
        read = self.read({"A": "one"})
        with (
            mock.patch.object(
                provision.scoped_secrets, "token_from_store", return_value=("", "no-token")
            ),
            self.assertRaises(provision.ReadError) as ctx,
        ):
            provision.scoped_values(["A"], "fleet-ops", "/zulip")
        self.assertIn("alert-ingress-fleetops-ro", str(ctx.exception))
        self.assertEqual(read.calls, [])

    def test_a_malformed_store_token_fails_closed_and_names_the_account(self):
        # A store token that is not a service token is a refusal,  not a value:  the
        # error names the account it came from,  and no read starts.
        read = self.read({"A": "one"})
        with (
            mock.patch.object(
                provision.scoped_secrets,
                "token_from_store",
                return_value=("not-a-service-token", "ok"),
            ),
            self.assertRaises(provision.ReadError) as ctx,
        ):
            provision.scoped_values(["A"], "fleet-ops", "/zulip")
        self.assertIn("alert-ingress-fleetops-ro", str(ctx.exception))
        self.assertEqual(read.calls, [])

    def test_an_unsupported_project_is_refused_before_a_read(self):
        read = self.read({"A": "one"})
        with self.assertRaises(provision.ReadError) as ctx:
            provision.scoped_values(["A"], "not-a-project", "/zulip")
        self.assertIn("unsupported Infisical project", str(ctx.exception))
        self.assertEqual(read.calls, [])


class ScopedReadContract(ServiceTokenTestCase):
    """The read keeps the contract the replaced reader enforced.

    The strict read refuses a row that cannot be tied to the folder it came from,  and
    a hung read keeps the timeout exit code the old subprocess contract promised.
    """

    def test_the_read_asks_for_strict_records(self):
        # The strict read, with the bounds the contract carries:  a deadline,  a cap on the
        # answer,  and no writer.
        read = self.read({"ONLY_ONE": "the-value"})
        provision.scoped_values(["ONLY_ONE"], "fleet-ops", "/")
        asked = read.calls[0][3]
        self.assertIs(asked["strict_records"], True)
        self.assertIs(asked["writer"], False)
        self.assertGreater(asked["timeout"], 0)
        self.assertGreater(asked["max_output_bytes"], 0)

    def test_a_row_without_its_provenance_is_refused(self):
        # The strict read refuses a row that cannot be tied to the folder it came from:
        # the client raises the refusal,  and the provisioner names it.  The client's own
        # suite proves the refusal against the real worker
        # (fleet_infisical_cli_tests.test_bounded_reads).
        def without_provenance(project, path, env, *, token=None, names=None, **kwargs):
            raise provision.scoped_secrets.SecretsError(
                "unverified-source:  no secret row for ONLY_ONE at /", 4
            )

        with (
            mock.patch.object(
                provision.scoped_secrets, "bounded_fetch_project", without_provenance
            ),
            self.assertRaises(provision.ReadError) as caught,
        ):
            provision.scoped_values(["ONLY_ONE"], "fleet-ops", "/")
        self.assertIn("unverified-source", caught.exception.reason)
        self.assertEqual(caught.exception.code, 1)

    def test_a_hung_read_exits_with_the_timeout_code(self):
        # `SecretsTimeout` is both a `SecretsError` and a `TimeoutError`,  so the
        # timeout has to be caught first to keep exit code 124.
        self.assertTrue(issubclass(provision.scoped_secrets.SecretsTimeout, TimeoutError))

        def hung(project, path, env, *, token=None, names=None, **kwargs):
            raise provision.scoped_secrets.SecretsTimeout()

        with mock.patch.object(provision.scoped_secrets, "bounded_fetch_project", hung):
            with self.assertRaises(provision.ReadError) as caught:
                provision.scoped_values(["ONLY_ONE"], "fleet-ops", "/")
        self.assertEqual(caught.exception.code, provision.EXIT_TIMEOUT)
        self.assertEqual(caught.exception.code, 124)

    def test_a_broken_credential_exits_with_one(self):
        def broken(project, path, env, *, token=None, names=None, **kwargs):
            raise provision.scoped_secrets.SecretsError("token-expired-or-invalid", 3)

        with mock.patch.object(provision.scoped_secrets, "bounded_fetch_project", broken):
            with self.assertRaises(provision.ReadError) as caught:
                provision.scoped_values(["ONLY_ONE"], "fleet-ops", "/")
        self.assertEqual(caught.exception.code, 1)
        self.assertNotEqual(caught.exception.code, provision.EXIT_TIMEOUT)


class BatchValues(ServiceTokenTestCase):
    """One batched scoped read, an absence is an error."""

    def test_the_environment_does_not_replace_the_scoped_read(self):
        # The reviewed source is the scoped read:  a hand-set environment copy never
        # stands in for it,  so the value written into a target is the reviewed one.
        read = self.read({"A": "from-infisical"})
        with mock.patch.dict(os.environ, {"A": "from-the-environment"}):
            values = provision.batch_values(["A"], "fleet-ops", "/")
        self.assertEqual(values, {"A": "from-infisical"})
        self.assertEqual(len(read.calls), 1)

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


class FleetOpsFolder(ServiceTokenTestCase):
    """The Fleet-OPS read names the folder its own grant covers.

    The Fleet-OPS project root is an alias layer:  a name at the root comes back as
    an unexpanded reference,  which the shared client refuses.  The read names the
    folder where the literal lives,  and this pins that,  so a change back to the
    root cannot pass quietly.
    """

    def test_the_fleet_ops_read_names_its_own_folder_and_not_the_project_root(self):
        seen = []

        def fake_scoped_values(names, project, path):
            seen.append((project, path))
            if project == provision.FLEETOPS_SLUG:
                # Stop here:  this test is about the folder the read asked for,  and
                # nothing may be written.
                raise provision.ReadError(
                    "stop before any write", provision.EXIT_MISSING_EXECUTABLE
                )
            return {name: "fixture-value" for name in names}

        with (
            mock.patch.object(provision, "scoped_values", fake_scoped_values),
            mock.patch.object(sys, "argv", ["provision-secrets.py", "--apply"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = provision.main()
        self.assertEqual(code, provision.EXIT_MISSING_EXECUTABLE)
        self.assertEqual(
            seen,
            [
                (provision.AFC_SLUG, provision.SECRET_PATH),
                (provision.FLEETOPS_SLUG, "/global-api-keys"),
            ],
        )
        self.assertNotEqual(seen[-1][1], "/")


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
