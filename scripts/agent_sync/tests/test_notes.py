"""Apple Notes from any host (AFC-486, docs/protocols/operator-notes.md):  the request object, the title and flag
rules, who may send what, the direct runner, the provenance line, the content a read returns and the action log.

The script is never run for real here:  every test injects the subprocess runner, so no osascript, no Notes.app and
no iCloud folder is touched.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_sync import notes as N
from agent_sync.idem import IdemStore

TITLE = "[AFC, Claude] Relay Check"
MASTER = "⭐️ Background Jobs Master List"
SESSION = "11112222-3333-4444-5555-666677778888"
RID = "requestid-0001"

# A note shaped as Notes stores it (ids made up):  written by the old #notes-relay for MM, so it ends with the relay's
# footer.  STORED_HTML is the `body` of `apple-notes-coding.sh --read`, STORED_TEXT its `plaintext`.
STORED_TITLE = "[AFC, MM] Relay Live Test"
STORED_HTML = (
    '<div><b><font face=".AppleSystemUIFontBold"><span style="font-size: 24px">[AFC, MM] Relay Live Test</span>'
    "</font></b></div>\n"
    "<div>Tue, Oct 20, 3:05am</div>\n"
    "<div>Session: mvs_00000000example0session00000000</div>\n"
    "<div><br></div>\n"
    "<div>Live end-to-end test of the #notes-relay consumer on this Mac.</div>\n"
    "<div><br></div>\n"
    "<div>Disposable.  If you can read this note, the relay wrote a note end to end.</div>\n"
    "<div><br></div>\n"
    "<div><br><br></div>\n"
    "<div><br></div>\n"
    "<div><i>Relayed for MM by the notes relay on Tue, Oct 20, 2026: </i><i><u>"
    '<a href="https://chat.example.test/#narrow/id/100000001">request message</a></u></i><i>.</i><br></div>\n'
    "<div><br></div>\n"
)
STORED_TEXT = (
    "[AFC, MM] Relay Live Test\n"
    "Tue, Oct 20, 3:05am\n"
    "Session: mvs_00000000example0session00000000\n"
    "\n"
    "Live end-to-end test of the #notes-relay consumer on this Mac.\n"
    "\n"
    "Disposable.  If you can read this note, the relay wrote a note end to end.\n"
    "\n"
    "\ufffc\n"
    "\n"
    "Relayed for MM by the notes relay on Tue, Oct 20, 2026: request message.\n"
    "\n"
)
STORED_CONTENT_HTML = (
    "<div>Live end-to-end test of the #notes-relay consumer on this Mac.</div>\n"
    "<div><br></div>\n"
    "<div>Disposable.  If you can read this note, the relay wrote a note end to end.</div>"
)
STORED_CONTENT_TEXT = (
    "Live end-to-end test of the #notes-relay consumer on this Mac.\n"
    "\n"
    "Disposable.  If you can read this note, the relay wrote a note end to end."
)


def read_answer(title: str = STORED_TITLE, body: str = STORED_HTML, plaintext: str = STORED_TEXT) -> str:
    """What `apple-notes-coding.sh --read` prints."""
    return json.dumps(
        {
            "title": title,
            "id": "x-coredata://note/p1",
            "created": "2026-10-20T08:05:00Z",
            "modified": "2026-10-22T22:03:00Z",
            "body": body,
            "plaintext": plaintext,
        }
    )


class FakeRun:
    """Stands in for subprocess.run:  records argv, stdin, env and the html file's mode, returns a canned result."""

    def __init__(
        self,
        code: int = 0,
        stdout: str = "created note id=x-coredata://abc in folder Coding\n",
        stderr: str = "",
    ):
        self.calls: list[dict] = []
        self.result = (code, stdout, stderr)
        self.html_mode = None
        self.html_body = None

    def __call__(self, argv, **kwargs):
        call = {
            "argv": list(argv),
            "input": kwargs.get("input"),
            "env": kwargs.get("env"),
            "kwargs": kwargs,
        }
        if "--html" in argv:
            path = argv[argv.index("--html") + 1]
            self.html_mode = stat.S_IMODE(os.stat(path).st_mode)
            self.html_body = Path(path).read_bytes()
        self.calls.append(call)
        code, out, err = self.result
        return subprocess.CompletedProcess(argv, code, out.encode(), err.encode())


def request(action="create", title=TITLE, markdown="body", flags=None, rid=RID, identity="CLAUDE", session=None, **extra):
    if action not in N.BODY_ACTIONS:
        markdown, flags = "", None
    if action == "list":
        title = None
    return N.make_request(
        action, identity, request_id=rid, title=title, markdown=markdown, flags=flags, session=session, **extra
    )


class TitleTests(unittest.TestCase):
    def refuses(self, title, action="create"):
        with self.assertRaises(N.NotesError) as caught:
            N.check_title(title, action)
        self.assertEqual(caught.exception.code, "invalid_argument")
        return caught.exception.message

    def test_a_create_needs_the_team_key_and_agent_grammar(self):
        self.assertEqual(N.check_title("  " + TITLE + "  "), TITLE)
        for good in (
            "[AFC, CLAUDE] relay rollout",
            "[AFC, CT, GROK-WEB] notes",
            "[UM, MM] Topic",
            "[AFC, Grok Web] Topic",
            "[AFC2, Claude] digits in a team key",
        ):
            with self.subTest(good):
                self.assertEqual(N.check_title(good, "create"), good)
        for bad, why in (
            ("[AFC", "unclosed"),
            ("[] Topic", "empty scope"),
            ("[AFC] Topic", "no agent"),
            ("[AFC, ] Topic", "empty agent"),
            ("[AFC, Claude]", "no topic"),
            ("[afc, Claude] Topic", "team key not capitals"),
            ("[AFC-2, Claude] x", "hyphen is not a team key"),
            ("[AFC,  Claude] Topic", "two spaces"),
            (MASTER, "a static note is not a new note's title"),
        ):
            with self.subTest(why):
                self.assertIn("[TEAM_KEY, Agent] Topic", self.refuses(bad, "create"))

    def test_every_other_action_names_any_existing_note(self):
        for action in ("update", "pin", "unpin", "read"):
            for title in (MASTER, "[AFC, Codex] Server Secret Access", "Plain Title", "[AFC] no agent"):
                with self.subTest(action=action, title=title):
                    self.assertEqual(N.check_title(title, action), title)

    def test_every_action_refuses_what_is_special_to_applescript_or_reads_as_a_flag(self):
        for action in ("create", "update", "pin", "unpin", "read"):
            with self.subTest(action):
                self.assertIn("must not be empty", self.refuses("   ", action))
                self.assertIn("must not start with -", self.refuses("--update [AFC, Claude] x", action))
                self.assertIn("must not start with -", self.refuses(" -x", action))
                self.assertIn("double quote", self.refuses('[AFC, Claude] a "quoted" word', action))
                self.assertIn("backslash", self.refuses("[AFC, Claude] back\\slash", action))
                for control in ("\n", "\t", "\x00", "\x7f", "\x85", "\u2028", "\u2029"):
                    self.assertIn("control character", self.refuses("[AFC, Claude] a%sb" % control, action))
                self.assertIn("at most", self.refuses("[AFC, Claude] " + "x" * 300, action))
                self.assertIn("must be a string", self.refuses(None, action))

    def test_the_schema_patterns_agree_with_check_title(self):
        import re

        tools = {
            t["name"]: t
            for t in json.loads((Path(N.__file__).resolve().parent / "mcp" / "notes-tools.json").read_text())["tools"]
        }
        samples = [
            TITLE,
            "[AFC, CT, CLAUDE] Topic",
            "[AFC, Grok Web] Topic",
            MASTER,
            "Plain Title",
            "[AFC] no agent",
            "-leading dash",
            'a "quote"',
            "back\\slash",
            "line\u2028break",
            "tab\there",
            "[afc, Claude] Topic",
        ]
        for name, action in (
            ("notes_create", "create"),
            ("notes_update", "update"),
            ("notes_pin", "pin"),
            ("notes_unpin", "unpin"),
            ("notes_read", "read"),
        ):
            pattern = tools[name]["inputSchema"]["properties"]["title"]["pattern"]
            for title in samples:
                with self.subTest(tool=name, title=title):
                    try:
                        N.check_title(title, action)
                        accepted = True
                    except N.NotesError:
                        accepted = False
                    self.assertEqual(re.search(pattern, title) is not None, accepted)
        self.assertIn("[AFC, Claude]", tools["notes_create"]["inputSchema"]["properties"]["title"]["description"])
        self.assertIn(MASTER, tools["notes_update"]["inputSchema"]["properties"]["title"]["description"])


class FlagTests(unittest.TestCase):
    def test_flags_are_a_closed_set_and_activate_is_not_in_it(self):
        self.assertEqual(
            N.check_flags({"pin": False, "summary": "short", "pr": "46, 47"}),
            {"pin": False, "summary": "short", "pr": "46, 47"},
        )
        self.assertEqual(N.check_flags({"html": True, "notify": True, "needs_owner": False}), {"html": True, "notify": True})
        # A flag at its default is left out, so a tool that fills the schema defaults in and simple-notes, which does
        # not, send the same request (and the same digest) for the same note.
        self.assertEqual(N.check_flags({"pin": True, "notify": False, "needs_owner": False, "html": False}), {})
        self.assertEqual(N.check_flags(None), {})
        self.assertEqual(N.check_flags({"summary": "", "pr": " "}), {}, "an empty summary or pr is left out")
        for bad in (
            {"activate": True},
            {"pin": "yes"},
            {"html": 1},
            {"summary": 'a "b"'},
            {"summary": "x" * 300},
            {"summary": "two\nlines"},
            {"pr": "46; rm -rf"},
            {"pr": 46},
        ):
            with self.subTest(bad), self.assertRaises(N.NotesError):
                N.check_flags(bad)

    def test_an_unknown_flag_name_is_shown_only_when_it_is_plain(self):
        with self.assertRaises(N.NotesError) as caught:
            N.check_flags({"activate": True})
        self.assertIn("'activate'", caught.exception.message)
        with self.assertRaises(N.NotesError) as caught:
            N.check_flags({"x\ny": True})
        self.assertIn("(name not shown)", caught.exception.message)

    def test_request_ids(self):
        self.assertRegex(N.new_request_id(), r"^nr[0-9a-f]{20}$")
        self.assertEqual(N.check_request_id("abcdefgh"), "abcdefgh")
        for bad in ("short", "has space 1234", "x" * 70, "-leading-dash", 12345678, None):
            with self.subTest(bad), self.assertRaises(N.NotesError):
                N.check_request_id(bad)

    def test_text_fields_cover_the_title_the_body_and_the_text_flags(self):
        req = request(markdown="body", flags={"summary": "sum", "pr": "46", "notify": True})
        self.assertEqual(N.text_fields(req), {"title": TITLE, "markdown": "body", "summary": "sum", "pr": "46"})
        self.assertEqual(N.text_fields(request("pin")), {"title": TITLE})


class RequestTests(unittest.TestCase):
    def test_each_action_carries_only_its_own_fields(self):
        self.assertEqual(
            request("create", markdown="b", flags={"pin": False}, session=SESSION),
            {
                "v": 2,
                "action": "create",
                "request_id": RID,
                "identity": "CLAUDE",
                "title": TITLE,
                "markdown": "b",
                "flags": {"pin": False},
                "session": SESSION,
            },
        )
        self.assertNotIn("session", request("update", title=MASTER), "an exempt writer's session is left out")
        self.assertEqual(
            request("pin", title=MASTER),
            {"v": 2, "action": "pin", "request_id": RID, "identity": "CLAUDE", "title": MASTER},
        )
        self.assertEqual(request("unpin")["action"], "unpin")
        self.assertEqual(request("read", title=MASTER)["format"], "html")
        self.assertEqual(request("read", title=MASTER, fmt="raw")["format"], "raw")
        self.assertEqual(
            request("list"),
            {"v": 2, "action": "list", "request_id": RID, "identity": "CLAUDE", "query": "", "limit": 100},
        )
        self.assertEqual(request("list", query=" Plan ", limit=5)["query"], "Plan")

    def test_make_request_refuses_a_field_the_action_does_not_use(self):
        for kwargs in (
            {"action": "pin", "markdown": "a body"},
            {"action": "unpin", "flags": {"pin": True}},
            {"action": "read", "markdown": "x"},
            {"action": "create", "fmt": "html"},
            {"action": "read", "query": "x"},
            {"action": "pin", "limit": 5},
            {"action": "delete"},
            {"action": "read", "fmt": "pdf"},
            {"action": "list", "limit": 0},
            {"action": "list", "limit": 501},
            {"action": "list", "limit": True},
            {"action": "list", "query": "x" * 101},
            {"action": "list", "query": 'a "q"'},
            {"action": "create", "markdown": 5},
            {"action": "create", "markdown": "x" * (N.MARKDOWN_MAX + 1)},
            {"action": "create", "session": "no"},
        ):
            action = kwargs.pop("action")
            with self.subTest(action=action, **{k: str(v)[:20] for k, v in kwargs.items()}):
                with self.assertRaises(N.NotesError) as caught:
                    N.make_request(action, "CLAUDE", request_id=RID, title=TITLE, **kwargs)
                self.assertEqual(caught.exception.code, "invalid_argument")
        for identity in ("claude", "", None, "A" * 40):
            with self.subTest(identity=identity), self.assertRaises(N.NotesError):
                N.make_request("pin", identity, request_id=RID, title=TITLE)
        self.assertEqual(len(request(markdown="x" * N.MARKDOWN_MAX)["markdown"]), N.MARKDOWN_MAX)

    def test_check_request_round_trips_and_refuses_unknown_unused_and_old_fields(self):
        for req in (
            request("create", markdown="b", flags={"html": True, "summary": "s"}, session=SESSION),
            request("update", title=MASTER, markdown="b", session=SESSION),
            request("pin", title=MASTER),
            request("unpin"),
            request("read", title=MASTER, fmt="text"),
            request("list", query="Plan", limit=7),
        ):
            with self.subTest(req["action"]):
                self.assertEqual(N.check_request(json.loads(json.dumps(req))), req)
        good = request("pin")
        for name, raw in (
            ("not an object", ["pin"]),
            ("unknown field", dict(good, transport="relay")),
            ("old upload field", dict(good, markdown_url="/user_uploads/x")),
            ("version 1", dict(good, v=1)),
            ("no version", {k: v for k, v in good.items() if k != "v"}),
            ("unknown action", dict(good, action="delete")),
            ("pin with a body", dict(good, markdown="")),
            ("pin with a session", dict(good, session=SESSION)),
            ("read with flags", dict(request("read"), flags={})),
            ("list with a title", dict(request("list"), title=TITLE)),
            ("bad title", dict(good, title="-x")),
            ("bad request id", dict(good, request_id="x")),
            ("bad identity", dict(good, identity="not a seat")),
        ):
            with self.subTest(name), self.assertRaises(N.NotesError) as caught:
                N.check_request(raw)
            self.assertEqual(caught.exception.code, "invalid_argument")

    def test_request_problem_ties_the_request_to_the_identity_that_sends_it(self):
        mine = request("create", session=SESSION)
        self.assertIsNone(N.request_problem(mine, "CLAUDE"))
        self.assertIn("names identity CLAUDE, not CODEX", N.request_problem(mine, "CODEX"))
        theirs = request("create", title="[AFC, Codex] Not Mine", session=SESSION)
        problem = N.request_problem(theirs, "CLAUDE")
        self.assertIn('names agent "Codex"', problem)
        self.assertIn("a new note's title carries the Agent of the identity that creates it", problem)
        self.assertIsNone(N.request_problem(request("create", title="[AFC, CLAUDE] Tag Form", session=SESSION), "CLAUDE"))
        # update, pin, unpin, read and list may name any note
        for req in (
            request("update", title="[AFC, Codex] Not Mine", session=SESSION),
            request("update", title=MASTER, session=SESSION),
            request("pin", title="[AFC, Codex] Not Mine"),
            request("unpin", title=MASTER),
            request("read", title=MASTER),
            request("list"),
        ):
            with self.subTest(req["action"]):
                self.assertIsNone(N.request_problem(req, "CLAUDE"))

    def test_a_create_or_update_without_a_session_is_refused_unless_the_identity_is_single_thread(self):
        for action in ("create", "update"):
            with self.subTest(action):
                problem = N.request_problem(request(action), "CLAUDE")
                self.assertIn("session id is required", problem)
                self.assertIsNone(
                    N.request_problem(request(action, identity="JET", title="[AFC, JET] Static"), "JET")
                )
        self.assertIsNone(N.request_problem(request("pin"), "CLAUDE"), "a pin carries no session")

    def test_an_update_of_a_note_the_title_does_not_tie_to_the_requester_is_existing_only(self):
        self.assertTrue(N.existing_only(request("update", title="[AFC, Codex] Not Mine")))
        self.assertTrue(N.existing_only(request("update", title=MASTER)))
        self.assertFalse(N.existing_only(request("update", title=TITLE)))
        self.assertFalse(N.existing_only(request("update", title="[AFC, CLAUDE] Tag Form")))
        for action in ("create", "pin", "unpin", "read", "list"):
            with self.subTest(action):
                self.assertFalse(N.existing_only(request(action, title=MASTER if action != "create" else TITLE)))

    def test_the_digest_names_the_request_whatever_its_key_order(self):
        req = request("create", markdown="b", flags={"pin": False, "summary": "s"}, session=SESSION)
        shuffled = dict(reversed(list(req.items())))
        shuffled["flags"] = dict(reversed(list(req["flags"].items())))
        self.assertEqual(N.request_digest(req), N.request_digest(shuffled))
        self.assertRegex(N.request_digest(req), r"^[0-9a-f]{64}$")
        self.assertEqual(N.request_digest(req), N.request_digest(json.loads(json.dumps(req))))
        for change in ({"markdown": "c"}, {"title": "[AFC, Claude] Other"}, {"flags": {"pin": True}}, {"session": "other-session"}):
            with self.subTest(change):
                self.assertNotEqual(N.request_digest(req), N.request_digest(dict(req, **change)))


class ArgvTests(unittest.TestCase):
    SCRIPT = Path("/fake/apple-notes-coding.sh")

    def argv(self, req, html_path=None, **kwargs):
        return N.build_argv(self.SCRIPT, req, html_path, **kwargs)

    def test_every_action_has_its_mode_and_the_title_last(self):
        cases = (
            (request("create", session=SESSION), ["--session", SESSION, "--title", TITLE]),
            (request("update", session=SESSION), ["--update", "--session", SESSION, "--title", TITLE]),
            (request("pin"), ["--pin-only", "--title", TITLE]),
            (request("unpin"), ["--unpin-only", "--title", TITLE]),
            (request("read"), ["--read", "--title", TITLE]),
            (request("list"), ["--list", "--limit", "100"]),
            (request("list", query="Plan", limit=7), ["--list", "--limit", "7", "--query", "Plan"]),
        )
        for req, tail in cases:
            with self.subTest(req["action"]):
                argv, extra = self.argv(req)
                self.assertEqual(argv, [str(self.SCRIPT)] + tail)
                self.assertEqual(
                    extra,
                    {"AGENT_NOTES_CALLER": "cli", "AGENT_NOTES_REQUEST_ID": RID, "AGENT_SEAT": "CLAUDE"},
                )

    def test_existing_only_rides_an_update_and_the_flags_come_before_the_title(self):
        req = request(
            "update",
            title=MASTER,
            session=SESSION,
            flags={"notify": True, "needs_owner": True, "summary": "short", "pr": "46", "html": True, "pin": False},
        )
        argv, extra = self.argv(req, "/tmp/agent-sync-note-1.html", existing=True, caller="runner", requester="CODEX")
        self.assertEqual(
            argv[1:],
            [
                "--update",
                "--existing-only",
                "--notify",
                "--needs-owner",
                "--summary",
                "short",
                "--pr",
                "46",
                "--html",
                "/tmp/agent-sync-note-1.html",
                "--session",
                SESSION,
                "--title",
                MASTER,
            ],
        )
        self.assertEqual(
            extra,
            {
                "AGENT_NOTES_CALLER": "runner",
                "AGENT_NOTES_REQUEST_ID": RID,
                "AGENT_NOTES_REQUESTER": "CODEX",
                "AGENT_SEAT": "CLAUDE",
                "APPLE_NOTES_PIN": "0",
            },
        )
        self.assertNotIn("--existing-only", self.argv(request("create", session=SESSION), existing=True)[0])

    def test_the_body_never_reaches_the_command_line(self):
        body = "PRIVATE-BODY-WORDS line\nsecond line"
        for flags in ({}, {"html": True}):
            argv, extra = self.argv(request("create", markdown=body, flags=flags, session=SESSION), "/tmp/x.html")
            self.assertFalse(any("PRIVATE-BODY" in part for part in argv + list(extra.values())))


class DirectRunnerTests(unittest.TestCase):
    SCRIPT = Path("/fake/apple-notes-coding.sh")
    ENV = {
        "HOME": "/h",
        "PATH": "/usr/bin",
        "ZULIP_API_KEY": "never-passed",
        "OPERATOR_NOTES_RUNNER_KEY": "never-passed-either",
        "AGENT_SYNC_STATE_DIR": "/s",
        "APPLE_NOTES_ACTION_LOG": "/logs/actions.jsonl",
    }

    def run_direct(self, req, runner, **kwargs):
        return N.run_direct(req, self.SCRIPT, env=self.ENV, runner=runner, **kwargs)

    def test_markdown_goes_on_stdin_and_never_on_argv(self):
        run = FakeRun()
        body = "SECRET-LOOKING-BODY line\nsecond line"
        result = self.run_direct(request(markdown=body, flags={"summary": "short", "pr": "46", "notify": True}), run)
        self.assertEqual(result, {"status": "done", "note_id": "x-coredata://abc"})
        call = run.calls[0]
        self.assertEqual(call["input"], body.encode())
        self.assertEqual(call["argv"], [str(self.SCRIPT), "--notify", "--summary", "short", "--pr", "46", "--title", TITLE])
        self.assertFalse(any("SECRET-LOOKING" in part for part in call["argv"]))
        self.assertEqual(call["kwargs"]["check"], False)

    def test_update_pin_and_unpin_modes(self):
        run = FakeRun()
        self.run_direct(request("update", markdown="b"), run)
        self.run_direct(request("pin"), run)
        self.run_direct(request("unpin"), run)
        self.assertEqual(run.calls[0]["argv"][1:], ["--update", "--title", TITLE])
        self.assertEqual(run.calls[1]["argv"][1:], ["--pin-only", "--title", TITLE])
        self.assertEqual(run.calls[2]["argv"][1:], ["--unpin-only", "--title", TITLE])
        self.assertEqual(run.calls[1]["input"], b"")

    def test_an_update_of_a_note_that_is_not_the_callers_runs_existing_only(self):
        run = FakeRun()
        self.run_direct(request("update", title=MASTER, markdown="b"), run)
        self.run_direct(request("update", title="[AFC, Codex] Not Mine", markdown="b"), run)
        self.run_direct(request("update", title=TITLE, markdown="b"), run)
        self.assertEqual(run.calls[0]["argv"][1:3], ["--update", "--existing-only"])
        self.assertEqual(run.calls[1]["argv"][1:3], ["--update", "--existing-only"])
        self.assertNotIn("--existing-only", run.calls[2]["argv"])
        self.run_direct(request("update", title=TITLE, markdown="b"), run, existing=True)
        self.assertIn("--existing-only", run.calls[3]["argv"], "a caller may force it")

    def test_exit_3_says_whether_the_update_could_have_created_the_note(self):
        with self.assertRaises(N.NotesError) as caught:
            self.run_direct(request("update", title=MASTER, markdown="b"), FakeRun(code=3))
        self.assertEqual(caught.exception.code, "not_found")
        self.assertIn("carries your own agent name", caught.exception.message)
        for req in (request("pin"), request("read", title=MASTER)):
            with self.subTest(req["action"]), self.assertRaises(N.NotesError) as caught:
                self.run_direct(req, FakeRun(code=3))
            self.assertEqual(caught.exception.message, "no note with that title in the Coding folder")

    def test_html_goes_through_a_mode_600_temp_file_that_is_removed(self):
        run = FakeRun()
        self.run_direct(request(markdown="<div><p>hi</p></div>", flags={"html": True}), run)
        argv = run.calls[0]["argv"]
        path = argv[argv.index("--html") + 1]
        self.assertEqual(run.html_mode, 0o600)
        self.assertEqual(run.html_body, b"<div><p>hi</p></div>")
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("<div>", " ".join(argv))
        self.assertEqual(run.calls[0]["input"], b"")

    def test_unpinned_create_sets_the_script_switch_and_the_child_env_holds_no_credential(self):
        run = FakeRun()
        self.run_direct(request(flags={"pin": False}), run)
        env = run.calls[0]["env"]
        self.assertEqual(env["APPLE_NOTES_PIN"], "0")
        self.assertEqual(
            set(env),
            {
                "HOME",
                "PATH",
                "APPLE_NOTES_ACTION_LOG",
                "APPLE_NOTES_PIN",
                "AGENT_SEAT",
                "AGENT_NOTES_CALLER",
                "AGENT_NOTES_REQUEST_ID",
            },
        )  # AGENT_SEAT is the verified identity;  no Zulip or Operator key
        self.assertEqual((env["AGENT_SEAT"], env["AGENT_NOTES_CALLER"], env["AGENT_NOTES_REQUEST_ID"]), ("CLAUDE", "cli", RID))

    def test_a_runner_write_names_the_requester_and_carries_one_provenance_line(self):
        run = FakeRun()
        self.run_direct(request("update", title=MASTER, markdown="Body text."), run, caller="runner", requester="CODEX")
        call = run.calls[0]
        self.assertEqual((call["env"]["AGENT_NOTES_CALLER"], call["env"]["AGENT_NOTES_REQUESTER"]), ("runner", "CODEX"))
        body = call["input"].decode()
        self.assertTrue(body.startswith("Body text."))
        self.assertRegex(body, r"\*Written for CODEX through the Operator on \w{3}, \w{3} \d{1,2}, \d{4} \(request requestid-0001\)\.\*$")
        self.assertEqual(body.count("Written for"), 1)
        # a pin carries no body and no line
        self.run_direct(request("pin"), run, caller="runner")
        self.assertEqual(run.calls[1]["input"], b"")
        self.assertNotIn("AGENT_NOTES_REQUESTER", run.calls[1]["env"])

    def test_a_write_on_this_mac_drops_an_old_provenance_line_instead_of_keeping_it(self):
        run = FakeRun()
        old = "Content.\n\n---\n\n*Written for CODEX through the Operator on Thu, Oct 22, 2026 (request nr1).*"
        self.run_direct(request("update", markdown=old), run)
        self.assertEqual(run.calls[0]["input"], b"Content.")
        self.run_direct(request("update", markdown=STORED_HTML, flags={"html": True}), run)
        self.assertNotIn(b"Relayed for", run.html_body)

    def test_a_read_parses_the_scripts_answer_in_each_format(self):
        for fmt, body in (("html", STORED_CONTENT_HTML), ("text", STORED_CONTENT_TEXT), ("raw", STORED_HTML)):
            with self.subTest(fmt):
                run = FakeRun(stdout=read_answer())
                result = self.run_direct(request("read", title=STORED_TITLE, fmt=fmt, identity="MM"), run)
                self.assertEqual(
                    result,
                    {
                        "note": {
                            "title": STORED_TITLE,
                            "id": "x-coredata://note/p1",
                            "created": "2026-10-20T08:05:00Z",
                            "modified": "2026-10-22T22:03:00Z",
                            "format": fmt,
                            "body": body,
                            "bytes": len(body.encode()),
                            "truncated": False,
                            "withheld": 0,
                        }
                    },
                )
                self.assertEqual(run.calls[0]["argv"][1:], ["--read", "--title", STORED_TITLE])
                self.assertEqual(run.calls[0]["kwargs"]["timeout"], N.READ_TIMEOUT_S)

    def test_a_long_note_is_cut_and_says_so(self):
        note = N.read_result({"title": "t", "body": "x" * 50}, "raw", limit=10)
        self.assertEqual((note["body"], note["bytes"], note["truncated"]), ("x" * 10, 10, True))

    def test_a_cut_ends_on_a_whole_line_so_a_withheld_marker_is_never_split(self):
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        lines = ["<div>line %02d</div>" % n for n in range(6)]
        lines[3] = "<div>k %s</div>" % token  # shorter than its marker line, so withholding makes it longer
        body = "\n".join(lines)
        marker_end = len("\n".join(lines[:3] + ["<div>%s</div>" % N.WITHHELD]).encode())
        for limit in range(marker_end - 20, marker_end + 3):
            with self.subTest(limit=limit):
                note = N.read_result({"title": "t", "body": body}, "raw", limit=limit)
                self.assertNotIn(token, note["body"])
                self.assertLessEqual(note["bytes"], limit)
                self.assertTrue(note["truncated"])
                # Whatever survives the cut is whole lines, so the marker is either all there or not there at all.
                self.assertNotIn("[withheld", note["body"].replace(N.WITHHELD, ""))
                self.assertTrue(note["body"].endswith("\n") or note["body"] == "", note["body"][-30:])
                # The count is of the markers the caller actually gets, never one the second cut dropped.
                self.assertEqual(note["withheld"], note["body"].count(N.WITHHELD))

    def test_a_log_line_keeps_each_field_in_its_own_type_and_only_text_utf8_can_hold(self):
        entry = N.clean_log_entry(
            {"id": "e1", "ts": "2026-10-23T00:00:00Z", "exit": "boom", "duration_ms": 1.9, "seat": ["x"], "mode": {"a": 1},
             "title": "t\ud800x", "pin": "skipped", "count": True}
        )
        self.assertEqual(entry["exit"], None)  # a count field takes a whole number only
        self.assertEqual(entry["duration_ms"], 1)
        self.assertNotIn("seat", entry)  # a list is no value of a text field:  dropped, never str()'d
        self.assertNotIn("mode", entry)
        self.assertEqual(entry["count"], None)  # a bool is no count
        self.assertEqual(entry["title"], "t?x")  # a lone surrogate cannot be sent on as UTF-8
        json.dumps(entry, ensure_ascii=False).encode("utf-8")  # so the line can always be sent
        self.assertIsNone(N.clean_log_entry({"id": 5, "ts": "x"}))
        with self.assertRaises(N.NotesError) as caught:
            N.check_flags("pin")
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_a_title_is_scanned_whole_before_it_is_cut(self):
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        long_title = "x" * 380 + " " + token  # the token crosses character 400
        self.assertEqual(N.list_result({"notes": [{"title": long_title}]})["notes"][0]["title"], N.WITHHELD)
        entry = N.clean_log_entry({"id": "e1", "ts": "2026-10-23T00:00:00Z", "title": long_title})
        self.assertEqual(entry["title"], N.WITHHELD)

    def test_a_list_and_the_log_withhold_a_secret_shaped_title(self):
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        listed = N.list_result({"notes": [{"title": "[AFC, Claude] %s" % token}, {"title": TITLE}], "total": 2})
        self.assertEqual([row["title"] for row in listed["notes"]], [N.WITHHELD, TITLE])
        self.assertEqual(N.list_result({"notes": "not a list"})["notes"], [])
        entry = N.clean_log_entry({"id": "e1", "ts": "2026-10-23T00:00:00Z", "title": "[AFC, Claude] %s" % token})
        self.assertEqual(entry["title"], N.WITHHELD)
        with self.assertRaises(N.NotesError) as caught:
            N.check_log_args({"title": "-x"})
        self.assertIn("argument 'title'", caught.exception.message)  # the field the caller set

    def test_a_read_withholds_each_secret_shaped_line_and_the_marker_is_never_written_back(self):
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        stored = "<div>Before.</div>\n<div>key %s</div>\n<div>After.</div>" % token
        for fmt, raw in (("raw", {"title": "t", "body": stored}), ("text", {"title": "t", "plaintext": "t\nkey %s\nAfter." % token})):
            with self.subTest(fmt):
                note = N.read_result(raw, fmt)
                self.assertNotIn(token, note["body"])
                self.assertIn(N.WITHHELD, note["body"])
                self.assertEqual(note["withheld"], 1)
        self.assertEqual(
            N.read_result({"title": "t", "body": stored}, "raw")["body"],
            "<div>Before.</div>\n<div>%s</div>\n<div>After.</div>" % N.WITHHELD,
            "a <div> line keeps its <div>",
        )
        with self.assertRaises(N.NotesError) as caught:
            N.make_request("update", "CLAUDE", request_id="withheld-req-01", title=MASTER, markdown="<div>%s</div>" % N.WITHHELD, session=SESSION)
        self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertIn("Edit the note on the Mac", caught.exception.message)

    def test_a_secret_shaped_title_is_refused_for_every_action_but_create(self):
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        for action in ("read", "update", "pin", "unpin"):
            with self.subTest(action):
                with self.assertRaises(N.NotesError) as caught:
                    N.check_title("%s note" % token, action)
                self.assertEqual(caught.exception.code, "invalid_argument")
                self.assertIn("argument 'title' looks like it contains a secret", caught.exception.message)
                self.assertNotIn(token, caught.exception.message)
        # A create's title gets the write scan with its body, which answers refused_secret (test_mcp_extras).
        self.assertTrue(N.check_title("[AFC, Claude] %s" % token, "create"))

    def test_a_query_never_starts_with_a_dash(self):
        for query in ("-x", " --read", "-"):
            with self.subTest(query):
                with self.assertRaises(N.NotesError) as caught:
                    N.check_query(query)
                self.assertIn("must not start with -", caught.exception.message)
        self.assertEqual(N.check_query("a-b"), "a-b")
        # A query or a log title filter is sent to the Operator, so it gets the secret scan.
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        with self.assertRaises(N.NotesError) as caught:
            N.check_log_args({"title": token})
        self.assertIn("looks like it contains a secret", caught.exception.message)
        self.assertNotIn(token, caught.exception.message)

    def test_a_list_answers_four_text_fields_per_note(self):
        answer = {
            "notes": [
                {"title": TITLE, "id": "x-coredata://n1", "created": "2026-10-21T10:00:00Z", "modified": "2026-10-22T22:03:00Z", "body": "never"},
                {"title": MASTER, "id": "x-coredata://n2", "created": None, "modified": "2026-10-22T21:00:00Z"},
                "not a row",
            ],
            "total": 40,
            "truncated": True,
        }
        run = FakeRun(stdout=json.dumps(answer))
        result = self.run_direct(request("list", query="a", limit=2), run)
        self.assertEqual(
            result,
            {
                "notes": [
                    {"title": TITLE, "id": "x-coredata://n1", "created": "2026-10-21T10:00:00Z", "modified": "2026-10-22T22:03:00Z"},
                    {"title": MASTER, "id": "x-coredata://n2", "created": "", "modified": "2026-10-22T21:00:00Z"},
                ],
                "total": 40,
                "truncated": True,
            },
        )
        self.assertEqual(run.calls[0]["argv"][1:], ["--list", "--limit", "2", "--query", "a"])
        self.assertEqual(N.list_result({"notes": [], "total": True})["total"], 0, "a bool is not a count")

    def test_a_read_or_list_answer_that_is_not_json_is_internal(self):
        for stdout in ("not json", "[1, 2]"):
            for req in (request("read"), request("list")):
                with self.subTest(stdout=stdout, action=req["action"]), self.assertRaises(N.NotesError) as caught:
                    self.run_direct(req, FakeRun(stdout=stdout))
                self.assertEqual(caught.exception.code, "internal")

    def test_a_body_over_the_limit_is_refused_and_nothing_runs(self):
        raw = dict(request(), markdown="x" * (N.MARKDOWN_MAX + 1001))  # a request built some other way
        run = FakeRun()
        with self.assertRaises(N.NotesError) as caught:
            self.run_direct(raw, run)
        self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(run.calls, [], "nothing was written")

    def test_script_exit_codes_become_tool_errors(self):
        # An exit the script does not plan for (9) is an osascript error:  a create or update may still have written
        # the note, so it is outcome_unknown;  a pin changed nothing a retry could repeat, so it is internal.
        for action, code, expected in (
            ("create", 2, "invalid_argument"),
            ("create", 3, "not_found"),
            ("create", 4, "unavailable"),
            ("create", 9, "outcome_unknown"),
            ("update", 9, "outcome_unknown"),
            ("pin", 9, "internal"),
        ):
            with self.subTest(action=action, code=code):
                with self.assertRaises(N.NotesError) as caught:
                    self.run_direct(request(action), FakeRun(code=code, stderr="usage: nope " + "x" * 400))
                self.assertEqual(caught.exception.code, expected)
                self.assertLess(len(caught.exception.message), 400)

    def test_a_skipped_pin_is_carried_out_in_the_result_and_a_clean_run_has_no_such_key(self):
        """A create or update whose pin was skipped exits 0 (AFC-454), so the stderr warning is the only sign."""
        warning = "pin skipped: Pin Coding Note Shortcut failed; note saved unpinned\n"
        skipped = self.run_direct(request(), FakeRun(code=0, stderr=warning))
        self.assertEqual(skipped["status"], "done")
        self.assertEqual(skipped["pin_skipped"], "Pin Coding Note Shortcut failed; note saved unpinned")
        for stderr in ("", "some other warning\n", "the line pin skipped: is not at the start\n"):
            with self.subTest(stderr=stderr):
                self.assertNotIn("pin_skipped", self.run_direct(request(), FakeRun(stderr=stderr)))
        noisy = "warming up\n" + "pin skipped: " + "x" * 400 + "\ntrailing\n"
        self.assertEqual(len(self.run_direct(request(), FakeRun(stderr=noisy))["pin_skipped"]), 200)

    def test_exit_4_names_the_pin_or_unpin_that_failed(self):
        for action, word in (("pin", "pinned"), ("unpin", "unpinned")):
            with self.subTest(action):
                with self.assertRaises(N.NotesError) as caught:
                    self.run_direct(request(action), FakeRun(code=4))
                self.assertEqual(caught.exception.code, "unavailable")
                self.assertIn("could not be %s" % word, caught.exception.message)

    def test_a_write_timeout_is_an_unknown_outcome_and_a_read_timeout_is_not(self):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        with self.assertRaises(N.NotesError) as caught:
            self.run_direct(request(markdown="<p>x</p>", flags={"html": True}), slow)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertIn("may or may not", caught.exception.message)
        for req in (request("read"), request("list")):
            with self.subTest(req["action"]), self.assertRaises(N.NotesError) as caught:
                self.run_direct(req, slow)
            self.assertEqual(caught.exception.code, "unavailable")

    def test_a_script_that_cannot_start_is_unavailable(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        with self.assertRaises(N.NotesError) as caught:
            self.run_direct(request(), missing)
        self.assertEqual(caught.exception.code, "unavailable")

    def test_a_busy_lock_is_refused_as_busy_and_not_waited_out(self):
        """One wedged write must not hold every other write for the whole write budget (AFC-436)."""
        original = N.LOCK_WAIT_S
        N.LOCK_WAIT_S = 0.05
        N._RUN_LOCK.acquire()
        try:
            with self.assertRaises(N.NotesError) as caught:
                self.run_direct(request(), FakeRun())
            read = self.run_direct(request("read", title=STORED_TITLE, identity="MM"), FakeRun(stdout=read_answer()))
        finally:
            N._RUN_LOCK.release()
            N.LOCK_WAIT_S = original
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("busy", caught.exception.message)
        self.assertEqual(read["note"]["title"], STORED_TITLE, "a read takes no lock")

    def test_the_lock_is_released_after_a_write(self):
        self.run_direct(request(), FakeRun())
        self.run_direct(request(rid="requestid-0002"), FakeRun())
        self.assertFalse(N._RUN_LOCK.locked())

    def test_the_script_gets_the_cap_minus_the_lock_wait(self):
        """The wait for the lock is counted inside the cap, so the request cannot outlive it (AFC-436)."""
        run = FakeRun()
        self.run_direct(request(), run)
        self.assertEqual(run.calls[0]["kwargs"]["timeout"], N.SCRIPT_TIMEOUT_S - N.LOCK_WAIT_S)

    def test_availability_needs_macos_osascript_and_the_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "apps").mkdir()
            script = home / "apps" / N.SCRIPT_NAME
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
            env = {"AGENT_SYNC_NOTES_SCRIPT": str(home / "missing.sh")}
            have = lambda name: "/usr/bin/" + name
            self.assertEqual(N.direct_available(env, home, platform="darwin", which=have).name, N.SCRIPT_NAME)
            self.assertIsNone(N.direct_available(env, home, platform="linux", which=have))
            self.assertIsNone(N.direct_available(env, home, platform="darwin", which=lambda name: None))
            script.chmod(0o644)
            repo_copy = Path(N.__file__).resolve().parent.parent / N.SCRIPT_NAME
            expected = repo_copy if repo_copy.is_file() and os.access(repo_copy, os.X_OK) else None
            self.assertEqual(N.direct_available(env, home, platform="darwin", which=have), expected)


class DirectOnceTests(unittest.TestCase):
    """The persistent idempotency rows a Mac's direct writes share (idem.IdemStore)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = IdemStore(os.path.join(self.tmp.name, "CLAUDE", "mcp-idem.json"))
        self.req = request(session=SESSION)
        self.sent = 0

    def send(self):
        self.sent += 1
        return {"request_id": RID, "status": "done", "note_id": "x-coredata://abc"}

    def test_a_repeat_answers_the_first_result_and_status_reads_the_row(self):
        self.assertIsNone(N.direct_status(self.store, RID))
        first = N.direct_once(self.store, self.req, self.send)
        again = N.direct_once(self.store, self.req, self.send)
        self.assertEqual((first["duplicate"], again["duplicate"], self.sent), (False, True, 1))
        self.assertEqual(again["note_id"], "x-coredata://abc")
        self.assertEqual(
            N.direct_status(self.store, RID),
            {"request_id": RID, "status": "done", "note_id": "x-coredata://abc", "transport": "direct"},
        )

    def test_a_failure_that_wrote_nothing_frees_the_id_and_an_unknown_one_keeps_it(self):
        def refused():
            raise N.NotesError("not_found", "no note")

        with self.assertRaises(N.NotesError):
            N.direct_once(self.store, self.req, refused)
        self.assertIsNone(self.store.row("k:notes-direct:%s" % RID))
        self.assertFalse(N.direct_once(self.store, self.req, self.send)["duplicate"])

        other = request(rid="requestid-0099", session=SESSION)

        def hung():
            raise N.NotesError("outcome_unknown", "timed out")

        with self.assertRaises(N.NotesError):
            N.direct_once(self.store, other, hung)
        self.assertEqual(N.direct_status(self.store, "requestid-0099")["status"], "unknown")
        with self.assertRaises(N.NotesError) as caught:
            N.direct_once(self.store, other, self.send)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertIn("may have been interrupted", caught.exception.message)
        self.assertEqual(self.sent, 1)

    def test_a_write_in_flight_in_another_process_is_busy(self):
        self.store.begin("k:notes-direct:%s" % RID, 60, {"tool": "notes_direct"})
        self.assertEqual(N.direct_status(self.store, RID)["status"], "running")
        with self.assertRaises(N.NotesError) as caught:
            N.direct_once(self.store, self.req, self.send)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(self.sent, 0)


class ProvenanceTests(unittest.TestCase):
    NOW = datetime(2026, 10, 22, 22, 3, tzinfo=timezone.utc)  # Thu, Oct 22, 5:03pm in Chicago
    LINE = "Written for MM through the Operator on Thu, Oct 22, 2026 (request requestid-0001)."

    def update(self, markdown, html=False):
        return request("update", title=STORED_TITLE, markdown=markdown, flags={"html": True} if html else {}, identity="MM", session=SESSION)

    def test_markdown_gets_one_italic_last_line_and_a_repeat_replaces_it(self):
        req = self.update("# Head\n\nBody one.\n\nBody two.\n\nBody three.")
        once = N.with_provenance(req, "MM", now=self.NOW)
        self.assertEqual(
            once["markdown"],
            "# Head\n\nBody one.\n\nBody two.\n\nBody three.\n\n---\n\n*%s*" % self.LINE,
        )
        twice = N.with_provenance(once, "MM", now=self.NOW + timedelta(days=1))
        self.assertEqual(twice["markdown"].count("through the Operator"), 1)
        self.assertIn("Fri, Oct 23, 2026", twice["markdown"])
        self.assertEqual(N.strip_provenance(once["markdown"], False), req["markdown"])
        self.assertEqual({k: v for k, v in once.items() if k != "markdown"}, {k: v for k, v in req.items() if k != "markdown"})

    def test_the_old_relay_footer_is_replaced_not_kept(self):
        old_md = "Body.\n\n---\n\n*Relayed for MM by the notes relay on Tue, Oct 20, 2026: [request message](https://x/1).*"
        out = N.with_provenance(self.update(old_md), "MM", now=self.NOW)["markdown"]
        self.assertNotIn("Relayed for", out)
        self.assertEqual(out, "Body.\n\n---\n\n*%s*" % self.LINE)

    def test_html_in_the_form_notes_stores_is_replaced_never_piled_up(self):
        out = N.with_provenance(self.update(STORED_HTML, html=True), "MM", now=self.NOW)["markdown"]
        self.assertNotIn("Relayed for", out)
        self.assertTrue(out.endswith("end to end.</div><hr><p><i>%s</i></p>" % self.LINE), out[-200:])
        self.assertEqual(out.count("through the Operator"), 1)
        # Notes keeps the new line as its own italic <div> after an <hr> it stores as a spacer row
        stored_again = out.replace(
            "<hr><p><i>%s</i></p>" % self.LINE,
            "\n<div><br><br></div>\n<div><i>%s</i></div>\n<div><br></div>\n" % self.LINE,
        )
        again = N.with_provenance(self.update(stored_again, html=True), "MM", now=self.NOW)["markdown"]
        self.assertEqual(again.count("through the Operator"), 1)
        self.assertEqual(again, out)

    def test_only_a_write_with_a_body_gets_a_line(self):
        for req in (request("pin"), request("read"), request("list")):
            with self.subTest(req["action"]):
                self.assertEqual(N.with_provenance(req, "MM", now=self.NOW), req)

    def test_a_body_with_no_footer_is_left_as_it_is(self):
        for body, is_html in (("Plain body.", False), ("<div>Plain</div>", True), ("", False)):
            with self.subTest(body):
                self.assertEqual(N.strip_provenance(body, is_html), body)
        mention = "We talked about the line Written for X through the Operator on Thu earlier.\n\nMore text."
        self.assertEqual(N.strip_provenance(mention, False), mention, "only a trailing italic line is a footer")


class ContentTests(unittest.TestCase):
    def test_the_stored_note_reads_back_as_its_content_only(self):
        html_content = N.content_html(STORED_HTML, STORED_TITLE)
        self.assertEqual(html_content, STORED_CONTENT_HTML)
        self.assertTrue(html_content.startswith("<div>Live end-to-end"))
        self.assertTrue(html_content.endswith("end to end.</div>"))
        text_content = N.content_text(STORED_TEXT, STORED_TITLE)
        self.assertEqual(text_content, STORED_CONTENT_TEXT)
        self.assertTrue(text_content.startswith("Live end-to-end"))
        self.assertTrue(text_content.endswith("end to end."))
        for content in (html_content, text_content):
            self.assertNotIn("Relayed for", content)
            self.assertNotIn("Session:", content)
            self.assertNotIn("3:05am", content)

    def test_the_banners_after_the_spacer_are_dropped(self):
        banners = (
            '<div style="background-color: #FFF3CD"><b>⚠️ NEEDS OWNER REVIEW / ACTION</b></div>\n'
            '<div style="background-color: #F8F9FA"><b>📌 Mobile Quick View:</b> the short summary</div>\n'
        )
        head, rest = STORED_HTML.split("<div>Live end-to-end", 1)
        body = head + banners + "<div>Live end-to-end" + rest
        self.assertEqual(N.content_html(body, STORED_TITLE), STORED_CONTENT_HTML)
        text = STORED_TEXT.replace(
            "\nLive end-to-end",
            "\n⚠️ NEEDS OWNER REVIEW / ACTION\n📌 Mobile Quick View: the short summary\nLive end-to-end",
        )
        self.assertEqual(N.content_text(text, STORED_TITLE), STORED_CONTENT_TEXT)

    def test_a_note_without_the_header_rows_keeps_its_first_block(self):
        body = "<div>First line of a hand-made note.</div>\n<div>Second.</div>"
        self.assertEqual(N.content_html(body, "Hand Made"), body)
        self.assertEqual(N.content_text("First line.\nSecond.", "Hand Made"), "First line.\nSecond.")

    def test_a_note_written_through_the_operator_reads_back_without_its_line(self):
        stored = STORED_HTML.split("<div><i>Relayed")[0] + (
            "<div><i>Written for MM through the Operator on Thu, Oct 22, 2026 (request nr0123456789abcdef0123).</i></div>\n"
            "<div><br></div>\n"
        )
        self.assertEqual(N.content_html(stored, STORED_TITLE), STORED_CONTENT_HTML)
        text = STORED_TEXT.replace(
            "Relayed for MM by the notes relay on Tue, Oct 20, 2026: request message.",
            "Written for MM through the Operator on Thu, Oct 22, 2026 (request nr0123456789abcdef0123).",
        )
        self.assertEqual(N.content_text(text, STORED_TITLE), STORED_CONTENT_TEXT)


class ReviewFixTests(unittest.TestCase):
    """PR #876 review:  the end-anchored footer patterns must not scan a whole body, and a read is cut by bytes."""

    def test_a_body_of_many_newlines_is_handled_at_once(self):
        import time

        footer = "\n\n---\n\n*Written for CODEX through the Operator on Thu, Oct 22, 2026 (request nr0001).*"
        for body in ("\n" * 400_000 + "text" + footer, "x\n" * 200_000 + footer):
            started = time.monotonic()
            stripped = N.strip_provenance(body, False)
            N.strip_provenance(body.replace("\n", "<div><br></div>\n"), True)
            N.content_text(body, "t")
            N.content_html("<div>a</div>" + "<div><br></div>\n" * 100_000, "t")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertFalse(stripped.endswith("*"), "the footer at the end is still removed")
            # The pattern takes the blank lines before the footer with it, as it always has.
            self.assertEqual(stripped.rstrip("\n"), body[: len(body) - len(footer)].rstrip("\n"))

    def test_a_read_is_cut_by_utf8_bytes_and_never_inside_a_character(self):
        raw = {"title": "t", "body": "", "plaintext": "😀" * 1000}
        note = N.read_result(raw, "text", limit=401)  # 100 emoji are 400 bytes;  the 101st would pass the limit
        self.assertTrue(note["truncated"])
        self.assertEqual(note["body"], "😀" * 100)
        self.assertEqual(note["bytes"], 400)
        self.assertLessEqual(len(note["body"].encode("utf-8")), 401)
        self.assertFalse(N.read_result(raw, "text")["truncated"])


class ActionLogTests(unittest.TestCase):
    NOW = datetime(2026, 10, 22, 22, 0, tzinfo=timezone.utc)

    def entry(self, n, hours_ago=1.0, **fields):
        moment = self.NOW - timedelta(hours=hours_ago)
        row = {
            "v": 1,
            "id": "e%04d" % n,
            "ts": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": "mac",
            "mode": "create",
            "title": TITLE,
            "exit": 0,
            "outcome": "ok",
            "seat": "CLAUDE",
            "caller": "cli",
        }
        row.update(fields)
        return row

    def test_the_path_is_the_home_log_an_override_or_off(self):
        home = Path("/home/someone")
        self.assertEqual(N.action_log_path({}, home), home / "Library" / "Logs" / "fleet" / "apple-notes-actions.jsonl")
        self.assertEqual(N.action_log_path({"APPLE_NOTES_ACTION_LOG": " /x/log.jsonl "}, home), Path("/x/log.jsonl"))
        self.assertEqual(
            N.action_log_path({"APPLE_NOTES_ACTION_LOG": "~/l.jsonl"}, home), Path(os.path.expanduser("~/l.jsonl"))
        )
        for off in ("off", "OFF", " Off "):
            self.assertIsNone(N.action_log_path({"APPLE_NOTES_ACTION_LOG": off}, home))
        self.assertEqual(N.read_action_log({"APPLE_NOTES_ACTION_LOG": "off"}, home), [])

    def test_a_log_line_keeps_only_the_known_fields_and_each_is_short(self):
        raw = self.entry(
            1,
            body="the note's text never belongs here",
            markdown="nor this",
            title="t" * 900,
            outcome="o" * 900,
            duration_ms=12.7,
            pin=True,
            note_id=None,
            reformatted={"dates": 2, "gaps": 3, "x" * 80: "y" * 200, **{"k%d" % i: i for i in range(20)}},
            flags=["not", "an", "object"],
        )
        out = N.clean_log_entry(raw)
        self.assertNotIn("body", out)
        self.assertNotIn("markdown", out)
        self.assertNotIn("flags", out, "a malformed object is dropped, not kept")
        self.assertEqual((len(out["title"]), len(out["outcome"])), (400, 200))
        self.assertEqual((out["duration_ms"], out["pin"], out["note_id"]), (12, True, None))
        self.assertLessEqual(len(out["reformatted"]), 12)
        self.assertEqual((out["reformatted"]["dates"], out["reformatted"]["gaps"]), (2, 3))
        self.assertTrue(all(len(k) <= 40 and (not isinstance(v, str) or len(v) <= 80) for k, v in out["reformatted"].items()))
        self.assertTrue(set(out) <= set(N.LOG_FIELDS))
        for bad in ([1], "line", {"ts": "x"}, {"id": "e1"}, {"id": 5, "ts": "x"}):
            with self.subTest(bad):
                self.assertIsNone(N.clean_log_entry(bad))

    def test_the_filter_picks_by_time_seat_action_and_title_newest_first(self):
        entries = [
            self.entry(1, 30),
            self.entry(2, 3, seat="codex", mode="update", title=MASTER),
            self.entry(3, 2),
            self.entry(4, 1, mode="read", title="[AFC, Claude] Release Plan"),
            self.entry(5, 0.5, ts="not a time"),
        ]
        pick = lambda **kw: [e["id"] for e in N.log_filter(entries, now=self.NOW, **kw)]
        self.assertEqual(pick(), ["e0004", "e0003", "e0002"])
        self.assertEqual(pick(since_hours=48), ["e0004", "e0003", "e0002", "e0001"])
        self.assertEqual(pick(seat="CODEX"), ["e0002"])
        self.assertEqual(pick(seat="codex"), ["e0002"])
        self.assertEqual(pick(action="read"), ["e0004"])
        self.assertEqual(pick(title="background JOBS"), ["e0002"])
        self.assertEqual(pick(limit=2), ["e0004", "e0003"])
        naive = self.entry(6, 0.25)
        naive["ts"] = naive["ts"].rstrip("Z")
        self.assertEqual([e["id"] for e in N.log_filter([naive], now=self.NOW)], ["e0006"], "no zone reads as UTC")

    def test_the_log_is_read_across_its_rotations_oldest_file_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "apple-notes-actions.jsonl"
            files = {
                path.with_name(path.name + ".2"): [self.entry(1)],
                path.with_name(path.name + ".1"): [self.entry(2), self.entry(3)],
                path: [self.entry(4)],
            }
            for target, rows in files.items():
                target.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with open(path, "a") as fh:
                fh.write("not json\n[1]\n" + json.dumps({"id": "no-ts"}) + "\n" + "x" * (N.LOG_LINE_MAX * 4 + 10) + "\n")
                fh.write(json.dumps(self.entry(5, body="dropped")) + "\n")
            got = list(N.iter_log_files(path))
            self.assertEqual([e["id"] for e in got], ["e0001", "e0002", "e0003", "e0004", "e0005"])
            self.assertNotIn("body", got[-1])
            self.assertEqual([e["id"] for e in N.iter_log_files(Path(tmp) / "missing.jsonl")], [])
            now = datetime.now(timezone.utc)
            fresh = Path(tmp) / "fresh.jsonl"
            fresh.write_text(json.dumps(dict(self.entry(9), ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"))) + "\n")
            self.assertEqual(
                [e["id"] for e in N.read_action_log({"APPLE_NOTES_ACTION_LOG": str(fresh)}, Path(tmp))], ["e0009"]
            )

    def test_the_log_arguments_are_checked(self):
        self.assertEqual(
            N.check_log_args({}), {"since_hours": 24, "limit": 50, "seat": None, "action": None, "title": None}
        )
        self.assertEqual(
            N.check_log_args({"since_hours": 720, "limit": 500, "by_seat": "codex", "action": "update", "title": " Plan "}),
            {"since_hours": 720, "limit": 500, "seat": "CODEX", "action": "update", "title": "Plan"},
        )
        self.assertIsNone(N.check_log_args({"title": "   "})["title"])
        for bad in (
            {"since_hours": 0},
            {"since_hours": 721},
            {"since_hours": True},
            {"since_hours": "5"},
            {"limit": 0},
            {"limit": 501},
            {"by_seat": "bad seat!"},
            {"by_seat": 5},
            {"action": "delete"},
            {"title": 'a "q"'},
            {"title": "x" * 101},
        ):
            with self.subTest(bad), self.assertRaises(N.NotesError) as caught:
                N.check_log_args(bad)
            self.assertEqual(caught.exception.code, "invalid_argument")

    def test_iso_now_is_utc_seconds(self):
        self.assertEqual(N.iso_now(lambda: 1761170400.9), "2025-10-22T22:00:00Z")
        self.assertRegex(N.iso_now(time.time), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
