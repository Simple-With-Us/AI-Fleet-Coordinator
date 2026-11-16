"""Every post the daemon composes goes out as the Operator, never as a seat's bot (owner ruling, Oct 30 2026),
and the Operator never answers on behalf of a platform (owner ruling, Nov 10 2026).

The first rule, repeated for weeks: anytime an agent's Zulip identity is used it has to be something they
actually thought about and said.  An escalation note and the held-wake digest are the daemon's own text, so
they are the Operator's.  With no Operator they are not sent at all: there is no fallback to the seat.
The second rule:  a headless wake's reply, an owner ack, a route page, a quota notice and the typing
indicator are not sent at all, by anyone.

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

from simple_tools import daemon as D
from simple_tools import live as L
from simple_tools import operator_post as OPV
from simple_tools import router as R
from simple_tools import zulip as Z
from simple_tools.tests.fake_zulip import BOT_EMAIL
from simple_tools.tests.listener_harness import OPERATOR_EMAIL, OPERATOR_USER_ID
from simple_tools.tests.test_daemon import MENTION, DaemonHarness

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

    def test_the_snapshot_says_whether_the_operator_can_speak_and_since_when(self) -> None:
        wall = [1_700_000_000.0]  # 2023-11-14 22:13:20 UTC

        def vault_down(owner: int) -> Any:
            raise Z.CredentialError("fleet-secrets is not installed (~/.local/bin/fleet-secrets)")

        voice = OPV.OperatorVoice(
            env=self.ENV,
            owner_user_id=lambda: self.owner,
            factory=self.factory,
            clock=lambda: self.now,
            wall=lambda: wall[0],
        )
        self.assertEqual(voice.snapshot(), {"available": None, "code": None, "since": None})
        self.make = vault_down
        with self.assertRaises(OPV.OperatorUnavailable):
            voice.client()
        down = voice.snapshot()
        self.assertEqual((down["available"], down["code"]), (False, OPV.CODE_UNAVAILABLE))
        self.assertEqual(down["since"], "2023-11-14T22:13:20Z")
        self.assertNotIn("fleet-secrets", json.dumps(down), "a code, never the reason sentence")
        # The same failure retried after its window keeps the time it began.
        wall[0] += 600
        self.now += OPV.RETRY_SECONDS + 1
        with self.assertRaises(OPV.OperatorUnavailable):
            voice.client()
        self.assertEqual(voice.snapshot(), down)
        # It comes back:  reachable, no code, and a new time.
        self.make = lambda owner: FakeOperatorClient(operator_me())
        wall[0] += 600
        self.now += OPV.RETRY_SECONDS + 1
        voice.client()
        self.assertEqual(
            voice.snapshot(), {"available": True, "code": None, "since": "2023-11-14T22:33:20Z"}
        )
        # A rejected credential makes it unavailable again, with its own code.
        wall[0] += 60
        voice.forget(voice.client())
        again = voice.snapshot()
        self.assertEqual((again["available"], again["code"]), (False, "operator_credential_rejected"))
        self.assertEqual(again["since"], "2023-11-14T22:34:20Z")

    def test_a_moved_pin_drops_the_verified_client_and_a_remembered_failure(self) -> None:
        voice = self.voice()
        first = voice.client()
        voice.update_env(dict(self.ENV))  # nothing moved:  the same client stays
        self.assertIs(voice.client(), first)
        self.assertTrue(voice.snapshot()["available"])
        voice.update_env(dict(self.ENV, OPERATOR_ZULIP_EMAIL="other-bot@zulip.test"))
        moved = voice.snapshot()
        self.assertEqual(
            (moved["available"], moved["code"]),
            (None, None),
            "the published state is about the old pin too:  not known until a build answers",
        )
        with self.assertRaises(OPV.OperatorUnavailable) as caught:
            voice.client()  # rebuilt (the fake still answers as the old bot), judged against the new pin
        self.assertIn("not the pinned Operator", caught.exception.reason)
        self.assertEqual(len(self.built), 2, "a new build, not the cached client")
        # A failure remembered under the old pin is forgotten the moment the pin moves.
        self.assertIsNotNone(voice.known_unavailable())
        voice.update_env(dict(self.ENV))
        self.assertIsNone(voice.known_unavailable())

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
        mid = self.fake.add_message("Codex", "simple-tools", topic, MENTION)
        self.pump_until(daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger()))
        self.wake_cycle(daemon)
        return mid

    def final_row(self) -> dict[str, Any]:
        return self.terminal_wakes()[-1]


class WakeReplyTests(OperatorHarness):
    """A wake is answered by nobody:  the Operator never answers on behalf of a platform (owner ruling,
    Nov 10 2026), in a channel or in a direct message, and the seat's own bot never does either."""

    def assert_dropped(self) -> dict[str, Any]:
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(self.seat_requests(), [], "the seat's own bot wrote nothing")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [], "nothing was even attempted")
        row = self.final_row()
        self.assertEqual((row["state"], row["post_error"]), ("done", "operator_never_answers"))
        self.assertIsNone(row["posted_id"])
        self.assertNotIn("posted_by", row)
        return row

    def test_a_channel_wake_is_not_answered(self) -> None:
        daemon = self.started()
        self.peer_wake(daemon)
        self.assert_dropped()
        self.assertEqual(self.log_events("post-refused")[0]["reason"], "operator_never_answers")

    def test_a_dm_wake_with_the_owner_in_the_thread_is_not_answered(self) -> None:
        daemon = self.started()
        self.fake.add_direct_message("Jay Wedgeworth", "status of the cutover?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        self.assert_dropped()

    def test_a_dm_wake_with_no_owner_in_the_thread_is_the_same(self) -> None:
        daemon = self.started()
        self.fake.add_direct_message("Codex", "status?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assert_dropped()
        self.assertEqual(self.log_events("post-refused-dm"), [], "the owner guard never sees a reply to refuse")

    def test_a_dropped_reply_never_counts_on_the_loop_guard_of_the_dm_thread(self) -> None:
        daemon = self.started()
        self.fake.add_direct_message("Codex", "status of the cutover?", recipients=[self.fake.me, self.fake.user_by_id(OWNER)], deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 25)
        self.assert_dropped()
        trigger_key = L.topic_key("DM", "%d,%d,%d" % (CLAUDE_ID, CODEX, OWNER))
        self.assertEqual(daemon.loopguard.count(trigger_key, self.clock.time()), 0)

    def test_nothing_is_attempted_whatever_the_server_would_say(self) -> None:
        """A channel the Operator cannot post in used to be a `post_failed` row and a log line.  Nothing is
        sent now, so the rejection the fake server holds ready is never used, and the Operator is never
        asked to post."""
        daemon = self.started()
        self.fake.inject(
            "POST",
            "messages",
            status=400,
            body={"result": "error", "msg": "Not authorized to send to channel", "code": "BAD_REQUEST"},
        )
        self.peer_wake(daemon)
        self.assert_dropped()
        self.assertEqual(self.log_events("post-failed"), [])
        self.assertEqual(self.log_events("operator-post-refused"), [])

    def test_an_escalating_wake_posts_only_the_owner_note(self) -> None:
        daemon = self.started()
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "No.", "board": None, "risk": "high",
                                           "owner_note": "asked for a key"})
        self.peer_wake(daemon)
        posts = self.operator_posts()
        self.assertEqual(len(posts), 1, "the owner DM, and no reply to the peer")
        self.assertEqual(posts[0]["type"], "private")
        self.assertTrue(posts[0]["content"].startswith("[OPERATOR\u00b7note] re="), posts[0]["content"])
        self.assertEqual(self.final_row()["post_error"], "operator_never_answers")
        self.assertEqual(self.seat_requests(), [])


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

    def test_a_host_that_pins_the_operator_only_in_listener_toml_still_has_a_voice(self) -> None:
        """The Mac's LaunchAgent carries no OPERATOR_ZULIP_* names:  the pins the host wrote in
        `[daemon]` stand in for them, and an environment value still wins over the file."""
        self.write_config(
            extra_daemon='operator_user_id = %d\noperator_email = "%s"' % (OPERATOR_USER_ID, OPERATOR_EMAIL)
        )
        daemon = self.daemon(env=self.env(OPERATOR_ZULIP_USER_ID=None, OPERATOR_ZULIP_EMAIL=None))
        self.assertEqual(daemon.env["OPERATOR_ZULIP_USER_ID"], str(OPERATOR_USER_ID))
        self.assertEqual(daemon.env["OPERATOR_ZULIP_EMAIL"], OPERATOR_EMAIL)
        self.assertEqual(OPV.pinned_user_id(daemon.env), OPERATOR_USER_ID)
        winner = self.daemon(env=self.env(OPERATOR_ZULIP_USER_ID="999"))
        self.assertEqual(winner.env["OPERATOR_ZULIP_USER_ID"], "999", "the environment wins")

    def test_a_reload_applies_pins_that_came_from_listener_toml_and_never_replaces_the_environment(
        self,
    ) -> None:
        pins = 'operator_user_id = %d\noperator_email = "%s"'
        self.write_config(extra_daemon=pins % (999, "other-bot@zulip.test"))
        daemon = self.daemon(env=self.env(OPERATOR_ZULIP_USER_ID=None, OPERATOR_ZULIP_EMAIL=None))
        self.assertEqual(daemon.env["OPERATOR_ZULIP_USER_ID"], "999")
        self.write_config(extra_daemon=pins % (OPERATOR_USER_ID, OPERATOR_EMAIL))
        daemon.reload()
        self.assertEqual(daemon.env["OPERATOR_ZULIP_USER_ID"], str(OPERATOR_USER_ID))
        self.assertEqual(daemon.env["OPERATOR_ZULIP_EMAIL"], OPERATOR_EMAIL)
        self.assertEqual(
            OPV.pinned_user_id(daemon.operator_voice._env), OPERATOR_USER_ID, "the voice was told"
        )
        self.write_config()  # the pins are gone from the file
        daemon.reload()
        self.assertNotIn("OPERATOR_ZULIP_USER_ID", daemon.env)
        self.assertNotIn("OPERATOR_ZULIP_EMAIL", daemon.env)
        # A name the host's environment set is the host's, whatever the file says.
        winner = self.daemon(env=self.env(OPERATOR_ZULIP_USER_ID="999"))
        self.write_config(extra_daemon="operator_user_id = 5")
        winner.reload()
        self.assertEqual(winner.env["OPERATOR_ZULIP_USER_ID"], "999")

    def test_the_status_says_the_operator_cannot_speak_and_since_when(self) -> None:
        self.operator_available = False
        daemon = self.started()
        self.peer_wake(daemon)
        voice = daemon.status()["operator_voice"]
        self.assertEqual((voice["available"], voice["code"]), (False, "operator_unavailable"))
        self.assertRegex(voice["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("fleet-secrets", json.dumps(voice))
        self.operator_available = True
        self.clock.advance(OPV.RETRY_SECONDS + 1)
        self.peer_wake(daemon, "d")
        voice = daemon.status()["operator_voice"]
        self.assertEqual((voice["available"], voice["code"]), (True, None))

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
        self.assertEqual(self.final_row()["state"], "done", "and the wakes run again when it is back")
        self.assertEqual(self.operator_posts(), [], "though it posts nothing for them")
        self.peer_wake(daemon, "e")
        self.assertEqual(self.operator_builds, 2, "one client serves every later check")

    def test_a_rejected_operator_key_fails_the_owner_note_and_the_next_one_builds_a_new_client(self) -> None:
        daemon = self.started()
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "No.", "board": None, "risk": "high",
                                           "owner_note": "asked for a key"})
        self.fake.inject(
            "POST", "messages", status=401, body={"result": "error", "msg": "Invalid API key", "code": "UNAUTHORIZED"}
        )
        self.peer_wake(daemon, "a")
        self.assertEqual(self.operator_posts(), [])
        row = self.final_row()
        self.assertIsNone(row["owner_dm"])
        self.assertTrue(row["owner_dm_error"], "the failed note is recorded on the row")
        self.assertEqual(self.operator_builds, 1)
        self.peer_wake(daemon, "b")
        self.assertEqual(self.operator_builds, 2, "the rejected client was dropped")
        self.assertEqual(len(self.operator_posts()), 1, "and the next owner note went out")
        self.assertEqual(self.seat_requests(), [])

    def test_an_operator_lost_after_the_preflight_is_a_logged_refusal_and_never_a_seat_post(self) -> None:
        """The preflight passed, then the Operator went away during the run: the owner note is refused
        and logged, the row says why, and the seat's bot is not tried.  The reply was never going out."""
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
        self.assertEqual((row["state"], row["post_error"]), ("done", "operator_never_answers"))
        self.assertIn("operator unavailable", row["owner_dm_error"])
        self.assertEqual([r["site"] for r in self.log_events("operator-post-refused")], ["owner-dm"])
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_the_choke_point_scans_the_text_before_it_leaves(self) -> None:
        daemon = self.started()
        daemon.operator_voice.client()
        with self.assertRaises(OPV.OperatorRefused):
            daemon._operator_send(
                {"type": "stream", "to": "simple-tools", "topic": "t", "content": "key %s" % self.operator_key}
            )
        self.assertEqual(self.operator_posts(), [])


class SiteTests(OperatorHarness):
    """Each place the daemon speaks."""

    def escalate(self, daemon: D.Daemon, risk: str = "high") -> int:
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "I can't share that.", "board": None,
                                           "risk": risk, "owner_note": "Codex asked for the key."})
        return self.peer_wake(daemon)

    def test_the_escalation_note_is_a_dm_from_the_operator_and_names_the_seat_in_its_body(self) -> None:
        daemon = self.started()
        mid = self.escalate(daemon)
        notes = [m for m in self.operator_posts() if m["type"] == "private"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(sorted(r["id"] for r in notes[0]["display_recipient"]), [OWNER, OPERATOR_USER_ID])
        self.assertTrue(notes[0]["content"].startswith("[OPERATOR·note] re=%d\n" % mid), notes[0]["content"])
        self.assertIn("\nSeat: CLAUDE\n", notes[0]["content"], "the tag names the Operator, so the body names the seat")
        self.assertEqual(self.final_row()["owner_dm"], notes[0]["id"])
        self.assertEqual(self.seat_requests(), [])

    def test_there_is_no_quota_notice_any_more(self) -> None:
        # The notice was the Operator telling a sender that a seat cannot answer.  The behaviour is
        # pinned in test_daemon (a quota hold sends nobody a DM);  this is the code that went with it.
        self.assertFalse(hasattr(D.Daemon, "_tell_requester"))
        self.assertFalse(hasattr(D, "QUOTA_HOLD_TELL_WINDOW"))

    TYPING_SEAT = '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\ntyping_indicator = true\n'

    def test_no_typing_indicator_is_raised_for_a_channel_wake_whatever_the_seat_says(self) -> None:
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.peer_wake(daemon)
        self.assertEqual(self.fake.requests_to("POST", "typing"), [])
        self.assertEqual(self.fake.typing, [])
        (event,) = self.log_events("typing-skipped")
        self.assertEqual(event["code"], "operator_never_answers")
        self.assertEqual(self.final_row()["state"], "done")

    def test_no_typing_indicator_is_raised_for_a_dm_wake_either(self) -> None:
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.fake.add_direct_message("Jay Wedgeworth", "status of the cutover?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        self.assertEqual(self.fake.requests_to("POST", "typing"), [])
        self.assertEqual(self.fake.typing, [])

    def test_a_seat_with_the_indicator_off_logs_nothing_about_it(self) -> None:
        daemon = self.started()
        self.peer_wake(daemon)
        self.assertEqual(self.log_events("typing-skipped"), [])
        self.assertEqual(self.fake.typing, [])

    def test_no_indicator_at_all_when_there_is_no_operator(self) -> None:
        self.operator_available = False
        self.write_config(seats=self.TYPING_SEAT)
        daemon = self.started()
        self.peer_wake(daemon)
        self.assertEqual(self.fake.typing, [])
        self.assertEqual(self.dumps(), [], "and the wake itself was not run")


class DmModeChokePointTests(OperatorHarness):
    """`SIMPLE_TOOLS_OPERATOR_DM_MODE=clarify` is enforced at the daemon's two choke points before they spend anything.

    `_operator_send` and `_operator_edit` used to build the Operator client (a vault read) and wait out
    the pacing before `OperatorVoice.post` refused, so a refused direct message still cost both.  Each
    now asks the voice first.  A spy on `client()` and on `_operator_pace_ready` says nothing was spent,
    and in `legacy` mode both choke points behave exactly as before.
    """

    MODE = "SIMPLE_TOOLS_OPERATOR_DM_MODE"
    DM = {"type": "direct", "to": [OWNER], "content": "a note for the owner"}
    STREAM = {"type": "stream", "to": "simple-tools", "topic": "t", "content": "hello"}

    def mode(self, daemon: D.Daemon, value: str | None) -> None:
        """Set the DM mode the way a reload does: in the daemon's environment, then told to the voice."""
        if value is None:
            daemon.env.pop(self.MODE, None)
        else:
            daemon.env[self.MODE] = value
        daemon.operator_voice.update_env(daemon.env)

    def spy(self, daemon: D.Daemon) -> dict[str, int]:
        """Count the client builds asked for and the pacing waits taken from here on."""
        spent = {"client": 0, "pace": 0}
        real_client, real_pace = daemon.operator_voice.client, daemon._operator_pace_ready

        def client(*args: Any, **kwargs: Any) -> Any:
            spent["client"] += 1
            return real_client(*args, **kwargs)

        def pace() -> None:
            spent["pace"] += 1
            real_pace()

        daemon.operator_voice.client = client
        daemon._operator_pace_ready = pace
        return spent

    def content_of(self, message_id: int) -> str:
        return next(m for m in self.operator_posts() if m["id"] == message_id)["content"]

    def test_a_refused_dm_costs_no_client_build_and_no_pacing_at_the_send_choke_point(self) -> None:
        daemon = self.started()
        self.mode(daemon, "clarify")
        spent = self.spy(daemon)
        builds = self.operator_builds
        for wait in (True, False):
            with self.subTest(wait=wait), self.assertRaises(OPV.OperatorRefused) as caught:
                daemon._operator_send(self.DM, wait=wait)
            self.assertEqual(caught.exception.code, OPV.CODE_DM_CLARIFY_ONLY)
        self.assertEqual(spent, {"client": 0, "pace": 0}, "the refusal came first")
        self.assertEqual(self.operator_builds, builds, "the vault was not read")
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_the_refusal_comes_before_the_secret_scan(self) -> None:
        daemon = self.started()
        daemon.operator_voice.client()  # the key is hidden from here on, so the scanner would catch it
        self.mode(daemon, "clarify")
        text = "key %s" % self.operator_key
        with self.assertRaises(OPV.OperatorRefused) as caught:
            daemon._operator_send(dict(self.DM, content=text))
        self.assertEqual(caught.exception.code, OPV.CODE_DM_CLARIFY_ONLY, "not the scanner's code")
        with self.assertRaises(OPV.OperatorRefused) as scanned:
            daemon._operator_send(dict(self.STREAM, content=text))
        self.assertEqual(scanned.exception.code, OPV.CODE_SECRET, "control: the scanner does see it")

    def test_a_refused_edit_costs_no_client_build_and_no_pacing_at_the_edit_choke_point(self) -> None:
        daemon = self.started()
        sent = daemon._operator_send(self.DM)  # legacy:  it goes out, and the voice remembers its id
        self.mode(daemon, "clarify")
        spent = self.spy(daemon)
        builds = self.operator_builds
        for wait in (True, False):
            with self.subTest(wait=wait), self.assertRaises(OPV.OperatorRefused) as caught:
                daemon._operator_edit(sent["id"], "rewritten", wait=wait)
            self.assertEqual(caught.exception.code, OPV.CODE_DM_CLARIFY_ONLY)
        self.assertEqual(spent, {"client": 0, "pace": 0}, "the refusal came first")
        self.assertEqual(self.operator_builds, builds, "the vault was not read")
        self.assertEqual(self.content_of(sent["id"]), self.DM["content"], "the message was not rewritten")

    def test_stream_posts_and_stream_edits_are_unchanged_in_clarify(self) -> None:
        daemon = self.started()
        self.mode(daemon, "clarify")
        spent = self.spy(daemon)
        sent = daemon._operator_send(self.STREAM)
        daemon._operator_edit(sent["id"], "edited")
        self.assertEqual(spent["pace"], 2, "a post and an edit, each paced as before")
        self.assertGreaterEqual(spent["client"], 2)
        self.assertEqual(self.content_of(sent["id"]), "edited")

    def test_legacy_is_unchanged_at_both_choke_points(self) -> None:
        daemon = self.started()
        spent = self.spy(daemon)
        for value in (None, "legacy", "not a mode"):
            with self.subTest(value=value):
                self.mode(daemon, value)
                before = dict(spent)
                sent = daemon._operator_send(self.DM)
                daemon._operator_edit(sent["id"], "edited %s" % value)
                self.assertEqual(spent["pace"] - before["pace"], 2, "a post and an edit, each paced")
                self.assertGreaterEqual(spent["client"] - before["client"], 2)
                self.assertEqual(self.content_of(sent["id"]), "edited %s" % value)

    def test_the_two_choke_points_ask_the_voice_before_anything_else(self) -> None:
        """Source order, so a later edit cannot slide the check behind the scan, the build or the wait."""
        for method, check in (
            (D.Daemon._operator_send, "refuse_unclarified_dm"),
            (D.Daemon._operator_edit, "refuse_unclarified_edit"),
        ):
            body = inspect.getsource(method).split('"""')[2]  # after the docstring
            first = body.index(check)
            for later in ("secretscan.scan", ".client(", "_operator_pace_ready"):
                self.assertLess(first, body.index(later), (method.__name__, later))


class StructureTests(unittest.TestCase):
    """A tripwire in the source: the daemon no longer writes through a seat's client anywhere."""

    def test_no_write_goes_through_a_seat_client(self) -> None:
        writes = re.findall(r"\bclient\.(?:post|patch|put|delete|request)\(", DAEMON_SOURCE)
        self.assertEqual(writes, [], "a seat's client must not write in the daemon;  use _operator_send")

    def test_the_typing_indicator_is_never_raised_and_never_handed_a_seat_client(self) -> None:
        source = inspect.getsource(D.Daemon._typing_signal)
        body = source.split('"""')[2]  # after the docstring
        self.assertNotIn("TypingSignal(", body)
        self.assertNotIn("runner.client", body)
        self.assertNotIn("operator_voice", body)

    def test_the_old_alert_seat_picker_is_gone(self) -> None:
        self.assertNotIn("_alert_runner", DAEMON_SOURCE)


# --------------------------------------------------------------------------------------------
# The route page is the Operator's, and still reaches the seat it pages
# --------------------------------------------------------------------------------------------

ME = R.SeatIdentity("CODEX", CODEX, "Codex", "codex-bot@zulip.test")
ROUTE = "[OPERATOR·route→CODEX] re=500\n@**Codex|11** CLAUDE paged you on Jay's behalf: look at the build"


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
            "client": "ZulipPython", "type": "stream", "display_recipient": "simple-tools", "subject": "t",
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
            ("a wake relay", "[OPERATOR·wake] re=500\n@**Codex|11** hello"),
            ("a status message", "[OPERATOR·status] @**Codex|11** your wake ran"),
            ("a note", "[OPERATOR·note] re=500\n@**Codex|11** note"),
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
