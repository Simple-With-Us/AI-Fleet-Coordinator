"""All ownership consumers honor the bounded session receipt fence."""
import contextlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simple_tools import cli
from simple_tools.state import PostReceiptPending, State
from simple_tools.mcp.tools import Tools


class ReceiptContractTests(unittest.TestCase):
    def test_shared_agent_and_mcp_checks_wait_for_delayed_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = object.__new__(cli.Agent)
            agent.state = State(Path(tmp), "CLAUDE", "session")
            agent.creds = SimpleNamespace(email="fixture@example.invalid")
            agent.tag = None
            message = {"id": 42, "sender_email": agent.creds.email}
            tools = SimpleNamespace(posted_ids=set(), agent=agent)
            entered = threading.Event()
            def post():
                with agent.state.stream_post_guard():
                    entered.set()
                    time.sleep(0.08)
                    agent.state.record_posted(42)
            for check in (agent.is_self, lambda m: Tools._generic_is_self(tools, m)):
                agent.state.path.unlink(missing_ok=True)
                entered.clear()
                thread = threading.Thread(target=post)
                thread.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(check(message))
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertFalse(check({"id": 43, "sender_email": agent.creds.email}))

    def test_timeout_does_not_guess_sibling_or_advance_delivery_cursor(self):
        agent = object.__new__(cli.Agent)
        agent.creds = SimpleNamespace(email="fixture@example.invalid")
        agent.tag = None
        agent.state = mock.Mock()
        agent.state.cursor.return_value = None
        @contextlib.contextmanager
        def stalled():
            raise PostReceiptPending("fixture")
            yield
        agent.state.stream_post_guard = stalled
        delivery = cli.Delivery(agent)
        message = {"id": 42, "sender_email": agent.creds.email}
        with self.assertRaises(cli.UsageError):
            delivery.deliver([message], [], assume=cli.Scope("channel", "topic"))
        agent.state.advance_cursor.assert_not_called()

    def test_retry_retains_same_batch_and_does_not_repeat_partial_delivery(self):
        agent = object.__new__(cli.Agent)
        with tempfile.TemporaryDirectory() as tmp:
            agent.state = State(Path(tmp), "CLAUDE", "session")
            agent.creds = SimpleNamespace(email="fixture@example.invalid")
            agent.tag = None
            shown = []
            agent.emit = lambda message, **kwargs: shown.append(message["id"])
            original = agent.is_self
            attempts = 0
            def ownership(message, **kwargs):
                nonlocal attempts
                if message["id"] == 42:
                    attempts += 1
                    if attempts == 1:
                        raise cli.ReceiptDeferred("pending fixture")
                    agent.state.record_posted(42)
                return original(message, **kwargs)
            agent.is_self = ownership
            scope = cli.Scope("channel", "topic")
            batch = [{"id": n, "sender_email": email} for n, email in [
                (41, "peer@example.invalid"), (42, agent.creds.email), (43, "peer@example.invalid")]]
            delivery = cli.Delivery(agent)
            result = cli.deliver_with_receipt_retry(delivery, batch, [], assume=scope)
            self.assertEqual(result, 2)
            self.assertEqual(shown, [41, 43])
            self.assertEqual(agent.state.cursor(*scope.cursor_args), 43)
            self.assertEqual(attempts, 2)

    def test_pending_retry_obeys_stop_and_wait_deadline_without_classifying(self):
        delivery = mock.Mock()
        delivery.count = 0
        delivery.deliver.side_effect = cli.ReceiptDeferred("pending fixture")
        scope = cli.Scope("channel", "topic")
        batch = [{"id": 42}]
        before = time.monotonic()
        self.assertEqual(cli.deliver_with_receipt_retry(delivery, batch, [scope], deadline=before + 0.03), 0)
        self.assertLess(time.monotonic() - before, 0.2)
        stop = threading.Event()
        stop.set()
        calls = delivery.deliver.call_count
        self.assertEqual(cli.deliver_with_receipt_retry(delivery, batch, [scope], stop=stop), 0)
        self.assertEqual(delivery.deliver.call_count, calls)

    def test_actual_held_receipt_fence_respects_wait_deadline_and_stop(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as tmp:
                state = State(Path(tmp), "CLAUDE", "session")
                agent = object.__new__(cli.Agent)
                agent.state = state
                agent.creds = SimpleNamespace(email="fixture@example.invalid")
                agent.tag = None
                agent.emit = mock.Mock()
                scope = cli.Scope("channel", "topic")
                state.advance_cursor(*scope.cursor_args, 41)
                entered, release, stop = threading.Event(), threading.Event(), threading.Event()
                def hold():
                    with state.stream_post_guard():
                        entered.set()
                        release.wait(2)
                thread = threading.Thread(target=hold)
                thread.start()
                self.assertTrue(entered.wait(1))
                timer = threading.Timer(0.03, stop.set) if cancel else None
                if timer:
                    timer.start()
                before = time.monotonic()
                try:
                    delivery = cli.Delivery(agent)
                    self.assertEqual(cli.deliver_with_receipt_retry(delivery,
                        [{"id": 42, "sender_email": agent.creds.email}], [], assume=scope,
                        stop=stop, deadline=None if cancel else before + 0.03), 0)
                    self.assertLess(time.monotonic() - before, 0.3)
                    self.assertEqual(state.cursor(*scope.cursor_args), 41)
                    agent.emit.assert_not_called()
                finally:
                    release.set()
                    thread.join(1)
                    if timer:
                        timer.join(1)

    def test_wait_stops_without_poll_spin_when_stop_is_already_set(self):
        stop = threading.Event()
        stop.set()
        agent = mock.Mock()
        agent.rt.stop = stop
        args = SimpleNamespace(channel="channel", topic="topic", timeout=10)
        with mock.patch.object(cli, "EventQueue") as queue:
            self.assertEqual(cli.cmd_wait(agent, args), 0)
            queue.return_value.poll.assert_not_called()

    def test_real_human_and_json_emit_carries_confirmed_ownership_without_reentry(self):
        for json_output in (False, True):
            with self.subTest(json_output=json_output), tempfile.TemporaryDirectory() as tmp:
                agent = object.__new__(cli.Agent)
                agent.state = State(Path(tmp), "CLAUDE", "session")
                agent.creds = SimpleNamespace(email="fixture@example.invalid")
                agent.tag = None
                agent.json = json_output
                agent.rt = SimpleNamespace(env={})
                output = []
                agent.out = output.append
                agent.emit_json = output.append
                agent.sender_is_bot = lambda m: True
                agent.sender_seat = lambda m, b: ("CLAUDE", "fixture")
                scope = cli.Scope("channel", "topic")
                message = {"id": 42, "sender_email": agent.creds.email, "content": "sibling",
                    "type": "stream", "display_recipient": "channel", "subject": "topic"}
                original = agent.is_self
                checks = []
                entered, release = threading.Event(), threading.Event()
                def hold_after_classification():
                    with agent.state.stream_post_guard():
                        entered.set()
                        release.wait(1)
                thread = threading.Thread(target=hold_after_classification)
                def ownership(message, **kwargs):
                    checks.append(kwargs)
                    result = original(message, **kwargs)
                    if len(checks) == 1:
                        thread.start()
                        self.assertTrue(entered.wait(1))
                    return result
                agent.is_self = ownership
                timer = threading.Timer(0.45, release.set)
                timer.start()
                before = time.monotonic()
                try:
                    self.assertEqual(cli.deliver_with_receipt_retry(cli.Delivery(agent), [message], [],
                        assume=scope, deadline=before + 0.03), 1)
                    self.assertLess(time.monotonic() - before, 0.3)
                finally:
                    release.set()
                    thread.join(1)
                    timer.cancel()
                    timer.join(1)
                self.assertEqual(len(checks), 1)
                self.assertEqual(len(output), 1)
                if json_output:
                    self.assertIs(output[0]["sibling"], True)
                else:
                    self.assertIn("(sibling)", output[0])
