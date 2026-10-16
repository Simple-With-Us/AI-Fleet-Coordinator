"""`agent-sync notes ...` and `agent-sync guide` (AFC-331):  the CLI twin of the notes_* and guide tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from agent_sync import notes as N
from agent_sync.mcp import guide as G
from agent_sync.tests.harness import Harness
from agent_sync.tests.test_notes import FakeRun

TITLE = "[AFC, Claude] cli check"


class NotesCliTests(Harness):
    def setUp(self):
        super().setUp()
        self.fake.streams[N.RELAY_CHANNEL] = 140
        self.fake.subscribed.add(N.RELAY_CHANNEL)
        self.script = Path("/fake/apple-notes-coding.sh")

    def direct(self, run):
        return mock.patch.object(N, "direct_available", return_value=self.script), mock.patch.object(N.subprocess, "run", run)

    def test_a_create_on_a_mac_runs_the_script_with_the_body_from_stdin(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            result = self.run_cli("notes", "create", TITLE, "--json", stdin="body from stdin\n")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual((info["status"], info["transport"], info["action"]), ("done", "direct", "create"))
        self.assertEqual(run.calls[0]["input"], b"body from stdin\n")
        self.assertEqual(run.calls[0]["argv"][-2:], ["--title", TITLE])
        self.assertEqual(self.fake.messages, [], "a direct write posts nothing and needs no Zulip request")
        self.assertEqual([r.path for r in self.fake.requests], [])

    def test_the_body_comes_from_a_private_file_or_an_html_file_and_flags_pass_through(self):
        run = FakeRun()
        body = self.tmp / "body.md"
        body.write_text("# From a file")
        body.chmod(0o600)
        html = self.tmp / "body.html"
        html.write_text("<div><p>hi</p></div>")
        a, b = self.direct(run)
        with a, b:
            first = self.run_cli("notes", "update", TITLE, "--body-file", str(body), "--no-pin", "--notify",
                                 "--summary", "short", "--pr", "46")
            second = self.run_cli("notes", "create", TITLE, "--html", str(html))
            wide = self.tmp / "wide.md"
            wide.write_text("x")
            wide.chmod(0o644)
            third = self.run_cli("notes", "create", TITLE, "--body-file", str(wide))
        self.assertEqual((first.code, second.code), (0, 0), first.err + second.err)
        self.assertEqual(run.calls[0]["input"], b"# From a file")
        self.assertEqual(run.calls[0]["argv"][1:], ["--update", "--notify", "--summary", "short", "--pr", "46", "--title", TITLE])
        self.assertEqual(run.calls[0]["env"]["APPLE_NOTES_PIN"], "0")
        self.assertIn("--html", run.calls[1]["argv"])
        self.assertEqual(third.code, 2)
        self.assertEqual(len(run.calls), 2)

    def test_pin_and_unpin_take_only_a_title(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            self.assertEqual(self.run_cli("notes", "pin", TITLE).code, 0)
            self.assertEqual(self.run_cli("notes", "unpin", TITLE).code, 0)
        self.assertEqual([c["argv"][1] for c in run.calls], ["--pin-only", "--unpin-only"])

    def test_a_bad_title_and_the_script_failing_are_usage_and_api_exits(self):
        a, b = self.direct(FakeRun())
        with a, b:
            self.assertEqual(self.run_cli("notes", "create", "no bracket", stdin="x").code, 2)
            self.assertEqual(self.run_cli("notes", "create", '[AFC, Claude] "q"', stdin="x").code, 2)
            wrong = self.run_cli("notes", "create", "[AFC, Codex] not mine", stdin="x")
            self.assertEqual(wrong.code, 2)
            self.assertIn("Agent in the title", wrong.err)
            leak = self.run_cli("notes", "create", TITLE, stdin="token " + "ghp_" + "EXAMPLE0" * 5)
            self.assertEqual(leak.code, 2)
            self.assertIn("looks like it contains", leak.err)
        a, b = self.direct(FakeRun(code=3))
        with a, b:
            result = self.run_cli("notes", "pin", TITLE)
        self.assertEqual(result.code, 5)
        self.assertIn("no note with that title", result.err)

    def test_relay_posts_the_envelope_as_the_identity_and_status_reads_it_back(self):
        a, b = self.direct(FakeRun())
        with a, b:
            sent = self.run_cli("notes", "create", TITLE, "--relay", "--request-id", "cli-request-0001", "--json", stdin="relayed body")
        self.assertEqual(sent.code, 0, sent.err)
        info = json.loads(sent.out)
        self.assertEqual((info["status"], info["transport"], info["request_id"]), ("queued", "relay", "cli-request-0001"))
        posted = next(m for m in self.fake.messages if m["id"] == info["message_id"])
        self.assertEqual(posted["display_recipient"], N.RELAY_CHANNEL)
        self.assertEqual(N.parse_message(posted["content"])["markdown"], "relayed body")
        queued = self.run_cli("notes", "status", "cli-request-0001", "--json")
        self.assertEqual(json.loads(queued.out)["status"], "queued")
        self.fake.reactions.add((posted["id"], 11, "check"))
        done = self.run_cli("notes", "status", "cli-request-0001")
        self.assertEqual(done.code, 0)
        self.assertIn("done", done.out)

    def test_the_cli_refuses_a_non_agent_identity(self):
        a, b = self.direct(FakeRun())
        with a, b:
            result = self.run_cli("notes", "pin", TITLE, "--identity", "SENTRY")
        self.assertEqual(result.code, 3)
        self.assertEqual(self.fake.messages, [])

    def test_the_guide_command_prints_the_guide_or_a_section(self):
        whole = self.run_cli("guide")
        self.assertEqual(whole.code, 0)
        self.assertEqual(whole.out.strip(), G.read_text().strip())
        one = self.run_cli("guide", "the notes relay")
        self.assertTrue(one.out.startswith("## The Notes Relay"))
        self.assertEqual(self.run_cli("guide", "nope").code, 2)
        self.assertEqual(self.run_cli("guide", env={"HOME": str(self.home), "AGENT_SYNC_REALM": self.fake.url}).code, 0)
