"""The MiniMax wake delivery (AFC-804):  every test drives the module the way the fleet does.
The registry is a stub that answers `resolve` from a fixed row, the store is a temporary sqlite
database with MiniMax Code's own schema, and the delivery command is a fake runner that leaves
its argv in a list - never a real `mcode` run."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simple_tools import minimax_wake
from simple_tools import wake_router as WR

SID = "mvs_1a2b3c4d11112222333344445555abcd"
SID_OTHER = "mvs_9999ffff11112222333344445555abcd"
SID_SUB = "mvs_deadbeef11112222333344445555abcd"
SID_CRON = "mvs_c0ffee0011112222333344445555abcd"
SID_MAC = "mvs_cafe123411112222333344445555abcd"
SID_ARCHIVED = "mvs_a7c4e0011112222333344445555abcd"


def _holding_row(session_id: str = SID):
    return {
        "found": True,
        "native_session_id": session_id,
        "seat": "MM",
        "label": "CC",
        "generation": 2,
        "verified_host": "server",
        "state": "active",
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir(parents=True)
        # The workspace a chat is bound to has to exist on this host:  the module refuses a
        # recorded workspace that is gone before it ever compares it with the setting.
        self.workspace = Path(tmp.name) / "workspace"
        self.workspace.mkdir(parents=True)
        # A second real directory:  a recorded workspace that exists but is not the configured
        # one is a mismatch, which is a different refusal from a workspace that is gone.
        self.other_workspace = Path(tmp.name) / "other-workspace"
        self.other_workspace.mkdir(parents=True)
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.store = self.home / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite"
        self.store.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.store)
        con.execute(
            "CREATE TABLE local_runtime_sessions ("
            "session_id TEXT, session_kind TEXT, parent_session_id TEXT, archived INTEGER, "
            "status TEXT, workspace_dir TEXT)"
        )
        for sid in (SID, SID_OTHER, SID_MAC):
            con.execute(
                "INSERT INTO local_runtime_sessions VALUES (?, 'conversation', '', 0, 'idle', ?)",
                (sid, str(self.workspace)),
            )
        con.execute(
            "INSERT INTO local_runtime_sessions VALUES (?, 'task', '', 0, 'idle', ?)",
            (SID_SUB, str(self.workspace)),
        )
        con.execute(
            "INSERT INTO local_runtime_sessions VALUES (?, 'cron', '', 0, 'idle', ?)",
            (SID_CRON, str(self.workspace)),
        )
        con.commit()
        con.close()
        self.calls: list[list[str]] = []
        fake_bin = self.home / "bin" / "mcode"
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        self.env["MCODE_BIN"] = str(fake_bin)

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

    def _update(self, sid, **fields):
        con = sqlite3.connect(self.store)
        sets = ", ".join("%s = ?" % k for k in fields)
        con.execute(
            "UPDATE local_runtime_sessions SET %s WHERE session_id = ?" % sets,
            tuple(fields.values()) + (sid,),
        )
        con.commit()
        con.close()

    def _deliver_local(self, session_id=SID, wake=None, config=None, recheck=False,
                       exit_code=0, registry_rows=None):
        holding = _holding_row(session_id)
        rows = registry_rows or [holding]
        target = WR.Target(session_id=session_id, letter="CC", host="server", generation=2)
        wake = WR.Wake(
            wake_id="w1-abcdef",
            seat="MM",
            message_id=639428919,
            channel="Work-In-Progress",
            topic="AFC-804 MiniMax wake delivery platform",
            zulip_link="https://simplewithus.zulipchat.com/narrow/topic/1",
            sender="ZCode",
            letter="CC",
            picked="letter",
            reason="letter",
            link_note=(
                "The router linked this wake to this chat because the sender addressed its letter."
            ),
        )
        config = dict(config or {})
        config.setdefault("workspace", str(self.workspace))
        config.setdefault("local_host", "server")
        with patch.object(
            minimax_wake.WR.registry_api, "call", side_effect=self._registry(rows)
        ):
            delivery = minimax_wake.deliver_local(
                self.env,
                config,
                session_id,
                wake,
                target=target if recheck else None,
                recheck_registry=recheck,
                runner=self._runner(exit_code),
            )
        return delivery


class DeliverLocalTests(Base):
    def test_delivers_sent_when_workspace_matches(self):
        delivery = self._deliver_local(exit_code=0)
        self.assertEqual(delivery.outcome, "sent")
        self.assertEqual(delivery.route, "wake")
        self.assertEqual(delivery.note, "mcode-exit-0")
        argv = self.calls[0]
        self.assertIn("exec", argv)
        self.assertIn("--session", argv)
        self.assertIn(SID, argv)
        self.assertIn("--cwd", argv)
        self.assertIn(str(self.workspace), argv)
        self.assertTrue(any("wake id w1-abcdef" in str(part) for part in argv))
        self.assertTrue(any("the sender addressed its letter" in str(part) for part in argv))

    def test_session_not_found_refuses(self):
        con = sqlite3.connect(self.store)
        con.execute("DELETE FROM local_runtime_sessions WHERE session_id = ?", (SID,))
        con.commit()
        con.close()
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-not-found")

    def test_a_cron_row_is_not_a_chat(self):
        delivery = self._deliver_local(session_id=SID_CRON)
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-not-a-chat")

    def test_a_subagent_child_refuses(self):
        self._update(SID_SUB, parent_session_id=SID)
        delivery = self._deliver_local(session_id=SID_SUB)
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-subagent")

    def test_an_archived_chat_refuses(self):
        self._update(SID, archived=1)
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-archived")

    def test_a_running_session_is_busy(self):
        self._update(SID, status="started")
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "busy")
        self.assertEqual(delivery.code, "session-busy")

    def test_workspace_gone_refuses(self):
        self._update(SID, workspace_dir="")
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-cwd-gone")

    def test_workspace_that_does_not_exist_refuses(self):
        self._update(SID, workspace_dir=str(self.home / "gone"))
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "session-cwd-gone")

    def test_workspace_mismatch_refuses(self):
        self._update(SID, workspace_dir=str(self.other_workspace))
        delivery = self._deliver_local(config={"workspace": str(self.workspace)})
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
        self.assertEqual(delivery.code, "wake-exit-3")

    def test_exit_zero_is_sent_never_completed(self):
        delivery = self._deliver_local(exit_code=0)
        self.assertNotEqual(delivery.outcome, "completed")
        self.assertEqual(delivery.outcome, "sent")

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

    def test_a_missing_binary_is_a_bounded_failure(self):
        self.env["MCODE_BIN"] = str(self.home / "bin" / "absent")
        delivery = self._deliver_local()
        self.assertEqual(delivery.outcome, "failed")
        self.assertEqual(delivery.code, "mcode-not-found")
        self.assertEqual(self.calls, [])


class ExcludeTests(Base):
    def test_exclude_narrows_subagents_cron_and_archived(self):
        self._update(SID_SUB, parent_session_id=SID)
        con = sqlite3.connect(self.store)
        con.execute(
            "INSERT INTO local_runtime_sessions VALUES (?, 'conversation', '', 1, 'idle', ?)",
            (SID_ARCHIVED, str(self.workspace)),
        )
        con.commit()
        con.close()
        exclude = minimax_wake.exclude_non_interactive(self.env, "server")
        targets = [
            WR.Target(session_id=SID, letter="CC", host="server", generation=2),
            WR.Target(session_id=SID_SUB, letter="CC", host="server", generation=2),
            WR.Target(session_id=SID_CRON, letter="CC", host="server", generation=2),
            WR.Target(session_id=SID_ARCHIVED, letter="CC", host="server", generation=2),
            WR.Target(session_id=SID_MAC, letter="CC", host="mac", generation=2),
        ]
        excluded = exclude(targets)
        self.assertIn(SID_SUB, excluded)
        self.assertIn(SID_CRON, excluded)
        self.assertIn(SID_ARCHIVED, excluded)
        self.assertNotIn(SID, excluded)
        self.assertNotIn(SID_MAC, excluded)


class RouterPlatformTests(Base):
    def _platform(self):
        with patch.object(minimax_wake.WR.registry_api, "call",
                          side_effect=self._registry([_holding_row()])):
            return minimax_wake.router_platform(
                self.env, {"workspace": str(self.workspace), "local_host": "server"},
                runner=self._runner(0),
            )

    def test_builds_platform_with_minimax_identity(self):
        platform = self._platform()
        self.assertEqual(platform.seat, "MM")
        self.assertEqual(platform.platform, "minimax")
        self.assertEqual(platform.mention, "MiniMax")
        self.assertEqual(platform.target_re.pattern, r"^mvs_[0-9a-f]{32}$")

    def test_deliver_uses_configured_workspace(self):
        platform = self._platform()
        delivery = platform.deliver(
            WR.Target(session_id=SID, letter="CC", host="server", generation=2),
            WR.Wake(wake_id="w2-abcdef", message_id=1),
        )
        self.assertEqual(delivery.outcome, "sent")
        self.assertIn(str(self.workspace), self.calls[0])

    def test_an_unknown_host_is_refused(self):
        platform = self._platform()
        delivery = platform.deliver(
            WR.Target(session_id=SID, letter="CC", host="cloud", generation=2),
            WR.Wake(wake_id="w3-abcdef", message_id=1),
        )
        self.assertEqual(delivery.outcome, "refused")
        self.assertEqual(delivery.code, "unknown-host")


class BuilderTableTests(Base):
    def test_the_shared_table_carries_the_minimax_row(self):
        from simple_tools import wake_service as SVC

        entry = SVC.by_platform("minimax")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.platform, "minimax")
        self.assertEqual(entry.module, "simple_tools.minimax_wake")
        self.assertEqual(entry.session_pattern.pattern, r"^mvs_[0-9a-f]{32}$")

    def test_the_minimax_module_is_the_one_the_table_names(self):
        from simple_tools import wake_service as SVC

        entry = SVC.by_platform("minimax")
        self.assertIsNotNone(entry)
        self.assertIs(minimax_wake, __import__(entry.module, fromlist=["SEAT"]))

    def test_the_never_ran_codes_are_the_house_shape(self):
        for code in ("bad-session-id", "session-not-found", "session-subagent",
                     "session-cwd-gone", "workspace-mismatch", "mcode-not-found",
                     "unknown-host", WR.RELEASED):
            self.assertIn(code, minimax_wake.NEVER_RAN)


if __name__ == "__main__":
    unittest.main()
