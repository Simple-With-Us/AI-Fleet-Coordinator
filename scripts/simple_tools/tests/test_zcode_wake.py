"""Tests for the zcode wake delivery (OPS-1048):  checks before the spawn, outcomes after.

The late-fence, fail-closed-store and bridge-lock tests are written to fail against the
pre-fix behavior (AA's re-review 639462267, items 1-3)."""

from __future__ import annotations

import fcntl
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simple_tools import wake_router as WR
from simple_tools import zcode_wake

WORKSPACE = "/mnt/fleet-extra/Code/AI-Fleet-Coordinator"
SID = "sess_1a2b3c4d-1111-2222-3333-444455556666"
LOCK_DIR = "~/.local/state/zc-wake"


def _request(letter: str = "BB", generation: int = 3, wake_id: str = "w1-abcdef",
             host: str = "server"):
    return {
        "platform": "zcode",
        "target": {
            "session_id": SID,
            "letter": letter,
            "host": host,
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
            "link_note": (
                "The router linked this wake to this chat because the sender addressed its letter."
            ),
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


def _completed(argv, **kwargs):
    return SimpleNamespace(returncode=0)


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
        self.registry = patch.object(
            zcode_wake.WR.registry_api, "call", return_value=_holding_row()
        )
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        self.locks = self.tmp / "zc-wake"

    def _deliver(self, request=None, exit_code=0, workspace=WORKSPACE, index=None):
        return zcode_wake.deliver_request(
            request or _request(),
            self.env,
            workspace=workspace,
            zcode_bin="/usr/bin/true",
            index_path=str(index or self.index),
            lock_dir=str(self.locks),
            runner=self._runner(exit_code),
        )

    def _runner(self, exit_code: int = 0):
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=exit_code)

        self.calls = calls
        return runner

    def test_delivers_sent_when_workspace_matches(self):
        answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "sent")
        self.assertEqual(answer["session_id"], SID)
        self.assertEqual(answer["generation"], 3)
        argv = self.calls[0]
        self.assertIn("--cwd", argv)
        self.assertIn(WORKSPACE, argv)
        self.assertIn("--resume", argv)
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
        with patch.object(zcode_wake.WR.registry_api, "call", return_value={"found": False}):
            answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], WR.RELEASED)
        self.assertEqual(self.calls, [])

    def test_registry_unavailable_fails_before_spawn(self):
        with patch.object(zcode_wake.WR.registry_api, "call",
                          side_effect=RuntimeError("registry down")):
            answer = self._deliver()
        self.assertEqual(answer["delivery"]["outcome"], "failed")
        self.assertEqual(answer["delivery"]["code"], "registry-unavailable")
        self.assertEqual(self.calls, [])

    def test_bad_generation_refused(self):
        answer = self._deliver(_request(generation=0))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")
        self.assertEqual(answer["session_id"], SID)

    def test_lowercase_letter_refused(self):
        answer = self._deliver(_request(letter="bb"))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")

    def test_bad_wake_id_refused(self):
        answer = self._deliver(_request(wake_id="not ok!"))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "bad-request")

    def test_foreign_host_refused_typed(self):
        answer = self._deliver(_request(host="mac"))
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "host-not-configured")
        self.assertEqual(self.calls, [])


class LateFenceTests(unittest.TestCase):
    """Item 2: the registry is re-read under the lock, immediately before the spawn."""

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
        self.env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        self.locks = self.tmp / "zc-wake"

    def test_release_after_first_check_stops_the_spawn(self):
        holding = _holding_row()
        released = dict(_holding_row(), found=False)
        with patch.object(zcode_wake.WR.registry_api, "call",
                          side_effect=[holding, released]):
            calls: list[list[str]] = []

            def runner(argv, **kwargs):
                calls.append(list(argv))
                return SimpleNamespace(returncode=0)

            answer = zcode_wake.deliver_request(
                _request(),
                self.env,
                workspace=WORKSPACE,
                zcode_bin="/usr/bin/true",
                index_path=str(self.index),
                lock_dir=str(self.locks),
                runner=runner,
            )
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], WR.RELEASED)
        self.assertEqual(calls, [])


class BridgeLockTests(unittest.TestCase):
    """Item 3: the plugin contends on the bridge's own lock file, so a second writer is busy."""

    def test_bridge_lock_held_means_busy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        index = tmp_path / "tasks-index.sqlite"
        con = sqlite3.connect(index)
        con.execute("CREATE TABLE tasks (task_id TEXT, workspace_path TEXT)")
        con.execute("INSERT INTO tasks VALUES (?, ?)", (SID, WORKSPACE))
        con.commit()
        con.close()
        lock_dir = tmp_path / "zc-wake"
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = lock_dir / ("session-%s.lock" % SID)
        with patch.object(zcode_wake.WR.registry_api, "call", return_value=_holding_row()):
            holder = open(lock_path, "a", encoding="utf-8")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                calls: list[list[str]] = []

                def runner(argv, **kwargs):
                    calls.append(list(argv))
                    return SimpleNamespace(returncode=0)

                answer = zcode_wake.deliver_request(
                    _request(),
                    {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                    workspace=WORKSPACE,
                    zcode_bin="/usr/bin/true",
                    index_path=str(index),
                    lock_dir=str(lock_dir),
                    runner=runner,
                )
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()
        self.assertEqual(answer["delivery"]["outcome"], "busy")
        self.assertEqual(answer["delivery"]["code"], "session-busy")
        self.assertEqual(calls, [])


class StoreFailClosedTests(unittest.TestCase):
    """Item 1: an unreadable store holds the wake (`failed`), never widens into a delivery."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.junk = self.tmp / "junk.sqlite"
        self.junk.write_bytes(b"this is not a database\n")
        self.env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}

    def test_unreadable_stores_fail_before_running(self):
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0)

        platform = zcode_wake.build_platform(
            env=self.env,
            workspace=WORKSPACE,
            zcode_bin="/usr/bin/true",
            index_path=str(self.junk),
            cli_db=str(self.junk),
            runner=runner,
        )
        delivery = platform.deliver(
            WR.Target(session_id=SID, letter="BB", host="server", generation=3),
            WR.Wake(wake_id="w2-123456", message_id=1),
        )
        self.assertEqual(delivery.outcome, "failed")
        self.assertEqual(calls, [])

    def test_exclude_still_narrows_when_stores_read(self):
        index = self.tmp / "tasks-index.sqlite"
        con = sqlite3.connect(index)
        con.execute("CREATE TABLE tasks (task_id TEXT, workspace_path TEXT)")
        con.execute("INSERT INTO tasks VALUES (?, ?)", (SID, WORKSPACE))
        con.execute(
            "INSERT INTO tasks VALUES (?, ?)", ("sess_other-1111-2222-3333-444455556666", "/other")
        )
        con.commit()
        con.close()
        cli = self.tmp / "cli.sqlite"
        con = sqlite3.connect(cli)
        con.execute("CREATE TABLE session (id TEXT, task_type TEXT)")
        con.execute(
            "INSERT INTO session VALUES (?, 'subagent_child')",
            ("sess_sub-1111-2222-3333-444455556666",),
        )
        con.commit()
        con.close()
        platform = zcode_wake.build_platform(
            env=self.env,
            workspace=WORKSPACE,
            index_path=str(index),
            cli_db=str(cli),
        )
        excluded = platform.exclude([
            WR.Target(session_id=SID, letter="BB", generation=3),
            WR.Target(
                session_id="sess_other-1111-2222-3333-444455556666", letter="BB", generation=3
            ),
            WR.Target(
                session_id="sess_sub-1111-2222-3333-444455556666", letter="BB", generation=3
            ),
        ])
        self.assertEqual(excluded, {"sess_other-1111-2222-3333-444455556666",
                                    "sess_sub-1111-2222-3333-444455556666"})


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
            "ZCODE_WAKE_BIN": "/usr/bin/true",
        }

    def test_round_trip_through_platforms_table(self):
        from simple_tools import claude_wake

        entry = claude_wake.PLATFORMS["zcode"]
        self.assertEqual(entry.session_re.pattern, "^sess_[A-Za-z0-9._-]{1,123}$")
        request = _request()
        with patch.object(zcode_wake.WR.registry_api, "call", return_value=_holding_row()), \
             patch.object(zcode_wake, "_tasks_workspace", return_value=("ok", WORKSPACE)):
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


if __name__ == "__main__":
    unittest.main()
