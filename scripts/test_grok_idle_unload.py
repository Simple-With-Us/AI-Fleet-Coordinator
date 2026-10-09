#!/usr/bin/env python3
"""Tests for scripts/grok-acp-runtime/grok-idle-unload.py and its eligibility rules.

Self-contained: a temporary ~/.grok layout (SESSIONS_ROOT, ACTIVE_SESSIONS) and
fixture processes.  Touches no live Grok state, spawns no leader client, and
never signals a real process.  Runs on macOS and Linux.

  python3 scripts/test_grok_idle_unload.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
DISK = REPO / "scripts" / "grok-acp-runtime"
sys.path.insert(0, str(DISK))

import session_disk  # noqa: E402
from session_disk import (  # noqa: E402
    DEFAULT_IDLE_UNLOAD_SEC,
    DEFAULT_STUB_IDLE_SEC,
    annotate_user_turns,
    apply_pid_liveness,
    count_real_user_turns,
    is_stub_row,
    last_activity_epoch,
    select_idle_unload,
    unload_skip_reason,
)

_spec = importlib.util.spec_from_file_location("grok_idle_unload", DISK / "grok-idle-unload.py")
gu = importlib.util.module_from_spec(_spec)
sys.modules["grok_idle_unload"] = gu
_spec.loader.exec_module(gu)

NOW = 1_800_000_000.0
MIN = 60.0
HOUR = 3600.0
SID = "01a11e6f-a47a-7a30-ac9f-96e043bc698d"
SID2 = "01a11e6b-c206-7992-9be2-9fddd804d7fb"
SID3 = "01a11d80-dc01-77c1-a08a-6c22370468d7"


def row(**kw):
    """A loaded, unattached, zero-turn chat idle for 40 minutes (a stub)."""
    base = {
        "sessionId": SID,
        "live": False,
        "loaded": True,
        "turnState": "idle",
        "userTurns": 0,
        "updatedAt": NOW - 40 * MIN,
    }
    base.update(kw)
    return base


def reason(r, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("self_id", "me")
    kw.setdefault("stub_idle_sec", DEFAULT_STUB_IDLE_SEC)
    return unload_skip_reason(r, **kw)


class EligibilityTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(DEFAULT_IDLE_UNLOAD_SEC, 4 * 3600)
        self.assertEqual(DEFAULT_STUB_IDLE_SEC, 30 * 60)

    def test_stub_unloads_after_thirty_minutes(self) -> None:
        self.assertIsNone(reason(row()))
        self.assertEqual(reason(row(updatedAt=NOW - 20 * MIN)), "fresh")

    def test_stub_without_stub_clock_uses_long_clock(self) -> None:
        self.assertEqual(reason(row(), stub_idle_sec=None), "fresh")
        self.assertEqual(reason(row(), stub_idle_sec=0), "fresh")

    def test_ordinary_chat_uses_four_hour_clock(self) -> None:
        busy_history = {"userTurns": 3}
        self.assertEqual(reason(row(updatedAt=NOW - 3 * HOUR, **busy_history)), "fresh")
        self.assertIsNone(reason(row(updatedAt=NOW - 5 * HOUR, **busy_history)))

    def test_stub_rule_never_applies_when_a_client_is_attached(self) -> None:
        attached = row(live=True)
        self.assertFalse(is_stub_row(attached))
        self.assertEqual(reason(attached), "fresh")
        self.assertIsNone(reason(row(live=True, updatedAt=NOW - 5 * HOUR)))

    def test_unknown_history_is_not_a_stub(self) -> None:
        unknown = row(userTurns=None)
        self.assertFalse(is_stub_row(unknown))
        self.assertEqual(reason(unknown), "fresh")

    def test_a_started_turn_is_not_a_stub(self) -> None:
        self.assertFalse(is_stub_row(row(turnStartedAt=NOW - 35 * MIN)))

    def test_recent_turn_activity_keeps_a_chat_up(self) -> None:
        r = row(userTurns=2, updatedAt=NOW - 9 * HOUR, turnEndedAt=NOW - 10 * MIN)
        self.assertEqual(last_activity_epoch(r), NOW - 10 * MIN)
        self.assertEqual(reason(r), "fresh")

    def test_existing_skips_still_win_over_the_stub_clock(self) -> None:
        self.assertEqual(reason(row(turnState="working")), "busy")
        self.assertEqual(reason(row(turnState="needs-input")), "busy")
        self.assertEqual(reason(row(pendingTool="bash")), "pending_tool")
        self.assertEqual(reason(row(sessionId="me")), "self")
        self.assertEqual(reason(row(backgroundPids=2)), "background_process")
        self.assertEqual(reason(row(sessionId="")), "no_id")
        no_ts = row()
        no_ts.pop("updatedAt")
        self.assertEqual(reason(no_ts), "no_timestamp")

    def test_a_chat_with_no_mcp_children_and_no_client_is_left_alone(self) -> None:
        self.assertEqual(reason(row(loaded=False)), "not_loaded")

    def test_select_picks_only_eligible_rows(self) -> None:
        rows = [
            row(sessionId="a"),
            row(sessionId="b", updatedAt=NOW - 5 * MIN),
            row(sessionId="c", turnState="working"),
            row(sessionId="d", loaded=False),
        ]
        picked = select_idle_unload(
            rows, now=NOW, self_id="me", stub_idle_sec=DEFAULT_STUB_IDLE_SEC
        )
        self.assertEqual([r["sessionId"] for r in picked], ["a"])


class SessionFixture(unittest.TestCase):
    """A throwaway ~/.grok/sessions plus active_sessions.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.sessions = self.home / "sessions"
        self.active = self.home / "active_sessions.json"
        patches = [
            patch.object(session_disk, "SESSIONS_ROOT", self.sessions),
            patch.object(session_disk, "ACTIVE_SESSIONS", self.active),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_session(self, sid: str, history: list[dict], updated: str = "2026-10-08T18:00:00Z") -> Path:
        d = self.sessions / "%2FUsers%2Fjay" / sid
        d.mkdir(parents=True)
        with (d / "chat_history.jsonl").open("w", encoding="utf-8") as fh:
            for entry in history:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        (d / "summary.json").write_text(
            json.dumps({"info": {"id": sid, "cwd": "/Users/jay"}, "updated_at": updated}),
            encoding="utf-8",
        )
        (d / "events.jsonl").write_text("", encoding="utf-8")
        return d


SYSTEM = {"type": "system", "content": "system prompt"}
REMINDER = {
    "type": "user",
    "content": [{"type": "text", "text": "<system-reminder>ctx</system-reminder>"}],
    "synthetic_reason": "system_reminder",
}
REAL_USER = {"type": "user", "content": [{"type": "text", "text": "hello"}]}


class StubDetectionTests(SessionFixture):
    def test_system_prompt_plus_reminder_is_zero_turns(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        self.assertEqual(count_real_user_turns(SID), 0)

    def test_synthetic_user_entries_never_count(self) -> None:
        meta = dict(REMINDER, synthetic_reason="compaction_meta")
        done = dict(REMINDER, synthetic_reason="task_completed")
        self.make_session(SID, [SYSTEM, REMINDER, meta, done])
        self.assertEqual(count_real_user_turns(SID), 0)

    def test_a_real_user_message_counts(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER, REAL_USER, REMINDER])
        self.assertEqual(count_real_user_turns(SID), 1)

    def test_text_that_mentions_user_does_not_count(self) -> None:
        assistant = {"type": "assistant", "content": '{"type":"user"} in prose'}
        self.make_session(SID, [SYSTEM, REMINDER, assistant])
        self.assertEqual(count_real_user_turns(SID), 0)

    def test_missing_history_is_unknown_not_a_stub(self) -> None:
        d = self.make_session(SID, [SYSTEM])
        (d / "chat_history.jsonl").unlink()
        self.assertIsNone(count_real_user_turns(SID))
        self.assertIsNone(count_real_user_turns("no-such-session"))

    def test_annotate_only_touches_loaded_unattached_rows(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        self.make_session(SID2, [SYSTEM, REMINDER, REAL_USER])
        rows = [
            {"sessionId": SID, "loaded": True, "live": False},
            {"sessionId": SID2, "loaded": True, "live": False},
            {"sessionId": SID3, "loaded": False, "live": False},
            {"sessionId": "attached", "loaded": True, "live": True},
        ]
        annotate_user_turns(rows)
        self.assertEqual(rows[0]["userTurns"], 0)
        self.assertEqual(rows[1]["userTurns"], 1)
        self.assertNotIn("userTurns", rows[2])
        self.assertNotIn("userTurns", rows[3])

    def test_stale_active_entry_with_a_dead_pid_is_not_attached(self) -> None:
        rows = [
            {"sessionId": "a", "live": True, "pid": os.getpid()},
            {"sessionId": "b", "live": True, "pid": 2_000_000_000},
            {"sessionId": "c", "live": True, "pid": None},
        ]
        apply_pid_liveness(rows)
        self.assertTrue(rows[0]["live"])
        self.assertFalse(rows[1]["live"])
        self.assertTrue(rows[1]["stalePid"])
        self.assertTrue(rows[2]["live"])  # cannot tell, so keep it protected


def procargs(argv: list[str], env: dict[str, str], exe: str = "/opt/x/grok") -> bytes:
    """Build a KERN_PROCARGS2 buffer: argc, exec path, padding, argv, env."""
    out = struct.pack("i", len(argv)) + exe.encode() + b"\0\0\0"
    out += b"".join(a.encode() + b"\0" for a in argv)
    out += b"".join(("%s=%s" % kv).encode() + b"\0" for kv in env.items())
    return out + b"\0\0"


class ProcargsTests(unittest.TestCase):
    def test_reads_session_id_from_the_environment(self) -> None:
        raw = procargs(["node", "server.js"], {"PATH": "/bin", "GROK_SESSION_ID": SID, "TOKEN": "s3cret"})
        argv, env = gu.parse_procargs(raw)
        self.assertEqual(argv, ["node", "server.js"])
        self.assertEqual(env, {"GROK_SESSION_ID": SID})

    def test_unwanted_environment_is_dropped(self) -> None:
        raw = procargs(["x"], {"API_KEY": "live-key-value", "GROK_SESSION_ID": SID})
        _, env = gu.parse_procargs(raw)
        self.assertNotIn("API_KEY", env)
        self.assertNotIn("live-key-value", json.dumps(env))

    def test_session_id_inside_argv_is_not_environment(self) -> None:
        raw = procargs(["node", "GROK_SESSION_ID=%s" % SID], {"PATH": "/bin"})
        argv, env = gu.parse_procargs(raw)
        self.assertIn("GROK_SESSION_ID=%s" % SID, argv)
        self.assertEqual(env, {})
        self.assertIsNone(gu.session_id_from_env(env))

    def test_junk_ids_are_rejected(self) -> None:
        for junk in ("", "abc", SID + "-extra", "../../etc/passwd"):
            self.assertIsNone(gu.session_id_from_env({"GROK_SESSION_ID": junk}))

    def test_short_and_empty_buffers_are_safe(self) -> None:
        self.assertEqual(gu.parse_procargs(b""), ([], {}))
        self.assertEqual(gu.parse_procargs(b"\x01\x00\x00\x00"), ([], {}))
        self.assertEqual(gu.parse_procargs(struct.pack("i", 3) + b"/bin/x"), ([], {}))

    def test_empty_argument_is_kept(self) -> None:
        raw = procargs(["tool", "", "last"], {"GROK_SESSION_ID": SID})
        argv, env = gu.parse_procargs(raw)
        self.assertEqual(argv, ["tool", "", "last"])
        self.assertEqual(env["GROK_SESSION_ID"], SID)

    def test_leader_is_the_bare_subcommand_not_the_stdio_client(self) -> None:
        leader = ["/Users/jay/.grok/bin/grok-1.0.50", "agent", "--always-approve", "leader", "--no-exit-on-disconnect"]
        client = ["/Users/jay/.grok/bin/grok", "agent", "--always-approve", "--leader", "stdio"]
        tui = ["/Users/jay/.grok/bin/grok", "agent", "--always-approve", "--no-leader", "serve"]
        self.assertTrue(gu.is_leader_argv(leader))
        self.assertFalse(gu.is_leader_argv(client))
        self.assertFalse(gu.is_leader_argv(tui))
        self.assertFalse(gu.is_leader_argv(["/usr/bin/vim", "agent", "leader"]))
        self.assertFalse(gu.is_leader_argv([]))


def proc(pid, ppid, sid=None, leader=False, started=0.0):
    return gu.ProcRow(pid=pid, ppid=ppid, comm="p%d" % pid, started=started, leader=leader, session_id=sid)


class ProcessTreeTests(unittest.TestCase):
    def test_tree_and_outside_are_split_by_the_leader(self) -> None:
        procs = [
            proc(1, 0),
            proc(10, 1, leader=True),
            proc(11, 10, SID),  # MCP server
            proc(12, 11, SID),  # its child (npx -> node)
            proc(20, 10, SID2),
            proc(30, 1, SID2),  # outlived its parent: background work
            proc(40, 1, SID3),  # not under the leader at all
            proc(50, 10),  # no session id
        ]
        smap = gu.classify_sessions(procs)
        self.assertEqual(sorted(smap[SID]["tree"]), [11, 12])
        self.assertEqual(smap[SID]["outside"], [])
        self.assertEqual(smap[SID2], {"tree": [20], "outside": [30]})
        self.assertEqual(smap[SID3], {"tree": [], "outside": [40]})

    def test_the_helper_itself_is_not_counted(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID), proc(99, 1, SID)]
        smap = gu.classify_sessions(procs, self_pid=99)
        self.assertEqual(smap[SID], {"tree": [11], "outside": []})

    def test_no_leader_means_nothing_is_loaded(self) -> None:
        smap = gu.classify_sessions([proc(11, 1, SID)])
        self.assertEqual(smap[SID]["tree"], [])


class BuildRowsTests(SessionFixture):
    def test_loaded_session_beyond_the_first_list_page_is_added(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER], updated="2026-10-08T18:00:00Z")
        smap = {SID: {"tree": [11, 12], "outside": []}}
        rows = gu.build_rows([], smap)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertTrue(r["loaded"])
        self.assertEqual(r["procs"], 2)
        self.assertEqual(r["userTurns"], 0)
        self.assertTrue(is_stub_row(r))

    def test_unloaded_listed_sessions_stay_unloaded(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        listed = [{"sessionId": SID, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"}]
        rows = gu.build_rows(listed, {})
        self.assertFalse(rows[0]["loaded"])
        self.assertEqual(unload_skip_reason(rows[0], now=NOW, self_id="me"), "not_loaded")

    def test_background_processes_are_recorded(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER, REAL_USER])
        smap = {SID: {"tree": [11], "outside": [30, 31]}}
        rows = gu.build_rows([], smap)
        self.assertEqual(rows[0]["backgroundPids"], 2)

    def test_missing_directory_is_not_a_row(self) -> None:
        rows = gu.build_rows([], {SID: {"tree": [11], "outside": []}})
        self.assertEqual(rows, [])


def fake_leader_script(tmp: Path, body: str) -> Path:
    path = tmp / "fake-leader.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class HelperTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def run_with(self, script: Path, timeout: float) -> object:
        with patch.object(gu, "LEADER", script), patch.object(gu, "PY", sys.executable):
            return gu._run_leader(["list"], timeout, "list")

    def test_a_hung_helper_is_killed_and_reported_as_a_timeout(self) -> None:
        script = fake_leader_script(self.tmp, "import time\ntime.sleep(60)\n")
        started = time.time()
        with self.assertRaises(gu.HelperTimeout) as ctx:
            self.run_with(script, 1.0)
        self.assertLess(time.time() - started, 30.0)
        self.assertEqual(ctx.exception.call, "list")
        self.assertEqual(ctx.exception.timeout, 1.0)

    def test_the_inner_rpc_timeout_is_also_a_timeout(self) -> None:
        script = fake_leader_script(
            self.tmp,
            """
            import json, sys
            print(json.dumps({"ok": False, "error": "no ACP result for session/list within 20s"}), file=sys.stderr)
            sys.exit(1)
            """,
        )
        with self.assertRaises(gu.HelperTimeout):
            self.run_with(script, 30.0)

    def test_the_helper_gets_a_budget_a_little_under_the_outer_timeout(self) -> None:
        script = fake_leader_script(
            self.tmp,
            """
            import json, sys
            print(json.dumps({"ok": True, "argv": sys.argv[1:]}))
            """,
        )
        out = self.run_with(script, 180.0)
        self.assertEqual(out["argv"][:2], ["--rpc-timeout", "165"])
        self.assertEqual(out["argv"][2:], ["list"])

    def test_other_helper_errors_are_not_timeouts(self) -> None:
        script = fake_leader_script(
            self.tmp,
            """
            import json, sys
            print(json.dumps({"ok": False, "error": "boom"}), file=sys.stderr)
            sys.exit(1)
            """,
        )
        out = self.run_with(script, 30.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "boom")


class MainTests(SessionFixture):
    """main() end to end with the process table, leader and kill mocked out."""

    def setUp(self) -> None:
        super().setUp()
        self.make_session(SID, [SYSTEM, REMINDER], updated="2026-10-08T18:00:00Z")  # stub
        self.make_session(SID2, [SYSTEM, REMINDER, REAL_USER], updated="2026-10-08T18:00:00Z")
        self.procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID),
            proc(21, 10, SID2),
        ]
        self.listed = [
            {"sessionId": SID, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"},
            {"sessionId": SID2, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"},
        ]
        # Pretend it is 18:35 UTC, 35 minutes after the last update.
        self.now = session_disk._parse_ts("2026-10-08T18:35:00Z")
        self.closed: list[str] = []
        self.killed: list[int] = []
        # What the live process table says right before a SIGTERM.
        self.pid_sid: dict[int, str] = {11: SID, 21: SID2}
        env = patch.dict(os.environ, {"GROK_SESSION_ID": "me"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, argv, *, close=None, listing=None, now=None):
        def fake_close(sid, cwd, timeout):
            self.closed.append(sid)
            if close is not None:
                return close(sid)
            return {"ok": True, "sessionId": sid, "diskKept": True}

        def fake_list(timeout):
            if isinstance(listing, Exception):
                raise listing
            return self.listed

        patches = [
            patch.object(gu, "scan_processes", return_value=self.procs),
            patch.object(gu, "list_leader_sessions", side_effect=fake_list),
            patch.object(gu, "close_session", side_effect=fake_close),
            patch.object(gu, "grok_session_id_from_pid", side_effect=lambda pid: self.pid_sid.get(pid)),
            patch.object(gu.os, "kill", side_effect=lambda pid, sig: self.killed.append(pid) if sig == 15 else None),
            patch.object(gu.time, "time", return_value=now or self.now),
            patch.object(gu.os, "getpid", return_value=99999),
        ]
        for p in patches:
            p.start()
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gu.main(argv)
        finally:
            for p in reversed(patches):
                p.stop()
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else {}), err.getvalue()

    def test_only_the_stub_is_closed_inside_four_hours(self) -> None:
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(self.closed, [SID])
        self.assertEqual(out["closed"][0]["kind"], "stub")
        self.assertEqual(out["skippedByReason"], {"fresh": 1})
        self.assertNotIn("skipped", out)  # quiet unless --verbose or --dry-run

    def test_the_ordinary_chat_unloads_after_four_hours(self) -> None:
        later = self.now + 4 * HOUR
        code, out, _ = self.run_main([], now=later)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.closed), sorted([SID, SID2]))
        kinds = {c["sessionId"]: c["kind"] for c in out["closed"]}
        self.assertEqual(kinds, {SID: "stub", SID2: "idle"})

    def test_env_overrides_both_clocks(self) -> None:
        # 65 minutes idle: under the 90 minute stub clock, over the 1 hour long clock.
        env = {"GROK_IDLE_UNLOAD_STUB_MINUTES": "90", "GROK_IDLE_UNLOAD_HOURS": "1"}
        with patch.dict(os.environ, env):
            code, out, _ = self.run_main([], now=self.now + 30 * MIN)
        self.assertEqual(code, 0)
        self.assertEqual(self.closed, [SID2])
        self.assertEqual(out["stubMinutes"], 90.0)
        self.assertEqual(out["maxAgeHours"], 1.0)

    def test_stub_rule_can_be_turned_off(self) -> None:
        code, out, _ = self.run_main(["--stub-minutes", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(self.closed, [])

    def test_dry_run_never_closes_or_signals(self) -> None:
        code, out, _ = self.run_main(["--dry-run"], now=self.now + 5 * HOUR)
        self.assertEqual(code, 0)
        self.assertEqual(self.closed, [])
        self.assertEqual(self.killed, [])
        self.assertTrue(all(c["action"] == "would_close" for c in out["closed"]))
        self.assertIn("skipped", out)

    def test_self_session_is_never_closed(self) -> None:
        with patch.dict(os.environ, {"GROK_SESSION_ID": SID}):
            code, out, _ = self.run_main([], now=self.now + 5 * HOUR)
        self.assertNotIn(SID, self.closed)
        self.assertEqual(out["skippedByReason"].get("self"), 1)

    def test_a_background_process_outside_the_leader_protects_the_chat(self) -> None:
        self.procs.append(proc(77, 1, SID2))  # a dev server that outlived its tool call
        code, out, _ = self.run_main([], now=self.now + 5 * HOUR)
        self.assertNotIn(SID2, self.closed)
        self.assertEqual(out["skippedByReason"].get("background_process"), 1)

    def test_busy_chat_is_not_closed(self) -> None:
        events = self.sessions / "%2FUsers%2Fjay" / SID2 / "events.jsonl"
        events.write_text(
            json.dumps({"type": "turn_started", "ts": "2026-10-08T18:30:00Z"}) + "\n",
            encoding="utf-8",
        )
        code, out, _ = self.run_main([], now=self.now + 2 * MIN)
        self.assertNotIn(SID2, self.closed)

    def test_list_timeout_still_closes_loaded_stubs_then_exits_75(self) -> None:
        code, out, err = self.run_main([], listing=gu.HelperTimeout("list", 180.0))
        self.assertEqual(code, gu.EX_TEMPFAIL)
        self.assertEqual(self.closed, [SID])  # discovered from the process table + disk
        line = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(line["error"], "helper timed out")
        self.assertEqual(line["call"], "list")
        self.assertEqual(line["timeoutSec"], 180.0)
        self.assertRegex(line["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(out["timeouts"][0]["call"], "list")

    def test_close_timeout_stops_further_closes_and_falls_back_to_sigterm(self) -> None:
        def slow(sid):
            raise gu.HelperTimeout("close", 180.0)

        code, out, err = self.run_main([], close=slow, now=self.now + 5 * HOUR)
        self.assertEqual(code, gu.EX_TEMPFAIL)
        self.assertEqual(len(self.closed), 1)  # the second close was deferred
        actions = sorted(c["action"] for c in out["closed"])
        self.assertEqual(actions, ["deferred", "session/close"])
        timed_out = next(c for c in out["closed"] if c["action"] == "session/close")
        self.assertEqual(self.killed, [11 if timed_out["sessionId"] == SID else 21])

    def test_failed_close_terminates_that_chats_mcp_children_only(self) -> None:
        def fail(sid):
            return {"ok": False, "error": "boom"}

        code, out, _ = self.run_main([], close=fail)
        self.assertEqual(code, 1)
        self.assertEqual(self.killed, [11])  # only the candidate (the stub), not SID2
        self.assertTrue(out["orphans"][0]["fallbackAfterCloseFailure"])

    def test_a_pid_reused_during_a_slow_close_is_not_signalled(self) -> None:
        def fail_and_reuse(sid):
            # By the time the close returns, pid 11 belongs to someone else.
            self.pid_sid[11] = "01aaaaaa-0000-7000-8000-000000000000"
            return {"ok": False, "error": "boom"}

        code, out, _ = self.run_main([], close=fail_and_reuse)
        self.assertEqual(self.killed, [])
        self.assertTrue(out["orphans"][0]["fallbackAfterCloseFailure"])

    def test_a_child_that_already_exited_is_not_signalled(self) -> None:
        def fail_and_exit(sid):
            self.pid_sid.pop(11, None)  # the session's MCP child is gone
            return {"ok": False, "error": "boom"}

        self.run_main([], close=fail_and_exit)
        self.assertEqual(self.killed, [])

    def test_no_reap_orphans_leaves_processes_alone(self) -> None:
        def fail(sid):
            return {"ok": False, "error": "boom"}

        self.run_main(["--no-reap-orphans"], close=fail)
        self.assertEqual(self.killed, [])

    def test_list_error_is_a_plain_failure(self) -> None:
        code, out, err = self.run_main([], listing=gu.HelperError("session/list exploded"))
        self.assertEqual(code, 1)
        self.assertIn("list: session/list exploded", out["errors"][0])
        self.assertEqual(err, "")


class MissingDirectoryReapTests(SessionFixture):
    def test_old_children_of_a_deleted_session_are_terminated(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=NOW - 2 * HOUR),
            proc(12, 10, SID, started=NOW - 5),  # too young: directory may be mid-creation
        ]
        smap = gu.classify_sessions(procs)
        killed: list[int] = []
        with patch.object(gu, "grok_session_id_from_pid", return_value=SID), patch.object(
            gu.os, "kill", side_effect=lambda pid, sig: killed.append(pid) if sig == 15 else None
        ):
            acts = gu.reap_missing_dir_orphans(smap, procs, protected_ids=set(), now=NOW, dry_run=False)
        self.assertEqual(killed, [11])
        self.assertEqual(acts[0]["sessionId"], SID)

    def test_a_reused_pid_is_not_reaped(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=NOW - 2 * HOUR)]
        smap = gu.classify_sessions(procs)
        killed: list[int] = []
        with patch.object(gu, "grok_session_id_from_pid", return_value=None), patch.object(
            gu.os, "kill", side_effect=lambda pid, sig: killed.append(pid) if sig == 15 else None
        ):
            gu.reap_missing_dir_orphans(smap, procs, protected_ids=set(), now=NOW, dry_run=False)
        self.assertEqual(killed, [])

    def test_protected_and_existing_sessions_are_never_reaped(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=NOW - 2 * HOUR),
                 proc(21, 10, SID2, started=NOW - 2 * HOUR)]
        smap = gu.classify_sessions(procs)
        killed: list[int] = []
        with patch.object(gu.os, "kill", side_effect=lambda pid, sig: killed.append(pid) if sig == 15 else None):
            gu.reap_missing_dir_orphans(smap, procs, protected_ids={SID2}, now=NOW, dry_run=False)
        self.assertEqual(killed, [])


if __name__ == "__main__":
    unittest.main()
