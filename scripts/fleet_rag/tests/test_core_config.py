"""Unit tests for fleet_rag.core credential loading and its environment overrides.

    cd scripts && python3 -m unittest fleet_rag.tests.test_core_config -v

The point of these tests is portability: somebody who is not the owner of this repo must be
able to run the recall CLI with five environment variables and nothing else -- no Infisical
account, no handoff file, no edit to core.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fleet_knobs.tests._fake import ScopedReadAPI
from fleet_rag import core

# The five variables a third party sets to run their own corpus.
BYO = {
    "QDRANT_URL": "https://qdrant.example.invalid",
    "QDRANT_API_KEY": "byo-write-key",
    "TEI_URL": "https://tei.example.invalid",
    "TEI_API_KEY": "byo-tei-key",
    "QDRANT_FLEET_COLLECTION": "my-agents",
}
SERVICE_TOKEN = "st." + "synthetic-recall-reader-token-value"


class ExplodingHttp:
    """Stands in for core.http_json and fails the test if anything reaches the network."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url, body=None, headers=None, method=None, timeout=None, retries=None):
        self.calls.append(url)
        raise AssertionError(f"unexpected network call to {url}")


class CleanEnvironmentTests(unittest.TestCase):
    """A machine with only the five variables set: no Infisical, no handoff file."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A handoff path that deliberately does not exist.
        self.absent = pathlib.Path(self.tmp.name) / "no-such-handoff"
        self.http = ExplodingHttp()
        patcher = mock.patch.object(core, "http_json", self.http)
        patcher.start()
        self.addCleanup(patcher.stop)
        no_bin = mock.patch.object(core, "_fleet_secrets_bin", lambda: None)
        no_bin.start()
        self.addCleanup(no_bin.stop)

    def env(self, **extra: str) -> dict[str, str]:
        return {**BYO, "FLEET_RAG_HANDOFF_FILE": str(self.absent), **extra}

    def test_five_env_vars_are_enough_for_reads(self) -> None:
        with mock.patch.dict(os.environ, self.env(), clear=True):
            cfg = core.load_config()
        for key, value in BYO.items():
            self.assertEqual(cfg[key], value)
        self.assertEqual(self.http.calls, [], "load_config must not call Infisical")

    def test_five_env_vars_are_enough_for_writes(self) -> None:
        with mock.patch.dict(os.environ, self.env(), clear=True):
            cfg = core.load_config(need_write=True)
        self.assertEqual(cfg["QDRANT_API_KEY"], BYO["QDRANT_API_KEY"])
        self.assertEqual(self.http.calls, [])

    def test_complete_environment_ignores_unrelated_generic_service_token(self) -> None:
        with (mock.patch.dict(os.environ, self.env(INFISICAL_TOKEN="st." + "x" * 32), clear=True),
              mock.patch.object(core, "fleet_secrets_many", side_effect=AssertionError("unexpected fetch"))):
            cfg = core.load_config()
        self.assertEqual({key: cfg[key] for key in BYO}, BYO)
        self.assertEqual(self.http.calls, [])

    def test_optional_keys_are_absent_not_fatal(self) -> None:
        with mock.patch.dict(os.environ, self.env(), clear=True):
            cfg = core.load_config()
        for key in core.OPTIONAL_KEYS:
            self.assertNotIn(key, cfg)
        self.assertFalse(core.rerank_configured(cfg))
        self.assertEqual(self.http.calls, [])

    def test_extra_key_from_environment(self) -> None:
        with mock.patch.dict(os.environ, self.env(SOMETHING_ELSE="x"), clear=True):
            cfg = core.load_config(extra=("SOMETHING_ELSE",))
        self.assertEqual(cfg["SOMETHING_ELSE"], "x")
        self.assertEqual(self.http.calls, [])

    def test_missing_required_key_names_it_without_values(self) -> None:
        env = self.env()
        env.pop("TEI_URL")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(core.FleetRagError) as ctx:
                core.load_config()
        message = str(ctx.exception)
        self.assertIn("TEI_URL", message)
        self.assertNotIn(BYO["TEI_API_KEY"], message)
        self.assertEqual(self.http.calls, [])

    def test_no_identity_without_a_handoff_file(self) -> None:
        with mock.patch.dict(os.environ, self.env(), clear=True):
            self.assertEqual(core._identity("INFISICAL_SHARED"), (None, None))
            self.assertIsNone(core.infisical_login())
        self.assertEqual(self.http.calls, [])


FAKE_FLEET_SECRETS = """#!/bin/sh
# Stands in for `fleet-secrets run -- python -c PICK names...`: runs the child with a fake env.
[ "$1" = run ] || exit 2
shift; [ "$1" = -- ] && shift
TEI_URL=https://tei.example.invalid TEI_API_KEY=fake-tei QDRANT_URL=https://q.example.invalid \\
QDRANT_FLEET_COLLECTION=fake-coll exec "$@"
"""


class FleetSecretsFallbackTests(unittest.TestCase):
    """The Mac path:  env first, then the `fleet-secrets` command, never the flat file."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = pathlib.Path(self.tmp.name) / "fleet-secrets"
        self.bin.write_text(FAKE_FLEET_SECRETS)
        self.bin.chmod(0o755)

    def test_many_returns_only_present_names(self) -> None:
        with mock.patch.object(core, "_fleet_secrets_bin", lambda: str(self.bin)):
            got = core.fleet_secrets_many(["TEI_URL", "NOT_THERE"])
        self.assertEqual(got, {"TEI_URL": "https://tei.example.invalid"})

    def test_identity_falls_back_to_fleet_secrets_get(self) -> None:
        self.bin.write_text(
            "#!/bin/sh\n"
            '[ "$1" = get ] || exit 2\n'
            'case "$2" in\n'
            "  INFISICAL_SHARED_CLIENT_ID) printf cid-fs ;;\n"
            "  INFISICAL_SHARED_CLIENT_SECRET) printf placeholder-fs ;;\n"
            "  *) exit 3 ;;\n"
            "esac\n"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(core, "_fleet_secrets_bin", lambda: str(self.bin)):
                self.assertEqual(core._identity("INFISICAL_SHARED"), (None, None))
                self.assertEqual(core._identity("INFISICAL_OTHER"), (None, None))

    def test_identity_environment_beats_fleet_secrets(self) -> None:
        env = {"INFISICAL_SHARED_CLIENT_ID": "cid-env", "INFISICAL_SHARED_CLIENT_SECRET": "sec-env"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(core, "_fleet_secrets_bin", lambda: "/nonexistent/fleet-secrets"),
        ):
            self.assertEqual(core._identity("INFISICAL_SHARED"), (None, None))

    def test_missing_binary_is_just_absent(self) -> None:
        with mock.patch.object(core, "_fleet_secrets_bin", lambda: None):
            self.assertEqual(core.fleet_secrets_many(["TEI_URL"]), {})
            self.assertIsNone(core.fleet_secret("TEI_URL"))

    def test_failing_binary_is_just_absent(self) -> None:
        self.bin.write_text("#!/bin/sh\nexit 4\n")
        with mock.patch.object(core, "_fleet_secrets_bin", lambda: str(self.bin)):
            self.assertEqual(core.fleet_secrets_many(["TEI_URL"]), {})

    def test_missing_config_does_not_borrow_a_foreign_project_loader(self) -> None:
        with (mock.patch.dict(os.environ, {"QDRANT_API_KEY": "k"}, clear=True),
              mock.patch.object(core, "fleet_secrets_many", side_effect=AssertionError("foreign fallback"))):
            with self.assertRaises(core.FleetRagError):
                core.load_config()


    def test_identity_comes_from_the_environment(self) -> None:
        env = {"INFISICAL_SHARED_CLIENT_ID": "cid-env", "INFISICAL_SHARED_CLIENT_SECRET": "sec-env"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(core._identity("INFISICAL_SHARED"), (None, None))


class OverrideTests(unittest.TestCase):
    """FLEET_RAG_* overrides, with the owner's values as the fallback."""

    def test_defaults_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(core.infisical_api(), core.INFISICAL_API)
            self.assertEqual(core.infisical_project(), core.SHARED_PROJECT)
            self.assertEqual(core.infisical_env(), core.SHARED_ENV)
            self.assertIsNone(core.handoff_file())

    def test_environment_overrides(self) -> None:
        env = {
            "FLEET_RAG_INFISICAL_API": "https://infisical.example.invalid/api/",
            "FLEET_RAG_INFISICAL_PROJECT": "proj-1234",
            "FLEET_RAG_INFISICAL_ENV": "prod",
            "FLEET_RAG_HANDOFF_FILE": "~/elsewhere/creds",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(core.infisical_api(), "https://infisical.example.invalid/api")
            self.assertEqual(core.infisical_project(), "proj-1234")
            self.assertEqual(core.infisical_env(), "prod")
            self.assertEqual(
                core.handoff_file(), pathlib.Path(os.path.expanduser("~/elsewhere/creds"))
            )

    def test_non_prod_env_is_refused(self) -> None:
        for slug in ("dev", "staging", "DEV", "production"):
            with mock.patch.dict(os.environ, {"FLEET_RAG_INFISICAL_ENV": slug}, clear=True):
                with self.assertRaises(core.FleetRagError, msg=slug):
                    core.infisical_env()

    def test_empty_override_falls_back_to_default(self) -> None:
        env = {
            "FLEET_RAG_INFISICAL_PROJECT": "",
            "FLEET_RAG_INFISICAL_ENV": "",
            "FLEET_RAG_INFISICAL_API": "",
            "FLEET_RAG_HANDOFF_FILE": "",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(core.infisical_project(), core.SHARED_PROJECT)
            self.assertEqual(core.infisical_env(), core.SHARED_ENV)
            self.assertEqual(core.infisical_api(), core.INFISICAL_API)
            self.assertIsNone(core.handoff_file())

    def test_identity_read_from_overridden_handoff_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "creds"
            path.write_text(
                'INFISICAL_SHARED_CLIENT_ID="cid-1"\n'
                "INFISICAL_SHARED_CLIENT_SECRET='placeholder-1'\n"
            )
            with mock.patch.dict(os.environ, {"FLEET_RAG_HANDOFF_FILE": str(path)}, clear=True):
                self.assertEqual(core._identity("INFISICAL_SHARED"), (None, None))

    def test_login_and_fetch_use_the_overridden_infisical(self) -> None:
        seen: list[str] = []

        from fleet_secrets import core as secrets

        def fake_api(method, path, token, body, env, **kwargs):
            seen.append(path)
            if path == "/v2/service-token":
                return 200, {"workspace": "proj-1234", "permissions": ["read"],
                             "scopes": [{"environment": "prod", "secretPath": "/"}], "expiresAt": None}
            name = path.split("?", 1)[0].rsplit("/", 1)[-1]
            if name != "TEI_RERANK_URL":
                return 404, {}
            record = {"secretKey": "TEI_RERANK_URL", "secretValue": "https://rr",
                      "type": "shared", "workspace": "proj-1234",
                      "environment": "prod", "secretPath": "/"}
            return 200, {"secret": record}

        env = {
            **BYO,
            "FLEET_RECALL_INFISICAL_TOKEN": SERVICE_TOKEN,
            "FLEET_RAG_INFISICAL_API": "https://infisical.example.invalid/api",
            "FLEET_RAG_INFISICAL_PROJECT": "proj-1234",
            "FLEET_RAG_INFISICAL_ENV": "prod",
        }
        with (mock.patch.dict(os.environ, env, clear=True),
              mock.patch.object(secrets, "_api_json", fake_api)):
            cfg = core.load_config()
        self.assertEqual(cfg["TEI_RERANK_URL"], "https://rr")
        self.assertEqual(seen[0], "/v2/service-token")
        self.assertIn("/v3/secrets/raw", seen[1])
        self.assertIn("workspaceId=proj-1234", seen[1])
        self.assertIn("environment=prod", seen[1])
        # The owner's project id is never used once an override is set.
        self.assertNotIn(core.SHARED_PROJECT, seen[1])


@unittest.skipUnless(shutil.which("rsync"), "standalone installer requires rsync")
class StandaloneScopedReaderTests(unittest.TestCase):
    def test_installed_recall_verifies_real_scoped_reads_and_replaces_only_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            home.mkdir()
            stable = home / "apps/fleet-rag"
            installer = pathlib.Path(__file__).resolve().parents[2] / "install-fleet-rag.sh"
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "FLEET_RAG_HOME": str(stable)}
            installed = subprocess.run(["bash", str(installer)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            package = stable / "fleet_secrets"
            self.assertEqual(sorted(p.name for p in package.iterdir()), ["__init__.py", "core.py"])
            self.assertFalse((stable / "fleet-secrets").exists())
            (package / "cli.py").write_text("# stale code must not ship\n")
            (stable / "state/keep").write_text("preserved")
            again = subprocess.run(["bash", str(installer)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertFalse((package / "cli.py").exists())
            self.assertEqual((stable / "state/keep").read_text(), "preserved")
            boot = (
                "import json,os,sys;sys.path.insert(0,sys.argv[1]);"
                "from fleet_rag import core;import fleet_secrets.core as verifier;"
                "assert os.path.commonpath([sys.argv[1],verifier.__file__])==sys.argv[1];"
                "print(json.dumps(core.infisical_fetch({'RECALL_API_URL'})))"
            )
            with ScopedReadAPI({"RECALL_API_URL": "https://recall.invalid"}, project=core.SHARED_PROJECT) as api:
                child = {**env, "FLEET_RECALL_INFISICAL_TOKEN": api.token,
                         "FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"],
                         "FLEET_RAG_INFISICAL_API": api.env["FLEET_SECRETS_API"]}
                ran = subprocess.run([sys.executable, "-I", "-B", "-c", boot, str(stable)],
                                     env=child, cwd=home, capture_output=True, text=True, timeout=15)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertEqual(json.loads(ran.stdout), {"RECALL_API_URL": "https://recall.invalid"})
                self.assertEqual(len(api.requests), 2)
                self.assertEqual(api.requests[0], "/api/v2/service-token")
                self.assertTrue(api.requests[1].startswith("/api/v3/secrets/raw/RECALL_API_URL?"))
                api.requests.clear()
                api.metadata_override["permissions"] = ["read", "write"]
                denied = subprocess.run([sys.executable, "-I", "-B", "-c", boot, str(stable)],
                                        env=child, cwd=home, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(api.requests, ["/api/v2/service-token"])


class ReadTokenFileTests(unittest.TestCase):
    """FLEET_RECALL_INFISICAL_TOKEN can come from a mode-600 file of the caller's own (AFC-677)."""

    TOKEN = "st." + "a" * 36 + "." + "b" * 32 + "." + "c" * 32

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        (self.home / ".secrets").mkdir()
        self.default = self.home / ".secrets" / "fleet-recall-infisical.env"

    def write(self, path: pathlib.Path, text: str, mode: int = 0o600) -> pathlib.Path:
        path.write_text(text)
        path.chmod(mode)
        return path

    def binding(self, **env: str) -> str | None:
        with mock.patch.dict(os.environ, {"HOME": str(self.home), **env}, clear=True):
            return core.read_token_binding()

    def test_default_file_supplies_the_token(self) -> None:
        self.write(self.default, f"# recall reader\nexport FLEET_RECALL_INFISICAL_TOKEN='{self.TOKEN}'\n")
        self.assertEqual(self.binding(), self.TOKEN)

    def test_environment_variable_wins_and_no_file_is_none(self) -> None:
        self.assertIsNone(self.binding())
        self.write(self.default, "FLEET_RECALL_INFISICAL_TOKEN=st.fromfile0000000000000\n")
        self.assertEqual(self.binding(FLEET_RECALL_INFISICAL_TOKEN=self.TOKEN), self.TOKEN)

    def test_no_home_means_no_default_file(self) -> None:
        self.write(self.default, f"FLEET_RECALL_INFISICAL_TOKEN={self.TOKEN}\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(core.read_token_binding())

    def test_named_file_must_exist(self) -> None:
        with self.assertRaises(core.FleetRagError):
            self.binding(FLEET_RECALL_INFISICAL_TOKEN_FILE=str(self.home / "missing.env"))
        named = self.write(self.home / "other.env", f"FLEET_RECALL_INFISICAL_TOKEN={self.TOKEN}\n")
        self.assertEqual(self.binding(FLEET_RECALL_INFISICAL_TOKEN_FILE=str(named)), self.TOKEN)

    def test_open_mode_symlink_and_missing_line_are_refused_without_the_value(self) -> None:
        self.write(self.default, f"FLEET_RECALL_INFISICAL_TOKEN={self.TOKEN}\n", mode=0o644)
        with self.assertRaises(core.FleetRagError) as ctx:
            self.binding()
        self.assertNotIn(self.TOKEN, str(ctx.exception))
        self.default.unlink()
        real = self.write(self.home / "real.env", f"FLEET_RECALL_INFISICAL_TOKEN={self.TOKEN}\n")
        self.default.symlink_to(real)
        with self.assertRaises(core.FleetRagError):
            self.binding()
        self.default.unlink()
        self.write(self.default, "OTHER=1\n")
        with self.assertRaises(core.FleetRagError):
            self.binding()

    def test_infisical_login_uses_the_file(self) -> None:
        self.write(self.default, f"FLEET_RECALL_INFISICAL_TOKEN={self.TOKEN}\n")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "INFISICAL_TOKEN": "st." + "z" * 40}, clear=True):
            self.assertEqual(core.infisical_login(), self.TOKEN)


if __name__ == "__main__":
    unittest.main()
