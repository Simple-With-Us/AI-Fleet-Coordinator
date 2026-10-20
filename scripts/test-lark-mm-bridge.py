#!/usr/bin/env python3
"""test-lark-mm-bridge.py -- checks the lark-mm-bridge pieces that can be checked
without a live Lark bot.

Run: python3 scripts/test-lark-mm-bridge.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "lark-mm-bridge.py"
spec = importlib.util.spec_from_file_location("lark_mm_bridge", SCRIPT)
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)


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

    def test_a_pending_delivery_may_be_retried(self):
        """Its last reply provably never left, so a retry cannot double-post."""
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.rows = {"om_x": "pending"}
        bridge.seen_lock = threading.Lock()
        self.assertTrue(bridge._claim("om_x"))

    def test_bot_sender_is_ignored(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--allow-sender", "ou_ok"])
        bridge.rows = {}
        bridge.seen_lock = threading.Lock()
        bridge.turns = bridge_mod.queue.Queue()
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

    def test_thought_chunks_are_not_answered_to_lark(self):
        """Private reasoning never reaches the chat surface."""
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

    def test_answer_request_answers_a_permission_request_with_allow(self):
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
        self.assertEqual(payload["result"]["outcome"]["optionId"], "allow_once")

    def test_answer_request_prefers_allow_once_over_allow_always(self):
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
        self.assertEqual(payload["result"]["outcome"]["optionId"], "allow_once")

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

    def test_lark_env_prepends_cli_prefix(self):
        env = bridge_mod.lark_env()
        self.assertIn(os.path.expanduser("~/.local/npm-global/bin"), env.get("PATH", ""))

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
            ("lark-cli failed: connection reset by peer", "pending", True),
            ("lark-cli failed: 400 invalid message_id", "refused", False),
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
        with self.assertRaises(SystemExit):
            bridge_mod.main(
                [
                    "--lark-cli",
                    "/bin/true",
                    "--mcode",
                    "/bin/true",
                    "--state-dir",
                    self.state_dir.name,
                ]
            )
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
