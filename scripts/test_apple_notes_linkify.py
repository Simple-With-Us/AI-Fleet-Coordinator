"""AFC-547:  apple-notes-coding.sh turns bare project and pull request references into links.

Owner 2026-10-25:  links everywhere, Apple Notes included, in the house linkifier style, and the helper does the
converting so every seat gets it without writing it.

The helper already owns the note's other automatic rewrites, the fleet dates and the sentence gaps, so linkification
belongs to the same place and the same shape:  a rewrite the writer does, counted, logged and reported on stderr.
What is pinned here:

  a Linear issue identifier (OPS-1000) becomes a link to that issue in the fleet workspace;
  a pull request reference (PR #957) becomes a link to the pull request, and the text is normalised to the house form;
  a qualified reference (AFC#957) links to the pull request it names in the repository it names;
  a list of pull requests (PRs #380, #386 and #389) becomes one link per number, and a file count that follows a
  list is not one of them;
  the repository a pull request belongs to is read from the block it sits in:  a repository named next to it, the
  subject of a table row, the nearest repository named near it, the team key in the note title, and the fleet
  repository as the fallback;
  a block that names several repositories, and a reference that names none of them, is left as plain text, because a
  link to the wrong repository is worse than no link;
  an identifier that is not a Linear issue (SRV-S07) is left alone;
  code, a fenced block, a hand-written link and an identifier inside a URL are left exactly as they were;
  NOTES_LINKIFY=0 turns the whole step off;
  the count of conversions reaches the action log, and it reaches it even when the date counts are missing;
  a network error that only looks like a 404 is not a missing pull request:  the repository of the team is the answer.

The script runs with `osascript`, `shortcuts`, `open`, `pgrep` and `sleep` stubbed on PATH, as in
test_apple_notes_actions.py, so nothing reaches Notes.  The link assertions use the preview hook
(APPLE_NOTES_PRINT_HTML=1), which prints the HTML the note would get and writes nothing and logs nothing.

Run:  cd scripts && python3 -m unittest test_apple_notes_linkify
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_apple_notes_actions import (  # noqa: E402  (the stubs live with the AFC-486 test)
    NOT_FOUND,
    NOTES,
    OPEN_STUB,
    OSASCRIPT_STUB,
    PGREP_STUB,
    SHORTCUTS_STUB,
    SLEEP_STUB,
)

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"
SESSION = "test-session-1234"
LINEAR = "https://linear.app/simple-with-us/issue/"

#  A stand-in for the gh CLI, which the helper asks which repository holds a pull request.  It says
#  every repository holds every pull request unless GH_STUB_MISSING names one that does not, or is
#  the star for all of them.  GH_STUB_ERROR makes it fail the way a broken network does, which is
#  not the same thing as a pull request that is not there.
GH_STUB = """#!/bin/sh
url=""
for arg in "$@"; do
  case "$arg" in
    repos/*) url="$arg" ;;
  esac
done
if [ -n "${GH_STUB_ERROR:-}" ]; then
  printf '%s\\n' "$GH_STUB_ERROR" >&2
  exit 1
fi
if [ "${GH_STUB_MISSING:-}" = "*" ]; then
  echo "gh: Not Found (HTTP 404)" >&2
  exit 1
fi
case ",${GH_STUB_MISSING:-}," in
  *",${url},"*)
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
esac
exit 0
"""

#  A stand-in for mktemp that cannot make the file the date counts are written to.  The helper then
#  reads no date counts at all, and the action log has to keep the link counts anyway.
MTEMP_STUB = """#!/bin/sh
case "$1" in
  *changes*) exit 1 ;;
esac
for p in /usr/bin/mktemp /bin/mktemp /usr/local/bin/mktemp; do
  [ -x "$p" ] && exec "$p" "$@"
done
echo "mktemp: no usable copy" >&2
exit 1
"""


class LinkifyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir(mode=0o700)
        for name, body in (
            ("osascript", OSASCRIPT_STUB.format(python=sys.executable, not_found=NOT_FOUND)),
            ("shortcuts", SHORTCUTS_STUB),
            ("pgrep", PGREP_STUB),
            ("open", OPEN_STUB),
            ("sleep", SLEEP_STUB),
            ("gh", GH_STUB),
        ):
            stub = self.bindir / name
            stub.write_text(body)
            stub.chmod(0o700)
        self.osa = self.tmp / "osa"
        self.osa.mkdir(mode=0o700)
        self.runs = self.tmp / "shortcut-runs"
        self.notes_file = self.tmp / "notes.json"
        self.notes_file.write_text(json.dumps(NOTES))
        self.log = self.tmp / "logs" / "actions.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_script(
        self, *args: str, body: str | None = None, **env: str
    ) -> subprocess.CompletedProcess:
        child = {
            "PATH": f"{self.bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.tmp),
            "LANG": "en_US.UTF-8",
            "AGENT_SEAT": "CLAUDE",
            "STUB_OSA": str(self.osa),
            "STUB_NOTES": str(self.notes_file),
            "STUB_RUNS": str(self.runs),
            "NOTES_PIN_LOCK_DIR": str(self.tmp / "pin.lock"),
            "NOTES_PIN_LOCK_WAIT_S": "1",
            "NOTES_SHORTCUT_TIMEOUT_S": "5",
            "APPLE_NOTES_PIN_RETRY": "0",
            "APPLE_NOTES_ACTION_LOG": str(self.log),
        }
        child.update(env)
        for key in [key for key, value in child.items() if value is None]:
            del child[key]
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            input=body if body is not None else "",
            capture_output=True,
            text=True,
            env=child,
            cwd=str(self.tmp),
            timeout=60,
            check=False,
        )

    def html(self, title: str, body: str, **env: str) -> str:
        """The HTML the note would get, from the preview hook:  no Notes, no log, no side effect."""
        p = self.run_script("--session", SESSION, title, body, APPLE_NOTES_PRINT_HTML="1", **env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def log_lines(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


class LinkifyTests(LinkifyTestCase):
    def test_a_linear_issue_identifier_becomes_a_link_to_the_issue(self):
        out = self.html("[AFC, MM] Notes", "Filed as OPS-1000 and tracked under AFC-547.")
        self.assertIn(f'<a href="{LINEAR}OPS-1000">OPS-1000</a>', out)
        self.assertIn(f'<a href="{LINEAR}AFC-547">AFC-547</a>', out)

    def test_a_pull_request_reference_becomes_a_link_and_keeps_the_house_form(self):
        out = self.html("[AFC, MM] Notes", "Shipped in PR #957 and PR 958.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957">PR #957</a>',
            out,
        )
        #  the bare form is normalised to the house form, PR #N
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/958">PR #958</a>',
            out,
        )

    def test_the_team_key_in_the_title_picks_the_repository(self):
        out = self.html("[ST, Claude] Trading note", "Shipped in PR #957.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/Socratic-Trade/pull/957">PR #957</a>', out
        )

    def test_a_note_with_no_team_key_falls_back_to_the_fleet_repository(self):
        out = self.html("⭐️ Background Jobs Master List", "Shipped in PR #957.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957">PR #957</a>',
            out,
        )

    def test_an_identifier_that_is_not_a_linear_issue_is_left_alone(self):
        out = self.html("[AFC, MM] Notes", "The row is SRV-S07 and the other is MAC-P09.")
        self.assertNotIn("<a href=", out.split("<h1>")[1].split("</h1>")[1].split("</p>")[2])
        self.assertIn("SRV-S07", out)
        self.assertIn("MAC-P09", out)

    def test_code_and_a_fenced_block_are_left_alone(self):
        out = self.html(
            "[AFC, MM] Notes",
            "The code `AFC-380` stays.\n\n```\nPR #12 OPS-1\n```\n",
        )
        self.assertNotIn('href="https://linear.app', out)
        self.assertNotIn('href="https://github.com', out)
        self.assertIn("AFC-380", out)

    def test_a_hand_written_link_is_not_doubled(self):
        written = (
            "[PR #957](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957) and "
            "[OPS-1000](https://linear.app/simple-with-us/issue/OPS-1000)"
        )
        out = self.html("[AFC, MM] Notes", f"Already linked: {written}.")
        #  the markdown link is already an anchor when the linkifier sees it, so it is left exactly as it is
        self.assertEqual(out.count("<a href="), 2, out)
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957">PR #957</a>',
            out,
        )
        self.assertIn(f'<a href="{LINEAR}OPS-1000">OPS-1000</a>', out)

    def test_an_identifier_inside_a_url_is_left_alone(self):
        out = self.html(
            "[AFC, MM] Notes",
            "See https://linear.app/simple-with-us/issue/OPS-1000 for the issue.",
        )
        self.assertNotIn("<a href=", out.split("<h1>")[1].split("</h1>")[1].split("</p>")[2])
        self.assertIn("https://linear.app/simple-with-us/issue/OPS-1000", out)

    def test_the_step_can_be_turned_off(self):
        out = self.html("[AFC, MM] Notes", "Filed as OPS-1000 and PR #957.", NOTES_LINKIFY="0")
        self.assertNotIn("<a href=", out)
        self.assertIn("OPS-1000", out)

    def test_the_pull_request_suffix_of_the_date_row_becomes_a_link(self):
        p = self.run_script(
            "--update",
            "--existing-only",
            "--session",
            SESSION,
            "--pr",
            "957",
            NOTES[2]["title"],
            "Body text.",
            APPLE_NOTES_PRINT_HTML="1",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957">PR #957</a>',
            p.stdout,
        )

    def test_the_writer_says_what_it_linkified(self):
        p = self.run_script(
            "--session",
            SESSION,
            "[AFC, MM] Notes",
            "Filed as OPS-1000 and PR #957.",
            APPLE_NOTES_PRINT_HTML="1",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("notes: linkified:", p.stderr)
        self.assertIn("1 project reference", p.stderr)
        self.assertIn("1 pull request reference", p.stderr)
        #  and it says it once:  a second pass over the same body would double the summary
        self.assertEqual(p.stderr.count("notes: linkified:"), 1, p.stderr)
        self.assertEqual(p.stderr.count("notes: left as text:"), 0, p.stderr)

    def test_the_count_of_conversions_reaches_the_action_log(self):
        p = self.run_script(
            "--update",
            "--existing-only",
            "--session",
            SESSION,
            NOTES[2]["title"],
            "Filed as OPS-1000 and AFC-547 and shipped in PR #957.",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        (line,) = self.log_lines()
        self.assertEqual(line["reformatted"]["links"], {"issues": 2, "prs": 1}, line["reformatted"])

    def test_the_log_keeps_the_link_counts_when_the_date_counts_are_missing(self):
        #  the file the date counts are written to could not be made, so the log has no dates to
        #  record:  the link counts still belong in it
        stub = self.bindir / "mktemp"
        stub.write_text(MTEMP_STUB)
        stub.chmod(0o700)
        p = self.run_script(
            "--update",
            "--existing-only",
            "--session",
            SESSION,
            NOTES[2]["title"],
            "Filed as OPS-1000 and AFC-547 and shipped in PR #957.",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        (line,) = self.log_lines()
        self.assertEqual(line["reformatted"]["links"], {"issues": 2, "prs": 1}, line["reformatted"])

    def test_a_network_error_that_only_looks_like_a_404_is_not_a_missing_pull_request(self):
        #  "pulls/1404" is not a 404:  GitHub was never asked, so the repository of the team is the
        #  best answer available, and a reference is not lost to a number that merely looks like one
        out = self.html(
            "[AFC, MM] Notes",
            "OPS-1000 in PR #957.",
            GH_STUB_ERROR="gh: could not resolve host (pulls/1404)",
        )
        self.assertIn(
            '<a href="https://github.com/jaywedgeworth22/Fleet-OPS/pull/957">PR #957</a>', out
        )

    def test_a_repository_named_next_to_the_reference_picks_the_repository(self):
        #  the title says Socratic-Trade and the reference names AI-Fleet-Coordinator
        out = self.html("[ST, Claude] Trading note", "Merged as AFC PR #318.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/318">PR #318</a>',
            out,
        )

    def test_the_subject_of_a_table_row_picks_the_repository(self):
        out = self.html(
            "⭐️ Background Jobs Master List",
            "| Name | What it is |\n|---|---|\n"
            "| `com.jay.botfleet.server` | The harness, reset to origin/main (PR #480). |\n",
        )
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/BotFleet/pull/480">PR #480</a>', out
        )

    def test_a_qualified_reference_links_to_the_repository_it_names(self):
        #  a pound sign names a pull request, so the link is the pull request
        out = self.html("[AFC, MM] Notes", "Fixed by BotFleet#1282 and AFC#957.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/BotFleet/pull/1282">BotFleet#1282</a>',
            out,
        )
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957">AFC#957</a>',
            out,
        )

    def test_the_repositories_of_the_teams_can_be_overridden(self):
        #  a test does not have to carry the names of the repositories of the fleet
        out = self.html(
            "[AFC, MM] Notes",
            "OPS-1000 in PR #957.",
            NOTES_LINKIFY_TEAM_REPOS="OPS=Some-Other-Repo",
            NOTES_LINKIFY_REPO_OWNERS="Some-Other-Repo=someone-else",
        )
        self.assertIn(
            '<a href="https://github.com/someone-else/Some-Other-Repo/pull/957">PR #957</a>', out
        )

    def test_a_team_key_names_the_repository_of_that_team(self):
        #  there is one Linear team per repository, and OPS is the Fleet-OPS team
        out = self.html("[AFC, MM] Notes", "OPS-1000 in PR #957.")
        self.assertIn(
            '<a href="https://github.com/jaywedgeworth22/Fleet-OPS/pull/957">PR #957</a>', out
        )

    def test_a_team_that_owns_more_than_one_repository_is_asked_which_one_holds_it(self):
        #  a CTS issue is implemented in the shared congress repository and in the applications
        #  that use it, so GitHub is asked which of them holds the number
        out = self.html(
            "[AFC, MM] Notes",
            "CTS-330 in PR #4523.",
            GH_STUB_MISSING=(
                "repos/Simple-With-Us/congress-trading-shared/pulls/4523,"
                "repos/Simple-With-Us/Congress.Trade/pulls/4523"
            ),
        )
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/Socratic-Trade/pull/4523">PR #4523</a>', out
        )

    def test_a_pull_request_no_repository_holds_is_left_alone(self):
        #  a link to the wrong repository is worse than no link
        out = self.html("[AFC, MM] Notes", "OPS-1000 in PR #957.", GH_STUB_MISSING="*")
        self.assertNotIn("pull/957", out)
        self.assertIn("PR #957", out)

    def test_a_block_that_names_two_teams_names_no_repository(self):
        #  AFC-547 and OPS-1000 together say nothing about where a pull request lives, so the
        #  repository of the note decides
        out = self.html(
            "[AFC, MM] Notes", "See also AFC-547 and OPS-1000.  Prior PRs #380, #386 and #389."
        )
        for num in (380, 386, 389):
            self.assertIn(
                f'<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/{num}">#{num}</a>',
                out,
            )

    def test_a_list_of_pull_requests_becomes_one_link_each(self):
        out = self.html(
            "[AFC, MM] Notes", "Installed live from AFC main (PRs #380, #386 and #389)."
        )
        for num in (380, 386, 389):
            self.assertIn(
                f'<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/{num}">#{num}</a>',
                out,
            )

    def test_a_file_count_after_a_list_is_not_a_pull_request(self):
        out = self.html("[AFC, MM] Notes", "refreshed after PRs 439, 440 and 445, 22 files.")
        for num in (439, 440, 445):
            self.assertIn(
                f'<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/{num}">#{num}</a>',
                out,
            )
        self.assertNotIn("pull/22", out)
        self.assertIn("22 files", out)

    def test_a_file_count_after_one_pull_request_stays_in_the_text(self):
        out = self.html("[AFC, MM] Notes", "shipped in PR #12, 34 files.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/12">PR #12</a>',
            out,
        )
        self.assertNotIn("pull/34", out)
        self.assertIn("34 files", out)

    def test_a_pull_request_without_a_space_is_the_keyword_form(self):
        out = self.html("[AFC, MM] Notes", "landed as PR#957.")
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957">PR #957</a>',
            out,
        )
        self.assertIn("<a href=", out)
        self.assertNotIn("PR#957", out)

    def test_a_timestamp_is_not_a_repository(self):
        #  CT is Central Time here, and Congress.Trade is not what this note is about
        out = self.html(
            "[AFC, MM] Notes", "At 9:13pm CT (MM) the maintenance trigger landed in PR #909."
        )
        self.assertIn(
            '<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/909">PR #909</a>',
            out,
        )

    def test_a_linear_issue_next_to_a_reference_does_not_move_it(self):
        #  OPS-1000 is a Linear issue on the Fleet-OPS board and says nothing about the repository
        out = self.html(
            "[AFC, MM] Notes", "See also AFC-547 and OPS-1000.  Prior PRs #380, #386 and #389."
        )
        for num in (380, 386, 389):
            self.assertIn(
                f'<a href="https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/{num}">#{num}</a>',
                out,
            )

    def test_a_reference_the_block_cannot_place_is_left_as_text(self):
        #  two repositories in the block and neither of them named next to the reference:  a link to the
        #  wrong repository is worse than no link
        out = self.html(
            "⭐️ Background Jobs Master List",
            "The BotFleet service and the Clutch app both changed, and then several other things happened "
            "before the shared library fix landed in PR #42.",
        )
        self.assertNotIn("pull/42", out)
        self.assertIn("PR #42", out)


if __name__ == "__main__":
    unittest.main()
