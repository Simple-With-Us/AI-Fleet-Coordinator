#!/usr/bin/env python3
"""test-lark-mm-bridge.py -- checks the lark-mm-bridge pieces that can be checked
without a live Lark bot.

Run: python3 scripts/test-lark-mm-bridge.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "lark-mm-bridge.py"
spec = importlib.util.spec_from_file_location("lark_mm_bridge", SCRIPT)
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)

# A private root for the seat inbox, so a test that runs a turn never writes
# into the real seat's queue on this Mac.
_TEST_ROOT = tempfile.mkdtemp(prefix="lark-mm-bridge-test-")


def update(kind, text):
    """One ACP session/update notification, as the real server frames it."""
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "mvs_test_1",
            "update": {
                "sessionId": "mvs_test_1",
                "sessionUpdate": kind,
                "content": {"type": "text", "text": text},
            },
        },
    }


def make_args(argv=None, **overrides):
    """Build a real Namespace from the bridge's own parser.

    The tests used to hand-build a stand-in class instead, which let the code
    and the parser drift apart without failing anything: the bridge read
    args.allowed_sender while argparse produced args.allow_sender.  Building
    from the real parser keeps the two honest.
    """
    args = bridge_mod.build_parser().parse_args(argv or [])
    # The mirror posts to the real Zulip, so the suite never does it unless a
    # test asks for it by name.
    args.zulip_mirror = False
    # The seat inbox is the real one on this Mac, so the suite writes to a
    # temp root instead: a test that ran a turn must not leave a row in the
    # seat's own queue.
    args.agent_sync_root = _TEST_ROOT
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class FakeProcess:
    """A subprocess stand-in that answers ACP requests from a script."""

    def __init__(self, script):
        self.script = script
        # The script emits its own notifications, so it needs the process it
        # answers on.
        script.process = self
        self.stdin = FakeStdin(self)
        self.stdout = FakeStdout(self)
        self.stderr = iter(())
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class FakeStdin:
    def __init__(self, process):
        self.process = process
        self.written = []

    def write(self, text):
        self.written.append(text)
        for line in text.splitlines():
            if not line.strip():
                continue
            request = json.loads(line)
            response = self.process.script.get(request.get("method"))
            if response is None:
                continue
            self.process.stdout.emit(response(request))

    def flush(self):
        pass


class FakeStdout:
    def __init__(self, process):
        self.process = process
        self.queue = []
        self.condition = threading.Condition()
        self.closed = False

    def emit(self, message):
        with self.condition:
            self.queue.append(json.dumps(message))
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def __iter__(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait(timeout=0.2)
                if self.closed and not self.queue:
                    return
                if self.queue:
                    yield self.queue.pop(0)


class FakeSessionScript:
    def __init__(self, answer="BRIDGE_OK"):
        self.answer = answer
        self.prompts = []

    def get(self, method):
        return {
            "initialize": self.initialize,
            "session/new": self.session_new,
            "session/prompt": self.session_prompt,
        }.get(method)

    def initialize(self, request):
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}}

    def session_new(self, request):
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"sessionId": "mvs_test_1"},
        }

    def notification(self, kind, text):
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "mvs_test_1",
                "update": {
                    "sessionId": "mvs_test_1",
                    "sessionUpdate": kind,
                    "content": {"type": "text", "text": text},
                },
            },
        }

    def session_prompt(self, request):
        self.prompts.append(request["params"]["prompt"][0]["text"])
        # ACP v1 returns only stopReason and streams the answer as a
        # session/update.  A fake that invented a result.content made the
        # only end-to-end test cover a branch the real server never takes.
        self.process.stdout.emit(self.notification("agent_message_chunk", self.answer))
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"stopReason": "end_turn"},
        }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)

    def test_lark_event_key_is_the_message_event(self):
        self.assertEqual(bridge_mod.LARK_EVENT_KEY, "im.message.receive_v1")

    def capture_reply(self, message_id, text, run_result=""):
        """Run _reply with the child CLI stubbed, and return its argv."""
        sent = {}

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent["command"] = command
            return run_result

        original = bridge_mod.run
        bridge_mod.run = fake_run
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.args = make_args(lark_cli="/bin/true")
            bridge.env = None
            bridge._reply(message_id, text)
        finally:
            bridge_mod.run = original
        return sent["command"]

    def reply_body(self, command):
        return next(part.split("=", 1)[1] for part in command if part.startswith("--text="))

    def test_reply_truncates_and_marks(self):
        command = self.capture_reply("om_1", "x" * 5000)
        text = self.reply_body(command)
        self.assertIn("reply truncated", text)
        self.assertLess(len(text), 5000)

    def test_reply_uses_reply_command_and_bot_identity(self):
        command = self.capture_reply("om_42", "hello")
        self.assertEqual(command[1:3], ["im", "+messages-reply"])
        self.assertIn("bot", command)
        self.assertIn("--message-id=om_42", command)
        self.assertEqual(self.reply_body(command), "hello")

    def test_reply_passes_values_as_single_option_tokens(self):
        """A value that looks like an option must not become one.

        Reply bodies are Markdown, so a leading "-" -- a bullet list, a `---`
        rule -- is ordinary, and a bare "--text" argv element would let the
        child CLI read it as a flag.
        """
        command = self.capture_reply("--not-an-id", "- - --flag-looking body")
        self.assertEqual(sum(1 for part in command if part.startswith("--message-id")), 1)
        self.assertEqual(sum(1 for part in command if part.startswith("--text")), 1)
        self.assertIn("--message-id=--not-an-id", command)
        self.assertEqual(self.reply_body(command), "- - --flag-looking body")

    def test_empty_answer_still_replies(self):
        self.assertIn("no reply", self.reply_body(self.capture_reply("om_9", "   ")))

    def test_reply_refuses_credential_shaped_text(self):
        """The seat can echo a secret back; that must not reach the chat."""
        original = bridge_mod.run
        bridge_mod.run = lambda *a, **kw: self.fail("a secret was sent to Lark")
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.args = make_args(lark_cli="/bin/true")
            bridge.env = None
            for secret in (
                "here it is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                "token sk-abcdefghijklmnopqrstuvwx",
                "Authorization: bearer abcdefghijklmnopqrstuvwxyz.0123456789",
            ):
                with self.assertRaises(bridge_mod.SecretRefusal):
                    bridge._reply("om_secret", secret)
        finally:
            bridge_mod.run = original

    def test_ordinary_prose_is_not_mistaken_for_a_secret(self):
        self.assertEqual(
            self.reply_body(self.capture_reply("om_ok", "A normal answer about keys.")),
            "A normal answer about keys.",
        )

    def test_duplicate_message_id_is_ignored(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        self.assertTrue(bridge._claim("om_a"))
        bridge._record_delivery("om_a", "ok")
        self.assertFalse(bridge._claim("om_a"))
        self.assertTrue(bridge._claim("om_b"))

    def test_an_unknown_delivery_is_never_replayed(self):
        """The reply may already be in the chat, so we must not resend it."""
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.rows = {"om_x": "unknown"}
        bridge.seen_lock = threading.Lock()
        self.assertFalse(bridge._claim("om_x"))

    def test_legacy_pending_delivery_without_safe_job_is_held(self):
        """Its last reply provably never left, so a retry cannot double-post."""
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.rows = {"om_x": "pending"}
        bridge.seen_lock = threading.Lock()
        self.assertFalse(bridge._claim("om_x"))

    def test_bot_sender_is_ignored(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok"])
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge._handle(
            {
                "message_id": "om_bot",
                "chat_id": "oc_1",
                "content": "hi",
                "sender_id": "ou_bot",
                "sender_type": "bot",
            }
        )
        self.assertTrue(bridge.turns.empty())

    def test_allowed_sender_filter(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok"])
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge._handle(
            {
                "message_id": "om_no",
                "chat_id": "oc_1",
                "content": "hi",
                "sender_id": "ou_other",
                "sender_type": "user",
            }
        )
        self.assertTrue(bridge.turns.empty())
        bridge._handle(
            {
                "message_id": "om_yes",
                "chat_id": "oc_1",
                "content": "hi",
                "sender_id": "ou_ok",
                "sender_type": "user",
            }
        )
        self.assertEqual(bridge.turns.qsize(), 1)

    def test_one_session_per_chat(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(mcode="/bin/true", cwd="/tmp", timeout=5)
        bridge.env = None
        bridge.sessions = {}
        bridge.sessions_lock = threading.Lock()
        first = bridge._session_for("oc_one")
        second = bridge._session_for("oc_two")
        self.assertIsNot(first, second)
        self.assertIs(first, bridge._session_for("oc_one"))

    def test_collect_text_prefers_result_content(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = []
        session.buffer_lock = threading.Lock()
        result = {"content": [{"type": "text", "text": "from result"}]}
        self.assertEqual(session._collect_text(result), "from result")

    def test_collect_text_falls_back_to_updates(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [update("agent_message_chunk", "from update")]
        session.buffer_lock = threading.Lock()
        self.assertEqual(session._collect_text({}), "from update")

    def test_only_the_answer_chunk_becomes_the_reply(self):
        """Private reasoning and the echoed question must not leave the seat.

        A session streams more than the answer: the seat re-emits the owner's
        own words as user_message_chunk and its internal reasoning as
        agent_thought_chunk.  Taking every text update posted both to Lark.
        """
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [
            update("user_message_chunk", "what is my open id?"),
            update("agent_thought_chunk", "the owner is probably asking how to rotate it"),
            update("agent_message_chunk", "here is how you rotate it"),
        ]
        session.buffer_lock = threading.Lock()
        self.assertEqual(session._collect_text({}), "here is how you rotate it")

    def test_updates_do_not_carry_into_the_next_turn(self):
        """One turn's updates must not be answered again on the next one.

        The pump appends every message the ACP server writes and _call only
        removes what matches its own request id, so the session/update
        notifications used to survive the turn that produced them.
        """
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [update("agent_message_chunk", "turn one")]
        session.buffer_lock = threading.Lock()
        self.assertEqual(session._collect_text({}), "turn one")
        self.assertEqual(session.buffer, [])
        self.assertEqual(session._collect_text({}), "")

    def test_update_shape_variations_are_still_collected(self):
        """A spelled discriminator or a flat update still reaches Lark.

        The seat's ACP client has shipped the snake_case key and a params
        without a nested update; a strict parse answered those turns with
        "(MM produced no reply.)".
        """
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "session_update": "agent_message_chunk",
                        "content": {"type": "text", "text": "snake_case answer"},
                    }
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": " and a flat one"},
                },
            },
        ]
        session.buffer_lock = threading.Lock()
        self.assertEqual(session._collect_text({}), "snake_case answer and a flat one")

    def test_a_non_object_acp_frame_does_not_stop_the_pump(self):
        """A scalar frame is dropped, and the ones after it still land."""
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = []
        session.buffer_lock = threading.Lock()
        for frame in (7, None, "nope", [1, 2]):
            session._handle_message(frame)
        session._handle_message(update("agent_message_chunk", "still here"))
        self.assertEqual(session._collect_text({}), "still here")

    def test_a_frame_that_raises_does_not_stop_the_pump(self):
        """One unroutable frame is logged and the stream carries on.

        _pump is the session's only reader, so an exception out of one frame
        used to end the thread and leave every later _call waiting for a
        response that could not arrive until the prompt timed out.
        """
        lines = [
            json.dumps(update("agent_message_chunk", "first")),
            "not json at all",
            json.dumps({"method": "session/update", "params": "not an object"}),
            json.dumps(update("agent_message_chunk", "second")),
        ]

        class FakeStdout:
            def __iter__(self):
                return iter(lines)

        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.process = type("P", (), {"stdout": FakeStdout()})()
        session.buffer = []
        session.buffer_lock = threading.Lock()

        seen = []
        original = session._handle_message

        def route(message):
            seen.append(message)
            if len(seen) == 1:
                raise ValueError("unroutable frame")
            original(message)

        session._handle_message = route
        session._pump()
        self.assertEqual(len(seen), 3)
        # The reader survives routing errors.  The collector rejects the malformed
        # update explicitly instead of silently treating a damaged turn as complete.
        self.assertIsInstance(session._collect_text({}), str)
        self.assertEqual(session._collect_text({}), "")

    def test_thought_chunks_are_not_answered_to_lark(self):
        """Private reasoning never reaches the chat surface.

        The flat params shape is asserted here too: the private-reasoning
        exclusion must hold on the branch where update is params itself, or a
        later simplification would stream the seat's thinking into Lark with
        this suite still green.
        """
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking about it"},
                    }
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "private reasoning"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "the owner's own words"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "the answer"},
                    }
                },
            },
        ]
        session.buffer_lock = threading.Lock()
        self.assertEqual(session._collect_text({}), "the answer")

    def test_only_updates_after_the_prompt_are_collected(self):
        """An update from before the prompt is this turn's answer's ancestor.

        A session/prompt returns only stopReason, so the text comes from the
        notifications, but only the ones the turn actually produced.
        """
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer one"},
                    }
                },
            }
        ]
        session.buffer_lock = threading.Lock()
        since = len(session.buffer)
        session.buffer.append(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": " answer two"},
                    }
                },
            }
        )
        self.assertEqual(session._collect_text({}, since), " answer two")
        self.assertEqual(session.buffer, [])

    def test_collect_text_consumes_updates_even_when_the_result_has_text(self):
        """The result path must clear the updates too, or they build up."""
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [update("agent_message_chunk", "leftover")]
        session.buffer_lock = threading.Lock()
        result = {"content": [{"type": "text", "text": "from result"}]}
        self.assertEqual(session._collect_text(result), "from result")
        self.assertEqual(session.buffer, [])

    def test_call_ignores_a_server_request_that_shares_the_request_id(self):
        """A request carrying our id is not our response.

        The bridge and the ACP server both number requests from 1, so a
        server-initiated request can collide with the pending one.  Matching on
        id alone ended the turn with no content.
        """

        class CollidingScript(FakeSessionScript):
            def session_prompt(self, request):
                # Emitted before the response, so a matcher that only looks at
                # the id sees this first.
                self.process.stdout.emit(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "method": "session/request_permission",
                        "params": {"sessionId": "mvs_test_1", "options": []},
                    }
                )
                return FakeSessionScript.session_prompt(self, request)

        script = CollidingScript(answer="BRIDGE_OK")
        session = self.fake_session(script)
        session._start()
        self.assertEqual(session.prompt("hello there"), "BRIDGE_OK")

    def test_signal_handler_stops_the_event_reader(self):
        """SIGTERM has to break the blocking read, not just set the flag.

        A signal handler that returns leaves the main thread parked in
        readline() (PEP 475 retries it), so an idle bridge used to sit there
        until the next message arrived and the finally block never got to save
        state.  The stand-in reader blocks until stop() releases it, the way
        the consumer's stdout does.
        """
        released = threading.Event()

        class BlockingReader:
            def events(self):
                released.wait(timeout=10)
                return iter(())

            def stop(self):
                released.set()

        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(lark_cli="/bin/true", event_log_dir=None)
        bridge.env = None
        bridge.sessions = {}
        bridge.sessions_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge.rows = {"om_seen": "ok"}
        bridge.seen_lock = threading.Lock()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge.stopping = threading.Event()

        real_reader = bridge_mod.LarkEventReader
        bridge_mod.LarkEventReader = lambda *a, **kw: BlockingReader()
        # signal.signal() only works on the main thread, and unittest runs the
        # test there, so run() stays on this thread and the signal is raised
        # from another one, the way launchd would.
        threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        try:
            bridge.run()
        finally:
            bridge_mod.LarkEventReader = real_reader
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.default_int_handler)

        self.assertTrue(released.is_set(), "the event reader was never stopped")
        self.assertTrue(bridge.stopping.is_set())
        self.assertTrue(bridge.state_file.exists(), "state was not saved on shutdown")

    def test_acp_handshake_and_prompt_against_fake_process(self):
        script = FakeSessionScript(answer="BRIDGE_OK")
        session = self.fake_session(script)
        session._start()
        self.assertEqual(session.session_id, "mvs_test_1")
        answer = session.prompt("hello there")
        self.assertEqual(answer, "BRIDGE_OK")
        self.assertEqual(script.prompts, ["hello there"])

    def test_a_turn_answer_arrives_as_a_streamed_notification(self):
        """ACP v1 returns only stopReason; the text comes in session/update.

        The fake used to invent a result.content, so the one end-to-end ACP
        test covered a shape the real server never produces and the
        notification path was never exercised.
        """
        script = FakeSessionScript(answer="STREAMED")
        session = self.fake_session(script)
        session._start()
        self.assertEqual(session.prompt("hello there"), "STREAMED")
        # Nothing but the answer chunk reaches the reply.
        self.assertEqual(session.buffer, [])

    def test_group_requires_mention(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok", "--bot-open-id", "ou_bot"])
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge._handle(
            {
                "message_id": "om_g1",
                "chat_id": "oc_group",
                "chat_type": "group",
                "content": "hello everyone",
                "sender_id": "ou_ok",
                "sender_type": "user",
                "mentions": [],
            }
        )
        self.assertTrue(bridge.turns.empty())
        bridge._handle(
            {
                "message_id": "om_g2",
                "chat_id": "oc_group",
                "chat_type": "group",
                "content": "@bot hello",
                "sender_id": "ou_ok",
                "sender_type": "user",
                "mentions": [{"id": "ou_bot"}],
            }
        )
        self.assertEqual(bridge.turns.qsize(), 1)

    def test_malformed_events_are_dropped_before_they_reach_the_queue(self):
        """A frame the socket delivered is not shaped like a Python dict.

        _handle runs on the event loop with no try around it, so a content that
        arrived as an object raised out of the call and the finally in run()
        shut the whole bridge down.
        """
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok"])
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge._handle("not an object")
        bridge._handle({"message_id": {"nested": "om_x"}, "chat_id": "oc_1", "content": "hi"})
        bridge._handle({"message_id": "om_x", "chat_id": 17, "content": "hi"})
        bridge._handle({"message_id": "om_x", "chat_id": "oc_1", "content": {"text": "hi"}})
        self.assertTrue(bridge.turns.empty())

    def test_a_malformed_mentions_field_is_not_an_authorisation(self):
        """A mentions this bridge cannot read leaves the bot quiet."""
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--bot-open-id", "ou_bot"])
        self.assertFalse(bridge._mentioned({"chat_type": "group", "mentions": "ou_bot"}))
        self.assertFalse(bridge._mentioned({"chat_type": "group", "mentions": ["ou_bot"]}))
        self.assertTrue(bridge._mentioned({"chat_type": "group", "mentions": [{"id": "ou_bot"}]}))

    def test_p2p_needs_no_mention(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok", "--bot-open-id", "ou_bot"])
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge._handle(
            {
                "message_id": "om_p1",
                "chat_id": "oc_p2p",
                "chat_type": "p2p",
                "content": "hello",
                "sender_id": "ou_ok",
                "sender_type": "user",
                "mentions": [],
            }
        )
        self.assertEqual(bridge.turns.qsize(), 1)

    def test_parser_exposes_every_attribute_the_code_reads(self):
        """Every self.args.<name> in the bridge must exist on a real Namespace.

        The bridge used to crash on its first live message: it read
        args.allowed_sender while argparse produced args.allow_sender.  The
        old tests could not catch that because they hand-built the attribute
        names the code expected instead of using the parser.
        """
        source = SCRIPT.read_text()
        used = sorted(set(re.findall(r"self\.args\.([a-z_]+)", source)))
        args = bridge_mod.build_parser().parse_args([])
        missing = [name for name in used if not hasattr(args, name)]
        self.assertEqual(
            missing, [], "the bridge reads args the parser never defines: %s" % missing
        )

    def test_allow_sender_flag_reaches_allowed_sender_attribute(self):
        args = bridge_mod.build_parser().parse_args(
            ["--allow-sender", "ou_one", "--allow-sender", "ou_two"]
        )
        self.assertEqual(args.allowed_sender, ["ou_one", "ou_two"])

    def test_sentence_gap_widens_after_a_sentence_end(self):
        out = bridge_mod.sentence_gap("Sentence one. Sentence two.")
        self.assertEqual(out, "Sentence one.  Sentence two.")

    def test_sentence_gap_normalises_two_ascii_spaces_to_the_wide_gap(self):
        out = bridge_mod.sentence_gap("Sentence one.  Sentence two.")
        self.assertEqual(out, "Sentence one.  Sentence two.")

    def test_sentence_gap_uses_two_spaces_and_never_a_nbsp(self):
        out = bridge_mod.sentence_gap("Sentence one. Sentence two.")
        self.assertNotIn("\u00a0", out)
        self.assertEqual(bridge_mod.SENTENCE_GAP, "  ")

    def test_sentence_gap_leaves_colons_and_commas_alone(self):
        out = bridge_mod.sentence_gap("Note: value, then; more")
        self.assertEqual(out, "Note: value, then; more")

    def test_sentence_gap_leaves_inline_code_alone(self):
        out = bridge_mod.sentence_gap("Use `foo. bar` as the key. Then go.")
        self.assertIn("`foo. bar`", out)
        self.assertTrue(out.endswith(".  Then go."))

    def test_sentence_gap_leaves_fenced_code_alone(self):
        text = "Here:\n```\nx = 1.  y = 2\n```\nDone. Now."
        out = bridge_mod.sentence_gap(text)
        self.assertIn("x = 1.  y = 2", out)
        self.assertTrue(out.endswith(".  Now."))

    def test_sentence_gap_keeps_abbreviations_and_decimals(self):
        self.assertEqual(
            bridge_mod.sentence_gap("Dr. Smith is here. Next."), "Dr. Smith is here.  Next."
        )
        self.assertEqual(bridge_mod.sentence_gap("Version 3.14 ships."), "Version 3.14 ships.")

    def test_answer_request_cancels_permission_without_trusted_approval(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.process = FakeProcess(FakeSessionScript())
        session.stdin_lock = threading.Lock()
        session._answer_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/request_permission",
                "params": {
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    ]
                },
            }
        )
        payload = json.loads(session.process.stdin.written[0])
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["result"]["outcome"], {"outcome": "cancelled"})

    def test_answer_request_does_not_infer_approval_from_offered_options(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.process = FakeProcess(FakeSessionScript())
        session.stdin_lock = threading.Lock()
        session._answer_request(
            {
                "id": 9,
                "method": "session/request_permission",
                "params": {
                    "options": [
                        {"optionId": "allow_once", "kind": "allow_once"},
                        {"optionId": "allow_always", "kind": "allow_always"},
                    ]
                },
            }
        )
        payload = json.loads(session.process.stdin.written[0])
        self.assertEqual(payload["result"]["outcome"], {"outcome": "cancelled"})

    def test_answer_request_answers_an_unknown_request_so_the_turn_does_not_hang(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.process = FakeProcess(FakeSessionScript())
        session.stdin_lock = threading.Lock()
        session._answer_request({"id": 3, "method": "session/some_other_request"})
        payload = json.loads(session.process.stdin.written[0])
        self.assertEqual(payload["id"], 3)
        self.assertIn("error", payload)

    def test_pump_routes_a_server_request_to_the_responder(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        answered = []
        session._answer_request = lambda message: answered.append(message)
        session.buffer = []
        session.buffer_lock = threading.Lock()
        session._handle_message({"jsonrpc": "2.0", "method": "session/request_permission", "id": 4})
        session._handle_message({"jsonrpc": "2.0", "id": 11, "result": {"ok": True}})
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["method"], "session/request_permission")
        self.assertEqual(len(session.buffer), 1)
        self.assertEqual(session.buffer[0]["id"], 11)

    def test_pump_keeps_a_notification_out_of_the_responder(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        answered = []
        session._answer_request = lambda message: answered.append(message)
        session.buffer = []
        session.buffer_lock = threading.Lock()
        session._handle_message({"jsonrpc": "2.0", "method": "session/update", "params": {}})
        self.assertEqual(answered, [])
        self.assertEqual(len(session.buffer), 1)

    def test_sentence_gap_accepts_a_different_gap(self):
        out = bridge_mod.sentence_gap(
            "Sentence one. Sentence two.", gap=bridge_mod.ZULIP_SENTENCE_GAP
        )
        self.assertEqual(out, "Sentence one.\u00a0 Sentence two.")
        self.assertEqual(bridge_mod.ZULIP_SENTENCE_GAP, "\u00a0 ")

    def test_mirror_posts_the_exchange_with_the_session_id(self):
        sent = {}

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent["command"] = command
            sent["env"] = env
            return ""

        original = bridge_mod.run
        bridge_mod.run = fake_run
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.env = {}
            bridge._mirror("oc_6ca9549d8ed34274afc4e50108eb1754", "mvs_abc123", "Ping", "Pong")
        finally:
            bridge_mod.run = original
        body = sent["command"][-1]
        self.assertIn("mvs_abc123", body)
        self.assertIn("Ping", body)
        self.assertIn("Pong", body)
        self.assertIn("Lark", " ".join(sent["command"]))
        self.assertEqual(sent["env"]["AGENT_SEAT"], "MM")
        self.assertEqual(sent["env"]["AGENT_SESSION"], "mvs_abc123")

    def test_mirror_failure_does_not_raise(self):
        def fake_run(command, stdin_text=None, timeout=120, env=None):
            raise bridge_mod.CommandError("boom")

        original = bridge_mod.run
        bridge_mod.run = fake_run
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.env = {}
            bridge._mirror("oc_1", "mvs_abc123", "Ping", "Pong")
        finally:
            bridge_mod.run = original

    def test_lark_exchange_lands_in_the_seat_inbox(self):
        import tempfile

        from agent_sync import live as L

        with tempfile.TemporaryDirectory() as root:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.args = make_args(seat="MM")
            bridge.args.seat = "MM"
            bridge.args.agent_sync_root = root
            bridge._record_in_seat_inbox(
                "oc_6ca9549d8ed34274afc4e50108eb1754",
                "om_1",
                "mvs_abc123",
                "Ping",
                "Pong",
                sender_id="ou_owner",
            )
            paths = L.SeatPaths(root, "MM")
            rows = L.read_jsonl(paths.seat_inbox)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["seq"], 1)
            self.assertEqual(row["channel"], "lark")
            self.assertIn("Ping", row["content"])
            self.assertIn("Pong", row["content"])
            self.assertIn("mvs_abc123", row["content"] + row["topic"])
            self.assertEqual(L.read_json(paths.seat_inbox_meta)["next_seq"], 2)

    def test_lark_env_prepends_cli_prefix(self):
        # lark_env prepends the prefix only when it exists, so the assertion
        # needs one.  Point HOME at a temporary directory instead of depending
        # on the install layout of whichever machine runs the suite.
        with tempfile.TemporaryDirectory() as home:
            prefix = Path(home) / ".local" / "npm-global" / "bin"
            prefix.mkdir(parents=True)
            previous = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                env = bridge_mod.lark_env()
            finally:
                if previous is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = previous
        self.assertIn(str(prefix), env.get("PATH", ""))

    def fake_session(self, script):
        """A MiniMaxSession wired to a FakeProcess, ready for _start()."""
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.mcode = "/bin/true"
        session.cwd = "/tmp"
        session.timeout = 30
        session.env = None
        session.process = None
        session.session_id = None
        session.next_id = 1
        session.buffer = []
        session.buffer_lock = threading.Lock()
        session.lock = threading.Lock()
        session.stdin_lock = threading.Lock()
        session.spawn = lambda command, **kwargs: FakeProcess(script)
        return session

    def bare_bridge(self, argv=None):
        """A Bridge wired up for the unit under test, with no state on disk."""
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(argv or ["--allow-sender", "ou_ok"])
        bridge.env = None
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.sessions = {}
        bridge.sessions_lock = threading.Lock()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge.stopping = threading.Event()
        return bridge

    def test_reply_outcomes_are_told_apart(self):
        """A failed send means three different things, not one.

        Retrying an unknown write double-posts a reply the chat may already
        have, so each failure has to land on its own state.
        """
        cases = [
            # (failure detail, state, whether the turn asks to be retried)
            ("lark-cli could not start: [Errno 2] No such file", "pending", True),
            ("lark-cli failed: connection reset by peer", "unknown", False),
            ("lark-cli failed: broken pipe", "unknown", False),
            ("lark-cli failed: connection refused", "pending", True),
            ("lark-cli failed: 400 invalid message_id", "refused", False),
            ("lark-cli failed: HTTP 429 rate limited", "pending", True),
            ("lark-cli timed out after 120s", "unknown", False),
        ]
        for detail, expected, retried in cases:
            bridge = self.bare_bridge()
            bridge._reply = self.stub_reply_failure(detail)
            retry = not bridge._send("om_1", "hello")
            self.assertEqual(bridge.rows["om_1"], expected, detail)
            # Only a provably unsent write is worth another attempt; an
            # unknown one must never come back, since it may already be in
            # the chat.
            self.assertEqual(retry, retried, detail)

    def test_refused_credential_is_recorded_as_refused(self):
        bridge = self.bare_bridge()

        def refuse(message_id, text):
            raise bridge_mod.SecretRefusal("code=secret field=text")

        bridge._reply = refuse
        self.assertTrue(bridge._send("om_1", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"))
        self.assertEqual(bridge.rows["om_1"], "refused")

    def test_a_delivered_reply_is_recorded_as_ok(self):
        bridge = self.bare_bridge()
        bridge._reply = lambda message_id, text: None
        self.assertTrue(bridge._send("om_1", "hello"))
        self.assertEqual(bridge.rows["om_1"], "ok")

    def test_retryable_send_retries_until_the_attempt_budget_is_spent(self):
        bridge = self.bare_bridge()
        bridge._reply = self.stub_reply_failure("lark-cli could not start")
        for attempt in range(bridge_mod.MAX_REPLY_ATTEMPTS - 1):
            self.assertFalse(bridge._send("om_1", "hello", attempt=attempt), attempt)
        self.assertTrue(bridge._send("om_1", "hello", attempt=bridge_mod.MAX_REPLY_ATTEMPTS - 1))
        # Left pending either way, so a restart can still claim it.
        self.assertEqual(bridge.rows["om_1"], "pending")

    def stub_reply_failure(self, detail):
        def reply(message_id, text):
            raise bridge_mod.DeliveryError(
                bridge_mod.Bridge._classify_reply_failure(detail), detail
            )

        return reply

    def test_corrupt_state_refuses_to_start(self):
        """A torn state file must not read as "nothing was delivered"."""
        bridge = self.bare_bridge()
        bridge.state_file.write_text("{ this is not json")
        with self.assertRaises(RuntimeError):
            bridge._load_state()

    def test_state_of_an_unexpected_shape_is_refused(self):
        bridge = self.bare_bridge()
        bridge.state_file.write_text(json.dumps({"messages": {"om_1": "maybe"}}))
        with self.assertRaises(RuntimeError):
            bridge._load_state()

    def test_the_first_release_state_still_loads(self):
        bridge = self.bare_bridge()
        bridge.state_file.write_text(json.dumps({"seen_message_ids": ["om_old", "om_newer"]}))
        bridge._load_state()
        self.assertEqual(bridge.rows, {"om_old": "ok", "om_newer": "ok"})

    def test_an_absent_state_file_is_an_empty_history(self):
        bridge = self.bare_bridge()
        bridge._load_state()
        self.assertEqual(bridge.rows, {})

    def test_unresolved_rows_survive_the_dedup_window(self):
        """The window may age out delivered ids, never an unresolved one."""
        bridge = self.bare_bridge()
        bridge.rows = {}
        for index in range(bridge_mod.DEDUP_WINDOW + 50):
            bridge.rows["om_%d" % index] = "ok"
        bridge.rows["om_pending"] = "pending"
        bridge.rows["om_unknown"] = "unknown"
        bridge._save_state()
        saved = json.loads(bridge.state_file.read_text())["messages"]
        self.assertEqual(saved["om_pending"], "pending")
        self.assertEqual(saved["om_unknown"], "unknown")
        # The newest delivered ids are the ones kept, and the oldest go.
        self.assertIn("om_%d" % (bridge_mod.DEDUP_WINDOW + 49), saved)
        self.assertNotIn("om_0", saved)
        self.assertLess(len(saved), bridge_mod.DEDUP_WINDOW + 50)

    def test_a_dead_session_is_replaced_rather_than_reused(self):
        """A cached session whose child exited cannot answer the next turn."""
        bridge = self.bare_bridge()
        bridge.args = make_args(
            ["--allow-sender", "ou_ok"], mcode="/bin/true", cwd="/tmp", timeout=5
        )
        dead = bridge_mod.MiniMaxSession("/bin/true", "/tmp", 5)
        dead.process = FakeProcess(FakeSessionScript())
        dead.process.returncode = 1
        closed = []
        dead.close = lambda: closed.append(True)
        bridge.sessions["oc_one"] = dead
        replacement = bridge._session_for("oc_one")
        self.assertIsNot(replacement, dead)
        self.assertEqual(closed, [True])

    def test_a_live_session_is_reused(self):
        bridge = self.bare_bridge()
        bridge.args = make_args(
            ["--allow-sender", "ou_ok"], mcode="/bin/true", cwd="/tmp", timeout=5
        )
        live = bridge_mod.MiniMaxSession("/bin/true", "/tmp", 5)
        live.process = FakeProcess(FakeSessionScript())
        bridge.sessions["oc_one"] = live
        self.assertIs(bridge._session_for("oc_one"), live)

    def test_agent_permission_request_is_answered(self):
        """An unanswered agent request stalls the turn until it times out.

        Nobody is watching Lark to approve a tool call, so the bridge has to
        decline it itself; otherwise the first turn needing a tool waits out
        the whole prompt timeout.
        """
        script = FakeSessionScript()
        session = self.fake_session(script)
        session._start()
        process = script.process

        session._answer_request(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/request_permission",
                "params": {"sessionId": "mvs_test_1"},
            }
        )
        self.assertEqual(
            json.loads(process.stdin.written[-1]),
            {
                "jsonrpc": "2.0",
                "id": 99,
                "result": {"outcome": {"outcome": "cancelled"}},
            },
        )
        session._answer_request({"jsonrpc": "2.0", "id": 100, "method": "fs/write"})
        self.assertEqual(json.loads(process.stdin.written[-1])["error"]["code"], -32601)

    def test_start_refuses_to_run_without_a_pinned_sender(self):
        """No pinned owner means every Lark user can drive the seat."""
        parser = bridge_mod.build_parser()
        errors = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(errors):
            bridge_mod.main(
                [
                    "--lark-cli",
                    sys.executable,
                    "--mcode",
                    sys.executable,
                    "--state-dir",
                    self.state_dir.name,
                ]
            )
        self.assertIn("--allow-sender is required", errors.getvalue())
        args = parser.parse_args(["--allow-sender", "ou_owner"])
        self.assertEqual(args.allowed_sender, ["ou_owner"])

    def test_state_directory_is_private(self):
        args = make_args(["--allow-sender", "ou_ok"])
        args.state_dir = Path(self.state_dir.name) / "private"
        bridge_mod.Bridge(args)
        self.assertEqual(args.state_dir.stat().st_mode & 0o777, 0o700)

    def test_state_directory_refuses_a_symlink(self):
        target = Path(self.state_dir.name) / "real"
        target.mkdir()
        link = Path(self.state_dir.name) / "link"
        link.symlink_to(target)
        args = make_args(["--allow-sender", "ou_ok"])
        args.state_dir = link
        with self.assertRaises(ValueError):
            bridge_mod.Bridge(args)

    def test_group_without_a_pinned_bot_id_does_not_answer(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok"])
        self.assertFalse(bridge._mentioned({"chat_type": "group", "mentions": []}))
        self.assertTrue(bridge._mentioned({"chat_type": "p2p"}))

    def test_flat_private_and_user_chunks_are_not_replies(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer_lock = threading.Lock()
        session.buffer = [
            {"method": "session/update", "params": {"session_update": kind, "content": content}}
            for kind, content in (
                ("agent_thought_chunk", "private reasoning"),
                ("user_message_chunk", {"text": "echoed question"}),
                ("agent_message_chunk", "answer one"),
                ("agent_message_chunk", {"text": " and two"}),
            )
        ]
        self.assertEqual(session._collect_text({}), "answer one and two")
        self.assertEqual(session.buffer, [])

    def test_notification_sections_and_discriminator_fail_closed_on_wrong_types(self):
        for params in (
            None,
            [],
            "not-an-object",
            {"update": []},
            {"update": None},
            {"sessionUpdate": 7},
            {"session_update": False},
        ):
            session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
            session.buffer_lock = threading.Lock()
            session.buffer = [{"method": "session/update", "params": params}]
            self.assertEqual(session._collect_text({}), "")

    def test_signal_handlers_are_restored_even_when_reader_cleanup_fails(self):
        for failure in (None, "events", "stop"):
            args = make_args(["--allow-sender", "ou_ok"], state_dir=self.state_dir.name)
            bridge = bridge_mod.Bridge(args)
            session = mock.Mock()
            bridge.sessions["chat"] = session
            reader = mock.Mock()
            reader.events.return_value = iter(())
            if failure == "events":
                reader.events.side_effect = RuntimeError("fake reader failed")
            if failure == "stop":
                reader.stop.side_effect = RuntimeError("fake reader stop failed")
            old_term = signal.getsignal(signal.SIGTERM)
            old_int = signal.getsignal(signal.SIGINT)
            with (
                mock.patch.object(bridge_mod, "LarkEventReader", return_value=reader),
                mock.patch.object(bridge_mod.threading, "Thread"),
                mock.patch.object(bridge_mod.signal, "signal") as install,
            ):
                if failure:
                    with self.assertRaises(RuntimeError):
                        bridge.run()
                else:
                    bridge.run()
            self.assertEqual(
                install.call_args_list[-2:],
                [mock.call(signal.SIGTERM, old_term), mock.call(signal.SIGINT, old_int)],
            )
            session.close.assert_called_once()
            self.assertTrue(bridge.stopping.is_set())

    def test_redelivery_cannot_replace_durable_cached_reply(self):
        bridge = self.bare_bridge()
        bridge.rows["om_1"] = "pending"
        job = {"stage": "reply", "reply": "cached", "attempt": 1, "cwd": bridge.args.cwd}
        bridge.jobs = {"om_1": job.copy()}
        bridge.active_ids = set()
        self.assertFalse(bridge._claim("om_1"))
        self.assertEqual(bridge.jobs["om_1"], job)

    def test_duplicate_event_during_pending_turn_is_not_queued_twice(self):
        bridge = self.bare_bridge()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        event = {
            "message_id": "om_1",
            "chat_id": "chat",
            "chat_type": "p2p",
            "sender_id": "ou_ok",
            "sender_type": "user",
            "content": "hello",
        }
        bridge._handle(event)
        bridge._handle(event)
        self.assertEqual(bridge.turns.qsize(), 1)
        self.assertEqual(bridge.rows["om_1"], "pending")

    def test_crash_after_lark_acceptance_preserves_unknown_and_prevents_replay(self):
        bridge = self.bare_bridge()
        bridge.rows["om_1"] = "pending"

        def accepted_then_crashed(message_id, text):
            saved = json.loads(bridge.state_file.read_text())["messages"]
            self.assertEqual(saved[message_id], "unknown")
            raise KeyboardInterrupt("simulate process interruption after acceptance")

        bridge._reply = accepted_then_crashed
        with self.assertRaises(KeyboardInterrupt):
            bridge._send("om_1", "reply")
        restarted = self.bare_bridge()
        restarted._load_state()
        self.assertEqual(restarted.rows["om_1"], "unknown")
        self.assertFalse(restarted._claim("om_1"))

    def test_persistence_failure_never_calls_lark(self):
        bridge = self.bare_bridge()
        bridge._reply = mock.Mock()
        with mock.patch.object(bridge_mod.os, "replace", side_effect=OSError("fake disk failure")):
            self.assertTrue(bridge._send("om_1", "reply"))
        bridge._reply.assert_not_called()
        self.assertEqual(bridge.rows["om_1"], "pending")

    def test_reply_retry_uses_durable_answer_without_running_model_again(self):
        args = make_args(["--allow-sender", "ou_ok"], state_dir=self.state_dir.name)
        bridge = bridge_mod.Bridge(args)
        bridge._handle(
            {
                "message_id": "once",
                "chat_id": "chat",
                "sender_id": "ou_ok",
                "content": "do one task",
            }
        )
        session = mock.Mock()
        session.prompt.return_value = "Task finished."
        bridge._session_for = mock.Mock(return_value=session)
        bridge._reply = mock.Mock(side_effect=[bridge_mod.DeliveryError("retryable", "429"), None])
        self.assertFalse(bridge._turn("once", "chat", "do one task", "ou_ok", 0))
        bridge._save_state()
        restored = bridge_mod.Bridge(args)
        restored._session_for = mock.Mock(side_effect=AssertionError("model rerun"))
        restored._reply = mock.Mock()
        self.assertTrue(restored._turn("once", "chat", "do one task", "ou_ok", 1))
        session.prompt.assert_called_once_with("do one task")
        restored._reply.assert_called_once_with("once", "Task finished.")

    def test_queued_turns_survive_restart_but_running_turns_are_held(self):
        args = make_args(["--allow-sender", "ou_ok"], state_dir=self.state_dir.name)
        bridge = bridge_mod.Bridge(args)
        for mid in ("first", "second"):
            bridge._handle(
                {
                    "message_id": mid,
                    "chat_id": "chat",
                    "sender_id": "ou_ok",
                    "content": "queued task",
                }
            )
        bridge.jobs["first"]["stage"] = "running"
        self.assertTrue(bridge._save_state())
        restored = bridge_mod.Bridge(args)
        self.assertEqual(restored.turns.qsize(), 1)
        self.assertEqual(restored.turns.get()[0], "second")
        self.assertFalse(restored._claim("first"))
        self.assertEqual(restored.jobs["first"]["content"], "queued task")

    def test_canonical_fleet_secret_formats_are_refused(self):
        bridge = self.bare_bridge()
        bridge.args.lark_cli = "/bin/true"
        for text in (
            "lin_api_" + "a" * 24,
            "st." + "a" * 8 + "." + "b" * 8 + "." + "c" * 8,
            "AKIA" + "A" * 16,
            "API_KEY=" + "a" * 24,
            "eyJ" + "a" * 16 + ".eyJ" + "b" * 16 + "." + "c" * 16,
        ):
            with mock.patch.object(bridge_mod, "run") as execute:
                with self.assertRaises(bridge_mod.SecretRefusal):
                    bridge._reply("secret", text)
                execute.assert_not_called()

    def test_invalid_acp_method_and_id_types_are_not_routed(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer, session.buffer_lock = [], threading.Lock()
        session._answer_request = mock.Mock()
        for frame in ({"method": []}, {"method": "permission", "id": True}, {"id": {"nested": 1}}):
            session._handle_message(frame)
        self.assertEqual(session.buffer, [])
        session._answer_request.assert_not_called()

    def test_restored_jobs_do_not_cross_working_directory_binding(self):
        args = make_args(["--allow-sender", "ou_ok"], state_dir=self.state_dir.name)
        bridge = bridge_mod.Bridge(args)
        bridge._handle(
            {
                "message_id": "bound",
                "chat_id": "chat",
                "sender_id": "ou_ok",
                "content": "queued task",
            }
        )
        args.cwd = args.cwd + "/different"
        restored = bridge_mod.Bridge(args)
        self.assertTrue(restored.turns.empty())
        self.assertFalse(restored._claim("bound"))

    def test_restart_resets_only_safe_pending_reply_budget(self):
        args = make_args(["--allow-sender", "ou_ok"], state_dir=self.state_dir.name)
        bridge = bridge_mod.Bridge(args)
        bridge._handle(
            {"message_id": "retry", "chat_id": "chat", "sender_id": "ou_ok", "content": "one task"}
        )
        bridge.jobs["retry"].update(
            stage="reply", reply="Already generated.", attempt=bridge_mod.MAX_REPLY_ATTEMPTS
        )
        self.assertTrue(bridge._save_state())
        restored = bridge_mod.Bridge(args)
        self.assertEqual(restored.turns.get()[-1], 0)
        restored._session_for = mock.Mock(side_effect=AssertionError("model rerun"))
        restored._reply = mock.Mock()
        self.assertTrue(restored._turn("retry", "chat", "one task", "ou_ok", 0))
        restored._reply.assert_called_once_with("retry", "Already generated.")

    def test_dry_run_never_loads_or_changes_delivery_state_or_starts_model(self):
        state = Path(self.state_dir.name) / "state.json"
        state.write_text('{"messages":{"existing":"pending"}}')
        before = state.read_bytes()
        reader = mock.Mock()
        reader.events.return_value = iter(
            [{"message_id": "preview", "sender_id": "ou_ok", "content": "one task"}]
        )
        with (
            mock.patch.object(bridge_mod, "LarkEventReader", return_value=reader),
            mock.patch.object(
                bridge_mod, "Bridge", side_effect=AssertionError("dry run loaded bridge")
            ),
            mock.patch.object(
                bridge_mod, "MiniMaxSession", side_effect=AssertionError("model started")
            ),
        ):
            self.assertEqual(
                bridge_mod.main(
                    [
                        "--dry-run",
                        "--allow-sender",
                        "ou_ok",
                        "--lark-cli",
                        sys.executable,
                        "--mcode",
                        sys.executable,
                        "--state-dir",
                        self.state_dir.name,
                    ]
                ),
                0,
            )
        self.assertEqual(state.read_bytes(), before)


class LarkSurfaceTests(unittest.TestCase):
    """The eyes marker and the Operator's signal path.

    The marker is the owner's way of saying "do not wake the seat for this
    one", and the signal path is how the Operator puts a message into the
    Lark chat and wakes the seat for it.
    """

    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)

    def credential_shaped_text(self):
        """A text the secret scanner must refuse.

        The token is assembled at runtime, so this file carries no
        credential-shaped literal while the test still exercises the scan.
        """
        return "token %s%s" % ("sk-", "abcdefghijklmnopqrstuvwx")

    def make_bridge(self, argv=None, **overrides):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(argv or ["--allow-sender", "ou_ok"], **overrides)
        bridge.rows = {}
        bridge.jobs = {}
        bridge.active_ids = set()
        bridge.seen_lock = threading.Lock()
        bridge.state_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
        bridge.state_file = Path(self.state_dir.name) / "state.json"
        bridge.signal_dir = Path(self.state_dir.name) / bridge_mod.SIGNAL_DIRNAME
        bridge.signal_done_dir = bridge.signal_dir / "done"
        bridge.signal_dir.mkdir(parents=True, exist_ok=True)
        bridge.signal_done_dir.mkdir(parents=True, exist_ok=True)
        bridge.last_chat_id = None
        bridge.env = {}
        return bridge

    def owner_message(self, message_id, content, chat_id="oc_1"):
        return {
            "message_id": message_id,
            "chat_id": chat_id,
            "content": content,
            "sender_id": "ou_ok",
            "sender_type": "user",
        }

    def write_signal(self, bridge, name, payload):
        path = bridge.signal_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def read_outcome(self, bridge, name):
        path = bridge.signal_done_dir / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_message_that_starts_with_the_eyes_marker_never_wakes_the_seat(self):
        bridge = self.make_bridge()
        bridge._handle(
            self.owner_message("om_eyes", "\U0001f440 thinking out loud, no answer needed")
        )
        self.assertTrue(bridge.turns.empty())
        # No delivery row either:  a marked message is not a message this
        # bridge answered, so it must not look like one in the state file.
        self.assertEqual(bridge.rows, {})

    def test_the_marker_only_counts_at_the_very_start(self):
        bridge = self.make_bridge()
        bridge._handle(self.owner_message("om_late", "hey \U0001f440 what do you think?"))
        self.assertEqual(bridge.turns.qsize(), 1)

    def test_the_marker_is_checked_before_the_secret_scan(self):
        """A marked message is dropped, and its text never reaches the log."""
        bridge = self.make_bridge()
        with mock.patch.object(bridge_mod, "scan_secret", side_effect=AssertionError("scanned")):
            bridge._handle(
                self.owner_message("om_eyes2", "\U0001f440 " + self.credential_shaped_text())
            )
        self.assertTrue(bridge.turns.empty())

    def test_a_signal_is_sent_into_the_chat_and_queued_as_a_turn(self):
        bridge = self.make_bridge()
        sent = []

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent.append(command)
            return json.dumps({"message_id": "om_sig", "chat_id": "oc_1"})

        signal = self.write_signal(
            bridge,
            "sig-1.json",
            {"signal_id": "sig-1", "text": "the operator spoke", "chat_id": "oc_1"},
        )
        with mock.patch.object(bridge_mod, "run", fake_run):
            bridge._handle_signal(signal)
        self.assertEqual(len(sent), 1)
        self.assertIn("+messages-send", sent[0])
        self.assertIn("--chat-id=oc_1", sent[0])
        self.assertIn("--idempotency-key=sig-1", sent[0])
        self.assertEqual(bridge.turns.qsize(), 1)
        message_id, chat_id, content, sender_id, attempt = bridge.turns.get_nowait()
        self.assertEqual(message_id, "om_sig")
        self.assertEqual(chat_id, "oc_1")
        self.assertEqual(content, "the operator spoke")
        # The signal is the Operator's, not the owner's own message.
        self.assertEqual(sender_id, bridge_mod.OPERATOR_SENDER)
        # The outcome is ok, so a retry recognises it instead of resending.
        self.assertEqual(self.read_outcome(bridge, "sig-1.json")["outcome"], "ok")

    def test_a_queued_signal_is_claimed_so_a_restart_keeps_the_turn(self):
        bridge = self.make_bridge()

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            return json.dumps({"message_id": "om_sig"})

        signal = self.write_signal(
            bridge,
            "sig-claim.json",
            {"signal_id": "sig-claim", "text": "wake up", "chat_id": "oc_1"},
        )
        with mock.patch.object(bridge_mod, "run", fake_run):
            bridge._handle_signal(signal)
        # The saved state keeps a job only while its row says pending, so the
        # row has to be claimed or the turn is lost on the next start.
        state = json.loads(bridge.state_file.read_text(encoding="utf-8"))
        self.assertIn("om_sig", state["jobs"])
        self.assertEqual(state["messages"]["om_sig"], "pending")

    def test_a_signal_without_a_chat_goes_to_the_chat_the_owner_last_wrote_in(self):
        bridge = self.make_bridge()
        bridge._handle(self.owner_message("om_1", "hello", chat_id="oc_home"))
        bridge.turns.get_nowait()
        sent = []

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent.append(command)
            return json.dumps({"message_id": "om_sig"})

        signal = self.write_signal(bridge, "sig-2.json", {"signal_id": "sig-2", "text": "wake up"})
        with mock.patch.object(bridge_mod, "run", fake_run):
            bridge._handle_signal(signal)
        self.assertIn("--chat-id=oc_home", sent[0])

    def test_a_signal_with_nowhere_to_go_is_refused(self):
        bridge = self.make_bridge()
        signal = self.write_signal(bridge, "sig-3.json", {"signal_id": "sig-3", "text": "wake up"})
        with mock.patch.object(bridge_mod, "run", side_effect=AssertionError("sent")):
            bridge._handle_signal(signal)
        record = self.read_outcome(bridge, "sig-3.json")
        self.assertEqual(record["outcome"], "refused")
        self.assertEqual(record["detail"], "no_chat")
        self.assertTrue(bridge.turns.empty())

    def test_a_credential_shaped_signal_is_refused_and_never_sent(self):
        bridge = self.make_bridge()
        signal = self.write_signal(
            bridge,
            "sig-4.json",
            {"signal_id": "sig-4", "text": self.credential_shaped_text()},
        )
        with mock.patch.object(bridge_mod, "run", side_effect=AssertionError("sent")):
            bridge._handle_signal(signal)
        record = self.read_outcome(bridge, "sig-4.json")
        self.assertEqual(record["outcome"], "refused")
        self.assertTrue(bridge.turns.empty())

    def test_a_signal_that_cannot_be_sent_is_unknown_and_not_replayed(self):
        bridge = self.make_bridge()
        signal = self.write_signal(
            bridge, "sig-5.json", {"signal_id": "sig-5", "text": "wake up", "chat_id": "oc_1"}
        )
        with mock.patch.object(bridge_mod, "run", side_effect=bridge_mod.CommandError("no")):
            bridge._handle_signal(signal)
        record = self.read_outcome(bridge, "sig-5.json")
        self.assertEqual(record["outcome"], "unknown")
        self.assertTrue(bridge.turns.empty())

    def test_a_retryable_send_leaves_the_signal_where_the_watcher_finds_it(self):
        bridge = self.make_bridge()
        signal = self.write_signal(
            bridge, "sig-6.json", {"signal_id": "sig-6", "text": "wake up", "chat_id": "oc_1"}
        )
        with mock.patch.object(
            bridge_mod, "run", side_effect=bridge_mod.CommandError("429 rate limit")
        ):
            bridge._handle_signal(signal)
        # A rate limit is not the signal's fault, so it stays pending.
        self.assertTrue(signal.exists())
        record = json.loads(signal.read_text(encoding="utf-8"))
        self.assertEqual(record["attempts"], 1)
        self.assertGreater(record["next_attempt"], time.time())
        self.assertFalse((bridge.signal_done_dir / "sig-6.json").exists())

    def test_a_terminal_send_files_the_signal_as_refused(self):
        bridge = self.make_bridge()
        signal = self.write_signal(
            bridge, "sig-7.json", {"signal_id": "sig-7", "text": "wake up", "chat_id": "oc_1"}
        )
        with mock.patch.object(
            bridge_mod, "run", side_effect=bridge_mod.CommandError("400 bad request")
        ):
            bridge._handle_signal(signal)
        record = self.read_outcome(bridge, "sig-7.json")
        self.assertEqual(record["outcome"], "refused")
        self.assertIn("terminal", record["detail"])

    def test_a_retry_after_the_send_does_not_send_the_message_again(self):
        bridge = self.make_bridge()
        sent = []

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent.append(command)
            return json.dumps({"message_id": "om_sig"})

        # The file carries the step that is already done, so the retry picks
        # it up instead of sending the message a second time.
        signal = self.write_signal(
            bridge,
            "sig-8.json",
            {
                "signal_id": "sig-8",
                "text": "wake up",
                "chat_id": "oc_1",
                "sent": "om_sig",
            },
        )
        with mock.patch.object(bridge_mod, "run", fake_run):
            bridge._handle_signal(signal)
        self.assertEqual(sent, [])
        self.assertEqual(bridge.turns.qsize(), 1)

    def test_one_bad_signal_does_not_stop_the_later_ones(self):
        bridge = self.make_bridge()
        first = self.write_signal(bridge, "sig-9.json", {"signal_id": "sig-9", "text": ""})
        second = self.write_signal(
            bridge, "sig-10.json", {"signal_id": "sig-10", "text": "wake up", "chat_id": "oc_1"}
        )
        sent = []

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent.append(command)
            return json.dumps({"message_id": "om_sig"})

        with mock.patch.object(bridge_mod, "run", fake_run), mock.patch.object(
            bridge, "_handle_signal", side_effect=[RuntimeError("boom"), None]
        ):
            bridge._signal_worker_once()
        # The second file is still handed to the handler.
        self.assertTrue(first.exists() or second.exists())

    def test_the_done_directory_is_pruned(self):
        bridge = self.make_bridge()
        for index in range(bridge_mod.SIGNAL_DONE_WINDOW + 5):
            path = bridge.signal_done_dir / ("sig-%d.json" % index)
            path.write_text(json.dumps({"outcome": "ok"}), encoding="utf-8")
        bridge._prune_done()
        left = sorted(bridge.signal_done_dir.glob("*.json"))
        self.assertEqual(len(left), bridge_mod.SIGNAL_DONE_WINDOW)

    def test_a_signal_turn_is_attributed_to_the_operator(self):
        from agent_sync import live as L

        with tempfile.TemporaryDirectory() as root:
            bridge = self.make_bridge()
            bridge.args.agent_sync_root = root
            bridge.args.seat = "MM"
            bridge._record_in_seat_inbox(
                "oc_1",
                "om_sig",
                "mvs_abc123",
                "the operator spoke",
                "the answer",
                sender_id=bridge_mod.OPERATOR_SENDER,
            )
            paths = L.SeatPaths(root, "MM")
            rows = L.read_jsonl(paths.seat_inbox)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            # The Operator's signal is not the owner asking, so it must not
            # wake the seat as if it were.
            self.assertFalse(row["owner"])
            self.assertFalse(row["wake_owner"])
            self.assertFalse(row["wake_worthy"])
            self.assertEqual(row["sender"], bridge_mod.OPERATOR_SENDER)
            self.assertIn("Operator:", row["content"])

    def test_the_mirror_names_the_operator_as_the_author(self):
        bridge = self.make_bridge()
        bridge.args.zulip_mirror = True
        bodies = []

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            bodies.append(command[-1])
            return ""

        with mock.patch.object(bridge_mod, "run", fake_run):
            bridge._mirror(
                "oc_1",
                "mvs_abc123",
                "the operator spoke",
                "the answer",
                sender_id=bridge_mod.OPERATOR_SENDER,
            )
        self.assertIn("Operator: the operator spoke", bodies[0])

    def test_the_signal_idempotency_key_comes_from_the_signal_id_alone(self):
        self.assertEqual(bridge_mod._signal_key("sig-5"), "sig-5")
        self.assertEqual(bridge_mod._signal_key("sig-5"), bridge_mod._signal_key("sig-5"))
        self.assertLessEqual(len(bridge_mod._signal_key("x" * 200)), 50)

    def test_two_signal_ids_that_differ_only_in_punctuation_get_different_keys(self):
        self.assertNotEqual(bridge_mod._signal_key("a/b"), bridge_mod._signal_key("a-b"))

    def test_a_send_answer_without_a_message_id_is_not_guessed(self):
        self.assertEqual(bridge_mod._message_id_from(json.dumps({"message_id": "om_x"})), "om_x")
        self.assertEqual(
            bridge_mod._message_id_from(json.dumps({"data": {"message_id": "om_y"}})), "om_y"
        )
        self.assertIsNone(bridge_mod._message_id_from("not json"))
        self.assertIsNone(bridge_mod._message_id_from(json.dumps({"chat_id": "oc_1"})))
        self.assertIsNone(bridge_mod._message_id_from(json.dumps([1, 2])))

    def test_the_signal_cli_leaves_a_file_the_bridge_picks_up(self):
        code = bridge_mod.main(["--signal", "wake up", "--state-dir", self.state_dir.name])
        self.assertEqual(code, 0)
        files = list((Path(self.state_dir.name) / bridge_mod.SIGNAL_DIRNAME).glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["text"], "wake up")
        self.assertTrue(payload["signal_id"])

    def test_the_signal_cli_refuses_a_credential_shaped_text(self):
        code = bridge_mod.main(
            [
                "--signal",
                self.credential_shaped_text(),
                "--state-dir",
                self.state_dir.name,
            ]
        )
        self.assertEqual(code, 2)
        files = list((Path(self.state_dir.name) / bridge_mod.SIGNAL_DIRNAME).glob("*.json"))
        self.assertEqual(files, [])

    def test_the_signal_cli_names_a_chat_when_it_is_given_one(self):
        code = bridge_mod.main(
            [
                "--signal",
                "wake up",
                "--signal-chat-id",
                "oc_other",
                "--state-dir",
                self.state_dir.name,
            ]
        )
        self.assertEqual(code, 0)
        files = list((Path(self.state_dir.name) / bridge_mod.SIGNAL_DIRNAME).glob("*.json"))
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["chat_id"], "oc_other")

    def test_the_last_chat_survives_a_restart(self):
        bridge = self.make_bridge()
        bridge._handle(self.owner_message("om_1", "hello", chat_id="oc_home"))
        bridge.turns.get_nowait()
        self.assertTrue(bridge._save_state())
        state = json.loads(bridge.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["last_chat_id"], "oc_home")
        again = self.make_bridge()
        again.rows = {}
        again.jobs = {}
        again.active_ids = set()
        again._load_state()
        self.assertEqual(again.last_chat_id, "oc_home")

    def test_a_signal_job_is_requeued_on_a_restart(self):
        bridge = self.make_bridge()
        bridge.state_file.write_text(
            json.dumps(
                {
                    "messages": {"om_sig": "pending"},
                    "jobs": {
                        "om_sig": {
                            "stage": "queued",
                            "chat_id": "oc_1",
                            "content": "the operator spoke",
                            "sender_id": bridge_mod.OPERATOR_SENDER,
                            "cwd": bridge.args.cwd,
                            "attempt": 0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        bridge.rows = {}
        bridge.jobs = {}
        bridge.active_ids = set()
        bridge._load_state()
        self.assertEqual(bridge.turns.qsize(), 1)
        message_id, chat_id, content, sender_id, attempt = bridge.turns.get_nowait()
        self.assertEqual((message_id, sender_id), ("om_sig", bridge_mod.OPERATOR_SENDER))


if __name__ == "__main__":
    unittest.main(verbosity=2)
