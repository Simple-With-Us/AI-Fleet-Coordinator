"""operator_registry_dispatch: the host-side registry route into the app container, with a fake docker."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISPATCH = HERE.parent / "server" / "operator_registry_dispatch.py"

IMAGE_ID = "sha256:" + "a1" * 32
PS_ROW = "abc123def456\tapp-l40rxd4r-123456\n"
MOUNTS = json.dumps([{"Destination": "/data", "Name": "agent-sync-data", "Type": "volume"}])
INSPECT_ROW = IMAGE_ID + " " + MOUNTS + "\n"

FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = "ps" ]; then printf '%s' "$FAKE_PS"; exit "${FAKE_PS_RC:-0}"; fi
if [ "$1" = "inspect" ]; then printf '%s\\n' "$FAKE_INSPECT"; exit 0; fi
if [ "$1" = "exec" ]; then
  printf '%s' "$FAKE_EXEC_OUT"; printf '%s' "$FAKE_EXEC_ERR" >&2; exit "${FAKE_EXEC_RC:-0}"
fi
exit 9
"""


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".config" / "fleet-cli").mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "docker.log"
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.config_path = self.home / ".config" / "fleet-cli" / "operator-registry-dispatch.json"
        self._write_config()
        os.chmod(self.home, 0o700)
        os.chmod(self.home / ".config", 0o700)
        os.chmod(self.home / ".config" / "fleet-cli", 0o700)

    def _write_config(self, **overrides):
        config = {
            "schema": 1,
            "container_name_regex": "^app-l40rxd4r-",
            "image_id": IMAGE_ID,
            "volume_name": "agent-sync-data",
        }
        config.update(overrides)
        self.config_path.write_text(json.dumps(config))
        self.config_path.chmod(0o600)

    def _run(self, argv, *, ps=PS_ROW, ps_rc="0", inspect=INSPECT_ROW, exec_rc="0",
             exec_out='{"ok": true}\n', exec_err=""):
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_PS": ps,
            "FAKE_PS_RC": ps_rc,
            "FAKE_INSPECT": inspect,
            "FAKE_EXEC_RC": exec_rc,
            "FAKE_EXEC_OUT": exec_out,
            "FAKE_EXEC_ERR": exec_err,
        })
        proc = subprocess.run(
            [sys.executable, str(DISPATCH)] + argv,
            capture_output=True, text=True, env=env, timeout=30,
        )
        calls = self.log.read_text() if self.log.exists() else ""
        return proc, calls

    def test_happy_path_execs_in_container_as_numeric_uid_and_propagates(self):
        proc, calls = self._run(["letter", "registry", "touch", "--location", "server"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, '{"ok": true}\n')
        exec_lines = [l for l in calls.splitlines() if l.startswith("exec")]
        self.assertEqual(len(exec_lines), 1)
        words = exec_lines[0].split()
        self.assertEqual(words[1:6], ["-i", "-u", "10001", "-w", "/app/scripts"])
        self.assertEqual(words[6], "abc123def456")
        self.assertEqual(words[7:], ["/usr/local/bin/simple-chat", "letter", "registry",
                                     "touch", "--location", "server"])
        self.assertIn("inspect --format {{.Image}} {{json .Mounts}}", calls)

    def test_exit_code_and_stderr_propagate(self):
        proc, _ = self._run(["letter", "registry", "close", "--location", "server"],
                            exec_rc="3", exec_out="", exec_err="no registry\n")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stderr, "no registry\n")

    def test_a_free_text_title_passes_as_one_argument(self):
        title = "plan: --location mac; $(whoami) `id`"
        proc, calls = self._run(["letter", "registry", "title", "--location", "server",
                                 "--title=" + title])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--title=" + title, calls.splitlines()[-1])

    def test_letter_show_is_served(self):
        proc, calls = self._run(["letter", "show", "--location", "server"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("simple-chat letter show", calls.splitlines()[-1])

    def test_the_operator_registry_module_route_is_served_from_the_workdir(self):
        # The caller never names a store:  the dispatcher pins the canonical db itself.
        proc, calls = self._run(["operator-registry", "pending-outbox", "--limit", "50"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = calls.splitlines()[-1]
        self.assertIn("-w /app/scripts", line)
        self.assertIn("python3 -m simple_tools.operator_registry pending-outbox --db "
                      "/data/operator/registry.sqlite3 --limit 50", line)

    def test_a_caller_supplied_db_flag_is_refused_in_every_form(self):
        # Canonical-store pinning:  no caller picks another store, in any argparse form.
        for argv in (
            ["operator-registry", "register", "--db", "/tmp/second-registry.sqlite3"],
            ["operator-registry", "register", "--db=/tmp/second-registry.sqlite3"],
            ["operator-registry", "register", "--d", "/tmp/second-registry.sqlite3"],
            ["operator-registry", "register", "--db", "/data/operator/registry.sqlite3"],
            ["operator-registry", "register", "--db", "/data/operator/registry.sqlite3",
             "--db", "/tmp/second-registry.sqlite3"],
            ["letter", "registry", "register", "--location", "server", "--db", "/tmp/x"],
            ["letter", "show", "--location", "server", "--db=/tmp/x"],
            ["platform-letter", "registry", "register", "--platform", "codex", "--db", "/tmp/x"],
        ):
            proc, calls = self._run(argv)
            self.assertIn("not-a-registry-route", proc.stdout, argv)
            self.assertEqual(proc.returncode, 2, argv)
            self.assertNotIn("exec ", calls, argv)  # never reached the container

    def test_the_module_route_pins_the_canonical_db_after_the_verb(self):
        # Verbs that REQUIRE --db (pending-outbox, mark-exported) get it from the pin.
        for verb in ("pending-outbox", "mark-exported"):
            proc, calls = self._run(["operator-registry", verb] + (["o1"] if verb == "mark-exported" else []))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            exec_lines = [l for l in calls.splitlines()
                          if l.startswith("exec") and (" " + verb + " ") in l]
            self.assertEqual(len(exec_lines), 1, verb)
            words = exec_lines[0].split()
            pos = words.index(verb)
            self.assertEqual(words[pos + 1:pos + 3],
                             ["--db", "/data/operator/registry.sqlite3"], verb)

    def test_every_registry_writer_verb_is_routed(self):
        for verb in ("register", "relabel", "resolve", "update", "close", "sweep", "touch",
                     "history", "pending-outbox", "mark-exported", "wake-target"):
            proc, _ = self._run(["operator-registry", "verb".replace("verb", verb)])
            self.assertNotIn("not-a-registry-route", proc.stdout, verb)


    def test_a_timeout_reports_outcome_unknown_with_honest_retry_class(self):
        # relabel --force double-applies on a blind retry:  it must be read-back-first.
        import contextlib
        import io
        import subprocess as sp
        import unittest.mock as mock
        import simple_tools.server.operator_registry_dispatch as D

        def boom(*a, **k):
            if a[0][1] == "exec":
                raise sp.TimeoutExpired(cmd=a[0], timeout=5.0)

            class R:
                returncode = 0
                stdout = "abc123def456\tapp-l40rxd4r-123456\n" if a[0][1] == "ps" else (
                    "sha256:" + "a1" * 32 + " " + MOUNTS + "\n")
            return R()

        cases = (
            (["operator-registry", "relabel", "--force"], "read-back-first"),
            (["operator-registry", "update", "--title", "x"], "read-back-first"),
            (["operator-registry", "sweep"], "read-back-first"),
            (["operator-registry", "register"], "safe"),
            (["operator-registry", "mark-exported", "o1"], "safe"),
            (["operator-registry", "pending-outbox"], "safe"),
            (["letter", "registry", "touch", "--location", "server"], "safe"),
            (["platform-letter", "registry", "show", "--platform", "codex"], "safe"),
        )
        for argv, retry in cases:
            with mock.patch.object(D.subprocess, "run", side_effect=boom):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = D.main_with_home(argv, str(self.home))
                self.assertEqual(code, 3, argv)
                answer = json.loads(buf.getvalue())
                self.assertEqual(answer["outcome"], "unknown", argv)
                self.assertEqual(answer["retry"], retry, argv)

    def test_an_unknown_module_verb_is_refused(self):
        proc, calls = self._run(["operator-registry", "drop-table"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not-a-registry-route", proc.stdout)
        self.assertNotIn("exec", calls)

    def test_a_malformed_image_id_is_invalid(self):
        self._write_config(image_id="example.invalid/fleet/chat:latest")
        proc, _ = self._run(["letter", "registry", "touch"])
        self.assertIn("config-invalid", proc.stdout)

    def test_a_config_without_volume_name_is_invalid(self):
        config = json.loads(self.config_path.read_text())
        del config["volume_name"]
        self.config_path.write_text(json.dumps(config))
        self.config_path.chmod(0o600)
        proc, _ = self._run(["letter", "registry", "touch"])
        self.assertIn("config-invalid", proc.stdout)

    def test_non_registry_routes_are_refused(self):
        for argv in (["whoami"], ["letter", "inbox"], ["letter", "registry"],
                     ["letter", "registry", "drop"], ["registry", "touch"]):
            proc, calls = self._run(argv)
            self.assertEqual(proc.returncode, 2, argv)
            self.assertIn("not-a-registry-route", proc.stdout)
            self.assertNotIn("exec", calls)

    def test_zero_containers_fail_closed(self):
        proc, _ = self._run(["letter", "registry", "touch"], ps="")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("container-missing", proc.stdout)

    def test_two_containers_fail_closed(self):
        proc, _ = self._run(["letter", "registry", "touch"], ps=PS_ROW + PS_ROW.replace("abc", "zzz"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("container-ambiguous", proc.stdout)

    def test_a_wrong_image_id_fails_closed(self):
        # The reviewer's repro:  a retagged or different image must not pass, whatever ps says.
        proc, _ = self._run(["letter", "registry", "touch"],
                            inspect=INSPECT_ROW.replace(IMAGE_ID, "sha256:" + "b2" * 32))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("image-mismatch", proc.stdout)

    def test_a_bind_mount_is_not_the_canonical_store(self):
        proc, _ = self._run(["letter", "registry", "touch"],
                            inspect=INSPECT_ROW.replace('"volume"', '"bind"'))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("volume-mismatch", proc.stdout)

    def test_a_config_may_not_widen_uid_cli_or_workdir(self):
        for key, value in (("container_user", 1), ("container_user", "agentsync"),
                           ("container_cli", "/bin/sh"), ("container_workdir", "/")):
            self._write_config(**{key: value})
            proc, _ = self._run(["letter", "registry", "touch"])
            self.assertIn("config-invalid", proc.stdout, key)

    def test_a_hardlinked_config_is_refused(self):
        os.link(self.config_path, self.home / ".config" / "fleet-cli" / "hardlink.json")
        proc, _ = self._run(["letter", "registry", "touch"])
        self.assertIn("config-custody", proc.stdout)

    def test_missing_data_volume_fails_closed(self):
        proc, _ = self._run(["letter", "registry", "touch"], inspect=IMAGE_ID + " []\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("volume-mismatch", proc.stdout)

    def test_wrong_volume_name_fails_closed(self):
        proc, _ = self._run(["letter", "registry", "touch"],
                            inspect=INSPECT_ROW.replace("agent-sync-data", "other-data"))
        self.assertIn("volume-mismatch", proc.stdout)

    def test_a_missing_config_fails_closed(self):
        self.config_path.unlink()
        proc, _ = self._run(["letter", "registry", "touch"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("config-", proc.stdout)

    def test_a_world_readable_config_fails_closed(self):
        self.config_path.chmod(0o644)
        proc, _ = self._run(["letter", "registry", "touch"])
        self.assertIn("config-custody", proc.stdout)

    def test_a_symlinked_config_fails_closed(self):
        real = self.home / "real.json"
        real.write_text(self.config_path.read_text())
        self.config_path.unlink()
        self.config_path.symlink_to(real)
        proc, _ = self._run(["letter", "registry", "touch"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("config-", proc.stdout)

    def test_docker_ps_failure_fails_closed(self):
        proc, _ = self._run(["letter", "registry", "touch"], ps_rc="1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("docker-unavailable", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class MainIfConfiguredGateTest(DispatchTest):
    """The managed-wrapper gate:  no config means untouched;  a config means the dispatcher owns the call."""

    def _gate(self, argv, home=None):
        import contextlib
        import io

        from simple_tools.server import operator_registry_dispatch as dispatch

        env = {
            "PATH": str(self.bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_PS": PS_ROW,
            "FAKE_PS_RC": "0",
            "FAKE_INSPECT": INSPECT_ROW,
            "FAKE_EXEC_RC": "0",
            "FAKE_EXEC_OUT": '{"ok": true}\n',
            "FAKE_EXEC_ERR": "",
        }
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out):
                code = dispatch.main_if_configured(argv, home=home or str(self.home))
        return code, out.getvalue()

    def test_no_config_returns_none_and_runs_nothing(self):
        self.config_path.unlink()
        code, out = self._gate(["letter", "registry", "register", "--session-id", "x"])
        self.assertIsNone(code)
        self.assertEqual(out, "")
        self.assertFalse(self.log.exists())

    def test_a_present_config_routes_to_the_dispatcher(self):
        code, out = self._gate(["letter", "registry", "register", "--session-id", "x"])
        self.assertEqual(code, 0)
        self.assertIn('{"ok": true}', out)
        self.assertIn("exec", self.log.read_text())

    def test_a_present_but_invalid_config_fails_closed(self):
        self.config_path.write_text("not json")
        self.config_path.chmod(0o600)
        code, out = self._gate(["letter", "show"])
        self.assertEqual(code, 2)
        self.assertIn("config-invalid", out)
        self.assertFalse(self.log.exists())

    def test_a_non_registry_shape_is_refused_not_passed_through(self):
        code, out = self._gate(["letter", "frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("not-a-registry-route", out)
        # Container resolution runs before the shape check;  the refusal is that no exec happened.
        self.assertNotIn("exec", self.log.read_text())

    def test_a_config_under_an_unreadable_directory_fails_closed(self):
        """lexists would answer False here (it suppresses PermissionError);  the gate must not."""
        protected = self.home / ".config"
        protected.chmod(0o000)
        self.addCleanup(protected.chmod, 0o700)
        code, out = self._gate(["letter", "registry", "register", "--session-id", "x"])
        self.assertIsNotNone(code)
        self.assertNotEqual(code, 0)
        self.assertIn("operator registry dispatch:", out)
