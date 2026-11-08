"""Z Code on `platform-letter`:  who takes a letter (the main agent's chats, not sub-agents or `zcode -p` runs), the
answer Z Code reads (hookSpecificOutput.additionalContext, and nothing for an event this adapter does not hook), and the
installer, which writes next to the lane guard in `~/.zcode/cli/config.json`.  The registry side runs in process
against a temporary registry, on the Mac's rules (SESSION_LETTER_LOCATION), with the process lookup replaced."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL
from agent_sync import platform_letter_zcode as PZ

SID = "sess_01K9ZCODE0000000000000001"
TUI_ARGS = ["/home/x/.zcode/server/bin/node", "/home/x/.zcode/server/zcode.cjs", "tui"]
PRINT_ARGS = ["/home/x/.zcode/server/bin/node", "/home/x/.zcode/server/zcode.cjs", "-p", "probe"]

# What ~/.zcode/cli/config.json holds on the server today:  the lane guard, in Claude Code's shape.
GUARD_CONFIG = {
    "hooks": {
        "events": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/x/lane-guard-hook --format claude", "timeout": 5}],
                }
            ]
        },
        "enabled": True,
    }
}


def payload(event: str, sid: str = SID, **extra) -> dict:
    """What Z Code 3.17.2 sends a hook:  its own fields spread out, plus the Claude-style names."""
    base = {
        "agentName": "zcode-agent",
        "agent_type": "zcode-agent",
        "cwd": "/home/x/apps/lanes/AI-Fleet-Coordinator/zcode-x",
        "hookEventName": event,
        "hook_event_name": event,
        "mode": "yolo",
        "permission_mode": "yolo",
        "sessionId": sid,
        "session_id": sid,
        "transcript_path": "/tmp/zcode-hook-x/transcript.jsonl",
    }
    if event == "SessionStart":
        base["source"] = "startup"
    if event == "UserPromptSubmit":
        base["prompt"] = "fix the failing login test"
    base.update(extra)
    return base


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()
        self.calls: list[list[str]] = []
        self.args = TUI_ARGS
        patcher = mock.patch.object(PZ, "zcode_arguments", side_effect=lambda *a, **k: list(self.args[2:]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "AGENT_SYNC_STATE_DIR": self.state, "SESSION_LETTER_LOCATION": "mac"}
        env.update(extra)
        return env

    def transport(self, argv: list[str]) -> dict:
        self.calls.append(list(argv))
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.env(), stdout=out)
        return json.loads(out.getvalue())

    def hook(self, data: dict, env: dict | None = None) -> dict:
        out, err = io.StringIO(), io.StringIO()
        code = PZ.hook_main(env or self.env(), io.StringIO(json.dumps(data)), out, transport=self.transport, stderr=err)
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text) if text else {}

    def lookup(self, sid: str = SID) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("zcode", "mac", PL.SCOPE, sid)
        finally:
            reg.close_db()


class HookTests(Base):
    def test_a_new_chat_gets_a_pair_letter_as_context(self) -> None:
        answer = self.hook(payload("SessionStart"))
        spec = answer["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "SessionStart")
        self.assertEqual(set(spec), {"hookEventName", "additionalContext"})  # Z Code's schema has no title field
        label = self.lookup()["label"]
        self.assertRegex(label, r"^[A-Z]{2}$")
        self.assertIn('"@**ZCode** %s"' % label, spec["additionalContext"])
        self.assertIn("does not carry the letter", spec["additionalContext"])

    def test_the_letter_is_kept_on_the_next_prompt(self) -> None:
        self.hook(payload("SessionStart"))
        first = self.lookup()["label"]
        self.hook(payload("UserPromptSubmit"))
        self.assertEqual(self.lookup()["label"], first)

    def test_a_subagent_takes_no_letter(self) -> None:
        answer = self.hook(payload("SessionStart", agentName="explore", agent_type="explore"))
        self.assertEqual(answer, {})
        self.assertIsNone(self.lookup())
        self.assertEqual(self.calls, [])

    def test_a_one_shot_run_takes_no_letter(self) -> None:
        self.args = PRINT_ARGS
        self.assertEqual(self.hook(payload("SessionStart")), {})
        self.assertIsNone(self.lookup())

    def test_an_unreadable_process_table_fails_closed(self) -> None:
        with mock.patch.object(PZ, "zcode_arguments", return_value=None):
            self.assertEqual(self.hook(payload("SessionStart")), {})
        self.assertIsNone(self.lookup())

    def test_an_event_it_does_not_hook_answers_nothing(self) -> None:
        self.assertEqual(self.hook(payload("Stop")), {})
        self.assertEqual(self.calls, [])

    def test_the_session_id_comes_from_the_environment_when_the_payload_has_none(self) -> None:
        data = payload("SessionStart")
        del data["session_id"], data["sessionId"]
        self.hook(data, env=self.env(ZCODE_SESSION_ID=SID))
        self.assertIsNotNone(self.lookup())

    def test_a_broken_payload_never_blocks_the_chat(self) -> None:
        out = io.StringIO()
        self.assertEqual(PZ.hook_main(self.env(), io.StringIO("{not json"), out, transport=self.transport), 0)
        self.assertEqual(out.getvalue(), "")


class KindTests(unittest.TestCase):
    def test_process_walk(self) -> None:
        rows = {10: (9, ["sh", "-c", "hook"]), 9: (8, ["node", "/x/zcode.cjs", "-p", "probe"])}
        with mock.patch.object(PZ, "process_row", side_effect=lambda pid: rows.get(pid, (0, []))):
            self.assertEqual(PZ.zcode_arguments(10), ["-p", "probe"])
        with mock.patch.object(PZ, "process_row", return_value=(0, [])):
            self.assertIsNone(PZ.zcode_arguments(10))

    def test_one_shot_flags(self) -> None:
        self.assertTrue(PZ.one_shot(["-p", "x"]))
        self.assertTrue(PZ.one_shot(["--prompt=x"]))
        self.assertFalse(PZ.one_shot(["tui"]))
        self.assertFalse(PZ.one_shot(["app-server", "--surface", "desktop"]))


class InstallTests(Base):
    def config(self) -> dict:
        with open(os.path.join(self.home, PZ.CONFIG_FILE)) as fh:
            return json.load(fh)

    def write_config(self, doc: dict) -> None:
        path = os.path.join(self.home, PZ.CONFIG_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def install(self, **kw) -> tuple[int, dict]:
        out = io.StringIO()
        opts = PL.InstallOptions("zcode", "server", "mac-host", kw.get("check", False), kw.get("remove", False))
        code = PL.install_json_hooks(PZ.INSTALL, opts, self.env(), out)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_it_installs_next_to_the_lane_guard_and_removes_cleanly(self) -> None:
        self.write_config(GUARD_CONFIG)
        code, _ = self.install()
        self.assertEqual(code, 0)
        events = self.config()["hooks"]["events"]
        self.assertEqual(events["PreToolUse"], GUARD_CONFIG["hooks"]["events"]["PreToolUse"])
        self.assertTrue(self.config()["hooks"]["enabled"])
        for event in PZ.HOOK_TIMEOUTS:
            command = events[event][0]["hooks"][0]["command"]
            self.assertIn("--platform zcode", command)
        code, answer = self.install(check=True)
        self.assertEqual(code, 0, answer)
        self.install(remove=True)
        events = self.config()["hooks"]["events"]
        self.assertEqual(events["PreToolUse"], GUARD_CONFIG["hooks"]["events"]["PreToolUse"])
        self.assertFalse(any(events.get(e) for e in PZ.HOOK_TIMEOUTS))


if __name__ == "__main__":
    unittest.main()
