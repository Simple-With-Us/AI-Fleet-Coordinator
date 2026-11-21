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
