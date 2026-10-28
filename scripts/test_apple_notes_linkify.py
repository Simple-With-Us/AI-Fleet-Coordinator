"""AFC-547:  apple-notes-coding.sh turns bare project and pull request references into links.

Owner 2026-10-25:  links everywhere, Apple Notes included, in the house linkifier style, and the helper does the
converting so every seat gets it without writing it.

The helper already owns the note's other automatic rewrites, the fleet dates and the sentence gaps, so linkification
belongs to the same place and the same shape:  a rewrite the writer does, counted, logged and reported on stderr.
What is pinned here:

  a Linear issue identifier (OPS-1000) becomes a link to that issue in the fleet workspace;
  a Linear project reference (P-ST-9) becomes a link to that project, and a project is coded by hand
  because a project URL carries a slug and a hash that nothing can derive from the reference;
  a project reference is never read as an issue, so the number inside it never becomes an issue link;
  a project reference that names no known project stays plain text;
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
  a network error that only looks like a 404 is not a missing pull request:  the repository of the team is the answer;
  a context length that is not a number does not cost the note its body;
  a pull request lookup that hangs does not hang the note save;
  a team key further down the block does not veto the team named right above the reference;
  a team named a long way from the reference is asked of GitHub, exactly as a nearby one is;
  a qualified reference names a pull request, so the link is the pull request and not the issue that shares its number;
  a fleet shortcut (fl/fleet-tools, fleetlink/zulip) becomes a link to that page, and a shortcut inside a URL or a
  longer path is left alone (AFC-562);
  a site page (SWU/docs), a repository (GH@user/repo), an incident (PagerDuty#384), a Sentry short id
  (BOTFLEET-16) and a Cursor agent id (bc-a1b2c3d4-e5f6) each become a link to what they name, and each is
  counted under its own kind.

The fixture is synthetic end to end.  Every hostname, owner, repository name and workspace slug in this file belongs
to no one and exists nowhere but here (Kody rule:  use synthetic hostnames and paths in test fixtures).  The world
the tests describe is:

  the GitHub host         example.test
  the GitHub owner        example-org, and other-org for the one repository that is not under it
  the Linear host         notes.example.test
  the Linear workspace    example-workspace
  the fleet repository    Fixture-Repo
  the team keys           AFC -> Fixture-Repo, ST -> Title-Repo, OPS -> Other-Repo, CTS -> three repositories
  the named services      widget -> Fixture-Repo2, gadget -> Fixture-Repo3

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

from test_apple_notes_actions import (
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

#  The world of the fixture.  Nothing here exists outside this file.
GITHUB_HOST = "example.test"
GITHUB_OWNER = "example-org"
OTHER_OWNER = "other-org"
LINEAR_HOST = "notes.example.test"
LINEAR_WORKSPACE = "example-workspace"
FLEET_REPO = "Fixture-Repo"
TITLE_REPO = "Title-Repo"
OTHER_REPO = "Other-Repo"
SECOND_REPO = "Second-Repo"
THIRD_REPO = "Third-Repo"
SERVICE_REPO = "Fixture-Repo2"
APP_REPO = "Fixture-Repo3"

#  The team keys, and the repositories each of them works in.  A plus sign lists the other repositories
#  of a team that owns more than one, and a key that is not a team key names a service instead.
TEAM_REPOS = {
    "AFC": FLEET_REPO,
    "ST": TITLE_REPO,
    "OPS": OTHER_REPO,
    "CTS": FLEET_REPO + "+" + SECOND_REPO + "+" + THIRD_REPO,
    "CT": "Clock-Repo",
    "UM": "Meter-Repo",
    "DD": "Data-Repo",
    "PS": "Page-Repo",
    "AR": "Auto-Repo",
    "CL": "Contact-Repo",
    "BF": "Harness-Repo",
    "HH": "Hunter-Repo",
    "C": "Gear-Repo",
    "CC": "Caps-Repo",
    "FL": "Fleet-Repo",
    "SWU": "Home-Repo",
    "CK": "Gear-Repo",
    "CAPS": "Caps-Repo",
    "CLUTCH": "Gear-Repo",
    "widget": SERVICE_REPO,
    "gadget": APP_REPO,
}
SYNTHETIC_TEAM_REPOS = ",".join(key + "=" + value for key, value in TEAM_REPOS.items())
SYNTHETIC_OWNERS = OTHER_REPO + "=" + OTHER_OWNER


def gh_url(repo: str, num: str, owner: str = GITHUB_OWNER, kind: str = "pull") -> str:
    """The link the helper makes for a pull request (or an issue) in a repository."""
    return "https://" + GITHUB_HOST + "/" + owner + "/" + repo + "/" + kind + "/" + str(num)


def gh_path(repo: str, num: str, owner: str = GITHUB_OWNER) -> str:
    """The path the helper asks the gh CLI about."""
    return "repos/" + owner + "/" + repo + "/pulls/" + num


LINEAR_URL = "https://" + LINEAR_HOST + "/" + LINEAR_WORKSPACE + "/issue/"

#  A project the fixture codes by hand.  A project URL carries a slug and a hash, so a project is
#  named in full:  the reference a writer types, and the URL of that project.
PROJECT_URL = "https://" + LINEAR_HOST + "/" + LINEAR_WORKSPACE + "/project/fixture-project-abc123"

#  The other hosts the linkifier writes, all synthetic like the rest of the fixture (AFC-562).
FLEETLINK_HOST = "fleetlink.example.test"
SWU_HOST = "site.example.test"
PAGERDUTY_HOST = "pager.example.test"
SENTRY_HOST = "sentry.example.test"
CURSOR_HOST = "agents.example.test"

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

#  A stand-in for the gh CLI that never answers, the way a network that hangs does.  The helper has to
#  give up on it and carry on.
GH_STUB_HANG = """#!/bin/sh
exec sleep 30
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
            #  the synthetic world, in force for every test unless a test overrides it
            "NOTES_LINKIFY_GITHUB_HOST": GITHUB_HOST,
            "NOTES_LINKIFY_FLEETLINK_HOST": FLEETLINK_HOST,
            "NOTES_LINKIFY_SWU_HOST": SWU_HOST,
            "NOTES_LINKIFY_PAGERDUTY_HOST": PAGERDUTY_HOST,
            "NOTES_LINKIFY_SENTRY_HOST": SENTRY_HOST,
            "NOTES_LINKIFY_CURSOR_HOST": CURSOR_HOST,
            "NOTES_LINKIFY_LINEAR_HOST": LINEAR_HOST,
            "NOTES_GITHUB_OWNER": GITHUB_OWNER,
            "NOTES_LINKIFY_REPO": FLEET_REPO,
            "NOTES_LINEAR_WORKSPACE": LINEAR_WORKSPACE,
            "NOTES_LINKIFY_TEAM_REPOS": SYNTHETIC_TEAM_REPOS,
            "NOTES_LINKIFY_REPO_OWNERS": SYNTHETIC_OWNERS,
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
    def test_the_fixture_is_synthetic(self):
        #  no hostname, owner or repository of the fleet can reach a link, and the fixture itself is in
        #  force:  the link it makes names the synthetic host
        out = self.html("[AFC, MM] Notes", "Shipped in PR #957.")
        self.assertIn("https://" + GITHUB_HOST + "/", out)
        for real in (
            "Simple-With-Us",
            "jaywedgeworth22",
            "github.com",
            "linear.app",
            "simple-with-us",
            "fleetlink.online",
            "simplewithus.com",
            "cursor.com",
            "pagerduty.com",
            "sentry.io",
        ):
            self.assertNotIn(real, out)

    def test_a_linear_issue_identifier_becomes_a_link_to_the_issue(self):
        out = self.html("[AFC, MM] Notes", "Filed as OPS-1000 and tracked under AFC-547.")
        self.assertIn(f'<a href="{LINEAR_URL}OPS-1000">OPS-1000</a>', out)
        self.assertIn(f'<a href="{LINEAR_URL}AFC-547">AFC-547</a>', out)

    def test_a_coded_project_reference_becomes_a_link_to_that_project(self):
        #  a project has no URL that can be derived from its reference, so it is coded:  the
        #  reference a writer types, and the URL of that project
        out = self.html(
            "[AFC, MM] Notes",
            "The rewrite is P-ST-9.",
            NOTES_LINEAR_PROJECTS="P-ST-9=" + PROJECT_URL,
        )
        self.assertIn(f'<a href="{PROJECT_URL}">P-ST-9</a>', out)

    def test_the_projects_in_the_table_link_into_the_configured_workspace(self):
        #  the project the fleet names, built from the host and the workspace the note is written
        #  for, so a note written for another workspace links into that one
        out = self.html("[AFC, MM] Notes", "The rewrite is P-BF-6.")
        self.assertIn('href="https://' + LINEAR_HOST + "/" + LINEAR_WORKSPACE + "/project/", out)
        self.assertIn(">P-BF-6</a>", out)

    def test_a_project_reference_is_never_read_as_an_issue(self):
        #  the number in a project reference is a project number, so it never becomes an issue link
        out = self.html("[AFC, MM] Notes", "The rewrite is P-BF-6 and the other is P-ST-8.")
        self.assertNotIn("/issue/BF-6", out)
        self.assertNotIn("/issue/ST-8", out)
        self.assertIn("P-BF-6", out)
        self.assertIn("P-ST-8", out)

    def test_a_short_project_reference_is_never_read_as_an_issue(self):
        #  a writer may leave the team out of a project reference, and the number in it is still a
        #  project number, so the short form is claimed as a project and never becomes an issue
        out = self.html("[AFC, MM] Notes", "The rewrite is P-6 and the issue is BF-6.")
        self.assertNotIn("/issue/6", out)
        self.assertNotIn(">P-6</a>", out)
        self.assertIn("P-6", out)
        self.assertIn(f'<a href="{LINEAR_URL}BF-6">BF-6</a>', out)

    def test_a_short_project_reference_can_be_coded_too(self):
        #  the short form is a reference of its own, so it links only when it is coded, exactly
        #  like the long form
        out = self.html(
            "[AFC, MM] Notes",
            "The rewrite is P-6.",
            NOTES_LINEAR_PROJECTS="P-6=" + PROJECT_URL,
        )
        self.assertIn(f'<a href="{PROJECT_URL}">P-6</a>', out)

    def test_a_project_reference_that_is_not_coded_stays_plain_text(self):
        #  a project that is not in the table is left alone:  a link to the wrong thing is worse
        #  than no link, and the reference is never guessed at
        out = self.html("[AFC, MM] Notes", "The rewrite is P-ST-8.")
        self.assertNotIn("<a href=", out.split("<h1>")[1].split("</h1>")[1].split("</p>")[2])
        self.assertIn("P-ST-8", out)

    def test_the_writer_counts_a_project_reference_as_a_project(self):
        p = self.run_script(
            "--session",
            SESSION,
            "[AFC, MM] Notes",
            "The rewrite is P-BF-6.",
            APPLE_NOTES_PRINT_HTML="1",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("1 project reference", p.stderr)
        self.assertIn("0 issue references", p.stderr)

    def test_a_pull_request_reference_becomes_a_link_and_keeps_the_house_form(self):
        out = self.html("[AFC, MM] Notes", "Shipped in PR #957 and PR 958.")
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', out)
        #  the bare form is normalised to the house form, PR #N
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "958")}">PR #958</a>', out)

    def test_the_team_key_in_the_title_picks_the_repository(self):
        out = self.html("[ST, Claude] Trading note", "Shipped in PR #957.")
        self.assertIn(f'<a href="{gh_url(TITLE_REPO, "957")}">PR #957</a>', out)

    def test_a_note_with_no_team_key_falls_back_to_the_fleet_repository(self):
        out = self.html("⭐️ Background Jobs Master List", "Shipped in PR #957.")
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', out)

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
        self.assertNotIn('href="' + LINEAR_HOST, out)
        self.assertNotIn('href="' + GITHUB_HOST, out)
        self.assertIn("AFC-380", out)

    def test_a_hand_written_link_is_not_doubled(self):
        written = f"[PR #957]({gh_url(FLEET_REPO, '957')}) and [OPS-1000]({LINEAR_URL}OPS-1000)"
        out = self.html("[AFC, MM] Notes", f"Already linked: {written}.")
        #  the markdown link is already an anchor when the linkifier sees it, so it is left exactly as it is
        self.assertEqual(out.count("<a href="), 2, out)
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', out)
        self.assertIn(f'<a href="{LINEAR_URL}OPS-1000">OPS-1000</a>', out)

    def test_an_identifier_inside_a_url_is_left_alone(self):
        out = self.html(
            "[AFC, MM] Notes",
            f"See {LINEAR_URL}OPS-1000 for the issue.",
        )
        self.assertNotIn("<a href=", out.split("<h1>")[1].split("</h1>")[1].split("</p>")[2])
        self.assertIn(LINEAR_URL + "OPS-1000", out)

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
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', p.stdout)

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
        self.assertIn("1 issue reference", p.stderr)
        self.assertIn("0 project references", p.stderr)
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
        self.assertEqual(
            line["reformatted"]["links"],
            {"issues": 2, "prs": 1, "projects": 0, "fleetlink": 0, "swu": 0, "repos": 0, "pagerduty": 0, "sentry": 0, "cursor": 0},
            line["reformatted"],
        )

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
        self.assertEqual(
            line["reformatted"]["links"],
            {"issues": 2, "prs": 1, "projects": 0, "fleetlink": 0, "swu": 0, "repos": 0, "pagerduty": 0, "sentry": 0, "cursor": 0},
            line["reformatted"],
        )

    def test_a_network_error_that_only_looks_like_a_404_is_not_a_missing_pull_request(self):
        #  "pulls/1404" is not a 404:  GitHub was never asked, so the repository of the team is the
        #  best answer available, and a reference is not lost to a number that merely looks like one
        out = self.html(
            "[AFC, MM] Notes",
            "OPS-1000 in PR #957.",
            GH_STUB_ERROR="gh: could not resolve host (pulls/1404)",
        )
        self.assertIn(f'<a href="{gh_url(OTHER_REPO, "957", OTHER_OWNER)}">PR #957</a>', out)

    def test_a_repository_named_next_to_the_reference_picks_the_repository(self):
        #  the title says one team and the reference names another
        out = self.html("[ST, Claude] Trading note", "Merged as AFC PR #318.")
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "318")}">PR #318</a>', out)

    def test_the_subject_of_a_table_row_picks_the_repository(self):
        out = self.html(
            "⭐️ Background Jobs Master List",
            "| Name | What it is |\n|---|---|\n"
            "| `com.example.widget.server` | The harness, reset to origin/main (PR #480). |\n",
        )
        self.assertIn(f'<a href="{gh_url(SERVICE_REPO, "480")}">PR #480</a>', out)

    def test_a_qualified_reference_links_to_the_pull_request_and_not_to_the_issue(self):
        #  a pound sign names a pull request, so the link is the pull request
        out = self.html("[AFC, MM] Notes", "Fixed by widget#1282 and AFC#957.")
        self.assertIn(f'<a href="{gh_url(SERVICE_REPO, "1282")}">widget#1282</a>', out)
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">AFC#957</a>', out)
        #  the control:  nothing in the note points at an issue of the same number
        self.assertNotIn("/issues/1282", out)
        self.assertNotIn("/issues/957", out)

    def test_the_repositories_of_the_teams_can_be_overridden(self):
        #  a test does not have to carry the names of the repositories of the fleet
        repos = (
            ",".join(key + "=" + value for key, value in TEAM_REPOS.items() if key != "OPS")
            + ",OPS=Another-Repo"
        )
        out = self.html(
            "[AFC, MM] Notes",
            "OPS-1000 in PR #957.",
            NOTES_LINKIFY_TEAM_REPOS=repos,
            NOTES_LINKIFY_REPO_OWNERS="Another-Repo=third-org",
        )
        self.assertIn(f'<a href="{gh_url("Another-Repo", "957", "third-org")}">PR #957</a>', out)

    def test_a_team_key_names_the_repository_of_that_team(self):
        #  there is one Linear team per repository, and OPS is the team of the fleet repository
        out = self.html("[AFC, MM] Notes", "OPS-1000 in PR #957.")
        self.assertIn(f'<a href="{gh_url(OTHER_REPO, "957", OTHER_OWNER)}">PR #957</a>', out)

    def test_a_team_that_owns_more_than_one_repository_is_asked_which_one_holds_it(self):
        #  a CTS issue is implemented in the shared repository and in the applications that use it, so
        #  GitHub is asked which of them holds the number
        out = self.html(
            "[AFC, MM] Notes",
            "CTS-330 in PR #4523.",
            GH_STUB_MISSING=(gh_path(FLEET_REPO, "4523") + "," + gh_path(SECOND_REPO, "4523")),
        )
        self.assertIn(f'<a href="{gh_url(THIRD_REPO, "4523")}">PR #4523</a>', out)

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
            self.assertIn(f'<a href="{gh_url(FLEET_REPO, num)}">#{num}</a>', out)

    def test_a_list_of_pull_requests_becomes_one_link_each(self):
        out = self.html(
            "[AFC, MM] Notes", "Installed live from AFC main (PRs #380, #386 and #389)."
        )
        for num in (380, 386, 389):
            self.assertIn(f'<a href="{gh_url(FLEET_REPO, num)}">#{num}</a>', out)

    def test_a_file_count_after_a_list_is_not_a_pull_request(self):
        out = self.html("[AFC, MM] Notes", "refreshed after PRs 439, 440 and 445, 22 files.")
        for num in (439, 440, 445):
            self.assertIn(f'<a href="{gh_url(FLEET_REPO, num)}">#{num}</a>', out)
        self.assertNotIn("pull/22", out)
        self.assertIn("22 files", out)

    def test_a_file_count_after_one_pull_request_stays_in_the_text(self):
        out = self.html("[AFC, MM] Notes", "shipped in PR #12, 34 files.")
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "12")}">PR #12</a>', out)
        self.assertNotIn("pull/34", out)
        self.assertIn("34 files", out)

    def test_a_pull_request_without_a_space_is_the_keyword_form(self):
        out = self.html("[AFC, MM] Notes", "landed as PR#957.")
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', out)
        self.assertIn("<a href=", out)
        self.assertNotIn("PR#957", out)

    def test_a_timestamp_is_not_a_repository(self):
        #  CT is Central Time here, and the team of that key is not what this note is about
        out = self.html(
            "[AFC, MM] Notes", "At 9:13pm CT (MM) the maintenance trigger landed in PR #909."
        )
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "909")}">PR #909</a>', out)

    def test_a_linear_issue_next_to_a_reference_does_not_move_it(self):
        #  OPS-1000 is a Linear issue on the fleet board and says nothing about the repository
        out = self.html(
            "[AFC, MM] Notes", "See also AFC-547 and OPS-1000.  Prior PRs #380, #386 and #389."
        )
        for num in (380, 386, 389):
            self.assertIn(f'<a href="{gh_url(FLEET_REPO, num)}">#{num}</a>', out)

    def test_a_reference_the_block_cannot_place_is_left_as_text(self):
        #  two repositories in the block and neither of them named next to the reference:  a link to the
        #  wrong repository is worse than no link
        out = self.html(
            "⭐️ Background Jobs Master List",
            "The widget service and the gadget app both changed, and then several other things happened "
            "before the shared library fix landed in PR #42.",
        )
        self.assertNotIn("pull/42", out)
        self.assertIn("PR #42", out)

    def test_a_context_length_that_is_not_a_number_does_not_cost_the_note_its_body(self):
        #  a bad value used to raise, the helper exited with empty output and the note was written blank
        body = "Shipped in PR #957."
        good = self.html("[AFC, MM] Notes", body)
        bad = self.html("[AFC, MM] Notes", body, NOTES_LINKIFY_CONTEXT_CHARS="not-a-number")
        #  the control:  with the documented default the same body links the same way
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', good)
        #  and the body is still there:  a crash would have left it empty
        self.assertIn("Shipped in", bad)
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', bad)

    def test_a_pull_request_lookup_that_hangs_does_not_hang_the_note_save(self):
        #  a gh that never answers must not stall the write path
        stub = self.bindir / "gh"
        stub.write_text(GH_STUB_HANG)
        stub.chmod(0o700)
        out = self.html(
            "[AFC, MM] Notes",
            "OPS-1000 in PR #957.",
            NOTES_LINKIFY_GH_TIMEOUT_S="1",
        )
        #  the control:  the note still has its body, and the reference still resolves to the
        #  repository of the team, because a question that was not answered is not a no
        self.assertIn("OPS-1000", out)
        self.assertIn(f'<a href="{gh_url(OTHER_REPO, "957", OTHER_OWNER)}">PR #957</a>', out)

    def test_a_team_key_below_the_reference_does_not_veto_the_one_above_it(self):
        #  a team key further down the block says nothing about what came before it
        out = self.html(
            "⭐️ Background Jobs Master List",
            "AFC-547.  PR #957 closed it.  And much later OPS-1000 was filed.",
        )
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', out)
        #  the control:  the same two teams, both above the reference, name no repository at all
        both_above = self.html(
            "⭐️ Background Jobs Master List",
            "AFC-547 and OPS-1000.  PR #957 closed it.",
        )
        self.assertNotIn("pull/957", both_above)

    def test_a_team_named_a_long_way_from_the_reference_is_asked_of_github(self):
        #  a team that owns more than one repository is asked of GitHub, however far away it is named
        body = (
            "AFC-547 opened the work.  "
            + ("Filler that takes up the room.  " * 5)
            + "PR #957 closed it."
        )
        out = self.html("⭐️ Background Jobs Master List", body)
        self.assertIn(f'<a href="{gh_url(FLEET_REPO, "957")}">PR #957</a>', out)
        #  the control:  when GitHub says no repository holds it, the reference is left as text
        missing = self.html("⭐️ Background Jobs Master List", body, GH_STUB_MISSING="*")
        self.assertNotIn("pull/957", missing)
        self.assertIn("PR #957", missing)

    def test_fl_and_fleetlink_paths_become_links(self):
        #  the owner's example, and a nested path ending a sentence:  the sentence's dot stays
        #  outside the link, because a path never ends on punctuation
        out = self.html(
            "[AFC, MM] Notes", "See fl/fleet-tools and fleetlink/zulip/zulip-fleet-guide.md."
        )
        self.assertIn(
            f'<a href="https://{FLEETLINK_HOST}/fleet-tools">fl/fleet-tools</a>', out
        )
        self.assertIn(
            f'<a href="https://{FLEETLINK_HOST}/zulip/zulip-fleet-guide.md">'
            "fleetlink/zulip/zulip-fleet-guide.md</a>.",
            out,
        )
        self.assertNotIn("guide.md.</a>", out)

    def test_a_shortcut_inside_a_url_or_path_is_left_alone(self):
        #  fl/ inside a URL or a longer path is part of that address, not a shortcut
        out = self.html("[AFC, MM] Notes", "See https://x/fl/y and foo/fl/x for it.")
        self.assertNotIn(FLEETLINK_HOST, out)
        self.assertIn("https://x/fl/y", out)
        self.assertIn("foo/fl/x", out)

    def test_a_swu_page_becomes_a_link(self):
        out = self.html("[AFC, MM] Notes", "Read SWU/docs for the background.")
        self.assertIn(f'<a href="https://{SWU_HOST}/docs">SWU/docs</a>', out)
        #  the control:  the issue form of the same key still links to the issue
        issues = self.html("[AFC, MM] Notes", "Read SWU-4 for the background.")
        self.assertIn(f'<a href="{LINEAR_URL}SWU-4">SWU-4</a>', issues)

    def test_a_gh_user_repo_becomes_a_link_to_that_repository(self):
        out = self.html("[AFC, MM] Notes", "Vendored from GH@example-org/widget for now")
        self.assertIn(
            f'<a href="https://{GITHUB_HOST}/example-org/widget">GH@example-org/widget</a>',
            out,
        )

    def test_a_pagerduty_incident_becomes_a_link_and_wins_over_the_qualified_rule(self):
        #  a pound sign usually names a pull request, but PagerDuty names an incident instead
        out = self.html("[AFC, MM] Notes", "Paged as PagerDuty#384 and PagerDuty#Q1ABC23.")
        self.assertIn(
            f'<a href="https://{PAGERDUTY_HOST}/incidents/384">PagerDuty#384</a>', out
        )
        self.assertIn(
            f'<a href="https://{PAGERDUTY_HOST}/incidents/Q1ABC23">PagerDuty#Q1ABC23</a>',
            out,
        )
        self.assertNotIn("/pull/", out)

    def test_a_cursor_agent_id_becomes_a_link(self):
        out = self.html("[AFC, MM] Notes", "Continued in bc-a1b2c3d4-e5f6 for review")
        self.assertIn(
            f'<a href="https://{CURSOR_HOST}/agents/bc-a1b2c3d4-e5f6">bc-a1b2c3d4-e5f6</a>',
            out,
        )
        #  the control:  fewer than eight hex digits is not an agent id
        short = self.html("[AFC, MM] Notes", "Continued in bc-12 for review")
        self.assertNotIn(CURSOR_HOST, short)
        self.assertIn("bc-12", short)

    def test_a_sentry_short_id_becomes_a_link_and_not_a_linear_issue(self):
        out = self.html("[AFC, MM] Notes", "Filed as BOTFLEET-16 and CONGRESS-TRADE-4A.")
        self.assertIn(
            f'<a href="https://{SENTRY_HOST}/issues/?query=BOTFLEET-16">BOTFLEET-16</a>',
            out,
        )
        self.assertIn(
            f'<a href="https://{SENTRY_HOST}/issues/?query=CONGRESS-TRADE-4A">'
            "CONGRESS-TRADE-4A</a>",
            out,
        )
        self.assertNotIn("/issue/", out)

    def test_the_new_kinds_leave_code_and_a_hand_written_link_alone(self):
        out = self.html(
            "[AFC, MM] Notes",
            f"The code `fl/tools` stays, and [SWU/docs](https://{SWU_HOST}/docs) is kept.",
        )
        self.assertEqual(out.count("<a href="), 1, out)
        self.assertIn("fl/tools</code>", out)

    def test_every_live_project_reference_becomes_a_link(self):
        #  the eight P-* rules of the live Zulip set, each coded by hand like P-BF-6
        refs = {
            "P-AFC-7": "muster-fleet-admin-mac-app-9c28cf61dcc8",
            "P-AFC-8": "fleet-gitops-272cdd72e951",
            "P-BF-1": "system-prompt-receipt-port-bf-1411-cc08b4cf163b",
            "P-BF-6": "botfleet-native-macos-rewrite-5afcbc055535",
            "P-CTS-5": "cts-shared-dependency-a04bf2274b2d",
            "P-OPS-2": "mac-resource-mgmt-22f992634f02",
            "P-OPS-4": "fleet-hygiene-cross-team-32fd751b0b36",
            "P-SWU-3": "public-websites-6e26b4c70c72",
        }
        out = self.html("[AFC, MM] Notes", "The plan covers " + ", ".join(refs) + ".")
        base = "https://" + LINEAR_HOST + "/" + LINEAR_WORKSPACE + "/project/"
        for ref, slug in refs.items():
            with self.subTest(ref=ref):
                self.assertIn(f'<a href="{base}{slug}">{ref}</a>', out)

    def test_the_writer_says_what_the_new_kinds_became(self):
        p = self.run_script(
            "--session",
            SESSION,
            "[AFC, MM] Notes",
            "See fl/tools and SWU/docs and GH@example-org/widget and PagerDuty#384 and "
            "BOTFLEET-16 and bc-a1b2c3d4-e5f6 for the lot.",
            APPLE_NOTES_PRINT_HTML="1",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        for kind in (
            "1 fleetlink reference",
            "1 SWU reference",
            "1 repository reference",
            "1 PagerDuty reference",
            "1 Sentry reference",
            "1 Cursor agent reference",
        ):
            with self.subTest(kind=kind):
                self.assertIn(kind, p.stderr)

    def test_the_log_keeps_the_new_kinds_too(self):
        p = self.run_script(
            "--update",
            "--existing-only",
            "--session",
            SESSION,
            NOTES[2]["title"],
            "See fl/tools and SWU/docs for the background.",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        (line,) = self.log_lines()
        self.assertEqual(
            line["reformatted"]["links"],
            {
                "issues": 0,
                "prs": 0,
                "projects": 0,
                "fleetlink": 1,
                "swu": 1,
                "repos": 0,
                "pagerduty": 0,
                "sentry": 0,
                "cursor": 0,
            },
            line["reformatted"],
        )


if __name__ == "__main__":
    unittest.main()
