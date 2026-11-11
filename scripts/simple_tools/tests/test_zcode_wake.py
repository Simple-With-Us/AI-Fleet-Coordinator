"""Tests for the zcode wake delivery (OPS-1048):  checks before the spawn, outcomes after."""

from __future__ import annotations

import itertools
import json
import sqlite3
import subprocess
import unittest
from pathlib import Path

from simple_tools import zcode_wake


def _request(session_id: str = "sess_1a2b3c4d-1111-2222-3333-444455556666", letter: str = ""):
    return {
        "platform": "zcode",
        "target": {
            "session_id": session_id,
            "letter": letter,
            "host": "server",
            "title": "BB: Finish and Merge Draft PRs",
            "thread_link": "https://simplewithus.zulipchat.com/#task/1",
            "generation": 3,
        },
        "wake": {
            "wake_id": "w1-abcdef",
            "seat": "ZC",
            "message_id": 639428919,
            "channel": "Work-In-Progress",
            "topic": "OPS-1048 ZCode letters and wakes",
            "zulip_link": "https://simplewithus.zulipchat.com/narrow/topic/1",
            "sender": "Claude",
            "letter": letter,
        },
    }


_index_counter = itertools.count()


def _index(tmp: Path, rows: list[tuple[str, str]]) -> Path:
    import itertools
    path = tmp / ("tasks-index-%d.sqlite" % next(_index_counter))
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (task_id TEXT, workspace_path TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return path


class ZcodeWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.tmp_path = Path(self.tmp)
        self.calls: list[list[str]] = []
        self.index = _index(
            self.tmp_path,
            [(_request()["target"]["session_id"], "/mnt/fleet-extra/Code/AI-Fleet-Coordinator")],
        )
        self.env = {"HOME": str(self.tmp_path), "PATH": "/usr/bin:/bin"}

    def _run(self, exit_code: int = 0):
        def runner(argv, **kwargs):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, exit_code)

        return runner

    def test_delivers_completed_when_workspace_matches(self):
        answer = zcode_wake.deliver_request(
            _request(),
            self.env,
            workspace="/mnt/fleet-extra/Code/AI-Fleet-Coordinator",
            index_path=str(self.index),
            zcode_bin="/usr/bin/true",
            runner=self._run(0),
            lock_dir=str(self.tmp_path / "locks"),
        )
        self.assertEqual(answer["delivery"]["outcome"], "completed")
        self.assertEqual(answer["session_id"], _request()["target"]["session_id"])
        self.assertEqual(answer["generation"], 3)
        argv = self.calls[0]
        self.assertIn("--cwd", argv)
        self.assertIn("/mnt/fleet-extra/Code/AI-Fleet-Coordinator", argv)
        self.assertIn("--resume", argv)
        self.assertIn("-p", argv)
        self.assertTrue(any("wake id w1-abcdef" in part for part in argv))

    def test_workspace_mismatch_refuses_without_running(self):
        self.index = _index(
            self.tmp_path, [(_request()["target"]["session_id"], "/mnt/fleet-extra/Code/Other")]
        )
        answer = zcode_wake.deliver_request(
            _request(),
            self.env,
            workspace="/mnt/fleet-extra/Code/AI-Fleet-Coordinator",
            index_path=str(self.index),
            zcode_bin="/usr/bin/true",
            runner=self._run(0),
            lock_dir=str(self.tmp_path / "locks"),
        )
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "workspace-mismatch")
        self.assertEqual(self.calls, [])

    def test_workspace_unknown_refuses(self):
        self.index = _index(self.tmp_path, [("sess_other-1111-2222-3333-444455556666", "/x")])
        answer = zcode_wake.deliver_request(
            _request(),
            self.env,
            index_path=str(self.index),
            zcode_bin="/usr/bin/true",
            runner=self._run(0),
            lock_dir=str(self.tmp_path / "locks"),
        )
        self.assertEqual(answer["delivery"]["code"], "workspace-mismatch")
        self.assertEqual(self.calls, [])

    def test_nonzero_exit_is_refused(self):
        answer = zcode_wake.deliver_request(
            _request(),
            self.env,
            index_path=str(self.index),
            zcode_bin="/usr/bin/true",
            runner=self._run(3),
            lock_dir=str(self.tmp_path / "locks"),
        )
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "command-failed-3")

    def test_bad_request_refused(self):
        answer = zcode_wake.deliver_request(
            {"platform": "zcode", "target": {"session_id": "not-a-session"}, "wake": {"wake_id": "w"}},
            self.env,
            index_path=str(self.index),
            lock_dir=str(self.tmp_path / "locks"),
        )
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")

    def test_registry_unavailable_fails_before_spawn(self):
        index = _index(self.tmp_path, [(_request()["target"]["session_id"], "/mnt/fleet-extra/Code/AI-Fleet-Coordinator")])
        request = _request()
        request["target"]["letter"] = "ZZ"  # resolve will miss; registry read itself is what we force
        answer = zcode_wake.deliver_request(
            request,
            self.env,
            index_path=str(index),
            zcode_bin="/usr/bin/true",
            runner=self._run(0),
            lock_dir=str(self.tmp_path / "locks"),
        )
        # With no registry reachable, a lettered target fails closed before the spawn.
        self.assertEqual(answer["delivery"]["outcome"], "failed")
        self.assertIn("registry", answer["delivery"]["code"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
