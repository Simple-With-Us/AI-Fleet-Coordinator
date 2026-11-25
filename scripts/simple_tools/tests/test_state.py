"""State store: modes, atomicity, monotonic cursors, the 500-id ledger, concurrency."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from simple_tools import state as S


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="simple-tools-state-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = self.tmp / "root"

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    @unittest.skipIf(S.fcntl is None, "POSIX advisory locks required")
    def test_stream_publication_lock_is_session_scoped_and_released(self) -> None:
        import subprocess
        import sys

        state = S.State(self.root, "CLAUDE", "abcd1234")
        sibling = S.State(self.root, "CLAUDE", "different")
        probe = (
            "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); "
            "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )
        path = state.session_dir / "stream-post.lock"
        with self.assertRaisesRegex(RuntimeError, "fixture stop"):
            with state.stream_post_guard():
                with sibling.stream_post_guard():
                    state.record_posted(42)
                blocked = subprocess.run(
                    [sys.executable, "-c", probe, str(path)], capture_output=True
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn(b"BlockingIOError", blocked.stderr)
                raise RuntimeError("fixture stop")
        released = subprocess.run(
            [sys.executable, "-c", probe, str(path)], capture_output=True
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(state.posted(), {42})
        self.assertEqual(self.mode(path), 0o600)


    def test_stream_guard_reentry_uses_one_advisory_lock(self) -> None:
        state = S.State(self.root, "CLAUDE", "abcd1234")
        other_handle = S.State(self.root, "CLAUDE", "abcd1234")
        with state.stream_post_guard(timeout=0.1):
            with other_handle.stream_post_guard(timeout=0.1):
                state.record_posted(43)
        self.assertEqual(state.posted(), {43})

    def test_stream_guard_thread_wait_is_bounded_and_releases(self) -> None:
        state = S.State(self.root, "CLAUDE", "abcd1234")
        entered = threading.Event()
        release = threading.Event()
        def hold():
            with state.stream_post_guard():
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(TimeoutError):
                with state.stream_post_guard(timeout=0.03):
                    self.fail("entered stalled guard")
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        with state.stream_post_guard(timeout=0.1):
            pass

    @unittest.skipIf(S.fcntl is None, "POSIX advisory locks required")
    def test_stream_guard_process_wait_is_bounded(self) -> None:
        import subprocess
        import sys
        state = S.State(self.root, "CLAUDE", "abcd1234")
        with state.stream_post_guard():
            pass
        path = state.session_dir / "stream-post.lock"
        code = (
            "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); "
            "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()"
        )
        proc = subprocess.Popen([sys.executable, "-c", code, str(path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), b"ready")
            with self.assertRaises(TimeoutError):
                with state.stream_post_guard(timeout=0.03):
                    self.fail("entered other process guard")
            proc.communicate(timeout=2)
        except BaseException:
            proc.terminate()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise
        with state.stream_post_guard(timeout=0.1):
            pass

    def test_stream_guard_rejects_session_and_ancestor_symlinks_before_creation(self) -> None:
        target = self.tmp / "outside"
        target.mkdir()
        for part in ("session", "seat", "root"):
            with self.subTest(part=part):
                root = self.tmp / part
                state = S.State(root, "CLAUDE", "abcd1234")
                link = {"session": state.session_dir, "seat": state.seat_dir,
                        "root": root}[part]
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaises(PermissionError):
                    with state.stream_post_guard():
                        self.fail("entered redirected guard")
                self.assertEqual(list(target.iterdir()), [])

    def test_layout_and_modes_even_with_a_permissive_umask(self) -> None:
        old = os.umask(0)
        try:
            state = S.State(self.root, "CLAUDE", "abcd1234")
            state.advance_cursor("simple-tools", "topic", 5)
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
        self.assertEqual(data["cursors"], {"simple-tools\u0000topic": 5})
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

    def test_the_full_session_id_is_recorded_next_to_its_short_tag(self) -> None:
        # The tag in a post is 8 characters, so it cannot carry the id;  a resume-wake that reads
        # `[SEAT·tag]` needs this to find the session that wrote it.
        state = S.State(self.root, "CLAUDE", "abcd1234", session_id="abcd1234-full-session-id")
        state.record_posted(1)
        data = json.loads(state.path.read_text())
        self.assertEqual(data.get("session"), "abcd1234-full-session-id")

    def test_a_seat_with_no_session_id_records_none(self) -> None:
        state = S.State(self.root, "ECHO", None)
        state.record_posted(1)
        data = json.loads(state.path.read_text())
        self.assertNotIn("session", data)

    def test_cursors_never_move_backwards_and_keys_ignore_case(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        state.advance_cursor("Simple-Tools", "Roll Call", 10)
        state.advance_cursor("simple-tools", "roll call", 4)
        self.assertEqual(state.cursor("SIMPLE-TOOLS", "ROLL CALL"), 10)
        self.assertIsNone(state.cursor("simple-tools", "other"))

    def test_ledger_keeps_the_newest_500(self) -> None:
        state = S.State(self.root, "CLAUDE", "t")
        state.record_posted(1)
        data = json.loads(state.path.read_text())
        data["posted"] = list(
            range(1, 601)
        )  # seeded: 600 separate fsynced writes would only be slow
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
        self.assertEqual(
            S.state_root({"SIMPLE_TOOLS_STATE_DIR": str(self.tmp / "x")}), self.tmp / "x"
        )
        self.assertEqual(S.state_root({"HOME": str(self.tmp)}), self.tmp / ".simple-tools")


if __name__ == "__main__":
    unittest.main()
