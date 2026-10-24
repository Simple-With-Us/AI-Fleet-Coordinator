"""The seat's typing indicator while it thinks (AFC-515).

A wake run is silent for its whole length:  the adapter takes 30 to 50 seconds to answer and
the reply is posted only after it has, so the person who woke the seat watches an empty topic
the whole time.  The fleet shipped a typing op long ago and nothing called it.

These tests cover the two halves.  The first is the signal itself:  which conversation a wake
types in, that the indicator is refreshed while the seat thinks (Zulip expires it when no new
"start" arrives), that it is always stopped, that a failure to post it is swallowed rather
than allowed to fail the run, that the caller is never held up by a slow endpoint, and that a
transient error does not take the indicator down for the rest of the run.  The second is the
daemon:  a seat with the switch on types through its wake, a seat with it off says nothing,
and the indicator stays up for the fallback that answers when the primary cannot.
"""
from __future__ import annotations

import time
import unittest

from agent_sync import typing_signal as TYPING
from agent_sync.tests.test_daemon import DaemonHarness

MENTION = "@**Claude** can you check the AFC cutover?"


class FakeClient:
    """The seat's Zulip client, reduced to the one call the signal makes."""

    def __init__(
        self,
        *,
        fail_always: bool = False,
        fail_first: int = 0,
        delay: float = 0.0,
    ) -> None:
        self.posts: list[dict[str, object]] = []
        self.ops: list[str] = []
        self.calls = 0
        self.timeouts: list[float] = []
        self.fail_always = fail_always
        self.fail_first = fail_first
        self.delay = delay

    def post(self, path: str, params: dict[str, object], **kw: object) -> dict[str, str]:
        self.calls += 1
        if "timeout" in kw:
            self.timeouts.append(float(kw["timeout"]))  # type: ignore[arg-type]
        if self.delay:
            time.sleep(self.delay)
        self.ops.append(str(params.get("op")))
        if self.fail_always or self.calls <= self.fail_first:
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

    def wait_for(self, predicate, timeout: float = 4.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.02)

    def test_start_posts_start_and_stop_posts_stop(self) -> None:
        client = FakeClient()
        signal = TYPING.TypingSignal(client, {"type": "stream", "stream_id": 101, "topic": "t"})
        self.assertTrue(signal.start())
        signal.stop()
        self.wait_for(lambda: "stop" in client.ops)
        self.assertEqual(client.ops, ["start", "stop"])
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
        self.wait_for(lambda: len(client.posts) >= 2)
        signal.stop()
        self.wait_for(lambda: "stop" in client.ops)
        self.assertGreaterEqual(len(client.posts), 2, "the indicator was refreshed")
        self.assertEqual(client.posts[-1]["op"], "stop")

    def test_the_caller_is_never_held_up_by_a_slow_endpoint(self) -> None:
        # The indicator is cosmetic, so a stalled typing endpoint must not delay the answer.
        # The client's own default is thirty seconds; the signal posts on its own thread with
        # a short timeout of its own instead.
        client = FakeClient(delay=1.0)
        signal = TYPING.TypingSignal(client, {"type": "stream", "stream_id": 101, "topic": "t"})
        began = time.monotonic()
        self.assertTrue(signal.start())
        elapsed = time.monotonic() - began
        signal.stop()
        self.assertLess(elapsed, 0.5, "the caller waited for the typing post")
        self.assertTrue(client.timeouts, "the post carried its own timeout")
        self.assertLessEqual(max(client.timeouts), 10.0)

    def test_a_failed_post_is_reported_and_the_refresh_carries_on(self) -> None:
        errors: list[Exception] = []
        client = FakeClient(fail_first=1)
        signal = TYPING.TypingSignal(
            client,
            {"type": "stream", "stream_id": 101, "topic": "t"},
            refresh=0.5,
            on_error=errors.append,
        )
        self.assertTrue(signal.start())
        self.wait_for(lambda: bool(client.posts))
        signal.stop()
        self.assertTrue(errors, "the failure was reported")
        self.assertTrue(client.posts, "the indicator came up after the failure")
        self.assertEqual(client.posts[0]["op"], "start")

    def test_a_transient_error_does_not_end_the_refresh(self) -> None:
        # One blip is not a dead endpoint:  the indicator must survive it, because the run
        # lasts far longer than the server's expiry and a dead refresh leaves the topic silent.
        client = FakeClient(fail_first=1)
        signal = TYPING.TypingSignal(
            client, {"type": "stream", "stream_id": 101, "topic": "t"}, refresh=0.5
        )
        signal.start()
        self.wait_for(lambda: len(client.posts) >= 2)
        signal.stop()
        self.assertGreaterEqual(
            len(client.posts), 2, "the refresh survived the transient error"
        )
        self.assertEqual([post["op"] for post in client.posts[:2]], ["start", "start"])

    def test_a_dead_endpoint_ends_the_refresh_instead_of_hammering_it(self) -> None:
        client = FakeClient(fail_always=True)
        signal = TYPING.TypingSignal(
            client, {"type": "stream", "stream_id": 101, "topic": "t"}, refresh=0.5
        )
        signal.start()
        self.wait_for(lambda: client.calls >= 5)
        signal.stop()
        self.assertGreaterEqual(client.calls, 5, "the endpoint was tried again")
        self.assertLessEqual(client.calls, 7, "and then the refresh gave up")

    def test_a_broken_error_hook_never_raises(self) -> None:
        def boom(exc: Exception) -> None:
            raise ValueError("the hook is broken")

        client = FakeClient(fail_always=True)
        signal = TYPING.TypingSignal(
            client, {"type": "stream", "stream_id": 101, "topic": "t"}, on_error=boom
        )
        self.assertTrue(signal.start())
        self.wait_for(lambda: client.calls >= 1)
        signal.stop()


class DaemonTypingTests(DaemonHarness):
    """The daemon:  a seat with the switch on types through its wake, one with it off says
    nothing, and the indicator stays up for the whole of the answer."""

    def typing_posts(self) -> list[dict[str, str]]:
        return list(self.fake.typing)

    def wait_for_typing(self, count: int = 1, timeout: float = 4.0) -> list[dict[str, str]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.fake.typing) < count:
            time.sleep(0.02)
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
        posts = self.wait_for_typing(2)
        ops = [post["op"] for post in posts]
        self.assertEqual(ops, ["start", "stop"], ops)
        first = posts[0]
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

    def test_the_indicator_stays_up_while_the_fallback_answers(self) -> None:
        # The fallback is part of making the answer:  the seat is still thinking while it runs,
        # so the indicator must not go down before it.
        self.write_config(
            seats=(
                '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\n'
                "typing_indicator = true\n"
            )
        )
        daemon = self.started()
        seen: dict[str, object] = {}
        original_signal = daemon._typing_signal
        original_fallback = daemon.run_fallback
        original_run = daemon.claude_runner.run

        def capture_signal(runner, pending):
            signal = original_signal(runner, pending)
            seen["signal"] = signal
            return signal

        def watch_fallback(*args, **kwargs):
            signal = seen.get("signal")
            seen["live_at_fallback"] = bool(signal is not None and signal.live)
            seen["ran"] = True
            return original_fallback(*args, **kwargs)

        def failing_run(*args, **kwargs):
            # A fault another runtime could answer differently, so the fallback is tried.
            result = original_run(*args, **kwargs)
            result.error = "no parseable result"
            result.result = None
            return result

        daemon._typing_signal = capture_signal
        daemon.run_fallback = watch_fallback
        daemon.claude_runner.run = failing_run
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(19)
        daemon.tick()
        self.wake_cycle(daemon, 2)
        self.assertTrue(seen.get("ran"), "the fallback was tried")
        self.assertTrue(
            seen.get("live_at_fallback"),
            "the indicator was down while the fallback was answering",
        )


if __name__ == "__main__":
    unittest.main()
