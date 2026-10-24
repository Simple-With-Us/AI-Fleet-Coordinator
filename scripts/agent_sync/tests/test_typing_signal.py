"""The seat's typing indicator while it thinks (AFC-515).

A wake run is silent for its whole length:  the adapter takes 30 to 50 seconds to answer and
the reply is posted only after it has, so the person who woke the seat watches an empty topic
the whole time.  The fleet shipped a typing op long ago and nothing called it.

These tests cover the two halves.  The first is the signal itself:  which conversation a wake
types in, that the indicator is refreshed while the seat thinks (Zulip expires it when no new
"start" arrives), that it is always stopped, and that a failure to post it is swallowed rather
than allowed to fail the run.  The second is the daemon:  a seat with the switch on types
through its wake and a seat with it off says nothing.
"""
from __future__ import annotations

import time
import unittest

from agent_sync import typing_signal as TYPING
from agent_sync.tests.test_daemon import DaemonHarness

MENTION = "@**Claude** can you check the AFC cutover?"


class FakeClient:
    """The seat's Zulip client, reduced to the one call the signal makes."""

    def __init__(self, fail: bool = False) -> None:
        self.posts: list[dict[str, object]] = []
        self.fail = fail

    def post(self, path: str, params: dict[str, object]) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("the realm refused the typing notification")
        self.posts.append(dict(params))
        return {"result": "success"}


class Pending:
    """The wake's conversation, as the daemon hands it over."""

    def __init__(self, kind: str = "stream", *, channel: str = "agent-sync", topic: str = "t",
                 recipients: list[int] | None = None, stream_id: int | None = 101) -> None:
        self.type = kind
        self.channel = channel
        self.topic = topic
        self.recipients = recipients or []
        self.stream_id = stream_id


class TypingTargetTests(unittest.TestCase):
    """Which conversation a wake types in."""

    def test_a_stream_wake_types_in_its_channel_and_topic(self) -> None:
        params = TYPING.target(Pending(), 10)
        self.assertEqual(params, {"type": "stream", "stream_id": 101, "topic": "t"})

    def test_a_private_wake_types_to_its_partners_and_not_to_itself(self) -> None:
        pending = Pending("private", recipients=[10, 12, 34])
        self.assertEqual(TYPING.target(pending, 10), {"type": "direct", "to": [12, 34]})

    def test_a_private_wake_with_nobody_else_has_no_target(self) -> None:
        self.assertIsNone(TYPING.target(Pending("private", recipients=[10]), 10))

    def test_a_private_wake_with_more_partners_than_zulip_accepts_has_no_target(self) -> None:
        pending = Pending("private", recipients=[10] + list(range(20, 29)))
        self.assertIsNone(TYPING.target(pending, 10))

    def test_a_stream_wake_with_no_channel_or_no_topic_has_no_target(self) -> None:
        self.assertIsNone(TYPING.target(Pending(stream_id=None), 10))
        self.assertIsNone(TYPING.target(Pending(topic=""), 10))


class TypingSignalTests(unittest.TestCase):
    """The indicator itself."""

    def test_start_posts_start_and_stop_posts_stop(self) -> None:
        client = FakeClient()
        signal = TYPING.TypingSignal(client, {"type": "stream", "stream_id": 101, "topic": "t"})
        self.assertTrue(signal.start())
        signal.stop()
        ops = [post["op"] for post in client.posts]
        self.assertEqual(ops, ["start", "stop"])
        self.assertEqual(client.posts[0]["stream_id"], 101)
        self.assertEqual(client.posts[0]["topic"], "t")

    def test_the_indicator_is_refreshed_while_the_seat_thinks(self) -> None:
        # Zulip expires the indicator when no new "start" arrives, and a wake run is far longer
        # than that, so one notification is not enough.
        client = FakeClient()
        signal = TYPING.TypingSignal(
            client, {"type": "stream", "stream_id": 101, "topic": "t"}, refresh=0.5
        )
        signal.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(client.posts) < 2:
            time.sleep(0.05)
        signal.stop()
        self.assertGreaterEqual(len(client.posts), 2, "the indicator was refreshed")
        self.assertEqual(client.posts[-1]["op"], "stop")

    def test_a_failed_first_post_never_raises_and_leaves_nothing_up(self) -> None:
        errors: list[Exception] = []
        client = FakeClient(fail=True)
        signal = TYPING.TypingSignal(
            client,
            {"type": "stream", "stream_id": 101, "topic": "t"},
            on_error=errors.append,
        )
        self.assertFalse(signal.start())
        signal.stop()
        self.assertEqual(len(errors), 1)
        self.assertEqual(client.posts, [])

    def test_a_broken_error_hook_never_raises(self) -> None:
        def boom(exc: Exception) -> None:
            raise ValueError("the hook is broken")

        client = FakeClient(fail=True)
        signal = TYPING.TypingSignal(
            client, {"type": "stream", "stream_id": 101, "topic": "t"}, on_error=boom
        )
        self.assertFalse(signal.start())


class DaemonTypingTests(DaemonHarness):
    """The daemon:  a seat with the switch on types through its wake, and one with it off
    says nothing."""

    def typing_posts(self) -> list[dict[str, str]]:
        return list(self.fake.typing)

    def test_a_seat_with_the_indicator_on_types_while_it_thinks(self) -> None:
        self.write_config(
            seats=(
                '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\n'
                "typing_indicator = true\n"
            )
        )
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(19)
        daemon.tick()
        self.wake_cycle(daemon, 2)
        ops = [post["op"] for post in self.typing_posts()]
        self.assertEqual(ops, ["start", "stop"], ops)
        first = self.typing_posts()[0]
        self.assertEqual(first["type"], "stream")
        self.assertEqual(first["topic"], "t")
        self.assertEqual(first["stream_id"], str(self.fake.streams["agent-sync"]))
        self.assertEqual([r["state"] for r in self.ledger()][-1], "done")

    def test_a_seat_with_the_indicator_off_says_nothing(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(19)
        daemon.tick()
        self.wake_cycle(daemon, 2)
        self.assertEqual(self.typing_posts(), [])
        self.assertEqual([r["state"] for r in self.ledger()][-1], "done")


if __name__ == "__main__":
    unittest.main()
