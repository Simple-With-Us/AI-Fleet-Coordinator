"""The managed wrappers' registry route, end to end at the installed entry points.

`simple-chat letter registry|show` and `platform-letter registry|show` hand the call to the
reviewed dispatcher when the host holds its config, and behave exactly as before when it does
not.  A present config never degrades to a local store:  custody or validation failures are
the dispatcher's own refusal, and shapes outside the registry routes are never shadowed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # scripts/
SIMPLE_CHAT = ROOT / "simple-chat"
PLATFORM_LETTER = ROOT / "platform-letter"
DISPATCH_PREFIX = "operator registry dispatch:"
IMAGE_ID = "sha256:" + "a1" * 32


class WrapperRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".config" / "fleet-cli").mkdir(parents=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.home / ".config", 0o700)
        os.chmod(self.home / ".config" / "fleet-cli", 0o700)
        self.config_path = self.home / ".config" / "fleet-cli" / "operator-registry-dispatch.json"
        self.env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join([str(Path(self.tmp.name) / "bin"), "/usr/bin", "/bin"]),
            "LC_ALL": "C.UTF-8",
        }
        (Path(self.tmp.name) / "bin").mkdir()  # no docker on PATH:  the route must fail closed

    def _run(self, script, argv):
        return subprocess.run(
            [sys.executable, str(script)] + argv,
            capture_output=True, text=True, env=self.env, timeout=30,
        )

    def _write_config(self, raw=None):
        if raw is None:
            raw = json.dumps({
                "schema": 1,
                "container_name_regex": "^app-l40rxd4r-",
                "image_id": IMAGE_ID,
                "volume_name": "agent-sync-data",
            })
        self.config_path.write_text(raw)
        self.config_path.chmod(0o600)

    # -- configured: the dispatcher owns the exact registry shapes ----------------------------

    def test_simple_chat_letter_registry_routes_to_the_dispatcher(self):
        self._write_config()
        proc = self._run(SIMPLE_CHAT, ["letter", "registry", "register", "--session-id", "x"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(DISPATCH_PREFIX + " docker-unavailable", proc.stdout)

    def test_simple_chat_letter_show_routes_to_the_dispatcher(self):
        self._write_config()
        proc = self._run(SIMPLE_CHAT, ["letter", "show"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(DISPATCH_PREFIX + " docker-unavailable", proc.stdout)

    def test_platform_letter_registry_routes_to_the_dispatcher(self):
        self._write_config()
        proc = self._run(PLATFORM_LETTER, ["registry", "register", "--platform", "codex", "--session-id", "x"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(DISPATCH_PREFIX + " docker-unavailable", proc.stdout)

    def test_platform_letter_show_routes_to_the_dispatcher(self):
        self._write_config()
        proc = self._run(PLATFORM_LETTER, ["show", "--platform", "codex"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(DISPATCH_PREFIX + " docker-unavailable", proc.stdout)

    def test_a_present_but_invalid_config_fails_closed_on_both_wrappers(self):
        self._write_config(raw="{ not json")
        for script, argv in (
            (SIMPLE_CHAT, ["letter", "registry", "register", "--session-id", "x"]),
            (PLATFORM_LETTER, ["registry", "show", "--platform", "codex"]),
        ):
            proc = self._run(script, argv)
            self.assertEqual(proc.returncode, 2, argv)
            self.assertIn(DISPATCH_PREFIX + " config-invalid", proc.stdout, argv)

    def test_other_verbs_are_never_shadowed(self):
        self._write_config()
        proc = self._run(SIMPLE_CHAT, ["letter"])  # no subcommand:  claude_letter's own usage, not the route
        self.assertNotIn(DISPATCH_PREFIX, proc.stdout + proc.stderr)
        proc = self._run(PLATFORM_LETTER, ["frobnicate"])
        self.assertNotIn(DISPATCH_PREFIX, proc.stdout + proc.stderr)

    # -- not configured:  exactly the old behavior ---------------------------------------------

    def test_simple_chat_without_config_behaves_as_before(self):
        proc = self._run(SIMPLE_CHAT, ["letter", "registry", "register", "--session-id", "x"])
        self.assertNotIn(DISPATCH_PREFIX, proc.stdout + proc.stderr)

    def test_platform_letter_without_config_behaves_as_before(self):
        proc = self._run(PLATFORM_LETTER, ["registry", "show", "--platform", "codex"])
        self.assertNotIn(DISPATCH_PREFIX, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
