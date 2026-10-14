from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_sync import chat_transport as CT
from agent_sync.chat_catalog import ChatCatalog, _stable_id


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeGrok:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.calls = 0
        self.kwargs = []

    def discover(self, **_kwargs):
        self.calls += 1
        self.kwargs.append(_kwargs)
        return list(self.rows)


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = 0
        self.excerpt_calls = 0

    def discover(self):
        self.calls += 1
        return list(self.rows)

    def excerpt(self, session_id, limit=4):
        self.excerpt_calls += 1
        return [{"role": "assistant", "text": "safe excerpt"},
                {"role": "tool", "text": "must not appear"}][:limit]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ChatCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = FakeClock()

    def catalog(self, **kwargs):
        return ChatCatalog(home=self.home, host="test-host", clock=self.clock, **kwargs)

    def test_one_shot_discovery_can_wait_for_grok_leader(self):
        grok = FakeGrok([{"session_id": "chat-1", "cwd": "/repo", "state": "idle",
                          "turn_ended_at": 1, "wake": True}])
        catalog = self.catalog(grok_transport=grok, cursor_transport=FakeCursor())
        rows = catalog.discover(wait_grok=True)
        self.assertTrue(any(row["session_id"] == "chat-1" for row in rows))
        self.assertEqual(grok.kwargs, [{"wait": True, "force": False, "limit": 160}])

    def test_forced_catalog_scan_forwards_to_grok_transport(self):
        grok = FakeGrok([{"session_id": "chat-1", "cwd": "/repo", "state": "idle",
                          "turn_ended_at": 1, "wake": True}])
        catalog = self.catalog(grok_transport=grok, cursor_transport=FakeCursor())
        catalog.discover(force=True, wait_grok=True)
        self.assertEqual(grok.kwargs, [{"wait": True, "force": True, "limit": 160}])

    def test_timed_out_forced_rescan_marks_the_previous_grok_row_stale(self):
        grok = FakeGrok([{"session_id": "chat-1", "cwd": "/repo", "state": "idle",
                          "turn_ended_at": 1, "wake": True, "title": "Work"}])
        catalog = self.catalog(grok_transport=grok, cursor_transport=FakeCursor())
        first = catalog.discover(wait_grok=True)
        self.assertTrue(next(row for row in first if row["session_id"] == "chat-1")["capabilities"]["wake"])

        class PendingGrok:
            def __init__(self) -> None:
                self.kwargs = None

            def discover(self, **kwargs):
                self.kwargs = kwargs
                raise RuntimeError("Grok leader discovery pending")

        pending = PendingGrok()
        catalog._grok = pending
        rows = catalog.discover(force=True, wait_grok=True, grok_timeout=CT.discovery_wait_budget())
        self.assertEqual(pending.kwargs["timeout"], CT.discovery_wait_budget())
        self.assertTrue(pending.kwargs["wait"])
        self.assertTrue(pending.kwargs["force"])
        again = next(row for row in rows if row["session_id"] == "chat-1")
        self.assertTrue(again["stale"])
        self.assertFalse(again["capabilities"]["wake"])

    def test_claude_codex_ids_and_text_allowlist(self):
        write_jsonl(self.home / ".claude/projects/p1/claude-session.jsonl", [
            {"type": "user", "cwd": "/work/project", "message": {"content": "hello api_key=secret-value"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"},
                                                                  {"type": "tool_use", "input": "secret tool"}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "private"}]}},
            {"type": "tool_result", "content": "tool output"},
        ])
        write_jsonl(self.home / ".codex/sessions/2026/rollout-codex-session.jsonl", [
            {"type": "session_meta", "payload": {"id": "codex-session", "cwd": "/repo", "title": "A Codex task"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": "hidden"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "read_file", "arguments": "hidden"}},
        ])
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        rows = catalog.discover()
        claude = next(row for row in rows if row["platform"] == "claude")
        codex = next(row for row in rows if row["platform"] == "codex")
        self.assertEqual(claude["id"], _stable_id("test-host", "claude", "claude-session"))
        self.assertEqual(codex["title"], "A Codex task")
        self.assertEqual(catalog.excerpt(claude["id"]), [
            {"role": "user", "text": "hello api_key=[REDACTED]"},
            {"role": "assistant", "text": "answer"},
        ])
        self.assertEqual(catalog.excerpt(codex["id"]), [{"role": "user", "text": "question"}])
        self.assertNotIn("internal_path", catalog.projection(claude["id"]))
        self.assertNotIn("_internal_path", catalog.projection(claude["id"]))
        self.assertIsNone(catalog.resolve("/etc/passwd"))

    def test_codex_header_survives_large_rollout_tail(self):
        path = self.home / ".codex/sessions/2026/rollout-placeholder.jsonl"
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"id": "real-session-id",
                                                                          "cwd": "/real/cwd"}}) + "\n")
            for _ in range(120):
                fh.write(json.dumps({"type": "response_item", "payload": {"type": "message",
                         "role": "assistant", "channel": "analysis", "content": "x" * 3000}}) + "\n")
            fh.write(json.dumps({"type": "response_item", "payload": {"type": "message",
                     "role": "assistant", "channel": "final", "content": [{"type": "output_text",
                     "text": "visible final"}]}}) + "\n")
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "codex")
        self.assertEqual(row["session_id"], "real-session-id")
        self.assertEqual(row["cwd"], "/real/cwd")
        self.assertEqual(catalog.excerpt(row["id"]), [{"role": "assistant", "text": "visible final"}])

    def test_discovery_is_ttl_cached_and_ids_are_stable(self):
        transport = FakeGrok([{"session_id": "session-a", "cwd": "/repo", "state": "idle",
                               "turn_ended_at": 1, "wake": True}])
        catalog = self.catalog(grok_transport=transport, cursor_transport=FakeCursor())
        first = catalog.discover()
        catalog.discover()
        self.assertEqual(transport.calls, 1)
        grok = next(row for row in first if row["platform"] == "grok-leader")
        self.assertTrue(grok["capabilities"]["wake"])
        catalog.discover(force=True)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(grok["id"], next(row for row in catalog.discover() if row["platform"] == "grok-leader")["id"])

    def test_catalog_cap_is_fair_across_platforms(self):
        for index in range(30):
            write_jsonl(self.home / f".claude/projects/project/session-{index}.jsonl", [
                {"type": "user", "message": {"content": f"question {index}"}}
            ])
        catalog = self.catalog(grok_transport=FakeGrok([
            {"session_id": "live-grok", "cwd": "/repo", "state": "idle", "turn_ended_at": 1, "wake": True}
        ]), cursor_transport=FakeCursor())
        rows = catalog.discover()
        self.assertLessEqual(len(rows), 24)
        self.assertTrue(any(row["platform"] == "grok-leader" and row["session_id"] == "live-grok" for row in rows))

    def test_grok_requires_live_idle_existing_nonself_session(self):
        rows = [
            {"session_id": "idle", "cwd": "/repo", "state": "idle", "turn_ended_at": 1, "wake": True},
            {"session_id": "busy", "cwd": "/repo", "state": "busy"},
            {"session_id": "self", "cwd": "/repo", "state": "idle", "turn_ended_at": 1, "wake": True, "self": True},
            {"session_id": "permission", "cwd": "/repo", "state": "idle", "turn_ended_at": 1,
             "wake": True, "pending_permission": True},
        ]
        catalog = self.catalog(grok_transport=FakeGrok(rows), cursor_transport=FakeCursor())
        got = [row for row in catalog.discover() if row["platform"] == "grok-leader"]
        self.assertEqual([row["capabilities"]["wake"] for row in got], [True, False, False, False])

    def test_grok_leader_chat_without_workspace_is_visible_but_not_wakeable(self):
        catalog = self.catalog(grok_transport=FakeGrok([
            {"session_id": "no-cwd", "cwd": None, "title": "Leader-only session", "updated_at": 99,
             "state": "idle", "turn_ended_at": 1, "wake": False}
        ]), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "grok-leader")
        self.assertEqual(row["session_id"], "no-cwd")
        self.assertIsNone(row["cwd"])
        self.assertEqual(row["title"], "Leader-only session")
        self.assertEqual(row["updated_at"], 99)
        self.assertFalse(row["capabilities"]["read"])
        self.assertFalse(row["capabilities"]["wake"])

    def test_disk_cwd_does_not_promote_transport_readonly_session_to_wakeable(self):
        sid = "leader-no-cwd"
        summary = self.home / ".grok/sessions/group" / sid / "summary.json"
        summary.parent.mkdir(parents=True)
        summary.write_text(json.dumps({"info": {"cwd": "/repo"}, "generated_title": "Old chat"}))
        catalog = self.catalog(grok_transport=FakeGrok([
            {"session_id": sid, "cwd": None, "state": "idle", "turn_ended_at": 1, "wake": False}
        ]), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "grok-leader")
        self.assertEqual(row["cwd"], "/repo")
        self.assertFalse(row["capabilities"]["wake"])

    def test_cursor_excerpt_filters_roles_and_cloud_wake_states(self):
        transport = FakeCursor([
            {"host": "cursor-cloud", "session_id": "cloud-1", "cwd": "cursor-cloud://no-repository", "state": "idle", "title": "Cloud task"},
            {"host": "cursor-cloud", "session_id": "cloud-2", "cwd": "https://example.test/repo", "state": "busy"},
            {"host": "cursor-cloud", "session_id": "cloud-3", "state": "archived"},
        ])
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=transport)
        rows = [row for row in catalog.discover() if row["platform"] == "cursor-cloud"]
        by_session = {row["session_id"]: row for row in rows}
        self.assertEqual([by_session[key]["capabilities"]["wake"] for key in ("cloud-1", "cloud-2", "cloud-3")],
                         [True, True, False])
        self.assertEqual(by_session["cloud-2"]["cwd"], "https://example.test/repo")
        self.assertEqual(by_session["cloud-1"]["host"], "cursor-cloud")
        self.assertEqual(transport.excerpt_calls, 0)
        self.assertEqual(catalog.excerpt(by_session["cloud-1"]["id"]),
                         [{"role": "assistant", "text": "safe excerpt"}])
        self.assertEqual(transport.excerpt_calls, 1)

    def test_cursor_local_is_metadata_only_and_never_wakeable(self):
        meta = self.home / ".cursor/acp-sessions/local-1/meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"cwd": "/repo", "title": "Local edit"}), encoding="utf-8")
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        rows = [row for row in catalog.discover() if row["platform"] == "cursor-local"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "local-1")
        self.assertEqual(rows[0]["status"], "metadata-only")
        self.assertFalse(rows[0]["capabilities"]["wake"])
        self.assertEqual(catalog.excerpt(rows[0]["id"]), [])

    def test_muse_reads_safe_sqlite_metadata_only(self):
        db = self.home / ".local/share/muse/session-index.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (ID TEXT, workspace TEXT, title TEXT, created_at TEXT, "
                     "updated_at TEXT, status TEXT, first_user_prompt TEXT, search_text TEXT)")
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     ("muse-session", "/repo", "Muse task", "2026-10-12T10:00:00Z", "2026-10-13T10:00:00Z",
                      "active", "do not expose this", "also private"))
        conn.commit()
        conn.close()
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "muse")
        self.assertEqual(row["session_id"], "muse-session")
        self.assertEqual(row["cwd"], "/repo")
        self.assertEqual(row["title"], "Muse task")
        self.assertFalse(row["capabilities"]["read"])
        self.assertEqual(catalog.excerpt(row["id"]), [])

    def test_minimax_metadata_and_text_only_message_excerpt(self):
        folder = self.home / ".minimax/v2/sessions/2026/10/13/09-00-00-ms-session_example"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"schemaVersion": 1, "sessionId": "mm-session-id",
                                                             "createdAtMs": 1791885600000,
                                                             "updatedAtMs": 1791889200000,
                                                             "layout": {}, "paths": {},
                                                             "title": "must not trust", "workspace": "/not-verified"}), encoding="utf-8")
        write_jsonl(folder / "messages.jsonl", [
            {"message_id": "m1", "message": {"role": "user", "content": "question"}},
            {"message_id": "m2", "message": {"role": "tool", "content": "tool result hidden"}},
            {"message_id": "m3", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "answer"}, {"type": "tool_result", "text": "hidden"}
            ]}},
        ])
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "minimax")
        self.assertEqual(row["session_id"], "mm-session-id")
        self.assertTrue(row["title"].startswith("MiniMax Code chat"))
        self.assertIsNone(row["cwd"])
        self.assertFalse(row["capabilities"]["wake"])
        self.assertEqual(catalog.excerpt(row["id"]), [
            {"role": "user", "text": "question"}, {"role": "assistant", "text": "answer"}
        ])

    def test_minimax_excerpt_budget_keeps_newest_messages(self):
        folder = self.home / ".minimax/v2/sessions/2026/10/13/09-00-00-ms-session_latest"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"sessionId": "mm-latest"}), encoding="utf-8")
        write_jsonl(folder / "messages.jsonl", [
            {"message": {"role": "assistant", "content": f"row-{index}:" + ("x" * 1592)}}
            for index in range(8)
        ])
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "minimax")
        excerpts = catalog.excerpt(row["id"], limit=4)
        self.assertEqual(len(excerpts), 4)
        self.assertTrue(excerpts[0]["text"].startswith("row-4:"))
        self.assertTrue(excerpts[-1]["text"].startswith("row-7:"))
        self.assertLessEqual(sum(len(item["text"]) for item in excerpts), 6000)

    def test_fx_is_metadata_only_without_unverified_event_parser(self):
        base = self.home / ".fx/sessions"
        (base / "fx-1").mkdir(parents=True)
        (base / "index.json").write_text(json.dumps({"schema_version": 1, "sessions": [{"id": "fx-1"}]}), encoding="utf-8")
        (base / "fx-1/session.json").write_text(json.dumps({"id": "fx-1", "title": "Fx task",
                                                               "workspace_root": "/repo"}), encoding="utf-8")
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "fx")
        self.assertEqual(row["title"], "Fx task")
        self.assertEqual(row["cwd"], "/repo")
        self.assertFalse(row["capabilities"]["read"])
        self.assertFalse(row["capabilities"]["wake"])
        self.assertEqual(catalog.excerpt(row["id"]), [])

    def test_symlinked_transcript_is_ignored_and_missing_stores_are_explicit(self):
        target = self.home / "outside.jsonl"
        write_jsonl(target, [{"type": "user", "message": {"content": "not in catalog"}}])
        parent = self.home / ".claude/projects/project"
        parent.mkdir(parents=True)
        try:
            os.symlink(target, parent / "linked.jsonl")
        except OSError:
            self.skipTest("symlinks unavailable")
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        rows = catalog.discover()
        claude = next(row for row in rows if row["platform"] == "claude")
        self.assertEqual(claude["status"], "unavailable")
        for platform in ("muse", "minimax", "fx", "antigravity", "cursor-local"):
            row = next(row for row in rows if row["platform"] == platform)
            self.assertEqual(row["status"], "unavailable")
            self.assertFalse(row["capabilities"]["wake"])

    def test_cloud_api_failure_is_visible_and_not_cached_as_live_session(self):
        class Broken:
            def discover(self):
                raise TimeoutError
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=Broken())
        row = next(row for row in catalog.discover() if row["platform"] == "cursor-cloud")
        self.assertEqual(row["status"], "unavailable")
        self.assertFalse(row["capabilities"]["wake"])


if __name__ == "__main__":
    unittest.main()
