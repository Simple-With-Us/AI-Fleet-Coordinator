"""Hermetic test of scripts/telemetry/configure-agents.sh (stub Infisical, temp HOME)."""
from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from relay_common import CONFIGURE, PRODUCERS


def _token() -> str:
    return "relay-" + secrets.token_hex(16)


class ConfigureAgentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-relay-cfg-"))
        self.home = self.tmp / "home"
        self.out = self.tmp / "out"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.tokens = {
            "RELAY_BASE_URL": "https://relay.example.test",
            "RELAY_BEARER_CLAUDE_CODE": _token(),
            "RELAY_BEARER_CODEX": _token(),
            "RELAY_BEARER_GROK": _token(),
            "RELAY_BEARER_SHIM": _token(),
        }
        export = self.tmp / "export.json"
        export.write_text(json.dumps(self.tokens), encoding="utf-8")
        stub = self.bin / "infisical"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%%s\\n' \"$*\" >> \"%s\"\n"
            "cat \"%s\"\n" % (self.tmp / "infisical.args", export),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": "%s:%s" % (self.bin, os.environ.get("PATH", "")),
            "HOME": str(self.home),
            "INFISICAL_PROJECT_ID": "00000000-0000-0000-0000-000000000000",
            "OUT_DIR": str(self.out),
            "RELAY_URL": "",
        }

    def tearDown(self) -> None:
        # Temp tree only; never touch the real HOME.
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        self.tmp.rmdir()

    def _run(self, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if extra:
            env.update(extra)
        return subprocess.run(
            ["bash", str(CONFIGURE), *args],
            cwd=str(self.tmp),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_writes_snippets_and_prints_no_secrets(self) -> None:
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        for name, value in self.tokens.items():
            if name == "RELAY_BASE_URL":
                continue
            self.assertNotIn(value, combined, name)
            self.assertIn("%s=set" % name, proc.stdout)
        self.assertTrue((self.out / "claude-settings.json").is_file())
        self.assertTrue((self.out / "codex-config-otel.toml").is_file())
        self.assertTrue((self.out / "grok-telemetry.json").is_file())
        self.assertTrue((self.out / "README.txt").is_file())
        for path in self.out.iterdir():
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, path.name)
        # Nothing landed in the fake HOME (the script must not target a Mac).
        leftover = [p for p in self.home.rglob("*") if p != self.home]
        self.assertEqual(leftover, [])

    def test_snippets_point_at_the_relay_ports_and_keep_prompts(self) -> None:
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        claude = json.loads((self.out / "claude-settings.json").read_text(encoding="utf-8"))
        env = claude["env"]
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://relay.example.test:4318")
        self.assertEqual(env["OTEL_LOG_USER_PROMPTS"], "1")
        self.assertEqual(env["OTEL_LOG_TOOL_DETAILS"], "1")
        self.assertEqual(env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"], "1")
        self.assertIn("signal=agent", env["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertIn(self.tokens["RELAY_BEARER_CLAUDE_CODE"], env["OTEL_EXPORTER_OTLP_HEADERS"])
        codex = (self.out / "codex-config-otel.toml").read_text(encoding="utf-8")
        self.assertIn("https://relay.example.test:4319", codex)
        self.assertIn("log_user_prompt = true", codex)
        grok = json.loads((self.out / "grok-telemetry.json").read_text(encoding="utf-8"))
        self.assertEqual(grok["telemetry"]["otel_endpoint"], "https://relay.example.test:4320")
        self.assertTrue(grok["telemetry"]["otel_log_user_prompts"])

    def test_infisical_call_is_prod_only(self) -> None:
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = (self.tmp / "infisical.args").read_text(encoding="utf-8")
        self.assertIn("--env=prod", args)
        self.assertNotIn("--env=dev", args)
        self.assertNotIn("--env=staging", args)

    def test_empty_bearer_fails_closed(self) -> None:
        broken = dict(self.tokens)
        broken["RELAY_BEARER_CODEX"] = ""
        (self.tmp / "export.json").write_text(json.dumps(broken), encoding="utf-8")
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELAY_BEARER_CODEX=empty", proc.stdout + proc.stderr)
        self.assertFalse((self.out / "claude-settings.json").exists())

    def test_script_has_no_jay_mac_path_and_no_home_writes(self) -> None:
        src = CONFIGURE.read_text(encoding="utf-8")
        self.assertNotIn("/Users/jay", src)
        self.assertIn("SHIPPED ONLY", src)
        # The only writes are OUT_DIR and a temp report; $HOME is never a destination.
        self.assertNotIn('>"$HOME', src.replace(" ", ""))
        self.assertNotIn(">>$HOME", src.replace(" ", ""))
        self.assertNotRegex(src, r"(?m)^\s*(cp|mv|install)\s+.*~")

    def test_producer_ports_match_the_relay_contract(self) -> None:
        src = CONFIGURE.read_text(encoding="utf-8")
        self.assertIn(":4318", src)
        self.assertIn(":4319", src)
        self.assertIn(":4320", src)
        self.assertIn("4321", src)
        self.assertEqual(PRODUCERS["claude"]["port"], 4318)


if __name__ == "__main__":
    unittest.main()
