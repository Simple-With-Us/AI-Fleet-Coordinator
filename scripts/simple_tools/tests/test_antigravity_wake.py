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
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simple_tools import antigravity_wake as AGW
from simple_tools import wake_router as WR
from simple_tools import wake_service as SVC
from simple_tools.tests import test_wake_router as TWR

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

# The lines the compiled `agy` writes to stderr when a resume fails (AFC-838).  Fixtures only:  no
# test here launches the real CLI.
SIGNIN_LINES = (
    "Error: authentication required. Run 'agy' to log in, then retry.",
    "error: authentication failed or timed out",
)
QUOTA_LINES = (
    "error: Individual quota reached.",
    'AGY_ERROR: {"short_error":"Individual quota reached.","status":"RESOURCE_EXHAUSTED",'
    '"error_code":429,"retryable":false,"error_id":"e-1"}',
)
INTERACTIVE_LINES = (
    "Authentication required. Please visit the URL to log in:",
    "  https://example.invalid/auth",
    "Or, paste the authorization code here and press Enter:",
    "Error: authentication timed out.",
)
# printmode.go:440:  the browser branch ran StartOAuthProcess (433) and got no URL back.
BROWSER_BRANCH_LINE = "Error: authentication required. Run 'agy' to log in."
# projectresolve.go:75:  the id was not found, so agy started a new chat.
NEW_CHAT_LINE = 'warning: conversation "%s" not found' % SID_VALID
CANARY = "CANARY-afc838"
RESOLVE_MAC = {
    "found": True,
    "seat": "AG",
    "platform": "antigravity",
    "native_session_id": SID_VALID,
    "letter": "CC",
    "generation": 1,
    "verified_host": "mac",
}

# A stand-in for the compiled sign-in gate (printmode.go:390-397, 425-433):  a character device
# on stdin, or a controlling terminal, takes the browser branch;  anything else fails fast.
FAKE_AGY = """#!@PYTHON@
import os
import stat
import sys


def interactive():
    try:
        if stat.S_ISCHR(os.fstat(0).st_mode):
            return True
    except OSError:
        pass
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


if interactive():
    open(os.path.join(os.environ["HOME"], "browser-opened"), "w").close()
    sys.stderr.write("Authentication required. Please visit the URL to log in:\\n")
    sys.stderr.write("Error: authentication timed out.\\n")
else:
    sys.stderr.write("Error: authentication required. Run 'agy' to log in, then retry.\\n")
    sys.stderr.write("error: authentication failed or timed out\\n")
sys.exit(1)
"""


def agy(returncode: int, *lines: str, calls: list | None = None):
    """A fake `agy` run:  writes `lines` into the stderr pipe it is handed, then exits."""

    def run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        os.write(kwargs["stderr"], "".join(line + "\n" for line in lines).encode("utf-8"))
        return subprocess.CompletedProcess(argv, returncode)

    return run


def open_fds() -> set[str]:
    """This process's open file descriptors (Linux /proc, else the BSD /dev/fd)."""
    for folder in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(folder):
            return set(os.listdir(folder))
    raise unittest.SkipTest("no descriptor listing on this host")


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

    def test_sign_in_never_ran_quota_may_have(self) -> None:
        # A sign-in failure ends before the turn starts;  a quota failure comes after it started.
        self.assertLessEqual({"resume-signin-required", "resume-signin-interactive"}, AGW.NEVER_RAN)
        self.assertNotIn("resume-quota-exhausted", AGW.NEVER_RAN)

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
        custom_base = self.root / "custom-state"
        custom_bridge = custom_base / "ag-wake"
        custom_bridge.mkdir(parents=True)
        fd2 = os.open(custom_bridge / f"session-{SID_VALID}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            env_custom = {**self.env, "AG_WAKE_STATE": str(custom_base)}
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

        # Schema missing parent_conversation_id fails closed (zero execution)
        db_missing_parent = self.root / "missing_parent.db"
        conn = sqlite3.connect(db_missing_parent)
        try:
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT PRIMARY KEY, not_fully_idle INTEGER, killed INTEGER, "
                "nesting_depth INTEGER, workspace_uris TEXT)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, 0, 0, 0, ?)",
                (SID_VALID, json.dumps([f"file://{self.valid_project}"])),
            )
            conn.commit()
        finally:
            conn.close()

        rec_p, err_p = AGW.find_conversation_record(
            {**self.env, "AG_WAKE_STORE": str(db_missing_parent)}, SID_VALID
        )
        self.assertIsNone(rec_p)
        self.assertEqual(err_p, "schema-missing-parent")
        d_p = AGW.deliver_local(
            {**self.env, "AG_WAKE_STORE": str(db_missing_parent)}, AGW.DEFAULTS, SID_VALID, self.wake()
        )
        self.assertEqual((d_p.outcome, d_p.code), ("failed", "store-schema-missing-parent"))

        # Schema missing nesting_depth fails closed (zero execution)
        db_missing_depth = self.root / "missing_depth.db"
        conn = sqlite3.connect(db_missing_depth)
        try:
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT PRIMARY KEY, not_fully_idle INTEGER, killed INTEGER, "
                "parent_conversation_id TEXT, workspace_uris TEXT)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, 0, 0, '', ?)",
                (SID_VALID, json.dumps([f"file://{self.valid_project}"])),
            )
            conn.commit()
        finally:
            conn.close()

        rec_d, err_d = AGW.find_conversation_record(
            {**self.env, "AG_WAKE_STORE": str(db_missing_depth)}, SID_VALID
        )
        self.assertIsNone(rec_d)
        self.assertEqual(err_d, "schema-missing-depth")
        d_d = AGW.deliver_local(
            {**self.env, "AG_WAKE_STORE": str(db_missing_depth)}, AGW.DEFAULTS, SID_VALID, self.wake()
        )
        self.assertEqual((d_d.outcome, d_d.code), ("failed", "store-schema-missing-depth"))

        # deliver_local maps schema-missing to retryable failed
        delivery = AGW.deliver_local(
            {**self.env, "AG_WAKE_STORE": str(db_missing_idle)}, AGW.DEFAULTS, SID_VALID, self.wake()
        )
        self.assertEqual((delivery.outcome, delivery.code), ("failed", "store-schema-missing-idle"))

        # Minimal schema with all required columns succeeds
        db_ok = self.root / "minimal_ok.db"
        conn = sqlite3.connect(db_ok)
        try:
            conn.execute(
                "CREATE TABLE conversation_summaries ("
                "conversation_id TEXT PRIMARY KEY, not_fully_idle INTEGER, killed INTEGER, "
                "parent_conversation_id TEXT, nesting_depth INTEGER, workspace_uris TEXT)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, 0, 0, '', 0, ?)",
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
        self.assertEqual(rec3["parent_conversation_id"], "")
        self.assertEqual(rec3["nesting_depth"], 0)
        self.assertEqual(rec3["title"], "")

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


class ResumeBase(BaseTest):
    """One idle chat on this host, and `agy` found at a fixed path (AFC-838)."""

    def setUp(self) -> None:
        super().setUp()
        self._insert(SID_VALID)
        patcher = mock.patch.object(AGW, "_find_agy_binary", return_value="/bin/agy")
        patcher.start()
        self.addCleanup(patcher.stop)

    def server(self, runner) -> WR.Delivery:
        deliver = AGW.make_deliver(self.env, AGW.DEFAULTS, runner=runner)
        return deliver(WR.Target(SID_VALID, "CC", "server", generation=1), self.wake())

    def mac_request(self) -> dict:
        return {
            "platform": "antigravity",
            "target": {"session_id": SID_VALID, "letter": "CC", "host": "mac", "generation": 1},
            "wake": WR.asdict(self.wake()),
        }

    def mac(self, runner) -> dict:
        with mock.patch("simple_tools.wake_router.registry_api.call", return_value=RESOLVE_MAC):
            return AGW.deliver_request(self.mac_request(), env=self.env, runner=runner)

    @staticmethod
    def four(delivery: WR.Delivery) -> tuple[str, str, str, str]:
        return (delivery.outcome, delivery.route, delivery.code, delivery.note)

    @staticmethod
    def four_of(answer: dict) -> tuple[str, str, str, str]:
        d = answer["delivery"]
        return (d["outcome"], d["route"], d["code"], d["note"])


class TestAntigravityResumeHeadless(ResumeBase):
    """Every resume runs with no terminal:  an empty pipe on stdin and a session of its own, so
    the compiled sign-in fails fast instead of opening a browser (AFC-838)."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_agy = self.root / "bin" / "agy"
        self.fake_agy.parent.mkdir()
        self.fake_agy.write_text(FAKE_AGY.replace("@PYTHON@", sys.executable), encoding="utf-8")
        self.fake_agy.chmod(0o755)

    def record(self, calls: list):
        def run(argv, **kwargs):
            stderr = kwargs.get("stderr")
            is_pipe = type(stderr) is int and stat.S_ISFIFO(os.fstat(stderr).st_mode)
            calls.append((list(argv), dict(kwargs), is_pipe))
            return subprocess.CompletedProcess(argv, 0)

        return run

    def assert_headless(self, calls: list, text: str) -> None:
        self.assertEqual(len(calls), 1)
        argv, kwargs, is_pipe = calls[0]
        self.assertEqual(argv, ["/bin/agy", "--conversation", SID_VALID, "-p", text])
        self.assertEqual(kwargs.get("input"), b"")
        self.assertNotIn("stdin", kwargs)
        self.assertIs(kwargs.get("start_new_session"), True)
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        # stderr is the write end of our own pipe:  a real descriptor, not one of subprocess's
        # PIPE, DEVNULL or STDOUT constants, and not a file on disk.
        self.assertIs(type(kwargs.get("stderr")), int)
        self.assertGreater(kwargs["stderr"], 2)
        self.assertTrue(is_pipe)
        self.assertEqual(kwargs.get("timeout"), 900)
        self.assertEqual(kwargs.get("cwd"), str(self.valid_project))

    def test_server_resume_has_no_tty_and_an_empty_pipe(self) -> None:
        calls: list = []
        delivery = self.server(self.record(calls))
        self.assertEqual(delivery.outcome, "sent")
        self.assert_headless(calls, AGW.wake_text(self.wake()))

    def test_mac_resume_has_no_tty_and_an_empty_pipe(self) -> None:
        calls: list = []
        answer = self.mac(self.record(calls))
        self.assertEqual(answer["delivery"]["outcome"], "sent")
        text = AGW.wake_text(AGW._wake_from(self.mac_request()["wake"]))
        self.assert_headless(calls, text)

    def test_fake_cli_takes_the_fail_fast_branch(self) -> None:
        # The real subprocess.run and a real child process:  no browser branch on either host.
        opened = self.root / "browser-opened"
        with mock.patch.object(AGW, "_find_agy_binary", return_value=str(self.fake_agy)):
            delivery = self.server(subprocess.run)
            self.assertFalse(opened.exists())
            self.assertEqual(
                self.four(delivery),
                ("refused", "resume", "resume-signin-required", "agy-exit-1"),
            )
            answer = self.mac(subprocess.run)
            self.assertFalse(opened.exists())
            self.assertEqual(
                self.four_of(answer),
                ("refused", "resume", "resume-signin-required", "agy-exit-1"),
            )


class TestAntigravityResumeOutcome(ResumeBase):
    """A failed resume is named from a bounded read of the CLI's own stderr lines:  quota, sign-in,
    or today's resume-exit-N.  No output text reaches a receipt (AFC-838)."""

    def test_server_quota_exhausted(self) -> None:
        expected = ("refused", "resume", "resume-quota-exhausted", "agy-exit-3")
        self.assertEqual(self.four(self.server(agy(3, *QUOTA_LINES))), expected)
        self.assertNotIn("resume-quota-exhausted", AGW.NEVER_RAN)
        for line in (
            "error: rpc error: RESOURCE_EXHAUSTED",
            'AGY_ERROR: {"short_error":"request failed with code 429"}',
            "error: rpc error: code = ResourceExhausted desc = quota",
            'AGY_ERROR: {"code": 429, "message": "x"}',
            "error: 429 Too Many Requests",
            "error: You have exhausted your quota on this model.",
            "error: Your AI credits balance is too low to continue.",
        ):
            with self.subTest(line=line):
                self.assertEqual(self.four(self.server(agy(3, line))), expected)

    def test_mac_sign_in_required(self) -> None:
        answer = self.mac(agy(1, *SIGNIN_LINES))
        self.assertEqual(
            self.four_of(answer), ("refused", "resume", "resume-signin-required", "agy-exit-1")
        )
        self.assertIn("resume-signin-required", AGW.NEVER_RAN)

    def test_mac_sign_in_reaches_the_server_receipt(self) -> None:
        config = {**AGW.DEFAULTS, "mac_ssh": "mac"}
        commands: list[str] = []

        def ssh(argv, **kwargs):
            commands.append(argv[-1])
            if argv[-1] == "true":
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            request = json.loads(kwargs["input"])
            answer = AGW.deliver_request(request, env=self.env, runner=agy(1, *SIGNIN_LINES))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(answer) + "\n", stderr="")

        with mock.patch("simple_tools.wake_router.registry_api.call", return_value=RESOLVE_MAC):
            deliver = AGW.make_deliver(self.env, config, runner=ssh)
            delivery = deliver(WR.Target(SID_VALID, "CC", "mac", generation=1), self.wake())
        self.assertEqual(len(commands), 2)
        self.assertEqual(
            delivery,
            WR.Delivery(
                "refused", "ssh:resume", code="resume-signin-required", note="agy-exit-1"
            ),
        )

    def test_interactive_sign_in_is_named(self) -> None:
        for last in (
            "Error: authentication timed out.",
            "Error: authentication interrupted.",
            "Error: authentication failed: x",
        ):
            with self.subTest(last=last):
                delivery = self.server(agy(1, *INTERACTIVE_LINES[:-1], last))
                self.assertEqual(
                    self.four(delivery),
                    ("refused", "resume", "resume-signin-interactive", "agy-exit-1"),
                )
        self.assertIn("resume-signin-interactive", AGW.NEVER_RAN)

    def test_browser_branch_sign_in_is_interactive(self) -> None:
        # printmode.go:440 prints the same opening as the fast exit (428), but only after
        # StartOAuthProcess ran:  the no-browser stdio did not hold, so it is not "required".
        expected = ("refused", "resume", "resume-signin-interactive", "agy-exit-1")
        tail = (BROWSER_BRANCH_LINE, "error: authentication failed or timed out")
        self.assertEqual(self.four(self.server(agy(1, *tail))), expected)
        self.assertEqual(self.four_of(self.mac(agy(1, *tail))), expected)
        # An opening with neither of the two endings is not read as either branch.
        delivery = self.server(agy(1, "Error: authentication required. Sign in elsewhere."))
        self.assertEqual(self.four(delivery), ("refused", "resume", "resume-exit-1", ""))

    def test_the_last_line_that_names_an_ending_decides(self) -> None:
        cases = (
            # A sign-in-shaped line from another process, then the CLI's own quota ending.
            ((SIGNIN_LINES[0], "tool says hello", *QUOTA_LINES), 3, "resume-quota-exhausted"),
            # A quota-shaped line earlier, then the CLI's own fast sign-in exit.
            ((QUOTA_LINES[0], *SIGNIN_LINES), 1, "resume-signin-required"),
            ((SIGNIN_LINES[0], BROWSER_BRANCH_LINE), 1, "resume-signin-interactive"),
        )
        for lines, returncode, code in cases:
            with self.subTest(code=code):
                delivery = self.server(agy(returncode, *lines))
                self.assertEqual(
                    self.four(delivery), ("refused", "resume", code, "agy-exit-%d" % returncode)
                )

    def test_unknown_failure_keeps_today_s_code(self) -> None:
        cases = (
            (1, ("error: invalid model",)),
            (3, ('AGY_ERROR: {"short_error":"x","status":"INTERNAL","error_code":500}',)),
            (2, ()),
            (4, ("error: request 4290 failed", "error: line 1429 of the plan")),
            (5, (NEW_CHAT_LINE, "error: invalid model")),
        )
        for returncode, lines in cases:
            with self.subTest(returncode=returncode):
                delivery = self.server(agy(returncode, *lines))
                self.assertEqual(
                    self.four(delivery), ("refused", "resume", "resume-exit-%d" % returncode, "")
                )

    def test_success_names_only_a_new_chat(self) -> None:
        delivery = self.server(agy(0, QUOTA_LINES[1], SIGNIN_LINES[0]))
        self.assertEqual(
            (delivery.outcome, delivery.route, delivery.ack, delivery.note, delivery.code),
            ("sent", "resume", "unknown", "agy-exit-0", ""),
        )
        # The CLI could not find the chat and ran the turn in a new one (projectresolve.go:75):
        # final, never retried, and not "never ran", since a turn did run.
        expected = ("refused", "resume", "resume-new-chat", "agy-exit-0")
        self.assertEqual(self.four(self.server(agy(0, NEW_CHAT_LINE))), expected)
        self.assertEqual(self.four_of(self.mac(agy(0, NEW_CHAT_LINE))), expected)
        self.assertNotIn("resume-new-chat", AGW.NEVER_RAN)
        delivery = self.server(agy(0, "note: " + NEW_CHAT_LINE))
        self.assertEqual(delivery.outcome, "sent")

    def test_markers_must_open_a_line(self) -> None:
        lines = ("tool output: RESOURCE_EXHAUSTED", "note: Error: authentication required.")
        delivery = self.server(agy(1, *lines))
        self.assertEqual(self.four(delivery), ("refused", "resume", "resume-exit-1", ""))

    def test_only_the_tail_is_read(self) -> None:
        cap = AGW.OUTPUT_SCAN_CAP
        filler = ["filler line %06d" % i for i in range((cap + 100) // 19 + 1)]
        self.assertGreater(sum(len(line) + 1 for line in filler), cap + 100)
        mark = SIGNIN_LINES[0]
        with self.subTest("a sign-in line before the tail"):
            delivery = self.server(agy(1, mark, *filler))
            self.assertEqual(self.four(delivery), ("refused", "resume", "resume-exit-1", ""))
        with self.subTest("a sign-in line in the tail"):
            delivery = self.server(agy(1, *filler, mark))
            self.assertEqual(
                self.four(delivery),
                ("refused", "resume", "resume-signin-required", "agy-exit-1"),
            )
        with self.subTest("a sign-in line the cap cuts"):
            # The cap starts the read exactly at the mark inside this line:  the cut first line
            # is dropped, so it is not read as one the CLI began.
            cut = "x" * 5 + mark
            rest = "f" * (cap + 4 - len(cut) - 1)
            self.assertEqual(len(cut) + 1 + len(rest) + 1, cap + 5)
            delivery = self.server(agy(1, cut, rest))
            self.assertEqual(self.four(delivery), ("refused", "resume", "resume-exit-1", ""))
        # Only the cap is kept in memory, however much the CLI writes.
        tail = AGW._StderrTail(cap)
        for _ in range(3 * cap // 100 + 1):
            os.write(tail.write_fd, ("y" * 99 + "\n").encode("ascii"))
        tail.finish(5.0)
        self.assertLessEqual(len(tail._buf), cap)
        self.assertTrue(tail.text())
        self.assertLessEqual(len(tail.text().encode("utf-8")), cap)

    def test_receipt_carries_no_output_text(self) -> None:
        fixtures = (
            (
                "resume-quota-exhausted",
                3,
                (
                    "error: Individual quota reached. " + CANARY,
                    'AGY_ERROR: {"short_error":"Individual quota reached. %s",'
                    '"status":"RESOURCE_EXHAUSTED","error_code":429}' % CANARY,
                ),
            ),
            ("resume-signin-required", 1, (SIGNIN_LINES[0], SIGNIN_LINES[1] + " " + CANARY)),
            (
                "resume-signin-interactive",
                1,
                (
                    INTERACTIVE_LINES[0],
                    "  https://example.invalid/auth?" + CANARY,
                    INTERACTIVE_LINES[-1],
                ),
            ),
            ("resume-signin-interactive", 1, (CANARY, BROWSER_BRANCH_LINE)),
            ("resume-new-chat", 0, (NEW_CHAT_LINE, "turn note " + CANARY)),
        )
        for code, returncode, lines in fixtures:
            with self.subTest(code=code):
                delivery = self.server(agy(returncode, *lines))
                self.assertEqual(delivery.code, code)
                self.assertNotIn(CANARY, json.dumps(WR.asdict(delivery)))
                answer = self.mac(agy(returncode, *lines))
                self.assertEqual(answer["delivery"]["code"], code)
                self.assertNotIn(CANARY, json.dumps(answer))

    def test_capture_closes_every_descriptor(self) -> None:
        def raises(exc):
            def run(argv, **kwargs):
                raise exc

            return run

        runners = (
            ("sign-in", agy(1, *SIGNIN_LINES)),
            ("success", agy(0)),
            ("timeout", raises(subprocess.TimeoutExpired(["agy"], timeout=10))),
            ("no exec", raises(FileNotFoundError("agy"))),
            ("other", raises(RuntimeError("x"))),
        )
        self.server(agy(0))  # the lock file and folders exist before the count
        before = open_fds()
        for name, runner in runners:
            with self.subTest(name):
                self.server(runner)
                self.assertEqual(open_fds(), before)
        names = sorted(p.name for p in (AGW.state_dir(self.env) / "locks").iterdir())
        self.assertEqual(names, ["session-%s.lock" % SID_VALID])
        self.assertEqual(list(self.valid_project.iterdir()), [])

    def test_capture_failure_runs_nothing(self) -> None:
        before = open_fds()
        calls: list = []
        with mock.patch.object(AGW.os, "pipe", side_effect=OSError):
            delivery = self.server(agy(0, calls=calls))
        self.assertEqual(
            (delivery.outcome, delivery.route, delivery.code),
            ("failed", "resume", "resume-capture-OSError"),
        )
        with mock.patch.object(AGW.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("can't start new thread")
            delivery = self.server(agy(0, calls=calls))
        self.assertEqual(
            (delivery.outcome, delivery.route, delivery.code),
            ("failed", "resume", "resume-capture-RuntimeError"),
        )
        self.assertEqual(calls, [])
        self.assertEqual(open_fds(), before)

    def test_a_process_left_holding_the_pipe_is_not_waited_for(self) -> None:
        # A real child that keeps agy's stderr open after agy itself has exited:  the answer comes
        # after the short wait, from what was read by then, and the child is never blocked.
        held: list = []
        tails: list = []
        real_tail = AGW._StderrTail

        def make_tail(cap):
            tails.append(real_tail(cap))
            return tails[-1]

        def run(argv, **kwargs):
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=kwargs["stderr"],
            )
            held.append(child)
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            os.write(kwargs["stderr"], (SIGNIN_LINES[0] + "\n").encode("utf-8"))
            return subprocess.CompletedProcess(argv, 1)

        # Runs last:  once the child is gone the drain thread reads the end and closes its side.
        self.addCleanup(lambda: [tail._done.wait(10) for tail in tails])
        with (
            mock.patch.object(AGW, "OUTPUT_WAIT_SECONDS", 0.5),
            mock.patch.object(AGW, "_StderrTail", side_effect=make_tail),
        ):
            started = time.monotonic()
            delivery = self.server(run)
            elapsed = time.monotonic() - started
        self.assertEqual((len(held), len(tails)), (1, 1))
        self.assertIsNone(held[0].poll())
        self.assertFalse(tails[0]._done.is_set())  # still draining for the child
        self.assertLess(elapsed, 10)
        self.assertEqual(
            self.four(delivery), ("refused", "resume", "resume-signin-required", "agy-exit-1")
        )


class TestAntigravityEndingsInTheRouter(TWR.Base):
    """The new AG endings through the router itself, with the router tests' fixtures."""

    def test_antigravity_failures_settle_once(self) -> None:
        """A sign-in or quota failure is one final receipt (AFC-838):  no retry against the quota
        or the missing sign-in, and no hold on the chat, so the next wake to it runs as usual."""
        self.registry.rows = [TWR.row(TWR.SID_A, "AA", "Only chat")]
        cases = (
            ("resume-signin-required", False),
            ("resume-signin-interactive", False),
            ("resume-quota-exhausted", None),
        )
        for code, delivered in cases:
            with self.subTest(code=code):
                self.delivered.clear()
                self.results = [WR.Delivery("refused", "resume", code=code, note="agy-exit-1")]
                router = self.router()
                router.never_ran = WR.NEVER_RAN | AGW.NEVER_RAN
                wake_id = "w-" + code
                router.accept(self.wake(wake_id))
                self.drain(router)
                self.assertEqual(len(self.delivered), 1)
                receipt = self.done(wake_id)
                self.assertEqual(receipt["status"], "dead")
                self.assertIs(receipt["delivered"], delivered)
                self.assertFalse(self.pending(wake_id).exists())
                router.accept(self.wake(wake_id + "-next"))
                self.drain(router)
                self.assertEqual(
                    self.delivered, [(TWR.SID_A, wake_id), (TWR.SID_A, wake_id + "-next")]
                )
                self.assertEqual(self.done(wake_id + "-next")["status"], "delivered")


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
