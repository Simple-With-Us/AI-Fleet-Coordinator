"""Tests for the Operator's chat-thread registry (thread_registry.py).

Hermetic:  the store lives in a temp dir and the sheet is an in-memory adapter with the
documented protocol, so no Google call and no Zulip read happens here.  The cases pin the
promises the design note makes:  one row per (stream, topic) with case-folded topics,
message-id dedupe across duplicate deliveries, deterministic extraction, idempotent
upserts, duplicate-key handling that never deletes, header validation before any write,
and a failed write that stops the sync and reports itself partial.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_sync import thread_registry as TR  # noqa: E402

T0 = 1_763_000_000.0  # a fixed clock so statuses and stamps are deterministic


def message(mid, *, stream_id=10, stream="Work-In-Progress", topic="AFC-412 letters",
            sender_email="muse-assist-bot@simplewithus.zulipchat.com",
            sender_name="Muse Assist", ts=T0, content="hello", is_bot=True):
    return {
        "id": mid,
        "type": "stream",
        "stream_id": stream_id,
        "display_recipient": stream,
        "subject": topic,
        "sender_id": 4242,
        "sender_email": sender_email,
        "sender_full_name": sender_name,
        "sender_is_bot": is_bot,
        "timestamp": ts,
        "content": content,
    }


class FakeAdapter:
    """In-memory sheet:  rows are 1-based with the header at row 1, like the real tab."""

    def __init__(self, headers=None, rows=None, fail_on=None):
        self.headers = list(TR.HEADERS) if headers is None else headers
        self.rows = [list(r) for r in (rows or [])]  # data rows only; rowNumber = index + 2
        self.fail_on = fail_on  # "update" | "append" | None
        self.writes = 0

    def validate_headers(self):
        return list(self.headers)

    def read_rows(self):
        return [
            {"rowNumber": i + 2, "cells": list(cells)} for i, cells in enumerate(self.rows)
        ]

    def append_row(self, cells):
        self.writes += 1
        if self.fail_on == "append":
            raise RuntimeError("append blew up")
        self.rows.append(list(cells))
        return len(self.rows) + 1

    def update_row(self, row_number, cells):
        self.writes += 1
        if self.fail_on == "update":
            raise RuntimeError("update blew up")
        self.rows[row_number - 2] = list(cells)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "operator", "thread-registry.json")
        self.store = TR.ThreadRegistryStore(self.path)

    def test_observe_counts_once_per_message_id(self):
        msg = message(100)
        self.assertTrue(self.store.observe(msg))
        self.assertFalse(self.store.observe(msg))  # duplicate delivery to another seat
        self.assertFalse(self.store.observe(dict(msg)))  # replay through backfill
        rows = self.store.rows(now=T0 + 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][7], "1")  # Message Count

    def test_topics_fold_case_insensitively_but_keep_latest_casing(self):
        self.store.observe(message(1, topic="AFC-412 Letters"))
        self.store.observe(message(2, topic="afc-412 letters"))
        rows = self.store.rows(now=T0 + 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], TR.thread_key(10, "AFC-412 Letters"))
        self.assertEqual(rows[0][3], "afc-412 letters")  # latest casing wins
        self.assertEqual(rows[0][7], "2")

    def test_participants_use_seat_tags_and_human_names(self):
        self.store.observe(message(1))
        self.store.observe(message(2, sender_email="kimi-bot@simplewithus.zulipchat.com",
                                   sender_name="Kimi"))
        self.store.observe(message(3, sender_email="jay@simplewithus.zulipchat.com",
                                   sender_name="Jay Wedgeworth", is_bot=False))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[5], "JAY, KIMI, MA")  # sorted tags;  KIMI per the owner's ruling
        self.assertEqual(row[6], "3")

    def test_zcode_bot_yields_zc_tag(self):
        self.store.observe(message(1, sender_email="zc-bot@simplewithus.zulipchat.com",
                                   sender_name="Zcode"))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[5], "ZC")

    def test_first_last_and_last_id_track_the_extremes(self):
        self.store.observe(message(5, ts=T0 + 500))
        self.store.observe(message(3, ts=T0 + 100))  # out-of-order delivery
        row = self.store.rows(now=T0 + 600)[0]
        self.assertEqual(row[8], TR._iso(T0 + 100))
        self.assertEqual(row[9], TR._iso(T0 + 500))
        self.assertEqual(row[10], "5")

    def test_extraction_is_deterministic(self):
        content = ("see AFC-412 and CT-2442, PR #1258, "
                   "https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/1268 "
                   "and Simple-With-Us/BotFleet#1317 plus AFC-412 again")
        self.store.observe(message(1, content=content))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[11], "AFC-412, CT-2442")
        self.assertEqual(
            row[12],
            "PR #1258, Simple-With-Us/AI-Fleet-Coordinator#1268, Simple-With-Us/BotFleet#1317",
        )

    def test_non_stream_and_malformed_messages_are_ignored(self):
        self.assertFalse(self.store.observe({"id": 1, "type": "private", "timestamp": T0}))
        self.assertFalse(self.store.observe({"id": 1, "type": "stream", "timestamp": T0}))
        self.assertFalse(self.store.observe({"type": "stream", "stream_id": 1, "timestamp": T0}))
        self.assertEqual(self.store.records(), [])

    def test_status_derivation(self):
        self.assertEqual(TR.status_for(" anything", T0, T0 + 60), "active")
        self.assertEqual(TR.status_for(" anything", T0, T0 + 2 * 24 * 3600), "idle")
        self.assertEqual(TR.status_for(" anything", T0, T0 + 40 * 24 * 3600), "dormant")
        self.assertEqual(TR.status_for("✔ done topic", T0, T0 + 60), "closed")

    def test_store_survives_reopen(self):
        self.store.observe(message(1))
        again = TR.ThreadRegistryStore(self.path)
        self.assertEqual(len(again.records()), 1)
        self.assertFalse(again.observe(message(1)))  # dedupe set persisted too
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], TR.STORE_VERSION)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TR.ThreadRegistryStore(os.path.join(self.tmp.name, "t.json"))
        self.store.observe(message(1, content="AFC-412 work"))
        self.store.observe(message(2, topic="release notes", ts=T0 + 10,
                                   sender_email="kimi-bot@simplewithus.zulipchat.com",
                                   sender_name="Kimi"))

    def test_first_sync_appends_then_second_is_a_noop(self):
        adapter = FakeAdapter()
        first = TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        self.assertEqual((first["appended"], first["updated"], first["failed"]), (2, 0, 0))
        writes_after_first = adapter.writes
        second = TR.sync_to_sheet(self.store, adapter, now=T0 + 3600)
        self.assertEqual(second["unchanged"], 2)
        self.assertEqual(adapter.writes, writes_after_first)  # idempotent:  nothing rewritten

    def test_changed_thread_updates_its_row_only(self):
        adapter = FakeAdapter()
        TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        self.store.observe(message(3, ts=T0 + 120))  # more activity in the first thread
        result = TR.sync_to_sheet(self.store, adapter, now=T0 + 180)
        self.assertEqual((result["updated"], result["appended"], result["unchanged"]), (1, 0, 1))
        key = TR.thread_key(10, "AFC-412 letters")
        row = next(r for r in adapter.rows if r[0] == key)
        self.assertEqual(row[7], "2")

    def test_header_mismatch_writes_nothing(self):
        adapter = FakeAdapter(headers=["Wrong"] + TR.HEADERS[1:])
        with self.assertRaises(TR.SheetHeaderError):
            TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        self.assertEqual(adapter.writes, 0)
        self.assertEqual(adapter.rows, [])

    def test_failed_write_stops_and_reports_partial(self):
        adapter = FakeAdapter(fail_on="append")
        result = TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        self.assertTrue(result["partial"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["appended"], 0)
        self.assertIn("append blew up", result["error"])

    def test_duplicate_keys_in_sheet_are_reported_and_left_alone(self):
        adapter = FakeAdapter()
        TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        adapter.rows.append(list(adapter.rows[0]))  # a hand-made duplicate row
        result = TR.sync_to_sheet(self.store, adapter, now=T0 + 120)
        self.assertEqual(len(result["duplicate_rows"]), 1)
        self.assertEqual(len(adapter.rows), 3)  # never deleted by the sync

    def test_human_purpose_survives_sync(self):
        adapter = FakeAdapter()
        TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        key = TR.thread_key(10, "AFC-412 letters")
        row = next(r for r in adapter.rows if r[0] == key)
        row[TR.COL_PURPOSE] = "letters for every seat"  # a human types a purpose in
        self.store.observe(message(9, ts=T0 + 300))
        result = TR.sync_to_sheet(self.store, adapter, now=T0 + 360)
        self.assertEqual(result["failed"], 0)
        row = next(r for r in adapter.rows if r[0] == key)
        self.assertEqual(row[TR.COL_PURPOSE], "letters for every seat")

    def test_key_collision_between_topics_that_differ_only_by_case(self):
        self.store.observe(message(50, topic="Release Notes", ts=T0 + 20))
        adapter = FakeAdapter()
        result = TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        keys = [r[0] for r in adapter.rows]
        self.assertEqual(len(keys), len(set(keys)))  # one row per folded key, never two
        self.assertEqual(result["appended"], 2)  # "release notes" thread already existed


class LinearIssuesTest(unittest.TestCase):
    """The Linear Issues column (index 11):  a key mentioned in a thread's topic name or in
    any message body joins that thread's row as one comma-separated list — sorted,
    deduplicated, accumulated as new mentions are observed, and never removed once added.
    Ordinary text produces no key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TR.ThreadRegistryStore(os.path.join(self.tmp.name, "t.json"))

    def test_extraction_collects_every_key_in_a_body(self):
        found = TR.extract_linear_issues("landed CT-2442, back to AFC-412 and UM-1863")
        self.assertEqual(found, ["AFC-412", "CT-2442", "UM-1863"])

    def test_topic_name_alone_contributes_its_key(self):
        self.store.observe(message(1, topic="AFC-565 linkifiers", content="no keys in here"))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[11], "AFC-565")

    def test_topic_and_body_mentions_merge_into_one_list(self):
        self.store.observe(message(1, topic="AFC-412 letters", content="also CT-2442"))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[11], "AFC-412, CT-2442")

    def test_repeated_mentions_are_deduplicated(self):
        self.store.observe(message(1, topic="AFC-412 letters",
                                   content="AFC-412 AFC-412, see AFC-412 again"))
        self.store.observe(message(2, topic="AFC-412 letters",
                                   content="AFC-412 once more"))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[11], "AFC-412")

    def test_keys_accumulate_and_are_never_removed(self):
        self.store.observe(message(1, topic="letters", content="starting AFC-412"))
        self.store.observe(message(2, topic="letters", content="nothing to add"))
        self.store.observe(message(3, topic="letters", content="now also CT-2442"))
        self.store.observe(message(4, topic="letters", content="quiet again"))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[11], "AFC-412, CT-2442")

    def test_accumulation_lands_in_the_sheet_across_sync_runs(self):
        adapter = FakeAdapter()
        self.store.observe(message(1, topic="letters", content="starting AFC-412"))
        first = TR.sync_to_sheet(self.store, adapter, now=T0 + 60)
        self.assertEqual(first["appended"], 1)
        self.assertEqual(adapter.rows[0][11], "AFC-412")
        self.store.observe(message(2, topic="letters", content="picked up CT-2442 too"))
        second = TR.sync_to_sheet(self.store, adapter, now=T0 + 120)
        self.assertEqual((second["updated"], second["appended"]), (1, 0))
        self.assertEqual(len(adapter.rows), 1)  # same row updated, never a second row
        self.assertEqual(adapter.rows[0][11], "AFC-412, CT-2442")
        third = TR.sync_to_sheet(self.store, adapter, now=T0 + 180)
        self.assertEqual(third["unchanged"], 1)  # settled:  a quiet re-run writes nothing

    def test_non_issue_text_yields_no_keys(self):
        text = ("the follow-up lands 2026-11-03; afc-412 in lowercase is not a key, "
                "neither is a bare 412, an AFC- with no number, or well-known prose")
        self.assertEqual(TR.extract_linear_issues(text), [])
        self.store.observe(message(1, topic="release notes", content=text))
        row = self.store.rows(now=T0 + 60)[0]
        self.assertEqual(row[11], "")


if __name__ == "__main__":
    unittest.main()
