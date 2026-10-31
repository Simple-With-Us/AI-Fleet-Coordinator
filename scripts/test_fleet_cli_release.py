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
        if path in ("scripts/linear-board/board", "scripts/simple-board"):
            data = b'import sys\nsys.exit(2 if "show" in sys.argv else 0)\n# ' + version.encode()
        if path == "scripts/simple-git":
            data = (
                b'import sys\nsys.exit(2 if sys.argv[1:] == ["call"] else 0)\n# ' + version.encode()
            )
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
        # The host installs the seat helper (host configuration, not a release file) before the wrappers.
        (self.home / ".config/fleet-cli").mkdir(parents=True)
        (self.home / ".config/fleet-cli/seat-remote-defaults.py").write_text("# test helper\n")
        self.bundle = make_bundle()
        # The wrapper carries the roots the host names, so the suite must not read
        # the roots of the machine it runs on:  every one of them is empty here and
        # the tests that need a root name it themselves.
        self.patches = [
            mock.patch.dict(
                os.environ,
                {
                    "FLEET_CODE_ROOT": "",
                    "FLEET_APPS_ROOT": "",
                    "FLEET_LANES_ROOT": "",
                    "FLEET_SERVER_VOLUME": "",
                },
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for patch in reversed(self.patches):
            patch.stop()

    def receive(self, bundle=None, install=True):
        return release.receive(json.dumps(bundle or self.bundle).encode(), self.home, install)

    def test_notes_service_reader_required_only_for_new_bundles(self):
        path = "scripts/agent_sync/notes_service_reader.py"
        self.assertTrue(release.allowed_path(path))
        self.assertIn(path, release.TOOL_REQUIRED)
        self.assertNotIn(path, release.BASE_REQUIRED)
        self.bundle["files"].pop(path)
        self.bundle["bundle_sha256"] = release.manifest_digest(self.bundle["files"])
        with self.assertRaisesRegex(release.Refused, "bad_file_set"):
            release.decode_bundle(json.dumps(self.bundle).encode())

    def test_bearer_gate_required_only_for_new_bundles(self):
        path = "scripts/agent_sync/mcp/bearer_gate.py"
        self.assertTrue(release.allowed_path(path))
        self.assertIn(path, release.TOOL_REQUIRED)
        self.assertNotIn(path, release.BASE_REQUIRED)
        self.bundle["files"].pop(path)
        self.bundle["bundle_sha256"] = release.manifest_digest(self.bundle["files"])
        with self.assertRaisesRegex(release.Refused, "bad_file_set"):
            release.decode_bundle(json.dumps(self.bundle).encode())

    def test_allowlist_excludes_operator_data_and_noncanonical_paths(self):
        for path in [
            ".secrets/Linear/CODEX.env",
            "scripts/agent_sync/server/listener.toml",
            "scripts/agent_sync/tests/test_client.py",
            "AGENT-SYNC.md",
            "../../escape",
            "/tmp/escape",
            "scripts//simple-chat",
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
        # AFC-486:  simple-notes reads the OPERATOR_API_URL knob through fleet_knobs.  If the server release lacked it,
        # the import would fail and the server could never reach the Operator's notes queue.
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
                "k = knobs.resolve('OPERATOR_API_URL', '', 'str'); print(repr(k.value), k.source)"
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
            self.assertEqual(result.stdout.split(), ["''", "default"])

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
        for name in ("simple-board", "simple-chat", "simple-notes", "simple-git"):
            self.assertIn(
                "'/usr/bin/python3', '-I', '-B', str(release", (bin_dir / name).read_text()
            )

    def test_platform_letter_gets_a_managed_isolated_wrapper(self):
        # AFC-564: the session-letter hook of the non-Claude platforms runs this wrapper on the server.  It is
        # isolated like the other Python tools, carries no lane roots, and execs the script of the current release.
        self.receive()
        path = self.home / ".local/bin/platform-letter"
        text = path.read_text()
        self.assertIn(release.MARKER, text)
        self.assertIn("scripts/platform-letter", text)
        self.assertIn("'/usr/bin/python3', '-I', '-B', str(release", text)
        self.assertNotIn("FLEET_CODE_ROOT", text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_a_host_without_platform_letter_gets_its_wrapper_on_the_next_update(self):
        self.receive()
        bin_dir = self.home / ".local/bin"
        (bin_dir / "platform-letter").unlink()
        result = self.receive(make_bundle("two"), install=False)
        self.assertEqual(result["status"], "activated")
        self.assertIn(release.MARKER, (bin_dir / "platform-letter").read_text())

    def test_a_routine_update_adds_new_wrappers_but_never_overwrites(self):
        # A host installed before recall, lane and fleet-mode existed gets their wrappers on the next push.
        self.receive()
        bin_dir = self.home / ".local/bin"
        for name in ("recall", "lane", "fleet-mode", "simple-git"):
            (bin_dir / name).unlink()
        custom = b"#!/bin/sh\n# my own board, not managed\n"
        (bin_dir / "simple-board").write_bytes(custom)
        config = self.home / ".codex/config.toml"
        before = config.read_bytes() if config.exists() else None
        result = self.receive(make_bundle("two"), install=False)
        self.assertEqual(result["status"], "activated")
        for name in ("recall", "lane", "fleet-mode", "simple-git"):
            self.assertIn(release.MARKER, (bin_dir / name).read_text(), name)
        self.assertEqual(
            (bin_dir / "simple-board").read_bytes(),
            custom,
            "an existing wrapper is never overwritten",
        )
        self.assertEqual(
            config.read_bytes() if config.exists() else None,
            before,
            "the Codex config is untouched",
        )
        # Nothing new to add:  a further update writes no wrapper.
        self.assertEqual(release.new_wrapper_plan(self.home), {})

    def test_helper_wiring_is_generated_for_chat_board_and_recall_only(self):
        helper = str(self.home / ".config/fleet-cli/seat-remote-defaults.py")
        for name in ("simple-chat", "simple-board", "recall"):
            text = release.wrapper(self.home, "scripts/" + name).decode()
            line = f"import runpy\nrunpy.run_path({helper!r}, init_globals={{'fleet_command': {name!r}}})\n"
            self.assertIn(line, text, name)
            # the helper runs after the imports and before the release is resolved and the tool is exec'd
            self.assertLess(text.index("from pathlib import Path"), text.index(line), name)
            self.assertLess(text.index(line), text.index("release = Path("), name)
        for script in (
            "scripts/simple-notes",
            "scripts/fleet-mode",
            "scripts/fleet_lanes/bin/lane",
        ):
            self.assertNotIn("runpy", release.wrapper(self.home, script).decode(), script)
        # the CODEX-only MCP registration passes its own seat and never reads a bearer file
        mcp = release.wrapper(self.home, "scripts/simple-chat", ["mcp"], True).decode()
        self.assertNotIn("runpy", mcp)
        self.assertNotIn("seat-remote-defaults", mcp)
        # no credential value or secret path component is generated
        for name in ("simple-chat", "simple-board", "recall"):
            self.assertNotIn(".secrets", release.wrapper(self.home, "scripts/" + name).decode())

    def test_a_host_without_the_helper_keeps_plain_wrappers(self):
        helper = self.home / ".config/fleet-cli/seat-remote-defaults.py"
        helper.unlink()
        for name in ("simple-chat", "simple-board", "recall"):
            text = release.wrapper(self.home, "scripts/" + name).decode()
            self.assertNotIn("runpy", text, name)
            self.assertIn("os.execv(argv[0], argv)", text, name)
        self.receive()  # installs without refusing
        self.assertNotIn("runpy", (self.home / ".local/bin/recall").read_text())
        # a symlinked helper is not trusted either
        helper.symlink_to(self.home / "elsewhere")
        self.assertNotIn("runpy", release.wrapper(self.home, "scripts/recall").decode())
        helper.unlink()
        helper.write_text("# helper\n")
        self.assertIn("runpy", release.wrapper(self.home, "scripts/recall").decode())

    def test_helper_lifecycle_wired_wrapper_fails_closed_when_the_helper_is_removed(self):
        # Contract (see HELPER_COMMANDS):  generated with the helper -> wired;  helper removed later -> the
        # command exits nonzero and never runs the tool (no silent plain execution).
        marker = self.home / "ran"
        release_dir = self.home / ".local/share/fleet-cli/current/scripts"
        release_dir.mkdir(parents=True)
        (release_dir / "recall").write_text(
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        )
        helper = self.home / ".config/fleet-cli/seat-remote-defaults.py"
        wrapper = self.home / ".local/bin/recall"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_bytes(release.wrapper(self.home, "scripts/recall"))
        self.assertIn("seat-remote-defaults.py", wrapper.read_text())
        run = lambda: subprocess.run(
            [sys.executable, "-I", str(wrapper)], capture_output=True, text=True
        )
        self.assertEqual(run().returncode, 0)
        self.assertTrue(marker.exists())
        marker.unlink()
        helper.unlink()
        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists(), "the tool must not run without its helper")
        self.assertNotIn(".secrets", result.stdout + result.stderr)

    def test_helper_lifecycle_late_helper_needs_the_explicit_install(self):
        # Contract:  generated without the helper -> plain;  helper provisioned later -> a routine update leaves
        # the existing wrapper plain;  the canonical --install then wires it.
        helper = self.home / ".config/fleet-cli/seat-remote-defaults.py"
        helper.unlink()
        self.receive()
        recall = self.home / ".local/bin/recall"
        self.assertNotIn("runpy", recall.read_text())
        helper.write_text("# helper\n")
        self.receive(install=False)
        self.assertNotIn("runpy", recall.read_text())
        self.receive(install=True)
        self.assertIn("seat-remote-defaults.py", recall.read_text())
        for name in ("simple-chat", "simple-board"):
            self.assertIn("seat-remote-defaults.py", (self.home / ".local/bin" / name).read_text())

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

    def test_the_lane_wrapper_carries_the_roots(self):
        # A server host keeps its trees on a volume and links no ~/Code or ~/apps into the home,
        # so a wrapper that resolved only the home would find no tree and no repo.
        self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        self.assertIn("os.environ.setdefault('FLEET_CODE_ROOT'", text)
        self.assertIn("os.environ.setdefault('FLEET_APPS_ROOT'", text)

    def test_the_roots_follow_the_home_when_it_has_them(self):
        (self.home / "Code").mkdir(parents=True, exist_ok=True)
        (self.home / "apps").mkdir(parents=True, exist_ok=True)
        self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        self.assertIn(
            f"os.environ.setdefault('FLEET_CODE_ROOT', {str(self.home / 'Code')!r})", text
        )
        self.assertIn(
            f"os.environ.setdefault('FLEET_APPS_ROOT', {str(self.home / 'apps')!r})", text
        )

    def test_the_roots_follow_the_volume_the_host_names(self):
        # A server host keeps its trees on a volume and links no ~/Code or ~/apps into the home.
        # The volume's path is named by the host in the environment, so it is not in this repository.
        with tempfile.TemporaryDirectory() as volume:
            (Path(volume) / "Code").mkdir()
            (Path(volume) / "apps").mkdir()
            with mock.patch.dict(os.environ, {"FLEET_SERVER_VOLUME": volume}):
                self.receive()
            text = (self.home / ".local/bin/lane").read_text()
            self.assertIn(
                f"os.environ.setdefault('FLEET_CODE_ROOT', {str(Path(volume) / 'Code')!r})", text
            )
            self.assertIn(
                f"os.environ.setdefault('FLEET_APPS_ROOT', {str(Path(volume) / 'apps')!r})", text
            )

    def test_a_configured_root_wins_over_the_home_and_the_volume(self):
        with tempfile.TemporaryDirectory() as volume:
            (Path(volume) / "Code").mkdir()
            with mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": "/srv/other/Code"}):
                self.receive()
            text = (self.home / ".local/bin/lane").read_text()
            self.assertIn("os.environ.setdefault('FLEET_CODE_ROOT', '/srv/other/Code')", text)

    def test_only_the_lane_wrapper_carries_the_roots(self):
        self.receive()
        for name in ("simple-board", "simple-chat", "simple-notes", "recall", "fleet-mode"):
            self.assertNotIn("FLEET_CODE_ROOT", (self.home / ".local/bin" / name).read_text(), name)

    def test_the_wrapper_lets_the_caller_win(self):
        # setdefault, never an assignment:  an agent that exports the roots itself keeps them.
        body = b'#!/bin/bash\necho "${FLEET_CODE_ROOT:-unset}"\n'
        bundle = make_bundle()
        bundle["files"]["scripts/fleet_lanes/bin/lane"] = {
            "mode": 0o755,
            "sha256": hashlib.sha256(body).hexdigest(),
            "data": base64.b64encode(body).decode(),
        }
        bundle["bundle_sha256"] = release.manifest_digest(bundle["files"])
        self.receive(bundle)
        wrapper = self.home / ".local/bin/lane"
        out = subprocess.run(
            [str(wrapper)],
            capture_output=True,
            text=True,
            env={**os.environ, "FLEET_CODE_ROOT": "/from/the/caller"},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "/from/the/caller")

    def test_the_lane_wrapper_carries_the_lanes_root_the_host_names(self):
        # A host names its lanes root when the lanes do not sit beside the apps
        # folder:  the wrapper carries that one and not a derived path.
        lanes = self.home / "elsewhere" / "lanes"
        lanes.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": str(lanes)}):
            self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        self.assertIn(f"os.environ.setdefault('FLEET_LANES_ROOT', {str(lanes)!r})", text)
        self.assertNotIn(
            f"os.environ.setdefault('FLEET_LANES_ROOT', {str(self.home / 'apps' / 'lanes')!r})",
            text,
        )

    def test_the_lane_wrapper_carries_no_lanes_root_the_host_does_not_name(self):
        self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        self.assertNotIn("FLEET_LANES_ROOT", text)

    def test_the_wrapper_never_overrides_the_lanes_root_the_caller_set(self):
        # setdefault, never an assignment:  an agent that exports the lanes root
        # itself keeps it.
        body = b'#!/bin/bash\necho "${FLEET_LANES_ROOT:-unset}"\n'
        bundle = make_bundle()
        bundle["files"]["scripts/fleet_lanes/bin/lane"] = {
            "mode": 0o755,
            "sha256": hashlib.sha256(body).hexdigest(),
            "data": base64.b64encode(body).decode(),
        }
        bundle["bundle_sha256"] = release.manifest_digest(bundle["files"])
        self.receive(bundle)
        wrapper = self.home / ".local/bin/lane"
        out = subprocess.run(
            [str(wrapper)],
            capture_output=True,
            text=True,
            env={**os.environ, "FLEET_LANES_ROOT": "/from/the/caller"},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "/from/the/caller")

    def test_every_path_the_wrapper_carries_comes_from_the_fake_home(self):
        (self.home / "Code").mkdir()
        (self.home / "apps").mkdir()
        self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        roots = [line for line in text.splitlines() if "setdefault('FLEET_" in line]
        self.assertTrue(roots)
        for line in roots:
            self.assertIn(str(self.home), line)

    def test_a_whitespace_only_root_is_not_a_relative_path(self) -> None:
        # A value of spaces must not become "." and point the lane tools at the
        # caller's working directory.
        with mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": "   "}):
            self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        self.assertNotIn("os.environ.setdefault('FLEET_CODE_ROOT', '.')", text)
        self.assertIn(
            f"os.environ.setdefault('FLEET_CODE_ROOT', {str(self.home / 'Code')!r})", text
        )

    def test_a_tilde_user_root_is_left_alone(self) -> None:
        # A ~user spelling names another account's home:  the wrapper carries it as it
        # was named, and the tool that reads it resolves it.
        with mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": "~otheruser/Code"}):
            self.receive()
        text = (self.home / ".local/bin/lane").read_text()
        self.assertIn("os.environ.setdefault('FLEET_CODE_ROOT', '~otheruser/Code')", text)

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
        del meta["files"]["scripts/agent_sync/cli.py"]
        (current / "release.json").write_text(json.dumps(meta))
        with self.assertRaisesRegex(release.Refused, "invalid_release_metadata"):
            release.control(self.home, "status")

    def test_renamed_entry_points_are_new_bundle_only(self):
        # #860 r4:  a retained pre-rename release carries scripts/agent-sync, not the three cutover
        # entry points, so they must live in TOOL_REQUIRED (new bundles carry them) rather than
        # BASE_REQUIRED (which would make status and rollback refuse the rollback target itself).
        for path in ("scripts/simple-chat", "scripts/simple-notes", "scripts/simple-board"):
            self.assertIn(path, release.TOOL_REQUIRED, path)
            self.assertNotIn(
                path,
                release.BASE_REQUIRED,
                "a retained pre-rename release stays a valid rollback target: " + path,
            )

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

    def test_file_count_boundary_preserves_decode_and_refusal(self):
        # All entries are allowed and digest-valid, so count is the only boundary.
        for number in range(release.MAX_FILES - len(self.bundle["files"])):
            self.bundle["files"][f"scripts/agent_sync/boundary_{number}.py"] = {
                "mode": 0o644,
                "sha256": hashlib.sha256(b"# boundary\n").hexdigest(),
                "data": base64.b64encode(b"# boundary\n").decode(),
            }
        self.bundle["bundle_sha256"] = release.manifest_digest(self.bundle["files"])
        raw = json.dumps(self.bundle).encode()
        self.assertEqual(len(release.decode_bundle(raw)[1]), release.MAX_FILES)
        self.bundle["files"]["scripts/agent_sync/one_too_many.py"] = next(
            iter(self.bundle["files"].values())
        )
        self.bundle["bundle_sha256"] = release.manifest_digest(self.bundle["files"])
        with self.assertRaisesRegex(release.Refused, "bad_file_set"):
            release.decode_bundle(json.dumps(self.bundle).encode())
        self.assertFalse(self.base.exists())

    def test_rejects_corrupt_content(self):
        self.bundle["files"]["scripts/simple-chat"]["data"] = base64.b64encode(
            b"different"
        ).decode()
        with self.assertRaisesRegex(release.Refused, "bad_file_digest"):
            self.receive()

    def test_rejects_mode_and_size(self):
        self.bundle["files"]["scripts/simple-chat"]["mode"] = 0o777
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
            parsed["mcp_servers"]["simple-chat"]["command"].endswith("simple-chat-codex-mcp")
        )
        self.assertNotIn("env", parsed["mcp_servers"]["simple-chat"])
        wrapper = (self.home / ".local/bin/simple-chat-codex-mcp").read_text()
        self.assertIn("--codex-thread-meta", wrapper)
        self.assertNotIn("--codex-thread-meta", (self.home / ".local/bin/simple-chat").read_text())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            release.release_at(self.base, "current").name, self.bundle["bundle_sha256"]
        )

    def test_install_migrates_a_prerenamed_codex_registration_instead_of_refusing(self):
        # A host installed before the CLI rename carries the legacy wrapper path;  the release must
        # rewrite it (the old wrapper execs scripts/agent-sync, which this release no longer ships).
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        legacy = str(self.home / ".local/bin/agent-sync-codex-mcp")
        config.write_bytes(
            (
                'model = "existing-worker"\n'
                "# Local CODEX-only fleet MCP; no credentials or pinned seat.\n"
                "[mcp_servers.agent-sync]\n"
                'command = "%s"\n'
                'env_vars = ["AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG",'
                ' "AGENT_SESSION"]\n' % legacy
            ).encode()
        )
        config.chmod(0o600)
        result = self.receive()
        self.assertEqual(result["status"], "activated")
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed["model"], "existing-worker")
        self.assertEqual(
            parsed["mcp_servers"]["simple-chat"]["command"],
            str(self.home / ".local/bin/simple-chat-codex-mcp"),
        )
        self.assertNotIn("agent-sync", config.read_text())
        self.assertNotIn("agent-sync", parsed["mcp_servers"])

    def test_update_path_migrates_the_prerenamed_codex_registration_too(self):
        self.receive()
        config = self.home / ".codex/config.toml"
        legacy = str(self.home / ".local/bin/agent-sync-codex-mcp")
        config.write_bytes(
            (
                '[mcp_servers.agent-sync]\ncommand = "%s"\n'
                'env_vars = ["AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG",'
                ' "AGENT_SESSION"]\n' % legacy
            ).encode()
        )
        self.receive(make_bundle("two"), install=False)
        self.assertEqual(
            tomllib.loads(config.read_text())["mcp_servers"]["simple-chat"]["command"],
            str(self.home / ".local/bin/simple-chat-codex-mcp"),
        )

    def test_a_renamed_wrapper_under_the_old_key_is_migrated_to_the_new_key(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        command = str(self.home / ".local/bin/simple-chat-codex-mcp")
        config.write_bytes(
            (
                '[mcp_servers.agent-sync]\ncommand = "%s"\n'
                'env_vars = ["AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG",'
                ' "AGENT_SESSION"]\n' % command
            ).encode()
        )
        self.receive()
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(list(parsed["mcp_servers"]), ["simple-chat"])
        self.assertEqual(parsed["mcp_servers"]["simple-chat"]["command"], command)

    def test_a_hand_edited_codex_registration_is_still_refused(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_bytes(b'[mcp_servers.agent-sync]\ncommand = "/owner/custom"\n')
        with self.assertRaisesRegex(release.Refused, "existing_agent_sync_mcp_conflict"):
            self.receive()

    def test_non_table_codex_shapes_are_refused_not_an_internal_error(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        for text, code in (
            ('mcp_servers = "x"\n', "invalid_codex_config"),
            ('[mcp_servers]\nagent-sync = "x"\n', "existing_agent_sync_mcp_conflict"),
            ('[mcp_servers]\nsimple-chat = "x"\n', "existing_simple_chat_mcp_conflict"),
        ):
            with self.subTest(text=text):
                config.write_bytes(text.encode())
                with self.assertRaisesRegex(release.Refused, code):
                    self.receive()

    def test_migrate_refuses_non_table_mcp_servers_directly(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_bytes(b'mcp_servers = "x"\n')
        with self.assertRaisesRegex(release.Refused, "invalid_codex_config"):
            release.migrate_codex_mcp(self.home, {})

    def test_update_path_refuses_non_table_shapes(self):
        self.receive()
        config = self.home / ".codex/config.toml"
        for text, code in (
            ('mcp_servers = "x"\n', "invalid_codex_config"),
            ('[mcp_servers]\nagent-sync = "x"\n', "existing_agent_sync_mcp_conflict"),
        ):
            with self.subTest(text=text):
                config.write_bytes(text.encode())
                with self.assertRaisesRegex(release.Refused, code):
                    self.receive(make_bundle("two"), install=False)

    def test_legacy_table_with_extra_key_is_a_conflict_not_a_migration(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        expected = release.codex_mcp_expected(self.home / ".local/bin")
        text = (
            "[mcp_servers.agent-sync]\ncommand = " + json.dumps(expected["command"]) + "\n"
            "env_vars = " + json.dumps(expected["env_vars"]) + "\ntimeout = 30\n"
        ).encode()
        config.write_bytes(text)
        with self.assertRaisesRegex(release.Refused, "existing_agent_sync_mcp_conflict"):
            self.receive()
        self.assertEqual(config.read_bytes(), text)
        config.unlink()
        self.receive()
        config.write_bytes(text)
        with self.assertRaisesRegex(release.Refused, "existing_agent_sync_mcp_conflict"):
            self.receive(make_bundle("two"), install=False)
        self.assertEqual(config.read_bytes(), text)

    def test_update_keeps_configuration_and_wrappers_untouched(self):
        self.receive()
        config = self.home / ".codex/config.toml"
        config.write_bytes(config.read_bytes() + b"\n# owner edit\n")
        before = config.read_bytes()
        wrapper = self.home / ".local/bin/simple-board"
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
        # The first smoke case is a TOOL_REQUIRED script, which smoke skips when the release has no
        # such file (a retained pre-tool release);  materialize it so the error-code mapping runs.
        script = self.home / "scripts" / "simple-chat"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# test double\n")
        for failure, code in (
            (
                subprocess.TimeoutExpired("private command", 12, output=b"private output"),
                "smoke_timeout_simple_chat_version",
            ),
            (
                FileNotFoundError(2, "private executable path"),
                "smoke_start_failed_simple_chat_version_errno_2",
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
        (previous / "scripts/simple-chat").write_text("# syntax valid but altered\n")
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
        record["files"]["scripts/simple-chat"]["sha256"] = "0" * 64
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
        (bin_dir / "simple-board").symlink_to("/bin/echo")
        with self.assertRaisesRegex(release.Refused, "unmanaged_wrapper"):
            self.receive()

    def test_changed_existing_release_is_refused(self):
        self.receive()
        current = release.release_at(self.base, "current")
        (current / "scripts/simple-chat").write_text("# changed")
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
        self.assertFalse((self.home / ".local/bin/simple-board").exists())
        self.assertFalse((self.base / "current").exists())

    def test_mcp_wrapper_refuses_other_seats_and_unassigned_launchers(self):
        code = release.wrapper(
            self.home, "scripts/simple-chat", ["mcp", "--default-seat", "CODEX"], True
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
    a new one that is not breaks the server's `simple-chat mcp` at startup (eavesdropper-tools.json,
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
        # A new bundle must carry it (simple-chat mcp reads it at startup), but retained releases
        # are judged by BASE_REQUIRED, so the ones built before it stay valid rollback targets.
        self.assertIn("scripts/agent_sync/mcp/eavesdropper-tools.json", release.TOOL_REQUIRED)
        self.assertNotIn("scripts/agent_sync/mcp/eavesdropper-tools.json", release.BASE_REQUIRED)

    def test_fleetlink_tools_is_shipped_and_required_of_new_bundles_only(self):
        # The fleetlink_* contract:  simple-chat mcp reads it at startup, so a new bundle must carry it,
        # and an older retained release (built before the stdio FleetLink tools) stays a valid rollback target.
        path = "scripts/agent_sync/mcp/fleetlink-tools.json"
        self.assertIn(path, release.EXACT)
        self.assertTrue(release.allowed_path(path))
        self.assertIn(path, release.TOOL_REQUIRED)
        self.assertNotIn(path, release.BASE_REQUIRED)

    def test_git_tools_are_required_of_new_bundles_only(self):
        # The git tools CLI (entry wrapper + implementation):  a new bundle must carry both, and an
        # older retained release (built before them) stays a valid rollback target.
        for path in ("scripts/simple-git", "scripts/git-tools/git"):
            self.assertIn(path, release.EXACT, path)
            self.assertTrue(release.allowed_path(path), path)
            self.assertIn(path, release.TOOL_REQUIRED, "a new bundle must carry it: " + path)
            self.assertNotIn(
                path,
                release.BASE_REQUIRED,
                "an older retained release stays a valid rollback target: " + path,
            )
            bundle = make_bundle()
            bundle["files"].pop(path)
            bundle["bundle_sha256"] = release.manifest_digest(bundle["files"])
            with self.assertRaisesRegex(release.Refused, "bad_file_set"):
                release.decode_bundle(json.dumps(bundle).encode())

    def test_git_tools_join_the_helper_command_map(self):
        # The seat helper runs for the new command exactly like the existing remote tools.
        self.assertEqual(release.HELPER_COMMANDS["scripts/simple-git"], "simple-git")
        self.assertNotIn("scripts/git-tools/git", release.HELPER_COMMANDS)


if __name__ == "__main__":
    unittest.main()
