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
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "lark-mm-bridge.py"
spec = importlib.util.spec_from_file_location("lark_mm_bridge", SCRIPT)
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)


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

    def session_prompt(self, request):
        self.prompts.append(request["params"]["prompt"][0]["text"])
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "stopReason": "end_turn",
                "content": [{"type": "text", "text": self.answer}],
            },
        }


class BridgeTests(unittest.TestCase):
    def test_lark_event_key_is_the_message_event(self):
        self.assertEqual(bridge_mod.LARK_EVENT_KEY, "im.message.receive_v1")

    def test_reply_truncates_and_marks(self):
        sent = {}

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent["command"] = command
            return ""

        original = bridge_mod.run
        bridge_mod.run = fake_run
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.args = make_args(lark_cli="/bin/true")
            bridge.env = None
            bridge._reply("om_1", "x" * 5000)
        finally:
            bridge_mod.run = original
        text = sent["command"][sent["command"].index("--text") + 1]
        self.assertIn("reply truncated", text)
        self.assertLess(len(text), 5000)

    def test_reply_uses_reply_command_and_bot_identity(self):
        sent = {}

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent["command"] = command
            return ""

        original = bridge_mod.run
        bridge_mod.run = fake_run
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.args = make_args(lark_cli="/bin/true")
            bridge.env = None
            bridge._reply("om_42", "hello")
        finally:
            bridge_mod.run = original
        command = sent["command"]
        self.assertEqual(command[1:3], ["im", "+messages-reply"])
        self.assertIn("bot", command)
        self.assertIn("om_42", command)
        self.assertIn("hello", command)

    def test_empty_answer_still_replies(self):
        sent = {}

        def fake_run(command, stdin_text=None, timeout=120, env=None):
            sent["command"] = command
            return ""

        original = bridge_mod.run
        bridge_mod.run = fake_run
        try:
            bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
            bridge.args = make_args(lark_cli="/bin/true")
            bridge.env = None
            bridge._reply("om_9", "   ")
        finally:
            bridge_mod.run = original
        text = sent["command"][sent["command"].index("--text") + 1]
        self.assertIn("no reply", text)

    def test_duplicate_message_id_is_ignored(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.seen = set()
        bridge.seen_lock = threading.Lock()
        self.assertTrue(bridge._mark_seen("om_a"))
        self.assertFalse(bridge._mark_seen("om_a"))
        self.assertTrue(bridge._mark_seen("om_b"))

    def test_bot_sender_is_ignored(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args()
        bridge.seen = set()
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
        bridge.seen = set()
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
        result = {"content": [{"type": "text", "text": "from result"}]}
        self.assertEqual(session._collect_text(result), "from result")

    def test_collect_text_falls_back_to_updates(self):
        session = bridge_mod.MiniMaxSession.__new__(bridge_mod.MiniMaxSession)
        session.buffer = [
            {
                "method": "session/update",
                "params": {"update": {"content": {"type": "text", "text": "from update"}}},
            }
        ]
        session.buffer_lock = threading.Lock()
        self.assertEqual(session._collect_text({}), "from update")

    def test_acp_handshake_and_prompt_against_fake_process(self):
        script = FakeSessionScript(answer="BRIDGE_OK")
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
        session.spawn = lambda command, **kwargs: FakeProcess(script)
        session._start()
        self.assertEqual(session.session_id, "mvs_test_1")
        answer = session.prompt("hello there")
        self.assertEqual(answer, "BRIDGE_OK")
        self.assertEqual(script.prompts, ["hello there"])

    def test_group_requires_mention(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--bot-open-id", "ou_bot"])
        bridge.seen = set()
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

    def test_p2p_needs_no_mention(self):
        bridge = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        bridge.args = make_args(["--bot-open-id", "ou_bot"])
        bridge.seen = set()
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
        self.assertEqual(out, "Sentence one.\u00a0 Sentence two.")

    def test_sentence_gap_collapses_two_ascii_spaces_to_the_wide_gap(self):
        out = bridge_mod.sentence_gap("Sentence one.  Sentence two.")
        self.assertEqual(out, "Sentence one.\u00a0 Sentence two.")

    def test_sentence_gap_leaves_colons_and_commas_alone(self):
        out = bridge_mod.sentence_gap("Note: value, then; more")
        self.assertEqual(out, "Note: value, then; more")

    def test_sentence_gap_leaves_inline_code_alone(self):
        out = bridge_mod.sentence_gap("Use `foo. bar` as the key. Then go.")
        self.assertIn("`foo. bar`", out)
        self.assertTrue(out.endswith("\u00a0 Then go."))

    def test_sentence_gap_leaves_fenced_code_alone(self):
        text = "Here:\n```\nx = 1.  y = 2\n```\nDone. Now."
        out = bridge_mod.sentence_gap(text)
        self.assertIn("x = 1.  y = 2", out)
        self.assertTrue(out.endswith("\u00a0 Now."))

    def test_sentence_gap_keeps_abbreviations_and_decimals(self):
        self.assertEqual(
            bridge_mod.sentence_gap("Dr. Smith is here. Next."), "Dr. Smith is here.\u00a0 Next."
        )
        self.assertEqual(bridge_mod.sentence_gap("Version 3.14 ships."), "Version 3.14 ships.")

    def test_lark_env_prepends_cli_prefix(self):
        env = bridge_mod.lark_env()
        self.assertIn(os.path.expanduser("~/.local/npm-global/bin"), env.get("PATH", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
