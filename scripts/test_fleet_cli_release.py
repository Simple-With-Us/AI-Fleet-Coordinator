"""Offline receiver tests: no credentials, SSH, or services."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("release", Path(__file__).with_name("fleet-cli-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def make_bundle(version="one"):
    files = {}
    for path in release.REQUIRED:
        data = ("# " + version + "\n").encode()
        if path.endswith(".json"):
            data = b"{}"
        if path == "scripts/linear-board/board":
            data = b'import sys\nsys.exit(4 if "whoami" in sys.argv else 0)\n# ' + version.encode()
        files[path] = {"mode": 0o644, "sha256": hashlib.sha256(data).hexdigest(),
                       "data": base64.b64encode(data).decode()}
    return {"schema": 1, "source_commit": "a" * 40, "files": files,
            "bundle_sha256": release.manifest_digest(files)}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir()
        self.base = self.home / ".local/share/fleet-cli"
        self.bundle = make_bundle()

    def tearDown(self):
        self.temp.cleanup()

    def receive(self, bundle=None, install=True):
        return release.receive(json.dumps(bundle or self.bundle).encode(), self.home, install)

    def test_allowlist_excludes_operator_data_and_noncanonical_paths(self):
        for path in [".secrets/Linear/CODEX.env", "scripts/agent_sync/server/listener.toml",
                     "scripts/agent_sync/tests/test_client.py", "AGENT-SYNC.md", "../../escape",
                     "/tmp/escape", "scripts//agent-sync", "scripts/agent_sync/../bad.py",
                     "scripts/agent_sync/.env", "scripts/agent_sync/cache.sqlite"]:
            self.assertFalse(release.allowed_path(path), path)

    def test_board_ships_with_its_agent_sync_catalog(self):
        # `board commands`, `board run` and `board mcp` read these beside board;  a release without them
        # would leave those verbs dead on the host.
        root = Path(__file__).resolve().parent.parent
        for name in ("named-commands.json", "official-mcp-tools.json", "public-inputs.json"):
            path = "scripts/linear-board/catalog/" + name
            self.assertTrue(release.allowed_path(path), path)
            self.assertIn(path, release.REQUIRED)
            self.assertLessEqual((root / path).stat().st_size, release.MAX_FILE_BYTES, path)
        for path in ("scripts/linear-board/catalog/other.json", "scripts/linear-board/testdata/agent-sync-fixtures.json",
                     "scripts/linear-board/catalog/../board"):
            self.assertFalse(release.allowed_path(path), path)

    def test_rejects_digest_and_path_before_writing(self):
        self.bundle["files"]["../../escape"] = next(iter(self.bundle["files"].values()))
        with self.assertRaisesRegex(release.Refused, "refused_path"):
            self.receive()
        self.assertFalse(self.base.exists())

    def test_rejects_corrupt_content(self):
        self.bundle["files"]["scripts/agent-sync"]["data"] = base64.b64encode(b"different").decode()
        with self.assertRaisesRegex(release.Refused, "bad_file_digest"):
            self.receive()

    def test_rejects_mode_and_size(self):
        self.bundle["files"]["scripts/agent-sync"]["mode"] = 0o777
        with self.assertRaisesRegex(release.Refused, "bad_mode"):
            self.receive()
        with self.assertRaisesRegex(release.Refused, "bundle_too_large"):
            release.decode_bundle(b"x" * (release.MAX_BUNDLE_BYTES + 1))

    def test_install_preserves_config_and_records_backup(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        original = b'model = "existing-worker"\n[features]\nfoo = true\n'
        config.write_bytes(original)
        config.chmod(0o600)
        result = self.receive()
        self.assertEqual(result["status"], "activated")
        self.assertEqual(Path(result["config_backup"]).read_bytes(), original)
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed["model"], "existing-worker")
        self.assertEqual(parsed["features"], {"foo": True})
        self.assertTrue(parsed["mcp_servers"]["agent-sync"]["command"].endswith("agent-sync-codex-mcp"))
        self.assertNotIn("env", parsed["mcp_servers"]["agent-sync"])
        wrapper = (self.home / ".local/bin/agent-sync-codex-mcp").read_text()
        self.assertIn("--codex-thread-meta", wrapper)
        self.assertNotIn("--codex-thread-meta", (self.home / ".local/bin/agent-sync").read_text())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(release.release_at(self.base, "current").name, self.bundle["bundle_sha256"])

    def test_update_keeps_configuration_and_wrappers_untouched(self):
        self.receive()
        config = self.home / ".codex/config.toml"
        config.write_bytes(config.read_bytes() + b"\n# owner edit\n")
        before = config.read_bytes()
        wrapper = self.home / ".local/bin/board"
        wrapper_before = wrapper.stat().st_mtime_ns
        result = self.receive(make_bundle("two"), install=False)
        self.assertEqual(result["status"], "activated")
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(wrapper.stat().st_mtime_ns, wrapper_before)

    def test_no_install_flag_refuses_initial_install(self):
        with self.assertRaisesRegex(release.Refused, "initial_install_required"):
            self.receive(install=False)
        self.assertFalse((self.base / "current").exists())

    def test_smoke_failure_keeps_previous_active_release(self):
        self.receive()
        with mock.patch.object(release, "smoke", side_effect=release.Refused("smoke_failed")):
            with self.assertRaisesRegex(release.Refused, "smoke_failed"):
                self.receive(make_bundle("two"), install=False)
        self.assertEqual(release.release_at(self.base, "current").name, self.bundle["bundle_sha256"])
        self.assertEqual(list(self.base.glob(".stage-*")), [])

    def test_smoke_distinguishes_timeout_from_launch_failure_without_output(self):
        for failure, code in (
                (subprocess.TimeoutExpired("private command", 12, output=b"private output"),
                 "smoke_timeout_agent_sync_version"),
                (FileNotFoundError(2, "private executable path"),
                 "smoke_start_failed_agent_sync_version_errno_2")):
            with self.subTest(code=code), mock.patch.object(release.subprocess, "run", side_effect=failure):
                with self.assertRaises(release.Refused) as raised:
                    release.smoke(self.home, self.home)
                self.assertEqual(str(raised.exception), code)

    def test_rollback_pauses_future_sync_and_resume_allows_it(self):
        first = self.receive()
        self.receive(make_bundle("two"), install=False)
        rolled = release.control(self.home, "rollback")
        self.assertEqual(rolled["status"], "paused")
        self.assertEqual(rolled["bundle_sha256"], first["bundle_sha256"])
        self.assertEqual(self.receive(make_bundle("three"), False)["status"], "paused")
        release.control(self.home, "resume")
        self.assertEqual(self.receive(make_bundle("three"), False)["status"], "activated")

    def test_idempotence_echoes_requested_commit_without_rewriting_config(self):
        self.receive()
        config = self.home / ".codex/config.toml"
        stamp = config.stat().st_mtime_ns
        self.bundle["source_commit"] = "b" * 40
        result = self.receive()
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["source_commit"], "b" * 40)
        self.assertEqual(config.stat().st_mtime_ns, stamp)

    def test_rollback_refuses_syntax_valid_modified_retained_code(self):
        self.receive()
        second = self.receive(make_bundle("two"), install=False)
        previous = release.release_at(self.base, "previous")
        (previous / "scripts/agent-sync").write_text("# syntax valid but altered\n")
        with self.assertRaisesRegex(release.Refused, "existing_release_changed"):
            release.control(self.home, "rollback")
        self.assertEqual(release.release_at(self.base, "current").name, second["bundle_sha256"])
        self.assertFalse((self.base / "paused").exists())

    def test_control_refuses_symlinked_release_directory(self):
        self.receive()
        target = self.base / "releases-moved"
        (self.base / "releases").rename(target)
        (self.base / "releases").symlink_to(target)
        for command in ("status", "pause", "resume", "rollback"):
            with self.assertRaisesRegex(release.Refused, "unsafe_directory"):
                release.control(self.home, command)

    def test_control_refuses_modified_manifest(self):
        self.receive()
        current = release.release_at(self.base, "current")
        path = current / "release.json"
        record = json.loads(path.read_text())
        record["files"]["scripts/agent-sync"]["sha256"] = "0" * 64
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(release.Refused, "invalid_release_metadata"):
            release.control(self.home, "status")

    def test_conflicting_mcp_is_not_overwritten(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        original = b'[mcp_servers.agent-sync]\ncommand = "/owner/custom"\n'
        config.write_bytes(original)
        with self.assertRaisesRegex(release.Refused, "mcp_conflict"):
            self.receive()
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.base / "current").exists())

    def test_unmanaged_wrapper_and_symlink_are_refused(self):
        bin_dir = self.home / ".local/bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "board").symlink_to("/bin/echo")
        with self.assertRaisesRegex(release.Refused, "unmanaged_wrapper"):
            self.receive()

    def test_changed_existing_release_is_refused(self):
        self.receive()
        current = release.release_at(self.base, "current")
        (current / "scripts/agent-sync").write_text("# changed")
        with self.assertRaisesRegex(release.Refused, "existing_release_changed"):
            self.receive()

    def test_install_failure_restores_config_and_new_wrappers(self):
        original_writer = release.atomic_write
        def fail_config(path, data, mode=0o600):
            if path == self.home / ".codex/config.toml":
                raise OSError("injected")
            original_writer(path, data, mode)
        with mock.patch.object(release, "atomic_write", side_effect=fail_config):
            with self.assertRaises(OSError):
                self.receive()
        self.assertFalse((self.home / ".local/bin/board").exists())
        self.assertFalse((self.base / "current").exists())

    def test_mcp_wrapper_refuses_other_seats_and_unassigned_launchers(self):
        code = release.wrapper(self.home, "scripts/agent-sync", ["mcp", "--default-seat", "CODEX"], True)
        compile(code, "wrapper", "exec")
        for env in [{"AGENT_SEAT": "JET"}, {"AGENT_LAUNCHER": "botfleet"},
                    {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-BUILDER"}]:
            result = subprocess.run([os.sys.executable, "-c", code], env=env, capture_output=True)
            self.assertEqual(result.returncode, 3)

    def test_main_reports_internal_error_without_traceback(self):
        import io
        with mock.patch.object(release, "control", side_effect=RuntimeError("boom")), \
                mock.patch.object(release.sys, "stderr", new_callable=io.StringIO) as err:
            self.assertEqual(release.main(["status", "--home", str(self.home)]), 2)
        self.assertIn("fleet-cli: internal_error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
