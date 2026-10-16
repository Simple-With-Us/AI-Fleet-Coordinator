"""`agent-sync notes ...` and `agent-sync guide` (AFC-331):  the CLI twin of the notes_* and guide tools."""
from __future__ import annotations

import json
import subprocess
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

    def test_the_summary_is_scanned_and_a_repeated_request_id_writes_once(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            leak = self.run_cli("notes", "create", TITLE, "--summary", "token " + "ghp_" + "EXAMPLE0" * 5, stdin="x")
            self.assertEqual(leak.code, 2)
            self.assertIn("summary looks like it contains", leak.err)
            self.assertEqual(run.calls, [])
            first = self.run_cli("notes", "create", TITLE, "--request-id", "cli-once-0001", "--json", stdin="x")
            again = self.run_cli("notes", "create", TITLE, "--request-id", "cli-once-0001", "--json", stdin="x")
            fresh = self.run_cli("notes", "create", TITLE, "--request-id", "cli-once-0002", "--json", stdin="x")
        self.assertEqual([json.loads(r.out)["duplicate"] for r in (first, again, fresh)], [False, True, False])
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(json.loads(again.out)["status"], "done")

    def test_a_failed_status_check_is_a_read_and_leaves_no_audit_row(self):
        failed = self.run_cli("notes", "status", "nobody-asked-01")
        self.assertNotEqual(failed.code, 0)
        self.assertEqual(self.audit_rows(), [], "status is a read, whether it succeeds or fails")

    def test_an_interrupt_during_a_direct_write_is_audited_and_the_retry_is_refused(self):
        def interrupted(argv, **kwargs):
            raise KeyboardInterrupt()
        a, b = self.direct(interrupted)
        with a, b:
            first = self.run_cli("notes", "create", TITLE, "--request-id", "interrupt-request-1", stdin="secret-looking body")
        self.assertEqual(first.code, 130)
        rows = self.audit_rows()
        self.assertEqual([(r["tool"], r["outcome"]) for r in rows], [("notes create", "interrupted")])
        self.assertNotIn("secret-looking", json.dumps(rows))
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            retry = self.run_cli("notes", "create", TITLE, "--request-id", "interrupt-request-1", stdin="again")
        self.assertNotEqual(retry.code, 0)
        self.assertEqual(run.calls, [], "the interrupted write may have landed:  its id is refused")

    def test_an_interrupted_relay_write_is_audited_once_and_a_failed_unexpectedly_one_names_only_its_type(self):
        from agent_sync.mcp import tools as T

        def interrupted(self, name, arguments, **kwargs):
            raise KeyboardInterrupt()

        def broken(self, name, arguments, **kwargs):
            raise UnicodeEncodeError("ascii", "caf\u00e9 secret-looking", 3, 4, "ordinal not in range")
        for label, replacement, code, outcome in (("interrupted", interrupted, 130, "interrupted"),
                                                  ("unexpected", broken, 1, "outcome_unknown:UnicodeEncodeError")):
            with self.subTest(label):
                before = len(self.audit_rows())
                with mock.patch.object(T.Tools, "call", replacement):
                    result = self.run_cli("notes", "create", TITLE, "--relay", "--request-id", "relay-%s-001" % label, stdin="body")
                self.assertEqual(result.code, code, result.err)
                rows = self.audit_rows()[before:]
                self.assertEqual([(r["tool"], r["outcome"]) for r in rows], [("notes create", outcome)], "exactly one row")
                self.assertNotIn("secret-looking", json.dumps(rows))

    def test_a_status_check_that_is_interrupted_leaves_no_audit_row(self):
        from agent_sync.mcp import tools as T

        def interrupted(self, name, arguments, **kwargs):
            raise KeyboardInterrupt()
        before = len(self.audit_rows())
        with mock.patch.object(T.Tools, "call", interrupted):
            self.assertEqual(self.run_cli("notes", "status", "nobody-asked-02").code, 130)
        self.assertEqual(len(self.audit_rows()), before)

    def test_an_oversized_body_is_a_usage_error_not_cut_short(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            over = self.run_cli("notes", "create", TITLE, stdin="x" * (N.MARKDOWN_MAX + 1))
            body = self.tmp / "long.html"
            body.write_text("y" * (N.MARKDOWN_MAX + 1))
            over_html = self.run_cli("notes", "create", TITLE, "--html", str(body))
            exact = self.run_cli("notes", "create", TITLE, stdin="x" * N.MARKDOWN_MAX)
        self.assertEqual((over.code, over_html.code), (2, 2), over.err + over_html.err)
        self.assertIn("longer than", over.err)
        self.assertEqual(exact.code, 0, exact.err)
        self.assertEqual(len(run.calls), 1, "only the body at the limit was written")
        self.assertEqual(len(run.calls[0]["input"]), N.MARKDOWN_MAX)

    def test_an_unreadable_seat_registry_refuses_the_write_instead_of_trusting_the_identity(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            result = self.run_cli("notes", "create", TITLE, stdin="x",
                                  env=dict(self.env(), FLEET_APPS_JSON="/nonexistent/fleet-apps.json"))
        self.assertNotEqual(result.code, 0)
        self.assertIn("seat registry", result.err)
        self.assertEqual(run.calls, [])

    def test_the_cli_and_the_mcp_tools_share_the_request_id_rows_and_a_timeout_blocks_a_retry(self):
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)
        a, b = self.direct(hang)
        with a, b:
            first = self.run_cli("notes", "create", TITLE, "--request-id", "cli-timeout-001", stdin="x")
        self.assertEqual(first.code, 5)
        self.assertIn("may or may not", first.err)
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            retry = self.run_cli("notes", "create", TITLE, "--request-id", "cli-timeout-001", stdin="x")
        self.assertEqual(retry.code, 5)
        self.assertIn("may have been interrupted", retry.err)
        self.assertEqual(run.calls, [])
        rows = json.loads((self.state_dir / "CLAUDE" / "mcp-idem.json").read_text())["rows"]
        self.assertEqual(rows["k:notes-direct:cli-timeout-001"]["state"], "unknown")  # kept, in the file the tools read

    def audit_rows(self):
        path = self.home / ".agent-sync" / "logs" / "identity.jsonl"
        if not path.exists():
            path = self.state_dir / "logs" / "identity.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_failed_direct_write_is_audited_by_its_code_without_the_body(self):
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)
        body = "PRIVATE-NOTE-BODY"
        a, b = self.direct(hang)
        with a, b:
            self.assertEqual(self.run_cli("notes", "create", TITLE, "--request-id", "audit-timeout-01", stdin=body).code, 5)
        a, b = self.direct(FakeRun(code=3))
        with a, b:
            self.assertEqual(self.run_cli("notes", "pin", TITLE).code, 5)
        a, b = self.direct(FakeRun())
        with a, b:
            self.assertEqual(self.run_cli("notes", "create", TITLE, stdin=body).code, 0)
        rows = self.audit_rows()
        self.assertEqual([(r["tool"], r["outcome"]) for r in rows],
                         [("notes create", "outcome_unknown"), ("notes pin", "not_found"), ("notes create", "ok")])
        self.assertTrue(all(r["identity"] == "CLAUDE" and r["transport"] == "cli" for r in rows))
        self.assertNotIn(body, json.dumps(rows))
        self.assertNotIn(TITLE, json.dumps(rows))

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
        self.fake.reactions.add((posted["id"], 11, "check"))  # a peer:  not the consumer
        self.assertEqual(json.loads(self.run_cli("notes", "status", "cli-request-0001", "--json").out)["status"], "queued")
        self.fake.reactions.add((posted["id"], 10, "check"))  # CLAUDE, the default consumer
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
