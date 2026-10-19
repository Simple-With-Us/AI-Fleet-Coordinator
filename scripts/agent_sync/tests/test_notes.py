"""Apple Notes from any transport (AFC-331):  the envelope, the title and flag rules, and the direct runner.

The runner is never run for real here:  every test injects the subprocess runner, so no osascript, no
Notes.app and no iCloud folder is touched.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import notes as N

FIXTURES = json.loads((Path(__file__).resolve().parent / "notes-fixtures.json").read_text())["cases"]
TITLE = "[AFC, Claude] relay check"


class FakeRun:
    """Stands in for subprocess.run:  records argv, stdin, env and the html file's mode, returns a canned result."""

    def __init__(self, code: int = 0, stdout: str = "created note id=x-coredata://abc in folder Coding\n", stderr: str = ""):
        self.calls: list[dict] = []
        self.result = (code, stdout, stderr)
        self.html_mode = None
        self.html_body = None

    def __call__(self, argv, **kwargs):
        call = {"argv": list(argv), "input": kwargs.get("input"), "env": kwargs.get("env"), "kwargs": kwargs}
        if "--html" in argv:
            path = argv[argv.index("--html") + 1]
            self.html_mode = stat.S_IMODE(os.stat(path).st_mode)
            self.html_body = Path(path).read_bytes()
        self.calls.append(call)
        code, out, err = self.result
        return subprocess.CompletedProcess(argv, code, out.encode(), err.encode())


def envelope(action="create", title=TITLE, markdown="body", flags=None, rid="requestid-0001", identity="CLAUDE"):
    return N.make_envelope(action, title, markdown, flags or {}, rid, identity)


class TitleAndFlagTests(unittest.TestCase):
    def refuses(self, title):
        with self.assertRaises(N.NotesError) as caught:
            N.check_title(title)
        self.assertEqual(caught.exception.code, "invalid_argument")
        return caught.exception.message

    def test_a_title_must_start_with_a_bracket_and_hold_nothing_special_to_applescript(self):
        self.assertEqual(N.check_title("  " + TITLE + "  "), TITLE)
        self.assertIn("must not be empty", self.refuses("   "))
        self.assertIn("must start with", self.refuses("--update [AFC, Claude] x"))
        self.assertIn("double quote", self.refuses('[AFC, Claude] a "quoted" word'))
        self.assertIn("backslash", self.refuses("[AFC, Claude] back\\slash"))
        self.assertIn("control character", self.refuses("[AFC, Claude] two\nlines"))
        self.assertIn("control character", self.refuses("[AFC, Claude] line separator"))
        self.assertIn("at most", self.refuses("[AFC, Claude] " + "x" * 300))

    def test_flags_are_a_closed_set_and_activate_is_not_in_it(self):
        self.assertEqual(N.check_flags({"pin": False, "summary": "short", "pr": "46, 47"}),
                         {"pin": False, "summary": "short", "pr": "46, 47"})
        for bad in ({"activate": True}, {"pin": "yes"}, {"summary": 'a "b"'}, {"summary": "x" * 300}, {"pr": "46; rm -rf"}):
            with self.assertRaises(N.NotesError, msg=bad):
                N.check_flags(bad)

    def test_the_topic_is_the_title_cut_to_sixty_characters(self):
        self.assertEqual(N.topic_for(TITLE), TITLE)
        topic = N.topic_for("[AFC, Claude] " + "long " * 30)
        self.assertLessEqual(len(topic), 60)
        self.assertGreater(len(topic), 50)
        self.assertTrue(topic.endswith("…"))

    def test_request_ids(self):
        self.assertRegex(N.new_request_id(), r"^nr[0-9a-f]{20}$")
        self.assertEqual(N.check_request_id("abcdefgh"), "abcdefgh")
        for bad in ("short", "has space 1234", "x" * 70, "-leading-dash"):
            with self.assertRaises(N.NotesError):
                N.check_request_id(bad)


class AgentNameTests(unittest.TestCase):
    def test_the_agent_in_the_title_must_be_the_senders_own_name(self):
        ok = [("[AFC, Claude] t", "CLAUDE"), ("[AFC, CT, Claude] t", "CLAUDE"), ("[UM, Grok Web] t", "GROK-WEB"),
              ("[UM, GROK-WEB] t", "GROK-WEB"), ("[UM, grok-web] t", "GROK-WEB"), ("[BF, BF-Deployer] t", "BF-DEPLOYER"),
              ("[BF, bf-deployer] t", "BF-DEPLOYER"), ("[AFC, GB-Fixer] t", "GB-FIXER"), ("[AFC, Muse Assist] t", "MA")]
        for title, identity in ok:
            self.assertIsNone(N.title_problem(title, identity), (title, identity))

    def test_a_wrong_agent_or_shape_is_refused(self):
        for title, identity in (("[AFC, Codex] t", "CLAUDE"), ("[AFC, Claude] t", "CODEX"), ("[AFC, GB-Fixer] t", "GB-DEPLOYER"),
                                ("[AFC] t", "CLAUDE"), ("[afc, Claude] t", "CLAUDE"), ("[AFC, Claude]t", "CLAUDE"),
                                ("[AFC, Claude, Codex] t", "CLAUDE"), ("[Claude] t", "CLAUDE")):
            self.assertIsNotNone(N.title_problem(title, identity), (title, identity))

    def test_a_missing_registry_still_accepts_the_tag_itself(self):
        self.assertIsNone(N.title_problem("[AFC, CLAUDE] t", "CLAUDE", registry_path="/nonexistent/fleet-apps.json"))


class ConsumerKnobTests(unittest.TestCase):
    """NOTES_RELAY_CONSUMER (Infisical, through fleet_knobs) names the consumer;  the default is CLAUDE."""

    def setUp(self):
        patcher = mock.patch.object(N, "KNOB_RESOLVER", None)  # the real resolver, over a fake knob answer
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, source, value="CODEX", reason=None, ignored=False):
        from fleet_knobs import knobs
        knob = knobs.Knob("NOTES_RELAY_CONSUMER", value, source, stale=source != "infisical", reason=reason, local_ignored=ignored)
        return mock.patch("fleet_knobs.knobs.resolve", return_value=knob)

    def test_each_source_is_labelled(self):
        with self.answer("infisical"):
            self.assertEqual(N.relay_consumer("CODEX"), ("CODEX", "infisical", False))
        with self.answer("lkg", reason="unreachable:timeout"):
            self.assertEqual(N.relay_consumer(None), ("CODEX", "last-known-good copy (stale)", False))
        with self.answer("default", "CLAUDE", reason="unreachable:cli-missing,lkg-absent"):
            seat, label, _ = N.relay_consumer(None)
            self.assertEqual(seat, "CLAUDE")
            self.assertEqual(label, "default CLAUDE (unreachable:cli-missing,lkg-absent)")
        with self.answer("infisical", ignored=True):
            self.assertTrue(N.relay_consumer("CLAUDE")[2], "a differing local seat is reported as ignored")

    def test_an_unavailable_knob_layer_is_the_labelled_default_and_never_an_error(self):
        with mock.patch("fleet_knobs.knobs.resolve", side_effect=RuntimeError("boom")):
            self.assertEqual(N.relay_consumer("CODEX"), ("CLAUDE", "default CLAUDE (fleet_knobs unavailable)", False))
        with mock.patch.dict("sys.modules", {"fleet_knobs": None, "fleet_knobs.knobs": None}):
            self.assertEqual(N.relay_consumer(None)[0], "CLAUDE")

    def test_the_real_knob_ignores_a_local_seat_that_differs(self):
        from fleet_knobs import knobs, registry
        spec = registry.BY_NAME["NOTES_RELAY_CONSUMER"]
        self.assertEqual((spec.kind, spec.default, spec.secret), ("str", "CLAUDE", False))
        from fleet_knobs import values
        value, narrowed, ignored = values.narrow(spec, "CODEX", "CLAUDE")
        self.assertEqual((value, narrowed, ignored), ("CODEX", False, True))


class EnvelopeTests(unittest.TestCase):
    def test_golden_envelopes_render_and_parse_exactly(self):
        for case in FIXTURES:
            with self.subTest(case["name"]):
                self.assertEqual(N.render_message(case["envelope"]), case["message"])
                self.assertEqual(N.parse_message("[CLAUDE·ab12cd34] " + case["message"]), case["envelope"])
                self.assertEqual(N.parse_message(case["message"]), case["envelope"])

    def test_a_body_over_the_limit_is_refused_by_the_envelope_and_the_direct_runner(self):
        edge = "x" * N.MARKDOWN_MAX
        self.assertEqual(envelope(markdown=edge)["markdown"], edge)
        with self.assertRaises(N.NotesError) as caught:
            envelope(markdown=edge + "x")
        self.assertEqual(caught.exception.code, "invalid_argument")
        raw = dict(envelope(), markdown=edge + "x")  # an envelope built some other way
        run = FakeRun()
        with self.assertRaises(N.NotesError) as caught:
            N.run_direct(raw, Path("/fake/apple-notes-coding.sh"), env={"HOME": "/h"}, runner=run)
        self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(run.calls, [], "nothing was written")

    def test_round_trip_survives_quotes_backslashes_newlines_and_unicode(self):
        body = 'line one\n"two" \\ three—\U0001F680 end.  Done.'
        env = envelope(markdown=body)
        self.assertEqual(N.parse_message("[CLAUDE·s8] " + N.render_message(env))["markdown"], body)
        self.assertNotIn("\n", N.envelope_json(env))
        N.envelope_json(env).encode("ascii")

    def test_the_parser_refuses_what_is_not_an_envelope(self):
        good = N.render_message(envelope())
        cases = {
            "empty": "",
            "no fence": "hello",
            "bad json": "```json\n{nope\n```",
            "not an object": "```json\n[1]\n```",
            "unknown field": good.replace('"v":1', '"v":1,"x":1'),
            "wrong version": good.replace('"v":1', '"v":2'),
            "bad title": good.replace("[AFC, Claude] relay check", "no bracket"),
            "bad flag": good.replace('"flags":{}', '"flags":{"activate":true}'),
            "bad request id": good.replace("requestid-0001", "x"),
            "orphan upload fields": good.replace('"flags":{}', '"flags":{},"markdown_bytes":3'),
            "too long": "```json\n" + "x" * 30000 + "\n```",
            "upload path": good.replace('"markdown":"body"', '"markdown":"","markdown_url":"/etc/passwd","markdown_bytes":3,'
                                                           '"markdown_sha256":"' + "a" * 64 + '"'),
        }
        for name, content in cases.items():
            with self.subTest(name):
                with self.assertRaises(N.EnvelopeError):
                    N.parse_message(content)

    def test_a_large_body_travels_as_an_uploaded_file_the_envelope_names(self):
        big = "a sentence.  " * 800
        env = envelope(markdown=big)
        self.assertTrue(N.needs_upload(env))
        self.assertFalse(N.needs_upload(envelope(markdown="small")))
        data = big.encode()
        ref = N.with_upload(env, url="/user_uploads/2/ab/9/note.md", data=data)
        self.assertEqual((ref["markdown"], ref["markdown_bytes"]), ("", len(data)))
        self.assertFalse(N.needs_upload(ref))
        parsed = N.parse_message(N.render_message(ref))
        self.assertEqual(N.resolve_upload(parsed, lambda path: data)["markdown"], big)
        with self.assertRaises(N.NotesError):
            N.resolve_upload(parsed, lambda path: data + b"x")
        with self.assertRaises(N.NotesError):
            N.resolve_upload(parsed, lambda path: b"z" * len(data))
        with self.assertRaises(N.NotesError):
            N.with_upload(env, url="https://elsewhere/x.md", data=data)


class DirectRunnerTests(unittest.TestCase):
    SCRIPT = Path("/fake/apple-notes-coding.sh")
    ENV = {"HOME": "/h", "PATH": "/usr/bin", "ZULIP_API_KEY": "never-passed", "AGENT_SYNC_STATE_DIR": "/s"}

    def run_direct(self, env, runner):
        return N.run_direct(env, self.SCRIPT, env=self.ENV, runner=runner)

    def test_markdown_goes_on_stdin_and_never_on_argv(self):
        run = FakeRun()
        body = "SECRET-LOOKING-BODY line\nsecond line"
        result = self.run_direct(envelope(markdown=body, flags={"summary": "short", "pr": "46", "notify": True}), run)
        self.assertEqual(result, {"status": "done", "note_id": "x-coredata://abc"})
        call = run.calls[0]
        self.assertEqual(call["input"], body.encode())
        self.assertEqual(call["argv"], [str(self.SCRIPT), "--notify", "--summary", "short", "--pr", "46", "--title", TITLE])
        self.assertFalse(any(body in part or "SECRET-LOOKING" in part for part in call["argv"]))
        self.assertEqual(call["kwargs"]["check"], False)

    def test_update_pin_and_unpin_modes(self):
        run = FakeRun()
        self.run_direct(envelope("update", markdown="b"), run)
        self.run_direct(envelope("pin", markdown=""), run)
        self.run_direct(envelope("unpin", markdown=""), run)
        self.assertEqual(run.calls[0]["argv"][1:], ["--update", "--title", TITLE])
        self.assertEqual(run.calls[1]["argv"][1:], ["--pin-only", "--title", TITLE])
        self.assertEqual(run.calls[2]["argv"][1:], ["--unpin-only", "--title", TITLE])
        self.assertEqual(run.calls[1]["input"], b"")

    def test_html_goes_through_a_mode_600_temp_file_that_is_removed(self):
        run = FakeRun()
        self.run_direct(envelope(markdown="<div><p>hi</p></div>", flags={"html": True}), run)
        argv = run.calls[0]["argv"]
        path = argv[argv.index("--html") + 1]
        self.assertEqual(run.html_mode, 0o600)
        self.assertEqual(run.html_body, b"<div><p>hi</p></div>")
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("<div>", " ".join(argv))
        self.assertEqual(run.calls[0]["input"], b"")

    def test_unpinned_create_sets_the_script_switch_and_the_child_env_holds_no_credential(self):
        run = FakeRun()
        self.run_direct(envelope(flags={"pin": False}), run)
        env = run.calls[0]["env"]
        self.assertEqual(env["APPLE_NOTES_PIN"], "0")
        self.assertEqual(set(env) - {"APPLE_NOTES_PIN"}, {"HOME", "PATH", "AGENT_SEAT"})  # AFC-361:  AGENT_SEAT is the verified identity

    def test_script_exit_codes_become_tool_errors(self):
        for code, expected in ((2, "invalid_argument"), (3, "not_found"), (4, "unavailable"), (9, "internal")):
            with self.subTest(code):
                with self.assertRaises(N.NotesError) as caught:
                    self.run_direct(envelope(), FakeRun(code=code, stderr="usage: nope " + "x" * 400))
                self.assertEqual(caught.exception.code, expected)
                self.assertLess(len(caught.exception.message), 400)

    def test_a_timeout_is_an_unknown_outcome_and_the_temp_file_still_goes(self):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)
        with self.assertRaises(N.NotesError) as caught:
            self.run_direct(envelope(markdown="<p>x</p>", flags={"html": True}), slow)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertIn("may or may not", caught.exception.message)

    def test_text_fields_cover_the_title_the_body_and_the_text_flags(self):
        env = envelope(markdown="body", flags={"summary": "sum", "pr": "46", "notify": True})
        self.assertEqual(N.text_fields(env), {"title": TITLE, "markdown": "body", "summary": "sum", "pr": "46"})
        self.assertEqual(N.text_fields(envelope("pin", markdown="")), {"title": TITLE})

    def test_availability_needs_macos_osascript_and_the_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "apps").mkdir()
            script = home / "apps" / N.SCRIPT_NAME
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
            env = {"AGENT_SYNC_NOTES_SCRIPT": str(home / "missing.sh")}
            have = lambda name: "/usr/bin/" + name  # noqa: E731
            self.assertEqual(N.direct_available(env, home, platform="darwin", which=have).name, N.SCRIPT_NAME)
            self.assertIsNone(N.direct_available(env, home, platform="linux", which=have))
            self.assertIsNone(N.direct_available(env, home, platform="darwin", which=lambda name: None))
            script.chmod(0o644)
            repo_copy = Path(N.__file__).resolve().parent.parent / N.SCRIPT_NAME
            expected = repo_copy if repo_copy.is_file() and os.access(repo_copy, os.X_OK) else None
            self.assertEqual(N.direct_available(env, home, platform="darwin", which=have), expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
