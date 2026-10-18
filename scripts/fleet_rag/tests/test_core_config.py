"""Unit tests for fleet_rag.core credential loading and its environment overrides.

    cd scripts && python3 -m unittest fleet_rag.tests.test_core_config -v

The point of these tests is portability: somebody who is not the owner of this repo must be
able to run the recall CLI with five environment variables and nothing else -- no Infisical
account, no handoff file, no edit to core.py.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fleet_rag import core

# The five variables a third party sets to run their own corpus.
BYO = {
    "QDRANT_URL": "https://qdrant.example.invalid",
    "QDRANT_API_KEY": "byo-write-key",
    "TEI_URL": "https://tei.example.invalid",
    "TEI_API_KEY": "byo-tei-key",
    "QDRANT_FLEET_COLLECTION": "my-agents",
}


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
                self.assertEqual(core._identity("INFISICAL_SHARED"), ("cid-fs", "placeholder-fs"))
                self.assertEqual(core._identity("INFISICAL_OTHER"), (None, None))

    def test_identity_environment_beats_fleet_secrets(self) -> None:
        env = {"INFISICAL_SHARED_CLIENT_ID": "cid-env", "INFISICAL_SHARED_CLIENT_SECRET": "sec-env"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            core, "_fleet_secrets_bin", lambda: "/nonexistent/fleet-secrets"
        ):
            self.assertEqual(core._identity("INFISICAL_SHARED"), ("cid-env", "sec-env"))

    def test_missing_binary_is_just_absent(self) -> None:
        with mock.patch.object(core, "_fleet_secrets_bin", lambda: None):
            self.assertEqual(core.fleet_secrets_many(["TEI_URL"]), {})
            self.assertIsNone(core.fleet_secret("TEI_URL"))

    def test_failing_binary_is_just_absent(self) -> None:
        self.bin.write_text("#!/bin/sh\nexit 4\n")
        with mock.patch.object(core, "_fleet_secrets_bin", lambda: str(self.bin)):
            self.assertEqual(core.fleet_secrets_many(["TEI_URL"]), {})

    def test_load_config_fills_gaps_from_fleet_secrets(self) -> None:
        env = {"QDRANT_API_KEY": "k", "TEI_API_KEY": "from-env"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(core, "_fleet_secrets_bin", lambda: str(self.bin)),
            mock.patch.object(core, "http_json", ExplodingHttp()),
        ):
            cfg = core.load_config()
        self.assertEqual(cfg["TEI_URL"], "https://tei.example.invalid")
        self.assertEqual(cfg["TEI_API_KEY"], "from-env", "the environment always wins")

    def test_identity_comes_from_the_environment(self) -> None:
        env = {"INFISICAL_SHARED_CLIENT_ID": "cid-env", "INFISICAL_SHARED_CLIENT_SECRET": "sec-env"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(core._identity("INFISICAL_SHARED"), ("cid-env", "sec-env"))


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
            "FLEET_RAG_INFISICAL_ENV": "staging",
            "FLEET_RAG_HANDOFF_FILE": "~/elsewhere/creds",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(core.infisical_api(), "https://infisical.example.invalid/api")
            self.assertEqual(core.infisical_project(), "proj-1234")
            self.assertEqual(core.infisical_env(), "staging")
            self.assertEqual(
                core.handoff_file(), pathlib.Path(os.path.expanduser("~/elsewhere/creds"))
            )

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
                self.assertEqual(core._identity("INFISICAL_SHARED"), ("cid-1", "placeholder-1"))

    def test_login_and_fetch_use_the_overridden_infisical(self) -> None:
        seen: list[str] = []

        def fake_http(url, body=None, headers=None, method=None, timeout=None, retries=None):
            seen.append(url)
            if "/v1/auth/" in url:
                return {"accessToken": "placeholder-token"}
            return {"secrets": [{"secretKey": "TEI_RERANK_URL", "secretValue": "https://rr"}]}

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "creds"
            path.write_text(
                "INFISICAL_SHARED_CLIENT_ID=cid\nINFISICAL_SHARED_CLIENT_SECRET=placeholder\n"
            )
            env = {
                **BYO,
                "FLEET_RAG_HANDOFF_FILE": str(path),
                "FLEET_RAG_INFISICAL_API": "https://infisical.example.invalid/api",
                "FLEET_RAG_INFISICAL_PROJECT": "proj-1234",
                "FLEET_RAG_INFISICAL_ENV": "staging",
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(core, "http_json", fake_http),
            ):
                cfg = core.load_config()
        self.assertEqual(cfg["TEI_RERANK_URL"], "https://rr")
        self.assertEqual(
            seen[0], "https://infisical.example.invalid/api/v1/auth/universal-auth/login"
        )
        self.assertIn("https://infisical.example.invalid/api/v3/secrets/raw", seen[1])
        self.assertIn("workspaceId=proj-1234", seen[1])
        self.assertIn("environment=staging", seen[1])
        # The owner's project id is never used once an override is set.
        self.assertNotIn(core.SHARED_PROJECT, seen[1])


if __name__ == "__main__":
    unittest.main()
