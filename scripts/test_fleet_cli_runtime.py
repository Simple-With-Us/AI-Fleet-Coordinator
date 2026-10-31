#!/usr/bin/env python3
"""Offline integration of the real allowlisted runtime, in a disposable HOME.

Run directly: python3 scripts/test_fleet_cli_runtime.py
The receiver bundle is a fixture made from this checkout, not a published-source
or live SSH proof.  MCP protocol tests use the real dispatcher and catalogue with
no authenticated Tools instance; startup separately must refuse missing auth.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUN = subprocess.run

AUDIT = r"""
import os, sys
_home = os.path.realpath(os.environ["HOME"])
def _audit(event, args):
    if event.startswith("socket."):
        raise RuntimeError("offline_network_forbidden")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.path.realpath(os.fsdecode(args[0]))
        if any(part in path.split(os.sep) for part in (".secrets", ".codex", ".claude")):
            if os.path.commonpath([_home, path]) != _home:
                raise RuntimeError("outside_home_credentials_forbidden")
sys.addaudithook(_audit)
"""
BOOT = (
    AUDIT
    + r"""
import runpy
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
)
PROTOCOL = (
    AUDIT
    + r"""
import io
sys.path.insert(0, sys.argv[1])
from agent_sync.mcp.stdio import Server
from agent_sync.mcp.tools import load_served_tools
class CatalogueOnly:
    seat = "CODEX"
    spec = load_served_tools()
    def call(self, *args, **kwargs):
        raise AssertionError("offline_tool_execution_forbidden")
raise SystemExit(Server(CatalogueOnly()).serve(sys.stdin.buffer, sys.stdout.buffer))
"""
)


def env_for(home):
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "AGENT_SEAT": "CODEX"}


def safe_smoke_run(argv, **kwargs):
    # Keep the actual receiver smoke cases and process settings.  Inject only
    # an audit guard before executing the same real script with runpy.
    # lane and fleet-mode are bash scripts (AFC-331);  they run as the receiver runs them, without the
    # Python audit hook, and only ever touch the empty temporary HOME.
    bash = argv[:1] == ["/bin/bash"]
    if not bash and argv[:3] != [sys.executable, "-I", "-B"]:
        raise AssertionError("receiver smoke lost isolated interpreter flags")
    started = time.monotonic()
    skip = 2 if bash else 4
    command = argv if bash else [*argv[:3], "-c", BOOT, *argv[3:]]
    case = {
        "script": Path(argv[skip - 1]).name,
        "args": argv[skip:],
        "executable_exists": Path(argv[0]).is_file(),
        "cwd_exists": Path(kwargs["cwd"]).is_dir(),
        "env_names": sorted(kwargs["env"]),
        "timeout_s": kwargs["timeout"],
    }
    try:
        result = RUN(command, **kwargs)
    except subprocess.TimeoutExpired:
        print(
            json.dumps(
                {
                    "smoke": case,
                    "error": "TimeoutExpired",
                    "elapsed_s": round(time.monotonic() - started, 3),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        raise
    except OSError as exc:
        print(
            json.dumps(
                {
                    "smoke": case,
                    "error": "OSError",
                    "errno": exc.errno,
                    "elapsed_s": round(time.monotonic() - started, 3),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        raise
    print(
        json.dumps(
            {
                "smoke": case,
                "returncode": result.returncode,
                "elapsed_s": round(time.monotonic() - started, 3),
                "audit_refusal": b"offline_network_forbidden" in result.stderr
                or b"outside_home_credentials_forbidden" in result.stderr,
            }
        ),
        file=sys.stderr,
        flush=True,
    )
    return result


class ActualRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.version_info < (3, 11):
            raise unittest.SkipTest("runtime requires Python 3.11+")
        spec = importlib.util.spec_from_file_location(
            "fleet_cli_runtime_receiver", ROOT / "scripts/fleet-cli-release.py"
        )
        cls.receiver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.receiver)
        candidates = set(cls.receiver.EXACT)
        for directory in ("scripts/agent_sync", "scripts/agent_sync/mcp"):
            candidates.update(
                path.relative_to(ROOT).as_posix() for path in (ROOT / directory).glob("*.py")
            )
        files = {}
        for name in sorted(candidates):
            if not cls.receiver.allowed_path(name):
                continue
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise AssertionError("runtime fixture missing/nonregular: " + name)
            data = path.read_bytes()
            files[name] = {
                "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644,
                "sha256": hashlib.sha256(data).hexdigest(),
                "data": base64.b64encode(data).decode(),
            }
        cls.bundle = {
            "schema": 1,
            "source_commit": "a" * 40,
            "bundle_sha256": cls.receiver.manifest_digest(files),
            "files": files,
        }
        cls.raw = json.dumps(cls.bundle, sort_keys=True, separators=(",", ":")).encode()
        cls.temp = tempfile.TemporaryDirectory(prefix="fleet-cli-actual-runtime-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.home = Path(cls.temp.name).resolve() / "home"
        cls.home.mkdir()
        baseline_started = time.monotonic()
        try:
            baseline = RUN(
                [sys.executable, "-I", "-B", "-c", "pass"],
                env=env_for(cls.home),
                cwd=cls.home,
                capture_output=True,
                timeout=12,
            )
        except subprocess.TimeoutExpired:
            print(
                json.dumps(
                    {
                        "baseline": "isolated_python_pass",
                        "error": "TimeoutExpired",
                        "elapsed_s": round(time.monotonic() - baseline_started, 3),
                        "loadavg": os.getloadavg(),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            raise
        print(
            json.dumps(
                {
                    "baseline": "isolated_python_pass",
                    "returncode": baseline.returncode,
                    "elapsed_s": round(time.monotonic() - baseline_started, 3),
                    "loadavg": os.getloadavg(),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        with patch.object(cls.receiver.subprocess, "run", side_effect=safe_smoke_run):
            cls.receipt = cls.receiver.receive(cls.raw, cls.home, install=True)
        cls.release = (cls.home / ".local/share/fleet-cli/current").resolve(strict=True)
        cls.env = env_for(cls.home)

    def command(self, script, args, *, data=None):
        return RUN(
            [sys.executable, "-I", "-B", "-c", BOOT, str(self.release / script), *args],
            env=self.env,
            cwd=self.home,
            input=data,
            capture_output=True,
            timeout=15,
        )

    def assert_exit(self, result, code):
        # Subprocess payloads are never diagnostic output, even on test failure.
        self.assertEqual(result.returncode, code, "unexpected offline child exit status")
        self.assertFalse(
            b"offline_network_forbidden" in result.stderr, "offline network audit refused access"
        )
        self.assertFalse(
            b"outside_home_credentials_forbidden" in result.stderr,
            "offline credential-path audit refused access",
        )

    def test_notes_reader_import_is_in_installed_runtime(self):
        path = "scripts/agent_sync/notes_service_reader.py"
        self.assertIn(path, self.bundle["files"])
        self.assertIn(path, self.receiver.TOOL_REQUIRED)
        self.assertNotIn(path, self.receiver.BASE_REQUIRED)
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            AUDIT + "\nimport sys; sys.path.insert(0, sys.argv[1]); "
            "from agent_sync import notes_client, notes_service_reader; "
            "assert notes_client.SR is notes_service_reader; "
            "assert notes_service_reader.load() is None",
            str(self.release / "scripts"),
        ]
        result = RUN(command, env=self.env, cwd=self.home, capture_output=True, timeout=15)
        self.assert_exit(result, 0)
        self.assertFalse(result.stdout)

    def test_actual_allowlist_installs_and_verifies(self):
        self.assertEqual(self.receipt["status"], "activated")
        self.assertEqual(self.receipt["bundle_sha256"], self.bundle["bundle_sha256"])
        self.assertLessEqual(len(self.raw), self.receiver.MAX_BUNDLE_BYTES)
        self.assertLessEqual(len(self.bundle["files"]), self.receiver.MAX_FILES)
        sender_spec = importlib.util.spec_from_file_location(
            "fleet_cli_runtime_sender", ROOT / "scripts/fleet-cli-sync.py"
        )
        sender = importlib.util.module_from_spec(sender_spec)
        sender_spec.loader.exec_module(sender)
        self.assertEqual(sender.MAX_FILES, self.receiver.MAX_FILES)
        self.assertEqual(self.receiver.MAX_FILES, 145)
        self.assertIn("scripts/agent_sync/mcp/bearer_gate.py", self.bundle["files"])
        # The git tools CLI ships in the bundle:  required of new bundles, never of rollback targets.
        for path in ("scripts/simple-git", "scripts/git-tools/git"):
            self.assertIn(path, self.bundle["files"])
            self.assertIn(path, self.receiver.TOOL_REQUIRED)
            self.assertNotIn(path, self.receiver.BASE_REQUIRED)
        self.assertEqual(self.receiver.HELPER_COMMANDS["scripts/simple-git"], "simple-git")

        status = self.receiver.control(self.home, "status")
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["bundle_sha256"], self.bundle["bundle_sha256"])
        self.assertFalse((self.home / ".secrets").exists())
        with patch.object(self.receiver.subprocess, "run", side_effect=safe_smoke_run):
            again = self.receiver.receive(self.raw, self.home)
        self.assertEqual(again["status"], "unchanged")

    def test_actual_cli_commands(self):
        cases = [
            ("scripts/simple-chat", ["--version"], 0, b"simple-chat"),
            ("scripts/simple-chat", ["mcp", "--help"], 0, b"--default-seat"),
            ("scripts/linear-board/board", ["--help"], 0, b"whoami"),
            ("scripts/linear-board/board", ["whoami"], 4, b""),
            ("scripts/simple-git", ["--help"], 0, b"simple-git"),
            ("scripts/simple-git", ["list"], 4, b""),
        ]
        for script, args, code, text in cases:
            with self.subTest(script=script, args=args):
                result = self.command(script, args)
                self.assert_exit(result, code)
                if text:
                    self.assertTrue(text in result.stdout, "expected CLI help/version field absent")
                else:
                    self.assertTrue(result.stderr)

    def test_actual_recall_lane_and_fleet_mode(self):
        # AFC-331:  the three tools the server mirror gained run from the real release.
        result = self.command("scripts/recall", ["--help"])
        self.assert_exit(result, 0)
        self.assertTrue(b"recall" in result.stdout, "recall help absent")
        for script, args, code, text in [
            ("scripts/fleet_lanes/bin/lane", ["--help"], 0, b"lane"),
            ("scripts/fleet-mode", [], 0, b"Fleet mode"),
        ]:
            with self.subTest(script=script):
                result = RUN(
                    ["/bin/bash", str(self.release / script), *args],
                    env=self.env,
                    cwd=self.home,
                    capture_output=True,
                    timeout=15,
                )
                self.assert_exit(result, code)
                self.assertTrue(text in result.stdout, "expected output absent")
        result = RUN(
            ["/bin/bash", str(self.release / "scripts/fleet-mode"), "--full"],
            env=self.env,
            cwd=self.home,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.stdout, (self.release / "docs/FLEET-MODE.md").read_bytes())
        # Running the tools must leave the immutable release exactly as installed (no __pycache__).
        self.assertEqual(list(self.release.rglob("__pycache__")), [])
        self.assertEqual(self.receiver.control(self.home, "status")["status"], "ready")

    def test_recall_ships_no_lazy_mac_only_modules(self):
        self.assertFalse((self.release / "scripts/fleet_rag/ingest.py").exists())
        self.assertFalse((self.release / "scripts/fleet_rag/eval.py").exists())
        self.assertFalse((self.release / "scripts/fleet_lanes/install_tools.py").exists())

    def test_actual_mcp_startup_refuses_missing_credentials(self):
        result = self.command(
            "scripts/simple-chat", ["mcp", "--default-seat", "CODEX"], data=self.requests()
        )
        self.assert_exit(result, 3)
        self.assertFalse(result.stdout, "unauthenticated MCP emitted protocol output")
        # Own-bearer proof comes before provider credentials or any protocol output.
        self.assertIn(b"CODEX's own bearer was not accepted (not_configured)", result.stderr)
        self.assertNotIn(b"cannot read Infisical", result.stderr)

    @staticmethod
    def requests():
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "offline-runtime-fixture", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        return b"".join(json.dumps(item).encode() + b"\n" for item in requests)

    def test_real_dispatcher_and_catalogue_protocol_without_auth(self):
        result = RUN(
            [sys.executable, "-I", "-B", "-c", PROTOCOL, str(self.release / "scripts")],
            env=self.env,
            cwd=self.home,
            input=self.requests(),
            capture_output=True,
            timeout=15,
        )
        self.assert_exit(result, 0)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([item["id"] for item in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "simple-chat")
        # What the real server serves:  tools.json, eavesdropper-tools.json (AFC-330), notes-tools.json and
        # recall-tools.json (AFC-331), then the linear_* tools built from the board catalogs, then
        # fleetlink-tools.json.  Every file must ship, or `simple-chat mcp` fails at startup on the server (it
        # did, 2026-10-16).
        mcp = self.release / "scripts/agent_sync/mcp"
        served = []
        for name in (
            "tools.json",
            "eavesdropper-tools.json",
            "notes-tools.json",
            "recall-tools.json",
        ):
            served += json.loads((mcp / name).read_bytes())["tools"]
        self.assertTrue(served)
        listed = replies[1]["result"]["tools"]
        self.assertEqual(listed[: len(served)], served)
        catalog = self.release / "scripts/linear-board/catalog"
        linear = (
            [t["name"] for t in json.loads((catalog / "core-tools.json").read_bytes())["tools"]]
            + [
                c["name"]
                for c in json.loads((catalog / "named-commands.json").read_bytes())["commands"]
            ]
            + [
                t["name"]
                for t in json.loads((catalog / "official-mcp-tools.json").read_bytes())["tools"]
            ]
        )
        self.assertEqual(
            [t["name"] for t in listed[len(served) : len(served) + len(linear)]], linear
        )
        fleetlink = json.loads((mcp / "fleetlink-tools.json").read_bytes())["tools"]
        self.assertTrue(fleetlink)
        self.assertEqual(listed[len(served) + len(linear) :], fleetlink)

    def test_generated_wrapper_isolation_flags(self):
        for name in (
            "simple-chat",
            "simple-notes",
            "simple-board",
            "fleet-cli-release",
            "simple-chat-codex-mcp",
            "recall",
        ):
            with self.subTest(wrapper=name):
                content = (self.home / ".local/bin" / name).read_text()
                self.assertTrue(content.startswith("#!/usr/bin/python3 -I\n"))
                self.assertIn("['/usr/bin/python3', '-I', '-B',", content)
        for name in ("lane", "fleet-mode"):
            with self.subTest(wrapper=name):
                content = (self.home / ".local/bin" / name).read_text()
                self.assertTrue(content.startswith("#!/usr/bin/python3 -I\n"))
                self.assertIn("argv = ['/bin/bash', str(release /", content)

    def test_generated_wrappers_with_pinned_interpreter(self):
        # The immutable wrapper pins Coolify's Linux interpreter.  A Mac system
        # Python may be an older Apple developer-tools launcher; it is not the
        # deployment interpreter and cannot establish Linux wrapper readiness.
        if not sys.platform.startswith("linux"):
            self.skipTest(
                "pinned deployment wrapper execution requires Linux; runtime and flags checked separately"
            )
        interpreter = Path("/usr/bin/python3")
        if not interpreter.is_file():
            self.skipTest("deployment interpreter /usr/bin/python3 unavailable on this test host")
        version = RUN(
            [
                str(interpreter),
                "-I",
                "-B",
                "-c",
                "import sys;print(sys.version_info.major,sys.version_info.minor)",
            ],
            env=self.env,
            cwd=self.home,
            capture_output=True,
            timeout=5,
        )
        if version.returncode or tuple(map(int, version.stdout.split())) < (3, 11):
            self.skipTest(
                "deployment interpreter requires Python 3.11+; host /usr/bin/python3 is older"
            )
        for name, args, expected in [
            ("simple-chat", ["--version"], 0),
            ("simple-notes", ["--version"], 0),
            ("simple-board", ["--help"], 0),
            ("simple-board", ["whoami"], 4),
            ("simple-board", ["commands", "--kind", "read"], 0),
            ("simple-board", ["mcp", "list"], 0),
            ("fleet-cli-release", ["status"], 0),
            ("simple-chat-codex-mcp", [], 3),
            ("recall", ["--help"], 0),
            ("lane", ["--help"], 0),
            ("fleet-mode", [], 0),
        ]:
            with self.subTest(wrapper=name, args=args):
                wrapper = self.home / ".local/bin" / name
                result = RUN(
                    [str(wrapper), *args],
                    env=self.env,
                    cwd=self.home,
                    input=self.requests(),
                    capture_output=True,
                    timeout=15,
                )
                self.assert_exit(result, expected)
        result = RUN(
            [str(self.home / ".local/bin/simple-chat-codex-mcp")],
            env={**self.env, "AGENT_SEAT": "ASTRA"},
            cwd=self.home,
            input=self.requests(),
            capture_output=True,
            timeout=15,
        )
        self.assert_exit(result, 3)
        self.assertTrue(b"CODEX-only" in result.stderr, "seat guard diagnostic absent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", type=Path)
    options, rest = parser.parse_known_args()
    if options.repo_root:
        ROOT = options.repo_root.resolve()
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
