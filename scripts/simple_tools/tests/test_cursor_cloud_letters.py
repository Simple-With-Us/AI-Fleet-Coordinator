"""The Cursor Cloud letter sweep:  every open Cloud agent takes a letter at location `cloud` and is steered once to
put that letter in its title.  A chat that already carries its letter is left alone.  A steer the account refuses
falls back to one DM for a title that names a GB bot.  A run that only looks sends nothing.  The registry runs in
process against a temporary registry;  the Cursor API and `simple-chat dm` are replaced."""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from simple_tools import cursor_cloud_letters as CCL
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools.tests.registry_fake import FakeRegistry

A1 = "bc-11111111-aaaa-4bbb-8ccc-000000000001"
A2 = "bc-22222222-aaaa-4bbb-8ccc-000000000002"


def stamp(hours_ago: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def agent(sid: str, title: str, hours_ago: float = 1) -> dict:
    return {"session_id": sid, "title": title, "url": "https://cursor.com/agents?id=" + sid, "platform": "cursor-cloud",
            "updated_at": stamp(hours_ago)}


class Fake:
    def __init__(self, code: int = 0, out: str = "") -> None:
        self.calls: list[list[str]] = []
        self.code, self.out = code, out

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.code, self.out, "")


class Steer:
    def __init__(self, outcome: str = "sent") -> None:
        self.calls: list[tuple[str, str]] = []
        self.outcome = outcome

    def __call__(self, sid: str, text: str) -> str:
        self.calls.append((sid, text))
        return self.outcome


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        # The one assigner:  the registry API, served in process over a temporary store.
        self.assigner = FakeRegistry(os.path.join(self.tmp.name, "assigner")).install()
        self.addCleanup(self.assigner.uninstall)
        self.db = self.assigner.db
        self.env = {"HOME": self.tmp.name, "SIMPLE_TOOLS_STATE_DIR": self.state, "AGENT_SEAT": "CLAUDE",
                    **self.assigner.env()}
        self.agents = [agent(A1, "GB-Director: tidy the board"), agent(A2, "fix the login page")]
        self.dm = Fake()
        self.steer = Steer()

    def transport(self, argv: list[str]) -> dict:
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.env, stdout=out)
        return json.loads(out.getvalue())

    def run_sweep(self, **kw) -> dict:
        return CCL.sweep(
            self.env, discover=lambda: list(self.agents), runner=self.dm, steer=self.steer,
            transport=self.transport, **kw,
        )

    def label(self, sid: str) -> str:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("cursor", "cloud", PL.SCOPE, sid)["label"]
        finally:
            reg.close_db()

    def test_every_open_agent_takes_a_letter_and_is_steered_once(self) -> None:
        out = self.run_sweep()
        self.assertEqual(out["agents"], 2)
        l1, l2 = self.label(A1), self.label(A2)
        self.assertRegex(l1, r"^[A-Z]{2}$")
        self.assertNotEqual(l1, l2)
        self.assertEqual([sid for sid, _ in self.steer.calls], [A1, A2])
        self.assertIn('"%s: GB-Director: tidy the board"' % l1, self.steer.calls[0][1])
        self.assertIn("Then stop.", self.steer.calls[0][1])
        self.assertIn("\u00a0 ", self.steer.calls[0][1])
        self.assertEqual(self.dm.calls, [])
        self.run_sweep()  # the same letter again, inside the retry window:  no second steer
        self.assertEqual(len(self.steer.calls), 2)
        self.assertEqual(self.label(A1), l1)

    def test_a_chat_that_carries_its_letter_sends_nothing(self) -> None:
        self.run_sweep()
        l1 = self.label(A1)
        self.dm.calls.clear()
        self.agents[0] = agent(A1, "%s: GB-Director: tidy the board" % l1)
        CCL.write_state(self.env, {})  # even with nothing remembered
        self.steer.calls.clear()
        self.run_sweep()
        self.assertEqual([sid for sid, _ in self.steer.calls], [A2])  # A1 already carries its letter
        self.assertEqual(self.dm.calls, [])
        self.assertEqual(self.label(A1), l1)

    def test_a_dry_run_registers_and_sends_nothing(self) -> None:
        out = self.run_sweep(dry_run=True)
        self.assertEqual([r.get("bot") for r in out["rows"]], ["GB-Director", None])
        self.assertEqual(self.steer.calls, [])
        self.assertEqual(self.dm.calls, [])
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertIsNone(reg.lookup("cursor", "cloud", PL.SCOPE, A1))
        finally:
            reg.close_db()

    def test_a_steer_that_could_not_start_is_tried_again(self) -> None:
        self.steer = Steer("not-started")
        out = self.run_sweep()
        self.assertEqual(out["rows"][0]["steer"], "not-started")
        self.steer = Steer()
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), 2)  # both agents, now that steering can start

    def test_a_refused_steer_asks_the_named_bot_and_a_refused_dm_is_tried_again(self) -> None:
        self.steer = Steer("unavailable")
        self.dm = Fake(code=2, out="refused: unknown name GB-Director")
        row = self.run_sweep()["rows"][0]
        self.assertEqual(row["steer"], "unavailable")
        self.assertEqual(row["dm"], "refused")
        self.assertEqual(len(self.dm.calls), 1)  # the untitled chat names no bot
        self.dm = Fake()
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), 2)  # the refusal is not steered again
        self.assertEqual(len(self.dm.calls), 1)

    def test_a_steer_that_may_have_landed_is_not_repeated_at_once(self) -> None:
        self.steer = Steer("unknown")
        self.assertEqual(self.run_sweep()["rows"][0]["steer"], "unknown")
        sent = len(self.steer.calls)
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), sent)

    def test_past_the_budget_a_steer_waits_for_the_next_run(self) -> None:
        ticks = iter([0.0, 1000.0, 1000.0])
        out = self.run_sweep(clock=lambda: next(ticks), budget=60)
        self.assertEqual(out["rows"][0]["steer"], "deferred")
        self.assertEqual(self.steer.calls, [])
        self.assertIn("label", out["rows"][1])  # every agent is still registered
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), 2)

    def test_a_title_cannot_mention_anyone_or_format_the_dm(self) -> None:
        self.steer = Steer("unavailable")
        self.agents = [dict(agent(A1, "GB-Director: ping @**all** `x` [y](z)"), url="javascript:alert(1)")]
        self.run_sweep()
        text = self.dm.calls[0][-1]
        self.assertNotIn("@**all**", text)
        self.assertNotIn("`", text)
        self.assertNotIn("javascript", text)
        self.assertIn(A1, text)

    def test_an_agent_of_the_wrong_shape_gets_a_row_and_the_others_go_on(self) -> None:
        self.agents = ["nope", {"title": "x"}, dict(agent(A1, "GB-Director: tidy"), title=7),
                       dict(agent(A1, "GB-Director: tidy"), updated_at=True), agent(A2, "fix the login page")]
        out = self.run_sweep()
        self.assertEqual([r["error"] for r in out["rows"] if "error" in r], [
            "invalid agent: the agent must be an object",
            "invalid agent: session_id must be a non-empty string",
            "invalid agent: title must be a string",
            "invalid agent: updated_at must be a number or a string",
        ])
        self.assertEqual((out["invalid"], out["agents"]), (4, 1))
        self.assertEqual([sid for sid, _ in self.steer.calls], [A2])  # the one good agent is registered and steered
        self.assertRegex(self.label(A2), r"^[A-Z]{2}$")

    def test_one_agents_failure_does_not_stop_the_next(self) -> None:
        class Boom(Steer):
            def __call__(self, sid: str, text: str) -> str:
                if sid == A1:
                    raise RuntimeError("words from a title")
                return super().__call__(sid, text)

        self.steer = Boom()
        out = self.run_sweep()
        self.assertEqual(out["rows"][0]["error"], "failed (RuntimeError)")  # the type only, never the message
        self.assertNotIn("words from a title", json.dumps(out))
        self.assertEqual([sid for sid, _ in self.steer.calls], [A2])
        self.assertIn("label", out["rows"][1])

    def test_a_secret_shaped_word_in_a_title_never_reaches_the_dm(self) -> None:
        key = "ghp_" + "a1B2" * 9  # built here, so no key-shaped literal is in the file
        self.steer = Steer("unavailable")
        self.agents = [agent(A1, "GB-Director: rotate %s today" % key)]
        self.run_sweep()
        text = self.dm.calls[0][-1]
        self.assertNotIn(key, text)
        self.assertIn("rotate", text)
        self.assertIn("today", text)
        self.assertEqual(CCL.plain(key), "")

    def test_a_refused_dm_is_asked_again_and_is_final_after_three_refusals(self) -> None:
        self.steer = Steer("unavailable")
        self.dm = Fake(code=2, out="refused: unknown name GB-Director")
        for n in (1, 2, 3):
            self.assertEqual(self.run_sweep()["rows"][0]["dm"], "refused")
            self.assertEqual(len(self.dm.calls), n)
        self.assertNotIn("dm", self.run_sweep()["rows"][0])  # the fourth pass does not ask
        self.assertEqual(len(self.dm.calls), 3)
        self.assertEqual(CCL.read_state(self.env)[A1]["dm_refusals"], 3)

    def test_a_run_that_dies_in_the_send_does_not_send_the_dm_again(self) -> None:
        self.steer = Steer("unavailable")

        def dies(argv, **kw):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            CCL.sweep(self.env, discover=lambda: list(self.agents), runner=dies, steer=self.steer,
                      transport=self.transport)
        self.assertEqual(CCL.read_state(self.env)[A1]["dm"], "unknown")  # written before the send
        self.run_sweep()
        self.assertEqual(self.dm.calls, [])

    def test_a_dm_that_could_not_start_is_not_remembered_and_is_tried_again(self) -> None:
        self.steer = Steer("unavailable")

        def missing(argv, **kw):
            raise OSError("no such program")

        out = CCL.sweep(self.env, discover=lambda: list(self.agents), runner=missing, steer=self.steer,
                        transport=self.transport)
        self.assertEqual(out["rows"][0]["dm"], "not-started")
        self.assertNotIn("dm", CCL.read_state(self.env)[A1])
        self.run_sweep()
        self.assertEqual(len(self.dm.calls), 1)

    def test_a_gone_agent_is_forgotten(self) -> None:
        self.run_sweep()
        self.agents = [self.agents[1]]
        self.run_sweep()
        self.assertNotIn(A1, CCL.read_state(self.env))

    def test_a_partial_discovery_forgets_nothing_and_tells_no_bot_twice(self) -> None:
        self.run_sweep()
        everyone = list(self.agents)
        self.agents = [self.agents[1]]
        self.run_sweep(complete=lambda: False)
        self.assertIn(A1, CCL.read_state(self.env))
        self.agents = everyone
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), 2)


    def test_an_agent_untouched_past_the_window_takes_no_letter_and_wakes_no_bot(self) -> None:
        self.agents = [agent(A1, "GB-Director: an old chat", hours_ago=24 * 20), agent(A2, "fix the login page")]
        out = self.run_sweep()
        self.assertEqual((out["agents"], out["stale"]), (1, 1))
        self.assertEqual(self.dm.calls, [])
        self.assertEqual([sid for sid, _ in self.steer.calls], [A2])
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertIsNone(reg.lookup("cursor", "cloud", PL.SCOPE, A1))
        finally:
            reg.close_db()
        self.assertRegex(self.label(A2), r"^[A-Z]{2}$")

    def test_an_agent_that_ages_out_and_wakes_again_is_not_told_twice(self) -> None:
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), 2)
        self.agents[0] = agent(A1, "GB-Director: tidy the board", hours_ago=40)  # past the window, still open
        self.run_sweep()
        self.assertIn(A1, CCL.read_state(self.env))
        self.agents[0] = agent(A1, "GB-Director: tidy the board")  # it wakes before the registry frees its letter
        self.run_sweep()
        self.assertEqual(len(self.steer.calls), 2)

    def test_a_busy_agent_is_told_to_continue_and_two_steers_then_ask_the_bot(self) -> None:
        self.agents[0]["state"] = "busy"
        self.run_sweep(now=1_000)
        self.assertIn("continue the work already in progress", self.steer.calls[0][1])
        self.run_sweep(now=1_000 + CCL.STEER_RETRY)
        self.assertEqual(len(self.steer.calls), 4)  # the second steer is the last
        self.assertEqual(self.dm.calls, [])  # that steer is given time to land
        self.run_sweep(now=1_000 + 2 * CCL.STEER_RETRY)
        self.assertEqual(len(self.steer.calls), 4)
        self.assertEqual(len(self.dm.calls), 1)
        self.assertIn("GB-Director", self.dm.calls[0][3])

    def test_an_agent_with_no_readable_time_is_skipped(self) -> None:
        no_time = agent(A1, "GB-Director: tidy the board")
        del no_time["updated_at"]
        self.agents = [no_time, dict(agent(A2, "GB-Fixer: x"), updated_at="yesterday")]
        out = self.run_sweep()
        self.assertEqual((out["agents"], out["stale"]), (0, 2))
        self.assertEqual(self.dm.calls, [])
        self.assertEqual(self.steer.calls, [])


class SharedUnitSweepTests(unittest.TestCase):
    """The sweep as the shared Operator unit runs it (`--no-dm`, `--api-only`):  no Zulip DM ever goes out, and every
    row registers under the Cursor seat, never under the seat in AGENT_SEAT."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        # The one assigner:  the registry API, served in process over a temporary store.
        self.assigner = FakeRegistry(os.path.join(self.tmp.name, "assigner")).install()
        self.addCleanup(self.assigner.uninstall)
        self.db = self.assigner.db
        self.env = {"HOME": self.tmp.name, "SIMPLE_TOOLS_STATE_DIR": self.state, "AGENT_SEAT": "CLAUDE",
                    **self.assigner.env()}
        self.agents = [agent(A1, "GB-Director: tidy the board"), agent(A2, "fix the login page")]
        self.dm = Fake()
        self.sent: list[list[str]] = []

    def transport(self, argv: list[str]) -> dict:
        self.sent.append(list(argv))
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.env, stdout=out)
        return json.loads(out.getvalue())

    def test_no_dm_sends_nothing_even_when_a_bot_would_be_asked(self) -> None:
        steer = Steer("unavailable")  # the account refuses:  without --no-dm the named bot would get a DM now
        out = CCL.sweep(self.env, discover=lambda: list(self.agents), runner=self.dm, steer=steer,
                        transport=self.transport, send_dms=False)
        self.assertEqual(self.dm.calls, [])
        self.assertEqual(out["rows"][0]["dm"], "off")
        self.assertNotIn("dm", out["rows"][1])  # the untitled chat names no bot
        self.assertNotIn("dm", CCL.read_state(self.env)[A1])  # nothing is recorded as told
        self.assertEqual(len(steer.calls), 2)  # the steer through the Cursor API still goes out

    def test_rows_register_under_the_cursor_seat_whatever_agent_seat_says(self) -> None:
        CCL.sweep(self.env, discover=lambda: list(self.agents), runner=self.dm, steer=Steer(),
                  transport=self.transport, send_dms=False)
        self.assertEqual(len(self.sent), 2)
        for argv in self.sent:
            self.assertEqual(argv[argv.index("--seat") + 1], "CURSOR")
            self.assertNotIn("CLAUDE", argv)
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertEqual(reg.lookup("cursor", "cloud", PL.SCOPE, A1)["seat"], "CURSOR")
        finally:
            reg.close_db()

    def test_api_only_with_no_registry_url_tries_no_other_route(self) -> None:
        with mock.patch.object(PL, "with_registry_env", side_effect=AssertionError("env file read")):
            answer = CCL.register(
                {"HOME": self.tmp.name, "AGENT_SEAT": "CLAUDE"}, agent(A1, "x"), now=0.0, transport=None, api_only=True
            )
        self.assertEqual(answer["code"], "not_configured")
        self.assertIn("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL", answer["error"])
        self.assertEqual(self.dm.calls, [])  # no ssh hop:  nothing ran


class UnitRunTests(unittest.TestCase):
    """`sweep --api-only --no-dm`, run through main() the way the unit runs it:  the registry URL and key come only from
    the knobs (the process environment does not set them), AGENT_SEAT and SESSION_LETTER_REMOTE are ignored, the Cursor
    key is read with CURSOR_LETTERS_INFISICAL_TOKEN, and no DM goes out."""

    def setUp(self) -> None:
        from simple_tools import registry_api
        from simple_tools.tests.registry_fake import FakeRegistry

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fake = FakeRegistry().install()
        self.addCleanup(self.fake.uninstall)
        knobs = {registry_api.URL_KNOB: self.fake.url, registry_api.KEY_KNOB: self.fake.keys["OPERATOR"]}
        registry_api._knob_value = lambda name: knobs.get(name, "")  # restored by the fake's uninstall
        self.env = {
            "HOME": tmp.name,
            "SIMPLE_TOOLS_STATE_DIR": os.path.join(tmp.name, "state"),
            "AGENT_SEAT": "CLAUDE",
            "SESSION_LETTER_REMOTE": "mac-somewhere",
            CCL.CURSOR_TOKEN_ENV: "st." + "fixture-cursor-letters-token-0123",
        }
        self.cloud = mock.MagicMock()
        self.cloud.return_value.discover.return_value = [agent(A1, "GB-Director: tidy"), agent(A2, "fix it")]
        self.cloud.return_value.steer.return_value = "unavailable"
        self.cloud.return_value.last_discovery_status = "complete"

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", self.cloud), \
                mock.patch.object(CCL, "read_cursor_key", return_value="fixture-cursor-key") as key, \
                mock.patch.object(CCL, "send_dm", side_effect=AssertionError("a DM was sent")), \
                mock.patch.object(PL, "with_registry_env", side_effect=AssertionError("env file read")), \
                mock.patch("sys.stdout", out):
            code = CCL.main(argv)
            self.key_calls = key.call_count
            self.left = dict(os.environ)
        return code, out.getvalue()

    def test_the_unit_command_registers_under_cursor_and_sends_no_dm(self) -> None:
        code, out = self.run_main(["sweep", "--api-only", "--no-dm"])
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertEqual([row.get("dm") for row in report["rows"]], ["off", None])
        for sid in (A1, A2):
            row = self.fake.lookup("cursor", "cloud", PL.SCOPE, sid)
            self.assertEqual(row["seat"], "CURSOR")
        self.assertEqual(self.key_calls, 1)
        surfaces = self.cloud.call_args.kwargs["surfaces"]
        self.assertEqual(surfaces.load_api_key(), "fixture-cursor-key")
        self.assertNotIn("CURSOR_SYNC_API_KEY", self.left)  # the key never enters the environment
        self.assertNotIn("fixture-cursor-key", out)

    def test_the_unit_command_with_no_cursor_token_stops_before_any_cursor_call(self) -> None:
        # A missing env file or token line:  the run stops with service-token-required and never falls back to a key
        # in the environment, `fleet-secrets get`, or any other credential route.
        from simple_tools import cursor_chat_transport as CT

        env = dict(self.env)
        del env[CCL.CURSOR_TOKEN_ENV]
        env["CURSOR_SYNC_API_KEY"] = "fixture-environment-key"
        env["CURSOR_API_KEY"] = "fixture-environment-key"
        out = io.StringIO()
        refuse = AssertionError("another key source was tried")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", self.cloud), \
                mock.patch.object(CT._surfaces, "load_api_key", side_effect=refuse), \
                mock.patch.object(CT._surfaces, "_fleet_secrets_key", side_effect=refuse), \
                mock.patch("subprocess.run", side_effect=refuse), \
                mock.patch.object(CCL, "send_dm", side_effect=AssertionError("a DM was sent")), \
                mock.patch("sys.stdout", out):
            code = CCL.main(["sweep", "--api-only", "--no-dm"])
        self.assertEqual(code, 1)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {"error": "KeyUnavailable", "status": None,
                                                "code": "service-token-required"})
        self.cloud.assert_not_called()  # no discovery, no steer
        self.assertNotIn("fixture-environment-key", out.getvalue())
        for sid in (A1, A2):
            self.assertIsNone(self.fake.lookup("cursor", "cloud", PL.SCOPE, sid))

    def test_the_ssh_route_is_gone(self) -> None:
        # One assigner, reached through its API:  there is no --remote and no SESSION_LETTER_REMOTE to name another hop.
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            CCL.main(["sweep", "--api-only", "--remote", "mac"])


class CursorKeyTests(unittest.TestCase):
    """The Cursor key is read with the sweep's own token through fleet_secrets' scoped reader, and a failure names a
    reason token only."""

    class Core:
        class SecretsError(Exception):
            pass

        def __init__(self, answer=None, error: str = "") -> None:
            self.answer, self.error, self.calls = answer, error, []

        def home_dir(self, env):
            return env.get("HOME", "")

        def infisical_project_id(self, env, home):
            return "00000000-0000-4000-8000-000000000000"

        def fetch_project(self, project, path, env, **kw):
            self.calls.append((project, path, dict(env), kw))
            if self.error:
                raise self.SecretsError(self.error)
            return self.answer

    TOKEN = "st." + "fixture-cursor-letters-token-0123"

    def test_it_reads_the_one_name_from_the_one_folder(self) -> None:
        core = self.Core({"CURSOR_SYNC_API_KEY": "fixture-key"})
        key = CCL.read_cursor_key({CCL.CURSOR_TOKEN_ENV: self.TOKEN, "HOME": "/nowhere"}, core=core)
        self.assertEqual(key, "fixture-key")
        project, path, env, kw = core.calls[0]
        self.assertEqual(path, "/cursor-cloud-letters")
        self.assertEqual(env, {})
        self.assertEqual(kw["names"], ["CURSOR_SYNC_API_KEY"])
        self.assertEqual(kw["token"], self.TOKEN)

    def test_no_token_is_refused_before_any_read(self) -> None:
        core = self.Core({"CURSOR_SYNC_API_KEY": "fixture-key"})
        with self.assertRaises(CCL.KeyUnavailable) as caught:
            CCL.read_cursor_key({"CURSOR_SYNC_API_KEY": "from-the-environment"}, core=core)
        self.assertEqual(caught.exception.code, "service-token-required")
        self.assertEqual(core.calls, [])

    def test_a_refusal_is_a_reason_token_only(self) -> None:
        core = self.Core(error="token-scope-mismatch: " + self.TOKEN)
        with self.assertRaises(CCL.KeyUnavailable) as caught:
            CCL.read_cursor_key({CCL.CURSOR_TOKEN_ENV: self.TOKEN}, core=core)
        self.assertEqual(caught.exception.code, "token-scope-mismatch")
        self.assertNotIn(self.TOKEN, str(caught.exception))
        with self.assertRaises(CCL.KeyUnavailable) as caught:
            CCL.read_cursor_key({CCL.CURSOR_TOKEN_ENV: self.TOKEN}, core=self.Core({}))
        self.assertEqual(caught.exception.code, "unset")

    def test_a_missing_project_id_has_its_own_reason(self) -> None:
        core = self.Core({"CURSOR_SYNC_API_KEY": "fixture-key"})

        def no_project(env, home):
            raise core.SecretsError("the Fleet-OPS Infisical project id is not set:  set INFISICAL_FLEET_OPS_PROJECT_ID")

        core.infisical_project_id = no_project
        with self.assertRaises(CCL.KeyUnavailable) as caught:
            CCL.read_cursor_key({CCL.CURSOR_TOKEN_ENV: self.TOKEN}, core=core)
        self.assertEqual(caught.exception.code, "project-id-missing")
        self.assertEqual(core.calls, [])  # nothing is fetched without a project

    def test_a_message_in_plain_words_is_refused_not_its_first_word(self) -> None:
        for message, code in (("key CURSOR_SYNC_API_KEY is not in Infisical at /cursor-cloud-letters", "refused"),
                              ("refusing Infisical environment 'dev' from INFISICAL_ENV", "refused"),
                              ("fetch-failed:  bad-output for CURSOR_SYNC_API_KEY", "fetch-failed"),
                              ("token-expired-or-invalid", "token-expired-or-invalid")):
            with self.assertRaises(CCL.KeyUnavailable) as caught:
                CCL.read_cursor_key({CCL.CURSOR_TOKEN_ENV: self.TOKEN}, core=self.Core(error=message))
            self.assertEqual(caught.exception.code, code, message)


class CheckTests(unittest.TestCase):
    """`check`, the installer's preflight:  names and reason tokens only, and the knob token does not stay behind in
    the environment."""

    def test_it_reports_by_name_and_leaves_no_token_behind(self) -> None:
        from simple_tools import registry_api

        knob_token = "st." + "fixture-knobs-token-0123456789"
        cursor_token = "st." + "fixture-cursor-token-0123456789"
        seen = {}

        def knob(name: str) -> str:
            seen[name] = os.environ.get(CCL.KNOBS_TOKEN_ENV)
            return "https://registry.fake.invalid" if name == registry_api.URL_KNOB else ""

        def key(env):
            self.assertEqual(env[CCL.CURSOR_TOKEN_ENV], cursor_token)
            raise CCL.KeyUnavailable("token-scope-mismatch")

        stdin = io.StringIO("%s=%s\n%s=%s\n" % (CCL.KNOBS_TOKEN_ENV, knob_token, CCL.CURSOR_TOKEN_ENV, cursor_token))
        with mock.patch.dict(os.environ, {registry_api.URL_ENV: "https://from-env.invalid"}, clear=True), \
                mock.patch.object(registry_api, "_knob_value", side_effect=knob), \
                mock.patch.object(CCL, "read_cursor_key", side_effect=key):
            report = CCL.check(stdin)
            left = dict(os.environ)
        self.assertEqual(report["registry_url"], "present")
        self.assertEqual(report["cursor_key"], "token-scope-mismatch")
        self.assertNotEqual(report["registry_key"], "present")
        self.assertEqual(seen[registry_api.URL_KNOB], knob_token)  # the URL came from the knob, not the environment
        self.assertNotIn(CCL.KNOBS_TOKEN_ENV, left)
        self.assertNotIn(registry_api.URL_ENV, left)
        self.assertNotIn(knob_token, json.dumps(report))
        self.assertNotIn(cursor_token, json.dumps(report))


class RecentTests(unittest.TestCase):
    def test_window(self) -> None:
        now = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertTrue(CCL.recent({"updated_at": "2026-11-08T00:00:01.000Z"}, now))
        self.assertTrue(CCL.recent({"updated_at": "2026-11-07T00:00:00+00:00"}, now))
        self.assertFalse(CCL.recent({"updated_at": "2026-11-06T23:59:59Z"}, now))
        self.assertTrue(CCL.recent({"updated_at": now - 3600}, now))  # epoch seconds
        self.assertTrue(CCL.recent({"updated_at": int((now - 3600) * 1000)}, now))  # epoch milliseconds
        self.assertFalse(CCL.recent({"updated_at": now - 40 * 3600}, now))
        self.assertFalse(CCL.recent({"updated_at": "yesterday"}, now))
        self.assertFalse(CCL.recent({"updated_at": True}, now))
        self.assertFalse(CCL.recent({}, now))


class TransportTests(unittest.TestCase):
    """The sweep's own Cursor transport:  built with the 30 second timeout, and a partial discovery is not complete."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {"HOME": tmp.name, "SIMPLE_TOOLS_STATE_DIR": os.path.join(tmp.name, "state"), "AGENT_SEAT": "CLAUDE"}

    def built(self, status: str) -> tuple[mock.MagicMock, dict]:
        fake = mock.MagicMock()
        fake.return_value.discover.return_value = []
        fake.return_value.last_discovery_status = status
        with mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", fake):
            out = CCL.sweep(self.env, runner=Fake(), transport=lambda argv: {})
        return fake, out

    def test_it_builds_the_transport_with_the_long_timeout(self) -> None:
        fake, out = self.built("complete")
        fake.assert_called_once_with(timeout_seconds=CCL.API_TIMEOUT)
        self.assertEqual(CCL.API_TIMEOUT, 30)
        self.assertTrue(out["complete"])

    def test_a_partial_discovery_is_reported_as_not_complete(self) -> None:
        _, out = self.built("partial")
        self.assertFalse(out["complete"])


class TitleTests(unittest.TestCase):
    def test_the_letter_stays_in_front_and_an_old_letter_comes_off(self) -> None:
        self.assertEqual(CCL.wanted_title("CD", "AB: GB-Director: tidy"), "CD: GB-Director: tidy")
        self.assertEqual(CCL.wanted_title("AA", ""), "AA: New Chat")
        long = CCL.wanted_title("AA", "x" * 300)
        self.assertLessEqual(len(long), CCL.TITLE_LIMIT)
        self.assertTrue(long.startswith("AA: "))

    def test_an_idle_steer_stops_and_a_busy_one_continues(self) -> None:
        idle = CCL.rename_prompt("AA: tidy", busy=False)
        busy = CCL.rename_prompt('AA: say "hi"', busy=True)
        self.assertIn("Then stop.", idle)
        self.assertIn("continue the work already in progress", busy)
        self.assertIn("\"AA: say 'hi'\"", busy)
        self.assertNotIn('say "hi"', busy)
        self.assertIn("\u00a0 ", idle)

    def test_bot_of(self) -> None:
        self.assertEqual(CCL.bot_of("GB-Director: tidy the board"), ("GB-Director", "tidy the board"))
        self.assertEqual(CCL.bot_of("AB: GB-Fixer: hotfix"), ("GB-Fixer", "hotfix"))
        self.assertEqual(CCL.bot_of("BF-Plumber: tunnels"), ("", ""))
        self.assertEqual(CCL.bot_of("GB-Director"), ("", ""))
        self.assertEqual(CCL.bot_of("fix GB-Director: x"), ("", ""))


if __name__ == "__main__":
    unittest.main()
