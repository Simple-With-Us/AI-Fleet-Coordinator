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


class ReaderTestCase(unittest.TestCase):
    """Each reader check uses a cleared environment and synthetic loopback API."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"HOME": self.temp.name, "PATH": os.defpath}
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def use_api(self, api, *, own_binding, project_id=None, other_bindings=True):
        api.record_override.setdefault("secretValueHidden", False)
        env = {"FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"], own_binding: api.token}
        if project_id:
            env["INFISICAL_FLEET_OPS_PROJECT_ID"] = project_id
        if other_bindings:
            env.update(
                {
                    provision.READ_TOKEN_AFC: fixture_service_token(),
                    provision.READ_TOKEN_FLEETOPS: fixture_service_token(),
                    provision.WRITE_TOKEN_AFC: fixture_service_token(),
                    "INFISICAL_TOKEN": fixture_service_token(),
                    "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": fixture_service_token(),
                }
            )
            env[own_binding] = api.token
        os.environ.update(env)


class BoundedReader(ReaderTestCase):
    def test_empty_names_remain_offline_without_an_own_binding(self):
        with ScopedReadAPI({}, project=str(uuid.uuid4())) as api:
            os.environ["FLEET_SECRETS_API"] = api.env["FLEET_SECRETS_API"]
            for names in ([], iter(())):
                self.assertEqual(
                    provision.fleet_secrets_get_many(names, provision.FLEETOPS_SLUG, "/"), {}
                )
            self.assertEqual(api.requests, [])

    def test_one_and_multiple_names_use_the_real_bounded_reader(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI(
            {"ONLY_ONE": "one-value", "A": "alpha", "B": "beta"}, project=project
        ) as api:
            self.use_api(
                api,
                own_binding=provision.READ_TOKEN_FLEETOPS,
                project_id=project,
            )
            with mock.patch.object(
                provision.scoped_secrets,
                "bounded_fetch_project",
                wraps=provision.scoped_secrets.bounded_fetch_project,
            ) as fetch:
                self.assertEqual(
                    provision.fleet_secrets_get("ONLY_ONE", provision.FLEETOPS_SLUG, "/"),
                    "one-value",
                )
                self.assertEqual(
                    provision.fleet_secrets_get_many(["A", "B"], provision.FLEETOPS_SLUG, "/"),
                    {"A": "alpha", "B": "beta"},
                )
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(api.requests.count("/api/v2/service-token"), 2)
            self.assertEqual(len([path for path in api.requests if "/secrets/raw/" in path]), 3)
            for call in fetch.call_args_list:
                self.assertEqual(call.args[:2], (project, "/"))
                self.assertEqual(
                    call.args[2],
                    {
                        "FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"],
                        "INFISICAL_READ_PATHS": "/",
                    },
                )
                self.assertEqual(call.kwargs["token"], api.token)
                self.assertEqual(call.kwargs["timeout"], 90.0)
                self.assertTrue(call.kwargs["strict_records"])
                self.assertEqual(call.kwargs["max_output_bytes"], 1024 * 1024)

    def test_missing_names_are_omitted_and_required_batch_values_fail(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI({"PRESENT": "the-value"}, project=project) as api:
            self.use_api(api, own_binding=provision.READ_TOKEN_FLEETOPS, project_id=project)
            self.assertEqual(
                provision.fleet_secrets_get_many(["PRESENT", "GONE"], provision.FLEETOPS_SLUG, "/"),
                {"PRESENT": "the-value"},
            )
            self.assertIsNone(provision.fleet_secrets_get("GONE", provision.FLEETOPS_SLUG, "/"))
            with self.assertRaises(provision.ReadError) as caught:
                provision.batch_values(["PRESENT", "GONE"], provision.FLEETOPS_SLUG, "/")
            self.assertIn("GONE is absent from fleet-ops/", caught.exception.reason)

    def test_environment_precedence_avoids_a_provider_read(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI({"A": "from-provider"}, project=project) as api:
            self.use_api(api, own_binding=provision.READ_TOKEN_FLEETOPS, project_id=project)
            os.environ["A"] = "from-environment"
            self.assertEqual(
                provision.batch_values(["A"], provision.FLEETOPS_SLUG, "/"),
                {"A": "from-environment"},
            )
            self.assertEqual(api.requests, [])

    def test_afc_and_fleetops_use_only_their_own_project_and_folder(self):
        afc_values = {"AFC_VALUE": "from-afc"}
        with ScopedReadAPI(
            afc_values, project=provision.AFC, path=provision.SECRET_PATH
        ) as afc_api:
            self.use_api(afc_api, own_binding=provision.READ_TOKEN_AFC)
            self.assertEqual(
                provision.fleet_secrets_get("AFC_VALUE", provision.AFC_SLUG, provision.SECRET_PATH),
                "from-afc",
            )
            self.assertEqual(afc_api.requests.count("/api/v2/service-token"), 1)

        fleetops_project = str(uuid.uuid4())
        with ScopedReadAPI(
            {"FLEETOPS_VALUE": "from-fleetops"}, project=fleetops_project
        ) as fleetops_api:
            self.use_api(
                fleetops_api,
                own_binding=provision.READ_TOKEN_FLEETOPS,
                project_id=fleetops_project,
            )
            self.assertEqual(
                provision.fleet_secrets_get("FLEETOPS_VALUE", provision.FLEETOPS_SLUG, "/"),
                "from-fleetops",
            )

        with self.assertRaises(provision.ReadError):
            provision.fleet_secrets_get("AFC_VALUE", provision.AFC_SLUG, "/")
        self.assertEqual(len(afc_api.requests), 2)

    def test_other_project_or_folder_is_refused_before_any_read(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI({"A": "value"}, project=project) as api:
            self.use_api(api, own_binding=provision.READ_TOKEN_FLEETOPS, project_id=project)
            for invalid_project, invalid_path in (
                ("unknown", "/"),
                (provision.FLEETOPS_SLUG, "/other"),
            ):
                with self.assertRaises(provision.ReadError):
                    provision.fleet_secrets_get("A", invalid_project, invalid_path)
            self.assertEqual(api.requests, [])

    def test_missing_own_token_does_not_borrow_generic_or_other_bindings(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI({"A": "value"}, project=project) as api:
            os.environ.update(
                {
                    "FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"],
                    "HOME": self.temp.name,
                    provision.READ_TOKEN_AFC: fixture_service_token(),
                    provision.WRITE_TOKEN_AFC: fixture_service_token(),
                    "INFISICAL_TOKEN": api.token,
                    "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": api.token,
                    "INFISICAL_FLEET_OPS_PROJECT_ID": project,
                }
            )
            with self.assertRaises(provision.ReadError) as caught:
                provision.fleet_secrets_get("A", provision.FLEETOPS_SLUG, "/")
            self.assertEqual(
                caught.exception.reason, f"{provision.READ_TOKEN_FLEETOPS} is missing or invalid"
            )
            os.environ[provision.READ_TOKEN_FLEETOPS] = ""
            with self.assertRaises(provision.ReadError):
                provision.fleet_secrets_get("A", provision.FLEETOPS_SLUG, "/")
            self.assertEqual(api.requests, [])

    def test_invalid_metadata_and_strict_records_fail_without_values(self):
        project = str(uuid.uuid4())
        failures = (
            {"metadata_override": {"workspace": str(uuid.uuid4())}},
            {"metadata_override": {"scopes": [{"environment": "prod", "secretPath": "/wrong"}]}},
            {"metadata_override": {"scopes": [{"environment": "dev", "secretPath": "/"}]}},
            {"metadata_override": {"permissions": ["read", "write"]}},
            {"metadata_override": {"expiresAt": None}},
            {"metadata_override": {"expiresAt": "2000-01-01T00:00:00Z"}},
            {"record_override": {"secretValueHidden": True}},
            {"record_override": {"secretValueHidden": "false"}},
            {"record_override": {"secretPath": "/wrong"}},
            {"record_override": {"secretKey": "OTHER"}},
            {"record_override": {"secretValue": ""}},
            {"record_omissions": ("workspace",)},
            {"record_omissions": ("secretValueHidden",)},
        )
        for options in failures:
            with self.subTest(options=options):
                with ScopedReadAPI({"A": "sentinel-value"}, project=project) as api:
                    api.metadata_override.update(options.get("metadata_override", {}))
                    api.record_override.update(options.get("record_override", {}))
                    api.record_omissions = options.get("record_omissions", ())
                    self.use_api(api, own_binding=provision.READ_TOKEN_FLEETOPS, project_id=project)
                    with self.assertRaises(provision.ReadError) as caught:
                        provision.fleet_secrets_get("A", provision.FLEETOPS_SLUG, "/")
                    self.assertNotIn("sentinel-value", caught.exception.reason)
                    self.assertEqual(len(api.requests), 1 if "metadata_override" in options else 2)
                    self.assertEqual(api.requests[0], "/api/v2/service-token")

    def test_timeout_and_provider_failures_are_fixed_safe_errors(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI({"A": "sentinel-value"}, project=project) as api:
            self.use_api(api, own_binding=provision.READ_TOKEN_FLEETOPS, project_id=project)
            with (
                mock.patch.object(
                    provision.scoped_secrets,
                    "bounded_fetch_project",
                    side_effect=SecretsError("bounded-read-timeout", failure_kind="unavailable"),
                ),
                self.assertRaises(provision.ReadError) as caught,
            ):
                provision.fleet_secrets_get("A", provision.FLEETOPS_SLUG, "/")
            self.assertEqual(caught.exception.code, provision.EXIT_TIMEOUT)
            self.assertNotIn("sentinel-value", caught.exception.reason)

        with ScopedReadAPI({"A": "sentinel-value"}, project=project) as api:
            self.use_api(api, own_binding=provision.READ_TOKEN_FLEETOPS, project_id=project)
            api.status = 503
            with self.assertRaises(provision.ReadError) as caught:
                provision.fleet_secrets_get("A", provision.FLEETOPS_SLUG, "/")
            self.assertNotIn("sentinel-value", caught.exception.reason)
            self.assertLessEqual(len(caught.exception.reason), 96)


class IsolatedPayload(unittest.TestCase):
    """The public payload imports and reads without an installed app package."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.source_root = pathlib.Path(__file__).resolve().parent
        self.payload = self.root / "payload"
        self.app_sentinel = self.root / "external-app-invoked"
        app = self.root / "home" / ".local" / "bin" / "fleet-secrets"
        app.parent.mkdir(parents=True)
        app.write_text(
            f"#!{sys.executable}\n"
            "import pathlib\n"
            f"pathlib.Path({str(self.app_sentinel)!r}).write_text('called')\n"
            "raise SystemExit(97)\n"
        )
        app.chmod(0o700)
        (self.payload / "scripts" / "alert-ingress").mkdir(parents=True)
        shutil.copy2(
            self.source_root / "fleet_infisical.py", self.payload / "scripts" / "fleet_infisical.py"
        )
        shutil.copy2(
            self.source_root / "alert-ingress" / "provision-secrets.py",
            self.payload / "scripts" / "alert-ingress" / "provision-secrets.py",
        )

    def run_payload(self, script, env):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(script)],
            cwd=self.root,
            env={"HOME": str(self.root / "home"), "PATH": "", **env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

    def test_isolated_payload_reads_with_only_adjacent_canonical_library(self):
        project = str(uuid.uuid4())
        with ScopedReadAPI({"ISOLATED_VALUE": "isolated-value"}, project=project) as api:
            env = {
                "HOME": str(self.root / "home"),
                "PATH": "",
                "FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"],
                "INFISICAL_FLEET_OPS_PROJECT_ID": project,
                provision.READ_TOKEN_FLEETOPS: api.token,
            }
            child = self.root / "read.py"
            child.write_text(
                "import importlib.util, json, pathlib, sys\n"
                f"p=pathlib.Path({str(self.payload / 'scripts' / 'alert-ingress' / 'provision-secrets.py')!r})\n"
                "s=importlib.util.spec_from_file_location('isolated_provision', p)\n"
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "assert 'fleet_secrets' not in sys.modules\n"
                "assert m.fleet_secrets_get('ISOLATED_VALUE', m.FLEETOPS_SLUG, '/') == 'isolated-value'\n"
                "print('ok')\n"
            )
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(child)],
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"ok\n", b""))
            self.assertFalse(self.app_sentinel.exists())
            self.assertEqual(
                {
                    p.relative_to(self.payload).as_posix()
                    for p in self.payload.rglob("*")
                    if p.is_file()
                },
                {"scripts/fleet_infisical.py", "scripts/alert-ingress/provision-secrets.py"},
            )

    def test_missing_or_broken_canonical_payload_fails_with_fixed_127(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                scripts = self.payload / "scripts"
                canonical = scripts / "fleet_infisical.py"
                original = canonical.read_bytes()
                try:
                    canonical.unlink()
                    if broken:
                        canonical.write_text("raise RuntimeError('private sentinel')\n")
                    proc = self.run_payload(
                        self.payload / "scripts" / "alert-ingress" / "provision-secrets.py",
                        {"HOME": str(self.root / "home"), "PATH": ""},
                    )
                    self.assertEqual(proc.returncode, 127)
                    self.assertEqual(proc.stdout, b"")
                    self.assertNotIn(b"private sentinel", proc.stderr)
                    self.assertNotIn(b"Traceback", proc.stderr)
                    self.assertIn(b"shared Infisical reader is unavailable", proc.stderr)
                    self.assertFalse(self.app_sentinel.exists())
                finally:
                    canonical.write_bytes(original)


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
