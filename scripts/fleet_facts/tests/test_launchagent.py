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
ISSUE = "AFC" + "-285"                # likewise a real Linear identifier


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
        self.assertIn(ISSUE, text.split("## Inventory Changelog & Historical Updates", 1)[1])

    def test_the_changelog_records_the_inventory_that_added_the_row(self) -> None:
        """The launch agent's provenance is in the changelog,  as an inventory entry.

        The window used to be the first 600 characters of the section,  which only
        reached the entry by accident:  it held while the entries above it happened
        to be short,  and every new inventory pass adds one line at the top and
        pushes that entry further out,  so the next inventory by any seat would have
        failed a test about a launch agent.  The record is what matters,  so the
        entry is looked for where it is,  and it has to be an inventory entry like
        every other.
        """
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        changelog = text.split("## Inventory Changelog & Historical Updates", 1)[1]
        # The section ends at the next heading:  a later section's "Last inventory:"
        # line is not part of this changelog and must not be counted here.
        changelog = changelog.split("\n## ", 1)[0]
        entries = [line for line in changelog.splitlines() if line.startswith("Last inventory:")]
        self.assertTrue(entries, "the changelog has no inventory entries")
        matching = [line for line in entries if ISSUE in line]
        self.assertTrue(matching, "the changelog does not record the launch agent's inventory")
        self.assertTrue(
            any(LABEL in line for line in matching),
            "the entry that names the launch agent does not name it",
        )

    def test_the_helper_row_no_longer_calls_the_launcher_a_stub(self) -> None:
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        (row,) = [line for line in text.splitlines() if line.startswith("| `~/apps/fleet-facts/fleet-facts`")]
        self.assertNotIn("not yet written", row)
        self.assertNotIn("exits 3", row)
        self.assertIn("publish-mac", row)


class ZshenvRowTests(unittest.TestCase):
    """The always-on `~/.zshenv` row has to name the order the shell really uses.

    The row is the record of a shell init on the owner's Mac,  so a claim in it
    that the shell does not honour is the defect this class guards against:  the
    binding order has to say which fleet default it departs from and why,  the
    cache has to say what it holds and who reads it,  the identity has to be the
    credential that was measured,  and the durable piece has to name a mechanism
    the repository actually tracks.
    """

    def setUp(self) -> None:
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        (self.row,) = [line for line in text.splitlines() if line.startswith("| `~/.zshenv` (the `GH_TOKEN` refresh)")]
        # The row is prose, so the gap between its sentences is a real U+00A0 plus a
        # space in a converted document and two ASCII spaces in an unconverted one.
        # A check that names a phrase with a gap in it compares on the words, so it
        # does not break when the document is re-rendered in the other form.
        self.flat = re.sub(r"\s+", " ", self.row)

    def test_the_order_names_the_fleet_default_and_this_shell_as_the_exception(self) -> None:
        self.assertIn("A nonempty inherited `GH_TOKEN` wins", self.flat)
        self.assertIn("That is the fleet default", self.flat)
        self.assertIn("this shell is the one documented exception", self.flat)
        self.assertIn("approved by Jay on Oct 21, 2026", self.flat)

    def test_the_sync_row_lists_the_sources_the_script_accepts(self) -> None:
        text = (REPO / "docs" / "MAC-LOCAL-PROCESSES.md").read_text(encoding="utf-8")
        (row,) = [line for line in text.splitlines() if line.startswith("| `scripts/fleet_secrets/sync_cli_login.py`")]
        script = (REPO / "scripts" / "fleet_secrets" / "sync_cli_login.py").read_text(encoding="utf-8")
        for source in ('"env"', '"fleet-secrets"', '"file"'):
            self.assertIn(source, script, "the script no longer accepts this source")
        for source in ("--from env NAME", "--from fleet-secrets --from-path PATH", "--from file PATH"):
            self.assertIn(source, row, "the row does not list a source the script accepts")
        for rejected in ("--from gh", "--from stdin", "--from keychain"):
            self.assertNotIn(rejected, row, "the row lists a source the script rejects")

    def test_the_store_side_is_qualified_and_the_fallbacks_are_named(self) -> None:
        self.assertIn("a nonempty fleet-store value wins whenever the store answers with one", self.flat)
        self.assertIn("An empty store result or an unreachable store leaves the inherited token alone", self.flat)
        self.assertIn("fails closed with `GH_TOKEN is unavailable`", self.flat)

    def test_the_cache_says_what_it_holds_and_who_reads_it(self) -> None:
        self.assertIn("umask 077", self.flat)
        self.assertIn("It holds one value, the store's, copied verbatim", self.flat)
        self.assertIn("this shell is its only reader", self.flat)

    def test_the_identity_is_the_credential_that_was_measured(self) -> None:
        self.assertIn("fine-grained personal access token", self.flat)
        self.assertIn("the `github_pat_` shape", self.flat)
        self.assertNotIn("`gith_`", self.flat)
        self.assertIn("it writes nothing", self.flat)
        self.assertIn("git's helper hands that one to git only when this file has not exported a variable", self.flat)

    def test_the_durable_piece_names_a_mechanism_the_repository_tracks(self) -> None:
        self.assertIn("docs/protocols/cli-login-sync.md", self.flat)
        self.assertIn("scripts/fleet_secrets/sync_cli_login.py", self.flat)
        self.assertIn("`git-relay` is the consumer of those per-seat logins", self.flat)
        self.assertNotIn("Agents do not handle tokens at all", self.flat)

    def test_the_durable_piece_says_where_a_seat_own_login_comes_from(self) -> None:
        self.assertIn("A seat's own login is written from `--from file`", self.flat)
        self.assertIn("it is not a per-seat source", self.flat)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
