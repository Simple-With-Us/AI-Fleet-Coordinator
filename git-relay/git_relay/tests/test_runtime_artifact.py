"""Code-only artifact guards and a copied relay crossing the real bounded read worker."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from git_relay import app_tokens

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "relay_runtime_builder", ROOT / "git-relay/build-runtime.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "public-source"
        self.repo.mkdir()
        self.git("init", "-q")
        for path, mode in builder.FILES.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Public fixture code\n")
            target.chmod(int(mode, 8) & 0o777)
        extra = self.repo / "git-relay/deploy/seat-policy.json"
        extra.parent.mkdir()
        extra.write_text('{"synthetic": "not part of code inventory"}')
        self.commit = self.commit_source()
        self.selected = mock.patch.object(builder, "REPOSITORY", self.repo)
        self.selected.start()
        self.addCleanup(self.selected.stop)

    def git(self, *args):
        return (
            subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )

    def commit_source(self):
        self.git("add", "-A")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Public artifact fixture",
        )
        return self.git("rev-parse", "HEAD")

    def test_immutable_finite_inventory_ignores_working_changes_and_unselected_data(self):
        (self.repo / "scripts/fleet_infisical.py").write_text("uncommitted bytes")
        destination = self.root / "artifact"
        manifest = builder.build_runtime(self.commit, destination)
        self.assertEqual(set(manifest["files"]), set(builder.FILES))
        self.assertEqual(len(manifest["files"]), 38)
        self.assertEqual(manifest["sourceCommit"], self.commit)
        self.assertEqual(
            (destination / "scripts/fleet_infisical.py").read_text(), "# Public fixture code\n"
        )
        self.assertFalse((destination / "git-relay/deploy").exists())
        self.assertEqual(
            set(
                p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()
            ),
            set(builder.FILES) | {builder.MANIFEST},
        )
        builder.verify_artifact(destination, manifest)

    def test_requires_a_full_immutable_commit(self):
        for value in ("HEAD", self.commit[:12], "--help", "a" * 39, "A" * 40):
            with self.subTest(value=value), self.assertRaises(builder.ArtifactError):
                builder.build_runtime(value, self.root / "artifact")
        self.assertFalse((self.root / "artifact").exists())

    def test_missing_symlink_executable_and_oversize_source_are_refused_before_output(self):
        target = self.repo / "scripts/fleet_infisical.py"
        for problem in ("missing", "symlink", "mode", "oversize"):
            self.git("reset", "--hard", self.commit)
            if problem == "missing":
                target.unlink()
            elif problem == "symlink":
                target.unlink()
                target.symlink_to("../git-relay/git_relay/app_tokens.py")
            elif problem == "mode":
                target.chmod(0o755)
            else:
                target.write_bytes(b"x" * (builder.MAX_FILE_BYTES + 1))
            changed = self.commit_source()
            with self.subTest(problem=problem), self.assertRaises(builder.ArtifactError):
                builder.build_runtime(changed, self.root / "artifact")
            self.assertFalse((self.root / "artifact").exists())

    def test_existing_relative_dotdot_and_symlink_parent_destinations_are_refused(self):
        present = self.root / "present"
        present.mkdir()
        marker = present / "preserve"
        marker.write_text("keep")
        link = self.root / "link"
        link.symlink_to(present, target_is_directory=True)
        for destination in (
            present,
            "relative-artifact",
            str(self.root / "present/../new"),
            link / "new",
        ):
            with (
                self.subTest(destination=str(destination)),
                self.assertRaises(builder.ArtifactError),
            ):
                builder.build_runtime(self.commit, destination)
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse((present / "new").exists())

    def test_verification_refuses_content_mode_extra_file_and_symlink(self):
        for problem in ("content", "mode", "extra", "symlink"):
            destination = self.root / problem
            manifest = builder.build_runtime(self.commit, destination)
            target = destination / "scripts/fleet_infisical.py"
            if problem == "content":
                target.write_bytes(b"x" * target.stat().st_size)
            elif problem == "mode":
                target.chmod(0o755)
            elif problem == "extra":
                (destination / "unexpected").write_text("extra")
            else:
                target.unlink()
                target.symlink_to(destination / "git-relay/git_relay/app_tokens.py")
            with self.subTest(problem=problem), self.assertRaises(builder.ArtifactError):
                builder.verify_artifact(destination, manifest)


class SourceImportBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "own-binding"
        self.token_file.write_text("st." + secrets.token_urlsafe(32))
        self.token_file.chmod(0o600)
        self.source = app_tokens.FleetSecretsSource(
            credential_file=str(self.token_file),
            environ={"INFISICAL_FLEET_OPS_PROJECT_ID": str(uuid.uuid4())},
        )

    def test_default_source_pins_the_code_before_opening_or_passing_its_token(self):
        order = []

        def read(*args, **kwargs):
            order.append("read")
            return {"WANTED": "public fixture"}

        def load():
            order.append("code")
            return mock.Mock(
                bounded_fetch_project=read,
                infisical_project_id=lambda env, home: env["INFISICAL_FLEET_OPS_PROJECT_ID"],
            )

        original = self.source._token

        def token():
            order.append("binding")
            return original()

        with (
            mock.patch.object(app_tokens, "_shared_client", side_effect=load),
            mock.patch.object(self.source, "_token", side_effect=token),
        ):
            self.assertEqual(self.source.secret("WANTED"), "public fixture")
        self.assertEqual(order, ["code", "binding", "read"])

    def test_invalid_project_is_refused_without_binding_provider_or_project_file_io(self):
        self.source._environ = {"INFISICAL_FLEET_OPS_PROJECT_ID": "invalid-project-marker"}
        reader = mock.Mock()
        self.source._reader = reader
        with (
            mock.patch.object(self.source, "_token") as token,
            mock.patch.object(Path, "read_text", side_effect=AssertionError("project-file lookup")),
        ):
            with self.assertRaisesRegex(app_tokens.AppTokenError, "must be a UUID") as raised:
                self.source.secret("WANTED")
        self.assertNotIn("invalid-project-marker", str(raised.exception))
        token.assert_not_called()
        reader.assert_not_called()

    def test_missing_or_symlinked_canonical_source_fails_before_binding_read(self):
        origin = self.root / "git-relay/git_relay/app_tokens.py"
        origin.parent.mkdir(parents=True)
        origin.write_text("# Public fixture origin\n")
        for linked in (False, True):
            if linked:
                scripts = self.root / "scripts"
                scripts.mkdir()
                (scripts / "fleet_infisical.py").symlink_to(ROOT / "scripts/fleet_infisical.py")
            with (
                self.subTest(linked=linked),
                mock.patch.object(app_tokens, "__file__", str(origin)),
                mock.patch.object(app_tokens, "_CLIENT_CACHE", None),
                mock.patch.object(self.source, "_token") as token,
            ):
                with self.assertRaisesRegex(
                    app_tokens.AppTokenError, "canonical Infisical reader unavailable"
                ):
                    self.source.secret("WANTED")
                token.assert_not_called()

    def test_private_handle_ignores_foreign_canonical_and_private_module_cache(self):
        import types

        foreign = types.ModuleType("fleet_infisical")
        foreign.bounded_fetch_project = mock.Mock(side_effect=AssertionError("foreign code"))
        with (
            mock.patch.dict(
                sys.modules, {"fleet_infisical": foreign, app_tokens._CLIENT_NAME: foreign}
            ),
            mock.patch.object(app_tokens, "_CLIENT_CACHE", None),
        ):
            client = app_tokens._shared_client()
            self.assertIsNot(client, foreign)
            self.assertEqual(Path(client.__file__), ROOT / "scripts/fleet_infisical.py")
            sys.modules[app_tokens._CLIENT_NAME] = foreign
            self.assertIs(app_tokens._shared_client(), client)
            self.assertIs(sys.modules[app_tokens._CLIENT_NAME], client)
            foreign.bounded_fetch_project.assert_not_called()

    def test_symlinked_client_directory_refuses_before_binding_read(self):
        origin = self.root / "git-relay/git_relay/app_tokens.py"
        origin.parent.mkdir(parents=True)
        origin.write_text("# Public fixture origin\n")
        (self.root / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
        with (
            mock.patch.object(app_tokens, "__file__", str(origin)),
            mock.patch.object(app_tokens, "_CLIENT_CACHE", None),
            mock.patch.object(self.source, "_token") as token,
        ):
            with self.assertRaisesRegex(
                app_tokens.AppTokenError, "canonical Infisical reader unavailable"
            ):
                self.source.secret("WANTED")
            token.assert_not_called()

    def test_own_binding_custody_refuses_unsafe_files_without_calling_the_reader(self):
        for problem in ("mode", "symlink", "fifo", "oversize", "encoding", "empty"):
            binding = self.root / problem
            if problem == "symlink":
                binding.symlink_to(self.token_file)
            elif problem == "fifo":
                os.mkfifo(binding, 0o600)
            else:
                binding.write_bytes(
                    {"oversize": b"x" * 8195, "encoding": b"\xff", "empty": b""}.get(
                        problem, b"public fixture"
                    )
                )
                binding.chmod(0o644 if problem == "mode" else 0o600)
            reader = mock.Mock()
            source = app_tokens.FleetSecretsSource(
                credential_file=str(binding), environ=self.source._environ, reader=reader
            )
            with (
                self.subTest(problem=problem),
                self.assertRaises(app_tokens.AppTokenError) as raised,
            ):
                source.secret("WANTED")
            self.assertNotIn("public fixture", str(raised.exception))
            reader.assert_not_called()

    def test_own_file_rotation_applies_only_to_uncached_named_reads(self):
        seen = []

        def read(project, path, env, *, token, names, **options):
            seen.append(token)
            return {name: "public fixture" for name in names}

        self.source._reader = read
        before = self.token_file.read_text()
        self.source.secret("CACHED")
        after = "st." + secrets.token_urlsafe(32)
        self.token_file.write_text(after)
        self.source.secret("CACHED")
        self.source.secret("FRESH")
        self.assertEqual(seen, [before, after])


class CopiedReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build Git objects, never the working tree or an installed service's configuration.
        cls.commit = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.payload = cls.root / "payload"
        cls.manifest = builder.build_runtime(cls.commit, cls.payload)
        scripts = str(ROOT / "scripts")
        sys.path.insert(0, scripts)
        try:
            from fleet_knobs.tests._fake import ScopedReadAPI
        finally:
            sys.path.remove(scripts)
        cls.API = ScopedReadAPI

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_payload_contains_exact_canonical_source_without_app_or_writer_ledger(self):
        path = "scripts/fleet_infisical.py"
        expected = subprocess.check_output(
            ["git", "-C", str(ROOT), "show", self.commit + ":" + path]
        )
        self.assertEqual((self.payload / path).read_bytes(), expected)
        self.assertEqual(
            hashlib.sha256(expected).hexdigest(), self.manifest["files"][path]["sha256"]
        )
        self.assertFalse((self.payload / "scripts/fleet_secrets").exists())
        self.assertFalse((self.payload / "scripts/simple_tools/operator_github_state.py").exists())
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(self.payload / "git-relay/bin/git-relay"), "help"],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def read_payload(self, api, path, names, *, expect_failure=False):
        token_file = self.root / ("binding-" + uuid.uuid4().hex)
        token_file.write_text(api.token + "\n")
        token_file.chmod(0o600)
        script = r"""
import importlib, json, pathlib, sys, types
root, binding, project, folder, endpoint = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(root) / "git-relay"))
foreign = types.ModuleType("fleet_infisical")
def poison(*args, **kwargs): raise AssertionError("foreign import executed")
foreign.bounded_fetch_project = poison
sys.modules["fleet_infisical"] = foreign
sys.modules["_git_relay_fleet_infisical"] = foreign
from git_relay import app_tokens
client = app_tokens._shared_client()
assert pathlib.Path(client.__file__).resolve() == pathlib.Path(root) / "scripts/fleet_infisical.py"
assert client is not foreign
# Import the actual service/MCP/workflow surfaces, not standalone administrative prep scripts.
for name in ("server", "mcp_server", "workflow_verbs"):
    importlib.import_module("git_relay." + name)
request = json.load(sys.stdin)
calls = []
def read(project_arg, path_arg, env, **options):
    assert project_arg == project and path_arg == folder
    assert env == {"INFISICAL_ENVIRONMENT": "prod", "INFISICAL_READ_PATHS": folder}
    assert options["names"] == tuple(request["names"])
    assert options["timeout"] == 30 and options["max_output_bytes"] == 1048576
    calls.append(tuple(options["names"]))
    return client.bounded_fetch_project(project_arg, path_arg, {**env, "FLEET_SECRETS_API": endpoint}, **options)
source = app_tokens.FleetSecretsSource(path=folder, credential_file=binding,
    environ={"INFISICAL_FLEET_OPS_PROJECT_ID": project, "INFISICAL_TOKEN": "ignored",
             "FLEET_SECRETS_API": "https://ignored.invalid", "HOME": "/ignored"}, reader=read)
try:
    result = source.secrets(request["names"])
    assert result == request["expected"]
    assert source.secrets(request["names"]) == result and len(calls) == 1
    assert not request["expect_failure"]
except app_tokens.AppTokenError as error:
    assert request["expect_failure"]
    assert "lookup failed" in str(error)
    assert not source._cache and not source._missing
print(json.dumps({"ok": True, "reads": len(calls)}))
"""
        hostile = self.root / ("hostile-" + uuid.uuid4().hex)
        hostile.mkdir()
        (hostile / "fleet_infisical.py").write_text(
            "raise AssertionError('ambient module executed')\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                script,
                str(self.payload),
                str(token_file),
                api.project,
                path,
                api.env["FLEET_SECRETS_API"],
            ],
            input=json.dumps(
                {
                    "names": names,
                    "expected": {n: api.values[n].strip() for n in names},
                    "expect_failure": expect_failure,
                }
            ),
            text=True,
            capture_output=True,
            timeout=10,
            cwd=hostile,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(hostile)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "reads": 1})

    def test_each_existing_purpose_crosses_real_worker_with_own_exact_scope(self):
        for path, values in (
            (
                "/github-apps",
                {"GH_APP_FIXTURE_ID": "42", "GH_APP_FIXTURE_PRIVATE_KEY": "public fixture"},
            ),
            ("/tools", {"GR_BEARER_VERIFY_KEY": secrets.token_urlsafe(32)}),
            ("/git-relay-owner", {"GITHUB_TOKEN": secrets.token_urlsafe(32)}),
        ):
            with (
                self.subTest(path=path),
                self.API(values, project=str(uuid.uuid4()), path=path) as api,
            ):
                self.read_payload(api, path, list(values))
                self.assertEqual(len(api.requests), 1 + len(values))
                self.assertTrue(all("/raw?" not in request for request in api.requests))

    def test_expiry_broader_scope_and_write_grants_fail_before_any_value_read(self):
        for metadata in (
            {"expiresAt": None},
            {"permissions": ["read", "write"]},
            {"workspace": str(uuid.uuid4())},
            {"scopes": [{"environment": "prod", "secretPath": "/tools"}]},
            {"scopes": [{"environment": "dev", "secretPath": "/github-apps"}]},
            {
                "scopes": [
                    {"environment": "prod", "secretPath": "/github-apps"},
                    {"environment": "prod", "secretPath": "/tools"},
                ]
            },
        ):
            with (
                self.subTest(metadata=metadata),
                self.API(
                    {"WANTED": "public fixture"}, project=str(uuid.uuid4()), path="/github-apps"
                ) as api,
            ):
                api.metadata_override = metadata
                self.read_payload(api, "/github-apps", ["WANTED"], expect_failure=True)
                self.assertEqual(api.requests, ["/api/v2/service-token"])


if __name__ == "__main__":
    unittest.main()
