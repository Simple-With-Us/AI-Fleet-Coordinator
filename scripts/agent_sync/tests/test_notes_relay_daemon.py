"""[notes_relay] in listener.toml and the consumer inside the listener daemon (AFC-331).

The daemon runs against the fake Zulip with a fake script runner:  no osascript, no Notes.app.  The
interesting properties are the wiring:  an opt-in section, only the configured seat's bot reads the
channel, a relay request never reaches the seat inbox or wakes anyone, the slow script runs on a worker
thread, and a missing channel is a logged warning, never a created channel.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import config as C
from agent_sync import notes as N
from agent_sync.tests.harness import write_rc
from agent_sync.tests.test_daemon import DaemonHarness

CODEX_ID = 11
RELAY_SECTION = "[notes_relay]\nenabled = true\nseat = \"CLAUDE\"\n"


class ConfigTests(unittest.TestCase):
    def load(self, text: str) -> C.Config:
        import tomllib
        return C.from_dict(tomllib.loads(text))

    SEAT = '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\n'

    def test_off_by_default_and_seat_defaults_to_claude(self):
        cfg = self.load(self.SEAT)
        self.assertEqual((cfg.notes_relay_enabled, cfg.notes_relay_seat), (False, "CLAUDE"))
        self.assertEqual([e for e in cfg.errors if "notes_relay" in e], [])

    def test_enabled_with_a_held_seat(self):
        cfg = self.load(self.SEAT + RELAY_SECTION)
        self.assertEqual((cfg.notes_relay_enabled, cfg.notes_relay_seat), (True, "CLAUDE"))
        self.assertEqual([e for e in cfg.errors if "notes_relay" in e], [])

    def test_a_bad_section_is_an_error_and_never_enables(self):
        for text, fragment in (
            (self.SEAT + "[notes_relay]\nenabled = \"yes\"\n", "enabled must be true or false"),
            (self.SEAT + "[notes_relay]\nenabled = true\nseat = 5\n", "seat must be a seat name"),
            (self.SEAT + "[notes_relay]\nenabled = true\nchannel = \"x\"\n", "unknown setting"),
            (self.SEAT + "[notes_relay]\nenabled = true\nseat = \"CODEX\"\n", "is not an enabled [seat.X] section"),
            ("[daemon]\ninstance = \"server\"\n" + self.SEAT.replace("[seat.CLAUDE]", "[seat.CLAUDE]\ncreds = \"file\"")
             + RELAY_SECTION, "mac instance only"),
        ):
            with self.subTest(fragment):
                cfg = self.load(text)
                self.assertTrue(any(fragment in e for e in cfg.errors), cfg.errors)
                self.assertFalse(cfg.notes_relay_enabled)


class DaemonRelayTests(DaemonHarness):
    def setUp(self):
        super().setUp()
        self.calls: list[dict] = []
        self.ran = threading.Event()
        patcher = mock.patch("agent_sync.notes.direct_available", return_value=Path("/fake/apple-notes-coding.sh"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def enable(self, **kw):
        self.write_config(extra_daemon=kw.pop("extra_daemon", ""))
        path = self.state_dir / "listener.toml"
        path.write_text(path.read_text() + "\n" + RELAY_SECTION)

    def fake_run(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "input": kwargs.get("input")})
        self.ran.set()
        return subprocess.CompletedProcess(argv, 0, b"created note id=n1\n", b"")

    def envelope_message(self, body="relay body", title="[AFC, Codex] relay", rid="daemon-request-1", mention=False):
        env = N.make_envelope("create", title, body + (" @**Claude** please look" if mention else ""), {}, rid, "CODEX")
        return self.fake.add_message("Codex", "notes-relay", title, N.render_message(env))

    def wait(self, predicate, seconds=10.0):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_a_relay_request_runs_off_the_seat_thread_is_answered_and_never_reaches_the_inbox(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNotNone(daemon.notes_relay)
        self.assertIn("notes-relay", self.fake.subscribed, "the reading bot should have joined the existing channel")
        daemon.notes_relay.runner = self.fake_run
        mid = self.envelope_message(mention=True)
        self.pump_until(daemon, self.ran.is_set)
        self.assertTrue(self.wait(lambda: (mid, 10, "check") in self.fake.reactions))
        self.assertEqual(self.calls[0]["input"], b"relay body @**Claude** please look")
        self.assertEqual(self.calls[0]["argv"][-2:], ["--title", "[AFC, Codex] relay"])
        daemon.pump("CLAUDE", timeout=0.3)
        self.assertEqual(self.inbox(), [], "a relay request must not become seat inbox work")
        self.assertEqual(self.ledger(), [], "a relay request must not wake anyone")
        self.assertEqual(daemon.seats["CLAUDE"].cursor, mid, "the cursor still advances past the consumed message")
        daemon.notes_relay.stop()

    def test_a_refused_request_gets_a_cross_mark_and_a_reply_and_runs_nothing(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        daemon.notes_relay.runner = self.fake_run
        mid = self.envelope_message(title="[AFC, Cursor] wrong agent")
        self.pump_until(daemon, lambda: (mid, 10, "cross_mark") in self.fake.reactions or (mid, 10, "x") in self.fake.reactions)
        self.assertTrue(self.wait(lambda: any(m["sender_id"] == 10 and "notes-relay refused" in m["content"]
                                              for m in self.fake.messages)))
        self.assertEqual(self.calls, [])
        daemon.notes_relay.stop()

    def test_the_channel_missing_is_a_warning_and_nothing_is_created(self):
        self.enable()
        daemon = self.started()
        self.assertNotIn("notes-relay", self.fake.streams)
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("notes-relay-no-channel", log)
        self.assertIn("create it", log)
        daemon.notes_relay.stop()

    def test_off_by_default_the_channel_routes_like_any_other(self):
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNone(daemon.notes_relay)
        mid = self.fake.add_message("Codex", "notes-relay", "t", "@**Claude** can you check this?")
        self.pump_until(daemon, lambda: any(r["id"] == mid for r in self.inbox()))

    def two_seats(self):
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(self.secrets_dir / "Codex-zuliprc", email="codex-bot@zulip.test", key=codex_key, site=self.fake.url)
        self.write_config(seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n\n[seat.CODEX]\nbot = "Codex"\nwake = "inbox"\n')
        path = self.state_dir / "listener.toml"
        path.write_text(path.read_text() + "\n" + RELAY_SECTION)
        self.fake.streams["notes-relay"] = 140
        daemon = self.started(env=self.env(AGENT_SYNC_PARTITION=self.two_seat_partition()))
        self.assertTrue(daemon.connect_seat("CODEX"))
        return daemon

    def test_every_seats_runner_treats_a_relay_message_as_consumed_and_only_the_reader_acts(self):
        daemon = self.two_seats()
        spooled = []
        daemon.notes_relay.offer = lambda runner, message: spooled.append((runner.seat, message["id"])) or True
        mid = self.fake.add_message("Cursor", "notes-relay", "[AFC, Cursor] t", "@**Claude** and @**Codex** look at this envelope")
        message = dict(self.fake.messages[-1], flags=["mentioned"])
        for seat in ("CLAUDE", "CODEX"):
            runner = daemon.seats[seat]
            self.assertNotEqual(runner.cursor, mid)
            daemon.route_message(runner, message)
            self.assertEqual(runner.cursor, mid, "%s advances past the consumed message" % seat)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox("CODEX"), [], "a mention inside a relay envelope must not fill another seat's inbox")
        self.assertEqual(self.ledger("CODEX"), [], "or wake it")
        self.assertEqual(self.ledger(), [])
        self.assertEqual(spooled, [("CLAUDE", mid)], "only the configured seat hands it to the consumer")

    def test_the_consumer_seat_offline_loses_nothing_and_the_non_consumer_seeing_it_first_changes_nothing(self):
        """Each runner has its own event queue and its own cursor.  A non-consumer runner advancing never moves the
        consumer seat's cursor, and the consumer seat reads (or backfills from its own cursor) every message."""
        daemon = self.two_seats()
        spooled = []
        daemon.notes_relay.offer = lambda runner, message: spooled.append((runner.seat, message["id"])) or True
        reader, other = daemon.seats["CLAUDE"], daemon.seats["CODEX"]
        mid = self.fake.add_message("Cursor", "notes-relay", "[AFC, Cursor] t", "an envelope")
        message = dict(self.fake.messages[-1])
        before = reader.cursor
        daemon.route_message(other, message)  # the non-consumer reads it FIRST
        self.assertEqual(other.cursor, mid)
        self.assertEqual(reader.cursor, before, "the consumer seat's cursor is independent of every other runner's")
        self.assertEqual(spooled, [], "a non-consumer hands nothing over")
        daemon.route_message(reader, message)  # the consumer seat reads it second
        self.assertEqual(reader.cursor, mid)
        self.assertEqual(spooled, [("CLAUDE", mid)])
        # Consumer offline for a while:  the message is still in Zulip, so its backfill from its own cursor finds it.
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox("CODEX"), [])

    def test_a_request_that_was_not_kept_is_retried_by_the_next_backfill_not_skipped_as_a_duplicate(self):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        results = iter([False, True])
        offered = []
        daemon.notes_relay.offer = lambda r, m: offered.append(m["id"]) or next(results)
        before = runner.cursor
        mid = self.envelope_message()
        message = dict(self.fake.messages[-1])
        daemon.route_message(runner, message)  # the spool could not take it
        self.assertEqual(runner.cursor, before)
        self.assertNotIn(mid, runner.ring, "a failed offer must leave the id out of the dedupe ring")
        daemon.route_message(runner, message)  # the backfill delivers it again
        self.assertEqual(offered, [mid, mid], "the second delivery reached the consumer, not the duplicate path")
        self.assertEqual(runner.cursor, mid)

    def started_with_relay(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        return self.started()

    def test_the_cursor_is_held_for_a_request_that_could_not_be_kept(self):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        daemon.notes_relay.offer = lambda r, m: False
        before = runner.cursor
        mid = self.envelope_message()
        daemon.route_message(runner, self.fake.messages[-1])
        self.assertEqual(runner.cursor, before, "a request that was not accepted must not move the cursor")
        self.assertNotEqual(before, mid)
        self.assertIn("notes-relay-held", (self.state_dir / "logs" / "listener.log").read_text())

    def test_a_request_spooled_by_an_earlier_run_is_answered_when_the_reader_connects(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        mid = self.envelope_message()  # in Zulip, before this daemon ever connects
        message = dict(self.fake.messages[-1])
        spool = self.state_dir / "CLAUDE" / "notes-relay-spool"
        spool.mkdir(parents=True)
        (spool / ("%d.json" % mid)).write_text(json.dumps(message))
        with mock.patch.object(N.subprocess, "run", self.fake_run):
            daemon = self.started()
            self.assertTrue(self.ran.wait(10), "the spooled request should run on connect")
            self.assertTrue(self.wait(lambda: (mid, 10, "check") in self.fake.reactions))
            self.assertTrue(self.wait(lambda: daemon.notes_relay.spool.ids() == []))
        daemon.notes_relay.stop()

    def test_a_mac_without_the_script_logs_and_stays_off(self):
        self.enable()
        with mock.patch("agent_sync.notes.direct_available", return_value=None):
            daemon = self.started()
        self.assertIsNone(daemon.notes_relay)
        self.assertIn("notes-relay-unavailable", (self.state_dir / "logs" / "listener.log").read_text())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
