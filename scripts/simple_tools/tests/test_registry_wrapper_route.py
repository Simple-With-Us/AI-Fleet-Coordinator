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
        (Path(self.tmp.name) / "bin").mkdir()
        # A fake docker shadows any real one (this bin is first on PATH):  the default
        # answers like a host whose docker cannot serve, so "docker unavailable" is
        # deterministic whether or not the test machine has docker installed.  Tests
        # that need another answer replace it through _fake_docker;  every call the
        # route makes is logged, so a test can prove the route never ran an arbitrary
        # docker execution.
        self.docker_log = self._fake_docker("unavailable")

    def _fake_docker(self, behavior):
        log = Path(self.tmp.name) / "docker.log"
        if log.exists():
            log.unlink()
        record = "printf '%s\\n' \"$*\" >> '" + str(log) + "'\n"
        if behavior == "unavailable":
            body = "#!/bin/sh\n" + record + "exit 1\n"
        elif behavior == "no-container":
            # docker serves, but no managed container runs:  ps answers empty success,
            # anything else fails (and would show up in the log).
            body = (
                "#!/bin/sh\n" + record
                + "if [ \"$1\" = ps ]; then exit 0; fi\nexit 1\n"
            )
        else:
            raise AssertionError("unknown fake docker behavior: " + behavior)
        docker = Path(self.tmp.name) / "bin" / "docker"
        docker.write_text(body)
        docker.chmod(0o755)
        return log

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

    def test_docker_present_but_container_absent_refuses_container_missing(self):
        self.docker_log = self._fake_docker("no-container")
        self._write_config()
        for script, argv in (
            (SIMPLE_CHAT, ["letter", "registry", "register", "--session-id", "x"]),
            (SIMPLE_CHAT, ["letter", "show"]),
            (PLATFORM_LETTER, ["registry", "register", "--platform", "codex", "--session-id", "x"]),
            (PLATFORM_LETTER, ["show", "--platform", "codex"]),
            (SIMPLE_CHAT, ["operator-registry", "sweep", "--platform", "minimax"]),
        ):
            proc = self._run(script, argv)
            self.assertEqual(proc.returncode, 2, (script, argv, proc.stdout))
            self.assertIn(DISPATCH_PREFIX + " container-missing", proc.stdout, (script, argv))
        calls = self.docker_log.read_text().splitlines()
        self.assertTrue(calls, "the route never asked docker for the container")
        # Only ps lookups:  a missing container is refused before any inspect or exec,
        # so the route never ran an arbitrary docker execution.
        self.assertTrue(all(line.startswith("ps ") for line in calls), calls)

    def test_a_present_but_invalid_config_fails_closed_on_both_wrappers(self):
        self._write_config(raw="{ not json")
        for script, argv in (
            (SIMPLE_CHAT, ["letter", "registry", "register", "--session-id", "x"]),
            (PLATFORM_LETTER, ["registry", "show", "--platform", "codex"]),
        ):
            proc = self._run(script, argv)
            self.assertEqual(proc.returncode, 2, argv)
            self.assertIn(DISPATCH_PREFIX + " config-invalid", proc.stdout, argv)

    def test_simple_chat_operator_registry_route_when_configured(self):
        self._write_config()
        proc = self._run(SIMPLE_CHAT, ["operator-registry", "sweep", "--platform", "minimax"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(DISPATCH_PREFIX + " docker-unavailable", proc.stdout)

    def test_simple_chat_operator_registry_without_config_behaves_as_before(self):
        proc = self._run(SIMPLE_CHAT, ["operator-registry", "sweep", "--platform", "minimax"])
        self.assertNotIn(DISPATCH_PREFIX, proc.stdout + proc.stderr)

    def test_simple_chat_operator_registry_rejects_a_caller_supplied_db(self):
        # The --db refusal itself is covered at dispatcher level (refused in every form, before
        # any exec);  at wrapper level the call still reaches the dispatcher, never a local store.
        self._write_config()
        proc = self._run(SIMPLE_CHAT, ["operator-registry", "sweep", "--db", "/tmp/other.sqlite3"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(DISPATCH_PREFIX, proc.stdout)

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
