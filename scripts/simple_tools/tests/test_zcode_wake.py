"""The ZCode wake delivery (OPS-1048):  every test drives the module the way the fleet does.
The registry is a stub that answers `resolve` from a fixed row, the stores are temporary
sqlite databases with zcode's own schema, and the delivery command is a fake runner that
leaves its argv in a list - never a real `zcode` run."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simple_tools import wake_router as WR
from simple_tools import zcode_wake

SID = "sess_1a2b3c4d-1111-2222-3333-444455556666"
SID_OTHER = "sess_9999ffff-1111-2222-3333-444455556666"
SID_SUB = "sess_deadbeef-1111-2222-3333-444455556666"
SID_MAC = "sess_cafe1234-1111-2222-3333-444455556666"
WORKSPACE = "/tmp/zcode-wake-test-workspace"


def _holding_row(session_id: str = SID):
    return {
        "found": True,
        "native_session_id": session_id,
        "seat": "ZC",
        "label": "BB",
        "generation": 3,
        "verified_host": "server",
        "state": "active",
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir(parents=True)
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.index = self.home / ".zcode" / "v2" / "tasks-index.sqlite"
        self.index.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.index)
        con.execute("CREATE TABLE tasks (task_id TEXT, workspace_path TEXT, task_status TEXT)")
        for sid in (SID, SID_OTHER, SID_MAC):
            con.execute("INSERT INTO tasks VALUES (?, ?, ?)", (sid, WORKSPACE, "completed"))
        con.commit()
        con.close()
        self.cli = self.home / ".zcode" / "cli" / "db" / "db.sqlite"
        self.cli.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.cli)
        con.execute("CREATE TABLE session (id TEXT, task_type TEXT)")
        con.commit()
        con.close()
        self.calls: list[list[str]] = []
        fake_bin = self.home / "bin" / "zcode"
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        self.env["ZCODE_BIN"] = str(fake_bin)

    def _runner(self, exit_code: int = 0):
        def runner(argv, **kwargs):
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=exit_code)

        return runner

    def _registry(self, rows):
        table = {r["native_session_id"]: r for r in rows}

        def call(env, op, params=None, **kwargs):
            letter = (params or {}).get("letter")
            for row in table.values():
                if row.get("label") == letter:
                    return row
            return {"found": False}

        return call

    def _deliver_local(self, session_id=SID, wake=None, config=None, recheck=False,
                       exit_code=0, registry_rows=None):
        holding = _holding_row(session_id)
        rows = registry_rows or [holding]
        target = zcode_wake.WR.Target(
            session_id=session_id, letter="BB", host="server", generation=3
        )
        wake = zcode_wake.WR.Wake(
            wake_id="w1-abcdef",
            seat="ZC",
            message_id=639428919,
            channel="Work-In-Progress",
            topic="OPS-1048 ZCode letters and wakes",
            zulip_link="https://simplewithus.zulipchat.com/narrow/topic/1",
            sender="Claude",
            letter="BB",
            picked="letter",
            reason="letter",
            link_note=(
                "The router linked this wake to this chat because the sender addressed its letter."
            ),
        )
        config = dict(config or {})
        config.setdefault("workspace", WORKSPACE)
        config.setdefault("local_host", "server")
        with patch.object(
            zcode_wake.WR.registry_api, "call", side_effect=self._registry(rows)
        ):
            delivery = zcode_wake.deliver_local(
                self.env,
                config,
                session_id,
                wake,
                target=target if recheck else None,
                recheck_registry=recheck,
                runner=self._runner(exit_code),
            )
        return delivery


WORKSPACE = "/tmp/zcode-wake-test-workspace"


class DeliverLocalTests(Base):
    def test_delivers_sent_when_workspace_matches(self):
        delivery = self._deliver_local(exit_code=0)
        self.assertEqual(delivery.outcome, "sent")
        self.assertEqual(delivery.route, "resume")
        self.assertEqual(delivery.note, "zcode-exit-0")
        argv = self.calls[0]
        self.assertIn("--cwd", argv)
        self.assertIn(WORKSPACE, argv)
        self.assertIn("--resume", argv)
        self.assertIn(SID, argv)
        self.assertTrue(any("wake id w1-abcdef" in str(part) for part in argv))
        self.assertTrue(any("the sender addressed its letter" in str(part) for part in argv))

    def test_session_not_found_refuses(self):
        con = sqlite3.connect(self.index)
        con.execute("DELETE FROM tasks WHERE task_id = ?", (SID,))
        con.commit()
        con.close()
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-not-found")

    def test_subagent_child_refuses(self):
        con = sqlite3.connect(self.cli)
        con.execute("INSERT INTO session VALUES (?, 'subagent_child')", (SID,))
        con.commit()
        con.close()
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-subagent")

    def test_running_session_is_busy(self):
        con = sqlite3.connect(self.index)
        con.execute("UPDATE tasks SET task_status = 'running' WHERE task_id = ?", (SID,))
        con.commit()
        con.close()
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "busy")
        self.assertEqual(delivery.code, "session-busy")

    def test_workspace_gone_refuses(self):
        con = sqlite3.connect(self.index)
        con.execute("UPDATE tasks SET workspace_path = '' WHERE task_id = ?", (SID,))
        con.commit()
        con.close()
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-cwd-gone")

    def test_workspace_mismatch_refuses(self):
        con = sqlite3.connect(self.index)
        con.execute("UPDATE tasks SET workspace_path = '/elsewhere' WHERE task_id = ?", (SID,))
        con.commit()
        con.close()
        delivery = self._deliver_local(config={"workspace": WORKSPACE})
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "workspace-mismatch")

    def test_settings_missing_fails_and_holds(self):
        delivery = self._deliver_local(config={"workspace": ""})
        self.assertEqual(delivery.outcome, "failed")
        self.assertEqual(delivery.code, "settings-missing")
        self.assertEqual(self.calls, [])

    def test_nonzero_exit_is_refused(self):
        delivery = self._deliver_local(exit_code=3)
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "resume-exit-3")

    def test_registry_release_under_recheck_refuses(self):
        released = dict(_holding_row(), found=False)
        delivery = self._deliver_local(recheck=True, registry_rows=[released])
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, WR.RELEASED)

    def test_registry_hold_under_recheck_delivers(self):
        delivery = self._deliver_local(recheck=True)
        self.assertEqual(delivery.outcome, "sent")
        self.assertEqual(len(self.calls), 1)

    def test_bad_session_id_refused(self):
        delivery = self._deliver_local(session_id="not-a-session")
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "bad-session-id")



class ExcludeTests(Base):
    def test_exclude_narrows_subagents_and_unknown(self):
        con = sqlite3.connect(self.cli)
        con.execute("INSERT INTO session VALUES (?, 'subagent_child')", (SID_SUB,))
        con.commit()
        con.close()
        exclude = zcode_wake.exclude_non_interactive(self.env, "server")
        targets = [
            zcode_wake.WR.Target(session_id=SID, letter="BB", host="server", generation=3),
            zcode_wake.WR.Target(session_id=SID_SUB, letter="BB", host="server", generation=3),
            zcode_wake.WR.Target(session_id=SID_OTHER, letter="BB", host="server", generation=3),
            zcode_wake.WR.Target(session_id=SID_MAC, letter="BB", host="mac", generation=3),
        ]
        excluded = exclude(targets)
        self.assertIn(SID_SUB, excluded)
        self.assertNotIn(SID, excluded)
        self.assertNotIn(SID_MAC, excluded)


class RouterPlatformTests(Base):
    def _platform(self):
        with patch.object(zcode_wake.WR.registry_api, "call",
                          side_effect=self._registry([_holding_row()])):
            return zcode_wake.router_platform(self.env, {"workspace": WORKSPACE,
                                                         "local_host": "server"},
                                              runner=self._runner(0))

    def test_builds_platform_with_zc_identity(self):
        platform = self._platform()
        self.assertEqual(platform.seat, "ZC")
        self.assertEqual(platform.platform, "zcode")
        self.assertEqual(platform.mention, "ZCode")
        self.assertTrue(platform.target_re.pattern.startswith("^sess_"))

    def test_deliver_uses_configured_workspace(self):
        platform = self._platform()
        delivery = platform.deliver(
            zcode_wake.WR.Target(session_id=SID, letter="BB", host="server", generation=3),
            zcode_wake.WR.Wake(wake_id="w2-abcdef", message_id=1),
        )
        self.assertEqual(delivery.outcome, "sent")
        self.assertIn(WORKSPACE, self.calls[0])


if __name__ == "__main__":
    unittest.main()
