"""State store: modes, atomicity, monotonic cursors, the 500-id ledger, concurrency."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from agent_sync import state as S


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sync-state-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = self.tmp / "root"

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_layout_and_modes_even_with_a_permissive_umask(self) -> None:
        old = os.umask(0)
        try:
            state = S.State(self.root, "CLAUDE", "abcd1234")
            state.advance_cursor("agent-sync", "topic", 5)
            state.record_posted(6)
            state.advance_inbox(7)
        finally:
            os.umask(old)
        self.assertEqual(state.path, self.root / "CLAUDE" / "abcd1234" / "state.json")
        for directory in (self.root, self.root / "CLAUDE", self.root / "CLAUDE" / "abcd1234"):
            self.assertEqual(self.mode(directory), 0o700, directory)
        for file in (state.path, self.root / "CLAUDE" / "inbox.json"):
            self.assertEqual(self.mode(file), 0o600, file)
        data = json.loads(state.path.read_text())
        self.assertEqual(data["cursors"], {"agent-sync\u0000topic": 5})
        self.assertEqual(data["posted"], [6])

    def test_existing_loose_directories_of_ours_are_tightened_but_the_root_is_not(self) -> None:
        session_dir = self.root / "CLAUDE" / "abcd1234"
        session_dir.mkdir(parents=True)
        for directory in (self.root, self.root / "CLAUDE", session_dir):
            os.chmod(directory, 0o755)
        S.State(self.root, "CLAUDE", "abcd1234").record_posted(1)
        self.assertEqual(self.mode(self.root), 0o755)
        self.assertEqual(self.mode(self.root / "CLAUDE"), 0o700)
        self.assertEqual(self.mode(session_dir), 0o700)
        S.State(self.root, "CLAUDE", "abcd1234").advance_inbox(2)
        self.assertEqual(self.mode(self.root / "CLAUDE"), 0o700)

    def test_no_session_directory_name(self) -> None:
        state = S.State(self.root, "CLAUDE", None)
        state.record_posted(1)
        self.assertTrue((self.root / "CLAUDE" / "nosession" / "state.json").exists())

    def test_cursors_never_move_backwards_and_keys_ignore_case(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        state.advance_cursor("Agent-Sync", "Roll Call", 10)
        state.advance_cursor("agent-sync", "roll call", 4)
        self.assertEqual(state.cursor("AGENT-SYNC", "ROLL CALL"), 10)
        self.assertIsNone(state.cursor("agent-sync", "other"))

    def test_ledger_keeps_the_newest_500(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        state.record_posted(1)
        data = json.loads(state.path.read_text())
        data["posted"] = list(range(1, 601))  # seeded: 600 separate fsynced writes would only be slow
        state.path.write_text(json.dumps(data))
        state.record_posted(601)
        posted = state.posted()
        self.assertEqual(len(posted), 500)
        self.assertEqual((min(posted), max(posted)), (102, 601))

    def test_reads_create_nothing(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        self.assertIsNone(state.cursor("a", "b"))
        self.assertEqual(state.posted(), set())
        self.assertIsNone(state.inbox_cursor())
        self.assertFalse(self.root.exists())

    def test_a_corrupt_file_reads_as_empty_and_is_replaced_on_the_next_write(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        state.record_posted(1)
        state.path.write_text("{not json")
        self.assertEqual(state.posted(), set())
        state.record_posted(2)
        self.assertEqual(state.posted(), {2})

    def test_wrong_typed_content_does_not_crash(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        state.record_posted(1)
        state.path.write_text(json.dumps({"cursors": [1, 2], "posted": {"a": 1}}))
        self.assertIsNone(state.cursor("a", "b"))
        self.assertEqual(state.posted(), set())
        state.advance_cursor("a", "b", 3)
        self.assertEqual(state.cursor("a", "b"), 3)

    def test_concurrent_writers_lose_no_updates(self) -> None:
        def worker(index: int) -> None:
            state = S.State(self.root, "CLAUDE", "t")  # separate objects, like separate processes
            for step in range(4):
                state.advance_cursor("c", "topic%d" % index, step)
                state.record_posted(index * 100 + step)
                state.advance_inbox(index * 100 + step)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = S.State(self.root, "CLAUDE", "t")
        for index in range(5):
            self.assertEqual(state.cursor("c", "topic%d" % index), 3)
        self.assertEqual(len(state.posted()), 20)
        self.assertEqual(state.inbox_cursor(), 403)

    def test_inbox_is_per_seat(self) -> None:
        S.State(self.root, "CLAUDE", "a").advance_inbox(5)
        self.assertEqual(S.State(self.root, "CLAUDE", "b").inbox_cursor(), 5)
        self.assertIsNone(S.State(self.root, "CODEX", "a").inbox_cursor())

    def test_state_root_resolution(self) -> None:
        self.assertEqual(S.state_root({"AGENT_SYNC_STATE_DIR": str(self.tmp / "x")}), self.tmp / "x")
        self.assertEqual(S.state_root({"HOME": str(self.tmp)}), self.tmp / ".agent-sync")


if __name__ == "__main__":
    unittest.main()
