"""Offline receiver tests: no credentials, SSH, or services."""

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "release", Path(__file__).with_name("fleet-cli-release.py")
)
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
        files[path] = {
            "mode": 0o644,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode(),
        }
    return {
        "schema": 1,
        "source_commit": "a" * 40,
        "files": files,
        "bundle_sha256": release.manifest_digest(files),
    }


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
        for path in [
            ".secrets/Linear/CODEX.env",
            "scripts/agent_sync/server/listener.toml",
            "scripts/agent_sync/tests/test_client.py",
            "AGENT-SYNC.md",
            "../../escape",
            "/tmp/escape",
            "scripts//agent-sync",
            "scripts/agent_sync/../bad.py",
            "scripts/agent_sync/.env",
            "scripts/agent_sync/cache.sqlite",
        ]:
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
        for path in (
            "scripts/linear-board/catalog/other.json",
            "scripts/linear-board/testdata/agent-sync-fixtures.json",
            "scripts/linear-board/catalog/../board",
        ):
            self.assertFalse(release.allowed_path(path), path)

    def test_ships_recall_lane_and_fleet_mode_and_nothing_else_of_their_packages(self):
        # AFC-331:  the server mirror carries the five fleet tools.  Only the modules each one imports
        # ship;  recall eval/ingest (Mac and box jobs), lane's install and guard-hook modules, and the
        # whole of fleet_rag/tests stay out.
        for path in [
            "scripts/recall",
            "scripts/fleet_rag/core.py",
            "scripts/fleet_rag/recall_api.py",
            "scripts/fleet_lanes/bin/lane",
            "scripts/fleet_lanes/lane.py",
            "scripts/fleet_lanes/layout.py",
            "scripts/fleet-mode",
            "docs/FLEET-MODE.md",
        ]:
            self.assertTrue(release.allowed_path(path), path)
            self.assertIn(path, release.REQUIRED)
        for path in [
            "scripts/fleet_rag/eval.py",
            "scripts/fleet_rag/ingest.py",
            "scripts/fleet_rag/golden.jsonl",
            "scripts/fleet_rag/tests/test_core.py",
            "scripts/fleet_lanes/install_tools.py",
            "scripts/fleet_lanes/fleet_guard_hook.py",
            "scripts/apple-notes-coding.sh",
            "docs/RAG-FLEET-INFRA.md",
        ]:
            self.assertFalse(release.allowed_path(path), path)

    def test_recall_modules_cover_its_static_imports(self):
        # The names the receiver ships must be exactly the fleet_rag modules recall can reach without a
        # lazy import, so a new top-level import in recall or fleet_rag fails here, not on the server.
        import re

        root = Path(__file__).resolve().parent
        seen, queue = set(), ["recall_api", "core", "doctor", "public_fallback", "contribute_guard"]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            text = (root / "fleet_rag" / (name + ".py")).read_text()
            for line in text.splitlines():  # module-level imports only:  no indentation
                m = re.match(r"from \. import (.+)", line) or re.match(r"from \.(\w+) import", line)
                if m:
                    queue += [
                        part.strip().split(" ")[0]
                        for part in m.group(1).split(",")
                        if part.strip() and part.strip().split(" ")[0] != "__version__"
                    ]
        self.assertEqual(seen - {"__init__"}, set(release.RECALL_MODULES) - {"__init__"})

    def test_ships_the_knob_reader_and_not_its_tests_cli_or_readme(self):
        # AFC-331:  agent-sync reads the NOTES_RELAY_CONSUMER knob through fleet_knobs.  If the server release lacked it,
        # the import would fail and the host would silently trust the default consumer after the knob rotated.
        for name in release.KNOB_MODULES:
            path = "scripts/fleet_knobs/%s.py" % name
            self.assertTrue(release.allowed_path(path), path)
            self.assertIn(path, release.TOOL_REQUIRED, "a new bundle must carry it")
            self.assertNotIn(
                path,
                release.BASE_REQUIRED,
                "an older retained release stays a valid rollback target",
            )
        for path in [
            "scripts/fleet_knobs/tests/test_knobs.py",
            "scripts/fleet_knobs/__main__.py",
            "scripts/fleet_knobs/inventory.py",
            "scripts/fleet_knobs/README.md",
            "scripts/fleet_knobs/_fake.py",
        ]:
            self.assertFalse(release.allowed_path(path), path)

    def test_the_shipped_knob_modules_are_exactly_what_a_lookup_imports_and_run_on_stdlib_alone(
        self,
    ):
        import re
        import shutil

        root = Path(__file__).resolve().parent / "fleet_knobs"
        seen, queue = set(), ["knobs"]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            for line in (
                (root / (name + ".py")).read_text().splitlines()
            ):  # module-level imports only
                m = re.match(r"from \. import (.+)", line) or re.match(r"from \.(\w+) import", line)
                if m:
                    queue += [
                        part.strip().split(" ")[0] for part in m.group(1).split(",") if part.strip()
                    ]
        self.assertEqual(seen | {"__init__"}, set(release.KNOB_MODULES))
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "fleet_knobs"
            package.mkdir()
            for name in release.KNOB_MODULES:
                shutil.copy(root / (name + ".py"), package / (name + ".py"))
            # Only the shipped files, a clean environment and no infisical CLI:  the lookup must answer with a labelled default.
            code = (
                "import sys; sys.path.insert(0, %r); from fleet_knobs import knobs; "
                "k = knobs.resolve('NOTES_RELAY_CONSUMER', 'CLAUDE', 'str'); print(k.value, k.source)"
                % tmp
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", code],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
                env={"PATH": "/nonexistent", "HOME": tmp},
            )
            self.assertEqual(result.returncode, 0, result.stderr[-400:])
            self.assertEqual(result.stdout.split()[0], "CLAUDE")

    def test_install_writes_wrappers_for_all_five_tools(self):
        self.receive()
        bin_dir = self.home / ".local/bin"
        for name, script, interpreter in [
            ("recall", "scripts/recall", "/usr/bin/python3"),
            ("lane", "scripts/fleet_lanes/bin/lane", "/bin/bash"),
            ("fleet-mode", "scripts/fleet-mode", "/bin/bash"),
        ]:
            text = (bin_dir / name).read_text()
            self.assertIn(release.MARKER, text)
            self.assertIn(script, text)
            self.assertIn(interpreter, text)
            self.assertEqual((bin_dir / name).stat().st_mode & 0o777, 0o755)
        # Python tools stay isolated (-I -B);  the bash tools get the caller's environment untouched.
        self.assertIn("'-I', '-B'", (bin_dir / "recall").read_text())
        self.assertNotIn("-I", (bin_dir / "lane").read_text().split("argv =")[1])
        for name in ("board", "agent-sync"):
            self.assertIn(
                "'/usr/bin/python3', '-I', '-B', str(release", (bin_dir / name).read_text()
            )

    def test_a_routine_update_adds_new_wrappers_but_never_overwrites(self):
        # A host installed before recall, lane and fleet-mode existed gets their wrappers on the next push.
        self.receive()
        bin_dir = self.home / ".local/bin"
        for name in ("recall", "lane", "fleet-mode"):
            (bin_dir / name).unlink()
        custom = b"#!/bin/sh\n# my own board, not managed\n"
        (bin_dir / "board").write_bytes(custom)
        config = self.home / ".codex/config.toml"
        before = config.read_bytes() if config.exists() else None
        result = self.receive(make_bundle("two"), install=False)
        self.assertEqual(result["status"], "activated")
        for name in ("recall", "lane", "fleet-mode"):
            self.assertIn(release.MARKER, (bin_dir / name).read_text(), name)
        self.assertEqual(
            (bin_dir / "board").read_bytes(), custom, "an existing wrapper is never overwritten"
        )
        self.assertEqual(
            config.read_bytes() if config.exists() else None,
            before,
            "the Codex config is untouched",
        )
        # Nothing new to add:  a further update writes no wrapper.
        self.assertEqual(release.new_wrapper_plan(self.home), {})

    def test_installed_bash_wrappers_run_their_script(self):
        # A release holding real (tiny) tools:  the wrapper must reach the script in the current release.
        bundle = make_bundle()
        for path, body in {
            "scripts/fleet-mode": b'#!/bin/bash\necho fleet-mode-ran "$@"\n',
            "scripts/fleet_lanes/bin/lane": b'#!/bin/bash\necho lane-ran "$@"\n',
        }.items():
            bundle["files"][path] = {
                "mode": 0o755,
                "sha256": hashlib.sha256(body).hexdigest(),
                "data": base64.b64encode(body).decode(),
            }
        bundle["bundle_sha256"] = release.manifest_digest(bundle["files"])
        self.receive(bundle)
        for name, expected in [
            ("fleet-mode", b"fleet-mode-ran --full"),
            ("lane", b"lane-ran path a b"),
        ]:
            args = ["--full"] if name == "fleet-mode" else ["path", "a", "b"]
            out = subprocess.run([str(self.home / ".local/bin" / name), *args], capture_output=True)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, expected), out.stderr)

    def test_lane_shim_never_writes_bytecode_into_a_release(self):
        # Linux CI on #589:  `lane` ran python without -B, wrote __pycache__ into the release, and
        # the hash check then refused it as changed.
        shim = (Path(__file__).resolve().parent / "fleet_lanes/bin/lane").read_text()
        self.assertIn("-I -B -c", shim)

    def old_bundle(self):
        """A release as installed before AFC-331:  only the base files."""
        bundle = make_bundle("old")
        for path in release.TOOL_REQUIRED:
            bundle["files"].pop(path, None)
        bundle["bundle_sha256"] = release.manifest_digest(bundle["files"])
        return bundle

    def test_rollback_to_a_release_that_predates_the_new_tools(self):
        # Sentry on #589:  widening REQUIRED must not make verify_retained refuse the previous release.
        old = self.old_bundle()
        self.assertFalse(release.REQUIRED.issubset(old["files"]))
        with mock.patch.object(
            release, "REQUIRED", release.BASE_REQUIRED
        ):  # the receiver of that day
            self.receive(old)
        self.receive(make_bundle("new"), install=False)
        self.assertEqual(release.release_at(self.base, "previous").name, old["bundle_sha256"])
        result = release.control(self.home, "rollback")
        self.assertEqual(result["bundle_sha256"], old["bundle_sha256"])
        self.assertEqual(release.control(self.home, "status")["status"], "paused")
        self.assertFalse(
            (self.base / "releases" / old["bundle_sha256"] / "scripts/recall").exists()
        )

    def test_a_new_bundle_without_the_new_tools_is_still_refused(self):
        with self.assertRaisesRegex(release.Refused, "bad_file_set"):
            self.receive(self.old_bundle())

    def test_a_retained_release_still_needs_its_base_files(self):
        self.receive(make_bundle("new"))
        current = release.release_at(self.base, "current")
        meta = json.loads((current / "release.json").read_text())
        del meta["files"]["scripts/agent-sync"]
        (current / "release.json").write_text(json.dumps(meta))
        with self.assertRaisesRegex(release.Refused, "invalid_release_metadata"):
            release.control(self.home, "status")

    def test_smoke_runs_the_new_tools(self):
        # The smoke cases for recall, lane and fleet-mode fail a release whose tool exits wrongly.
        bundle = make_bundle()
        body = b"#!/bin/bash\nexit 7\n"
        bundle["files"]["scripts/fleet-mode"] = {
            "mode": 0o755,
            "sha256": hashlib.sha256(body).hexdigest(),
            "data": base64.b64encode(body).decode(),
        }
        bundle["bundle_sha256"] = release.manifest_digest(bundle["files"])
        with self.assertRaisesRegex(release.Refused, "smoke_exit_mismatch_fleet_mode_block"):
            self.receive(bundle)

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
        self.assertTrue(
            parsed["mcp_servers"]["agent-sync"]["command"].endswith("agent-sync-codex-mcp")
        )
        self.assertNotIn("env", parsed["mcp_servers"]["agent-sync"])
        wrapper = (self.home / ".local/bin/agent-sync-codex-mcp").read_text()
        self.assertIn("--codex-thread-meta", wrapper)
        self.assertNotIn("--codex-thread-meta", (self.home / ".local/bin/agent-sync").read_text())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            release.release_at(self.base, "current").name, self.bundle["bundle_sha256"]
        )

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
        self.assertEqual(
            release.release_at(self.base, "current").name, self.bundle["bundle_sha256"]
        )
        self.assertEqual(list(self.base.glob(".stage-*")), [])

    def test_smoke_distinguishes_timeout_from_launch_failure_without_output(self):
        for failure, code in (
            (
                subprocess.TimeoutExpired("private command", 12, output=b"private output"),
                "smoke_timeout_agent_sync_version",
            ),
            (
                FileNotFoundError(2, "private executable path"),
                "smoke_start_failed_agent_sync_version_errno_2",
            ),
        ):
            with (
                self.subTest(code=code),
                mock.patch.object(release.subprocess, "run", side_effect=failure),
            ):
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
        code = release.wrapper(
            self.home, "scripts/agent-sync", ["mcp", "--default-seat", "CODEX"], True
        )
        compile(code, "wrapper", "exec")
        for env in [
            {"AGENT_SEAT": "JET"},
            {"AGENT_LAUNCHER": "botfleet"},
            {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-BUILDER"},
        ]:
            result = subprocess.run([os.sys.executable, "-c", code], env=env, capture_output=True)
            self.assertEqual(result.returncode, 3)

    def test_main_reports_internal_error_without_traceback(self):
        import io

        with (
            mock.patch.object(release, "control", side_effect=RuntimeError("boom")),
            mock.patch.object(release.sys, "stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(release.main(["status", "--home", str(self.home)]), 2)
        self.assertIn("fleet-cli: internal_error", err.getvalue())


class ShippedDataFilesTests(unittest.TestCase):
    """Every data file the agent_sync package reads at run time ships in a release.  The .py
    modules are allowed by pattern;  JSON and similar files must be named in EXACT one by one, and
    a new one that is not breaks the server's `agent-sync mcp` at startup (listener-tools.json,
    2026-10-16)."""

    def test_every_tracked_agent_sync_data_file_is_allowed(self):
        root = Path(__file__).resolve().parent.parent
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "scripts/agent_sync"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        data = [
            p
            for p in tracked
            if not p.endswith(".py")
            and "/tests/" not in p
            and not p.endswith((".md", ".sh"))
            and "/server/" not in p
        ]
        self.assertTrue(data)
        missing = [p for p in data if not release.allowed_path(p)]
        self.assertEqual(missing, [], "add these to EXACT in scripts/fleet-cli-release.py")

    def test_listener_tools_is_required_of_new_bundles_only(self):
        # A new bundle must carry it (agent-sync mcp reads it at startup), but retained releases
        # are judged by BASE_REQUIRED, so the ones built before it stay valid rollback targets.
        self.assertIn("scripts/agent_sync/mcp/listener-tools.json", release.TOOL_REQUIRED)
        self.assertNotIn("scripts/agent_sync/mcp/listener-tools.json", release.BASE_REQUIRED)


if __name__ == "__main__":
    unittest.main()
