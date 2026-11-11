"""The scheduled archived-chat letter sweep (AFC-694):  the LaunchAgent template, its wrapper script,
the installer's rendering, the server timer units and the Muster control allowlist row.  A stub
`simple-chat` stands in for the real CLI, so nothing touches a registry.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_archive_sweep_schedule -v
"""

import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
LABEL = "com.jay.operator-archive-sweep"
WRAPPER = SCRIPTS / "operator-archive-sweep.sh"
INSTALLER = SCRIPTS / "launchd" / "install-operator-archive-sweep.sh"
SERVER = SCRIPTS / "simple_tools" / "server"


class PlistTests(unittest.TestCase):
    def setUp(self):
        with open(SCRIPTS / "launchd" / (LABEL + ".plist"), "rb") as handle:
            self.data = plistlib.load(handle)

    def test_label_interval_and_script(self):
        self.assertEqual(self.data["Label"], LABEL)
        self.assertEqual(self.data["StartInterval"], 3600)
        args = self.data["ProgramArguments"]
        self.assertEqual(args[0], "/bin/sh")
        self.assertTrue(args[1].endswith("/simple-tools-runtime/scripts/operator-archive-sweep.sh"))
        self.assertTrue(WRAPPER.is_file())

    def test_inert_by_default_and_no_secret(self):
        env = self.data["EnvironmentVariables"]
        self.assertEqual(env["OPERATOR_ARCHIVE_SWEEP_APPLY"], "0")
        self.assertEqual(env["OPERATOR_ARCHIVE_SWEEP_MAX"], "25")
        self.assertFalse([k for k in env if "KEY" in k or "TOKEN" in k or "SECRET" in k])

    def test_label_is_in_the_control_allowlist(self):
        config = json.loads((SCRIPTS / "fleet-ops-control.json").read_text())
        self.assertIn(LABEL, [row["label"] for row in config["mac"]["agents"]])

    def test_label_does_not_take_the_installed_stale_sweep_name(self):
        stale = "com.jay.operator-letter-sweep"
        self.assertNotEqual(self.data["Label"], stale)
        self.assertNotEqual(self.data["Label"] + ".plist", stale + ".plist")
        self.assertIn("LABEL=" + LABEL + "\n", INSTALLER.read_text())
        self.assertNotIn(stale, INSTALLER.read_text().replace("# ", ""))
        config = json.loads((SCRIPTS / "fleet-ops-control.json").read_text())
        self.assertEqual([r for r in config["mac"]["agents"] if r["label"] == stale], [])


class InstallerTests(unittest.TestCase):
    def render(self, *flags):
        out = subprocess.run(
            ["/bin/sh", str(INSTALLER), "--print", *flags], capture_output=True, text=True, check=True
        ).stdout
        return plistlib.loads(out.encode())

    def test_plain_render_is_dry_run_only(self):
        self.assertEqual(self.render()["EnvironmentVariables"]["OPERATOR_ARCHIVE_SWEEP_APPLY"], "0")

    def test_apply_flag_turns_the_apply_pass_on(self):
        data = self.render("--apply")
        self.assertEqual(data["EnvironmentVariables"]["OPERATOR_ARCHIVE_SWEEP_APPLY"], "1")
        self.assertEqual(data["EnvironmentVariables"]["OPERATOR_ARCHIVE_SWEEP_MAX"], "25")


class WrapperTests(unittest.TestCase):
    def run_wrapper(self, dry_output, dry_code=0, apply_output="", apply_code=0, **env):
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            stub = Path(tmp, "simple-chat")
            stub.write_text(
                "#!/bin/sh\n"
                'echo "$*" >> "%s"\n'
                'case "$*" in\n'
                "  *--apply*) printf '%%s\\n' '%s'; exit %d ;;\n"
                "  *) printf '%%s\\n' '%s'; exit %d ;;\n"
                "esac\n" % (calls, apply_output, apply_code, dry_output, dry_code)
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            log = Path(tmp, "state", "sweep.log")
            full = {
                "PATH": os.environ["PATH"],
                "HOME": tmp,
                "SIMPLE_CHAT_BIN": str(stub),
                "OPERATOR_ARCHIVE_SWEEP_LOG": str(log),
                "AGENT_SEAT": "ZC",
            }
            full.update(env)
            proc = subprocess.run(["/bin/sh", str(WRAPPER)], env=full, capture_output=True, text=True)
            made = calls.read_text().splitlines() if calls.exists() else []
            return proc.returncode, made, log.read_text() if log.exists() else ""

    DRY = "would close A (1a2b3c4d, mac)\nwould close B (5e6f7a8b, mac)\narchive sweep:  8 open, 2 archived"

    def test_default_is_a_logged_dry_run_only(self):
        code, calls, log = self.run_wrapper(self.DRY)
        self.assertEqual((code, calls), (0, ["letter sweep-archived"]))
        self.assertIn("would close A", log)
        self.assertIn("dry run only", log)

    def test_apply_runs_after_the_dry_run(self):
        code, calls, log = self.run_wrapper(
            self.DRY, apply_output="closed A (1a2b3c4d, mac)", OPERATOR_ARCHIVE_SWEEP_APPLY="1"
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls, ["letter sweep-archived", "letter sweep-archived", "letter sweep-archived --apply"]
        )
        self.assertIn("closed A", log)

    def test_cap_is_rechecked_on_a_fresh_count_before_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp, "n")
            stub = Path(tmp, "simple-chat")
            stub.write_text(
                "#!/bin/sh\n"
                'case "$*" in *--apply*) echo applied >> "%s.applied"; exit 0 ;; esac\n'
                'n=$(cat "%s" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "%s"\n'
                'if [ "$n" -eq 1 ]; then echo "would close A (1a2b3c4d, mac)"; '
                'else echo "would close A (1a2b3c4d, mac)"; echo "would close B (5e6f7a8b, mac)"; fi\n'
                % (counter, counter, counter)
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            log = Path(tmp, "state", "sweep.log")
            env = {
                "PATH": os.environ["PATH"], "HOME": tmp, "SIMPLE_CHAT_BIN": str(stub),
                "OPERATOR_ARCHIVE_SWEEP_LOG": str(log), "OPERATOR_ARCHIVE_SWEEP_APPLY": "1",
                "OPERATOR_ARCHIVE_SWEEP_MAX": "1",
            }
            proc = subprocess.run(["/bin/sh", str(WRAPPER)], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 4)
            self.assertFalse(Path(tmp, "n.applied").exists())
            self.assertIn("re-check names 2 chats", log.read_text())

    def test_cap_refuses_a_runaway_count(self):
        code, calls, log = self.run_wrapper(
            self.DRY, OPERATOR_ARCHIVE_SWEEP_APPLY="1", OPERATOR_ARCHIVE_SWEEP_MAX="1"
        )
        self.assertEqual((code, calls), (4, ["letter sweep-archived"]))
        self.assertIn("refusing to apply", log)

    def test_nothing_archived_skips_the_apply_call(self):
        code, calls, _ = self.run_wrapper(
            "archive sweep:  8 open, 0 archived", OPERATOR_ARCHIVE_SWEEP_APPLY="1"
        )
        self.assertEqual((code, calls), (0, ["letter sweep-archived"]))

    def test_dry_run_failure_is_the_exit_code_and_blocks_apply(self):
        code, calls, log = self.run_wrapper(
            "archive sweep:  no Operator registry on this host", dry_code=3, OPERATOR_ARCHIVE_SWEEP_APPLY="1"
        )
        self.assertEqual((code, calls), (3, ["letter sweep-archived"]))
        self.assertIn("dry run failed with exit 3", log)

    def test_apply_failure_is_the_exit_code(self):
        code, _, _ = self.run_wrapper(self.DRY, apply_code=1, OPERATOR_ARCHIVE_SWEEP_APPLY="1")
        self.assertEqual(code, 1)

    def test_a_non_numeric_cap_falls_back_to_the_default(self):
        code, calls, _ = self.run_wrapper(
            self.DRY, OPERATOR_ARCHIVE_SWEEP_APPLY="1", OPERATOR_ARCHIVE_SWEEP_MAX="lots"
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)


class ServerUnitTests(unittest.TestCase):
    def read(self, name):
        return (SERVER / name).read_text()

    def exec_line(self, text):
        return [l for l in text.splitlines() if l.startswith("ExecStart=")][0]

    def test_zcode_sweep_stays_dry_run(self):
        line = self.exec_line(self.read("afc-zc-letter-sweep.service"))
        self.assertIn("simple_tools.platform_letter_zcode sweep", line)
        self.assertNotIn("--apply", line)

    def test_antigravity_sweep_stays_dry_run(self):
        line = self.exec_line(self.read("afc-ag-letter-sweep.service"))
        self.assertIn("simple_tools.platform_letter_antigravity sweep", line)
        self.assertNotIn("--apply", line)

    def test_services_are_oneshot_and_timers_point_at_them(self):
        for short in ("zc", "ag"):
            service = self.read("afc-%s-letter-sweep.service" % short)
            timer = self.read("afc-%s-letter-sweep.timer" % short)
            self.assertIn("Type=oneshot", service)
            self.assertIn("operator-registry-env", service)
            self.assertIn("Unit=afc-%s-letter-sweep.service" % short, timer)
            self.assertIn("OnUnitInactiveSec=1h", timer)
            self.assertNotRegex(service + timer, r"(?i)(key|token)\s*=\s*\S{12,}")


if __name__ == "__main__":
    unittest.main()
