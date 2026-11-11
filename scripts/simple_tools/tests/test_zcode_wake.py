"""Tests for the zcode wake delivery (OPS-1048):  checks before the spawn, outcomes after."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_tools import wake_router as WR
from simple_tools import zcode_wake

WORKSPACE = "/mnt/fleet-extra/Code/AI-Fleet-Coordinator"
SID = "sess_1a2b3c4d-1111-2222-3333-444455556666"


def _request(letter: str = "BB", generation: int = 3, wake_id: str = "w1-abcdef"):
    return {
        "platform": "zcode",
        "target": {
            "session_id": SID,
            "letter": letter,
            "host": "server",
            "title": "BB: Finish and Merge Draft PRs",
            "thread_link": "https://simplewithus.zulipchat.com/#task/1",
            "generation": generation,
        },
        "wake": {
            "wake_id": wake_id,
            "seat": "ZC",
            "message_id": 639428919,
            "channel": "Work-In-Progress",
            "topic": "OPS-1048 ZCode letters and wakes",
            "zulip_link": "https://simplewithus.zulipchat.com/narrow/topic/1",
            "sender": "Claude",
            "letter": letter,
            "picked": "letter",
            "reason": "letter",
            "link_note": "The router linked this wake to this chat because the sender addressed its letter.",
        },
    }


def _holding_row():
    return {
        "found": True,
        "native_session_id": SID,
        "seat": "ZC",
        "label": "BB",
        "generation": 3,
        "verified_host": "server",
        "state": "active",
    }


class ZcodeWakeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.index = self.tmp / "tasks-index.sqlite"
        con = sqlite3.connect(self.index)
        con.execute("CREATE TABLE tasks (task_id TEXT, workspace_path TEXT)")
        con.execute("INSERT INTO tasks VALUES (?, ?)", (SID, WORKSPACE))
        con.commit()
        con.close()
        patcher = patch.object(
            zcode_wake.WR.registry_api, "call", return_value=_holding_row()
        )
        self.registry = patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        self.locks = self.tmp / "locks"

    def _run(self, exit_code: int = 0):
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess_completed(argv, exit_code)

        self.calls = calls
        return runner

    def _deliver(self, request=None, exit_code=0, workspace=WORKSPACE):
        return zcode_wake.deliver_request(
            request or _request(),
            self.env,
            workspace=workspace,
            zcode_bin="/usr/bin/true",
            index_path=str(self.index),
            lock_dir=str(self.locks),
            runner=self._run(exit_code),
        )

    def test_delivers_sent_when_workspace_matches(self):
        answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "sent")
        self.assertEqual(answer["session_id"], SID)
        self.assertEqual(answer["generation"], 3)
        argv = self.calls[0]
        self.assertIn("--cwd", argv)
        self.assertIn(WORKSPACE, argv)
        self.assertIn("--resume", argv)
        self.assertIn(SID, argv)
        self.assertTrue(any("wake id w1-abcdef" in str(part) for part in argv))
        self.assertTrue(any("the sender addressed its letter" in str(part) for part in argv))

    def test_workspace_mismatch_refuses_without_running(self):
        con = sqlite3.connect(self.index)
        con.execute("UPDATE tasks SET workspace_path = ? WHERE task_id = ?", ("/other/repo", SID))
        con.commit()
        con.close()
        answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "workspace-mismatch")
        self.assertEqual(self.calls, [])

    def test_workspace_unknown_refuses_without_running(self):
        con = sqlite3.connect(self.index)
        con.execute("DELETE FROM tasks WHERE task_id = ?", (SID,))
        con.commit()
        con.close()
        answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "workspace-unknown")
        self.assertEqual(self.calls, [])

    def test_nonzero_exit_is_refused(self):
        answer = self._deliver(exit_code=3)
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "command-failed-3")

    def test_registry_release_refuses(self):
        with patch.object(
            zcode_wake.WR.registry_api, "call", return_value={"found": False}
        ):
            answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], WR.RELEASED)
        self.assertEqual(self.calls, [])

    def test_registry_unavailable_fails_before_spawn(self):
        with patch.object(
            zcode_wake.WR.registry_api,
            "call",
            side_effect=RuntimeError("registry down"),
        ):
            answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "failed")
        self.assertEqual(answer["delivery"]["code"], "registry-unavailable")
        self.assertEqual(self.calls, [])

    def test_bad_generation_refused(self):
        answer = self._deliver(_request(generation=0))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")

    def test_lowercase_letter_refused(self):
        answer = self._deliver(_request(letter="bb"))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")

    def test_bad_wake_id_refused(self):
        answer = self._deliver(_request(wake_id="not ok!"))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")


def subprocess_completed(argv, exit_code):
    from types import SimpleNamespace

    return SimpleNamespace(returncode=exit_code)


if __name__ == "__main__":
    unittest.main()
