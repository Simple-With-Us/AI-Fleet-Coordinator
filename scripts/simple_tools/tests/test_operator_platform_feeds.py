"""Shared Operator external-feed behavior:  every open Cloud agent takes a letter at location `cloud` and is steered once to
put that letter in its title.  A chat that already carries its letter is left alone.  A steer the account refuses
falls back to one DM for a title that names a GB bot.  A run that only looks sends nothing.  The registry runs in
process against a temporary registry;  the Cursor API and `simple-chat dm` are replaced."""

from __future__ import annotations

import datetime
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from simple_tools import operator_platform_feeds as CCL
from simple_tools import operator_registry as OR
from simple_tools import listener_registry as LR
from simple_tools import platform_letter as PL
from simple_tools.tests.registry_fake import FakeRegistry

A1 = "bc-11111111-aaaa-4bbb-8ccc-000000000001"
A2 = "bc-22222222-aaaa-4bbb-8ccc-000000000002"


class RegistryCallAdapterTests(unittest.TestCase):
    def test_in_process_adapter_does_not_receive_network_timeout(self) -> None:
        env = {"mode": "test"}
        params = {"session_id": A1}
        calls: list[tuple[dict, str, dict]] = []

        def local_call(call_env: dict, op: str, call_params: dict) -> dict:
            calls.append((call_env, op, call_params))
            return {"ok": True}

        result = PL._call_api(env, "touch", params, timeout=7, api_call=local_call)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [(env, "touch", params)])

    def test_remote_registry_call_keeps_its_transport_timeout(self) -> None:
        env = {"mode": "test"}
        params = {"session_id": A1}
        with mock.patch("simple_tools.registry_api.call", return_value={"ok": True}) as remote_call:
            result = PL._call_api(env, "touch", params, timeout=7)

        self.assertEqual(result, {"ok": True})
        remote_call.assert_called_once_with(env, "touch", params, timeout=7)


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

    def test_a_key_that_one_mark_had_split_is_not_rejoined_into_the_dm(self) -> None:
        # Taking the markup out of a title can join the halves of a key that one mark had split.  The values are
        # built here, so no key-shaped literal is in the file.
        whole = ("sk-" + "ant-" + "a1B2c3", "D4e5F6g7H8i9J0k1L2"), ("AK" + "IA", "ABCDEFGHIJKLMNOP")
        self.steer = Steer("unavailable")
        for n, (head, tail) in enumerate(whole):
            self.assertTrue(PL.secretscan.scan(head + tail), n)  # the shape the scanner knows
            for mark in "`*|\\[":
                broken = head + mark + tail
                self.assertEqual(CCL.plain("rotate %s today" % broken), "rotate today", (n, mark))
                self.assertEqual(CCL.plain(broken), "", (n, mark))
        self.agents = [agent(A1, "GB-Director: rotate %s`%s today" % whole[0])]
        self.run_sweep()
        text = self.dm.calls[0][-1]
        self.assertNotIn(whole[0][0] + whole[0][1], text)
        self.assertNotIn(whole[0][1], text)
        self.assertIn('"GB-Director rotate today"', text)  # the words around it still go
        # Words that hold no secret still go, with their marks taken out as before.
        self.assertEqual(CCL.plain("fix `the` *login* page"), "fix the login page")

    def test_a_title_that_reads_as_a_secret_is_in_no_steer_and_no_rename_title(self) -> None:
        # The title goes into an instruction to the agent:  a title that reads as a secret, as a line or in one word,
        # contributes no text at all (the values are built here, so no key-shaped literal is in the file).
        key = "sk-" + "ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        split = ("AK" + "IA", "ABCDEFGHIJKLMNOP")
        for current in ("rotate %s today" % key, key, "%s%s" % split, "fix the login page %s" % split[0] + split[1]):
            wanted = CCL.wanted_title("AB", current)
            self.assertEqual(wanted, PL.compose("AB", ""), current[:8])
            self.assertFalse(any(part in wanted for part in (key, split[1], "rotate", "login")))
            prompt = CCL.rename_prompt(wanted, False)
            self.assertFalse(PL.secretscan.scan(prompt))
            self.assertFalse(any(part in prompt for part in (key, split[1])))
        # A title with no secret in it is as before.
        self.assertEqual(CCL.wanted_title("AB", "fix the login page"), "AB: fix the login page")
        # End to end:  an agent whose title carries a key is steered with the letter and no text of its title.
        self.steer = Steer("sent")
        self.agents = [agent(A1, "rotate %s today" % key)]
        self.run_sweep()
        self.assertTrue(self.steer.calls)
        self.assertFalse(any(key in text or split[1] in text for _, text in self.steer.calls))
        self.assertFalse(any(PL.secretscan.scan(text) for _, text in self.steer.calls))

    def test_a_bot_name_shaped_like_a_key_is_no_bots_name(self) -> None:
        name = "GB-" + "aB3dE6gH" + "9jK2mN5p" + "Q8sT1vW4" + "yZ7bC0dF"  # 32 mixed-case letters and digits
        self.assertTrue(PL.secretscan.scan(name))
        self.assertEqual(CCL.bot_of(name + ": fix the login page"), ("", ""))
        self.assertEqual(CCL.bot_of("GB-Director: fix the login page"), ("GB-Director", "fix the login page"))
        self.steer = Steer("unavailable")
        self.agents = [agent(A1, name + ": fix the login page")]
        out = self.run_sweep()
        row = out["rows"][0]
        self.assertNotIn("bot", row)  # not in the printed row
        self.assertNotIn("dm", row)
        self.assertEqual(self.dm.calls, [])  # no DM, so no `--to` and no text carries it
        self.assertNotIn(name, json.dumps(out))
        self.assertNotIn(name, json.dumps(CCL.read_state(self.env)))

    def test_a_state_that_cannot_be_saved_ends_the_run_before_anything_more_is_sent(self) -> None:
        self.steer = Steer("unavailable")
        self.agents = [agent(A1, "GB-Director: tidy the board"), agent(A2, "GB-Builder: fix the login page")]
        real, writes = CCL.write_state, []

        def failing_once(env, state):
            writes.append(json.loads(json.dumps(state)))
            if len(writes) == 1:
                raise OSError("no space left on device")
            return real(env, state)

        with mock.patch.object(CCL, "write_state", failing_once), self.assertRaises(CCL.StateUnsaved) as stopped:
            self.run_sweep()
        self.assertEqual(str(stopped.exception), "OSError")  # the type only:  a message can hold a path
        self.assertEqual(writes[0][A1]["dm"], "unknown")  # the write that failed was the one before the send
        self.assertEqual(self.dm.calls, [])  # nothing was sent
        self.assertEqual([sid for sid, _ in self.steer.calls], [A1])  # and the next agent was never reached
        self.assertEqual(len(writes), 1)  # nor was the unsaved row written by a later save
        self.assertNotIn("dm", CCL.read_state(self.env).get(A1, {}))
        # The next run, with a disk that works, sends the DM:  nothing recorded it as sent.
        out = self.run_sweep()
        self.assertEqual([row.get("dm") for row in out["rows"]], ["sent", "sent"])
        self.assertEqual([call[3] for call in self.dm.calls], ["GB-Director", "GB-Builder"])
        self.assertEqual(CCL.read_state(self.env)[A1]["dm"], "sent")

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

    def test_shared_sweeper_registers_and_reopens_through_the_registry_service(self) -> None:
        self.env[CCL.CURSOR_KEY_NAME] = "fixture-api-key"
        cloud = mock.MagicMock()
        cloud.return_value.discover.return_value = list(self.agents)
        cloud.return_value.steer.return_value = "not-started"
        cloud.return_value.last_discovery_status = "complete"
        with mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", cloud):
            first = CCL.run_platform_feeds(self.assigner.service, self.env, now=time.time())
            original = self.assigner.lookup("cursor", "cloud", PL.SCOPE, A1)
            self.assertIsNotNone(original)
            closed = self.assigner.service.call_operator(
                "close", {"platform": "cursor", "location": "cloud", "scope": PL.SCOPE,
                          "session_id": A1, "source": "test"}
            )
            self.assertNotIn("error", closed)
            self.agents[0] = agent(A1, "GB-Director: updated title")
            cloud.return_value.discover.return_value[0] = self.agents[0]
            second = CCL.run_platform_feeds(self.assigner.service, self.env, now=time.time() + 1)

        reopened = self.assigner.lookup("cursor", "cloud", PL.SCOPE, A1)
        self.assertTrue(first["cursor-cloud"]["ok"])
        self.assertTrue(second["cursor-cloud"]["ok"])
        self.assertEqual(reopened["generation"], original["generation"])
        self.assertEqual(reopened["label"], original["label"])
        self.assertEqual(reopened["title"], "GB-Director: updated title")
        self.assertEqual(reopened["state"], "active")
        self.assertEqual(reopened["seat"], "CURSOR")
        self.assertEqual(cloud.call_count, 2)

    def test_partial_discovery_protects_cursor_cleanup_but_keeps_other_sweeps(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=40)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        for platform, host, seat, sid in (
            ("cursor", "cloud", "CURSOR", "cursor-old"),
            ("testbed", "mac", "CLAUDE", "testbed-old"),
        ):
            OR.register_session(
                self.db, platform=platform, verified_host=host, profile_scope="default",
                native_session_id=sid, seat=seat, source="test", issuer=OR.ISSUER_OPERATOR,
            )
            conn = sqlite3.connect(self.db)
            try:
                with conn:
                    conn.execute(
                        "UPDATE sessions SET registered_at = ?, last_seen_at = ? WHERE native_session_id = ?",
                        (old, old, sid),
                    )
            finally:
                conn.close()
            OR.register_session(
                self.db, platform=platform, verified_host=host, profile_scope="default",
                native_session_id=platform + "-new", seat=seat, source="test", issuer=OR.ISSUER_OPERATOR,
            )

        self.env[CCL.CURSOR_KEY_NAME] = "fixture-api-key"
        cloud = mock.MagicMock()
        cloud.return_value.discover.return_value = [self.agents[0]]
        cloud.return_value.steer.return_value = "not-started"
        cloud.return_value.last_discovery_status = "partial"
        with mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", cloud):
            sweeper = LR.Sweeper(
                self.assigner.service, interval_minutes=15, stale_after_hours=36,
                feed_runner=lambda service, now: CCL.run_platform_feeds(service, self.env, now=now),
                feed_scopes=CCL.feed_scopes(),
            )
            result = sweeper.run(time.time())

        self.assertEqual(result["cleanup_protected"], [{"platform": "cursor", "host": "cloud"}])
        self.assertEqual(self.assigner.lookup("cursor", "cloud", PL.SCOPE, "cursor-old")["state"], "active")
        self.assertEqual(self.assigner.lookup("testbed", "mac", PL.SCOPE, "testbed-old")["state"], "closed")
        sweeper.sweep("cursor", "CURSOR")
        self.assertEqual(self.assigner.lookup("cursor", "cloud", PL.SCOPE, "cursor-old")["state"], "active")

    def test_missing_registry_store_skips_provider_before_any_cursor_call(self) -> None:
        os.unlink(self.db)
        self.env[CCL.CURSOR_KEY_NAME] = "fixture-api-key"
        cloud = mock.MagicMock()
        with mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", cloud):
            sweeper = LR.Sweeper(
                self.assigner.service, interval_minutes=15, stale_after_hours=36,
                feed_runner=lambda service, now: CCL.run_platform_feeds(service, self.env, now=now),
                feed_scopes=CCL.feed_scopes(),
            )
            result = sweeper.run(time.time())
        self.assertFalse(result["ok"])
        self.assertEqual(result["feeds"]["error"], "registry_unavailable")
        cloud.assert_not_called()
        self.assertFalse(os.path.exists(self.db))

    def test_missing_feed_report_keeps_all_known_scopes_protected(self) -> None:
        sweeper = LR.Sweeper(
            self.assigner.service,
            interval_minutes=15,
            stale_after_hours=36,
            feed_runner=lambda _service, _now: {},
            feed_scopes=CCL.feed_scopes(),
        )
        result = sweeper.run(time.time())
        self.assertEqual(result["cleanup_protected"], [{"platform": "cursor", "host": "cloud"}])

    def test_malformed_feed_report_keeps_all_known_scopes_protected(self) -> None:
        sweeper = LR.Sweeper(
            self.assigner.service,
            interval_minutes=15,
            stale_after_hours=36,
            feed_runner=lambda _service, _now: None,
            feed_scopes=CCL.feed_scopes(),
        )
        result = sweeper.run(time.time())
        self.assertEqual(result["feeds"]["error"], "invalid_feed_report")
        self.assertEqual(result["cleanup_protected"], [{"platform": "cursor", "host": "cloud"}])

    def test_discovery_does_not_hold_the_cleanup_lock_and_protects_other_sweeps(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=40)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        groups = (
            ("cursor", "cloud", "CURSOR", "cloud-old"),
            ("cursor", "mac", "CURSOR", "mac-old"),
            ("testbed", "mac", "CLAUDE", "other-old"),
        )
        for platform, host, seat, sid in groups:
            OR.register_session(
                self.db, platform=platform, verified_host=host, profile_scope="default",
                native_session_id=sid, seat=seat, source="test", issuer=OR.ISSUER_OPERATOR,
            )
            OR.register_session(
                self.db, platform=platform, verified_host=host, profile_scope="default",
                native_session_id=sid + "-fresh", seat=seat, source="test", issuer=OR.ISSUER_OPERATOR,
            )
            conn = sqlite3.connect(self.db)
            try:
                with conn:
                    conn.execute(
                        "UPDATE sessions SET registered_at = ?, last_seen_at = ? WHERE native_session_id = ?",
                        (old, old, sid),
                    )
            finally:
                conn.close()

        started, release, direct_sweeps_done = threading.Event(), threading.Event(), threading.Event()

        def blocked_feed(_service, _now):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test feed release timed out")
            return {
                "cursor-cloud": {
                    "ok": True,
                    "complete": False,
                    "platform": "cursor",
                    "host": "cloud",
                    "protect_stale_cleanup": True,
                }
            }

        sweeper = LR.Sweeper(
            self.assigner.service,
            interval_minutes=15,
            stale_after_hours=36,
            feed_runner=blocked_feed,
            feed_scopes=CCL.feed_scopes(),
        )
        run_result = {}
        run_thread = threading.Thread(target=lambda: run_result.update(sweeper.run(time.time())))
        run_thread.start()
        self.assertTrue(started.wait(2))

        def direct_sweeps() -> None:
            sweeper.sweep("testbed", "CLAUDE")
            sweeper.sweep("cursor", "CURSOR")
            direct_sweeps_done.set()

        direct_thread = threading.Thread(target=direct_sweeps)
        direct_thread.start()
        completed_before_release = direct_sweeps_done.wait(1)
        release.set()
        direct_thread.join(5)
        run_thread.join(5)
        self.assertTrue(completed_before_release, "platform sweeps must not wait for provider network I/O")
        self.assertFalse(direct_thread.is_alive())
        self.assertFalse(run_thread.is_alive())
        self.assertEqual(self.assigner.lookup("cursor", "cloud", PL.SCOPE, "cloud-old")["state"], "active")
        self.assertEqual(self.assigner.lookup("cursor", "mac", PL.SCOPE, "mac-old")["state"], "closed")
        self.assertEqual(self.assigner.lookup("testbed", "mac", PL.SCOPE, "other-old")["state"], "closed")

    def test_numeric_protection_flag_preserves_provider_host(self) -> None:
        sweeper = LR.Sweeper(
            self.assigner.service,
            interval_minutes=15,
            stale_after_hours=36,
            feed_runner=lambda _service, _now: {
                "cursor-cloud": {
                    "ok": False,
                    "platform": "cursor",
                    "host": "cloud",
                    "protect_stale_cleanup": 1,
                }
            },
            feed_scopes=CCL.feed_scopes(),
        )
        result = sweeper.run(time.time())
        self.assertEqual(result["cleanup_protected"], [{"platform": "cursor", "host": "cloud"}])


class StateSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"SIMPLE_TOOLS_STATE_DIR": self.tmp.name}

    def test_feed_write_preserves_other_feed_rows(self) -> None:
        path = CCL.state_path(self.env)
        os.makedirs(os.path.dirname(path), mode=0o700)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"feeds": {"another-feed": {"row": 1}, "cursor-cloud": {"old": 1}}}, fh)
        CCL.write_state(self.env, {"new": 2})
        saved = CCL.L.read_json(path)
        self.assertEqual(saved["feeds"]["another-feed"], {"row": 1})
        self.assertEqual(saved["feeds"]["cursor-cloud"], {"new": 2})

    def test_feed_write_refuses_symlinked_directory_and_state_file(self) -> None:
        target = os.path.join(self.tmp.name, "external")
        os.mkdir(target)
        os.symlink(target, os.path.join(self.tmp.name, "operator"))
        with self.assertRaises(ValueError):
            CCL.write_state(self.env, {"row": 1})
        self.assertFalse(os.path.exists(os.path.join(target, CCL.STATE_FILE)))
        os.unlink(os.path.join(self.tmp.name, "operator"))
        os.mkdir(os.path.join(self.tmp.name, "operator"))
        external = os.path.join(target, "state.json")
        with open(external, "w", encoding="utf-8") as fh:
            fh.write("sentinel")
        os.symlink(external, CCL.state_path(self.env))
        with self.assertRaises(ValueError):
            CCL.write_state(self.env, {"row": 1})
        with open(external, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "sentinel")

    def test_symlinked_or_malformed_existing_ledger_stops_before_register_or_steer(self) -> None:
        path = CCL.state_path(self.env)
        os.makedirs(os.path.dirname(path), mode=0o700)

        def refused_before_actions() -> None:
            registered: list[list[str]] = []
            steered: list[str] = []
            with self.assertRaises(ValueError):
                CCL.sweep(
                    self.env,
                    discover=lambda: [agent(A1, "GB-Director: tidy")],
                    transport=lambda argv: registered.append(list(argv)) or {},
                    steer=lambda sid, _text: steered.append(sid) or "sent",
                )
            self.assertEqual(registered, [])
            self.assertEqual(steered, [])

        outside_dir = os.path.join(self.tmp.name, "outside-dir")
        os.mkdir(outside_dir)
        directory_state = os.path.join(outside_dir, CCL.STATE_FILE)
        with open(directory_state, "w", encoding="utf-8") as fh:
            fh.write('{"feeds":{"cursor-cloud":{"already":"told"}}}')
        os.rmdir(os.path.dirname(path))
        os.symlink(outside_dir, os.path.dirname(path))
        refused_before_actions()
        with open(directory_state, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"feeds":{"cursor-cloud":{"already":"told"}}}')
        os.unlink(os.path.dirname(path))
        os.mkdir(os.path.dirname(path))

        target = os.path.join(self.tmp.name, "outside.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write('{"feeds":{"cursor-cloud":{"already":"told"}}}')
        os.symlink(target, path)
        refused_before_actions()
        os.unlink(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"feeds":')
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        refused_before_actions()
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"feeds":{"cursor-cloud":{"already":"told"}}}')


class NativeCursorKeyTests(unittest.TestCase):
    def test_api_only_transport_uses_the_shared_loaded_key(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            cloud = mock.MagicMock()
            cloud.return_value.discover.return_value = []
            cloud.return_value.last_discovery_status = "complete"
            with mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", cloud):
                CCL.sweep(
                    {CCL.CURSOR_KEY_NAME: "fixture-api-key", "SIMPLE_TOOLS_STATE_DIR": state},
                    now=time.time(),
                    api_only=True,
                    send_dms=False,
                )
            surfaces = cloud.call_args.kwargs["surfaces"]
            self.assertEqual(surfaces.load_api_key(), "fixture-api-key")

    def test_api_only_transport_cannot_fall_back_when_shared_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            cloud = mock.MagicMock()
            cloud.return_value.discover.return_value = []
            cloud.return_value.last_discovery_status = "complete"
            with mock.patch("simple_tools.cursor_chat_transport.CursorCloudTransport", cloud):
                CCL.sweep(
                    {"SIMPLE_TOOLS_STATE_DIR": state},
                    now=time.time(),
                    api_only=True,
                    send_dms=False,
                )
            surfaces = cloud.call_args.kwargs["surfaces"]
            self.assertEqual(surfaces.load_api_key(), "")


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
