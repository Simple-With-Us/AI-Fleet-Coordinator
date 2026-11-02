"""`simple-notes create|update|pin|unpin|read|list|status|log|runner` and `simple-chat guide` (AFC-331, AFC-486):  the CLI
twin of the notes_* tools, run through agent_sync.cli.main as the wrapper runs it.

On a "Mac" (notes.direct_available patched to a path, the helper patched to FakeRun) a command runs the script
directly;  off one (the default here, whatever host runs the tests) it goes to the hosted gateway as the bot's own
fleet bearer through a fake fleet client.  No bearer file, the fleet knob and fleet-secrets are never read for real.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes_cli
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_sync import notes as N
from agent_sync import notes_client as NC
from agent_sync import notes_wire as W
from agent_sync.mcp import guide as G
from agent_sync.tests.harness import SESSION, Harness
from agent_sync.tests.test_notes import STORED_CONTENT_HTML, STORED_CONTENT_TEXT, STORED_TITLE, FakeRun, read_answer
from agent_sync import fleet_bearer as F
from agent_sync.tests.test_notes_client import FakeFleet, FakeOperator, record

TITLE = "[AFC, Claude] Cli Check"
MASTER = "⭐️ Background Jobs Master List"
VERBS = ("create", "update", "pin", "unpin", "read", "list", "log", "runner", "status")


class NotesCliHarness(Harness):
    def setUp(self):
        super().setUp()
        self.script = Path("/fake/apple-notes-coding.sh")
        # Off a Mac by default, on every host (a test run on the owner's Mac must never reach the real script), and
        # neither the fleet knob nor fleet-secrets is read for real.
        for patcher in (
            mock.patch.object(N, "direct_available", return_value=None),
            mock.patch("fleet_knobs.knobs.resolve", side_effect=RuntimeError("no fleet knobs in tests")),
            mock.patch.object(NC, "fleet_secrets_get", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def direct(self, run):
        """A Mac:  the script is found and the helper is `run`."""
        return mock.patch.object(N, "direct_available", return_value=self.script), mock.patch.object(N, "_run_helper", run)

    def gateway(self, gw=None):
        """Off a Mac, through the hosted gateway as the bot's own fleet bearer (FakeFleet, no file is read).
        Returns (gw, the sleeps the wait took, the patchers):  the fleet client, a fake sleep and a fake clock
        advanced by it, so a wait's deadline is exact."""
        gw = gw or FakeFleet()
        now = [1000.0]
        sleeps: list[float] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        return (
            gw,
            sleeps,
            (
                mock.patch("agent_sync.fleet_bearer.Client", gw.factory),
                mock.patch.object(NC.time, "sleep", fake_sleep),
                mock.patch.object(NC.time, "monotonic", lambda: now[0]),
            ),
        )

    def notes(self, *argv, **kw):
        return self.run_cli(*argv, prog="simple-notes", **kw)

    def audit_rows(self):
        path = self.home / ".agent-sync" / "logs" / "identity.jsonl"
        if not path.exists():
            path = self.state_dir / "logs" / "identity.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ParserTests(NotesCliHarness):
    def test_every_verb_parses_and_the_help_lists_them(self):
        top = self.notes("--help")
        self.assertEqual(top.code, 0, top.err)
        for verb in VERBS:
            with self.subTest(verb):
                self.assertRegex(top.out, r"\n\s+%s\s" % verb)
                self.assertEqual(self.notes(verb, "--help").code, 0)
        for chat_verb in ("dm-read", "topics", "guide", "relay"):
            self.assertNotRegex(top.out, r"\n\s+%s\s" % chat_verb)
        self.assertIn("--format", self.notes("read", "--help").out)
        self.assertIn("--no-pin", self.notes("update", "--help").out)
        self.assertNotIn("--relay", self.notes("create", "--help").out)

    def test_simple_chat_read_is_still_the_topic_reader_and_has_no_notes_verbs(self):
        chat = self.run_cli("read", "--help")
        self.assertEqual(chat.code, 0)
        self.assertIn("--topic", chat.out)
        self.assertNotIn("--format", chat.out)
        notes = self.notes("read", "--help")
        self.assertNotIn("--topic", notes.out)
        for verb in ("create", "update", "pin", "list", "log", "runner"):
            with self.subTest(verb):
                result = self.run_cli(verb, TITLE)
                self.assertEqual(result.code, 2)
                self.assertIn("invalid choice", result.err)

    def test_attach_and_detach_are_refused_by_the_commands_that_do_not_own_them(self):
        for verb in ("attach", "detach"):
            for prog in ("simple-notes", "simple-chat"):
                with self.subTest(prog=prog, verb=verb):
                    result = self.run_cli(verb, prog=prog)
                    if prog == "simple-chat":
                        self.assertNotIn("invalid choice", result.err)
                    else:
                        self.assertEqual(result.code, 2, result.err)
                        self.assertIn("invalid choice", result.err)

    def test_the_guide_command_prints_the_guide_or_a_section(self):
        text = G.read_text()
        whole = self.run_cli("guide")
        self.assertEqual(whole.code, 0)
        self.assertEqual(whole.out.strip(), text.strip())
        first = G.section_names(text)[0]
        one = self.run_cli("guide", first.lower())
        self.assertTrue(one.out.startswith("## " + first), one.out[:80])
        self.assertEqual(self.run_cli("guide", "nope").code, 2)
        self.assertEqual(self.run_cli("guide", env={"HOME": str(self.home), "AGENT_SYNC_REALM": self.fake.url}).code, 0)


class MacTests(NotesCliHarness):
    def test_a_create_runs_the_script_with_the_body_from_stdin_and_prints_done_direct(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            result = self.notes("create", TITLE, "--request-id", "cli-create-0001", stdin="body from stdin\n")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "cli-create-0001:  done (direct), note x-coredata://abc\n")
        call = run.calls[0]
        self.assertEqual(call["input"], b"body from stdin\n")
        self.assertEqual(call["argv"][1:], ["--session", SESSION, "--title", TITLE])
        self.assertEqual(
            (call["env"]["AGENT_SEAT"], call["env"]["AGENT_NOTES_CALLER"], call["env"]["AGENT_NOTES_REQUEST_ID"]),
            ("CLAUDE", "cli", "cli-create-0001"),
        )
        self.assertEqual(self.fake.messages, [], "nothing is posted to Zulip")
        self.assertEqual([r.path for r in self.fake.requests], [])

    def test_a_create_whose_pin_was_skipped_says_so(self):
        """AFC-454:  the helper exits 0 with the note saved unpinned, and the CLI result must not hide it."""
        a, b = self.direct(FakeRun(stderr="pin skipped: Pin Coding Note Shortcut failed; note saved unpinned\n"))
        with a, b:
            result = self.notes("create", TITLE, "--json", stdin="body\n")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual((info["status"], info["transport"], info["action"]), ("done", "direct", "create"))
        self.assertEqual(info["pin_skipped"], "Pin Coding Note Shortcut failed; note saved unpinned")
        a, b = self.direct(FakeRun())
        with a, b:
            clean = self.notes("create", TITLE, "--json", "--request-id", "clean-request-01", stdin="body\n")
        self.assertNotIn("pin_skipped", json.loads(clean.out))
        a, b = self.direct(FakeRun(stderr="pin skipped: the Shortcut list could not be read; note saved unpinned\n"))
        with a, b:
            text = self.notes("create", TITLE, "--request-id", "text-request-01", stdin="body\n")
        self.assertIn("pin skipped:  the Shortcut list could not be read", text.out)

    def test_the_body_comes_from_a_private_file_or_an_html_file_and_flags_pass_through(self):
        run = FakeRun()
        body = self.tmp / "body.md"
        body.write_text("# From a file")
        body.chmod(0o600)
        html = self.tmp / "body.html"
        html.write_text("<div><p>hi</p></div>")
        wide = self.tmp / "wide.md"
        wide.write_text("x")
        wide.chmod(0o644)
        a, b = self.direct(run)
        with a, b:
            first = self.notes("update", TITLE, "--body-file", str(body), "--no-pin", "--notify", "--summary", "short", "--pr", "46")
            second = self.notes("create", TITLE, "--html", str(html))
            third = self.notes("create", TITLE, "--body-file", str(wide))
            both = self.notes("create", TITLE, "words", "--html", str(html))
        self.assertEqual((first.code, second.code), (0, 0), first.err + second.err)
        self.assertEqual(run.calls[0]["input"], b"# From a file")
        self.assertEqual(
            run.calls[0]["argv"][1:],
            ["--update", "--notify", "--summary", "short", "--pr", "46", "--session", SESSION, "--title", TITLE],
        )
        self.assertEqual(run.calls[0]["env"]["APPLE_NOTES_PIN"], "0")
        self.assertIn("--html", run.calls[1]["argv"])
        self.assertEqual(run.html_body, b"<div><p>hi</p></div>")
        self.assertEqual((third.code, both.code), (2, 2))
        self.assertIn("give the body one way", both.err)
        self.assertEqual(len(run.calls), 2)

    def test_an_update_of_a_note_that_is_not_the_callers_never_creates_it(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            ok = self.notes("update", MASTER, stdin="new master list body")
        self.assertEqual(ok.code, 0, ok.err)
        self.assertEqual(run.calls[0]["argv"][1:3], ["--update", "--existing-only"])
        self.assertEqual(run.calls[0]["argv"][-2:], ["--title", MASTER])
        a, b = self.direct(FakeRun(code=3))
        with a, b:
            missing = self.notes("update", "[AFC, Codex] Not Here", stdin="x")
        self.assertEqual(missing.code, 5)
        self.assertIn("carries your own agent name", missing.err)

    def test_pin_and_unpin_take_only_a_title_any_note(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            self.assertEqual(self.notes("pin", TITLE).code, 0)
            self.assertEqual(self.notes("unpin", MASTER).code, 0)
            self.assertEqual(self.notes("pin", TITLE, "--notify").code, 2)
        self.assertEqual([c["argv"][1:] for c in run.calls], [["--pin-only", "--title", TITLE], ["--unpin-only", "--title", MASTER]])
        self.assertEqual([c["input"] for c in run.calls], [b"", b""])

    def test_read_prints_the_notes_content_in_each_format(self):
        for argv, expected in (
            ((), STORED_CONTENT_HTML),
            (("--format", "html"), STORED_CONTENT_HTML),
            (("--format", "text"), STORED_CONTENT_TEXT),
        ):
            with self.subTest(argv):
                run = FakeRun(stdout=read_answer())
                a, b = self.direct(run)
                with a, b:
                    result = self.notes("read", STORED_TITLE, *argv)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(result.out, expected + "\n")
                self.assertEqual(run.calls[0]["argv"][1:], ["--read", "--title", STORED_TITLE])
                self.assertEqual(run.calls[0]["input"], b"")
        a, b = self.direct(FakeRun(stdout=read_answer()))
        with a, b:
            raw = self.notes("read", STORED_TITLE, "--format", "raw", "--json")
        info = json.loads(raw.out)
        self.assertEqual((info["status"], info["transport"], info["note"]["format"]), ("done", "direct", "raw"))
        self.assertIn("Relayed for MM", info["note"]["body"], "raw is exactly what Notes stores")
        self.assertEqual(self.notes("read", STORED_TITLE, "--format", "pdf").code, 2)
        self.assertEqual(self.audit_rows(), [], "a read leaves no audit row")

    def test_list_prints_chicago_twelve_hour_times(self):
        answer = {
            "notes": [
                {"title": TITLE, "id": "x-coredata://n1", "created": "2026-10-21T10:00:00Z", "modified": "2026-10-22T22:03:00Z"},
                {"title": MASTER, "id": "x-coredata://n2", "created": "", "modified": "2026-01-05T18:30:00Z"},
            ],
            "total": 40,
            "truncated": True,
        }
        run = FakeRun(stdout=json.dumps(answer))
        a, b = self.direct(run)
        with a, b:
            result = self.notes("list", "--query", "a", "--limit", "2")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            "Thu, Oct 22, 5:03pm  %s\nMon, Jan 5, 12:30pm  %s\n(2 of 40 shown;  raise --limit or narrow --query)\n" % (TITLE, MASTER),
        )
        self.assertEqual(run.calls[0]["argv"][1:], ["--list", "--limit", "2", "--query", "a"])
        a, b = self.direct(FakeRun(stdout=json.dumps(answer)))
        with a, b:
            as_json = json.loads(self.notes("list", "--json").out)
        self.assertEqual((as_json["total"], len(as_json["notes"])), (40, 2))
        self.assertEqual(self.notes("list", "--limit", "0").code, 2)

    def test_log_prints_the_entries_of_this_macs_action_log(self):
        now = datetime.now(timezone.utc)
        stamp = lambda hours: (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = self.home / "Library" / "Logs" / "fleet" / "apple-notes-actions.jsonl"
        log.parent.mkdir(parents=True)
        rows = [
            {"id": "e1", "ts": stamp(30), "mode": "create", "outcome": "ok", "seat": "CLAUDE", "caller": "cli", "title": "old"},
            {"id": "e2", "ts": stamp(2), "mode": "update", "outcome": "ok", "seat": "CLAUDE", "caller": "runner", "title": MASTER, "reformatted": {"dates": 2, "gaps": 1}},
            {"id": "e3", "ts": stamp(1), "mode": "pin", "exit": 3, "seat": "CODEX", "caller": "mcp", "title": TITLE, "body": "never"},
        ]
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            result = self.notes("log")
            codex = self.notes("log", "--by-seat", "codex", "--json")
            week = self.notes("log", "--since-hours", "48", "--action", "create")
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        self.assertEqual(len(lines), 3)
        time_re = r"\w{3}, \w{3} \d{1,2}, \d{1,2}:\d{2}[ap]m"
        self.assertRegex(lines[0], r"^%s  pin  3  CODEX  mcp  %s$" % (time_re, re.escape(TITLE)))
        self.assertRegex(lines[1], r"^%s  update  ok  CLAUDE  runner  reformatted 2 dates, 1 gaps  %s$" % (time_re, re.escape(MASTER)))
        self.assertEqual(lines[2], "(2 entries from the mac log)")
        info = json.loads(codex.out)
        self.assertEqual((info["source"], info["count"], [e["id"] for e in info["entries"]]), ("mac", 1, ["e3"]))
        self.assertNotIn("body", info["entries"][0])
        self.assertIn("  create  ok  CLAUDE  cli  old", week.out)
        self.assertEqual(run.calls, [], "the log is read from the file, not the script")
        # The log is the whole fleet's and acts as no one, so it takes no --identity (--by-seat filters it).
        a, b = self.direct(run)
        with a, b:
            self.assertEqual(self.notes("log", "--identity", "CLAUDE").code, 2)

    def test_a_secret_in_the_body_or_the_summary_is_refused_before_anything_runs(self):
        run = FakeRun()
        leak = "token " + "ghp_" + "EXAMPLE0" * 5
        a, b = self.direct(run)
        with a, b:
            body = self.notes("create", TITLE, stdin=leak)
            summary = self.notes("create", TITLE, "--summary", leak, stdin="x")
        for result, field in ((body, "markdown"), (summary, "summary")):
            self.assertEqual(result.code, 2)
            self.assertIn("the %s looks like it contains" % field, result.err)
        self.assertEqual(run.calls, [])

    def test_a_repeated_request_id_writes_once(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            first = self.notes("create", TITLE, "--request-id", "cli-once-0001", "--json", stdin="x")
            again = self.notes("create", TITLE, "--request-id", "cli-once-0001", "--json", stdin="x")
            fresh = self.notes("create", TITLE, "--request-id", "cli-once-0002", "--json", stdin="x")
            plain = self.notes("create", TITLE, "--request-id", "cli-once-0002", stdin="x")
            status = self.notes("status", "cli-once-0001", "--json")
        self.assertEqual([json.loads(r.out)["duplicate"] for r in (first, again, fresh)], [False, True, False])
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(json.loads(again.out)["status"], "done")
        self.assertIn("already done before", plain.out)
        self.assertEqual(json.loads(status.out)["status"], "done")
        self.assertEqual(json.loads(status.out)["transport"], "direct")

    def test_an_interrupt_during_a_direct_write_is_audited_and_the_retry_is_refused(self):
        def interrupted(argv, **kwargs):
            raise KeyboardInterrupt()

        a, b = self.direct(interrupted)
        with a, b:
            first = self.notes("create", TITLE, "--request-id", "interrupt-request-1", stdin="secret-looking body")
        self.assertEqual(first.code, 130)
        rows = self.audit_rows()
        self.assertEqual([(r["tool"], r["outcome"]) for r in rows], [("notes create", "interrupted")])
        self.assertNotIn("secret-looking", json.dumps(rows))
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            retry = self.notes("create", TITLE, "--request-id", "interrupt-request-1", stdin="again")
        self.assertNotEqual(retry.code, 0)
        self.assertEqual(run.calls, [], "the interrupted write may have landed:  its id is refused")
        # Interrupted before anything was sent:  still one audit row, saying so.
        a, b = self.direct(run)
        with a, b, mock.patch.object(N, "make_request", side_effect=KeyboardInterrupt()):
            early = self.notes("create", TITLE, "--request-id", "interrupt-request-2", stdin="x")
        self.assertEqual(early.code, 130)
        self.assertEqual([(r["tool"], r["outcome"]) for r in self.audit_rows()][-1], ("notes create", "interrupted_before_send"))
        self.assertEqual(run.calls, [])

    def test_an_oversized_body_is_a_usage_error_not_cut_short(self):
        run = FakeRun()
        a, b = self.direct(run)
        body = self.tmp / "long.html"
        body.write_text("y" * (N.MARKDOWN_MAX + 1))
        with a, b:
            over = self.notes("create", TITLE, stdin="x" * (N.MARKDOWN_MAX + 1))
            over_html = self.notes("create", TITLE, "--html", str(body))
            exact = self.notes("create", TITLE, stdin="x" * N.MARKDOWN_MAX)
        self.assertEqual((over.code, over_html.code), (2, 2), over.err + over_html.err)
        self.assertIn("longer than", over.err)
        self.assertEqual(exact.code, 0, exact.err)
        self.assertEqual(len(run.calls), 1, "only the body at the limit was written")
        self.assertEqual(len(run.calls[0]["input"]), N.MARKDOWN_MAX)

    def test_an_unreadable_seat_registry_refuses_the_write_instead_of_trusting_the_identity(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            result = self.notes("create", TITLE, stdin="x", env=dict(self.env(), FLEET_APPS_JSON="/nonexistent/fleet-apps.json"))
        self.assertEqual(result.code, 3)
        self.assertIn("seat registry", result.err)
        self.assertEqual(run.calls, [])

    def test_the_cli_and_the_mcp_tools_share_the_request_id_rows_and_a_timeout_blocks_a_retry(self):
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        a, b = self.direct(hang)
        with a, b:
            first = self.notes("create", TITLE, "--request-id", "cli-timeout-001", stdin="x")
        self.assertEqual(first.code, 5)
        self.assertIn("may or may not", first.err)
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            retry = self.notes("create", TITLE, "--request-id", "cli-timeout-001", stdin="x")
            status = self.notes("status", "cli-timeout-001")
        self.assertEqual(retry.code, 5)
        self.assertIn("may have been interrupted", retry.err)
        self.assertEqual(run.calls, [])
        self.assertIn("cli-timeout-001:  unknown (direct)", status.out)
        rows = json.loads((self.state_dir / "CLAUDE" / "mcp-idem.json").read_text())["rows"]
        self.assertEqual(rows["k:notes-direct:cli-timeout-001"]["state"], "unknown")  # kept, in the file the tools read

    def test_a_failed_direct_write_is_audited_by_its_code_without_the_body(self):
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        body = "PRIVATE-NOTE-BODY"
        a, b = self.direct(hang)
        with a, b:
            self.assertEqual(self.notes("create", TITLE, "--request-id", "audit-timeout-01", stdin=body).code, 5)
        a, b = self.direct(FakeRun(code=3))
        with a, b:
            self.assertEqual(self.notes("pin", TITLE).code, 5)
        a, b = self.direct(FakeRun())
        with a, b:
            self.assertEqual(self.notes("create", TITLE, stdin=body).code, 0)
            self.assertEqual(self.notes("read", TITLE).code, 5, "the read answer is not JSON:  internal")
        rows = self.audit_rows()
        self.assertEqual(
            [(r["tool"], r["outcome"]) for r in rows],
            [("notes create", "outcome_unknown"), ("notes pin", "not_found"), ("notes create", "ok")],
        )
        self.assertTrue(all(r["identity"] == "CLAUDE" and r["transport"] == "cli" for r in rows))
        self.assertNotIn(body, json.dumps(rows))
        self.assertNotIn(TITLE, json.dumps(rows))

    def test_a_bad_title_or_another_seats_title_is_a_usage_error(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            self.assertEqual(self.notes("create", "no bracket", stdin="x").code, 2)
            self.assertEqual(self.notes("create", '[AFC, Claude] "q"', stdin="x").code, 2)
            self.assertEqual(self.notes("pin", "-x").code, 2)
            wrong = self.notes("create", "[AFC, Codex] Not Mine", stdin="x")
        self.assertEqual(wrong.code, 2)
        self.assertIn('names agent "Codex"', wrong.err)
        self.assertEqual(run.calls, [])
        a, b = self.direct(FakeRun(code=3))
        with a, b:
            result = self.notes("pin", TITLE)
        self.assertEqual(result.code, 5)
        self.assertIn("no note with that title", result.err)

    def test_a_missing_session_is_a_usage_error(self):
        run = FakeRun()
        a, b = self.direct(run)
        with a, b:
            result = self.notes("create", TITLE, stdin="x", env=self.env(CLAUDE_CODE_SESSION_ID=None))
            pin = self.notes("pin", TITLE, env=self.env(CLAUDE_CODE_SESSION_ID=None))
        self.assertEqual(result.code, 2)
        self.assertIn("--session ID", result.err)
        self.assertEqual(pin.code, 0, "a pin needs no session")
        self.assertEqual(len(run.calls), 1)
        self.assertEqual([(r["tool"], r["outcome"]) for r in self.audit_rows()][0], ("notes create", "session_required"))
        # A malformed session id is refused and audited too, by its code only.
        a, b = self.direct(run)
        with a, b:
            bad = self.notes("create", TITLE, stdin="x", env=self.env(CLAUDE_CODE_SESSION_ID="bad/char"))
        self.assertEqual(bad.code, 2)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual([(r["tool"], r["outcome"]) for r in self.audit_rows()][-1], ("notes create", "invalid_argument"))
        # So is a write refused for its body once the identity is known:  nothing ran, by its error type only.
        a, b = self.direct(run)
        with a, b:
            unreadable = self.notes("create", TITLE, "--html", str(self.home / "no-such.html"))
        self.assertEqual(unreadable.code, 2)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual([(r["tool"], r["outcome"]) for r in self.audit_rows()][-1], ("notes create", "refused:UsageError"))

    def test_the_cli_refuses_a_non_agent_identity(self):
        a, b = self.direct(FakeRun())
        with a, b:
            result = self.notes("pin", TITLE, "--identity", "SENTRY")
        self.assertEqual(result.code, 3)


class GatewayTests(NotesCliHarness):
    def test_an_update_off_a_mac_goes_through_the_gateway_and_waits(self):
        gw, sleeps, patches = self.gateway()
        gw.queue("notes_update", {"request_id": "cli-update-0001", "action": "update", "status": "running", "transport": "operator", "title": MASTER})
        gw.queue("notes_status", {"request_id": "cli-update-0001", "action": "update", "status": "done", "transport": "operator", "title": MASTER, "note_id": "x-coredata://n9"})
        with patches[0], patches[1], patches[2]:
            result = self.notes("update", MASTER, "--request-id", "cli-update-0001", stdin="the new body")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "cli-update-0001:  done (operator), note x-coredata://n9\n")
        self.assertEqual(sleeps, [2.0])
        name, sent, write = gw.calls[0]
        self.assertEqual((name, write), ("notes_update", True))
        self.assertEqual(sent["title"], MASTER)
        self.assertEqual(sent["markdown"], "the new body")
        self.assertEqual(sent["session"], SESSION)
        self.assertEqual(sent["request_id"], "cli-update-0001")
        self.assertEqual(sent["wait_seconds"], 0)
        self.assertNotIn("identity", sent, "the gateway derives the seat from the bearer")
        self.assertEqual([c[0] for c in gw.calls], ["notes_update", "notes_status"])
        self.assertEqual(gw.constructed, [(str(self.home / ".secrets" / "fleet-tools" / "claude"), "CLAUDE")])
        self.assertEqual(self.fake.messages, [], "no Zulip message")
        self.assertEqual([(r["tool"], r["outcome"]) for r in self.audit_rows()], [("notes update", "ok")])
    def test_no_wait_answers_queued_at_once(self):
        for argv in (("--no-wait",), ("--wait", "0")):
            with self.subTest(argv):
                gw, sleeps, patches = self.gateway()
                gw.queue("notes_pin", {"request_id": "cli-queued-0001", "action": "pin", "status": "queued", "transport": "operator"})
                with patches[0], patches[1], patches[2]:
                    result = self.notes("pin", MASTER, "--request-id", "cli-queued-0001", *argv)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(
                    result.out,
                    "cli-queued-0001:  queued (operator);  the Mac runner has not finished it:  "
                    "simple-notes status cli-queued-0001\n",
                )
                self.assertEqual((sleeps, [c[0] for c in gw.calls]), ([], ["notes_pin"]))
        self.assertEqual(self.notes("pin", MASTER, "--wait", "301").code, 2)
    def test_a_wait_that_runs_out_answers_where_the_request_stands(self):
        gw, sleeps, patches = self.gateway()
        running = {"request_id": "cli-slow-00001", "action": "pin", "status": "running", "transport": "operator", "title": MASTER}
        gw.queue("notes_pin", dict(running))
        gw.queue("notes_status", dict(running), dict(running), dict(running), dict(running))
        with patches[0], patches[1], patches[2]:
            result = self.notes("pin", MASTER, "--request-id", "cli-slow-00001", "--wait", "3", "--json")
        info = json.loads(result.out)
        self.assertEqual(info["status"], "running")
        self.assertEqual(sum(sleeps), 3)
    def test_status_read_list_and_log_off_a_mac(self):
        gw, _, patches = self.gateway()
        gw.queue("notes_status", {"request_id": "cli-status-0001", "status": "refused", "error": {"code": "not_found", "message": "no note with that title"}})
        gw.queue("notes_read", {"request_id": "cli-read-00001", "action": "read", "status": "done", "transport": "operator", "note": {"title": MASTER, "body": "<div>Jobs</div>", "format": "html"}})
        gw.queue("notes_log", {"entries": [{"id": "e1", "ts": "2026-10-22T22:03:00Z", "mode": "update", "outcome": "ok", "seat": "CODEX", "caller": "runner", "title": MASTER}], "source": "operator", "count": 1})
        with patches[0], patches[1], patches[2]:
            status = self.notes("status", "cli-status-0001")
            read = self.notes("read", MASTER, "--request-id", "cli-read-00001")
            log = self.notes("log", "--by-seat", "CODEX", "--limit", "5")
        self.assertEqual(status.code, 0, status.err)
        self.assertEqual(status.out, "cli-status-0001:  refused, not_found:  no note with that title\n")
        self.assertEqual(read.out, "<div>Jobs</div>\n")
        self.assertEqual(log.out, "Thu, Oct 22, 5:03pm  update  ok  CODEX  runner  %s\n(1 entries from the operator log)\n" % MASTER)
        self.assertEqual(gw.calls[-1][:2], ("notes_log", {"limit": 5, "by_seat": "CODEX"}))
        self.assertEqual(self.audit_rows(), [], "status, read and log leave no audit row")
    def test_log_honors_explicit_seat_and_platform_default_without_env_seat(self):
        for argv in (("--as", "CODEX"), ("--default-seat", "CODEX")):
            with self.subTest(argv=argv):
                gw, _, patches = self.gateway()
                gw.queue("notes_log", {"entries": [], "source": "operator", "count": 0})
                env = {k: v for k, v in self.env().items() if k not in ("AGENT_SEAT", "AGENT_TAG")}
                with patches[0], patches[1], patches[2]:
                    result = self.notes("log", *argv, env=env)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(gw.constructed, [(str(self.home / ".secrets" / "fleet-tools" / "codex"), None)])

    def test_log_cannot_override_or_fill_an_unassigned_launcher_seat(self):
        cases = (
            ({"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "CLAUDE"}, ("--as", "CODEX")),
            ({"AGENT_LAUNCHER": "botfleet"}, ("--default-seat", "CODEX")),
        )
        for launch, argv in cases:
            with self.subTest(launch=launch, argv=argv):
                gw, _, patches = self.gateway()
                env = {k: v for k, v in self.env().items() if k not in ("AGENT_SEAT", "AGENT_TAG")}
                env.update(launch)
                with patches[0], patches[1], patches[2]:
                    result = self.notes("log", *argv, env=env)
                self.assertEqual(result.code, 3, result.err)
                self.assertEqual(gw.constructed, [])
                self.assertEqual(gw.calls, [])

    def test_exit_codes(self):
        # 2 usage
        self.assertEqual(self.notes("create", TITLE, "--wait", "x").code, 2)
        # 3 not configured:  off a Mac with no bearer file of the bot's own, the shared service credential is not used
        missing = self.notes("pin", MASTER)
        self.assertEqual(missing.code, 3)
        self.assertIn("fleet bearer file", missing.err)
        self.assertIn("no fallback", missing.err)
        # 5 the gateway refused this bearer
        gw, _, patches = self.gateway()
        gw.queue("notes_pin", F.FleetError("not_authorized", "gateway HTTP 401"))
        with patches[0]:
            self.assertEqual(self.notes("pin", MASTER).code, 5)
        # 5 the note was refused;  the error itself is one status check away, under the same request id
        gw, _, patches = self.gateway()
        gw.queue("notes_pin", {"request_id": "cli-refused-001", "action": "pin", "status": "refused", "error": {"code": "not_found", "message": "no note with that title"}})
        with patches[0]:
            refused = self.notes("pin", MASTER, "--request-id", "cli-refused-001")
        self.assertEqual(refused.code, 5)
        self.assertIn("notes request was refused", refused.err)
        self.assertIn("simple-notes status cli-refused-001", refused.err)
        gw, _, patches = self.gateway()
        gw.queue("notes_pin", {"request_id": "cli-unknown-001", "action": "pin", "status": "unknown", "error": {"code": "outcome_unknown", "message": "the lease ran out"}})
        with patches[0]:
            unknown = self.notes("pin", MASTER, "--request-id", "cli-unknown-001")
        self.assertEqual(unknown.code, 5)
        self.assertIn("cli-unknown-001", unknown.err)
        # 5 the gateway cannot be reached
        gw, _, patches = self.gateway()
        gw.queue("notes_pin", F.FleetError("unavailable", "gateway transport or response failed"))
        with patches[0]:
            down = self.notes("pin", MASTER)
        self.assertEqual(down.code, 5)
        self.assertIn("gateway transport", down.err)
    def test_a_secret_or_a_missing_session_sends_nothing(self):
        gw, _, patches = self.gateway()
        with patches[0]:
            leak = self.notes("update", MASTER, stdin="token " + "ghp_" + "EXAMPLE0" * 5)
            nosession = self.notes("update", MASTER, stdin="x", env=self.env(CLAUDE_CODE_SESSION_ID=None))
        self.assertEqual((leak.code, nosession.code), (2, 2))
        self.assertEqual(gw.calls, [])
    def test_an_interrupted_or_broken_gateway_write_is_audited_once(self):
        def interrupted(self, name, arguments, write=False):
            raise KeyboardInterrupt()

        def broken(self, name, arguments, write=False):
            raise UnicodeEncodeError("ascii", "caf\xe9 secret-looking", 3, 4, "ordinal not in range")

        for label, replacement, code, outcome in (
            ("interrupted", interrupted, 130, "interrupted"),
            ("unexpected", broken, 1, "outcome_unknown:UnicodeEncodeError"),
        ):
            with self.subTest(label):
                before = len(self.audit_rows())
                gw, _, patches = self.gateway()
                with patches[0], mock.patch.object(FakeFleet, "call", replacement):
                    result = self.notes("create", TITLE, "--request-id", "op-%s-001" % label, stdin="body")
                self.assertEqual(result.code, code, result.err)
                rows = self.audit_rows()[before:]
                self.assertEqual([(r["tool"], r["outcome"]) for r in rows], [("notes create", outcome)], "exactly one row")
                self.assertNotIn("secret-looking", json.dumps(rows))
    def test_a_status_check_that_fails_or_is_interrupted_leaves_no_audit_row(self):
        failed = self.notes("status", "nobody-asked-01")
        self.assertEqual(failed.code, 3, failed.err)

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()

        with mock.patch.object(NC, "status", interrupted):
            self.assertEqual(self.notes("status", "nobody-asked-02").code, 130)
        self.assertEqual(self.audit_rows(), [])

    def test_the_runner_verb_runs_one_pass(self):
        op = FakeOperator(principal=W.RUNNER)
        client = op.client(principal=W.RUNNER)
        with mock.patch.object(NC, "operator_client", lambda env, principal=W.SERVER, **kw: client):
            once = self.notes("runner", "--once", "--claim-wait", "0")
            bad = self.notes("runner", "--once", "--claim-wait", "26")
        self.assertEqual(once.code, 0, once.err)
        self.assertEqual(once.out, "notes runner:  nothing queued\n")
        self.assertEqual([c["path"] for c in op.calls], ["/notes/v1/runner/claim"])
        self.assertEqual(op.calls[0]["payload"], {"wait": 0})
        self.assertEqual(bad.code, 2)
        self.assertEqual(self.notes("runner", "--once").code, 3, "no Operator origin:  not configured")


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
