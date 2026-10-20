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
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import config as C
from agent_sync import notes as N
from agent_sync import notes_relay as NR
from agent_sync import zulip as Z
from agent_sync.tests.harness import write_rc
from agent_sync.tests.test_daemon import DaemonHarness

CODEX_ID = 11
MENTION = "@**Claude** can you check the AFC cutover?"
RELAY_SECTION = '[notes_relay]\nenabled = true\nseat = "CLAUDE"\n'

SESSION = "relay-test-session-0001"  # AFC-361:  a create or update from a non-exempt sender carries a session id


def make_envelope(action, title, markdown, flags, rid, identity, session=SESSION):
    return N.make_envelope(action, title, markdown, flags, rid, identity, session)


class AdminSeatTests(unittest.TestCase):
    """Only the inbox-only [notes_relay] seat may hold an administrator key (AFC-331, owner 2026-10-18)."""

    def test_only_the_inbox_notes_seat_may_hold_an_admin_key(self):
        from types import SimpleNamespace as NS

        from agent_sync import daemon as D

        cfg = NS(notes_relay_enabled=True, notes_relay_seat="ADMIN")
        admin, owner = {"role": 200, "is_admin": True}, {"role": 100, "is_owner": True}
        self.assertTrue(D.admin_allowed(cfg, NS(seat="ADMIN", wake="inbox")))
        self.assertFalse(
            D.role_refused(admin, D.admin_allowed(cfg, NS(seat="ADMIN", wake="inbox")))
        )
        self.assertTrue(D.role_refused(owner, True), "an owner key is never accepted")
        for seat in (NS(seat="ADMIN", wake="claude"), NS(seat="CLAUDE", wake="inbox")):
            self.assertTrue(D.role_refused(admin, D.admin_allowed(cfg, seat)), seat)
        self.assertTrue(
            D.role_refused(
                admin,
                D.admin_allowed(
                    NS(notes_relay_enabled=False, notes_relay_seat="ADMIN"),
                    NS(seat="ADMIN", wake="inbox"),
                ),
            )
        )


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
            (self.SEAT + '[notes_relay]\nenabled = "yes"\n', "enabled must be true or false"),
            (self.SEAT + "[notes_relay]\nenabled = true\nseat = 5\n", "seat must be a seat name"),
            (self.SEAT + '[notes_relay]\nenabled = true\nchannel = "x"\n', "unknown setting"),
            (
                self.SEAT + '[notes_relay]\nenabled = true\nseat = "CODEX"\n',
                "is not an enabled [seat.X] section",
            ),
            (
                '[daemon]\ninstance = "server"\n'
                + self.SEAT.replace("[seat.CLAUDE]", '[seat.CLAUDE]\ncreds = "file"')
                + RELAY_SECTION,
                "mac instance only",
            ),
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
        patcher = mock.patch(
            "agent_sync.notes.direct_available", return_value=Path("/fake/apple-notes-coding.sh")
        )
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

    def envelope_message(
        self, body="relay body", title="[AFC, Codex] relay", rid="daemon-request-1", mention=False
    ):
        env = make_envelope(
            "create",
            title,
            body + (" @**Claude** please look" if mention else ""),
            {},
            rid,
            "CODEX",
        )
        return self.fake.add_message("Codex", "notes-relay", title, N.render_message(env))

    def test_a_reload_that_ends_the_admin_exception_drops_the_admin_key(self):
        self.write_config(wake="inbox")
        path = self.state_dir / "listener.toml"
        path.write_text(path.read_text() + "\n" + RELAY_SECTION)
        self.fake.set_role(self.fake.me["user_id"], 200)
        self.addCleanup(self.fake.me.update, role=400, is_admin=False, is_owner=False)
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        self.assertTrue(runner.connected, "the inbox-only relay seat may hold an administrator key")
        self.write_config(wake="inbox")  # the [notes_relay] section is gone
        daemon.reload()
        self.assertTrue(runner.fatal)
        self.assertFalse(runner.connected)
        self.assertIn("refuses admin and owner keys", runner.error)

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
        self.assertIn(
            "notes-relay",
            self.fake.subscribed,
            "the reading bot should have joined the existing channel",
        )
        daemon.notes_relay.runner = self.fake_run
        mid = self.envelope_message(mention=True)
        self.pump_until(daemon, self.ran.is_set)
        self.assertTrue(self.wait(lambda: (mid, 10, "check") in self.fake.reactions))
        self.assertTrue(
            self.calls[0]["input"].startswith(
                b"relay body @**Claude** please look\n\n---\n\n*Relayed for CODEX "
            )
        )
        self.assertEqual(self.calls[0]["argv"][-2:], ["--title", "[AFC, Codex] relay"])
        daemon.pump("CLAUDE", timeout=0.3)
        self.assertEqual(self.inbox(), [], "a relay request must not become seat inbox work")
        self.assertEqual(self.ledger(), [], "a relay request must not wake anyone")
        self.assertEqual(
            daemon.seats["CLAUDE"].cursor,
            mid,
            "the cursor still advances past the consumed message",
        )
        daemon.notes_relay.stop()

    def test_a_refused_request_gets_a_cross_mark_and_a_reply_and_runs_nothing(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        daemon.notes_relay.runner = self.fake_run
        mid = self.envelope_message(title="[AFC, Cursor] wrong agent")
        self.pump_until(
            daemon,
            lambda: (
                (mid, 10, "cross_mark") in self.fake.reactions
                or (mid, 10, "x") in self.fake.reactions
            ),
        )
        self.assertTrue(
            self.wait(
                lambda: any(
                    m["sender_id"] == 10 and "notes-relay refused" in m["content"]
                    for m in self.fake.messages
                )
            )
        )
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

    def test_a_request_with_no_consumer_is_refused_instead_of_lost(self):
        """AFC-407:  with the relay off there is no consumer at all, and the request used to be routed like any
        chatter:  the cursor moved past it, nothing said a note was lost, and `notes_status` said "queued" for
        ever.  The consumer seat refuses it instead, so the requester hears back at once."""
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNone(daemon.notes_relay, "the relay is off, so there is no consumer")
        self.assertEqual(daemon.notes_relay_refuser, "CLAUDE")
        mid = self.envelope_message(mention=True)
        self.pump_until(
            daemon,
            lambda: (
                (mid, 10, "cross_mark") in self.fake.reactions
                or (mid, 10, "x") in self.fake.reactions
            ),
        )
        self.assertTrue(
            (mid, 10, "cross_mark") in self.fake.reactions or (mid, 10, "x") in self.fake.reactions,
            "the request carries the cross mark that notes_status reads",
        )
        self.assertTrue(
            self.wait(
                lambda: any(
                    m["sender_id"] == 10
                    and str(m.get("content") or "").startswith(
                        "notes-relay refused daemon-request-1:"
                    )
                    for m in self.fake.messages
                )
            ),
            "the requester is told that nothing was written",
        )
        reply = next(
            m
            for m in self.fake.messages
            if m["sender_id"] == 10
            and str(m.get("content") or "").startswith("notes-relay refused daemon-request-1:")
        )
        self.assertIn("no Notes relay consumer is running", reply["content"])
        self.assertEqual(self.inbox(), [], "a relay request must not become seat inbox work")
        self.assertEqual(self.ledger(), [], "a relay request must not wake anyone")
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("notes-relay-unconsumed", log)

    def test_a_host_that_is_not_the_consumer_does_not_answer_a_request(self):
        """AFC-407:  the knob names one consumer.  A host that is not it must leave the request alone:  it
        belongs to the Mac that is the consumer."""
        self.notes_knob = "MM"
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNone(daemon.notes_relay)
        self.assertIsNone(daemon.notes_relay_refuser, "another seat is the consumer")
        mid = self.envelope_message()
        self.pump_until(daemon, lambda: daemon.seats["CLAUDE"].cursor >= mid)
        self.assertFalse(
            [r for r in self.fake.reactions if r[0] == mid], "this host does not answer the request"
        )
        self.assertFalse(
            [m for m in self.fake.messages if "notes-relay refused" in str(m.get("content") or "")],
            "this host does not refuse the request either",
        )
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("notes-relay-unclaimed", log)

    def test_a_refusal_that_failed_is_held_and_published_on_the_next_tick(self):
        """AFC-407:  the reply is part of the result, so a refusal that failed on a retryable error is not a lost
        request.  The cursor is held below it (a later message must not carry it past) and the tick publishes the
        refusal, after which the hold falls and the requester has heard back."""
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        self.assertIsNone(daemon.notes_relay)
        real = NR.refuse_unconsumed
        attempts = []

        def flaky(r, message, reason, log):
            attempts.append(message.get("id"))
            if len(attempts) == 1:
                raise Z.ApiError("zulip is busy", code="RATE_LIMIT_HIT", status=429)
            return real(r, message, reason, log)

        mid = self.envelope_message()
        before = runner.cursor
        with mock.patch.object(NR, "refuse_unconsumed", flaky):
            daemon.route_message(runner, self.fake.messages[-1])
            self.assertEqual(attempts, [mid], "the refusal was tried and failed")
            self.assertEqual(runner.cursor, before, "a failed refusal must not move the cursor")
            self.assertEqual(runner.hold_below, mid, "the cursor is held below the request")
            later = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
            daemon.route_message(runner, self.fake.messages[-1])
            self.assertEqual(
                runner.cursor, mid - 1, "a later message must not carry the cursor past the request"
            )
            self.assertGreater(later, mid)
            daemon.retry_held_notes_relay()
        self.assertTrue(
            (mid, 10, "cross_mark") in self.fake.reactions or (mid, 10, "x") in self.fake.reactions,
            "the retry publishes the cross mark",
        )
        self.assertTrue(
            any("notes-relay refused" in str(m.get("content") or "") for m in self.fake.messages),
            "the retry tells the requester that nothing was written",
        )
        self.assertIsNone(runner.hold_below, "the hold falls once the refusal is published")
        self.assertGreaterEqual(runner.cursor, later, "the cursor catches up past the request")
        self.assertGreaterEqual(self.persisted_cursor(runner), mid)
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("notes-relay-refusal-held", log)
        self.assertIn("notes-relay-refusal-retried", log)

    def test_a_restart_backfills_the_relay_channel_for_the_refusing_seat(self):
        """A refusal held below the cursor survives a restart only if the backfill can reach the channel:  with no
        consumer running the relay narrow is otherwise never added, and the request would be stranded."""
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNone(daemon.notes_relay)
        runner = daemon.seats["CLAUDE"]
        mid = self.envelope_message()
        self.assertIn(
            mid,
            [m["id"] for m in runner.backfill()],
            "the refusing seat reads the relay channel so a held request comes back",
        )

    def two_seats(self):
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(
            self.secrets_dir / "Codex-zuliprc",
            email="codex-bot@zulip.test",
            key=codex_key,
            site=self.fake.url,
        )
        self.write_config(
            seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n\n[seat.CODEX]\nbot = "Codex"\nwake = "inbox"\n'
        )
        path = self.state_dir / "listener.toml"
        path.write_text(path.read_text() + "\n" + RELAY_SECTION)
        self.fake.streams["notes-relay"] = 140
        daemon = self.started(env=self.env(AGENT_SYNC_PARTITION=self.two_seat_partition()))
        self.assertTrue(daemon.connect_seat("CODEX"))
        return daemon

    def test_every_seats_runner_treats_a_relay_message_as_consumed_and_only_the_reader_acts(self):
        daemon = self.two_seats()
        spooled = []
        daemon.notes_relay.offer = lambda runner, message: (
            spooled.append((runner.seat, message["id"])) or True
        )
        mid = self.fake.add_message(
            "Cursor",
            "notes-relay",
            "[AFC, Cursor] t",
            "@**Claude** and @**Codex** look at this envelope",
        )
        message = dict(self.fake.messages[-1], flags=["mentioned"])
        for seat in ("CLAUDE", "CODEX"):
            runner = daemon.seats[seat]
            self.assertNotEqual(runner.cursor, mid)
            daemon.route_message(runner, message)
            self.assertEqual(runner.cursor, mid, "%s advances past the consumed message" % seat)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(
            self.inbox("CODEX"),
            [],
            "a mention inside a relay envelope must not fill another seat's inbox",
        )
        self.assertEqual(self.ledger("CODEX"), [], "or wake it")
        self.assertEqual(self.ledger(), [])
        self.assertEqual(
            spooled, [("CLAUDE", mid)], "only the configured seat hands it to the consumer"
        )

    def test_the_consumer_seat_offline_loses_nothing_and_the_non_consumer_seeing_it_first_changes_nothing(
        self,
    ):
        """Each runner has its own event queue and its own cursor.  A non-consumer runner advancing never moves the
        consumer seat's cursor, and the consumer seat reads (or backfills from its own cursor) every message."""
        daemon = self.two_seats()
        spooled = []
        daemon.notes_relay.offer = lambda runner, message: (
            spooled.append((runner.seat, message["id"])) or True
        )
        reader, other = daemon.seats["CLAUDE"], daemon.seats["CODEX"]
        mid = self.fake.add_message("Cursor", "notes-relay", "[AFC, Cursor] t", "an envelope")
        message = dict(self.fake.messages[-1])
        before = reader.cursor
        daemon.route_message(other, message)  # the non-consumer reads it FIRST
        self.assertEqual(other.cursor, mid)
        self.assertEqual(
            reader.cursor,
            before,
            "the consumer seat's cursor is independent of every other runner's",
        )
        self.assertEqual(spooled, [], "a non-consumer hands nothing over")
        daemon.route_message(reader, message)  # the consumer seat reads it second
        self.assertEqual(reader.cursor, mid)
        self.assertEqual(spooled, [("CLAUDE", mid)])
        # Consumer offline for a while:  the message is still in Zulip, so its backfill from its own cursor finds it.
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox("CODEX"), [])

    def test_a_request_that_was_not_kept_is_retried_by_the_next_backfill_not_skipped_as_a_duplicate(
        self,
    ):
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
        self.assertNotIn(
            mid, runner.ring, "a failed offer must leave the id out of the dedupe ring"
        )
        daemon.route_message(runner, message)  # the backfill delivers it again
        self.assertEqual(
            offered, [mid, mid], "the second delivery reached the consumer, not the duplicate path"
        )
        self.assertEqual(runner.cursor, mid)

    def test_a_request_posted_while_the_reader_was_disconnected_survives_a_later_mention_moving_the_cursor(
        self,
    ):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        mid = self.envelope_message()  # arrives while this seat is not listening
        later = self.fake.add_message(
            "Codex", "agent-sync", "t", MENTION
        )  # a mention that would carry the cursor past it
        daemon.notes_relay.runner = self.fake_run
        found = runner.backfill()  # the reconnect:  everything after the cursor
        self.assertEqual([m["id"] for m in found if m["id"] in (mid, later)], [mid, later])
        daemon.route_backfill(runner, found)
        self.assertTrue(self.ran.wait(10), "the request still ran")
        self.assertTrue(self.wait(lambda: (mid, 10, "check") in self.fake.reactions))
        self.assertEqual(runner.cursor, later)
        self.assertEqual([r["id"] for r in self.inbox()], [later], "only the mention is inbox work")
        daemon.notes_relay.stop()

    def test_a_reload_with_the_same_relay_config_keeps_the_consumer_and_its_worker(self):
        daemon = self.started_with_relay()
        consumer = daemon.notes_relay
        daemon.reload()
        self.assertIs(
            daemon.notes_relay, consumer, "no replacement, so no second worker on the spool"
        )
        consumer.stop()

    def test_a_reload_that_turns_the_relay_off_waits_for_the_worker_before_it_lets_go(self):
        daemon = self.started_with_relay()
        consumer = daemon.notes_relay
        gate, inside = threading.Event(), threading.Event()

        def slow(argv, **kwargs):
            inside.set()
            gate.wait(10)
            return subprocess.CompletedProcess(argv, 0, b"created note id=n1\n", b"")

        consumer.runner = slow
        mid = self.envelope_message()
        self.pump_until(daemon, inside.is_set)
        self.write_config()  # no [notes_relay] any more
        with mock.patch.object(NR, "STOP_TIMEOUT", 0.2):
            daemon.reload()
        self.assertIs(
            daemon.notes_relay,
            consumer,
            "the worker is still inside a request:  it stays the owner",
        )
        self.assertIn(
            "notes-relay-reload-deferred", (self.state_dir / "logs" / "listener.log").read_text()
        )
        gate.set()
        self.assertTrue(self.wait(lambda: (mid, 10, "check") in self.fake.reactions))
        with mock.patch.object(NR, "STOP_TIMEOUT", 5.0):
            daemon.reload()
        self.assertIsNone(daemon.notes_relay, "once the worker is out the relay is off")
        self.assertFalse(consumer.thread.is_alive())

    def test_a_deferred_reload_completes_when_the_worker_leaves_and_a_request_offered_meanwhile_still_runs(
        self,
    ):
        """The old worker was told to stop but stays inside a request.  It exits once that request ends, and a request
        offered after that must not sit in the spool until some later reload:  the offer itself finishes the swap."""
        daemon = self.started_with_relay()
        consumer = daemon.notes_relay
        gate, inside = threading.Event(), threading.Event()

        def slow(argv, **kwargs):
            inside.set()
            gate.wait(10)
            return subprocess.CompletedProcess(argv, 0, b"created note id=n1\n", b"")

        consumer.runner = slow
        first = self.envelope_message(rid="deferred-request-1")
        self.pump_until(daemon, inside.is_set)
        self.notes_knob = "CODEX"  # the fleet knob moves:  this listener must stop consuming
        with mock.patch.object(NR, "STOP_TIMEOUT", 0.2):
            daemon.reload()
        self.assertIs(daemon.notes_relay, consumer, "still inside a request:  the swap is pending")
        gate.set()
        self.assertTrue(self.wait(lambda: (first, 10, "check") in self.fake.reactions))
        self.assertTrue(self.wait(consumer.has_exited), "the worker leaves after its request")
        self.notes_knob = "CLAUDE"  # and the knob is back before anyone reloads again
        self.calls.clear()
        self.ran.clear()
        second = self.envelope_message(rid="deferred-request-2")
        with mock.patch("agent_sync.notes.subprocess.run", side_effect=self.fake_run):
            self.pump_until(daemon, self.ran.is_set)
            self.assertTrue(
                self.wait(lambda: (second, 10, "check") in self.fake.reactions),
                "the request offered during the pending swap was drained",
            )
        self.assertIsNot(daemon.notes_relay, consumer, "the swap completed without another reload")
        self.assertEqual(len(self.calls), 1)
        self.assertIn(
            "notes-relay-handover", (self.state_dir / "logs" / "listener.log").read_text()
        )
        daemon.notes_relay.stop()

    def test_a_tick_completes_a_pending_swap_with_nothing_offered(self):
        daemon = self.started_with_relay()
        consumer = daemon.notes_relay
        consumer.stopped.set()  # retiring, worker never ran
        self.assertIs(daemon.notes_relay, consumer)
        daemon.tick()
        self.assertIsNot(daemon.notes_relay, consumer)
        self.assertIsNotNone(daemon.notes_relay)
        daemon.notes_relay.stop()

    def test_a_local_seat_that_differs_from_the_fleet_knob_does_not_consume(self):
        self.notes_knob = "CODEX"  # Infisical names another consumer than this listener's [notes_relay] seat (CLAUDE)
        self.enable()
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNone(daemon.notes_relay)
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("notes-relay-seat-mismatch", log)
        self.assertIn("test knob", log)

    def test_a_reload_after_the_knob_moves_stops_consuming(self):
        daemon = self.started_with_relay()
        consumer = daemon.notes_relay
        self.assertIsNotNone(consumer)
        self.notes_knob = "CODEX"
        daemon.reload()
        self.assertIsNone(daemon.notes_relay)
        self.assertFalse(consumer.thread is not None and consumer.thread.is_alive())
        self.assertIn(
            "notes-relay-seat-mismatch", (self.state_dir / "logs" / "listener.log").read_text()
        )

    def test_another_seat_or_a_disabled_relay_does_not_backfill_the_channel(self):
        daemon = self.two_seats()
        other = daemon.seats["CODEX"]
        mid = self.envelope_message()
        self.assertNotIn(mid, [m["id"] for m in other.backfill()])
        daemon.notes_relay.stop()

    def persisted_cursor(self, runner):
        runner.flush_cursor(force=True)
        with open(runner.paths.cursor) as fh:
            return json.load(fh)["last_id"]

    def test_a_failed_offer_holds_the_persisted_cursor_below_it_for_the_rest_of_a_backfill_batch(
        self,
    ):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        offers = iter([False, True])
        daemon.notes_relay.offer = lambda r, m: next(offers)
        mid = self.envelope_message()  # the request that cannot be spooled
        later = self.fake.add_message(
            "Codex", "agent-sync", "t", MENTION
        )  # a message after it, in the same batch
        found = runner.backfill()
        self.assertEqual([m["id"] for m in found if m["id"] in (mid, later)], [mid, later])
        daemon.route_backfill(runner, found)
        self.assertEqual(
            runner.cursor, mid - 1, "the later message must not carry the cursor past the request"
        )
        self.assertLess(self.persisted_cursor(runner), mid)
        self.assertEqual([r["id"] for r in self.inbox()], [later], "other messages still route")
        # The next backfill (from the persisted cursor) finds the request again, and now it is kept.
        runner.floor = self.persisted_cursor(runner)
        again = runner.backfill()
        self.assertIn(mid, [m["id"] for m in again])
        daemon.route_backfill(runner, again)
        self.assertIsNone(runner.hold_below, "the hold ends once the request is spooled")
        self.assertEqual(runner.cursor, later)
        self.assertEqual(self.persisted_cursor(runner), later)
        self.assertEqual(
            [r["id"] for r in self.inbox()], [later], "the redelivered mention was not routed twice"
        )

    def test_a_failed_offer_holds_the_cursor_for_the_rest_of_an_event_batch_too(self):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        daemon.notes_relay.offer = lambda r, m: False
        mid = self.envelope_message()
        first = dict(self.fake.messages[-1])
        later = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        second = dict(self.fake.messages[-1], flags=["mentioned"])
        daemon.handle_events(
            runner, [{"type": "message", "message": first}, {"type": "message", "message": second}]
        )
        self.assertEqual(runner.hold_below, mid)
        self.assertEqual(runner.cursor, mid - 1)
        self.assertLess(self.persisted_cursor(runner), mid)
        self.assertEqual([r["id"] for r in self.inbox()], [later])
        # Two failures keep the lowest id.
        mid2 = self.envelope_message(rid="daemon-request-2")
        daemon.route_message(runner, dict(self.fake.messages[-1]))
        self.assertEqual(runner.hold_below, mid)
        self.assertGreater(mid2, mid)

    def started_with_relay(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        return self.started()

    def test_a_request_that_could_not_be_spooled_while_connected_is_offered_again_on_the_tick(self):
        """The live event is gone and the channel is only backfilled on a reconnect, so without a retry the request
        would sit until then.  The tick offers it again, runs it, and releases the hold on the cursor."""
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        real_offer = consumer.offer

        def offer(r, m):
            ok = next(outcomes, True)
            return real_offer(r, m) if ok else False

        outcomes = iter([False])
        consumer.offer = offer
        consumer.runner = self.fake_run
        mid = self.envelope_message(rid="retry-request-1")
        daemon.handle_events(runner, [{"type": "message", "message": dict(self.fake.messages[-1])}])
        self.assertEqual(runner.hold_below, mid)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(daemon.notes_relay_retry), 1)
        daemon.last_tick["relay_retry"] = -1e9  # the retry interval has passed
        daemon.tick()
        self.assertTrue(
            self.wait(lambda: (mid, 10, "check") in self.fake.reactions),
            "the request ran after the retry",
        )
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(runner.hold_below, "spooled:  the hold on the cursor ends")
        self.assertEqual(daemon.notes_relay_retry, {})
        self.assertEqual(runner.cursor, mid)
        consumer.stop()

    def test_the_retry_keeps_the_hold_at_the_lowest_waiting_request(self):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        real_offer = consumer.offer
        consumer.runner = self.fake_run
        consumer.offer = lambda r, m: False
        first = self.envelope_message(rid="retry-request-2")
        second = self.envelope_message(rid="retry-request-3")
        daemon.handle_events(
            runner, [{"type": "message", "message": dict(m)} for m in self.fake.messages[-2:]]
        )
        self.assertEqual(len(daemon.notes_relay_retry), 2)
        self.assertEqual(runner.hold_below, first)
        consumer.offer = lambda r, m: (
            real_offer(r, m) if m["id"] == first else False
        )  # only the first can be kept
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(runner.hold_below, second, "the hold falls to the request still waiting")
        self.assertEqual(list(daemon.notes_relay_retry), [("CLAUDE", second)])
        consumer.stop()

    def test_the_retry_map_is_bounded_and_overflowed_requests_are_recovered_by_an_in_session_backfill(
        self,
    ):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        real_offer = consumer.offer
        consumer.runner = self.fake_run
        consumer.offer = lambda r, m: False
        limit = daemon.NOTES_RELAY_RETRY_LIMIT
        count = limit + 40  # past the bound
        ids = [self.envelope_message(rid="many-request-%03d" % i) for i in range(count)]
        daemon.handle_events(
            runner, [{"type": "message", "message": dict(m)} for m in self.fake.messages[-count:]]
        )
        self.assertEqual(len(daemon.notes_relay_retry), limit, "memory stays bounded")
        self.assertEqual(sorted(mid for (_, mid) in daemon.notes_relay_retry), ids[:limit])
        self.assertEqual(
            daemon.notes_relay_overflow["CLAUDE"][1],
            ids[limit],
            "the marker is the lowest untracked id",
        )
        self.assertEqual(runner.hold_below, ids[0])
        consumer.offer = real_offer  # the spool accepts writes again
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(daemon.notes_relay_retry, {})
        self.assertEqual(daemon.notes_relay_overflow, {}, "the backfill reached the newest message")
        self.assertIsNone(runner.hold_below)
        self.assertTrue(
            self.wait(lambda: len(self.calls) == count, 30),
            "all %d ran, got %d" % (count, len(self.calls)),
        )
        consumer.stop()

    def test_the_cursor_catches_up_to_every_id_routed_while_a_hold_capped_it_on_the_retry_path(
        self,
    ):
        """Messages routed while a hold capped the cursor are remembered:  when the hold ends the cursor goes to the
        highest of them, so a restart does not route them again (duplicate inbox rows and wakes)."""
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        real_offer = consumer.offer
        consumer.runner = self.fake_run
        consumer.offer = lambda r, m: False
        mid = self.envelope_message(rid="catchup-request-1")
        later = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        daemon.handle_events(
            runner,
            [
                {"type": "message", "message": dict(m, flags=["mentioned"])}
                for m in self.fake.messages[-2:]
            ],
        )
        self.assertEqual(runner.cursor, mid - 1, "capped below the unspooled request")
        consumer.offer = real_offer
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertIsNone(runner.hold_below)
        self.assertEqual(
            runner.cursor, later, "the cursor reaches the later message that was already routed"
        )
        runner.flush_cursor(force=True)
        self.assertEqual(self.persisted_cursor(runner), later)
        consumer.stop()

    def test_the_cursor_catches_up_to_every_id_routed_while_a_hold_capped_it_on_the_overflow_path(
        self,
    ):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        real_offer = consumer.offer
        consumer.runner = self.fake_run
        consumer.offer = lambda r, m: False
        count = daemon.NOTES_RELAY_RETRY_LIMIT + 3
        for i in range(count):
            self.envelope_message(rid="catchup-many-%03d" % i)
        later = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        batch = [dict(m, flags=["mentioned"]) for m in self.fake.messages[-(count + 1) :]]
        daemon.handle_events(runner, [{"type": "message", "message": m} for m in batch])
        self.assertTrue(daemon.notes_relay_overflow)
        consumer.offer = real_offer
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(
            (daemon.notes_relay_retry, daemon.notes_relay_overflow, runner.hold_below),
            ({}, {}, None),
        )
        self.assertEqual(runner.cursor, later)
        consumer.stop()

    def test_requests_past_the_retry_limit_are_still_refused_with_no_consumer(self):
        """Past the retry limit only the overflow marker remembers a request, and with no consumer nothing spools
        it:  the refusal is the answer, so the tick must walk the relay channel from the marker rather than leave the
        marker pinned and the cursor below it for ever."""
        self.fake.streams["notes-relay"] = 140
        daemon = self.started()
        self.assertIsNone(daemon.notes_relay, "there is no consumer, so the seat refuses")
        runner = daemon.seats["CLAUDE"]
        # Fill the retry map past its limit so a later request can only live in the overflow marker.
        base = self.fake.messages[-1]["id"] if self.fake.messages else 1
        for i in range(daemon.NOTES_RELAY_RETRY_LIMIT + 2):
            daemon._keep_for_relay_retry(runner, {"id": base + 1 + i})
        self.assertTrue(daemon.notes_relay_overflow, "the map is full, so the marker is in use")
        mid = self.envelope_message(mention=True, rid="overflow-refuse-2")
        daemon._keep_for_relay_retry(runner, {"id": mid})
        self.assertNotIn(
            (runner.seat, mid),
            daemon.notes_relay_retry,
            "this request is past the limit, so only the marker remembers it",
        )
        self.assertEqual(
            daemon.notes_relay_overflow["CLAUDE"][1],
            base + 1 + daemon.NOTES_RELAY_RETRY_LIMIT,
            "the marker sits at the lowest id the full map could not track",
        )
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(daemon.notes_relay_overflow, {}, "the marker drains once the refusals land")
        self.assertTrue(
            self.wait(
                lambda: any(
                    str(m.get("content") or "").startswith("notes-relay refused overflow-refuse-2:")
                    for m in self.fake.messages
                )
            ),
            "the overflowed request is refused, not merely forgotten",
        )

    def test_an_overflow_backfill_stops_on_a_page_with_no_new_ids(self):
        """A deleted anchor (or a page of ids already seen) must end the pass, not repeat it to the page cap."""
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        consumer.runner = self.fake_run
        mid = self.envelope_message(rid="deleted-anchor-1")
        daemon.notes_relay_overflow["CLAUDE"] = (runner, mid)
        runner.hold_below = mid
        fetches = []

        def stale_page(narrow, *, anchor, before=0, after=0, include=False):
            fetches.append(
                anchor
            )  # always a full page of ids at or below the marker:  nothing new, ever
            return [dict(m) for m in self.fake.messages if m["id"] <= mid] * 100

        runner.fetch = stale_page
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(
            [a for a in fetches if a == mid - 1],
            [mid - 1],
            "a page with no new ids ends the pass at once",
        )
        self.assertLessEqual(len(fetches), 3, fetches)
        self.assertEqual(daemon.notes_relay_overflow, {})
        self.assertIsNone(runner.hold_below)
        consumer.stop()

    def test_stopping_consumption_on_purpose_releases_the_holds_and_the_cursor_catches_up(self):
        for why, change in (
            ("the relay is disabled", "disable"),
            ("this listener is not the consumer", "knob"),
        ):
            with self.subTest(why):
                daemon = self.started_with_relay()
                runner = daemon.seats["CLAUDE"]
                consumer = daemon.notes_relay
                consumer.offer = lambda r, m: False
                mid = self.envelope_message(rid="abandon-%s" % change)
                later = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
                daemon.handle_events(
                    runner,
                    [
                        {"type": "message", "message": dict(m, flags=["mentioned"])}
                        for m in self.fake.messages[-2:]
                    ],
                )
                daemon.notes_relay_overflow["CLAUDE"] = (
                    runner,
                    mid + 1000,
                )  # an overflow marker too
                self.assertEqual(runner.hold_below, mid)
                self.assertEqual(runner.cursor, mid - 1)
                if change == "disable":
                    self.write_config()  # no [notes_relay] any more
                    self.notes_knob = "CODEX"  # and another seat is the consumer, so this one is not its refuser
                else:
                    self.notes_knob = "CODEX"
                daemon.reload()
                self.assertIsNone(daemon.notes_relay)
                self.assertEqual((daemon.notes_relay_retry, daemon.notes_relay_overflow), ({}, {}))
                self.assertIsNone(runner.hold_below)
                self.assertEqual(
                    runner.cursor, later, "the cursor catches up to what was already routed"
                )
                log = (self.state_dir / "logs" / "listener.log").read_text()
                line = next(l for l in log.splitlines() if "notes-relay-holds-abandoned" in l)
                self.assertIn(why, line)
                self.assertIn(str(mid), line)
                self.assertIn("stay in Zulip", line)
                self.assertNotIn("relay body", line, "ids only")
                self.notes_knob = "CLAUDE"
                self.tearDown()
                self.setUp()

    def test_advance_and_set_hold_race_without_an_error_and_the_cursor_reaches_every_routed_id(
        self,
    ):
        """advance() runs on the seat's event thread, set_hold() on the tick thread.  Hammer both:  no exception (the
        old code could compute None - 1) and, once the last hold ends, a cursor at or above every id routed."""
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        errors: list[BaseException] = []
        base = (runner.cursor or 0) + 10
        rounds = 20000
        stop = threading.Event()

        def routing():
            try:
                for i in range(1, rounds + 1):
                    runner.advance(base + i)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                stop.set()

        def ticking():
            try:
                n = 0
                while not stop.is_set():
                    n += 1
                    runner.set_hold(base + 1 + (n % 50) if n % 3 else None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=routing), threading.Thread(target=ticking)]
        interval = sys.getswitchinterval()
        sys.setswitchinterval(
            1e-6
        )  # switch threads as often as possible, to give a race a real chance
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        finally:
            sys.setswitchinterval(interval)
        self.assertEqual(errors, [])
        runner.set_hold(None)
        self.assertGreaterEqual(
            runner.cursor, base + rounds, "no routed id was lost to a racing update"
        )
        self.assertIsNone(runner.held_high)
        daemon.notes_relay.stop()

    def test_a_relay_message_the_new_seat_sees_during_a_deferred_handover_is_recovered_by_a_backfill(
        self,
    ):
        daemon = self.two_seats()
        old = daemon.notes_relay
        new_runner = daemon.seats["CODEX"]
        self.assertEqual(old.seat, "CLAUDE")
        gate, inside = threading.Event(), threading.Event()

        def slow(argv, **kwargs):
            inside.set()
            gate.wait(10)
            return subprocess.CompletedProcess(argv, 0, b"created note id=n1\n", b"")

        old.runner = slow
        busy = self.envelope_message(rid="handover-busy-1")
        self.pump_until(daemon, inside.is_set)
        path = self.state_dir / "listener.toml"
        head, _, tail = path.read_text().partition("[notes_relay]")
        path.write_text(head + "[notes_relay]" + tail.replace('seat = "CLAUDE"', 'seat = "CODEX"'))
        self.notes_knob = "CODEX"
        with mock.patch.object(NR, "STOP_TIMEOUT", 0.2):
            daemon.reload()
        self.assertIs(
            daemon.notes_relay,
            old,
            "the old worker is still inside a request:  the swap is pending",
        )
        # CODEX's runner sees a relay request now:  the old consumer does not take it, but the cursor moves on.
        env = make_envelope(
            "create", "[AFC, Cursor] gap", "gap body", {}, "handover-gap-001", "CURSOR"
        )
        gap = self.fake.add_message(
            "Cursor", "notes-relay", "[AFC, Cursor] gap", N.render_message(env)
        )
        daemon.handle_events(
            new_runner, [{"type": "message", "message": dict(self.fake.messages[-1])}]
        )
        self.assertEqual(daemon.notes_relay_overflow["CODEX"][1], gap, "the gap is marked at once")
        self.assertEqual(new_runner.hold_below, gap)
        self.assertEqual(
            new_runner.cursor, gap - 1, "the new seat's cursor does not move past the request"
        )
        new_runner.flush_cursor(force=True)
        self.assertLess(
            self.persisted_cursor(new_runner),
            gap,
            "a crash now would not lose the request after a restart",
        )
        gate.set()
        self.assertTrue(self.wait(lambda: (busy, 10, "check") in self.fake.reactions))
        self.assertTrue(self.wait(old.has_exited))
        self.ran.clear()
        with mock.patch("agent_sync.notes.subprocess.run", side_effect=self.fake_run):
            daemon.tick()  # completes the swap
            self.assertEqual(daemon.notes_relay.seat, "CODEX")
            daemon.last_tick["relay_retry"] = -1e9
            daemon.tick()  # the in-session backfill reads the relay channel from the gap
            self.assertTrue(
                self.wait(self.ran.is_set), "the request the new seat saw during the handover ran"
            )
        self.assertEqual(daemon.notes_relay_overflow, {})
        self.assertIsNone(new_runner.hold_below)
        daemon.notes_relay.stop()

    def test_the_hold_stays_while_the_overflow_backfill_cannot_spool(self):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        consumer = daemon.notes_relay
        real_offer = consumer.offer
        consumer.runner = self.fake_run
        consumer.offer = lambda r, m: False
        limit = daemon.NOTES_RELAY_RETRY_LIMIT
        count = limit + 5
        ids = [self.envelope_message(rid="stuck-request-%03d" % i) for i in range(count)]
        daemon.handle_events(
            runner, [{"type": "message", "message": dict(m)} for m in self.fake.messages[-count:]]
        )
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()  # the spool still refuses everything
        self.assertEqual(runner.hold_below, ids[0])
        self.assertIn("CLAUDE", daemon.notes_relay_overflow)
        # It accepts the first overflowed request and then refuses:  progress is kept, the hold moves only that far.
        accepted = []

        def partly(r, m):
            if m["id"] <= ids[limit] or m["id"] in daemon.notes_relay_retry_ids():
                accepted.append(m["id"])
                return real_offer(r, m)
            return False

        daemon.notes_relay_retry_ids = lambda: {mid for (_, mid) in daemon.notes_relay_retry}
        consumer.offer = partly
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(
            daemon.notes_relay_overflow["CLAUDE"][1],
            ids[limit + 1],
            "resumes at the first refused request",
        )
        self.assertEqual(
            runner.hold_below,
            ids[limit + 1]
            if not daemon.notes_relay_retry
            else min(mid for (_, mid) in daemon.notes_relay_retry),
        )
        consumer.offer = real_offer
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertEqual(
            (daemon.notes_relay_retry, daemon.notes_relay_overflow, runner.hold_below),
            ({}, {}, None),
        )
        self.assertTrue(self.wait(lambda: len(self.calls) == count, 30))
        consumer.stop()

    def test_held_requests_move_to_the_new_consumer_when_a_reload_changes_the_seat(self):
        daemon = self.two_seats()
        old_runner = daemon.seats["CLAUDE"]
        old = daemon.notes_relay
        self.assertEqual(old.seat, "CLAUDE")
        old.offer = lambda r, m: False
        mid = self.envelope_message(rid="moved-request-1")
        daemon.handle_events(
            old_runner, [{"type": "message", "message": dict(self.fake.messages[-1])}]
        )
        self.assertEqual(old_runner.hold_below, mid)
        self.assertEqual(list(daemon.notes_relay_retry), [("CLAUDE", mid)])
        path = self.state_dir / "listener.toml"
        text = path.read_text()
        head, _, tail = text.partition("[notes_relay]")
        path.write_text(head + "[notes_relay]" + tail.replace('seat = "CLAUDE"', 'seat = "CODEX"'))
        self.notes_knob = "CODEX"
        daemon.reload()
        new = daemon.notes_relay
        self.assertIsNotNone(new)
        self.assertEqual(new.seat, "CODEX")
        self.assertIsNot(new, old)
        new.runner = self.fake_run
        daemon.last_tick["relay_retry"] = -1e9
        daemon.tick()
        self.assertTrue(
            self.wait(lambda: len(self.calls) == 1), "the new consumer ran the stranded request"
        )
        self.assertEqual(daemon.notes_relay_retry, {})
        self.assertIsNone(
            old_runner.hold_below, "the old runner's hold is released once it is spooled"
        )
        self.assertEqual(old_runner.cursor, mid)
        new.stop()

    def test_the_cursor_is_held_for_a_request_that_could_not_be_kept(self):
        daemon = self.started_with_relay()
        runner = daemon.seats["CLAUDE"]
        daemon.notes_relay.offer = lambda r, m: False
        before = runner.cursor
        mid = self.envelope_message()
        daemon.route_message(runner, self.fake.messages[-1])
        self.assertEqual(
            runner.cursor, before, "a request that was not accepted must not move the cursor"
        )
        self.assertNotEqual(before, mid)
        self.assertIn("notes-relay-held", (self.state_dir / "logs" / "listener.log").read_text())

    def test_a_request_spooled_by_an_earlier_run_is_answered_when_the_reader_connects(self):
        self.enable()
        self.fake.streams["notes-relay"] = 140
        mid = self.envelope_message()  # in Zulip, before this daemon ever connects
        message = dict(self.fake.messages[-1])
        spool = self.state_dir / "notes-relay" / "spool"
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
        self.assertIn(
            "notes-relay-unavailable", (self.state_dir / "logs" / "listener.log").read_text()
        )


    def test_disabled_reload_keeps_pending_refusal(self):
        self.write_config(wake="inbox")
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        mid = self.envelope_message(rid="fixture-disabled-reload")
        with mock.patch.object(NR, "refuse_unconsumed", side_effect=Z.NetworkError("unreachable")):
            daemon.route_message(runner, self.fake.messages[-1])
        self.assertIn((runner.seat, mid), daemon.notes_relay_retry)
        daemon.reload()
        self.assertIn((runner.seat, mid), daemon.notes_relay_retry)
        self.assertEqual(runner.hold_below, mid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RefusalBoundaryTests(unittest.TestCase):
    def runner(self, root):
        from types import SimpleNamespace as NS
        client = mock.Mock()
        message = {"id": 101, "sender_id": 11, "stream_id": 7, "subject": "relay",
                   "content": N.render_message(make_envelope("create", "[AFC, Codex] Relay", "Body.", {}, "fixture-request", "CODEX"))}
        client.get.side_effect = lambda path, *args, **kwargs: ({"message": message} if path != "messages" else {"messages": [], "found_newest": False})
        runner = NS(client=client, me=NS(user_id=10), paths=NS(root=root),
                    users={11: {"user_id": 11, "email": "codex-bot@zulip.test", "is_bot": True}},
                    daemon=NS(config=NS(owner_user_id=1)))
        return runner, message

    def test_empty_forward_scan_and_malformed_pages(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            runner, message = self.runner(root)
            self.assertFalse(NR.already_replied(runner, message, 101, "refused", lambda *a, **k: None))
            for payload in ({}, {"messages": None, "found_newest": False}):
                runner.client.get.side_effect = None
                runner.client.get.return_value = payload
                with self.assertRaises(NR.MalformedResponse):
                    NR.already_replied(runner, message, 101, "refused", lambda *a, **k: None)

    def test_untrusted_sender_is_not_answered(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            runner, message = self.runner(root)
            runner.users[11]["is_bot"] = False
            self.assertFalse(NR.refuse_unconsumed(runner, message, "Disabled.", lambda *a, **k: None))
            runner.client.post.assert_not_called()

    def test_unknown_send_is_not_sent_again(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            runner, message = self.runner(root)
            sends = []
            def post(path, params):
                if path == "messages":
                    sends.append(params)
                    raise Z.NetworkError("timed out", maybe_sent=True)
                return {}
            runner.client.post.side_effect = post
            with self.assertRaises(Z.NetworkError):
                NR.refuse_unconsumed(runner, message, "Disabled.", lambda *a, **k: None)
            self.assertTrue(NR.refuse_unconsumed(runner, message, "Disabled.", lambda *a, **k: None))
            self.assertEqual(len(sends), 1)

    def test_a_refusal_that_never_reached_zulip_is_sent_again(self):
        """A refused connection never reached Zulip, so the outcome is definite, not ambiguous:  the row must not
        stay `unknown`, or every retry stops at the reconcile guard and the refusal is never published at all."""
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            runner, message = self.runner(root)
            sends = []
            def post(path, params):
                if path == "messages":
                    sends.append(params)
                    raise Z.NetworkError("connection refused", maybe_sent=False)
                return {}
            runner.client.post.side_effect = post
            for _ in range(3):
                with self.assertRaises(Z.NetworkError):
                    NR.refuse_unconsumed(runner, message, "Disabled.", lambda *a, **k: None)
            self.assertEqual(len(sends), 3, "each retry sends again rather than wedging on `unknown`")

    def test_malformed_destination_never_posts_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            runner, message = self.runner(root)
            runner.client.get.side_effect = None
            runner.client.get.return_value = {"message": {"stream_id": True, "topic": "relay"}}
            with self.assertRaises(NR.MalformedResponse):
                NR.refuse_unconsumed(runner, message, "Disabled.", lambda *a, **k: None)
            self.assertFalse(any(call.args[0] == "messages" for call in runner.client.post.call_args_list))
