from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import chat_transport as CT
from agent_sync.chat_catalog import MAX_FILES_PER_SOURCE, ChatCatalog, _stable_id


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
        return [
            {"role": "assistant", "text": "safe excerpt"},
            {"role": "tool", "text": "must not appear"},
        ][:limit]


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
        grok = FakeGrok(
            [
                {
                    "session_id": "chat-1",
                    "cwd": "/repo",
                    "state": "idle",
                    "turn_ended_at": 1,
                    "wake": True,
                }
            ]
        )
        catalog = self.catalog(grok_transport=grok, cursor_transport=FakeCursor())
        rows = catalog.discover(wait_grok=True)
        self.assertTrue(any(row["session_id"] == "chat-1" for row in rows))
        self.assertEqual(grok.kwargs, [{"wait": True, "force": False, "limit": 160}])

    def test_forced_catalog_scan_forwards_to_grok_transport(self):
        grok = FakeGrok(
            [
                {
                    "session_id": "chat-1",
                    "cwd": "/repo",
                    "state": "idle",
                    "turn_ended_at": 1,
                    "wake": True,
                }
            ]
        )
        catalog = self.catalog(grok_transport=grok, cursor_transport=FakeCursor())
        catalog.discover(force=True, wait_grok=True)
        self.assertEqual(grok.kwargs, [{"wait": True, "force": True, "limit": 160}])

    def test_timed_out_forced_rescan_marks_the_previous_grok_row_stale(self):
        grok = FakeGrok(
            [
                {
                    "session_id": "chat-1",
                    "cwd": "/repo",
                    "state": "idle",
                    "turn_ended_at": 1,
                    "wake": True,
                    "title": "Work",
                }
            ]
        )
        catalog = self.catalog(grok_transport=grok, cursor_transport=FakeCursor())
        first = catalog.discover(wait_grok=True)
        self.assertTrue(
            next(row for row in first if row["session_id"] == "chat-1")["capabilities"]["wake"]
        )

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
        write_jsonl(
            self.home / ".claude/projects/p1/claude-session.jsonl",
            [
                {
                    "type": "user",
                    "cwd": "/work/project",
                    "message": {"content": "hello api_key=secret-value"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "answer"},
                            {"type": "tool_use", "input": "secret tool"},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "thinking", "thinking": "private"}]},
                },
                {"type": "tool_result", "content": "tool output"},
            ],
        )
        write_jsonl(
            self.home / ".codex/sessions/2026/rollout-codex-session.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "codex-session", "cwd": "/repo", "title": "A Codex task"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "question"}],
                    },
                },
                {"type": "response_item", "payload": {"type": "reasoning", "summary": "hidden"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": "hidden",
                    },
                },
            ],
        )
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        rows = catalog.discover()
        claude = next(row for row in rows if row["platform"] == "claude")
        codex = next(row for row in rows if row["platform"] == "codex")
        self.assertEqual(claude["id"], _stable_id("test-host", "claude", "claude-session"))
        self.assertEqual(codex["title"], "A Codex task")
        self.assertEqual(
            catalog.excerpt(claude["id"]),
            [
                {"role": "user", "text": "hello api_key=[REDACTED]"},
                {"role": "assistant", "text": "answer"},
            ],
        )
        self.assertEqual(catalog.excerpt(codex["id"]), [{"role": "user", "text": "question"}])
        self.assertNotIn("internal_path", catalog.projection(claude["id"]))
        self.assertNotIn("_internal_path", catalog.projection(claude["id"]))
        self.assertIsNone(catalog.resolve("/etc/passwd"))

    def test_codex_index_title_uses_newest_metadata_and_transcript_priority(self):
        path = self.home / ".codex/sessions/rollout-test.jsonl"
        write_jsonl(
            path, [{"type": "session_meta", "payload": {"id": "thread-id", "cwd": "/repo"}}]
        )
        write_jsonl(
            self.home / ".codex/session_index.jsonl",
            [
                {
                    "id": "thread-id",
                    "thread_name": "New Title",
                    "updated_at": "2026-10-19T12:00:00Z",
                },
                {
                    "id": "thread-id",
                    "thread_name": "Old Title",
                    "updated_at": "2026-10-18T12:00:00Z",
                },
            ],
        )
        row = self.catalog()._scan_codex()[0]
        self.assertEqual(row["title"], "New Title")
        write_jsonl(
            path,
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "thread-id", "cwd": "/repo", "title": "Transcript Title"},
                }
            ],
        )
        self.assertEqual(self.catalog()._scan_codex()[0]["title"], "Transcript Title")

    def test_codex_index_title_scrubs_controls_and_secrets_and_missing_falls_back(self):
        path = self.home / ".codex/sessions/rollout-test.jsonl"
        write_jsonl(
            path, [{"type": "session_meta", "payload": {"id": "thread-id", "cwd": "/repo"}}]
        )
        self.assertEqual(self.catalog()._scan_codex()[0]["title"], "Codex chat rollout-test")
        write_jsonl(
            self.home / ".codex/session_index.jsonl",
            [
                {
                    "id": "thread-id",
                    "thread_name": "Title\napi_key=privatevalue\x00",
                    "updated_at": 1,
                },
            ],
        )
        row = self.catalog()._scan_codex()[0]
        self.assertNotIn("privatevalue", row["title"])
        self.assertNotIn("\n", row["title"])
        self.assertNotIn("\x00", row["title"])
        self.assertIn("[REDACTED]", row["title"])

    def test_codex_header_survives_large_rollout_tail(self):
        path = self.home / ".codex/sessions/2026/rollout-placeholder.jsonl"
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "real-session-id", "cwd": "/real/cwd"},
                    }
                )
                + "\n"
            )
            for _ in range(120):
                fh.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "channel": "analysis",
                                "content": "x" * 3000,
                            },
                        }
                    )
                    + "\n"
                )
            fh.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "channel": "final",
                            "content": [{"type": "output_text", "text": "visible final"}],
                        },
                    }
                )
                + "\n"
            )
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "codex")
        self.assertEqual(row["session_id"], "real-session-id")
        self.assertEqual(row["cwd"], "/real/cwd")
        self.assertEqual(
            catalog.excerpt(row["id"]), [{"role": "assistant", "text": "visible final"}]
        )

    def test_discovery_is_ttl_cached_and_ids_are_stable(self):
        transport = FakeGrok(
            [
                {
                    "session_id": "session-a",
                    "cwd": "/repo",
                    "state": "idle",
                    "turn_ended_at": 1,
                    "wake": True,
                }
            ]
        )
        catalog = self.catalog(grok_transport=transport, cursor_transport=FakeCursor())
        first = catalog.discover()
        catalog.discover()
        self.assertEqual(transport.calls, 1)
        grok = next(row for row in first if row["platform"] == "grok-leader")
        self.assertTrue(grok["capabilities"]["wake"])
        catalog.discover(force=True)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(
            grok["id"],
            next(row for row in catalog.discover() if row["platform"] == "grok-leader")["id"],
        )

    def test_catalog_cap_is_fair_across_platforms(self):
        for index in range(30):
            write_jsonl(
                self.home / f".claude/projects/project/session-{index}.jsonl",
                [{"type": "user", "message": {"content": f"question {index}"}}],
            )
        catalog = self.catalog(
            grok_transport=FakeGrok(
                [
                    {
                        "session_id": "live-grok",
                        "cwd": "/repo",
                        "state": "idle",
                        "turn_ended_at": 1,
                        "wake": True,
                    }
                ]
            ),
            cursor_transport=FakeCursor(),
        )
        rows = catalog.discover()
        self.assertLessEqual(len(rows), 24)
        self.assertTrue(
            any(
                row["platform"] == "grok-leader" and row["session_id"] == "live-grok"
                for row in rows
            )
        )

    def test_grok_requires_live_idle_existing_nonself_session(self):
        rows = [
            {
                "session_id": "idle",
                "cwd": "/repo",
                "state": "idle",
                "turn_ended_at": 1,
                "wake": True,
            },
            {"session_id": "busy", "cwd": "/repo", "state": "busy"},
            {
                "session_id": "self",
                "cwd": "/repo",
                "state": "idle",
                "turn_ended_at": 1,
                "wake": True,
                "self": True,
            },
            {
                "session_id": "permission",
                "cwd": "/repo",
                "state": "idle",
                "turn_ended_at": 1,
                "wake": True,
                "pending_permission": True,
            },
        ]
        catalog = self.catalog(grok_transport=FakeGrok(rows), cursor_transport=FakeCursor())
        got = [row for row in catalog.discover() if row["platform"] == "grok-leader"]
        self.assertEqual([row["capabilities"]["wake"] for row in got], [True, False, False, False])

    def test_grok_leader_chat_without_workspace_is_visible_but_not_wakeable(self):
        catalog = self.catalog(
            grok_transport=FakeGrok(
                [
                    {
                        "session_id": "no-cwd",
                        "cwd": None,
                        "title": "Leader-only session",
                        "updated_at": 99,
                        "state": "idle",
                        "turn_ended_at": 1,
                        "wake": False,
                    }
                ]
            ),
            cursor_transport=FakeCursor(),
        )
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
        catalog = self.catalog(
            grok_transport=FakeGrok(
                [
                    {
                        "session_id": sid,
                        "cwd": None,
                        "state": "idle",
                        "turn_ended_at": 1,
                        "wake": False,
                    }
                ]
            ),
            cursor_transport=FakeCursor(),
        )
        row = next(row for row in catalog.discover() if row["platform"] == "grok-leader")
        self.assertEqual(row["cwd"], "/repo")
        self.assertFalse(row["capabilities"]["wake"])

    def test_cursor_excerpt_filters_roles_and_cloud_wake_states(self):
        transport = FakeCursor(
            [
                {
                    "host": "cursor-cloud",
                    "session_id": "cloud-1",
                    "cwd": "cursor-cloud://no-repository",
                    "state": "idle",
                    "title": "Cloud task",
                },
                {
                    "host": "cursor-cloud",
                    "session_id": "cloud-2",
                    "cwd": "https://example.test/repo",
                    "state": "busy",
                },
                {"host": "cursor-cloud", "session_id": "cloud-3", "state": "archived"},
            ]
        )
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=transport)
        rows = [row for row in catalog.discover() if row["platform"] == "cursor-cloud"]
        by_session = {row["session_id"]: row for row in rows}
        self.assertEqual(
            [by_session[key]["capabilities"]["wake"] for key in ("cloud-1", "cloud-2", "cloud-3")],
            [True, True, False],
        )
        self.assertEqual(by_session["cloud-2"]["cwd"], "https://example.test/repo")
        self.assertEqual(by_session["cloud-1"]["host"], "cursor-cloud")
        self.assertEqual(transport.excerpt_calls, 0)
        self.assertEqual(
            catalog.excerpt(by_session["cloud-1"]["id"]),
            [{"role": "assistant", "text": "safe excerpt"}],
        )
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
        conn.execute(
            "CREATE TABLE sessions (ID TEXT, workspace TEXT, title TEXT, created_at TEXT, "
            "updated_at TEXT, status TEXT, first_user_prompt TEXT, search_text TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "muse-session",
                "/repo",
                "Muse task",
                "2026-10-12T10:00:00Z",
                "2026-10-13T10:00:00Z",
                "active",
                "do not expose this",
                "also private",
            ),
        )
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
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "sessionId": "mm-session-id",
                    "createdAtMs": 1791885600000,
                    "updatedAtMs": 1791889200000,
                    "layout": {},
                    "paths": {},
                    "title": "must not trust",
                    "workspace": "/not-verified",
                }
            ),
            encoding="utf-8",
        )
        write_jsonl(
            folder / "messages.jsonl",
            [
                {"message_id": "m1", "message": {"role": "user", "content": "question"}},
                {"message_id": "m2", "message": {"role": "tool", "content": "tool result hidden"}},
                {
                    "message_id": "m3",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "answer"},
                            {"type": "tool_result", "text": "hidden"},
                        ],
                    },
                },
            ],
        )
        catalog = self.catalog(grok_transport=FakeGrok(), cursor_transport=FakeCursor())
        row = next(row for row in catalog.discover() if row["platform"] == "minimax")
        self.assertEqual(row["session_id"], "mm-session-id")
        self.assertTrue(row["title"].startswith("MiniMax Code chat"))
        self.assertIsNone(row["cwd"])
        self.assertFalse(row["capabilities"]["wake"])
        self.assertEqual(
            catalog.excerpt(row["id"]),
            [{"role": "user", "text": "question"}, {"role": "assistant", "text": "answer"}],
        )

    def test_minimax_excerpt_budget_keeps_newest_messages(self):
        folder = self.home / ".minimax/v2/sessions/2026/10/13/09-00-00-ms-session_latest"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(
            json.dumps({"sessionId": "mm-latest"}), encoding="utf-8"
        )
        write_jsonl(
            folder / "messages.jsonl",
            [
                {"message": {"role": "assistant", "content": f"row-{index}:" + ("x" * 1592)}}
                for index in range(8)
            ],
        )
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
        (base / "index.json").write_text(
            json.dumps({"schema_version": 1, "sessions": [{"id": "fx-1"}]}), encoding="utf-8"
        )
        (base / "fx-1/session.json").write_text(
            json.dumps({"id": "fx-1", "title": "Fx task", "workspace_root": "/repo"}),
            encoding="utf-8",
        )
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

    def test_find_returns_an_older_claude_chat_beyond_the_catalog_cap(self):
        # More chats than the cap:  recency decides which rows discover() keeps, and
        # the oldest chat falls outside that window.  A direct by-id lookup still
        # finds that one chat, in the same public shape discover() produces.
        for index in range(20):
            write_jsonl(
                self.home / (".claude/projects/project/session-%02d.jsonl" % index),
                [
                    {"type": "custom-title", "customTitle": "Session %d" % index},
                    {
                        "type": "user",
                        "cwd": "/repo",
                        "timestamp": 1000 + index,
                        "message": {"content": "question api_key=secret-value"},
                    },
                ],
            )
        catalog = self.catalog(
            grok_transport=FakeGrok(), cursor_transport=FakeCursor(), max_records=20
        )
        rows = catalog.discover()
        claude_ids = [row["session_id"] for row in rows if row["platform"] == "claude"]
        self.assertIn("session-19", claude_ids)
        self.assertNotIn("session-00", claude_ids)
        self.assertEqual(claude_ids, sorted(claude_ids, reverse=True))
        found = catalog.find("claude", "session-00")
        self.assertIsNotNone(found)
        self.assertEqual(found["session_id"], "session-00")
        self.assertEqual(found["title"], "Session 0")
        self.assertEqual(found["cwd"], "/repo")
        self.assertEqual(found["updated_at"], 1000)
        self.assertEqual(found["id"], _stable_id("test-host", "claude", "session-00"))
        self.assertNotIn("secret-value", json.dumps(found))
        self.assertIsNone(catalog.find("claude", "session-missing"))
        self.assertIsNone(catalog.find("claude", "../session-00"))
        self.assertIsNone(catalog.find("cursor-cloud", "bc-missing"))

    def test_a_found_older_chat_still_resolves_after_a_rescan(self):
        # The wake that finds a tagged older chat goes on to resolve and excerpt it, so the
        # found row must stay readable even though discovery rewrites its own rows.
        for index in range(20):
            write_jsonl(
                self.home / (".claude/projects/project/session-%02d.jsonl" % index),
                [{"type": "user", "cwd": "/repo", "timestamp": 1000 + index, "message": {"content": "hi"}}],
            )
        catalog = self.catalog(
            grok_transport=FakeGrok(), cursor_transport=FakeCursor(), max_records=20
        )
        catalog.discover()
        found = catalog.find("claude", "session-00")
        self.assertIsNotNone(found)
        self.assertIsNone(catalog._records.get(found["id"]))
        catalog.discover(force=True)
        resolved = catalog.resolve(found["id"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["session_id"], "session-00")

    def test_a_found_chat_expires_with_the_catalog_ttl(self):
        # A found chat is kept only for the wake that found it.  Once it is older than the
        # catalog TTL it is dropped, so a chat deleted later is never resolved from memory.
        for index in range(20):
            write_jsonl(
                self.home / (".claude/projects/project/session-%02d.jsonl" % index),
                [{"type": "user", "cwd": "/repo", "timestamp": 1000 + index, "message": {"content": "hi"}}],
            )
        catalog = self.catalog(
            grok_transport=FakeGrok(), cursor_transport=FakeCursor(), max_records=20
        )
        catalog.discover()
        found = catalog.find("claude", "session-00")
        self.assertIsNotNone(catalog.resolve(found["id"]))
        (self.home / ".claude/projects/project/session-00.jsonl").unlink()
        self.clock.now += catalog.ttl + 1
        self.assertIsNone(catalog.resolve(found["id"]))
        self.assertIsNone(catalog.find("claude", "session-00"))

    def test_find_returns_an_older_muse_chat_from_sqlite_beyond_the_catalog_cap(self):
        db = self.home / ".local/share/muse/session-index.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE sessions (ID TEXT, workspace TEXT, title TEXT, updated_at TEXT)"
        )
        for index in range(20):
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (
                    "muse-%02d" % index,
                    "/repo",
                    "Muse %d" % index,
                    "2026-10-%02dT10:00:00Z" % (index + 1),
                ),
            )
        conn.commit()
        conn.close()
        catalog = self.catalog(
            grok_transport=FakeGrok(), cursor_transport=FakeCursor(), max_records=20
        )
        rows = catalog.discover()
        muse_ids = [row["session_id"] for row in rows if row["platform"] == "muse"]
        self.assertIn("muse-19", muse_ids)
        self.assertNotIn("muse-00", muse_ids)
        found = catalog.find("muse", "muse-00")
        self.assertIsNotNone(found)
        self.assertEqual(found["session_id"], "muse-00")
        self.assertEqual(found["title"], "Muse 0")
        self.assertEqual(found["cwd"], "/repo")
        self.assertEqual(found["id"], _stable_id("test-host", "muse", "muse-00"))
        self.assertIsNone(catalog.find("muse", "muse-missing"))

    def test_find_returns_an_older_codex_chat_beyond_the_catalog_cap(self):
        for index in range(20):
            write_jsonl(
                self.home / (".codex/sessions/rollout-%02d.jsonl" % index),
                [
                    {
                        "timestamp": 1000 + index,
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-%02d" % index,
                            "cwd": "/repo",
                            "title": "Codex %d" % index,
                        },
                    }
                ],
            )
        catalog = self.catalog(
            grok_transport=FakeGrok(), cursor_transport=FakeCursor(), max_records=20
        )
        rows = catalog.discover()
        codex_ids = [row["session_id"] for row in rows if row["platform"] == "codex"]
        self.assertIn("codex-19", codex_ids)
        self.assertNotIn("codex-00", codex_ids)
        found = catalog.find("codex", "codex-00")
        self.assertIsNotNone(found)
        self.assertEqual(found["session_id"], "codex-00")
        self.assertEqual(found["title"], "Codex 0")
        self.assertEqual(found["cwd"], "/repo")
        self.assertIsNone(catalog.find("codex", "codex-missing"))

    def test_find_covers_the_remaining_local_stores(self):
        folder = self.home / ".minimax/v2/sessions/2026/10/13/09-00-00-ms-session_find"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(
            json.dumps({"sessionId": "mm-find", "updatedAtMs": 1791889200000}),
            encoding="utf-8",
        )
        write_jsonl(
            folder / "messages.jsonl",
            [{"message": {"role": "user", "content": "question"}}],
        )
        fx_home = self.home / ".fx/sessions/fx-find"
        fx_home.mkdir(parents=True)
        (fx_home / "session.json").write_text(
            json.dumps({"id": "fx-find", "title": "Fx find", "workspace_root": "/repo"}),
            encoding="utf-8",
        )
        meta = self.home / ".cursor/acp-sessions/local-find/meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"cwd": "/repo", "title": "Local find"}), encoding="utf-8")
        catalog = self.catalog(
            grok_transport=FakeGrok(
                [
                    {
                        "session_id": "grok-find",
                        "cwd": "/repo",
                        "state": "idle",
                        "turn_ended_at": 1,
                        "wake": True,
                    }
                ]
            ),
            cursor_transport=FakeCursor(),
        )
        minimax = catalog.find("minimax", "mm-find")
        self.assertIsNotNone(minimax)
        self.assertEqual(minimax["platform"], "minimax")
        self.assertEqual(minimax["status"], "available")
        fx = catalog.find("fx", "fx-find")
        self.assertIsNotNone(fx)
        self.assertEqual(fx["title"], "Fx find")
        local = catalog.find("cursor", "local-find")
        self.assertIsNotNone(local)
        self.assertEqual(local["platform"], "cursor-local")
        self.assertEqual(local["title"], "Local find")
        grok = catalog.find("grok-leader", "grok-find")
        self.assertIsNotNone(grok)
        self.assertEqual(grok["platform"], "grok-leader")
        self.assertTrue(grok["capabilities"]["wake"])
        self.assertIsNone(catalog.find("minimax", "mm-missing"))
        self.assertIsNone(catalog.find("antigravity", "whatever"))

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


class OpenCodeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = FakeClock()

    def catalog(self, **kwargs):
        return ChatCatalog(home=self.home, host="test-host", clock=self.clock, **kwargs)

    def _db(self) -> Path:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT,"
                " directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                " time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "INSERT INTO session VALUES ('ses_1', 'p1', NULL, '/srv/app', 'Real chat', 1000)"
            )
            conn.execute(
                "INSERT INTO message VALUES ('msg_1', 'ses_1', 2000,"
                " '{\"role\": \"user\", \"time\": {\"created\": 2000}}')"
            )
            conn.execute(
                "INSERT INTO part VALUES ('prt_1', 'msg_1', 'ses_1', 2000,"
                " '{\"type\": \"text\", \"text\": \"hello fleet\"}')"
            )
            conn.execute(
                "INSERT INTO part VALUES ('prt_2', 'msg_1', 'ses_1', 2001,"
                " '{\"type\": \"tool\", \"state\": {\"status\": \"completed\"}}')"
            )
            conn.commit()
        finally:
            conn.close()
        return db

    def test_sqlite_store_yields_a_catalog_row(self):
        self._db()
        catalog = self.catalog()
        rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["seat"], "OPENCODE")
        self.assertEqual(row["session_id"], "ses_1")
        self.assertEqual(row["title"], "Real chat")
        self.assertEqual(row["cwd"], "/srv/app")
        self.assertEqual(row["updated_at"], 2.0)
        self.assertEqual(row["status"], "available")
        self.assertFalse(row["capabilities"]["wake"])
        self.assertEqual(row["capabilities"]["wake_status"], "unsupported")
        self.assertEqual(catalog.excerpt(row["id"]), [{"role": "user", "text": "hello fleet"}])

    def _legacy_tree(self, session: dict) -> None:
        base = self.home / ".local" / "share" / "opencode" / "storage" / "session" / "proj"
        base.mkdir(parents=True)
        (base / "ses_legacy.json").write_text(json.dumps(session), encoding="utf-8")

    def _db_v2(self, with_content_tables=True) -> Path:
        # The live store layout the plugin README records:  session_v2 plus the
        # newer content tables.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session_v2 (id TEXT, directory TEXT, title TEXT,"
                " time_created INTEGER, time_archived INTEGER)"
            )
            conn.execute(
                "INSERT INTO session_v2 VALUES ('ses_v2', '/srv/v2', 'V2 chat', 3000, NULL)"
            )
            if with_content_tables:
                conn.execute("CREATE TABLE session_message (id TEXT, session_id TEXT)")
            conn.commit()
        finally:
            conn.close()
        return db

    def test_session_v2_store_yields_rows(self):
        self._db_v2()
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["session_id"], "ses_v2")
        self.assertEqual(row["title"], "V2 chat")
        self.assertEqual(row["status"], "metadata-only")

    def test_session_v2_without_content_tables_is_metadata_only(self):
        # Registry rows only, nothing readable:  metadata-only, never available
        # with an empty excerpt list (daemon.py already skips metadata-only).
        self._db_v2(with_content_tables=False)
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["session_id"], "ses_v2")
        self.assertEqual(row["status"], "metadata-only")
        self.assertFalse(row["capabilities"]["read"])

    def test_registry_only_legacy_store_is_metadata_only(self):
        # Legacy shape with no message/part tables at all:  same degradation.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE message")
            conn.execute("DROP TABLE part")
            conn.commit()
        finally:
            conn.close()
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "metadata-only")
        self.assertFalse(row["capabilities"]["read"])

    def test_message_only_store_lookup_fails_closed(self):
        # Exactly one legacy content table is shape drift:  the by-id lookup
        # fails closed like discovery, never a degraded metadata-only row.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE part")
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(self.catalog()._find_opencode("ses_1"))

    def test_oversized_directory_is_not_materialized_in_lookup(self):
        # Identity and workspace path guards live in SQL:  an oversized or
        # non-TEXT directory must not load before the row is classified; the
        # lookup finds no row.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE session SET directory = ? WHERE id = 'ses_1'",
                (sqlite3.Binary(b"d" * (20 * 1024 * 1024)),),
            )
            conn.execute(
                "INSERT INTO session VALUES ('ses_big', 'p1', NULL, ?, 'Big', 1000)",
                ("d" * 2000,),
            )
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        self.assertIsNone(catalog._find_opencode("ses_1"))
        self.assertIsNone(catalog._find_opencode("ses_big"))

    def test_uri_significant_store_path_stays_read_only(self):
        # A home path containing URI-significant characters must not change the
        # SQLite URI:  without percent-encoding, "?" would drop mode=ro (opening
        # the store read-write) and "#" would resolve a different file.
        db = self._db()
        tricky_home = Path(self.tmp.name + "?mode=rw")
        tricky_db = tricky_home / ".local" / "share" / "opencode" / "opencode.db"
        tricky_db.parent.mkdir(parents=True)
        import shutil

        shutil.copy(db, tricky_db)
        catalog = ChatCatalog(home=tricky_home, host="test-host", clock=self.clock)
        rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["session_id"], "ses_1")
        self.assertEqual(row["status"], "available")
        # Proof the handle is read-only:  a write through the catalog's own
        # connection shape is rejected by SQLite.
        import sqlite3 as s3
        from urllib.parse import quote as q

        ro = s3.connect(f"file:{q(str(tricky_db))}?mode=ro", uri=True)
        try:
            with self.assertRaises(s3.OperationalError):
                ro.execute("CREATE TABLE nope (id TEXT)")
        finally:
            ro.close()

    def test_one_unreadable_project_does_not_abort_the_scan(self):
        base = self.home / ".local" / "share" / "opencode" / "storage" / "session"
        (base / "good").mkdir(parents=True)
        (base / "bad").mkdir(parents=True)
        (base / "good" / "ses_a.json").write_text(
            json.dumps({"id": "ses_a", "title": "A", "time": {"created": 1000}}), encoding="utf-8"
        )
        (base / "bad" / "ses_b.json").write_text(
            json.dumps({"id": "ses_b", "title": "B", "time": {"created": 2000}}), encoding="utf-8"
        )
        real_scandir = os.scandir

        def flaky(path):
            if Path(path) == base / "bad":
                raise OSError("permission denied")
            return real_scandir(path)

        with mock.patch("agent_sync.chat_catalog.os.scandir", side_effect=flaky):
            rows = self.catalog().discover()
        found = {r.get("session_id") for r in rows if r["platform"] == "opencode"}
        self.assertIn("ses_a", found)

    def test_find_opencode_by_id_in_db(self):
        self._db()
        catalog = self.catalog()
        row = catalog.find("opencode", "ses_1")
        self.assertIsNotNone(row)
        self.assertEqual(row["session_id"], "ses_1")
        self.assertEqual(row["title"], "Real chat")
        self.assertIsNone(catalog.find("opencode", "ses_missing"))

    def test_find_opencode_by_id_in_legacy_tree(self):
        self._legacy_tree({"id": "ses_legacy", "title": "Old", "time": {"created": 5000}})
        catalog = self.catalog()
        row = catalog.find("opencode", "ses_legacy")
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Old")
        self.assertIsNone(catalog.find("opencode", "ses_nope"))

    def test_db_path_present_but_unusable_never_falls_through_to_legacy(self):
        # A db path that EXISTS but cannot be used (here: a directory) is an
        # explicit "unusable", never a silent fallthrough to the legacy tree
        # while a real store marker is present (Kody L944).
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.mkdir(parents=True)
        self._legacy_tree({"id": "ses_legacy", "title": "Old", "time": {"created": 5000}})
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("unusable", row["error"])
        self.assertNotIn(
            "ses_legacy", [r.get("session_id") for r in rows if r["platform"] == "opencode"]
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_db_symlink_escaping_home_reports_unusable(self):
        outside = self.home.parent / (self.home.name + "-outside.db")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        outside.write_bytes(b"not a database")
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, db)
        self._legacy_tree({"id": "ses_legacy", "title": "Old", "time": {"created": 5000}})
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("unusable", row["error"])

    def test_operational_database_error_is_not_reported_as_schema_drift(self):
        # A corrupt file is an OPERATIONAL failure ("file is not a database"),
        # not shape drift:  it must not be collapsed into "unrecognized"
        # (Kody L1142).
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"this is not a sqlite database at all")
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("operational", row["error"])
        self.assertNotIn("unrecognized", row["error"])

    def test_locked_database_reports_operational_not_unrecognized(self):
        db = self._db()
        lock = sqlite3.connect(db)
        self.addCleanup(lock.close)
        lock.execute("BEGIN EXCLUSIVE")
        lock.execute("INSERT INTO session VALUES ('ses_x', 'p', NULL, '/x', 'X', 1)")
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("operational", row["error"])

    def test_legacy_empty_id_fails_closed(self):
        # An empty-string id is malformed metadata:  fail the platform closed,
        # never substitute path.stem or skip silently (Kody L1215).
        self._legacy_tree({"id": "", "title": "Old", "time": {"created": 5000}})
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("malformed", row["error"])

    def test_legacy_nonstring_id_fails_closed(self):
        self._legacy_tree({"id": 123, "title": "Old", "time": {"created": 5000}})
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("malformed", row["error"])

    def test_bulk_excerpts_cover_every_session_in_one_pass(self):
        # The set-based excerpt query must return each session's own excerpts
        # (Kody L1120):  two sessions, two texts, no cross-talk.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                " time_created INTEGER, data TEXT)"
            )
            conn.execute("INSERT INTO session VALUES ('ses_a', '/a', 'A', 1000)")
            conn.execute("INSERT INTO session VALUES ('ses_b', '/b', 'B', 2000)")
            conn.execute(
                "INSERT INTO message VALUES ('m_a', 'ses_a', 1500, '{\"role\": \"user\"}')"
            )
            conn.execute(
                "INSERT INTO message VALUES ('m_b', 'ses_b', 2500, '{\"role\": \"assistant\"}')"
            )
            conn.execute(
                "INSERT INTO part VALUES ('p_a', 'm_a', 'ses_a', 1500,"
                " '{\"type\": \"text\", \"text\": \"alpha text\"}')"
            )
            conn.execute(
                "INSERT INTO part VALUES ('p_b', 'm_b', 'ses_b', 2500,"
                " '{\"type\": \"text\", \"text\": \"beta text\"}')"
            )
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        rows = {r["session_id"]: r for r in catalog.discover() if r["platform"] == "opencode"}
        self.assertEqual(rows["ses_a"]["status"], "available")
        self.assertEqual(rows["ses_b"]["status"], "available")
        self.assertEqual(catalog.excerpt(rows["ses_a"]["id"]), [{"role": "user", "text": "alpha text"}])
        self.assertEqual(catalog.excerpt(rows["ses_b"]["id"]), [{"role": "assistant", "text": "beta text"}])

    def test_missing_store_reports_unavailable(self):
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertFalse(row["capabilities"]["read"])

    def test_unknown_database_schema_reports_unrecognized(self):
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE something_else (id TEXT)")
        conn.commit()
        conn.close()
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("unrecognized", row["error"])

    def test_legacy_json_tree_is_read_when_no_database(self):
        base = self.home / ".local" / "share" / "opencode" / "storage" / "session" / "proj"
        base.mkdir(parents=True)
        (base / "ses_9.json").write_text(
            json.dumps({"id": "ses_9", "title": "Old chat", "directory": "/old",
                        "time": {"created": 5000, "updated": 9000}}),
            encoding="utf-8",
        )
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["session_id"], "ses_9")
        self.assertEqual(row["title"], "Old chat")
        self.assertEqual(row["updated_at"], 9.0)

    def test_newer_content_shape_is_registry_only(self):
        # session_message/session_input are defined at the same pinned owning
        # revision (sql.ts @411eff73); their content layout is unqualified, so a
        # store carrying them must degrade to registry-only rows: metadata-only,
        # no content read, no excerpt claims.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,"
                " seq INTEGER, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "INSERT INTO session_message VALUES ('sm_1', 'ses_1', 'user', 1, 3000, '{}')"
            )
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "metadata-only")
        self.assertFalse(row["capabilities"]["read"])
        self.assertFalse(row["capabilities"]["wake"])
        # Activity stamp still comes from the window-bounded newer table.
        self.assertEqual(row["updated_at"], 3.0)
        self.assertEqual(catalog.excerpt(row["id"]), [])

    def test_archived_session_is_excluded(self):
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT,"
                " time_created INTEGER, time_archived INTEGER)"
            )
            conn.execute("INSERT INTO session VALUES ('ses_live', '/a', 'Live', 1000, NULL)")
            conn.execute("INSERT INTO session VALUES ('ses_arch', '/b', 'Archived', 2000, 1234)")
            conn.commit()
        finally:
            conn.close()
        rows = self.catalog().discover()
        ids = [r["session_id"] for r in rows if r["platform"] == "opencode"]
        self.assertIn("ses_live", ids)
        self.assertNotIn("ses_arch", ids)

    def test_window_bounded_by_creation_ordered_by_activity(self):
        # The discovery window is the MAX_FILES_PER_SOURCE most recently created
        # sessions.  An old but recently active chat outside the window stays
        # omitted (bounded-subset contract); inside the window, rows are
        # activity-ordered and outside-window messages never leak stamps in.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                " time_created INTEGER, data TEXT)"
            )
            total = MAX_FILES_PER_SOURCE + 5
            for i in range(total):
                conn.execute(
                    "INSERT INTO session VALUES (?, '/w', ?, ?)",
                    ("ses_%03d" % i, "Chat %d" % i, (i + 1) * 1000),
                )
            conn.execute(
                "INSERT INTO message VALUES ('m_out', 'ses_000', 999999000, '{}')"
            )
            conn.execute(
                "INSERT INTO message VALUES ('m_in', 'ses_100', 500000000, '{}')"
            )
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog(max_records=1000)
        rows = [
            r
            for r in catalog.discover()
            if r["platform"] == "opencode" and r["status"] == "available"
        ]
        ids = [r["session_id"] for r in rows]
        self.assertEqual(len(ids), MAX_FILES_PER_SOURCE)
        for omitted in ("ses_000", "ses_001", "ses_002", "ses_003", "ses_004"):
            self.assertNotIn(omitted, ids)
        self.assertEqual(ids[0], "ses_100")
        self.assertEqual(rows[0]["updated_at"], 500000.0)
        self.assertEqual(ids[1], "ses_164")
        self.assertEqual(rows[1]["updated_at"], 165.0)

    def test_malformed_part_yields_empty_excerpt_not_unavailable(self):
        # Row-level malformed content is best-effort: skipped, empty excerpt.
        # Fail-closed applies to store shape only, never to row content.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                " time_created INTEGER, data TEXT)"
            )
            conn.execute("INSERT INTO session VALUES ('ses_1', '/a', 'Chat', 1000)")
            conn.execute("INSERT INTO message VALUES ('msg_1', 'ses_1', 2000, 'broken {')")
            conn.execute("INSERT INTO part VALUES ('prt_1', 'msg_1', 'ses_1', 2000, 'also broken [')")
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "available")
        self.assertEqual(catalog.excerpt(row["id"]), [])

    def test_oversized_part_is_skipped_before_parse(self):
        # Payloads beyond the row byte guard are filtered in SQL, before any
        # JSON parse; the session stays available with an empty excerpt.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                " time_created INTEGER, data TEXT)"
            )
            conn.execute("INSERT INTO session VALUES ('ses_1', '/a', 'Chat', 1000)")
            conn.execute(
                "INSERT INTO message VALUES ('msg_1', 'ses_1', 2000,"
                ' \'{"role": "user"}\')'
            )
            big = '{"type": "text", "text": "' + "x" * 200000 + '"}'
            conn.execute("INSERT INTO part VALUES ('prt_1', 'msg_1', 'ses_1', 2000, ?)", (big,))
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "available")
        self.assertEqual(catalog.excerpt(row["id"]), [])

    def test_legacy_tree_read_error_reports_unavailable(self):
        base = self.home / ".local" / "share" / "opencode" / "storage" / "session"
        base.mkdir(parents=True)
        with mock.patch("agent_sync.chat_catalog.os.scandir", side_effect=OSError("denied")):
            rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("unreadable", row["error"])

    def test_one_platform_scan_error_is_isolated(self):
        # A raising scanner degrades only its own platform; every other platform
        # is still scanned fresh.
        self._db()
        catalog = self.catalog()
        with mock.patch.object(catalog, "_scan_muse", side_effect=RuntimeError("boom")):
            rows = catalog.discover()
        opencode = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(opencode["status"], "available")
        muse = next(r for r in rows if r["platform"] == "muse")
        self.assertEqual(muse["status"], "unavailable")
        self.assertIn("scan error", muse["error"])

    def test_malformed_part_table_fails_closed(self):
        # Reviewer repro: valid session/message tables but a part table missing
        # the columns this reader parses is shape drift:  fail closed as
        # "unrecognized", never an available session with silently empty
        # excerpts.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute("CREATE TABLE part (id TEXT, wrong TEXT)")
            conn.execute("INSERT INTO session VALUES ('ses_1', '/a', 'Chat', 1000)")
            conn.commit()
        finally:
            conn.close()
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("unrecognized", row["error"])
        self.assertFalse(row["capabilities"]["read"])

    def test_partial_content_shape_fails_closed(self):
        # Exactly one of the legacy content tables present is drift too.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute("INSERT INTO session VALUES ('ses_1', '/a', 'Chat', 1000)")
            conn.commit()
        finally:
            conn.close()
        rows = self.catalog().discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("unrecognized", row["error"])

    def test_byte_cap_counts_utf8_bytes_not_characters(self):
        # 40k four-byte characters = 160KB of UTF-8:  above the 64KB byte cap
        # even though it is below 64k characters.
        db = self._db()
        big = '{"type": "text", "text": "' + "\U0001F600" * 40000 + '"}'
        conn = sqlite3.connect(db)
        try:
            conn.execute("INSERT INTO part VALUES ('prt_mb', 'msg_1', 'ses_1', 2002, ?)", (big,))
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "available")
        self.assertEqual(
            catalog.excerpt(row["id"]), [{"role": "user", "text": "hello fleet"}]
        )

    def test_real_heavy_query_trips_work_budget(self):
        # A real query (no mocked instant trip):  a few hundred windowed sessions
        # plus messages, a small but real instruction budget, and the read aborts
        # as "store exceeds read budget" - never "unrecognized", never silently
        # empty.
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
                " time_created INTEGER, data TEXT)"
            )
            for i in range(300):
                conn.execute(
                    "INSERT INTO session VALUES (?, '/w', ?, ?)", ("ses_%03d" % i, "C%d" % i, i)
                )
                conn.execute(
                    "INSERT INTO message VALUES (?, ?, ?, '{}')", ("m_%03d" % i, "ses_%03d" % i, i)
                )
            conn.commit()
        finally:
            conn.close()
        catalog = self.catalog()
        with mock.patch("agent_sync.chat_catalog.OPENCODE_SQLITE_WORK_STEPS", 2000), mock.patch(
            "agent_sync.chat_catalog.OPENCODE_SQLITE_PROGRESS_STEPS", 100
        ):
            rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("exceeds read budget", row["error"])

    def test_deadline_trip_reports_store_heavy(self):
        self._db()
        catalog = self.catalog()
        with mock.patch("agent_sync.chat_catalog.OPENCODE_SQLITE_DEADLINE_SECONDS", 0), mock.patch(
            "agent_sync.chat_catalog.OPENCODE_SQLITE_PROGRESS_STEPS", 1
        ):
            rows = catalog.discover()
        row = next(r for r in rows if r["platform"] == "opencode")
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("exceeds read budget", row["error"])

    def test_overlong_id_sessions_are_dropped_not_truncated(self):
        # Two distinct sessions whose ids share the first 128 characters must
        # never collapse into one truncated row:  both are dropped by the
        # length guard, the normal session keeps its FULL original id.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            prefix = "ses_" + "a" * 126
            conn.execute(
                "INSERT INTO session VALUES (?, 'p1', NULL, '/x', 'Chat X', 600)",
                (prefix + "X",),
            )
            conn.execute(
                "INSERT INTO session VALUES (?, 'p1', NULL, '/y', 'Chat Y', 700)",
                (prefix + "Y",),
            )
            conn.commit()
        finally:
            conn.close()
        rows = [
            r
            for r in self.catalog(max_records=1000).discover()
            if r["platform"] == "opencode" and r["status"] != "unavailable"
        ]
        ids = [r["session_id"] for r in rows]
        self.assertEqual(ids, ["ses_1"])  # full original id, no truncation
        self.assertNotIn(prefix, "".join(ids))

    def test_overlong_directory_session_is_dropped(self):
        # A truncated workspace path would be a wrong workspace for preflight:
        # the session is dropped, never emitted with a clipped cwd.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO session VALUES ('ses_longdir', 'p1', NULL, ?, 'Long dir', 800)",
                ("/" + "d" * 2000,),
            )
            conn.commit()
        finally:
            conn.close()
        rows = [
            r
            for r in self.catalog(max_records=1000).discover()
            if r["platform"] == "opencode" and r["status"] != "unavailable"
        ]
        ids = [r["session_id"] for r in rows]
        self.assertIn("ses_1", ids)
        self.assertNotIn("ses_longdir", ids)

    def test_oversized_title_is_clipped_but_session_kept(self):
        # Title is display-only:  clipping it is honest and the session stays.
        db = self._db()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO session VALUES ('ses_bigtitle', 'p1', NULL, '/bt', ?, 900)",
                ("T" * 1_000_000,),
            )
            conn.commit()
        finally:
            conn.close()
        rows = [
            r
            for r in self.catalog(max_records=1000).discover()
            if r["platform"] == "opencode" and r["status"] != "unavailable"
        ]
        row = next(r for r in rows if r["session_id"] == "ses_bigtitle")
        self.assertLessEqual(len(row["title"]), 180)

    def test_legacy_enumeration_is_bounded(self):
        # More ses_*.json files than the cap in one project:  exactly the cap is
        # enumerated, no error, no unbounded walk.
        base = self.home / ".local" / "share" / "opencode" / "storage" / "session" / "proj"
        base.mkdir(parents=True)
        for i in range(MAX_FILES_PER_SOURCE + 40):
            (base / ("ses_%03d.json" % i)).write_text(
                json.dumps({"id": "ses_%03d" % i, "title": "Chat %d" % i,
                            "time": {"created": 1000 + i}}),
                encoding="utf-8",
            )
        rows = [
            r
            for r in self.catalog(max_records=1000).discover()
            if r["platform"] == "opencode" and r["status"] == "available"
        ]
        self.assertEqual(len(rows), MAX_FILES_PER_SOURCE)


class AntigravityCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.clock = FakeClock()

    def catalog(self, **kwargs):
        return ChatCatalog(
            home=self.home,
            host="test-host",
            clock=self.clock,
            grok_transport=FakeGrok(),
            cursor_transport=FakeCursor(),
            **kwargs,
        )

    def test_antigravity_scanner_and_find_read_summaries_db(self) -> None:
        db = self.home / ".gemini/antigravity-cli/conversation_summaries.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        with conn:
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT,"
                " workspace_uris TEXT, killed INTEGER DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "4aade78e-6a38-40ed-9fc2-11a6eeac6189",
                    "Operator Setup",
                    "2026-11-08T00:00:00Z",
                    "2026-11-08T01:00:00Z",
                    '["file:///mnt/fleet-extra/Code/AI-Fleet-Coordinator"]',
                    0,
                ),
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "cd6b94bc-8497-4f06-974b-9e247440c923",
                    "Killed Chat",
                    "2026-11-08T00:00:00Z",
                    "2026-11-08T02:00:00Z",
                    '["/repo"]',
                    1,
                ),
            )
        conn.close()
        cat = self.catalog()
        rows = [r for r in cat.discover() if r["platform"] == "antigravity"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["session_id"], "4aade78e-6a38-40ed-9fc2-11a6eeac6189")
        self.assertEqual(row["seat"], "AG")
        self.assertEqual(row["title"], "Operator Setup")
        self.assertEqual(row["cwd"], "/mnt/fleet-extra/Code/AI-Fleet-Coordinator")
        self.assertEqual(row["status"], "metadata-only")
        self.assertFalse(row["capabilities"]["wake"])
        found = cat.find("antigravity", "4aade78e-6a38-40ed-9fc2-11a6eeac6189")
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "Operator Setup")
        self.assertIsNone(cat.find("antigravity", "cd6b94bc-8497-4f06-974b-9e247440c923"))
        self.assertIsNone(cat.find("antigravity", "missing"))
