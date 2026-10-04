#!/usr/bin/env python3
"""Unit tests for cursor chat surface helpers.  No network, no secrets."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cursor_chat_surfaces as ccs  # noqa: E402


class GithubUrlTests(unittest.TestCase):
    def test_ssh(self) -> None:
        self.assertEqual(
            ccs.github_https_url("git@github.com:Simple-With-Us/AI-Fleet-Coordinator.git"),
            "https://github.com/Simple-With-Us/AI-Fleet-Coordinator",
        )

    def test_https(self) -> None:
        self.assertEqual(
            ccs.github_https_url("https://github.com/Simple-With-Us/DealDex.git"),
            "https://github.com/Simple-With-Us/DealDex",
        )

    def test_ssh_scheme(self) -> None:
        self.assertEqual(
            ccs.github_https_url("ssh://git@github.com/Simple-With-Us/Usage-Monitor.git"),
            "https://github.com/Simple-With-Us/Usage-Monitor",
        )

    def test_rejects_non_github(self) -> None:
        self.assertIsNone(ccs.github_https_url("git@gitlab.com:org/repo.git"))


class PromptExtractTests(unittest.TestCase):
    def test_string(self) -> None:
        self.assertEqual(ccs.extract_prompt_text("  hello  "), "hello")

    def test_blocks(self) -> None:
        prompt = [
            {"type": "text", "text": "Fix the board"},
            {"type": "resource", "resource": {"uri": "file:///tmp/x.py"}},
        ]
        self.assertEqual(ccs.extract_prompt_text(prompt), "Fix the board\nfile:///tmp/x.py")

    def test_empty(self) -> None:
        self.assertEqual(ccs.extract_prompt_text([]), "")
        self.assertEqual(ccs.extract_prompt_text(None), "")


class AgentUrlTests(unittest.TestCase):
    def test_id(self) -> None:
        self.assertEqual(
            ccs.agent_web_url("bc-abc"),
            "https://cursor.com/agents/bc-abc",
        )

    def test_passthrough_url(self) -> None:
        url = "https://cursor.com/agents/bc-abc"
        self.assertEqual(ccs.agent_web_url(url), url)


class UnwrapTests(unittest.TestCase):
    def test_nested_agent_and_run(self) -> None:
        payload = {
            "agent": {"id": "bc-1", "url": "https://cursor.com/agents/bc-1"},
            "run": {"id": "run-9"},
        }
        agent = ccs.unwrap_created_agent(payload)
        self.assertEqual(agent["id"], "bc-1")
        self.assertEqual(agent["latestRunId"], "run-9")


class ShellularMergeTests(unittest.TestCase):
    def test_keeps_existing_custom_agents(self) -> None:
        existing = {
            "disabled": [],
            "custom": [
                {"id": "grok-build", "name": "Grok", "command": "/tmp/grok", "args": []},
                {"id": "kimi", "name": "Kimi Code", "command": "kimi", "args": ["acp"]},
            ],
        }
        merged = ccs.merge_shellular_agents(existing)
        ids = [row["id"] for row in merged["custom"]]
        self.assertIn("grok-build", ids)
        self.assertIn("kimi", ids)
        self.assertIn("cursor", ids)
        self.assertIn("cursor-local", ids)
        self.assertIn("deepseek", ids)
        cursor = next(row for row in merged["custom"] if row["id"] == "cursor")
        self.assertTrue(any("cursor_acp_cloud_bridge.py" in str(a) for a in cursor["args"]))
        deepseek = next(row for row in merged["custom"] if row["id"] == "deepseek")
        self.assertIn("dsh-acp.sh", deepseek["command"])
        self.assertNotIn("DEEPSEEK_API_KEY", deepseek.get("env") or {})

    def test_updates_existing_cursor_row(self) -> None:
        existing = {
            "custom": [
                {"id": "cursor", "name": "old", "command": "cursor-agent", "args": ["acp"]},
            ]
        }
        merged = ccs.merge_shellular_agents(existing)
        cursor_rows = [row for row in merged["custom"] if row["id"] == "cursor"]
        self.assertEqual(len(cursor_rows), 1)
        self.assertNotEqual(cursor_rows[0]["command"], "cursor-agent")


    def test_strips_embedded_deepseek_key(self) -> None:
        existing = {
            "custom": [
                {
                    "id": "deepseek",
                    "command": "dsh",
                    "args": ["acp"],
                    "env": {"DEEPSEEK_API_KEY": "should-not-survive"},
                }
            ]
        }
        merged = ccs.merge_shellular_agents(existing)
        deepseek = next(row for row in merged["custom"] if row["id"] == "deepseek")
        self.assertNotIn("DEEPSEEK_API_KEY", deepseek.get("env") or {})
        self.assertIn("dsh-acp.sh", deepseek["command"])


class LoadApiKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_api = os.environ.pop("CURSOR_API_KEY", None)
        self.old_sync = os.environ.pop("CURSOR_SYNC_API_KEY", None)
        self.old_global = ccs.GLOBAL_KEYS_FILE
        self.old_secrets = ccs.SECRET_FILES
        ccs.GLOBAL_KEYS_FILE = Path("/tmp/cursor-chat-surfaces-no-global-keys")
        ccs.SECRET_FILES = ()

    def tearDown(self) -> None:
        ccs.GLOBAL_KEYS_FILE = self.old_global
        ccs.SECRET_FILES = self.old_secrets
        for name, old in (
            ("CURSOR_API_KEY", self.old_api),
            ("CURSOR_SYNC_API_KEY", self.old_sync),
        ):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def test_env(self) -> None:
        os.environ["CURSOR_API_KEY"] = "cursor_test_key"
        self.assertEqual(ccs.load_api_key(), "cursor_test_key")

    def test_sync_env(self) -> None:
        os.environ["CURSOR_SYNC_API_KEY"] = "cursor_sync_test_key"
        self.assertEqual(ccs.load_api_key(), "cursor_sync_test_key")

    def test_file_plain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursor-api-key"
            path.write_text("cursor_from_file\n", encoding="utf-8")
            ccs.SECRET_FILES = (path,)
            self.assertEqual(ccs.load_api_key(), "cursor_from_file")

    def test_global_keys_sync_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "global-api-keys"
            path.write_text('CURSOR_SYNC_API_KEY="from_global"\n', encoding="utf-8")
            ccs.GLOBAL_KEYS_FILE = path
            self.assertEqual(ccs.load_api_key(), "from_global")


class SaveJsonTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            ccs.save_json(path, {"ids": ["bc-1"]})
            self.assertEqual(ccs.load_json(path, {}), {"ids": ["bc-1"]})


if __name__ == "__main__":
    unittest.main()
