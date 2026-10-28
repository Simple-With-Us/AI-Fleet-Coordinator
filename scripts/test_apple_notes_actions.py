"""AFC-486:  apple-notes-coding.sh reads, lists, updates only an existing note on request, and logs every run.

docs/protocols/operator-notes.md is the spec.  The script is about to be called from a Mac session, from
`simple-notes` and the MCP tools on the Mac, and from the seatless Mac runner that does the server's and the cloud
seats' requests, so four things are pinned here:

  --read      one note as one JSON object on stdout ({"title", "id", "created", "modified", "body", "plaintext"}),
              exit 3 with nothing on stdout when there is no such note, and no session id needed;
  --list      the Coding folder newest change first, --query a case-insensitive title filter, --limit 1 to 500
              (anything else exits 2), {"notes": [...], "total", "truncated"};
  --existing-only
              with --update only (exit 2 otherwise):  a missing note exits 3 and is never created;
  the log     one JSON line per run that got past argument parsing, success or failure, with the fields the spec
              lists and never the note's text;  APPLE_NOTES_ACTION_LOG=off writes nothing, the file rotates at 5 MB,
              and a log that cannot be written leaves the exit code and the output alone.

The whole script runs, on any host with bash and /usr/bin/python3, with `osascript`, `shortcuts`, `open`, `pgrep`
and `sleep` stubbed on PATH, as in test_apple_notes_pin.py.  The `osascript` stub answers from a JSON fixture the way
the script's JXA program and AppleScript blocks answer, and records what it was given (the arguments, the
environment, the program text and the HTML the script handed it), so the assertions read what reached it.  The JXA
program itself is run under node against a fake Notes object model when node is on the host, so its lookup, its
not-found answer, its plain-ASCII output and its three-try folder read are covered here too, not only on a Mac.

Run:  cd scripts && python3 -m unittest test_apple_notes_actions
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"
NOT_FOUND = "__APPLE_NOTES_NOT_FOUND__"
ROTATE_AT = 5 * 1024 * 1024
LOG_FIELDS = {
    "v",
    "id",
    "ts",
    "host",
    "mode",
    "title",
    "note_id",
    "exit",
    "outcome",
    "duration_ms",
    "seat",
    "session",
    "caller",
    "request_id",
    "requester",
    "input_bytes",
    "body_bytes",
    "body_sha256",
    "reformatted",
    "pin",
    "flags",
}
FLAG_FIELDS = {"notify", "needs_owner", "html", "existing_only", "summary", "pr"}

#  a stand-in for `osascript`.  It answers the JXA program (-l JavaScript) from the STUB_NOTES fixture in the shape
#  the program prints, the pin lookup (-e) with the note's id, and the AppleScript write blocks the way they answer
#  (the not-found sentinel for --existing-only on a missing note), and records every call as call.N.json.
OSASCRIPT_STUB = """#!{python}
import json, os, re, sys

calls = os.environ["STUB_OSA"]
n = len([name for name in os.listdir(calls) if name.startswith("call.")]) + 1
argv = sys.argv[1:]
source = "" if "-e" in argv else sys.stdin.read()
keys = ("APPLE_NOTES_JXA_MODE", "APPLE_NOTES_TITLE", "EXISTING_ONLY", "WANT_ACTIVATE", "TITLE_FILE", "TMP")
record = {{"argv": argv, "source": source, "env": {{key: os.environ.get(key) for key in keys}}}}
html_path = os.environ.get("TMP") if os.environ.get("TITLE_FILE") else None
found = re.search(r'set htmlPath to POSIX file "([^"]+)"', source)
if found:
    html_path = found.group(1)
if html_path and os.path.isfile(html_path):
    record["html_path"] = html_path
    with open(html_path, "rb") as fh:
        record["html_sha256"] = __import__("hashlib").sha256(fh.read()).hexdigest()
    record["html_bytes"] = os.path.getsize(html_path)
if os.environ.get("TITLE_FILE"):
    record["title_file"] = os.environ["TITLE_FILE"]
    with open(os.environ["TITLE_FILE"], encoding="utf-8") as fh:
        record["title"] = fh.read()
notes = []
if os.environ.get("STUB_NOTES"):
    with open(os.environ["STUB_NOTES"], encoding="utf-8") as fh:
        notes = json.load(fh)
titles = [note["title"] for note in notes]


def answer():
    if argv[:2] == ["-l", "JavaScript"]:
        if os.environ.get("APPLE_NOTES_JXA_MODE") == "list":
            return json.dumps({{
                "names": titles,
                "ids": [note["id"] for note in notes],
                "created": [note["created"] for note in notes],
                "modified": [note["modified"] for note in notes],
            }})
        for note in notes:
            if note["title"] == os.environ.get("APPLE_NOTES_TITLE"):
                return json.dumps(note)
        return "{not_found}"
    if "-e" in argv:
        asked = re.search(r'note "(.*)" of folder', " ".join(argv))
        record["title"] = asked.group(1) if asked else None
        return "x-coredata://stub/note/1" if record["title"] in titles else ""
    if "title" in record:
        if record["title"] in titles:
            record["wrote"] = "update"
            return "x-coredata://stub/note/1"
        if os.environ.get("EXISTING_ONLY") == "1":
            return "{not_found}"
        record["wrote"] = "create"
        return "x-coredata://stub/note/new"
    record["wrote"] = "create"
    return "x-coredata://stub/note/new"


out = answer()
with open(os.path.join(calls, "call.%d.json" % n), "w", encoding="utf-8") as fh:
    json.dump(record, fh)
if os.environ.get("STUB_OSA_FAIL") == "1":
    sys.stderr.write("execution error: the stub was told to fail" + chr(10))
    sys.exit(1)
print(out)
"""

#  `shortcuts`:  lists both note Shortcuts, counts every run, and fails a run when STUB_SHORTCUT_FAIL=1
SHORTCUTS_STUB = """#!/bin/bash
if [ "${1:-}" = "list" ]; then printf 'Pin Coding Note\\nUnpin Coding Note\\n'; exit 0; fi
n=0
if [ -f "$STUB_RUNS" ]; then n=$(cat "$STUB_RUNS"); fi
printf '%s' "$((n+1))" > "$STUB_RUNS"
if [ "${STUB_SHORTCUT_FAIL:-0}" = "1" ]; then exit 1; fi
exit 0
"""

#  the Shortcuts app is up, so nothing is opened, and every sleep returns at once
PGREP_STUB = "#!/bin/bash\nexit 0\n"
OPEN_STUB = "#!/bin/bash\nexit 0\n"
SLEEP_STUB = "#!/bin/bash\nexit 0\n"

REAL_BODY = (
    '<div><b><font face=".AppleSystemUIFontBold"><span style="font-size: 24px">[AFC, MM] Relay Live Test</span>'
    "</font></b></div>\n<div>Tue, Oct 20, 3:05am</div>\n<div>Session: mvs_1e43abcd</div>\n<div><br></div>\n"
    "<div>content BODY-TEXT-MARKER \u2b50\ufe0f done</div>\n"
)
NOTES = [
    {
        "title": "[AFC, MM] Relay Live Test",
        "id": "x-coredata://stub/ICNote/p101",
        "created": "2026-10-20T08:05:00.000Z",
        "modified": "2026-10-20T08:05:30.000Z",
        "body": REAL_BODY,
        "plaintext": "[AFC, MM] Relay Live Test\nTue, Oct 20, 3:05am\ncontent BODY-TEXT-MARKER done",
    },
    {
        "title": "\u2b50\ufe0f Background Jobs Master List",
        "id": "x-coredata://stub/ICNote/p7",
        "created": "2026-08-01T14:00:00.000Z",
        "modified": "2026-10-22T15:02:00.000Z",
        "body": "<div>master list</div>",
        "plaintext": "master list",
    },
    {
        "title": "[AFC, Claude] Notes Relay Retirement",
        "id": "x-coredata://stub/ICNote/p230",
        "created": "2026-10-22T13:00:00.000Z",
        "modified": "2026-10-22T14:10:00.000Z",
        "body": "<div>plan</div>",
        "plaintext": "plan",
    },
    {
        "title": "[CT, Codex] relay cutover",
        "id": "x-coredata://stub/ICNote/p88",
        "created": "2026-09-30T10:00:00.000Z",
        "modified": "2026-10-01T09:00:00.000Z",
        "body": "<div>cutover</div>",
        "plaintext": "cutover",
    },
    {
        "title": "[UM, Grok] TestFlight First Ship",
        "id": "x-coredata://stub/ICNote/p3",
        "created": "2026-08-09T20:52:00.000Z",
        "modified": "2026-08-09T20:52:00.000Z",
        "body": "<div>ship</div>",
        "plaintext": "ship",
    },
]


@unittest.skipUnless(
    SCRIPT.is_file() and Path("/usr/bin/python3").exists(), "needs the script and /usr/bin/python3"
)
class ActionsTestCase(unittest.TestCase):
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
            #  one Shortcut attempt, and no fleet_knobs lookup
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

    def calls(self) -> list[dict]:
        found = sorted(self.osa.glob("call.*.json"), key=lambda p: int(p.name.split(".")[1]))
        return [json.loads(p.read_text()) for p in found]

    def shortcut_runs(self) -> int:
        return int(self.runs.read_text()) if self.runs.exists() else 0

    def log_lines(self, path: Path | None = None) -> list[dict]:
        path = path or self.log
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def only_log_line(self) -> dict:
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, lines)
        return lines[0]


class ReadTests(ActionsTestCase):
    def test_read_prints_the_note_as_one_json_object_and_exits_0(self):
        note = NOTES[0]
        for args in (("--read", "--title", note["title"]), ("--read", note["title"])):
            with self.subTest(args=args):
                p = self.run_script(*args)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout), note)
                self.assertEqual(
                    set(json.loads(p.stdout)),
                    {"title", "id", "created", "modified", "body", "plaintext"},
                )
        #  nothing but the JXA read reached osascript, and nothing pinned, showed or activated anything
        for call in self.calls():
            self.assertEqual(call["argv"], ["-l", "JavaScript"])
            self.assertEqual(call["env"]["APPLE_NOTES_JXA_MODE"], "read")
            self.assertNotIn("activate", call["source"])
            self.assertNotIn(".show", call["source"])
        self.assertEqual(self.shortcut_runs(), 0)

    def test_read_needs_no_session_id(self):
        """A CLAUDE seat with no session anywhere is refused a write, but may read."""
        p = self.run_script("--read", NOTES[2]["title"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["id"], NOTES[2]["id"])

    def test_the_title_reaches_the_jxa_program_through_the_environment_only(self):
        title = '[AFC, Claude] It\'s "quoted" $(touch pwned) `tick` \');Application("Finder")//'
        p = self.run_script("--read", "--title", title)
        self.assertEqual(p.returncode, 3, p.stderr)
        (call,) = self.calls()
        self.assertEqual(call["env"]["APPLE_NOTES_TITLE"], title)
        self.assertNotIn("pwned", call["source"])
        self.assertNotIn("tick", call["source"])
        self.assertFalse((self.tmp / "pwned").exists())

    def test_read_of_a_missing_note_exits_3_with_nothing_on_stdout(self):
        p = self.run_script("--read", "[AFC, Claude] No Such Note")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertIn("no note with that title in the Coding folder", p.stderr)

    def test_a_failed_osascript_read_is_an_error_with_nothing_on_stdout(self):
        p = self.run_script("--read", NOTES[0]["title"], STUB_OSA_FAIL="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, "")
        self.assertIn("the read did not complete", p.stderr)

    def test_read_takes_the_title_and_nothing_else(self):
        p = self.run_script("--read", NOTES[0]["title"], "a body")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])


class ListTests(ActionsTestCase):
    def listed(self, *args: str) -> dict:
        p = self.run_script("--list", *args)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_list_is_newest_change_first_with_the_four_fields(self):
        out = self.listed()
        newest_first = sorted(NOTES, key=lambda n: n["modified"], reverse=True)
        self.assertEqual([row["title"] for row in out["notes"]], [n["title"] for n in newest_first])
        self.assertEqual(out["total"], len(NOTES))
        self.assertIs(out["truncated"], False)
        for row in out["notes"]:
            self.assertEqual(set(row), {"title", "id", "created", "modified"})
        self.assertEqual(out["notes"][0]["id"], NOTES[1]["id"])
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["-l", "JavaScript"])
        self.assertEqual(call["env"]["APPLE_NOTES_JXA_MODE"], "list")

    def test_query_is_a_case_insensitive_title_substring(self):
        out = self.listed("--query", "RELAY")
        self.assertEqual(
            [row["title"] for row in out["notes"]],
            [
                "[AFC, Claude] Notes Relay Retirement",
                "[AFC, MM] Relay Live Test",
                "[CT, Codex] relay cutover",
            ],
        )
        self.assertEqual(out["total"], 3)
        self.assertEqual(self.listed("--query", "master list")["notes"][0]["id"], NOTES[1]["id"])
        self.assertEqual(self.listed("--query", "nothing matches this")["notes"], [])
        #  the query never reaches the JXA program:  it is applied after the read
        for call in self.calls():
            self.assertNotIn("RELAY", call["source"])

    def test_limit_cuts_the_list_and_says_so(self):
        out = self.listed("--limit", "2")
        self.assertEqual(len(out["notes"]), 2)
        self.assertEqual(out["total"], len(NOTES))
        self.assertIs(out["truncated"], True)
        self.assertEqual([row["id"] for row in out["notes"]], [NOTES[1]["id"], NOTES[2]["id"]])
        out = self.listed("--query", "relay", "--limit", "1")
        self.assertEqual([row["id"] for row in out["notes"]], [NOTES[2]["id"]])
        self.assertEqual(out["total"], 3)
        self.assertIs(out["truncated"], True)
        out = self.listed("--limit", "500")
        self.assertEqual(len(out["notes"]), len(NOTES))
        self.assertIs(out["truncated"], False)

    def test_the_default_limit_is_100(self):
        many = [
            {
                "title": "[AFC, Claude] Note %03d" % i,
                "id": "x-coredata://stub/ICNote/m%d" % i,
                "created": "2026-10-01T00:00:00.000Z",
                "modified": "2026-10-%02dT%02d:00:00.000Z" % (1 + i // 24, i % 24),
                "body": "",
                "plaintext": "",
            }
            for i in range(130)
        ]
        self.notes_file.write_text(json.dumps(many))
        out = self.listed()
        self.assertEqual(len(out["notes"]), 100)
        self.assertEqual(out["total"], 130)
        self.assertIs(out["truncated"], True)
        self.assertEqual(out["notes"][0]["title"], "[AFC, Claude] Note 129")

    def test_a_bad_limit_exits_2_before_notes_is_asked(self):
        for value in ("0", "501", "abc", "", "-5", "1.5", "1e2", "99999999999999999999"):
            with self.subTest(limit=value):
                p = self.run_script("--list", "--limit", value)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertIn("--limit", p.stderr)
        p = self.run_script("--list", "--limit")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_query_and_limit_go_with_list_only(self):
        for args in (("--query", "x", "--read", "T"), ("--limit", "5", "--pin-only", "T")):
            with self.subTest(args=args):
                p = self.run_script(*args)
                self.assertEqual(p.returncode, 2)
        p = self.run_script("--list", "[AFC, Claude] a title")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])


class ExistingOnlyTests(ActionsTestCase):
    def test_update_existing_only_of_a_missing_note_exits_3_and_never_creates_it(self):
        p = self.run_script(
            "--update",
            "--existing-only",
            "--session",
            "test-session-1234",
            "[AFC, Claude] Not There",
            "Body text.",
        )
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("no note with that title in the Coding folder", p.stderr)
        self.assertNotIn("updated note id=", p.stdout)
        self.assertNotIn("created note id=", p.stdout)
        (call,) = self.calls()
        self.assertEqual(call["env"]["EXISTING_ONLY"], "1")
        self.assertNotIn("wrote", call)
        #  no pin either:  there is no note to pin
        self.assertEqual(self.shortcut_runs(), 0)

    def test_update_existing_only_of_a_note_that_exists_updates_it(self):
        p = self.run_script(
            "--existing-only",
            "--update",
            "--session",
            "test-session-1234",
            NOTES[2]["title"],
            "Body text.",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("updated note id=x-coredata://stub/note/1", p.stdout)
        (call,) = self.calls()
        self.assertEqual(call["wrote"], "update")

    def test_update_without_the_flag_still_creates_a_missing_note(self):
        p = self.run_script(
            "--update", "--session", "test-session-1234", "[AFC, Claude] Not There", "Body text."
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("updated note id=x-coredata://stub/note/new", p.stdout)
        (call,) = self.calls()
        self.assertEqual(call["env"]["EXISTING_ONLY"], "0")
        self.assertEqual(call["wrote"], "create")

    def test_the_check_returns_before_both_makes_in_the_same_applescript(self):
        """The not-found answer comes from inside the one AppleEvent that would have made the folder or the note."""
        self.run_script(
            "--update",
            "--existing-only",
            "--session",
            "test-session-1234",
            "[AFC, Claude] X",
            "Body.",
        )
        (call,) = self.calls()
        lines = [line.strip() for line in call["source"].splitlines()]
        check = 'if (system attribute "EXISTING_ONLY") is "1" then return "%s"' % NOT_FOUND
        for make in ("set codingFolder to make new folder", "set targetNote to make new note"):
            at = next(i for i, line in enumerate(lines) if line.startswith(make))
            branch = lines[max(0, at - 3) : at]
            self.assertIn(check, branch, f"no existing-only check right before {make!r}")
        self.assertEqual(sum(1 for line in lines if line == check), 2)

    def test_existing_only_without_update_exits_2(self):
        for args in (
            ("--existing-only", "--session", "test-session-1234", "[AFC, Claude] X", "Body."),
            ("--existing-only", "--pin-only", "[AFC, Claude] X"),
            ("--read", "--existing-only", NOTES[0]["title"]),
            ("--existing-only", "--list"),
        ):
            with self.subTest(args=args):
                p = self.run_script(*args)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("--existing-only goes with --update", p.stderr)
        self.assertEqual(self.calls(), [])


class ActionLogTests(ActionsTestCase):
    def assert_shape(self, entry: dict) -> None:
        self.assertTrue(LOG_FIELDS <= set(entry), LOG_FIELDS - set(entry))
        self.assertEqual(entry["v"], 1)
        self.assertRegex(entry["id"], r"^[0-9a-f]{32}$")
        self.assertIsNotNone(datetime.fromisoformat(entry["ts"]).tzinfo, entry["ts"])
        self.assertEqual(entry["host"], socket.gethostname().split(".")[0])
        self.assertIsInstance(entry["duration_ms"], int)
        self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertEqual(set(entry["flags"]), FLAG_FIELDS)

    def test_a_write_logs_one_line_with_every_field_and_no_note_text(self):
        body = "Shipped on 2026-10-19. Next step BODY-TEXT-MARKER \u2713 here."
        p = self.run_script(
            "--session",
            "test-session-1234",
            "--pr",
            "46",
            "--summary",
            "SUMMARY-TEXT-MARKER",
            "[AFC, Claude] Log Probe",
            body,
            AGENT_NOTES_CALLER="runner",
            AGENT_NOTES_REQUEST_ID="nr0123456789abcdef0123",
            AGENT_NOTES_REQUESTER="CODEX",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        raw = self.log.read_text()
        for text in ("BODY-TEXT-MARKER", "SUMMARY-TEXT-MARKER", "Shipped on", "Next step"):
            self.assertNotIn(text, raw)
        entry = self.only_log_line()
        self.assert_shape(entry)
        (call,) = self.calls()
        self.assertEqual(entry["mode"], "create")
        self.assertEqual(entry["title"], "[AFC, Claude] Log Probe")
        self.assertEqual(entry["note_id"], "x-coredata://stub/note/new")
        self.assertEqual((entry["exit"], entry["outcome"]), (0, "done"))
        self.assertEqual(entry["seat"], "CLAUDE")
        self.assertEqual(entry["session"], "test-session-1234")
        self.assertEqual(entry["caller"], "runner")
        self.assertEqual(entry["request_id"], "nr0123456789abcdef0123")
        self.assertEqual(entry["requester"], "CODEX")
        self.assertEqual(entry["input_bytes"], len(body.encode("utf-8")))
        #  the size and hash of exactly the HTML the script handed Notes
        self.assertEqual(entry["body_bytes"], call["html_bytes"])
        self.assertEqual(entry["body_sha256"], call["html_sha256"])
        #  the linkifier's own count rides along with the other rewrites (AFC-547)
        self.assertEqual(
            entry["reformatted"],
            {"dates": 1, "gaps": 1, "links": {"issues": 0, "prs": 1, "projects": 0, "fleetlink": 0, "swu": 0, "repos": 0, "pagerduty": 0, "sentry": 0, "cursor": 0}},
        )
        self.assertEqual(entry["pin"], "pinned")
        self.assertEqual(
            entry["flags"],
            {
                "notify": False,
                "needs_owner": False,
                "html": False,
                "existing_only": False,
                "summary": True,
                "pr": "46",
            },
        )
        self.assertNotIn("count", entry)
        self.assertNotIn("bytes", entry)

    def test_the_trap_still_removes_the_temp_files(self):
        """The log and the cleanup share one EXIT trap, so neither replaces the other."""
        p = self.run_script(
            "--update", "--session", "test-session-1234", NOTES[2]["title"], "Body."
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        (call,) = self.calls()
        self.assertFalse(
            Path(call["html_path"]).exists(), "the note HTML temp file was left behind"
        )
        self.assertFalse(Path(call["title_file"]).exists(), "the title temp file was left behind")
        self.assertEqual(self.only_log_line()["mode"], "update")

    def test_read_and_list_log_their_size_and_count(self):
        self.run_script("--read", NOTES[0]["title"], CLAUDE_CODE_SESSION_ID="claude-session-0001")
        self.run_script("--list", "--query", "relay")
        read, listed = self.log_lines()
        self.assert_shape(read)
        self.assertEqual((read["mode"], read["outcome"], read["exit"]), ("read", "done", 0))
        self.assertEqual(read["note_id"], NOTES[0]["id"])
        self.assertEqual(read["bytes"], len(REAL_BODY.encode("utf-8")))
        self.assertEqual(read["session"], "claude-session-0001")
        self.assertIsNone(read["input_bytes"])
        self.assertIsNone(read["body_sha256"])
        self.assertIsNone(read["pin"])
        self.assertNotIn("BODY-TEXT-MARKER", json.dumps(read))
        self.assertEqual((listed["mode"], listed["outcome"], listed["count"]), ("list", "done", 3))
        self.assertIsNone(listed["title"])
        self.assertEqual(listed["caller"], "script")
        self.assertIsNone(listed["request_id"])

    def test_every_failure_logs_one_line_with_its_outcome(self):
        cases = (
            (("--read", "[AFC, Claude] No Such Note"), {}, 3, "not_found", "read"),
            (("--list", "--limit", "0"), {}, 2, "usage", "list"),
            (("[AFC, Claude] No Session", "Body."), {}, 2, "usage", "create"),
            (("--existing-only", "[AFC, Claude] X"), {}, 2, "usage", "create"),
            (
                (
                    "--update",
                    "--existing-only",
                    "--session",
                    "test-session-1234",
                    "[AFC, Claude] Gone",
                    "Body.",
                ),
                {},
                3,
                "not_found",
                "update",
            ),
            (
                ("--pin-only", NOTES[2]["title"]),
                {"STUB_SHORTCUT_FAIL": "1"},
                4,
                "pin_failed",
                "pin",
            ),
            (("--pin-only", "[AFC, Claude] Gone"), {}, 3, "not_found", "pin"),
            (("--read", NOTES[0]["title"]), {"STUB_OSA_FAIL": "1"}, 1, "error", "read"),
        )
        for args, env, code, outcome, mode in cases:
            with self.subTest(args=args):
                if self.log.exists():
                    self.log.unlink()
                p = self.run_script(*args, **env)
                self.assertEqual(p.returncode, code, p.stdout + p.stderr)
                entry = self.only_log_line()
                self.assert_shape(entry)
                self.assertEqual(
                    (entry["exit"], entry["outcome"], entry["mode"]), (code, outcome, mode)
                )
                self.assertIsNone(entry["body_sha256"])
        self.assertEqual(entry["pin"], None)

    def test_a_skipped_pin_is_logged_on_a_write_that_still_succeeds(self):
        p = self.run_script(
            "--update",
            "--session",
            "test-session-1234",
            NOTES[2]["title"],
            "Body.",
            STUB_SHORTCUT_FAIL="1",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        entry = self.only_log_line()
        self.assertEqual((entry["outcome"], entry["pin"]), ("done", "skipped"))
        p = self.run_script(
            "--update",
            "--session",
            "test-session-1234",
            NOTES[2]["title"],
            "Body.",
            APPLE_NOTES_PIN="0",
        )
        self.assertEqual(self.log_lines()[-1]["pin"], "off")
        p = self.run_script("--pin-only", NOTES[2]["title"], STUB_SHORTCUT_FAIL="1")
        self.assertEqual(self.log_lines()[-1]["pin"], "skipped")

    def test_a_title_the_secret_scan_refuses_is_never_logged(self):
        # Built at run time from one repeated letter:  plainly synthetic, and still the shape the scan refuses.
        secret = "ghp_" + "a" * 36
        p = self.run_script("--read", f"[AFC, Claude] {secret}")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn(secret, self.log.read_text())
        entry = self.only_log_line()
        self.assertIsNone(entry["title"])
        self.assertEqual(entry["outcome"], "usage")

    def test_the_seat_falls_back_to_the_agent_in_the_title(self):
        p = self.run_script("[AFC, Wise] Seat Probe", "Body.", AGENT_SEAT=None)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.only_log_line()["seat"], "WISE")

    def test_an_unknown_flag_is_not_past_parsing_and_logs_nothing(self):
        p = self.run_script("--no-such-flag", "T")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.log_lines(), [])

    def test_off_writes_nothing(self):
        for value in ("off", "OFF"):
            with self.subTest(value=value):
                p = self.run_script("--read", NOTES[0]["title"], APPLE_NOTES_ACTION_LOG=value)
                self.assertEqual(p.returncode, 0, p.stderr)
                p = self.run_script(
                    "--read", "[AFC, Claude] No Such Note", APPLE_NOTES_ACTION_LOG=value
                )
                self.assertEqual(p.returncode, 3)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.tmp / "Library").exists())
                self.assertFalse((self.tmp / value).exists())

    def test_a_preview_logs_nothing(self):
        p = self.run_script(
            "--session",
            "test-session-1234",
            "[AFC, Claude] Preview",
            "Body.",
            APPLE_NOTES_PRINT_HTML="1",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.log_lines(), [])

    def test_the_default_log_is_private_to_the_owner(self):
        p = self.run_script("--read", NOTES[0]["title"], APPLE_NOTES_ACTION_LOG=None)
        self.assertEqual(p.returncode, 0, p.stderr)
        default = self.tmp / "Library" / "Logs" / "fleet" / "apple-notes-actions.jsonl"
        self.assertEqual(len(self.log_lines(default)), 1)
        self.assertEqual(default.stat().st_mode & 0o777, 0o600)
        self.assertEqual(default.parent.stat().st_mode & 0o777, 0o700)
        #  an existing file with a wider mode is narrowed on the next append
        default.chmod(0o644)
        self.run_script("--read", NOTES[0]["title"], APPLE_NOTES_ACTION_LOG=None)
        self.assertEqual(default.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.log_lines(default)), 2)

    def test_the_log_rotates_at_5_mb_and_keeps_five(self):
        self.log.parent.mkdir(mode=0o700)
        self.log.write_bytes(
            b"x" * (ROTATE_AT - 1) + b"\n"
        )  # whole lines:  a torn one would be ended first
        for n in range(1, 6):
            Path(f"{self.log}.{n}").write_text(f"old {n}\n")
        p = self.run_script("--read", NOTES[0]["title"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.log_lines()), 1)
        self.assertEqual(Path(f"{self.log}.1").stat().st_size, ROTATE_AT)
        for n in range(2, 6):
            self.assertEqual(Path(f"{self.log}.{n}").read_text(), f"old {n - 1}\n")
        self.assertFalse(Path(f"{self.log}.6").exists())

    def test_a_log_under_5_mb_is_appended_to(self):
        self.log.parent.mkdir(mode=0o700)
        self.log.write_bytes(b"x" * (ROTATE_AT - 10) + b"\n")
        self.run_script("--read", NOTES[0]["title"])
        self.assertFalse(Path(f"{self.log}.1").exists())
        self.assertGreater(self.log.stat().st_size, ROTATE_AT)
        #  and the next run, past the size now, rotates
        self.run_script("--read", NOTES[0]["title"])
        self.assertTrue(Path(f"{self.log}.1").exists())
        self.assertEqual(len(self.log_lines()), 1)

    def test_a_line_torn_by_an_earlier_write_is_ended_before_the_next_one(self):
        # A short write (a full disk) or a crash can leave part of a line with no newline:  the next line starts on a
        # line of its own, or the two would read as one line that is no entry and the runner would never send it.
        self.log.parent.mkdir(mode=0o700)
        self.log.write_bytes(b'{"v":1,"id":"torn')
        p = self.run_script("--read", NOTES[0]["title"])
        self.assertEqual(p.returncode, 0, p.stderr)
        torn, whole, rest = self.log.read_bytes().split(b"\n", 2)
        self.assertEqual(torn, b'{"v":1,"id":"torn')
        self.assertEqual(json.loads(whole)["mode"], "read")  # the new line parses on its own
        self.assertEqual(rest, b"")

    def test_a_torn_line_is_ended_before_its_file_rotates(self):
        # A whole entry that lost only its newline, in a log due to rotate:  it is ended in place first, so the copy it
        # rotates into holds it as a whole line the runner sends.
        self.log.parent.mkdir(mode=0o700)
        entry = b'{"v":1,"id":"' + b"e" * 32 + b'","ts":"2026-10-22T10:00:00Z"}'
        self.log.write_bytes(b"x" * (ROTATE_AT - len(entry) - 1) + b"\n" + entry)
        p = self.run_script("--read", NOTES[0]["title"])
        self.assertEqual(p.returncode, 0, p.stderr)
        rotated = Path(f"{self.log}.1").read_bytes()
        self.assertTrue(rotated.endswith(entry + b"\n"), rotated[-80:])
        self.assertEqual(len(self.log_lines()), 1)  # the new log holds only this run's line

    def test_a_log_only_its_owner_may_write_still_gets_the_line_and_its_mode_back(self):
        self.log.parent.mkdir(mode=0o700)
        self.log.write_bytes(b"")
        self.log.chmod(
            0o200
        )  # write only:  the torn-line check cannot read it, and the line is still written
        p = self.run_script("--read", NOTES[0]["title"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.log_lines()), 1)

    def test_an_unwritable_log_leaves_the_exit_code_and_the_output_alone(self):
        blocker = self.tmp / "a-file"
        blocker.write_text("not a directory")
        a_directory = self.tmp / "a-directory"
        a_directory.mkdir()
        runs = (
            ("--read", NOTES[0]["title"]),
            ("--read", "[AFC, Claude] No Such Note"),
            ("--list", "--limit", "2"),
            (
                "--update",
                "--session",
                "test-session-1234",
                NOTES[2]["title"],
                "Body.  2026-10-19 done.",
            ),
            ("--list", "--limit", "0"),
        )
        for unwritable in (str(blocker / "actions.jsonl"), str(a_directory)):
            for args in runs:
                with self.subTest(log=unwritable, args=args):
                    quiet = self.run_script(*args, APPLE_NOTES_ACTION_LOG="off")
                    broken = self.run_script(*args, APPLE_NOTES_ACTION_LOG=unwritable)
                    self.assertEqual(broken.returncode, quiet.returncode)
                    self.assertEqual(broken.stdout, quiet.stdout)
                    self.assertEqual(broken.stderr, quiet.stderr)
        self.assertEqual(blocker.read_text(), "not a directory")


def _jxa_program() -> str:
    found = re.search(
        r"osascript -l JavaScript <<'JXA'\n(.*?)\nJXA\n", SCRIPT.read_text(), re.DOTALL
    )
    assert found, "the quoted JXA heredoc is gone from apple-notes-coding.sh"
    return found.group(1)


@unittest.skipUnless(SCRIPT.is_file(), "needs scripts/apple-notes-coding.sh")
class JxaSourceTests(unittest.TestCase):
    def test_the_program_is_plain_ascii(self):
        """osascript is handed the program on stdin, and its reading of anything past ASCII is not something to
        lean on:  a stray character in the escape regex would change what the JSON escapes."""
        program = _jxa_program()
        self.assertTrue(program.isascii(), sorted({hex(ord(c)) for c in program if ord(c) > 127}))

    def test_the_program_never_shows_or_activates_notes(self):
        program = _jxa_program()
        for needle in ("activate", ".show", "System Events", "doShellScript"):
            self.assertNotIn(needle, program)


#  The JXA program under node, with a fake of the slice of the Notes object model it uses.  `Application`, `ObjC`
#  and `$` are the JXA globals;  the program's run() is called the way osascript calls it and its answer printed.
NODE_PRELUDE = r"""
const fixture = JSON.parse(process.env.FAKE_NOTES || "[]");
const folderThere = process.env.FAKE_FOLDER !== "0";
let idReads = 0;
globalThis.ObjC = { import() {}, unwrap(value) { return value; } };
globalThis.$ = { NSProcessInfo: { processInfo: { environment: { objectForKey(key) { return process.env[key]; } } } } };
function spec(note) {
  return {
    exists: () => true,
    name: () => note.title,
    id: () => note.id,
    creationDate: () => new Date(note.created),
    modificationDate: () => new Date(note.modified),
    body: () => note.body,
    plaintext: () => note.plaintext,
  };
}
const notes = {
  byName(title) {
    const note = fixture.find((n) => n.title === title);
    return note ? spec(note) : { exists: () => false };
  },
  id() {
    idReads += 1;
    const ids = fixture.map((n) => n.id);
    return process.env.FAKE_CHURN === "1" && idReads % 2 === 0 ? ids.reverse() : ids;
  },
  name: () => fixture.map((n) => n.title),
  creationDate: () => fixture.map((n) => new Date(n.created)),
  modificationDate: () => fixture.map((n) => new Date(n.modified)),
};
globalThis.Application = (name) => {
  if (name !== "Notes") throw new Error("unexpected application " + name);
  const coding = (account, folder) => ({
    exists: () => folderThere && account === "iCloud" && folder === "Coding",
    notes,
  });
  return {
    accounts: {
      byName: (account) => ({ folders: { byName: (folder) => coding(account, folder) } }),
    },
  };
};
"""


@unittest.skipUnless(SCRIPT.is_file() and shutil.which("node"), "needs node")
class JxaProgramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.program = _jxa_program()

    def run_jxa(self, mode: str, title: str = "", **env: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "jxa.js"
            harness.write_text(
                NODE_PRELUDE + self.program + '\nprocess.stdout.write(String(run()) + "\\n");\n'
            )
            child = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "FAKE_NOTES": json.dumps(NOTES),
                "APPLE_NOTES_JXA_MODE": mode,
                "APPLE_NOTES_TITLE": title,
            }
            child.update(env)
            return subprocess.run(
                ["node", str(harness)],
                capture_output=True,
                text=True,
                env=child,
                timeout=60,
                check=False,
            )

    def test_read_answers_the_note_in_plain_ascii_json(self):
        for note in NOTES[:2]:
            with self.subTest(title=note["title"]):
                p = self.run_jxa("read", note["title"])
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertTrue(p.stdout.isascii(), p.stdout[:200])
                self.assertEqual(json.loads(p.stdout), note)

    def test_read_of_a_missing_note_or_folder_answers_the_sentinel(self):
        self.assertEqual(
            self.run_jxa("read", "[AFC, Claude] No Such Note").stdout.strip(), NOT_FOUND
        )
        self.assertEqual(
            self.run_jxa("read", NOTES[0]["title"], FAKE_FOLDER="0").stdout.strip(), NOT_FOUND
        )

    def test_list_answers_four_aligned_columns(self):
        p = self.run_jxa("list")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.isascii())
        out = json.loads(p.stdout)
        self.assertEqual(out["names"], [n["title"] for n in NOTES])
        self.assertEqual(out["ids"], [n["id"] for n in NOTES])
        self.assertEqual(out["created"], [n["created"] for n in NOTES])
        self.assertEqual(out["modified"], [n["modified"] for n in NOTES])
        empty = json.loads(self.run_jxa("list", FAKE_FOLDER="0").stdout)
        self.assertEqual(empty, {"names": [], "ids": [], "created": [], "modified": []})

    def test_a_folder_that_keeps_changing_is_an_error_not_a_misaligned_list(self):
        p = self.run_jxa("list", FAKE_CHURN="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("kept changing", p.stderr)
        self.assertEqual(p.stdout, "")


if __name__ == "__main__":
    unittest.main()
