"""The LaunchAgent for the fallback notifier, and where it is registered (the watch script and the process list)."""
from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import unittest

from .world import SCRIPTS

REPO = SCRIPTS.parent
PLIST = SCRIPTS / "launchd" / "com.jay.needs-owner-notify.plist"
LABEL = "com.jay.needs-owner-notify"


class LaunchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plist = plistlib.loads(PLIST.read_bytes())

    def test_it_is_a_valid_plist_with_the_label(self) -> None:
        self.assertEqual(self.plist["Label"], LABEL)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is a macOS tool")
    def test_plutil_lints_it(self) -> None:
        ran = subprocess.run(["plutil", "-lint", str(PLIST)], capture_output=True, text=True, timeout=30)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_it_runs_every_five_minutes_at_normal_priority_and_not_at_load(self) -> None:
        self.assertEqual(self.plist["StartInterval"], 300)
        self.assertIs(self.plist["RunAtLoad"], False)
        self.assertEqual(self.plist["ProcessType"], "Standard")
        self.assertEqual(self.plist["Nice"], 5)
        self.assertNotIn("KeepAlive", self.plist, "scheduled, not always-on")

    def test_it_runs_the_stable_copy_in_isolated_mode_with_a_python_that_can_run_it(self) -> None:
        args = self.plist["ProgramArguments"]
        self.assertEqual(args, ["/opt/homebrew/bin/python3", "-I", "/Users/jay/apps/needs-owner/needs-owner", "notify"])
        install = (SCRIPTS / "needs_owner" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('dest="$HOME/apps/needs-owner"', install, "the plist runs the copy install.sh makes")

    def test_it_acts_as_claude_and_carries_no_launcher_or_secret(self) -> None:
        env = self.plist["EnvironmentVariables"]
        self.assertEqual(env["AGENT_SEAT"], "CLAUDE")
        self.assertEqual(sorted(env), ["AGENT_SEAT", "HOME", "PATH"])
        self.assertTrue(env["PATH"].startswith("/Users/jay/.local/bin:/opt/homebrew/bin"), "board and agent-sync live in ~/.local/bin")
        self.assertEqual(env["HOME"], "/Users/jay")

    def test_it_logs_to_one_file_of_ids(self) -> None:
        self.assertEqual(self.plist["StandardOutPath"], "/Users/jay/Library/Logs/needs-owner-notify.log")
        self.assertEqual(self.plist["StandardErrorPath"], self.plist["StandardOutPath"])

    def test_the_watch_script_keeps_it_loaded_and_expects_the_program(self) -> None:
        text = (SCRIPTS / "mac-process-watch.sh").read_text(encoding="utf-8")
        scheduled = re.search(r"expect_scheduled=\((.*?)\n\)", text, re.S).group(1)
        self.assertIn('"%s %s.plist"' % (LABEL, LABEL), scheduled)
        files = re.search(r"expect_files=\((.*?)\n\)", text, re.S).group(1)
        self.assertIn('"${HOME}/apps/needs-owner/needs-owner"', files)

    def test_the_process_list_has_a_scheduled_row(self) -> None:
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        scheduled = text.split("## Scheduled (fires, then exits)", 1)[1].split("\n## ", 1)[0]
        (row,) = [line for line in scheduled.splitlines() if line.startswith("| `%s`" % LABEL)]
        self.assertIn("every 5 min (300s)", row)
        self.assertIn("always-on", row.lower())
        for needle in ("AFC-286", "mac-process-watch.sh", "needs-owner/state", "pending-CLAUDE.json", "needs-owner-notify.log"):
            self.assertIn(needle, row)
        self.assertIn("Last inventory", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
