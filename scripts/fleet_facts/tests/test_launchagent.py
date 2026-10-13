"""The LaunchAgent for the publisher, and where it is registered (the watch script and the process list)."""
from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
REPO = SCRIPTS.parent
PLIST = SCRIPTS / "launchd" / "com.jay.mac-facts-publish.plist"
LABEL = "com.jay.mac-facts-publish"
HOME = "/" + "Users" + "/jay"        # the plist is for the owner's Mac;  built here because test data may not hold a literal real path
ISSUE = "AFC" + "-287"                # likewise a real Linear identifier


class LaunchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plist = plistlib.loads(PLIST.read_bytes())

    def test_it_is_a_valid_plist_with_the_label(self) -> None:
        self.assertEqual(self.plist["Label"], LABEL)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("plutil"), "plutil is a macOS tool (another plutil on Linux takes different arguments)")
    def test_plutil_lints_it(self) -> None:
        ran = subprocess.run(["plutil", "-lint", "--", str(PLIST)], capture_output=True, text=True, timeout=30)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_it_runs_at_half_past_every_third_hour_and_not_at_load(self) -> None:
        slots = self.plist["StartCalendarInterval"]
        self.assertIsInstance(slots, list)
        self.assertEqual(sorted((s["Hour"], s["Minute"]) for s in slots), [(h, 30) for h in (0, 3, 6, 9, 12, 15, 18, 21)])
        for slot in slots:
            self.assertEqual(sorted(slot), ["Hour", "Minute"])
        self.assertIs(self.plist["RunAtLoad"], False)
        self.assertNotIn("KeepAlive", self.plist, "scheduled, not always-on")
        self.assertNotIn("StartInterval", self.plist)

    def test_the_hours_read_as_the_brief_says(self) -> None:
        spoken = []
        for slot in sorted(self.plist["StartCalendarInterval"], key=lambda s: s["Hour"]):
            hour = slot["Hour"]
            spoken.append("%d:%02d%s" % (hour % 12 or 12, slot["Minute"], "am" if hour < 12 else "pm"))
        self.assertEqual(spoken, ["12:30am", "3:30am", "6:30am", "9:30am", "12:30pm", "3:30pm", "6:30pm", "9:30pm"])

    def test_it_runs_normal_priority_with_low_io(self) -> None:
        self.assertEqual(self.plist["ProcessType"], "Standard")
        self.assertEqual(self.plist["Nice"], 5)
        self.assertIs(self.plist["LowPriorityIO"], True)

    def test_it_runs_the_dedicated_launcher_that_install_sh_makes(self) -> None:
        self.assertEqual(self.plist["ProgramArguments"], [HOME + "/apps/fleet-facts/mac-facts-publish"])
        install = (SCRIPTS / "fleet_facts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('dest="$HOME/apps/fleet-facts"', install)
        self.assertIn('cp "$here/mac-facts-publish" "$stage/mac-facts-publish"', install)
        launcher = (SCRIPTS / "fleet_facts" / "mac-facts-publish").read_text(encoding="utf-8")
        self.assertIn("publish-mac", launcher)
        self.assertIn("exec ", launcher)

    def test_it_acts_as_claude_and_carries_no_launcher_or_secret(self) -> None:
        env = self.plist["EnvironmentVariables"]
        self.assertEqual(env["AGENT_SEAT"], "CLAUDE")
        self.assertEqual(sorted(env), ["AGENT_SEAT", "HOME", "PATH"])
        self.assertTrue(env["PATH"].startswith(HOME + "/.local/bin:/opt/homebrew/bin"), "gh, infisical and open-loops live there")
        self.assertEqual(env["HOME"], HOME)

    def test_it_logs_to_one_file(self) -> None:
        self.assertEqual(self.plist["StandardOutPath"], HOME + "/Library/Logs/mac-facts-publish.log")
        self.assertEqual(self.plist["StandardErrorPath"], self.plist["StandardOutPath"])

    def test_the_watch_script_keeps_it_loaded_and_expects_the_launcher(self) -> None:
        text = (SCRIPTS / "mac-process-watch.sh").read_text(encoding="utf-8")
        scheduled = re.search(r"expect_scheduled=\((.*?)\n\)", text, re.S).group(1)
        self.assertIn('"%s %s.plist"' % (LABEL, LABEL), scheduled)
        files = re.search(r"expect_files=\((.*?)\n\)", text, re.S).group(1)
        self.assertIn('"${HOME}/apps/fleet-facts/mac-facts-publish"', files)

    def test_the_process_list_has_a_scheduled_row_and_a_changelog_line(self) -> None:
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        scheduled = text.split("## Scheduled (fires, then exits)", 1)[1].split("\n## ", 1)[0]
        (row,) = [line for line in scheduled.splitlines() if line.startswith("| `%s`" % LABEL)]
        self.assertIn("every 3 hours", row)
        self.assertIn("always-on", row.lower())
        for needle in (ISSUE, "mac-process-watch.sh", "FLEET_FACTS_SIGNING_KEY", "FLEET_LOOPS_DATA_ALLOWED_APPS", "FLEET_FACTS_PUBLISH_FULL_COPY", "mac-facts-publish.json",
                       "mac-facts-publish.log", "gb/full-list.md", "gb/mac-facts.json", "fleet-facts verify", "L16c", "Removable Volumes"):
            self.assertIn(needle, row)
        self.assertIn(ISSUE, text.split("## Inventory Changelog & Historical Updates", 1)[1][:600])

    def test_the_helper_row_no_longer_calls_the_launcher_a_stub(self) -> None:
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        (row,) = [line for line in text.splitlines() if line.startswith("| `~/apps/fleet-facts/fleet-facts`")]
        self.assertNotIn("not yet written", row)
        self.assertNotIn("exits 3", row)
        self.assertIn("publish-mac", row)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
