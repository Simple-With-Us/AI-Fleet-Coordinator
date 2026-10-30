"""Every post the daemon composes goes out as the Operator, never as a seat's bot (owner ruling, Oct 30 2026).

The rule, repeated for weeks: anytime an agent's Zulip identity is used it has to be something they
actually thought about and said.  A headless wake's reply, an escalation note, a quota notice, the
held-wake digest and the typing indicator are the daemon's own text, so they are the Operator's.  With
no Operator they are not sent at all: there is no fallback to the seat.

Three layers: the voice on its own (`operator_post.OperatorVoice`, a fake client, no network), the
daemon end to end over the fake Zulip with a TRIPWIRE on the seat's own client (any message, edit or
typing write through it fails the test on the spot), and the router's one exception for the route page.
"""

from __future__ import annotations

import inspect
import json
import re
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import daemon as D
from agent_sync import live as L
from agent_sync import operator_post as OPV
from agent_sync import router as R
from agent_sync import wakes as W
from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.listener_harness import OPERATOR_EMAIL, OPERATOR_USER_ID
from agent_sync.tests.test_daemon import MENTION, DaemonHarness

OWNER = 12
CODEX = 11
CLAUDE_ID = 10
DAEMON_SOURCE = Path(D.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# The voice on its own
# --------------------------------------------------------------------------------------------


class FakeOperatorClient:
    """Enough of a ZulipClient for the voice: `users/me`, and the two writes it forwards."""

    def __init__(self, me: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.creds = Z.Credentials(email=OPERATOR_EMAIL, key="k" * 32, site="http://zulip.test", source="test")
        self.me = me
        self.error = error
        self.calls: list[tuple[str, str, Any]] = []
        self.post_error: Exception | None = None
        self.patch_error: Exception | None = None

    def get(self, path: str, params: Any = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        if self.error is not None:
            raise self.error
        assert self.me is not None
        return self.me

    def post(self, path: str, params: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("POST", path, params))
        if self.post_error is not None:
            raise self.post_error
        return {"id": 77}

    def patch(self, path: str, params: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("PATCH", path, params))
        if self.patch_error is not None:
            raise self.patch_error
        return {}


def operator_me(**changes: Any) -> dict[str, Any]:
    me = {
        "user_id": OPERATOR_USER_ID,
        "email": OPERATOR_EMAIL,
        "is_bot": True,
        "bot_owner_id": OWNER,
        "role": 200,
    }
    me.update(changes)
    return me


class VoiceTests(unittest.TestCase):
    ENV = {"OPERATOR_ZULIP_USER_ID": str(OPERATOR_USER_ID), "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL}

    def setUp(self) -> None:
        self.now = 1000.0
        self.owner = OWNER
        self.built: list[FakeOperatorClient] = []
        self.make: Any = lambda owner: FakeOperatorClient(operator_me())
        self.hidden: list[tuple[str, str]] = []
        self.logged: list[tuple[str, dict[str, Any]]] = []

    def factory(self, owner: int) -> Any:
        client = self.make(owner)
        self.built.append(client)
        return client

    def voice(self, env: dict[str, str] | None = None) -> OPV.OperatorVoice:
        return OPV.OperatorVoice(
            env=self.ENV if env is None else env,
            owner_user_id=lambda: self.owner,
            factory=self.factory,
            clock=lambda: self.now,
            log=lambda event, **fields: self.logged.append((event, fields)),
            hide=lambda email, key: self.hidden.append((email, key)),
        )

    def test_the_verified_client_is_built_once_and_its_key_is_hidden(self) -> None:
        voice = self.voice()
        first = voice.client()
        self.assertIs(voice.client(), first)
        self.assertEqual(len(self.built), 1, "one build, one users/me")
        self.assertEqual(voice.user_id(), OPERATOR_USER_ID)
        self.assertEqual(self.hidden, [(OPERATOR_EMAIL, "k" * 32)], "the key joins what the daemon scrubs")
        self.assertEqual([e for e, _ in self.logged], ["operator-voice-connected"])

    def test_a_bot_that_is_not_the_pinned_operator_is_refused(self) -> None:
        for label, me in (
            ("another id", operator_me(user_id=OPERATOR_USER_ID + 1)),
            ("another address", operator_me(email="claude-bot@zulip.test")),
            ("not a bot", operator_me(is_bot=False)),
            ("another owner", operator_me(bot_owner_id=OWNER + 1)),
        ):
            with self.subTest(label):
                self.make = lambda owner, me=me: FakeOperatorClient(me)
                voice = self.voice()
                with self.assertRaises(OPV.OperatorUnavailable) as caught:
                    voice.client()
                self.assertIn("not the pinned Operator", caught.exception.reason)

    def test_a_host_that_pins_no_operator_has_none(self) -> None:
        voice = self.voice(env={})
        with self.assertRaises(OPV.OperatorUnavailable) as caught:
            voice.client()
        self.assertIn("not configured", caught.exception.reason)

    def test_an_unpinned_owner_means_no_operator(self) -> None:
        self.owner = 0
        voice = self.voice()
        with self.assertRaises(OPV.OperatorUnavailable) as caught:
            voice.client()
        self.assertEqual(caught.exception.code, OPV.CODE_OWNER)
        self.assertEqual(self.built, [], "nothing is built for an owner nobody pinned")

    def test_an_unreadable_vault_is_a_refusal_remembered_for_the_window(self) -> None:
        def broken(owner: int) -> Any:
            raise Z.CredentialError("OPERATOR service: Fleet-OPS /zulip_operator: fleet-secrets timeout")

        self.make = broken
        voice = self.voice()
        with self.assertRaises(OPV.OperatorUnavailable) as caught:
            voice.client()
        self.assertIn("fleet-secrets timeout", caught.exception.reason)
        calls = {"n": 0}

        def counting(owner: int) -> Any:
            calls["n"] += 1
            return broken(owner)

        self.make = counting
        for _ in range(5):
            with self.assertRaises(OPV.OperatorUnavailable):
                voice.client()
        self.assertEqual(calls["n"], 0, "inside the window the vault is not asked again")
        self.now += OPV.RETRY_SECONDS + 1
        self.make = lambda owner: FakeOperatorClient(operator_me())
        self.assertEqual(voice.user_id(), OPERATOR_USER_ID, "and it recovers when the window is over")
        events = [e for e, _ in self.logged]
        self.assertEqual(events.count("operator-voice-unavailable"), 1, "one line per failed window")

    def test_a_rejected_credential_drops_the_client_and_the_next_post_builds_again(self) -> None:
        voice = self.voice()
        client = voice.client()
        client.post_error = Z.ApiError("Invalid API key", code="UNAUTHORIZED", status=401)
        with self.assertRaises(Z.ApiError):
            voice.post({"type": "stream", "to": "x", "topic": "t", "content": "hi"})
        self.assertIsNot(voice.client(), client)
        self.assertEqual(len(self.built), 2)

    def test_a_changed_owner_pin_builds_a_client_for_the_new_pin(self) -> None:
        voice = self.voice()
        voice.client()
        self.owner = OWNER + 1
        self.make = lambda owner: FakeOperatorClient(operator_me(bot_owner_id=OWNER + 1))
        voice.client()
        self.assertEqual(len(self.built), 2, "the guard's owner pin is part of the client")

    def test_the_pinned_id_comes_from_the_host_environment(self) -> None:
        self.assertEqual(OPV.pinned_user_id(self.ENV), OPERATOR_USER_ID)
        self.assertEqual(OPV.pinned_user_id({}), 0)
        self.assertEqual(OPV.pinned_user_id({"OPERATOR_ZULIP_USER_ID": "x", "OPERATOR_ZULIP_EMAIL": "a@b"}), 0)


    # ---- the build never runs under the lock (review findings on PR 1175) ------------------------
    def test_a_build_in_progress_does_not_hold_the_lock(self) -> None:
        """One slow vault read must not stall everyone behind a lock: while a build is stuck, a
        caller that must not wait gets an answer at once and the cached state can be read."""
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Event()

        def stuck(owner: int) -> Any:
            started.set()
            release.wait(30)
            return FakeOperatorClient(operator_me())

        self.make = stuck
        voice = self.voice()
        builder = threading.Thread(target=voice.client, daemon=True)
        builder.start()
        self.assertTrue(started.wait(3.0), "the build started")
        answers: list[Any] = []

        def ask() -> None:
            try:
                voice.client(wait=False)
            except OPV.OperatorUnavailable as exc:
                answers.append(exc.code)
            answers.append(voice.known_unavailable())
            voice.forget(None)

        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        asker.join(3.0)
        self.assertFalse(asker.is_alive(), "a caller waited behind the build's lock")
        self.assertEqual(answers, [OPV.CODE_BUILDING, None])
        release.set()
        builder.join(3.0)
        self.assertFalse(builder.is_alive())
        self.assertEqual(voice.user_id(), OPERATOR_USER_ID)

    def test_callers_that_arrive_during_a_build_share_it(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        calls = {"n": 0}

        def slow(owner: int) -> Any:
            calls["n"] += 1
            release.wait(30)
            return FakeOperatorClient(operator_me())

        self.make = slow
        voice = self.voice()
        got: list[Any] = []
        threads = [threading.Thread(target=lambda: got.append(voice.client()), daemon=True) for _ in range(5)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        release.set()
        for thread in threads:
            thread.join(3.0)
        self.assertEqual(calls["n"], 1, "one build, however many callers")
        self.assertEqual(len(got), 5)
        self.assertEqual(len({id(client) for client in got}), 1)

    def test_a_caller_that_must_not_wait_starts_the_build_in_the_background(self) -> None:
        voice = self.voice()
        with self.assertRaises(OPV.OperatorUnavailable) as caught:
            voice.client(wait=False)
        self.assertEqual(caught.exception.code, OPV.CODE_BUILDING)
        deadline = time.monotonic() + 3.0
        client = None
        while time.monotonic() < deadline and client is None:
            try:
                client = voice.client(wait=False)
            except OPV.OperatorUnavailable:
                time.sleep(0.01)
        self.assertIsNotNone(client, "the background build finished and the next ask finds the client")
        self.assertEqual(len(self.built), 1)

    def test_a_failed_background_build_is_raised_to_the_next_caller_that_must_not_wait(self) -> None:
        def broken(owner: int) -> Any:
            raise Z.CredentialError("OPERATOR service: Fleet-OPS /zulip_operator: fleet-secrets timeout")

        self.make = broken
        voice = self.voice()
        voice.warm()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and voice.known_unavailable() is None:
            time.sleep(0.01)
        self.assertIn("fleet-secrets timeout", voice.known_unavailable() or "")
        with self.assertRaises(OPV.OperatorUnavailable) as caught:
            voice.client(wait=False)
        self.assertEqual(caught.exception.code, OPV.CODE_UNAVAILABLE)

    def test_known_unavailable_reads_state_only(self) -> None:
        voice = self.voice()
        self.assertIsNone(voice.known_unavailable(), "nothing is known before the first build")
        self.assertEqual(self.built, [], "and asking builds nothing")
        voice.client()
        self.assertIsNone(voice.known_unavailable(), "a cached client is reachable")

    def test_a_pin_that_moves_during_a_build_is_not_published(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Event()

        def slow(owner: int) -> Any:
            started.set()
            release.wait(30)
            return FakeOperatorClient(operator_me())

        self.make = slow
        voice = self.voice()
        outcome: list[Any] = []

        def build() -> None:
            try:
                outcome.append(voice.client())
            except OPV.OperatorUnavailable as exc:
                outcome.append(exc.code)

        thread = threading.Thread(target=build, daemon=True)
        thread.start()
        self.assertTrue(started.wait(3.0))
        self.owner = OWNER + 1  # a reload moved the pin while the vault was being read
        release.set()
        thread.join(3.0)
        self.assertEqual(outcome, [OPV.CODE_OWNER])
        self.assertIsNone(voice.known_unavailable(), "and a moved pin is not a failed build to wait out")

    def test_a_403_on_an_edit_drops_the_client_like_a_post(self) -> None:
        for call in ("post", "edit"):
            with self.subTest(call):
                voice = self.voice()
                client = voice.client()
                client.post_error = Z.ApiError("Forbidden", code="UNAUTHORIZED", status=403)
                client.patch_error = Z.ApiError("Forbidden", code="UNAUTHORIZED", status=403)
                with self.assertRaises(Z.ApiError):
                    if call == "post":
                        voice.post({"type": "stream", "to": "x", "topic": "t", "content": "hi"})
                    else:
                        voice.edit(5, "hi")
                self.assertIsNot(voice.client(), client, "the rejected client is not reused")


# --------------------------------------------------------------------------------------------
# The daemon, with a tripwire on the seat's own client
# --------------------------------------------------------------------------------------------


class OperatorHarness(DaemonHarness):
    """A daemon whose seat bot cannot write a message, an edit or a typing indicator.

    The tripwire wraps the seat client's `request`, which every write goes through: a write to
    `messages`, `messages/<id>` or `typing` raises AssertionError on the spot, so on the code before
    this change (which posted through that client) every test here fails at the post."""

    def started(self, **kw: Any) -> D.Daemon:
        daemon = super().started(**kw)
        self.trip(daemon.seats["CLAUDE"])
        return daemon

    def trip(self, runner: D.SeatRunner) -> None:
        real = runner.client.request

        def guarded(method: str, path: str, params: Any = None, **kw: Any) -> Any:
            if method.upper() != "GET" and (path in ("messages", "typing") or path.startswith("messages/")):
                raise AssertionError("a seat's own bot was used to write: %s %s" % (method, path))
            return real(method, path, params, **kw)

        runner.client.request = guarded

    def log_text(self) -> str:
        return (self.state_dir / "logs" / "listener.log").read_text()

    def log_events(self, name: str) -> list[dict[str, Any]]:
        rows = [json.loads(line) for line in self.log_text().splitlines() if line.strip()]
        return [row for row in rows if row.get("event") == name]

    def peer_wake(self, daemon: D.Daemon, topic: str = "t") -> int:
        mid = self.fake.add_message("Codex", "agent-sync", topic, MENTION)
        self.pump_until(daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger()))
        self.wake_cycle(daemon)
        return mid

    def final_row(self) -> dict[str, Any]:
        return self.terminal_wakes()[-1]


class WakeReplyTests(OperatorHarness):
    def test_a_channel_wake_reply_is_the_operators_and_keeps_the_seat_tag(self) -> None:
        daemon = self.started()
        mid = self.peer_wake(daemon)
        posts = self.operator_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["display_recipient"], posts[0]["subject"]), ("agent-sync", "t"))
        self.assertTrue(posts[0]["content"].startswith("[CLAUDE·wake] re=%d\n" % mid), posts[0]["content"])
        self.assertEqual(self.seat_requests(), [], "the seat's own bot wrote nothing")
        row = self.final_row()
        self.assertEqual((row["state"], row["posted_id"], row["posted_by"]), ("done", posts[0]["id"], "OPERATOR"))

    def test_a_dm_wake_reply_is_the_operators_in_the_thread_with_the_seat_and_the_owner(self) -> None:
        daemon = self.started()
        dm = self.fake.add_direct_message("Jay Wedgeworth", "status of the cutover?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        posts = self.operator_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(sorted(r["id"] for r in posts[0]["display_recipient"]), [CLAUDE_ID, OWNER, OPERATOR_USER_ID])
        self.assertTrue(posts[0]["content"].startswith("[CLAUDE·wake] re=%d" % dm))
        self.assertEqual(self.seat_requests(), [])

    def test_a_reply_in_a_new_dm_thread_still_counts_on_the_thread_that_woke_the_seat(self) -> None:
        """The Operator joins the thread, so the reply is a different conversation to the router and
        its loop-guard count would never reach the trigger thread's key.  The daemon counts it there
        too, so three wake replies still stop a thread from waking."""
        daemon = self.started()
        self.fake.add_direct_message("Codex", "status of the cutover?", recipients=[self.fake.me, self.fake.user_by_id(OWNER)], deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 25)
        self.assertEqual(len(self.operator_posts()), 1, "the owner is in the thread, so the reply is allowed")
        trigger_key = L.topic_key("DM", "%d,%d,%d" % (CLAUDE_ID, CODEX, OWNER))
        self.assertEqual(daemon.loopguard.count(trigger_key, self.clock.time()), 1)

    def test_the_bot_to_bot_guard_still_refuses_a_reply_with_no_owner_in_the_thread(self) -> None:
        daemon = self.started()
        self.fake.add_direct_message("Codex", "status?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(self.operator_posts(), [], "the Operator gets no exemption from AFC-469 here")
        self.assertEqual(self.final_row()["post_error"], "dm_owner_not_participant")

    def test_a_wake_in_a_channel_the_operator_cannot_post_in_is_a_logged_refusal(self) -> None:
        """Not in the private channel, say: Zulip refuses, the wake fails with the reason on its row,
        and the seat's bot is NOT tried instead."""
        daemon = self.started()
        self.fake.inject(
            "POST",
            "messages",
            status=400,
            body={"result": "error", "msg": "Not authorized to send to channel", "code": "BAD_REQUEST"},
        )
        self.peer_wake(daemon)
        self.assertEqual(self.operator_posts(), [])
        row = self.final_row()
        self.assertEqual((row["state"], row["post_error"]), ("failed", "post_failed"))
        failed = self.log_events("post-failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["posted_by"], "OPERATOR")
        self.assertIn("Not authorized", failed[0]["error"])
        self.assertEqual(self.seat_requests(), [])

    def test_two_posts_in_one_wake_are_kept_three_seconds_apart(self) -> None:
        daemon = self.started()
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "No.", "board": None, "risk": "high",
                                           "owner_note": "asked for a key"})
        self.peer_wake(daemon)
        self.assertEqual(len(self.operator_posts()), 2, "the reply and the owner DM")
        self.assertIn(D.POST_SPACING, self.clock.slept, "the second post waited out the spacing")


class NoOperatorTests(OperatorHarness):
    """With no Operator the wake is not run at all, and nothing is posted by anyone else either."""

    def assert_not_run(self, reason: str = "operator_unavailable") -> None:
        self.assertEqual(self.dumps(), [], "the model was never run:  no budget was spent on it")
        self.assertNotIn("started", [row["state"] for row in self.ledger()])
        self.assertEqual((self.ledger()[-1]["state"], self.ledger()[-1]["reason"]), ("dropped", reason))
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(self.fake.requests_to("POST", "messages"), [], "not one message left the daemon")

    def test_an_unreadable_vault_runs_no_wake_and_does_not_use_the_seat(self) -> None:
        self.operator_available = False
        daemon = self.started()
        self.peer_wake(daemon)
        self.assert_not_run()
        refused = self.log_events("operator-post-refused")
        self.assertEqual([r["site"] for r in refused], ["wake-preflight"])
        self.assertIn("fleet-secrets timeout", refused[0]["reason"])
        self.assertEqual(self.log_events("operator-voice-unavailable")[0]["code"], "operator_unavailable")

    def test_a_bot_that_is_not_the_pinned_operator_is_never_used_to_post(self) -> None:
        """A factory that hands back a SEAT's client (a misconfigured credential) is caught by the
        identity check, and nothing is posted as that seat."""
        seat_credentials = Z.Credentials(email=BOT_EMAIL, key=self.key, site=self.fake.url, source="test")
        daemon = self.daemon()
        daemon.operator_voice._factory = lambda owner: Z.ZulipClient(
            seat_credentials, self.fake.url, owner_user_id=owner
        )
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        self.trip(daemon.seats["CLAUDE"])
        self.peer_wake(daemon)
        self.assert_not_run()
        refused = self.log_events("operator-post-refused")
        self.assertIn("not the pinned Operator", refused[0]["reason"])

    def test_a_host_that_pins_no_operator_runs_no_wake(self) -> None:
        daemon = self.started(env=self.env(OPERATOR_ZULIP_USER_ID=None, OPERATOR_ZULIP_EMAIL=None))
        self.peer_wake(daemon)
        self.assert_not_run()

    def test_a_known_outage_is_checked_without_the_network_before_the_wake_is_queued(self) -> None:
        """After one failed build the router-side check (`_wake_block`) knows the Operator is away for the
        window, so later wakes are dropped when they are finalized, with no vault read and no queue."""
        self.operator_available = False
        daemon = self.started()
        for topic in ("a", "b", "c"):
            self.peer_wake(daemon, topic)
        self.assertEqual(self.operator_builds, 1, "three dropped wakes cost one vault read")
        self.assertNotIn("accepted", [row["state"] for row in self.ledger()[3:]])
        self.assertEqual(self.dumps(), [])
        self.operator_available = True
        self.clock.advance(OPV.RETRY_SECONDS + 1)
        self.peer_wake(daemon, "d")
        self.assertEqual(self.operator_builds, 2)
        self.assertEqual(len(self.operator_posts()), 1, "and the Operator speaks again when it is back")
        self.peer_wake(daemon, "e")
        self.assertEqual(self.operator_builds, 2, "one client serves every later post")

    def test_a_rejected_operator_key_is_a_failed_post_and_the_next_one_builds_a_new_client(self) -> None:
        daemon = self.started()
        self.fake.inject(
            "POST", "messages", status=401, body={"result": "error", "msg": "Invalid API key", "code": "UNAUTHORIZED"}
        )
        self.peer_wake(daemon, "a")
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(self.operator_builds, 1)
        self.peer_wake(daemon, "b")
        self.assertEqual(self.operator_builds, 2, "the rejected client was dropped")
        self.assertEqual(len(self.operator_posts()), 1)
        self.assertEqual(self.seat_requests(), [])

    def test_an_operator_lost_after_the_preflight_is_a_logged_refusal_and_never_a_seat_post(self) -> None:
        """The preflight passed, then the Operator went away during the run: the reply and the owner
        note are refused and logged, the row says why, and the seat's bot is not tried."""
        daemon = self.started()
        client = daemon.operator_voice.client()
        real_run = daemon.claude_runner.run

        def run_then_lose_the_operator(*args: Any, **kwargs: Any) -> Any:
            result = real_run(*args, **kwargs)
            daemon.operator_voice.forget(client)
            self.operator_available = False
            return result

        daemon.claude_runner.run = run_then_lose_the_operator
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "No.", "board": None, "risk": "high",
                                           "owner_note": "asked for a key"})
        self.peer_wake(daemon)
        row = self.final_row()
        self.assertEqual((row["state"], row["post_error"]), ("failed", "operator_unavailable"))
        self.assertIn("operator unavailable", row["owner_dm_error"])
        self.assertEqual(
            sorted(r["site"] for r in self.log_events("operator-post-refused")), ["owner-dm", "wake-reply"]
        )
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_the_choke_point_scans_the_text_before_it_leaves(self) -> None:
        daemon = self.started()
        daemon.operator_voice.client()
        with self.assertRaises(OPV.OperatorRefused):
            daemon._operator_send(
                {"type": "stream", "to": "agent-sync", "topic": "t", "content": "key %s" % self.operator_key}
            )
        self.assertEqual(self.operator_posts(), [])


class SiteTests(OperatorHarness):
    """Each place the daemon speaks."""

    def escalate(self, daemon: D.Daemon, risk: str = "high") -> int:
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "I can't share that.", "board": None,
                                           "risk": risk, "owner_note": "Codex asked for the key."})
        return self.peer_wake(daemon)

    def test_the_escalation_note_is_a_dm_from_the_operator_tagged_with_the_seat(self) -> None:
        daemon = self.started()
        mid = self.escalate(daemon)
        notes = [m for m in self.operator_posts() if m["type"] == "private"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(sorted(r["id"] for r in notes[0]["display_recipient"]), [OWNER, OPERATOR_USER_ID])
        self.assertTrue(notes[0]["content"].startswith("[CLAUDE·note] re=%d\n" % mid), notes[0]["content"])
        self.assertEqual(self.final_row()["owner_dm"], notes[0]["id"])
        self.assertEqual(self.seat_requests(), [])

    def quota_pending(self, daemon: D.Daemon, sender: int, message_id: int) -> W.Pending:
        now = self.clock.time()
        pending = W.Pending("q%d" % message_id, "CLAUDE", {"channel": "agent-sync", "topic": "t", "type": "stream"},
                            owner=False, now=now, due=now)
        pending.add({"id": message_id, "sender_id": sender, "ts": now}, False)
        return pending

    QUOTA = {"known": True, "exhausted": True, "remaining": 0.0, "reset_at": "2026-10-14T18:00:00Z",
             "reason": "weekly", "source": "codecaps"}
    QUOTA_SEAT = '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\nprovider = "Claude Code"\n'

    def test_the_quota_notice_is_the_operators_and_waits_for_an_operator_without_spending_its_window(self) -> None:
        self.write_config(seats=self.QUOTA_SEAT)
        self.operator_available = False
        daemon = self.started()
        pending = self.quota_pending(daemon, CODEX, 55)
        runner = daemon.seats["CLAUDE"]
        with mock.patch("agent_sync.daemon.A.provider_quota", return_value=self.QUOTA):
            daemon._tell_requester(runner, pending, "quota is exhausted", self.clock.time())
            self.settle_operator(daemon)  # the router thread skipped; the build ran in the background
            daemon._tell_requester(runner, pending, "quota is exhausted", self.clock.time())
            self.assertEqual(self.fake.requests_to("POST", "messages"), [], "no Operator, no notice")
            self.assertEqual([r["site"] for r in self.log_events("operator-post-refused")], ["quota-hold"])
            self.operator_available = True
            self.clock.advance(OPV.RETRY_SECONDS + 1)
            daemon._tell_requester(runner, pending, "quota is exhausted", self.clock.time())
            self.settle_operator(daemon)
            daemon._tell_requester(runner, pending, "quota is exhausted", self.clock.time())
        notices = self.operator_posts()
        self.assertEqual(len(notices), 1, "the window was not spent by the notices that could not go")
        self.assertEqual(sorted(r["id"] for r in notices[0]["display_recipient"]), [CODEX, OWNER, OPERATOR_USER_ID])
        self.assertIn("cannot answer right now", notices[0]["content"])

    def test_a_notice_raised_on_the_router_thread_does_not_wait_for_the_operator_client(self) -> None:
        """No client yet and a build that is stuck: the notice is skipped at once, the build goes on in
        the background, and the router is not held."""
        self.write_config(seats=self.QUOTA_SEAT)
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(owner_user_id: int) -> Any:
            release.wait(30)
            return self.operator_client(owner_user_id)

        daemon = self.started(operator_client_factory=stuck)
        pending = self.quota_pending(daemon, CODEX, 57)
        runner = daemon.seats["CLAUDE"]
        asked = threading.Thread(
            target=lambda: daemon._tell_requester(runner, pending, "quota is exhausted", self.clock.time()),
            daemon=True,
        )
        with mock.patch("agent_sync.daemon.A.provider_quota", return_value=self.QUOTA):
            asked.start()
            asked.join(3.0)
            self.assertFalse(asked.is_alive(), "the router thread waited for the Operator client to be built")
            self.assertEqual(self.operator_posts(), [])
            release.set()
            self.settle_operator(daemon)
            daemon._tell_requester(runner, pending, "quota is exhausted", self.clock.time())
        self.assertEqual(len(self.operator_posts()), 1, "the next ask finds the client and the window unspent")

    def test_the_sender_is_never_the_operator_itself(self) -> None:
        self.write_config(seats=self.QUOTA_SEAT)
        daemon = self.started()
        self.warm_operator(daemon)
        pending = self.quota_pending(daemon, OPERATOR_USER_ID, 56)
        with mock.patch("agent_sync.daemon.A.provider_quota", return_value=self.QUOTA):
            daemon._tell_requester(daemon.seats["CLAUDE"], pending, "quota is exhausted", self.clock.time())
        self.assertEqual(self.operator_posts(), [])

    TYPING_SEAT = '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\ntyping_indicator = true\n'

    def typing_settled(self, count: int = 2, timeout: float = 4.0) -> list[dict[str, str]]:
        """The indicator posts from its own worker thread: wait for `count` posts (start and stop)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.fake.typing) < count:
            time.sleep(0.02)
        return list(self.fake.typing)

    def test_the_typing_indicator_is_the_operators(self) -> None:
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.peer_wake(daemon)
        self.assertEqual([t["op"] for t in self.typing_settled()], ["start", "stop"])
        typing = self.fake.requests_to("POST", "typing")
        self.assertEqual({r.user_id for r in typing}, {OPERATOR_USER_ID}, "it was the Operator that typed")
        self.assertEqual(self.seat_requests(), [])

    def test_the_typing_indicator_for_a_dm_wake_goes_to_the_thread_the_reply_lands_in(self) -> None:
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.fake.add_direct_message("Jay Wedgeworth", "status of the cutover?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        typing = [t for t in self.typing_settled() if t.get("type") == "direct"]
        self.assertTrue(typing)
        self.assertEqual(sorted(json.loads(typing[0]["to"])), [CLAUDE_ID, OWNER], "the seat stays in it")
        self.assertEqual({r.user_id for r in self.fake.requests_to("POST", "typing")}, {OPERATOR_USER_ID})

    def test_no_indicator_for_a_dm_the_reply_cannot_enter(self) -> None:
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.fake.add_direct_message("Codex", "status?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        time.sleep(0.2)
        self.assertEqual(self.fake.typing, [], "no owner in the thread, so no reply and no promise of one")

    def test_no_indicator_while_the_operator_client_is_still_being_built(self) -> None:
        """The indicator is cosmetic: it never waits for a build, it just is not raised this time."""
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        now = self.clock.time()
        pending = W.Pending("t1", "CLAUDE", {"channel": "agent-sync", "topic": "t", "type": "stream",
                                             "stream_id": self.fake.streams["agent-sync"]},
                            owner=False, now=now, due=now)
        self.assertIsNone(daemon._typing_signal(runner, pending))
        self.assertEqual(self.log_events("typing-skipped")[-1]["code"], OPV.CODE_BUILDING)
        self.settle_operator(daemon)
        self.assertIsNotNone(daemon._typing_signal(runner, pending), "and it is raised once the client is ready")

    def test_no_indicator_at_all_when_there_is_no_operator(self) -> None:
        self.operator_available = False
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.peer_wake(daemon)
        time.sleep(0.2)
        self.assertEqual(self.fake.typing, [])
        self.assertEqual(self.dumps(), [], "and the wake itself was not run")


class StructureTests(unittest.TestCase):
    """A tripwire in the source: the daemon no longer writes through a seat's client anywhere."""

    def test_no_write_goes_through_a_seat_client(self) -> None:
        writes = re.findall(r"\bclient\.(?:post|patch|put|delete|request)\(", DAEMON_SOURCE)
        self.assertEqual(writes, [], "a seat's client must not write in the daemon;  use _operator_send")

    def test_the_typing_indicator_is_never_handed_a_seat_client(self) -> None:
        source = inspect.getsource(D.Daemon._typing_signal)
        self.assertNotIn("runner.client", source)
        self.assertIn("operator_voice", source)

    def test_the_old_alert_seat_picker_is_gone(self) -> None:
        self.assertNotIn("_alert_runner", DAEMON_SOURCE)


# --------------------------------------------------------------------------------------------
# The route page is the Operator's, and still reaches the seat it pages
# --------------------------------------------------------------------------------------------

ME = R.SeatIdentity("CODEX", CODEX, "Codex", "codex-bot@zulip.test")
ROUTE = "[CLAUDE·route→CODEX] re=500\n@**Codex|11** CLAUDE paged you on Jay's behalf: look at the build"


def context(**changes: Any) -> R.Context:
    options: dict[str, Any] = dict(
        owner_user_id=OWNER,
        owner_clients=["website"],
        eligible_user_ids=[CLAUDE_ID, CODEX],
        is_bot={CLAUDE_ID: True, CODEX: True, OPERATOR_USER_ID: True, OWNER: False},
        now=10_000.0,
        stale_after=7200.0,
        presence=[],
        operator_user_id=OPERATOR_USER_ID,
    )
    options.update(changes)
    return R.Context(**options)


def message(sender: int, content: str, flags: tuple[str, ...] = ("mentioned",)) -> dict[str, Any]:
    return {"id": 501, "sender_id": sender, "content": content, "flags": list(flags), "timestamp": 9_990,
            "client": "ZulipPython", "type": "stream", "display_recipient": "agent-sync", "subject": "t",
            "sender_full_name": "x"}


class RouteEligibilityTests(unittest.TestCase):
    def wake(self, msg: dict[str, Any], ctx: R.Context | None = None) -> str | None:
        _, decision = R.route(msg, ME, ctx or context(), [])
        return decision.wake

    def test_the_operators_route_page_wakes_the_seat_it_pages(self) -> None:
        self.assertEqual(self.wake(message(OPERATOR_USER_ID, ROUTE)), "peer")

    def test_nothing_else_the_operator_says_is_eligible(self) -> None:
        for label, content in (
            ("a question", "@**Codex|11** which session did you mean?"),
            ("a wake relay", "[CLAUDE·wake] re=500\n@**Codex|11** hello"),
            ("a status message", "[OPERATOR·status] @**Codex|11** your wake ran"),
            ("a note", "[CLAUDE·note] re=500\n@**Codex|11** note"),
        ):
            with self.subTest(label):
                self.assertIsNone(self.wake(message(OPERATOR_USER_ID, content)))

    def test_a_route_tag_from_anyone_else_is_no_wider_than_before(self) -> None:
        stranger = 77
        ctx = context(is_bot={**context().is_bot, stranger: True})
        self.assertIsNone(self.wake(message(stranger, ROUTE), ctx), "an unknown bot is not the Operator")
        self.assertEqual(self.wake(message(CLAUDE_ID, ROUTE)), "peer", "a fleet bot's page works as it did")

    def test_a_host_that_pins_no_operator_makes_no_exception(self) -> None:
        self.assertIsNone(self.wake(message(OPERATOR_USER_ID, ROUTE), context(operator_user_id=0)))

    def test_the_pinned_id_must_be_a_known_bot(self) -> None:
        ctx = context(is_bot={CLAUDE_ID: True, CODEX: True, OWNER: False})
        self.assertIsNone(self.wake(message(OPERATOR_USER_ID, ROUTE), ctx))
        ctx = context(is_bot={**context().is_bot, OPERATOR_USER_ID: False})
        self.assertIsNone(self.wake(message(OPERATOR_USER_ID, ROUTE), ctx))

    def test_an_operator_dm_reaches_the_inbox_and_wakes_no_one(self) -> None:
        """The quota notice, an escalation note and the like land in a seat's inbox as items from a
        sender that is not eligible: they never wake the seat they reach."""
        dm = {
            "id": 502,
            "sender_id": OPERATOR_USER_ID,
            "content": "ANTHROPIC quota is exhausted",
            "flags": [],
            "timestamp": 9_990,
            "client": "ZulipPython",
            "type": "private",
            "display_recipient": [{"id": OPERATOR_USER_ID}, {"id": CODEX}, {"id": OWNER}],
            "subject": "",
        }
        _, decision = R.route(dm, ME, context(), [])
        self.assertTrue(decision.seat_inbox)
        self.assertIsNone(decision.wake)
        self.assertEqual(decision.wake_block, "not_eligible")

    def test_the_operators_route_page_counts_on_the_loop_guard_like_any_route_post(self) -> None:
        classes, _ = R.route(message(OPERATOR_USER_ID, ROUTE), ME, context(), [])
        self.assertTrue(classes.route_tag)
        self.assertFalse(classes.wake_tag)


if __name__ == "__main__":
    unittest.main()
