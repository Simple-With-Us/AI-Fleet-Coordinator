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
import shutil
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

    def test_an_attached_tui_with_nothing_loaded_is_not_a_close_candidate(self) -> None:
        # Closing it frees nothing: `live` is a skip input, never a reason to close.
        self.assertEqual(reason(row(live=True, loaded=False, updatedAt=NOW - 9 * HOUR)), "not_loaded")

    def test_a_resumed_chat_is_fresh_under_both_clocks(self) -> None:
        # Resumed 10 minutes ago after two days of silence: the summary and the
        # turns are old, but the load is not.
        stub = row(updatedAt=NOW - 48 * HOUR, loadedAt=NOW - 10 * MIN)
        self.assertEqual(last_activity_epoch(stub), NOW - 10 * MIN)
        self.assertEqual(reason(stub), "fresh")
        used = row(userTurns=4, updatedAt=NOW - 48 * HOUR, turnEndedAt=NOW - 47 * HOUR, loadedAt=NOW - 10 * MIN)
        self.assertEqual(reason(used), "fresh")

    def test_the_load_floor_ages_like_anything_else(self) -> None:
        stub = row(updatedAt=NOW - 48 * HOUR, loadedAt=NOW - 40 * MIN)
        self.assertIsNone(reason(stub))
        used = row(userTurns=4, updatedAt=NOW - 48 * HOUR, loadedAt=NOW - 3 * HOUR)
        self.assertEqual(reason(used), "fresh")
        used["loadedAt"] = NOW - 5 * HOUR
        self.assertIsNone(reason(used))

    def test_a_chat_with_an_attached_client_is_never_a_stub(self) -> None:
        attached = row(attached=True)
        self.assertFalse(is_stub_row(attached))
        self.assertEqual(reason(attached), "fresh")  # 40 minutes: under the long clock
        self.assertIsNone(reason(row(attached=True, updatedAt=NOW - 5 * HOUR)))

    def test_annotate_skips_attached_rows(self) -> None:
        rows = [{"sessionId": SID, "loaded": True, "live": False, "attached": True}]
        annotate_user_turns(rows)
        self.assertNotIn("userTurns", rows[0])

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

    def make_session(
        self,
        sid: str,
        history: list[dict],
        updated: str = "2026-10-08T18:00:00Z",
        events: list[dict] | None = None,
    ) -> Path:
        d = self.sessions / "%2FUsers%2Fjay" / sid
        d.mkdir(parents=True)
        with (d / "chat_history.jsonl").open("w", encoding="utf-8") as fh:
            for entry in history:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        (d / "summary.json").write_text(
            json.dumps({"info": {"id": sid, "cwd": "/Users/jay"}, "updated_at": updated}),
            encoding="utf-8",
        )
        (d / "events.jsonl").write_text(
            "".join(json.dumps(ev, separators=(",", ":")) + "\n" for ev in (events or [])),
            encoding="utf-8",
        )
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


E0 = 1_790_000_000.0  # a session's mcp_config_resolved
MARKS = {"configAt": E0, "initAt": E0 + 60}


def proc(pid, ppid, sid=None, leader=False, started=0.0, comm=None, client=False):
    return gu.ProcRow(
        pid=pid,
        ppid=ppid,
        comm=comm or "p%d" % pid,
        started=started,
        leader=leader,
        session_id=sid,
        grok_client=client,
    )


def classify(procs, marks=MARKS, **kw):
    return gu.classify_sessions(procs, marks_for=lambda sid: marks, **kw)


def slot(tree=(), mcp=None, task=(), outside=(), marks=None, mcp_started=0.0, nonshell=None):
    tree = list(tree)
    return {
        "tree": tree,
        "mcp": list(tree if mcp is None else mcp),
        "task": list(task),
        "nonshell": list(tree if nonshell is None else nonshell),
        "outside": list(outside),
        "mcpStarted": mcp_started,
        "marks": marks or {},
        "taskRoots": [],
    }


class ProcessTreeTests(unittest.TestCase):
    def test_tree_and_outside_are_split_by_the_leader(self) -> None:
        procs = [
            proc(1, 0),
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=E0 + 5),  # MCP server
            proc(12, 11, SID, started=E0 + 6),  # its child (npx -> node)
            proc(20, 10, SID2, started=E0 + 5),
            proc(30, 1, SID2, started=E0 + 5),  # outlived its parent: background work
            proc(40, 1, SID3, started=E0 + 5),  # not under the leader at all
            proc(50, 10),  # no session id
        ]
        smap = classify(procs)
        self.assertEqual(sorted(smap[SID]["tree"]), [11, 12])
        self.assertEqual(sorted(smap[SID]["mcp"]), [11, 12])
        self.assertEqual(smap[SID]["task"], [])
        self.assertEqual(smap[SID]["outside"], [])
        self.assertEqual((smap[SID2]["tree"], smap[SID2]["outside"]), ([20], [30]))
        self.assertEqual((smap[SID3]["tree"], smap[SID3]["outside"]), ([], [40]))

    def test_the_helper_itself_is_not_counted(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=E0 + 5), proc(99, 1, SID)]
        smap = classify(procs, self_pid=99)
        self.assertEqual((smap[SID]["tree"], smap[SID]["outside"]), ([11], []))

    def test_no_leader_means_nothing_is_loaded(self) -> None:
        smap = classify([proc(11, 1, SID, started=E0 + 5)])
        self.assertEqual(smap[SID]["tree"], [])

    def test_a_process_forked_after_the_load_window_is_work_not_mcp(self) -> None:
        late = E0 + 60 + gu.MCP_WINDOW_GRACE_SEC + 5
        procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=E0 + 5),
            proc(12, 10, SID, started=late, comm="node"),  # a watcher a tool started
        ]
        smap = classify(procs)
        self.assertEqual(smap[SID]["mcp"], [11])
        self.assertEqual(smap[SID]["task"], [12])
        # A late process never moves the load floor.
        self.assertEqual(smap[SID]["mcpStarted"], E0 + 5)

    def test_a_slow_server_inside_the_grace_period_is_still_mcp(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=E0 + 225)]
        self.assertEqual(classify(procs)[SID]["mcp"], [11])

    def test_a_shell_branch_is_work_even_inside_the_window(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(30, 10, SID, started=E0 + 5, comm="bash"),
            proc(31, 30, SID, started=E0 + 6, comm="node"),
        ]
        smap = classify(procs)
        self.assertEqual(smap[SID]["mcp"], [])
        self.assertEqual(sorted(smap[SID]["task"]), [30, 31])
        self.assertEqual(smap[SID]["taskRoots"], ["bash"])

    def test_a_shell_that_hides_its_environment_still_marks_its_children_as_work(self) -> None:
        # Apple platform binaries (/bin/sh, /bin/zsh) never show GROK_SESSION_ID.
        procs = [
            proc(10, 1, leader=True),
            proc(30, 10, None, started=E0 + 5, comm="zsh"),
            proc(31, 30, SID, started=E0 + 6, comm="node"),
        ]
        smap = classify(procs)
        self.assertEqual(smap[SID]["mcp"], [])
        self.assertEqual(smap[SID]["task"], [31])

    def test_children_of_an_mcp_server_are_mcp_even_when_they_are_shells(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=E0 + 5, comm="node"),
            proc(12, 11, SID, started=E0 + 900, comm="sh"),  # the server shelled out
        ]
        self.assertEqual(sorted(classify(procs)[SID]["mcp"]), [11, 12])

    def test_without_load_marks_nothing_is_mcp(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=E0 + 5)]
        smap = classify(procs, marks={})
        self.assertEqual(smap[SID]["mcp"], [])
        self.assertEqual(smap[SID]["task"], [11])

    def test_unknown_start_time_is_work(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=0.0)]
        self.assertEqual(classify(procs)[SID]["task"], [11])

    def test_the_floor_is_the_earliest_mcp_start(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=E0 + 40),
            proc(12, 10, SID, started=E0 + 3),
            proc(13, 10, SID, started=E0 + 200),
        ]
        self.assertEqual(classify(procs)[SID]["mcpStarted"], E0 + 3)

    def test_window_shapes(self) -> None:
        self.assertIsNone(gu.mcp_window({}))
        self.assertIsNone(gu.mcp_window({"configAt": None, "initAt": None}))
        lo, hi = gu.mcp_window({"configAt": E0, "initAt": E0 + 60})
        self.assertEqual((lo, hi), (E0 - gu.MCP_WINDOW_LEAD_SEC, E0 + 60 + gu.MCP_WINDOW_GRACE_SEC))
        # Init still pending: servers may still be starting.
        lo, hi = gu.mcp_window({"configAt": E0, "initAt": None})
        self.assertEqual(hi, E0 + gu.MCP_WINDOW_NO_INIT_SEC + gu.MCP_WINDOW_GRACE_SEC)
        # An init from an earlier load than the newest config: init is not done yet.
        lo, hi = gu.mcp_window({"configAt": E0, "initAt": E0 - 5000})
        self.assertEqual(hi, E0 + gu.MCP_WINDOW_NO_INIT_SEC + gu.MCP_WINDOW_GRACE_SEC)


class LeaderPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = Path(self._tmp.name) / "leader.lock"
        patcher = patch.object(gu, "LEADER_LOCK", self.lock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lock_pid_picks_the_leader_among_lookalikes(self) -> None:
        self.lock.write_text("10")
        procs = [proc(10, 1, leader=True), proc(20, 1, leader=True)]
        self.assertEqual(gu.pinned_leaders(procs), [10])
        self.assertFalse(gu.single_leader(procs))

    def test_a_single_leader_that_matches_the_lock(self) -> None:
        self.lock.write_text("10\n")
        self.assertTrue(gu.single_leader([proc(10, 1, leader=True)]))

    def test_a_lock_naming_another_pid_is_not_a_single_leader(self) -> None:
        self.lock.write_text("77")
        procs = [proc(10, 1, leader=True)]
        self.assertFalse(gu.single_leader(procs))
        self.assertEqual(gu.pinned_leaders(procs), [10])  # the lock names no live leader: fall back

    def test_missing_or_junk_lock_falls_back_to_the_scan(self) -> None:
        self.assertTrue(gu.single_leader([proc(10, 1, leader=True)]))
        self.lock.write_text("not-a-pid")
        self.assertTrue(gu.single_leader([proc(10, 1, leader=True)]))
        self.assertFalse(gu.single_leader([]))


class AttachedClientTests(unittest.TestCase):
    def test_attached_truth_table(self) -> None:
        self.assertFalse(gu.is_attached("/a", set()))
        self.assertTrue(gu.is_attached("/a", {os.path.realpath("/a")}))
        self.assertFalse(gu.is_attached("/a", {os.path.realpath("/b")}))
        self.assertTrue(gu.is_attached("/a", None))  # cannot tell: assume attached
        self.assertTrue(gu.is_attached(None, {"/b"}))  # nothing to compare, a client exists
        self.assertFalse(gu.is_attached(None, set()))

    def cwds(self, procs, mapping, **kw):
        with patch.object(gu, "proc_cwd", side_effect=lambda pid: mapping.get(pid)), patch.object(
            gu, "cwd_probe_works", return_value=True
        ):
            return gu.client_cwds(procs, own_pid=99, leaders=[10], **kw)

    def test_clients_are_non_leader_grok_processes_outside_the_leader_and_this_job(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(11, 10, client=True),  # a grok child of the leader: a tool, not a client
            proc(99, 1),  # this job
            proc(98, 99, client=True),  # this job's own helper client
            proc(60, 1, client=True),  # Shellular / BotFleet / a TUI
            proc(61, 1),  # not a grok process
        ]
        got = self.cwds(procs, {11: "/tool", 98: "/mine", 60: "/Users/jay/work", 61: "/other"})
        self.assertEqual(got, {os.path.realpath("/Users/jay/work")})

    def test_no_clients_is_an_empty_set(self) -> None:
        self.assertEqual(self.cwds([proc(10, 1, leader=True)], {}), set())

    def test_an_unreadable_client_cwd_means_unknown(self) -> None:
        self.assertIsNone(self.cwds([proc(60, 1, client=True)], {}))

    def test_a_broken_probe_means_unknown(self) -> None:
        with patch.object(gu, "proc_cwd", return_value="/x"), patch.object(gu, "cwd_probe_works", return_value=False):
            self.assertIsNone(gu.client_cwds([proc(60, 1, client=True)], own_pid=99, leaders=[10]))

    @unittest.skipUnless(sys.platform == "darwin", "libproc is macOS only")
    def test_the_real_probe_reads_this_processes_cwd(self) -> None:
        self.assertTrue(gu.cwd_probe_works())
        self.assertEqual(os.path.realpath(gu.proc_cwd(os.getpid())), os.path.realpath(os.getcwd()))


class BuildRowsTests(SessionFixture):
    def test_loaded_session_beyond_the_first_list_page_is_added(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER], updated="2026-10-08T18:00:00Z")
        smap = {SID: slot([11, 12])}
        rows = gu.build_rows([], smap, set())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertTrue(r["loaded"])
        self.assertEqual(r["procs"], 2)
        self.assertEqual(r["userTurns"], 0)
        self.assertFalse(r["attached"])
        self.assertTrue(is_stub_row(r))

    def test_unloaded_listed_sessions_stay_unloaded(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        listed = [{"sessionId": SID, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"}]
        rows = gu.build_rows(listed, {}, set())
        self.assertFalse(rows[0]["loaded"])
        self.assertEqual(unload_skip_reason(rows[0], now=NOW, self_id="me"), "not_loaded")

    def test_background_processes_are_recorded(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER, REAL_USER])
        smap = {SID: slot([11], outside=[30, 31])}
        rows = gu.build_rows([], smap, set())
        self.assertEqual(rows[0]["backgroundPids"], 2)

    def test_work_inside_the_leader_tree_counts_as_background(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER, REAL_USER])
        smap = {SID: slot([11, 12], mcp=[11], task=[12])}
        row = gu.build_rows([], smap, set())[0]
        self.assertEqual((row["procs"], row["backgroundPids"]), (1, 1))
        self.assertEqual(unload_skip_reason(row, now=NOW + 99 * HOUR, self_id="me"), "background_process")

    def test_missing_directory_is_not_a_row(self) -> None:
        rows = gu.build_rows([], {SID: slot([11])}, set())
        self.assertEqual(rows, [])

    def test_the_load_floor_comes_from_events_and_process_starts(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER], updated="2026-10-06T18:00:00Z")
        smap = {SID: slot([11], marks={"configAt": E0, "initAt": E0 + 60}, mcp_started=E0 + 90)}
        row = gu.build_rows([], smap, set())[0]
        self.assertEqual(row["loadedAt"], E0 + 90)
        smap = {SID: slot([11], marks={"configAt": E0, "initAt": E0 + 100}, mcp_started=E0 + 5)}
        self.assertEqual(gu.build_rows([], smap, set())[0]["loadedAt"], E0 + 100)

    def test_an_attached_client_in_the_chats_directory_removes_the_stub_clock(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        listed = [{"sessionId": SID, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"}]
        now = session_disk._parse_ts("2026-10-08T18:40:00Z")
        free = gu.build_rows(listed, {SID: slot([11])}, set())[0]
        self.assertIsNone(unload_skip_reason(free, now=now, self_id="me", stub_idle_sec=DEFAULT_STUB_IDLE_SEC))
        held = gu.build_rows(listed, {SID: slot([11])}, {os.path.realpath("/Users/jay")})[0]
        self.assertTrue(held["attached"])
        self.assertEqual(unload_skip_reason(held, now=now, self_id="me", stub_idle_sec=DEFAULT_STUB_IDLE_SEC), "fresh")
        unknown = gu.build_rows(listed, {SID: slot([11])}, None)[0]
        self.assertEqual(unload_skip_reason(unknown, now=now, self_id="me", stub_idle_sec=DEFAULT_STUB_IDLE_SEC), "fresh")


class EventScanTests(SessionFixture):
    def events_path(self, sid=SID):
        return next(self.sessions.glob("*/" + sid)) / "events.jsonl"

    def test_a_long_turn_stays_working_when_turn_started_is_beyond_the_tail(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        lines = [{"ts": "2026-10-08T18:00:00Z", "type": "turn_started"}]
        lines += [{"ts": "2026-10-08T18:00:%02dZ" % (i % 60), "type": "phase_changed", "phase": "tool_execution"} for i in range(1500)]
        self.events_path().write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        with patch.object(session_disk.time, "time", return_value=session_disk._parse_ts("2026-10-08T19:30:00Z")):
            state = session_disk.turn_state(SID)
        self.assertEqual(state["turnState"], "working")  # phase event is 90 minutes old
        self.assertEqual(state["turnStartedAt"], session_disk._parse_ts("2026-10-08T18:00:00Z"))

    def test_newest_turn_times_across_chunk_boundaries(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        lines = [{"ts": "2026-10-08T10:00:00Z", "type": "turn_started"},
                 {"ts": "2026-10-08T10:05:00Z", "type": "turn_ended"}]
        lines += [{"ts": "2026-10-08T10:06:00Z", "type": "phase_changed", "phase": "x" * 300} for _ in range(2000)]
        lines += [{"ts": "2026-10-08T11:00:00Z", "type": "turn_started", "note": "turn_ended"}]
        self.events_path().write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        got = session_disk._newest_event_times(self.events_path(), ("turn_started", "turn_ended"), chunk=4096)
        self.assertEqual(got["turn_started"], session_disk._parse_ts("2026-10-08T11:00:00Z"))
        self.assertEqual(got["turn_ended"], session_disk._parse_ts("2026-10-08T10:05:00Z"))

    def test_a_missing_type_is_absent_and_a_missing_file_is_empty(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER], events=[{"ts": "2026-10-08T10:00:00Z", "type": "turn_started"}])
        got = session_disk._newest_event_times(self.events_path(), ("turn_started", "turn_ended"))
        self.assertEqual(list(got), ["turn_started"])
        self.assertEqual(session_disk._newest_event_times(self.events_path().with_name("nope"), ("turn_started",)), {})

    def test_a_torn_last_line_is_ignored(self) -> None:
        self.make_session(SID, [SYSTEM, REMINDER])
        self.events_path().write_text(
            json.dumps({"ts": "2026-10-08T10:00:00Z", "type": "turn_started"}) + "\n" + '{"ts":"2026-10-08T11:0',
            encoding="utf-8",
        )
        got = session_disk._newest_event_times(self.events_path(), ("turn_started",))
        self.assertEqual(got["turn_started"], session_disk._parse_ts("2026-10-08T10:00:00Z"))

    def test_load_marks_use_config_and_init_only(self) -> None:
        events = [
            {"ts": "2026-10-07T09:00:00Z", "type": "mcp_config_resolved"},
            {"ts": "2026-10-07T09:01:00Z", "type": "mcp_init_completed"},
            {"ts": "2026-10-08T09:00:00Z", "type": "mcp_config_resolved"},
            {"ts": "2026-10-08T09:01:30Z", "type": "mcp_init_completed"},
            {"ts": "2026-10-08T20:00:00Z", "type": "mcp_server_starting", "server_name": "flaky"},  # crash loop
        ]
        self.make_session(SID, [SYSTEM, REMINDER], events=events)
        marks = session_disk.mcp_load_marks(SID)
        self.assertEqual(marks["configAt"], session_disk._parse_ts("2026-10-08T09:00:00Z"))
        self.assertEqual(marks["initAt"], session_disk._parse_ts("2026-10-08T09:01:30Z"))
        self.assertEqual(session_disk.mcp_load_marks("no-such-session"), {"configAt": None, "initAt": None})


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

    LOAD = session_disk._parse_ts("2026-10-08T18:00:00Z")

    def setUp(self) -> None:
        super().setUp()
        load_events = [
            {"ts": "2026-10-08T18:00:00Z", "type": "mcp_config_resolved"},
            {"ts": "2026-10-08T18:01:00Z", "type": "mcp_init_completed"},
        ]
        self.make_session(SID, [SYSTEM, REMINDER], updated="2026-10-08T18:00:00Z", events=load_events)  # stub
        self.make_session(SID2, [SYSTEM, REMINDER, REAL_USER], updated="2026-10-08T18:00:00Z", events=load_events)
        self.procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=self.LOAD + 5),
            proc(21, 10, SID2, started=self.LOAD + 5),
        ]
        self.listed = [
            {"sessionId": SID, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"},
            {"sessionId": SID2, "cwd": "/Users/jay", "updatedAt": "2026-10-08T18:00:00Z"},
        ]
        # Pretend it is 18:35 UTC, 35 minutes after the last update.
        self.now = session_disk._parse_ts("2026-10-08T18:35:00Z")
        self.closed: list[str] = []
        self.killed: list[int] = []
        self.list_calls = 0
        self.scans = 0
        self.on_rescan = None
        self.cwds: dict[int, str] = {}
        self.lock_pid = 10
        self.lock = self.home / "leader.lock"
        # What the live process table says right before a SIGTERM.
        self.pid_sid: dict[int, str] = {11: SID, 21: SID2}
        env = patch.dict(os.environ, {"GROK_SESSION_ID": "me"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def scan(self):
        self.scans += 1
        if self.scans > 1 and self.on_rescan is not None:
            self.on_rescan()
        return list(self.procs)

    def run_main(self, argv, *, close=None, listing=None, now=None):
        def fake_close(sid, cwd, timeout):
            self.closed.append(sid)
            if close is not None:
                return close(sid)
            return {"ok": True, "sessionId": sid, "diskKept": True, "closeOutcome": "closed"}

        def fake_list(timeout):
            self.list_calls += 1
            if isinstance(listing, Exception):
                raise listing
            return self.listed

        self.lock.write_text(str(self.lock_pid))
        patches = [
            patch.object(gu, "scan_processes", side_effect=self.scan),
            patch.object(gu, "list_leader_sessions", side_effect=fake_list),
            patch.object(gu, "close_session", side_effect=fake_close),
            patch.object(gu, "grok_session_id_from_pid", side_effect=lambda pid: self.pid_sid.get(pid)),
            patch.object(gu, "proc_cwd", side_effect=lambda pid: self.cwds.get(pid)),
            patch.object(gu, "cwd_probe_works", return_value=True),
            patch.object(gu, "LEADER_LOCK", self.lock),
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

    def events(self, sid):
        return next(self.sessions.glob("*/" + sid)) / "events.jsonl"

    def append_event(self, sid, event):
        with self.events(sid).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def test_the_leader_list_is_not_called_unless_asked(self) -> None:
        code, out, _ = self.run_main([], listing=gu.HelperTimeout("list", 180.0))
        self.assertEqual(self.list_calls, 0)
        self.assertEqual(code, 0)  # a dead-slow list cannot fail the run
        self.assertEqual(self.closed, [SID])  # candidates come from the process table and disk
        self.assertEqual(out["listed"], 0)
        with patch.dict(os.environ, {"GROK_IDLE_UNLOAD_USE_LIST": "1"}):
            self.run_main([])
        self.assertEqual(self.list_calls, 1)

    def test_a_list_that_succeeds_adds_rows_but_not_candidates(self) -> None:
        code, out, _ = self.run_main(["--use-list"])
        self.assertEqual(self.list_calls, 1)
        self.assertEqual(out["listed"], 2)
        self.assertEqual(self.closed, [SID])

    def test_only_the_stub_is_closed_inside_four_hours(self) -> None:
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(self.closed, [SID])
        self.assertEqual(out["closed"][0]["kind"], "stub")
        self.assertEqual(out["closed"][0]["closeOutcome"], "closed")
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
        # 64 minutes idle: under the 90 minute stub clock, over the 1 hour long clock.
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

    def test_a_shell_child_inside_the_leader_protects_the_chat_and_is_never_signalled(self) -> None:
        # A watcher started by a tool: bash under the leader, node under bash.
        self.procs.append(proc(30, 10, None, started=self.LOAD + 600, comm="bash"))
        self.procs.append(proc(31, 30, SID2, started=self.LOAD + 601, comm="node"))
        self.pid_sid[31] = SID2
        code, out, _ = self.run_main(["--verbose"], now=self.now + 9 * HOUR)
        self.assertNotIn(SID2, self.closed)
        skipped = {s["sessionId"]: s for s in out["skipped"]}
        self.assertEqual(skipped[SID2]["reason"], "background_process")
        self.assertEqual(skipped[SID2]["taskRoots"], ["bash"])
        self.assertNotIn(31, self.killed)

    def test_busy_chat_is_not_closed(self) -> None:
        self.append_event(SID2, {"type": "turn_started", "ts": "2026-10-08T18:30:00Z"})
        code, out, _ = self.run_main([], now=self.now + 2 * MIN)
        self.assertNotIn(SID2, self.closed)
        self.assertEqual(out["skippedByReason"].get("busy"), 1)

    def test_a_chat_resumed_after_a_day_is_not_closed_before_its_first_turn(self) -> None:
        # Summary and turns are two days old, but the chat (re)loaded 10 minutes ago.
        two_days_ago = "2026-10-06T18:00:00Z"
        for sid in (SID, SID2):
            d = next(self.sessions.glob("*/" + sid))
            (d / "summary.json").write_text(
                json.dumps({"info": {"id": sid, "cwd": "/Users/jay"}, "updated_at": two_days_ago}), encoding="utf-8"
            )
            self.events(sid).write_text(
                json.dumps({"ts": "2026-10-06T18:10:00Z", "type": "turn_started"}) + "\n"
                + json.dumps({"ts": "2026-10-06T18:20:00Z", "type": "turn_ended"}) + "\n"
                + json.dumps({"ts": "2026-10-08T18:25:00Z", "type": "mcp_config_resolved"}) + "\n"
                + json.dumps({"ts": "2026-10-08T18:26:00Z", "type": "mcp_init_completed"}) + "\n",
                encoding="utf-8",
            )
        self.listed = [dict(row, updatedAt=two_days_ago) for row in self.listed]
        resumed = session_disk._parse_ts("2026-10-08T18:25:05Z")
        self.procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=resumed), proc(21, 10, SID2, started=resumed)]
        code, out, _ = self.run_main([])  # now = 18:35
        self.assertEqual(self.closed, [])
        self.assertEqual(out["skippedByReason"], {"fresh": 2})

    def test_a_chat_that_turns_busy_after_selection_is_neither_closed_nor_signalled(self) -> None:
        def turn_begins():
            self.append_event(SID, {"type": "turn_started", "ts": "2026-10-08T18:34:30Z"})

        self.on_rescan = turn_begins
        code, out, _ = self.run_main([], close=lambda sid: {"ok": False, "error": "boom"})
        self.assertEqual(self.closed, [])
        self.assertEqual(self.killed, [])
        rec = out["closed"][0]
        self.assertEqual(rec["action"], "skipped")
        self.assertEqual(rec["reason"], "changed_since_selection:busy")
        self.assertEqual(code, 0)

    def test_a_chat_resumed_after_selection_is_not_closed(self) -> None:
        def resumed():
            # The leader reloads the chat: a new MCP set under a fresh load event.
            self.append_event(SID, {"type": "mcp_config_resolved", "ts": "2026-10-08T18:34:50Z"})
            self.append_event(SID, {"type": "mcp_init_completed", "ts": "2026-10-08T18:34:55Z"})
            self.procs[:] = [p for p in self.procs if p.pid != 11]
            self.procs.append(proc(12, 10, SID, started=session_disk._parse_ts("2026-10-08T18:34:52Z")))

        self.on_rescan = resumed
        code, out, _ = self.run_main([])
        self.assertEqual(self.closed, [])
        self.assertEqual(out["closed"][0]["reason"], "changed_since_selection:fresh")

    def test_a_client_that_connects_after_selection_keeps_the_stub(self) -> None:
        def client_arrives():
            self.procs.append(proc(60, 1, comm="grok", client=True))
            self.cwds[60] = "/Users/jay"

        self.on_rescan = client_arrives
        code, out, _ = self.run_main([])
        self.assertEqual(self.closed, [])
        self.assertEqual(out["closed"][0]["reason"], "changed_since_selection:fresh")

    def test_a_connected_client_in_the_chats_directory_keeps_the_stub(self) -> None:
        self.procs.append(proc(60, 1, comm="grok", client=True))  # BotFleet / Shellular / a TUI
        self.cwds[60] = "/Users/jay"
        code, out, _ = self.run_main([])
        self.assertEqual(self.closed, [])
        self.assertEqual(out["skippedByReason"], {"fresh": 2})
        self.assertEqual(out["clientCwds"], 1)
        # ...and it still unloads on the long clock.
        self.closed.clear()
        code, out, _ = self.run_main([], now=self.now + 4 * HOUR)
        self.assertEqual(sorted(self.closed), sorted([SID, SID2]))

    def test_a_client_in_another_directory_does_not_hold_the_stub(self) -> None:
        self.procs.append(proc(60, 1, comm="grok", client=True))
        self.cwds[60] = "/Users/jay/elsewhere"
        self.run_main([])
        self.assertEqual(self.closed, [SID])

    def test_an_unreadable_client_cwd_holds_every_stub(self) -> None:
        self.procs.append(proc(60, 1, comm="grok", client=True))  # no cwd recorded: unreadable
        code, out, _ = self.run_main([])
        self.assertEqual(self.closed, [])
        self.assertIsNone(out["clientCwds"])

    def test_list_timeout_still_closes_loaded_stubs_then_exits_75(self) -> None:
        code, out, err = self.run_main(["--use-list"], listing=gu.HelperTimeout("list", 180.0))
        self.assertEqual(code, gu.EX_TEMPFAIL)
        self.assertEqual(self.closed, [SID])  # discovered from the process table + disk
        line = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(line["error"], "helper timed out")
        self.assertEqual(line["call"], "list")
        self.assertEqual(line["timeoutSec"], 180.0)
        self.assertRegex(line["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(out["timeouts"][0]["call"], "list")

    def test_close_timeout_stops_further_closes_and_never_signals(self) -> None:
        def slow(sid):
            raise gu.HelperTimeout("close", 180.0)

        code, out, err = self.run_main([], close=slow, now=self.now + 5 * HOUR)
        self.assertEqual(code, gu.EX_TEMPFAIL)
        self.assertEqual(len(self.closed), 1)  # the second close was deferred
        actions = sorted(c["action"] for c in out["closed"])
        self.assertEqual(actions, ["deferred", "session/close"])
        # The leader may still be working on that close: leave its processes alone.
        self.assertEqual(self.killed, [])
        self.assertEqual(out["orphans"], [])

    def test_a_failed_close_is_a_failure_and_never_signals(self) -> None:
        code, out, _ = self.run_main([], close=lambda sid: {"ok": False, "error": "refused: turn running"})
        self.assertEqual(code, 1)
        self.assertEqual(self.killed, [])
        self.assertEqual(out["orphans"], [])

    def test_not_resident_with_leaked_mcp_children_terminates_them(self) -> None:
        def not_resident(sid):
            return {"ok": True, "closeOutcome": "notResident", "diskKept": True}

        code, out, _ = self.run_main([], close=not_resident)
        self.assertEqual(code, 0)
        self.assertEqual(self.killed, [11])  # only the candidate (the stub), not SID2
        self.assertTrue(out["orphans"][0]["fallbackAfterNotResident"])
        self.assertEqual(out["orphans"][0]["pids"], 1)

    def test_not_resident_fallback_rechecks_and_spares_a_chat_that_turned_busy(self) -> None:
        def not_resident(sid):
            # The close answered; by the time the fallback looks again a turn began.
            self.on_rescan = lambda: self.append_event(SID, {"type": "turn_started", "ts": "2026-10-08T18:34:50Z"})
            return {"ok": True, "closeOutcome": "notResident", "diskKept": True}

        self.scans = 0
        code, out, _ = self.run_main([], close=not_resident)
        self.assertEqual(self.killed, [])
        self.assertEqual(out["orphans"][0]["action"], "skipped")

    def test_a_pid_reused_during_a_slow_close_is_not_signalled(self) -> None:
        def reuse(sid):
            # By the time the close returns, pid 11 belongs to someone else.
            self.pid_sid[11] = "01aaaaaa-0000-7000-8000-000000000000"
            return {"ok": True, "closeOutcome": "notResident", "diskKept": True}

        code, out, _ = self.run_main([], close=reuse)
        self.assertEqual(self.killed, [])
        self.assertTrue(out["orphans"][0]["fallbackAfterNotResident"])

    def test_a_child_that_already_exited_is_not_signalled(self) -> None:
        def exits(sid):
            self.pid_sid.pop(11, None)  # the session's MCP child is gone
            return {"ok": True, "closeOutcome": "notResident", "diskKept": True}

        self.run_main([], close=exits)
        self.assertEqual(self.killed, [])

    def test_no_reap_orphans_leaves_processes_alone(self) -> None:
        def not_resident(sid):
            return {"ok": True, "closeOutcome": "notResident", "diskKept": True}

        self.run_main(["--no-reap-orphans"], close=not_resident)
        self.assertEqual(self.killed, [])

    def test_list_error_is_a_plain_failure(self) -> None:
        code, out, err = self.run_main(["--use-list"], listing=gu.HelperError("session/list exploded"))
        self.assertEqual(code, 1)
        self.assertIn("list: session/list exploded", out["errors"][0])
        self.assertEqual(err, "")

    def test_an_odd_close_outcome_is_not_recorded(self) -> None:
        code, out, _ = self.run_main([], close=lambda sid: {"ok": True, "closeOutcome": "x" * 80 + " secret"})
        self.assertNotIn("closeOutcome", out["closed"][0])

    # -- reaping sessions whose directory is gone: every guard

    def orphan_setup(self):
        gone = "01a0aaaa-0000-7000-8000-00000000000a"
        self.procs.append(proc(31, 10, gone, started=self.LOAD - 3 * HOUR))
        self.pid_sid[31] = gone
        return gone

    def test_children_of_a_deleted_session_are_reaped_when_everything_is_healthy(self) -> None:
        gone = self.orphan_setup()
        code, out, _ = self.run_main([])
        self.assertIn(31, self.killed)
        self.assertEqual([o["sessionId"] for o in out["orphans"]], [gone])
        self.assertNotIn("orphansSkipped", out)

    def test_no_reaping_with_two_leaders(self) -> None:
        self.orphan_setup()
        self.procs.append(proc(12, 1, leader=True))  # a second leader (another HOME)
        code, out, _ = self.run_main([])
        self.assertNotIn(31, self.killed)
        self.assertEqual(out["orphansSkipped"], "not exactly one leader process")

    def test_no_reaping_when_the_lock_names_another_process(self) -> None:
        self.orphan_setup()
        self.lock_pid = 4242
        code, out, _ = self.run_main([])
        self.assertNotIn(31, self.killed)
        self.assertEqual(out["orphansSkipped"], "not exactly one leader process")

    def test_no_reaping_when_the_leader_list_failed(self) -> None:
        self.orphan_setup()
        code, out, _ = self.run_main(["--use-list"], listing=gu.HelperError("boom"))
        self.assertNotIn(31, self.killed)
        self.assertEqual(out["orphansSkipped"], "leader list failed")

    def test_no_reaping_when_the_sessions_directory_is_empty_or_gone(self) -> None:
        self.orphan_setup()
        shutil.rmtree(self.sessions)
        code, out, _ = self.run_main([])
        self.assertEqual(self.killed, [])
        self.assertEqual(out["orphansSkipped"], "sessions directory unreadable or empty")
        self.sessions.mkdir()
        code, out, _ = self.run_main([])
        self.assertEqual(self.killed, [])
        self.assertEqual(out["orphansSkipped"], "sessions directory unreadable or empty")


class MissingDirectoryReapTests(SessionFixture):
    def test_old_children_of_a_deleted_session_are_terminated(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(11, 10, SID, started=NOW - 2 * HOUR),
            proc(12, 10, SID, started=NOW - 5),  # too young: directory may be mid-creation
        ]
        smap = classify(procs, marks={})
        killed: list[int] = []
        with patch.object(gu, "grok_session_id_from_pid", return_value=SID), patch.object(
            gu.os, "kill", side_effect=lambda pid, sig: killed.append(pid) if sig == 15 else None
        ):
            acts = gu.reap_missing_dir_orphans(smap, procs, protected_ids=set(), now=NOW, dry_run=False)
        self.assertEqual(killed, [11])
        self.assertEqual(acts[0]["sessionId"], SID)

    def test_a_reused_pid_is_not_reaped(self) -> None:
        procs = [proc(10, 1, leader=True), proc(11, 10, SID, started=NOW - 2 * HOUR)]
        smap = classify(procs, marks={})
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
        smap = classify(procs, marks={})
        killed: list[int] = []
        with patch.object(gu.os, "kill", side_effect=lambda pid, sig: killed.append(pid) if sig == 15 else None):
            gu.reap_missing_dir_orphans(smap, procs, protected_ids={SID2}, now=NOW, dry_run=False)
        self.assertEqual(killed, [])

    def test_a_shell_branch_is_left_alone(self) -> None:
        procs = [
            proc(10, 1, leader=True),
            proc(30, 10, None, started=NOW - 2 * HOUR, comm="zsh"),
            proc(31, 30, SID, started=NOW - 2 * HOUR, comm="node"),
        ]
        smap = classify(procs, marks={})
        killed: list[int] = []
        with patch.object(gu, "grok_session_id_from_pid", return_value=SID), patch.object(
            gu.os, "kill", side_effect=lambda pid, sig: killed.append(pid) if sig == 15 else None
        ):
            acts = gu.reap_missing_dir_orphans(smap, procs, protected_ids=set(), now=NOW, dry_run=False)
        self.assertEqual(killed, [])
        self.assertEqual(acts, [])

    def test_at_most_five_sessions_are_reaped_per_run(self) -> None:
        procs = [proc(10, 1, leader=True)]
        for i in range(8):
            procs.append(proc(100 + i, 10, "01a0aaaa-0000-7000-8000-0000000000%02d" % i, started=NOW - 2 * HOUR))
        smap = classify(procs, marks={})
        with patch.object(gu, "grok_session_id_from_pid", side_effect=lambda pid: "01a0aaaa-0000-7000-8000-0000000000%02d" % (pid - 100)), patch.object(
            gu.os, "kill"
        ):
            acts = gu.reap_missing_dir_orphans(smap, procs, protected_ids=set(), now=NOW, dry_run=False)
        self.assertEqual(len(acts), gu.MAX_REAP_PER_RUN)


if __name__ == "__main__":
    unittest.main()
