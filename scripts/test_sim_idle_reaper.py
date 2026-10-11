#!/usr/bin/env python3
"""Tests for scripts/sim-idle-reaper.py and scripts/hooks/sim-session-hook.py.

Self-contained:  a temporary CoreSimulator Devices directory of fake device.plist files and a
temporary state directory.  No xcrun, simctl or pgrep is ever run (every call is patched), so no
simulator is booted or shut down.  Runs on macOS and Linux.

  python3 scripts/test_sim_idle_reaper.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
ROOT = tempfile.mkdtemp(prefix="sim-reaper-test-")
os.environ["SIM_REAPER_STATE_DIR"] = os.path.join(ROOT, "state")
os.environ["SIM_REAPER_DEVICES_DIR"] = os.path.join(ROOT, "Devices")
os.environ["SIM_REAPER_HOOK_LOG"] = os.path.join(ROOT, "hook.log")
os.environ["SIM_REAPER_XCRUN"] = "/nonexistent/xcrun"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = load("sim_session_hook", REPO / "scripts" / "hooks" / "sim-session-hook.py")
reaper = load("sim_idle_reaper", REPO / "scripts" / "sim-idle-reaper.py")

A = "AAAAAAAA-0000-4000-8000-000000000001"
B = "BBBBBBBB-0000-4000-8000-000000000002"
SID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-2222-3333-4444-555555555555"


def set_state(udid: str, state: int, name: str = "iPhone 17") -> None:
    d = os.path.join(ROOT, "Devices", udid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "device.plist"), "wb") as fh:
        plistlib.dump({"UDID": udid, "name": name, "state": state, "isDeleted": False}, fh)


def bash(event: str, command: str, tuid: str = "toolu_1", sid: str = SID, agent: str | None = None) -> dict:
    p = {"hook_event_name": event, "session_id": sid, "tool_name": "Bash", "tool_use_id": tuid,
         "tool_input": {"command": command}}
    if agent:
        p["agent_id"] = agent
    return p


def session_doc(sid: str = SID) -> dict:
    with open(hook.session_path(sid)) as fh:
        return json.load(fh)


class Base(unittest.TestCase):
    def setUp(self):
        for sub in ("state", "Devices"):
            shutil.rmtree(os.path.join(ROOT, sub), ignore_errors=True)
            os.makedirs(os.path.join(ROOT, sub))
        set_state(A, 1)
        set_state(B, 1, "iPad")


class HookAttribution(Base):
    def test_plist_states(self):
        set_state(A, 3)
        set_state(B, 2)
        self.assertEqual(set(hook.booted_devices()), {A, B})
        set_state(B, 4)
        self.assertEqual(set(hook.booted_devices()), {A})

    def test_boot_in_bash_is_owned_by_main(self):
        hook.on_pre(bash("PreToolUse", f"xcrun simctl boot {A}"))
        set_state(A, 3)
        hook.on_post(bash("PostToolUse", f"xcrun simctl boot {A}"))
        doc = session_doc()
        self.assertEqual(doc["udids"][A]["agent"], "main")
        self.assertFalse(os.listdir(os.path.join(ROOT, "state", "pre")))

    def test_failed_xcodebuild_still_attributes(self):
        cmd = "xcodebuild test -scheme X -destination 'platform=iOS Simulator,name=iPhone 17'"
        hook.on_pre(bash("PreToolUse", cmd))
        set_state(A, 3)
        hook.on_post(bash("PostToolUseFailure", cmd))
        self.assertIn(A, session_doc()["udids"])

    def test_subagent_boot_carries_agent_id(self):
        hook.on_pre(bash("PreToolUse", f"xcrun simctl boot {A}", agent="agent-7"))
        set_state(A, 3)
        hook.on_post(bash("PostToolUse", f"xcrun simctl boot {A}", agent="agent-7"))
        self.assertEqual(session_doc()["udids"][A]["agent"], "agent-7")

    def test_unrelated_and_readonly_commands_take_no_snapshot(self):
        for cmd in ("ls -la", "xcrun simctl list devices", "git status", "xcrun simctl io booted screenshot x.png"):
            hook.on_pre(bash("PreToolUse", cmd))
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "state", "pre")))

    def test_post_without_snapshot_attributes_nothing(self):
        set_state(A, 3)
        hook.on_post(bash("PostToolUse", f"xcrun simctl boot {A}"))
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def test_already_booted_device_is_not_claimed(self):
        set_state(A, 3)
        hook.on_pre(bash("PreToolUse", "xcodebuild test -destination id=" + A))
        hook.on_post(bash("PostToolUse", "xcodebuild test -destination id=" + A))
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def test_first_claim_wins(self):
        hook.on_pre(bash("PreToolUse", "xcodebuild test", tuid="t-mine"))
        hook.on_pre(bash("PreToolUse", f"xcrun simctl boot {A}", tuid="t-other", sid=OTHER))
        set_state(A, 3)
        hook.on_post(bash("PostToolUse", f"xcrun simctl boot {A}", tuid="t-other", sid=OTHER))
        hook.on_post(bash("PostToolUse", "xcodebuild test", tuid="t-mine"))
        self.assertIn(A, session_doc(OTHER)["udids"])
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def test_stale_claim_from_an_earlier_boot_is_replaced(self):
        hook.write_json(hook.session_path(OTHER), {"udids": {A: {"agent": "main", "t": time.time() - 7200}}})
        hook.on_pre(bash("PreToolUse", f"xcrun simctl boot {A}"))
        set_state(A, 3)
        hook.on_post(bash("PostToolUse", f"xcrun simctl boot {A}"))
        self.assertIn(A, session_doc()["udids"])

    def test_mcp_attach_marks_watched(self):
        set_state(A, 3)
        p = {"hook_event_name": "PreToolUse", "session_id": SID, "tool_use_id": "m1",
             "tool_name": "mcp__Claude_Code_iOS_Simulator__control", "tool_input": {"action": "attach"}}
        hook.on_pre(p)
        hook.on_post(dict(p, hook_event_name="PostToolUse"))
        self.assertIn(A, session_doc()["watched"])


class HookStop(Base):
    def own(self, udid: str, agent: str = "main", sid: str = SID, watched: bool = False):
        path = hook.session_path(sid)
        doc = hook.load_json(path, {})
        doc.setdefault("udids", {})[udid] = {"agent": agent, "t": time.time(), "name": "x"}
        if watched:
            doc.setdefault("watched", {})[udid] = time.time()
        hook.write_json(path, doc)
        set_state(udid, 3)

    def stop(self, event: str, agent: str | None = None) -> list:
        calls = []
        p = {"hook_event_name": event, "session_id": SID}
        if agent:
            p["agent_id"] = agent
        with patch.object(hook, "spawn_worker", lambda *a: calls.append(a)):
            hook.on_stop(p, event)
        return calls

    def test_stop_spawns_for_main_only(self):
        self.own(A, "agent-7")
        self.assertEqual(self.stop("Stop"), [])
        self.own(B, "main")
        self.assertEqual(self.stop("Stop"), [("Stop", SID, "main")])

    def test_subagent_stop_targets_that_agent(self):
        self.own(A, "agent-7")
        self.assertEqual(self.stop("SubagentStop", "agent-8"), [])
        self.assertEqual(self.stop("SubagentStop", "agent-7"), [("SubagentStop", SID, "agent-7")])

    def test_watched_session_is_left_to_session_end(self):
        self.own(A, "main", watched=True)
        self.assertEqual(self.stop("Stop"), [])
        self.assertEqual(self.stop("SessionEnd"), [("SessionEnd", SID, "*")])

    def test_no_session_file_is_a_no_op(self):
        self.assertEqual(self.stop("Stop"), [])
        self.assertEqual(self.stop("SessionEnd"), [])

    def test_worker_shuts_down_main_and_keeps_subagent(self):
        self.own(A, "main")
        self.own(B, "agent-7")
        shut = []
        with patch.object(hook, "running", lambda *n: []), \
                patch.object(hook, "shutdown", lambda u: shut.append(u) or 0):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [A])
        self.assertEqual(set(session_doc()["udids"]), {B})

    def test_worker_defers_while_xcodebuild_runs(self):
        self.own(A, "main")
        shut = []
        with patch.object(hook, "running", lambda *n: ["xcodebuild"]), \
                patch.object(hook, "shutdown", lambda u: shut.append(u) or 0):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [])
        self.assertIn(A, session_doc()["udids"])

    def test_session_end_shuts_everything_and_deletes_file(self):
        self.own(A, "main", watched=True)
        self.own(B, "agent-7")
        shut = []
        with patch.object(hook, "running", lambda *n: ["xcodebuild"]), \
                patch.object(hook, "shutdown", lambda u: shut.append(u) or 0):
            hook.run_worker("SessionEnd", SID, "*")
        self.assertEqual(sorted(shut), sorted([A, B]))
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def test_worker_skips_device_reclaimed_by_another_session(self):
        self.own(A, "main")
        later = {"udids": {A: {"agent": "main", "t": time.time() + 60}}}
        hook.write_json(hook.session_path(OTHER), later)
        shut = []
        with patch.object(hook, "running", lambda *n: []), \
                patch.object(hook, "shutdown", lambda u: shut.append(u) or 0):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [])


class HookMain(Base):
    def run_main(self, stdin: str) -> str:
        buf = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(buf):
            rc = hook.main(["sim-session-hook.py"])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_garbage_input_is_silent(self):
        for raw in ("", "not json", "[1,2]", "{\"hook_event_name\": \"PostToolUse\"}"):
            self.assertEqual(self.run_main(raw), "")

    def test_disabled_by_env(self):
        with patch.dict(os.environ, {"SIM_REAPER_HOOKS": "0"}):
            self.assertEqual(self.run_main(json.dumps(bash("PreToolUse", "xcrun simctl boot x"))), "")
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "state", "pre")))

    def test_shell_prefilter_exits_zero_and_quiet(self):
        sh = REPO / "scripts" / "hooks" / "sim-session-hook.sh"
        for payload in (bash("PreToolUse", "ls"), bash("PreToolUse", f"xcrun simctl boot {A}"),
                        {"hook_event_name": "Stop", "session_id": SID}):
            p = subprocess.run(["/bin/sh", str(sh)], input=json.dumps(payload), capture_output=True,
                               text=True, timeout=30)
            self.assertEqual((p.returncode, p.stdout), (0, ""))


LAUNCHCTL = """PID\tStatus\tLabel
-\t0\tcom.apple.progressd
4242\t0\tUIKitApplication:codes.clutch.ios[1a2b][rb-legacy]
-\t0\tUIKitApplication:com.example.stopped[3c4d][rb-legacy]
777\t0\tUIKitApplication:com.apple.mobilesafari[5e6f][rb-legacy]
"""


class Reaper(Base):
    DEV = {"udid": A, "name": "iPhone 17", "runtime": "iOS-27-0"}

    def test_desktop_log_stream_simctl_is_not_busy(self):
        def fake(args):
            table = {("-x", "simctl"): {"100", "200"}, ("-f", reaper.LOG_STREAM_PATTERN): {"100"}}
            return set(table.get(tuple(args), set()))
        with patch.object(reaper, "pgrep_pids", fake):
            self.assertEqual(reaper.busy_processes(), ["simctlx1"])     # 200 is real work
        with patch.object(reaper, "pgrep_pids", lambda a: {"100"} if a[0] == "-x" and a[1] == "simctl"
                          else ({"100"} if a[1] == reaper.LOG_STREAM_PATTERN else set())):
            self.assertEqual(reaper.busy_processes(), [])                # only the streamer
        with patch.object(reaper, "pgrep_pids", lambda a: None):
            self.assertIn("xcodebuild?", reaper.busy_processes())         # cannot tell: busy

    def test_running_ui_apps_parses_launchctl(self):
        self.assertEqual(reaper.running_ui_apps(LAUNCHCTL), {"codes.clutch.ios", "com.apple.mobilesafari"})

    def test_user_app_check_uses_user_bundles_only(self):
        with patch.object(reaper, "user_bundles", lambda u: {"codes.clutch.ios"}), \
                patch.object(reaper, "run", lambda *a, **k: (0, LAUNCHCTL)):
            self.assertEqual(reaper.user_app_running(A), (True, "user app running: codes.clutch.ios"))
        with patch.object(reaper, "user_bundles", lambda u: {"com.example.stopped"}), \
                patch.object(reaper, "run", lambda *a, **k: (0, LAUNCHCTL)):
            self.assertFalse(reaper.user_app_running(A)[0])
        with patch.object(reaper, "user_bundles", lambda u: None):
            self.assertTrue(reaper.user_app_running(A)[0])

    def decide(self, age_min: float, busy=(), sim_app=0, watched=(), app=(False, "none running")):
        now = time.time()
        return reaper.decide(self.DEV, now - age_min * 60, now, list(busy), sim_app, set(watched), 3600,
                             check_apps=lambda u: app)

    def test_decisions(self):
        self.assertEqual(self.decide(5)[0], "skip")
        self.assertIn("booted-seen 5m < 60m", self.decide(5)[1])
        self.assertEqual(self.decide(90)[0], "shutdown")
        self.assertEqual(self.decide(90, busy=["xcodebuildx1"])[0], "skip")
        self.assertEqual(self.decide(90, sim_app=1)[0], "skip")
        self.assertEqual(self.decide(90, watched=[A])[0], "skip")
        self.assertEqual(self.decide(90, app=(True, "user app running: x"))[0], "skip")

    def run_reaper(self, args: list, booted: list, state: dict | None = None) -> tuple[str, list]:
        state_path = os.path.join(ROOT, "state", "state.json")
        if state is not None:
            hook.write_json(state_path, state)
        shut = []

        def fake_run(cmd, timeout=60, stdin_text=None):
            if cmd[1:3] == ["simctl", "shutdown"]:
                shut.append(cmd[3])
                return 0, ""
            raise AssertionError(f"unexpected command {cmd[:3]}")

        buf = io.StringIO()
        with patch.object(reaper, "list_booted", lambda: booted), \
                patch.object(reaper, "busy_processes", lambda: []), \
                patch.object(reaper, "pgrep_exact", lambda n: 0), \
                patch.object(reaper, "user_app_running", lambda u: (False, "0 user app(s) installed, none running")), \
                patch.object(reaper, "run", fake_run), redirect_stdout(buf):
            reaper.main(args + ["--state", state_path])
        return buf.getvalue(), shut

    def test_fresh_boot_is_recorded_and_skipped(self):
        log, shut = self.run_reaper([], [self.DEV])
        self.assertIn("SKIP", log)
        self.assertEqual(shut, [])
        self.assertIn(A, hook.load_json(os.path.join(ROOT, "state", "state.json"), {}))

    def test_old_idle_boot_dry_run_and_live(self):
        old = {A: time.time() - 2 * 3600}
        log, shut = self.run_reaper(["--dry-run"], [self.DEV], old)
        self.assertIn("WOULD-SHUTDOWN", log)
        self.assertEqual(shut, [])
        log, shut = self.run_reaper([], [self.DEV], old)
        self.assertIn("SHUTDOWN", log)
        self.assertEqual(shut, [A])

    def test_udid_filter_never_touches_other_devices(self):
        other = {"udid": B, "name": "iPad", "runtime": "iOS-27-0"}
        old = {A: time.time() - 7200, B: time.time() - 7200}
        log, shut = self.run_reaper(["--udid", B], [self.DEV, other], old)
        self.assertEqual(shut, [B])
        self.assertNotIn(A, log)

    def test_tidy_drops_entries_for_devices_no_longer_booted(self):
        hook.write_json(hook.session_path(SID), {"udids": {A: {"agent": "main", "t": 1}, B: {"agent": "main", "t": 1}}})
        reaper.tidy_hook_state({B}, time.time(), False, False)
        self.assertEqual(set(session_doc()["udids"]), {B})
        reaper.tidy_hook_state(set(), time.time(), False, False)
        self.assertFalse(os.path.exists(hook.session_path(SID)))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
