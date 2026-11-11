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


class PlatformBuilderTests(unittest.TestCase):
    """The server-side WR.Platform: the exclude narrows the pick, the deliver runs the command."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.good = "sess_1a2b3c4d-1111-2222-3333-444455556666"
        self.other = "sess_9999ffff-1111-2222-3333-444455556666"
        self.subagent = "sess_deadbeef-1111-2222-3333-444455556666"
        self.index = self.tmp / "tasks-index.sqlite"
        con = sqlite3.connect(self.index)
        con.execute("CREATE TABLE tasks (task_id TEXT, workspace_path TEXT)")
        con.execute("INSERT INTO tasks VALUES (?, ?)", (self.good, WORKSPACE))
        con.execute("INSERT INTO tasks VALUES (?, ?)", (self.other, "/other/repo"))
        con.commit()
        con.close()
        self.cli_db = self.tmp / "cli.sqlite"
        con = sqlite3.connect(self.cli_db)
        con.execute("CREATE TABLE session (id TEXT, task_type TEXT)")
        con.execute("INSERT INTO session VALUES (?, 'subagent_child')", (self.subagent,))
        con.commit()
        con.close()

    def _platform(self, runner):
        return zcode_wake.build_platform(
            env={"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"},
            workspace=WORKSPACE,
            zcode_bin="/usr/bin/true",
            index_path=str(self.index),
            cli_db=str(self.cli_db),
            runner=runner,
        )

    def test_exclude_narrows_pick(self):
        platform = self._platform(lambda argv, **k: None)
        targets = [
            WR.Target(session_id=self.good, letter="BB", generation=3),
            WR.Target(session_id=self.other, letter="BB", generation=3),
            WR.Target(session_id=self.subagent, letter="BB", generation=3),
        ]
        excluded = platform.exclude(targets)
        self.assertEqual(excluded, {self.other, self.subagent})

    def test_deliver_runs_command_and_reports_sent(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            from types import SimpleNamespace

            return SimpleNamespace(returncode=0)

        platform = self._platform(runner)
        delivery = platform.deliver(
            WR.Target(session_id=self.good, letter="BB", generation=3),
            WR.Wake(wake_id="w2-123456", message_id=1, link_note="The router linked this wake to this chat because the sender addressed its letter."),
        )
        self.assertEqual(delivery.outcome, "sent")
        argv = calls[0]
        self.assertEqual(argv[0], "/usr/bin/true")
        self.assertIn("--cwd", argv)
        self.assertIn(WORKSPACE, argv)
        self.assertIn("--resume", argv)
        self.assertIn(self.good, argv)


class RoundTripTests(unittest.TestCase):
    """The seam AA's review flagged: a request through the PLATFORMS table reaches the module
    and the answer's ids correlate with the sess_ pattern."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {
            "HOME": str(self.tmp),
            "PATH": "/usr/bin:/bin",
            # The round trip runs the real command path; /usr/bin/true exits 0 for any argv,
            # so the test proves the seam without a model call.
            "ZCODE_WAKE_BIN": "/usr/bin/true",
        }

    def test_round_trip_through_platforms_table(self):
        from simple_tools import claude_wake

        entry = claude_wake.PLATFORMS["zcode"]
        self.assertEqual(entry.session_re.pattern, "^sess_[A-Za-z0-9._-]{1,123}$")
        request = _request()
        with patch.object(
            zcode_wake.WR.registry_api, "call", return_value=_holding_row()
        ), patch.object(
            zcode_wake, "_tasks_workspace", return_value=("ok", WORKSPACE)
        ):
            answer = entry.deliver(request, self.env)
        self.assertEqual(answer["wake_id"], "w1-abcdef")
        self.assertEqual(answer["session_id"], SID)
        self.assertEqual(answer["generation"], 3)
        self.assertEqual(answer["delivery"]["outcome"], "sent")

    def test_table_refuses_bad_session_before_deliver(self):
        from simple_tools import claude_wake

        entry = claude_wake.PLATFORMS["zcode"]
        request = _request()
        request["target"]["session_id"] = "not-a-session"
        with patch.object(zcode_wake.subprocess, "run") as run_mock:
            answer = entry.deliver(request, self.env)
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        run_mock.assert_not_called()
