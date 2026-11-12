"""Unit tests for Antigravity's wake router adapter (scripts/simple_tools/antigravity_wake.py).

Verifies the Platform builder, local delivery under per-session locking, sub-agent and killed
conversation exclusion, CWD resolution, agy invocation, Mac remote delegation, and the Mac-side
deliver_request handler.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simple_tools import antigravity_wake as AGW
from simple_tools import wake_router as WR
from simple_tools import wake_service as SVC

SID_VALID = "11111111-2222-4333-8444-555555555555"
SID_SUB = "22222222-3333-4444-8555-666666666666"
SID_KILLED = "33333333-4444-4555-8666-777777777777"
SID_BUSY = "44444444-5555-4666-8777-888888888888"
SID_ABSENT = "99999999-9999-4999-8999-999999999999"

SUMMARIES_DDL = (
    "CREATE TABLE conversation_summaries ("
    " conversation_id TEXT PRIMARY KEY,"
    " title TEXT NOT NULL DEFAULT '',"
    " workspace_uris TEXT,"
    " parent_conversation_id TEXT,"
    " nesting_depth INTEGER DEFAULT 0,"
    " killed INTEGER DEFAULT 0,"
    " not_fully_idle INTEGER DEFAULT 0)"
)


class BaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.valid_project = self.root / "project"
        self.valid_project.mkdir()
        self.env = {
            "HOME": str(self.root),
            "AG_ROUTER_STATE": str(self.root / "state"),
            "AG_WAKE_STORE": str(self.root / "summaries.db"),
        }
        self.db_path = self.root / "summaries.db"
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(SUMMARIES_DDL)
            conn.commit()
        finally:
            conn.close()

    def _insert(
        self,
        cid: str,
        *,
        title: str = "Test Chat",
        workspace_uris: str | None = None,
        parent: str = "",
        depth: int = 0,
        killed: int = 0,
        not_fully_idle: int = 0,
    ) -> None:
        if workspace_uris is None:
            workspace_uris = json.dumps([f"file://{self.valid_project}"])
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO conversation_summaries "
                "(conversation_id, title, workspace_uris, parent_conversation_id, "
                "nesting_depth, killed, not_fully_idle) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, title, workspace_uris, parent, depth, killed, not_fully_idle),
            )
            conn.commit()
        finally:
            conn.close()

    def wake(self, **overrides) -> WR.Wake:
        base = {
            "wake_id": "w-test-1",
            "seat": "AG",
            "message_id": 12345,
            "channel": "Work-In-Progress",
            "topic": "AFC-694 Operator cross-platform coordination",
            "zulip_link": "https://simplewithus.zulipchat.com/#narrow/near/12345",
            "sender": "Claude",
            "dm": False,
            "letter": "CC",
            "picked": "topic-match",
            "reason": "topic-match",
            "link_note": "A note about the link.",
        }
        base.update(overrides)
        return WR.Wake(**base)


class TestAntigravityWakeConfig(BaseTest):
    def test_constants_and_defaults(self) -> None:
        self.assertEqual(AGW.SEAT, "AG")
        self.assertEqual(AGW.PLATFORM, "antigravity")
        self.assertEqual(AGW.MENTION, "Antigravity")
        self.assertTrue(set(WR.ROUTER_DEFAULTS) <= set(AGW.DEFAULTS))
        self.assertEqual(AGW.DEFAULTS["local_host"], "server")
        self.assertNotIn("agy_bin", AGW.DEFAULTS)
        self.assertEqual(AGW.DEFAULTS["resume_timeout_seconds"], 900)

    def test_never_ran_codes_match_re(self) -> None:
        for code in AGW.NEVER_RAN:
            self.assertTrue(SVC.NEVER_RAN_CODE.fullmatch(code), code)

    def test_state_dir(self) -> None:
        self.assertEqual(AGW.state_dir(self.env), self.root / "state")
        self.assertEqual(
            AGW.state_dir({"HOME": "/tmp/test"}),
            Path("/tmp/test/.local/state/wake-router/ag"),
        )

    def test_store_paths(self) -> None:
        self.assertEqual(AGW.store_paths(self.env), [self.root / "summaries.db"])
        paths = AGW.store_paths({"HOME": "/tmp/test"})
        self.assertEqual(len(paths), 2)
        self.assertIn(Path("/tmp/test/.gemini/antigravity-cli/conversation_summaries.db"), paths)


class TestAntigravityWakePayload(BaseTest):
    def test_wake_text_formatting(self) -> None:
        w = self.wake()
        text = AGW.wake_text(w)
        self.assertIn("[AG wake] You were tagged in Zulip (wake id w-test-1).", text)
        self.assertIn("zulip_get_message with message_id 12345", text)
        self.assertIn("A note about the link.", text)
        self.assertNotIn("BODY", text)
        # Parse json block at the end
        lines = text.strip().splitlines()
        fields = json.loads(lines[-1])
        self.assertEqual(fields["channel"], "Work-In-Progress")
        self.assertEqual(fields["sender"], "Claude")

    def test_child_env(self) -> None:
        env = AGW._child_env(self.env)
        self.assertEqual(env["HOME"], str(self.root))
        self.assertEqual(env["AGENT_LAUNCHER"], "simple-tools-wake")
        self.assertEqual(env["AGENT_LAUNCH_SEAT"], "AG")
        self.assertEqual(env["SIMPLE_TOOLS_ATTACH"], "0")
        self.assertNotIn("WAKE_ROUTER_KEY", env)

        # AGY_BIN is preserved when present in env
        fake_bin = self.root / "bin" / "custom-agy"
        fake_bin.parent.mkdir(parents=True, exist_ok=True)
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        env_with_bin = {**self.env, "AGY_BIN": str(fake_bin)}
        c_env = AGW._child_env(env_with_bin)
        self.assertEqual(c_env["AGY_BIN"], str(fake_bin))
        self.assertEqual(AGW._find_agy_binary(c_env), str(fake_bin))

    def test_resolve_cwd(self) -> None:
        rec_ok = {"workspace_uris": json.dumps([f"file://{self.valid_project}"])}
        self.assertEqual(AGW._resolve_cwd(rec_ok), str(self.valid_project))

        rec_gone = {"workspace_uris": json.dumps(["file:///nonexistent/directory/xyz"])}
        self.assertEqual(AGW._resolve_cwd(rec_gone), "")

        # Multiple URIs: first non-existent, second valid directory
        rec_multi = {
            "workspace_uris": json.dumps(
                ["file:///nonexistent/path1", f"file://{self.valid_project}"]
            )
        }
        self.assertEqual(AGW._resolve_cwd(rec_multi), str(self.valid_project))

        # Empty, missing, or malformed URIs return empty string (session-cwd-gone)
        self.assertEqual(AGW._resolve_cwd({"workspace_uris": ""}), "")
        self.assertEqual(AGW._resolve_cwd({"workspace_uris": None}), "")
        self.assertEqual(AGW._resolve_cwd({"workspace_uris": "invalid-json"}), "")
        self.assertEqual(AGW._resolve_cwd({"workspace_uris": json.dumps({"not": "a list"})}), "")


class TestAntigravityExcludeNonInteractive(BaseTest):
    def test_exclude_subagent_and_killed(self) -> None:
        self._insert(SID_VALID, killed=0, parent="", depth=0)
        self._insert(SID_SUB, killed=0, parent=SID_VALID, depth=1)
        self._insert(SID_KILLED, killed=1, parent="", depth=0)

        targets = [
            WR.Target(SID_VALID, "AA", "server", 1),
            WR.Target(SID_SUB, "AB", "server", 1),
            WR.Target(SID_KILLED, "AC", "server", 1),
            WR.Target(SID_VALID, "AA", "mac", 1),
        ]
        exclude = AGW.exclude_non_interactive(self.env, "server")
        excluded = exclude(targets)
        self.assertIn(SID_SUB, excluded)
        self.assertIn(SID_KILLED, excluded)
        self.assertNotIn(SID_VALID, excluded)


class TestAntigravityDeliverLocal(BaseTest):
    def test_bad_session_id(self) -> None:
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, "bad-id", self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("refused", "bad-session-id"))

    def test_session_lock_contention(self) -> None:
        lock_dir = self.root / "state" / "locks"
        lock_dir.mkdir(parents=True)
        fd = os.open(lock_dir / f"session-{SID_VALID}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_VALID, self.wake())
            self.assertEqual((delivery.outcome, delivery.code), ("busy", "session-locked"))
        finally:
            os.close(fd)

    def test_bridge_lock_contention(self) -> None:
        bridge_dir = self.root / ".local" / "state" / "ag-wake"
        bridge_dir.mkdir(parents=True)
        fd = os.open(bridge_dir / f"session-{SID_VALID}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_VALID, self.wake())
            self.assertEqual((delivery.outcome, delivery.code), ("busy", "session-locked"))
        finally:
            os.close(fd)

        # AG_WAKE_STATE custom directory contention
        custom_bridge = self.root / "custom-ag-wake"
        custom_bridge.mkdir(parents=True)
        fd2 = os.open(custom_bridge / f"session-{SID_VALID}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            env_custom = {**self.env, "AG_WAKE_STATE": str(custom_bridge)}
            delivery2 = AGW.deliver_local(env_custom, AGW.DEFAULTS, SID_VALID, self.wake())
            self.assertEqual((delivery2.outcome, delivery2.code), ("busy", "session-locked"))
        finally:
            os.close(fd2)

    def test_bridge_lock_eacces_fails(self) -> None:
        bridge_dir = self.root / ".local" / "state" / "ag-wake"
        bridge_dir.mkdir(parents=True)
        orig_flock = fcntl.flock
        call_count = 0

        def fake_flock(fd, op):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # First is session lock, second is bridge lock
                raise PermissionError("EACCES")
            return orig_flock(fd, op)

        with mock.patch("fcntl.flock", side_effect=fake_flock):
            delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_VALID, self.wake())
            self.assertEqual((delivery.outcome, delivery.code), ("failed", "bridge-lock-eacces"))

    def test_recheck_under_lock(self) -> None:
        self._insert(SID_VALID)
        target = WR.Target(SID_VALID, "CC", "server", 1)
        ran = False

        def fake_runner(argv, **kwargs):
            nonlocal ran
            ran = True
            return subprocess.CompletedProcess(argv, returncode=0)

        # When recheck_registry=True (Mac path), _recheck runs and can refuse
        with mock.patch.object(
            AGW, "_recheck", return_value=WR.Delivery("refused", code=WR.RELEASED)
        ):
            delivery = AGW.deliver_local(
                self.env,
                AGW.DEFAULTS,
                SID_VALID,
                self.wake(),
                target=target,
                recheck_registry=True,
                runner=fake_runner,
            )
        self.assertEqual((delivery.outcome, delivery.code), ("refused", WR.RELEASED))
        self.assertFalse(ran)

        # When recheck_registry=False (Server path), _recheck is skipped even if target is present
        with (
            mock.patch.object(AGW, "_find_agy_binary", return_value="/bin/agy"),
            mock.patch.object(
                AGW, "_recheck", return_value=WR.Delivery("refused", code=WR.RELEASED)
            ),
        ):
            delivery2 = AGW.deliver_local(
                self.env,
                AGW.DEFAULTS,
                SID_VALID,
                self.wake(),
                target=target,
                recheck_registry=False,
                runner=fake_runner,
            )
        self.assertEqual(delivery2.outcome, "sent")
        self.assertTrue(ran)

    def test_conversation_not_found(self) -> None:
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_ABSENT, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("refused", "conversation-not-found"))

    def test_find_conversation_record_schema_tolerance(self) -> None:
        # Schema missing not_fully_idle fails closed
        db_missing_idle = self.root / "missing_idle.db"
        conn = sqlite3.connect(db_missing_idle)
        try:
            conn.execute(
                "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, killed INTEGER)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, killed) VALUES (?, 0)",
                (SID_VALID,),
            )
            conn.commit()
        finally:
            conn.close()

        rec, err = AGW.find_conversation_record(
            {**self.env, "AG_WAKE_STORE": str(db_missing_idle)}, SID_VALID
        )
        self.assertIsNone(rec)
        self.assertEqual(err, "schema-missing-idle")

        # Schema missing killed fails closed
        db_missing_killed = self.root / "missing_killed.db"
        conn = sqlite3.connect(db_missing_killed)
        try:
            conn.execute(
                "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, not_fully_idle INTEGER)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, not_fully_idle) VALUES (?, 0)",
                (SID_VALID,),
            )
            conn.commit()
        finally:
            conn.close()

        rec2, err2 = AGW.find_conversation_record(
            {**self.env, "AG_WAKE_STORE": str(db_missing_killed)}, SID_VALID
        )
        self.assertIsNone(rec2)
        self.assertEqual(err2, "schema-missing-killed")

        # deliver_local maps schema-missing to retryable failed
        delivery = AGW.deliver_local(
            {**self.env, "AG_WAKE_STORE": str(db_missing_idle)}, AGW.DEFAULTS, SID_VALID, self.wake()
        )
        self.assertEqual((delivery.outcome, delivery.code), ("failed", "store-schema-missing-idle"))

        # Minimal schema with both required columns succeeds
        db_ok = self.root / "minimal_ok.db"
        conn = sqlite3.connect(db_ok)
        try:
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT PRIMARY KEY, not_fully_idle INTEGER, killed INTEGER, "
                "workspace_uris TEXT)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, 0, 0, ?)",
                (SID_VALID, json.dumps([f"file://{self.valid_project}"])),
            )
            conn.commit()
        finally:
            conn.close()

        rec3, err3 = AGW.find_conversation_record(
            {**self.env, "AG_WAKE_STORE": str(db_ok)}, SID_VALID
        )
        self.assertIsNone(err3)
        self.assertIsNotNone(rec3)
        self.assertEqual(rec3["not_fully_idle"], 0)
        self.assertEqual(rec3["killed"], 0)
        self.assertEqual(rec3["title"], "")
        self.assertEqual(rec3["nesting_depth"], 0)

    def test_find_conversation_record_locked_corrupt_read_error(self) -> None:
        # Corrupt DB file
        db_corrupt = self.root / "corrupt.db"
        db_corrupt.write_bytes(b"not an sqlite database file")
        rec, err = AGW.find_conversation_record({**self.env, "AG_WAKE_STORE": str(db_corrupt)}, SID_VALID)
        self.assertIsNone(rec)
        self.assertEqual(err, "db-error")

        # Locked DB
        with mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            rec, err = AGW.find_conversation_record(self.env, SID_VALID)
            self.assertIsNone(rec)
            self.assertEqual(err, "db-operational-error")

    def test_store_error_does_not_fall_through(self) -> None:
        db1 = self.root / "store1.db"
        db2 = self.root / "store2.db"
        db1.write_bytes(b"corrupt header")
        conn2 = sqlite3.connect(db2)
        try:
            conn2.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)")
            conn2.execute("INSERT INTO conversation_summaries VALUES (?)", (SID_VALID,))
            conn2.commit()
        finally:
            conn2.close()

        with mock.patch.object(AGW, "store_paths", return_value=[db1, db2]):
            rec, err = AGW.find_conversation_record(self.env, SID_VALID)
            self.assertIsNone(rec)
            self.assertEqual(err, "db-error")

    def test_deliver_local_db_error_fails_bounded_retry(self) -> None:
        with mock.patch.object(
            AGW, "find_conversation_record", return_value=(None, "db-operational-error")
        ):
            delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_VALID, self.wake())
            self.assertEqual(
                (delivery.outcome, delivery.code),
                ("failed", "store-db-operational-error"),
            )

    def test_conversation_subagent(self) -> None:
        self._insert(SID_SUB, parent=SID_VALID, depth=1)
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_SUB, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("refused", "conversation-subagent"))

    def test_conversation_killed(self) -> None:
        self._insert(SID_KILLED, killed=1)
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_KILLED, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("refused", "conversation-killed"))

    def test_conversation_busy_running_turn(self) -> None:
        self._insert(SID_BUSY, not_fully_idle=1)
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_BUSY, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("busy", "session-busy"))

        # NULL not_fully_idle also counts as busy
        sid_null = "55555555-5555-4555-8555-555555555555"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO conversation_summaries "
                "(conversation_id, title, workspace_uris, parent_conversation_id, "
                "nesting_depth, killed, not_fully_idle) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (sid_null, "Null Idle", json.dumps([f"file://{self.valid_project}"]), "", 0, 0),
            )
            conn.commit()
        finally:
            conn.close()
        d_null = AGW.deliver_local(self.env, AGW.DEFAULTS, sid_null, self.wake())
        self.assertEqual((d_null.outcome, d_null.code), ("busy", "session-busy"))

    def test_session_cwd_gone(self) -> None:
        self._insert(
            SID_VALID, workspace_uris=json.dumps(["file:///nonexistent/project/path"])
        )
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_VALID, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("refused", "session-cwd-gone"))

    def test_session_cwd_missing_refused(self) -> None:
        sid_no_cwd = "66666666-6666-4666-8666-666666666666"
        self._insert(sid_no_cwd, workspace_uris="")
        delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, sid_no_cwd, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("refused", "session-cwd-gone"))

    def test_agy_binary_not_found(self) -> None:
        self._insert(SID_VALID)
        with mock.patch.object(AGW, "_find_agy_binary", return_value=None):
            delivery = AGW.deliver_local(self.env, AGW.DEFAULTS, SID_VALID, self.wake())
        self.assertEqual((delivery.outcome, delivery.code), ("failed", "agy-not-found"))

    def test_successful_delivery(self) -> None:
        self._insert(SID_VALID)
        ran: list[list[str]] = []

        def fake_runner(argv, **kwargs):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, returncode=0)

        with mock.patch.object(AGW, "_find_agy_binary", return_value="/bin/agy"):
            delivery = AGW.deliver_local(
                self.env, AGW.DEFAULTS, SID_VALID, self.wake(), runner=fake_runner
            )
        self.assertEqual(delivery.outcome, "sent")
        self.assertEqual(delivery.route, "resume")
        self.assertEqual(delivery.ack, "unknown")
        self.assertEqual(delivery.note, "agy-exit-0")
        self.assertEqual(ran[0][:3], ["/bin/agy", "--conversation", SID_VALID])
        self.assertEqual(ran[0][3], "-p")

    def test_timeout_and_error_handling(self) -> None:
        self._insert(SID_VALID)

        def timeout_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, timeout=10)

        def oserror_runner(argv, **kwargs):
            raise OSError("exec failed")

        def fail_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, returncode=127)

        with mock.patch.object(AGW, "_find_agy_binary", return_value="/bin/agy"):
            d_time = AGW.deliver_local(
                self.env, AGW.DEFAULTS, SID_VALID, self.wake(), runner=timeout_runner
            )
            self.assertEqual((d_time.outcome, d_time.code), ("refused", "resume-timeout"))

            d_os = AGW.deliver_local(
                self.env, AGW.DEFAULTS, SID_VALID, self.wake(), runner=oserror_runner
            )
            self.assertEqual((d_os.outcome, d_os.code), ("failed", "resume-OSError"))

            d_fail = AGW.deliver_local(
                self.env, AGW.DEFAULTS, SID_VALID, self.wake(), runner=fail_runner
            )
            self.assertEqual((d_fail.outcome, d_fail.code), ("refused", "resume-exit-127"))


class TestAntigravityMakeDeliver(BaseTest):
    def test_branches_on_host(self) -> None:
        deliver = AGW.make_deliver(self.env, AGW.DEFAULTS)
        t_local = WR.Target(SID_VALID, "CC", "server", 1)
        t_mac = WR.Target(SID_VALID, "CC", "mac", 1)
        t_unknown = WR.Target(SID_VALID, "CC", "other", 1)

        with (
            mock.patch.object(
                AGW, "deliver_local", return_value=WR.Delivery("completed", "resume")
            ) as spy_local,
            mock.patch(
                "simple_tools.claude_wake.deliver_remote",
                return_value=WR.Delivery("completed", "ssh:resume"),
            ) as spy_remote,
        ):
            d1 = deliver(t_local, self.wake())
            self.assertEqual(d1.outcome, "completed")
            spy_local.assert_called_once()

            d2 = deliver(t_mac, self.wake())
            self.assertEqual(d2.route, "ssh:resume")
            spy_remote.assert_called_once()

            d3 = deliver(t_unknown, self.wake())
            self.assertEqual((d3.outcome, d3.code), ("refused", "unknown-host"))

    def test_server_delivery_without_registry_env_succeeds(self) -> None:
        self._insert(SID_VALID)
        clean_env = dict(self.env)
        clean_env.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_BEARER", None)
        clean_env.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL", None)
        fake_runner = lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode=0)
        deliver = AGW.make_deliver(clean_env, AGW.DEFAULTS, runner=fake_runner)
        target = WR.Target(SID_VALID, "CC", "server", 1)

        with mock.patch.object(AGW, "_find_agy_binary", return_value="/bin/agy"):
            d = deliver(target, self.wake())
        self.assertEqual(d.outcome, "sent")
        self.assertEqual(d.note, "agy-exit-0")


class TestAntigravityDeliverRequest(BaseTest):
    def test_bad_request_shape(self) -> None:
        ans = AGW.deliver_request({}, env=self.env)
        self.assertEqual(ans["delivery"]["outcome"], "refused")
        self.assertEqual(ans["delivery"]["code"], AGW.BAD_REQUEST)

    def test_wrong_host_refused(self) -> None:
        req = {
            "platform": "antigravity",
            "target": {"session_id": SID_VALID, "letter": "CC", "host": "server", "generation": 1},
            "wake": WR.asdict(self.wake()),
        }
        ran = False

        def fake_runner(argv, **kwargs):
            nonlocal ran
            ran = True
            return subprocess.CompletedProcess(argv, returncode=0)

        ans = AGW.deliver_request(req, env=self.env, runner=fake_runner)
        self.assertEqual(ans["delivery"]["outcome"], "refused")
        self.assertEqual(ans["delivery"]["code"], "unknown-host")
        self.assertFalse(ran)

    def test_recheck_released(self) -> None:
        req = {
            "platform": "antigravity",
            "target": {"session_id": SID_VALID, "letter": "CC", "host": "mac", "generation": 1},
            "wake": WR.asdict(self.wake()),
        }
        with mock.patch("simple_tools.wake_router.registry_api.call", return_value={"found": False}):
            ans = AGW.deliver_request(req, env=self.env)
        self.assertEqual(ans["delivery"]["outcome"], "refused")
        self.assertEqual(ans["delivery"]["code"], WR.RELEASED)

    def test_successful_request_correlation(self) -> None:
        self._insert(SID_VALID)
        req = {
            "platform": "antigravity",
            "target": {"session_id": SID_VALID, "letter": "CC", "host": "mac", "generation": 2},
            "wake": WR.asdict(self.wake(wake_id="w-req-42")),
        }
        resolve_ans = {
            "found": True,
            "seat": "AG",
            "platform": "antigravity",
            "native_session_id": SID_VALID,
            "letter": "CC",
            "generation": 2,
            "verified_host": "mac",
        }
        fake_proc = subprocess.CompletedProcess([], 0)
        with (
            mock.patch(
                "simple_tools.wake_router.registry_api.call", return_value=resolve_ans
            ),
            mock.patch.object(AGW, "_find_agy_binary", return_value="/bin/agy"),
        ):
            ans = AGW.deliver_request(
                req, env=self.env, runner=lambda argv, **kwargs: fake_proc
            )

        self.assertEqual(ans["wake_id"], "w-req-42")
        self.assertEqual(ans["session_id"], SID_VALID)
        self.assertEqual(ans["generation"], 2)
        self.assertEqual(ans["delivery"]["outcome"], "sent")
        self.assertEqual(ans["delivery"]["ack"], "unknown")
        self.assertEqual(ans["delivery"]["route"], "resume")


class TestAntigravityBuilderIntegration(unittest.TestCase):
    def test_builder_in_table(self) -> None:
        self.assertIn("AG", SVC.BUILDERS)
        b = SVC.BUILDERS["AG"]
        self.assertEqual(b.platform, "antigravity")
        self.assertEqual(b.module, "simple_tools.antigravity_wake")
        self.assertEqual(b.function, "router_platform")
        self.assertEqual(b.handler, "deliver_request")
        self.assertEqual(b.session_re, SVC.UUID_SESSION)

    def test_by_platform_finds_ag(self) -> None:
        b = SVC.by_platform("antigravity")
        self.assertIsNotNone(b)
        self.assertEqual(b.platform, "antigravity")

    def test_prepare_and_platform_contract(self) -> None:
        ready = SVC.prepare("AG", SVC.BUILDERS["AG"], {})
        self.assertEqual(ready.seat, "AG")
        platform = ready.make({}, AGW.DEFAULTS)
        self.assertIsInstance(platform, WR.Platform)
        self.assertEqual(platform.seat, "AG")
        self.assertEqual(platform.platform, "antigravity")
        self.assertEqual(platform.mention, "Antigravity")
        self.assertEqual(platform.target_re.pattern, SVC.UUID_SESSION)


if __name__ == "__main__":
    unittest.main()
