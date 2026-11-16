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


def bash(
    event: str, command: str, tuid: str = "toolu_1", sid: str = SID, agent: str | None = None
) -> dict:
    p = {
        "hook_event_name": event,
        "session_id": sid,
        "tool_name": "Bash",
        "tool_use_id": tuid,
        "tool_input": {"command": command},
    }
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
        p = patch.object(hook, "is_bitrig_running", lambda: False)
        p.start()
        self.addCleanup(p.stop)


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
        for cmd in (
            "ls -la",
            "xcrun simctl list devices",
            "git status",
            "xcrun simctl io booted screenshot x.png",
        ):
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
        hook.write_json(
            hook.session_path(OTHER), {"udids": {A: {"agent": "main", "t": time.time() - 7200}}}
        )
        hook.on_pre(bash("PreToolUse", f"xcrun simctl boot {A}"))
        set_state(A, 3)
        hook.on_post(bash("PostToolUse", f"xcrun simctl boot {A}"))
        self.assertIn(A, session_doc()["udids"])

    def test_mcp_attach_marks_watched(self):
        set_state(A, 3)
        p = {
            "hook_event_name": "PreToolUse",
            "session_id": SID,
            "tool_use_id": "m1",
            "tool_name": "mcp__Claude_Code_iOS_Simulator__control",
            "tool_input": {"action": "attach"},
        }
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
        with (
            patch.object(hook, "running", lambda *n: []),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [A])
        self.assertEqual(set(session_doc()["udids"]), {B})

    def test_worker_defers_while_xcodebuild_runs(self):
        self.own(A, "main")
        shut = []
        with (
            patch.object(hook, "running", lambda *n: ["xcodebuild"]),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [])
        self.assertIn(A, session_doc()["udids"])

    def test_session_end_shuts_everything_and_deletes_file(self):
        self.own(A, "main", watched=True)
        self.own(B, "agent-7")
        shut = []
        with (
            patch.object(hook, "running", lambda *n: ["xcodebuild"]),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("SessionEnd", SID, "*")
        self.assertEqual(sorted(shut), sorted([A, B]))
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def test_worker_skips_device_reclaimed_by_another_session(self):
        self.own(A, "main")
        later = {"udids": {A: {"agent": "main", "t": time.time() + 60}}}
        hook.write_json(hook.session_path(OTHER), later)
        shut = []
        with (
            patch.object(hook, "running", lambda *n: []),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [])

    def test_worker_skips_bitrig_exempt_simulator(self):
        self.own(A, "main")
        shut = []
        with (
            patch.object(hook, "running", lambda *n: []),
            patch.object(hook, "is_bitrig_running", lambda: True),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [])

    def test_worker_shuts_down_extra_simulator_even_when_bitrig_running(self):
        self.own(A, "main")
        self.own(B, "main")
        shut = []
        # A is newer than B, so A is Bitrig exempt and B is shut down
        with (
            patch.object(hook, "running", lambda *n: []),
            patch.object(hook, "is_bitrig_running", lambda: True),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [min(B, A)])

    def test_session_end_preserves_bitrig_exempt_simulator(self):
        self.own(A, "main")
        shut = []
        with (
            patch.object(hook, "running", lambda *n: []),
            patch.object(hook, "is_bitrig_running", lambda: True),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("SessionEnd", SID, "*")
        self.assertEqual(shut, [])
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def test_worker_defers_while_bitrig_cli_runs(self):
        self.own(A, "main")
        shut = []
        with (
            patch.object(hook, "running", lambda *n: ["bitrig"] if "bitrig" in n else []),
            patch.object(hook, "shutdown", lambda u: shut.append(u) or 0),
        ):
            hook.run_worker("Stop", SID, "main")
        self.assertEqual(shut, [])
        self.assertIn(A, session_doc()["udids"])


class HookMain(Base):
    def run_main(self, stdin: str) -> str:
        buf = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(buf):
            rc = hook.main(["sim-session-hook.py"])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_garbage_input_is_silent(self):
        for raw in ("", "not json", "[1,2]", '{"hook_event_name": "PostToolUse"}'):
            self.assertEqual(self.run_main(raw), "")

    def test_disabled_by_env(self):
        with patch.dict(os.environ, {"SIM_REAPER_HOOKS": "0"}):
            self.assertEqual(
                self.run_main(json.dumps(bash("PreToolUse", "xcrun simctl boot x"))), ""
            )
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "state", "pre")))

    def test_shell_prefilter_exits_zero_and_quiet(self):
        sh = REPO / "scripts" / "hooks" / "sim-session-hook.sh"
        for payload in (
            bash("PreToolUse", "ls"),
            bash("PreToolUse", f"xcrun simctl boot {A}"),
            {"hook_event_name": "Stop", "session_id": SID},
        ):
            p = subprocess.run(
                ["/bin/sh", str(sh)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual((p.returncode, p.stdout), (0, ""))


LAUNCHCTL = """PID\tStatus\tLabel
-\t0\tcom.apple.progressd
4242\t0\tUIKitApplication:codes.clutch.ios[1a2b][rb-legacy]
-\t0\tUIKitApplication:com.example.stopped[3c4d][rb-legacy]
777\t0\tUIKitApplication:com.apple.mobilesafari[5e6f][rb-legacy]
"""


class Reaper(Base):
    DEV = {"udid": A, "name": "iPhone 17", "runtime": "iOS-27-0"}

    def setUp(self):
        super().setUp()
        p = patch("os.getloadavg", return_value=(100.0, 100.0, 100.0))
        p.start()
        self.addCleanup(p.stop)

    def test_desktop_log_stream_simctl_is_not_busy(self):
        def fake(args):
            table = {("-x", "simctl"): {"100", "200"}, ("-f", reaper.LOG_STREAM_PATTERN): {"100"}}
            return set(table.get(tuple(args), set()))

        with patch.object(reaper, "pgrep_pids", fake):
            self.assertEqual(reaper.busy_processes(), ["simctlx1"])  # 200 is real work
        with patch.object(
            reaper,
            "pgrep_pids",
            lambda a: (
                {"100"}
                if a[0] == "-x" and a[1] == "simctl"
                else ({"100"} if a[1] == reaper.LOG_STREAM_PATTERN else set())
            ),
        ):
            self.assertEqual(reaper.busy_processes(), [])  # only the streamer
        with patch.object(reaper, "pgrep_pids", lambda a: None):
            self.assertIn("xcodebuild?", reaper.busy_processes())  # cannot tell: busy

    def test_xcodebuild_version_check_is_not_busy(self):
        def fake_pgrep(args):
            return {"101", "102"} if tuple(args) == ("-x", "xcodebuild") else set()

        def fake_run(args, timeout=60, stdin_text=None):
            if args[:3] == ["/bin/ps", "-o", "command="]:
                pid = args[args.index("-p") + 1]
                return (0, "xcodebuild -version\n" if pid == "101" else "xcodebuild -scheme Foo\n")
            return (0, "")

        with (
            patch.object(reaper, "pgrep_pids", fake_pgrep),
            patch.object(reaper, "run", fake_run),
        ):
            self.assertEqual(reaper.busy_processes(), ["xcodebuildx1"])  # 102 is a real build

    def test_xcodebuild_that_exited_before_ps_is_not_busy(self):
        # pgrep saw 101 (a probe) and 102 (a build);  101 exits before ps reads it.
        def fake_run(args, timeout=60, stdin_text=None):
            if args[:3] == ["/bin/ps", "-o", "command="]:
                pid = args[args.index("-p") + 1]
                return (1, "") if pid == "101" else (0, "xcodebuild -scheme Foo\n")
            return (0, "")

        for alive, want in (
            ({"102"}, ["xcodebuildx1"]),  # 101 is gone:  not a build
            (None, ["xcodebuildx2"]),  # pgrep failed on the re-check:  101 stays busy
            ({"101", "102"}, ["xcodebuildx2"]),  # ps failed but 101 still runs:  stays busy
        ):
            scans = iter([{"101", "102"}])

            def fake_pgrep(args, alive=alive, scans=scans):
                if tuple(args) != ("-x", "xcodebuild"):
                    return set()
                return next(scans, alive)  # the first call is the scan, later ones the re-check

            with (
                self.subTest(alive=alive),
                patch.object(reaper, "pgrep_pids", fake_pgrep),
                patch.object(reaper, "run", fake_run),
            ):
                self.assertEqual(reaper.busy_processes(), want)

    def test_load_guard_trip_point(self):
        with patch.object(reaper.os, "getloadavg", lambda: (799.9, 0.0, 0.0)):
            self.assertIsNone(reaper.load_guard_tripped())
        with patch.object(reaper.os, "getloadavg", lambda: (800.0, 0.0, 0.0)):
            self.assertEqual(reaper.load_guard_tripped(), 800.0)
        with patch.object(reaper.os, "getloadavg", side_effect=OSError):
            self.assertIsNone(reaper.load_guard_tripped())

    def test_load_guard_emergency_shuts_down_all_and_alerts(self):
        devs = [dict(self.DEV), {"udid": B, "name": "iPhone 17 Pro", "runtime": "iOS-27-0"}]
        shut, alerts = [], []

        def fake_run(cmd, timeout=60, stdin_text=None):
            if cmd[1:3] == ["simctl", "shutdown"]:
                shut.append(cmd[3])
                return (0, "")
            return (0, "")

        state_path = os.path.join(ROOT, "state", "lg.json")
        with (
            patch.object(reaper, "list_booted", lambda: devs),
            patch.object(reaper.os, "getloadavg", lambda: (850.0, 0.0, 0.0)),
            patch.object(reaper, "run", fake_run),
            patch.object(reaper, "fleet_alert", lambda c: alerts.append(c) or "ok"),
        ):
            self.assertEqual(reaper.main(["--state", state_path]), 0)
        self.assertEqual(sorted(shut), sorted([A, B]))
        self.assertEqual(len(alerts), 1)
        self.assertIn(A, alerts[0])
        self.assertIn(B, alerts[0])

    def test_load_guard_dry_run_shuts_down_nothing(self):
        shut = []

        def fake_run(cmd, timeout=60, stdin_text=None):
            if cmd[1:3] == ["simctl", "shutdown"]:
                shut.append(cmd[3])
            return (0, "")

        state_path = os.path.join(ROOT, "state", "lg2.json")
        with (
            patch.object(reaper, "list_booted", lambda: [dict(self.DEV)]),
            patch.object(reaper.os, "getloadavg", lambda: (950.0, 0.0, 0.0)),
            patch.object(reaper, "run", fake_run),
            patch.object(reaper, "fleet_alert", lambda c: self.fail("no alert in dry-run")),
        ):
            self.assertEqual(reaper.main(["--dry-run", "--state", state_path]), 0)
        self.assertEqual(shut, [])

    def test_running_ui_apps_parses_launchctl(self):
        self.assertEqual(
            reaper.running_ui_apps(LAUNCHCTL), {"codes.clutch.ios", "com.apple.mobilesafari"}
        )

    def test_user_app_check_uses_user_bundles_only(self):
        with (
            patch.object(reaper, "user_bundles", lambda u: {"codes.clutch.ios"}),
            patch.object(reaper, "run", lambda *a, **k: (0, LAUNCHCTL)),
        ):
            self.assertEqual(
                reaper.user_app_running(A), (True, "user app running: codes.clutch.ios")
            )
        with (
            patch.object(reaper, "user_bundles", lambda u: {"com.example.stopped"}),
            patch.object(reaper, "run", lambda *a, **k: (0, LAUNCHCTL)),
        ):
            self.assertFalse(reaper.user_app_running(A)[0])
        with patch.object(reaper, "user_bundles", lambda u: None):
            self.assertTrue(reaper.user_app_running(A)[0])

    def decide(self, age_min: float, busy=(), sim_app=0, watched=(), app=(False, "none running")):
        now = time.time()
        return reaper.decide(
            self.DEV,
            now - age_min * 60,
            now,
            list(busy),
            sim_app,
            set(watched),
            3600,
            check_apps=lambda u: app,
        )

    def test_decisions(self):
        self.assertEqual(self.decide(5)[0], "skip")
        self.assertIn("booted-seen 5m < 60m", self.decide(5)[1])
        self.assertEqual(self.decide(90)[0], "shutdown")
        self.assertEqual(self.decide(90, busy=["xcodebuildx1"])[0], "skip")
        self.assertEqual(self.decide(90, sim_app=1)[0], "skip")
        self.assertEqual(self.decide(90, watched=[A])[0], "skip")
        self.assertEqual(self.decide(90, app=(True, "user app running: x"))[0], "skip")

    def run_reaper(
        self,
        args: list,
        booted: list,
        state: dict | None = None,
        bitrig_app: int = 0,
        state_path: str | None = None,
    ) -> tuple[str, list]:
        state_path = state_path or os.path.join(ROOT, "state", "state.json")
        if state is not None:
            hook.write_json(state_path, state)
        shut = []

        def fake_run(cmd, timeout=60, stdin_text=None):
            if cmd[1:3] == ["simctl", "shutdown"]:
                shut.append(cmd[3])
                return 0, ""
            raise AssertionError(f"unexpected command {cmd[:3]}")

        buf = io.StringIO()
        with (
            patch.object(reaper, "list_booted", lambda: booted),
            patch.object(reaper, "busy_processes", list),
            patch.object(
                reaper, "pgrep_exact", lambda n: bitrig_app if n in ("Bitrig", "bitrig") else 0
            ),
            patch.object(
                reaper,
                "user_app_running",
                lambda u: (False, "0 user app(s) installed, none running"),
            ),
            patch.object(reaper, "run", fake_run),
            redirect_stdout(buf),
        ):
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

    def test_bitrig_exempts_one_simulator_and_reaps_extras(self):
        other = {"udid": B, "name": "iPad", "runtime": "iOS-27-0"}
        now = time.time()
        old = {A: now - 7200, B: now - 5400}
        log_single, shut_single = self.run_reaper([], [self.DEV], {A: now - 7200}, bitrig_app=1)
        self.assertIn("Bitrig is running (1 simulator exempt)", log_single)
        self.assertEqual(shut_single, [])

        log_multi, shut_multi = self.run_reaper([], [self.DEV, other], old, bitrig_app=1)
        self.assertEqual(shut_multi, [A])
        self.assertIn("Bitrig is running (1 simulator exempt)", log_multi)

    def test_bitrig_app_matches_lowercase(self):
        now = time.time()

        def fake_pgrep(n):
            return 1 if n == "bitrig" else 0

        state_path = os.path.join(ROOT, "state", "state.json")
        hook.write_json(state_path, {A: now - 7200})
        buf = io.StringIO()
        with (
            patch.object(reaper, "list_booted", lambda: [self.DEV]),
            patch.object(reaper, "busy_processes", list),
            patch.object(reaper, "pgrep_exact", fake_pgrep),
            patch.object(reaper, "user_app_running", lambda u: (False, "none running")),
            redirect_stdout(buf),
        ):
            reaper.main(["--state", state_path])
        self.assertIn("Bitrig is running (1 simulator exempt)", buf.getvalue())

    def test_udid_filter_ignores_letter_case(self):
        other = {"udid": B, "name": "iPad", "runtime": "iOS-27-0"}
        old = {A: time.time() - 7200, B: time.time() - 7200}
        log, shut = self.run_reaper(["--udid", B.lower()], [self.DEV, other], old)
        self.assertEqual(shut, [B])  # simctl reports uppercase;  a lowercase --udid still matches
        alerts = []
        log, shut = self.run_guard(["--udid", B.lower()], [self.DEV, other], alerts)
        self.assertEqual(shut, [B])
        self.assertEqual(len(alerts), 1)

    def test_udid_filter_never_touches_other_devices(self):
        other = {"udid": B, "name": "iPad", "runtime": "iOS-27-0"}
        old = {A: time.time() - 7200, B: time.time() - 7200}
        log, shut = self.run_reaper(["--udid", B], [self.DEV, other], old)
        self.assertEqual(shut, [B])
        self.assertNotIn(A, log)

    def test_tidy_drops_entries_for_devices_no_longer_booted(self):
        hook.write_json(
            hook.session_path(SID),
            {"udids": {A: {"agent": "main", "t": 1}, B: {"agent": "main", "t": 1}}},
        )
        reaper.tidy_hook_state({B}, time.time(), False, False)
        self.assertEqual(set(session_doc()["udids"]), {B})
        reaper.tidy_hook_state(set(), time.time(), False, False)
        self.assertFalse(os.path.exists(hook.session_path(SID)))

    def run_guard(self, args, booted, alerts, outcome="ok", state_path=None):
        with (
            patch.object(reaper, "load_guard_tripped", lambda: 900.0),
            patch.object(reaper, "fleet_alert", lambda c: alerts.append(c) or outcome),
        ):
            return self.run_reaper(args, booted, state_path=state_path)

    def test_load_guard_respects_udid_filter(self):
        other = {"udid": B, "name": "iPad", "runtime": "iOS-27-0"}
        alerts = []
        log, shut = self.run_guard(["--udid", B], [self.DEV, other], alerts)
        self.assertEqual(shut, [B])
        self.assertNotIn(A, " ".join(shut))
        self.assertEqual(len(alerts), 1)

    def test_load_guard_alert_cooldown(self):
        alerts = []
        self.run_guard([], [self.DEV], alerts)
        self.assertEqual(len(alerts), 1)
        log, shut = self.run_guard([], [self.DEV], alerts)
        self.assertEqual(len(alerts), 1)  # second trip inside the hour: no second post
        self.assertIn("alert cooldown active", log)

    def test_load_guard_unknown_outcome_starts_cooldown(self):
        alerts = []
        self.run_guard([], [self.DEV], alerts, outcome="unknown")
        log, _ = self.run_guard([], [self.DEV], alerts, outcome="unknown")
        self.assertEqual(len(alerts), 1)  # it may already be in #Alerts:  never post it twice
        self.assertIn("alert cooldown active", log)

    def test_load_guard_unsent_outcome_does_not_start_cooldown(self):
        alerts = []
        self.run_guard([], [self.DEV], alerts, outcome="unsent")
        self.run_guard([], [self.DEV], alerts, outcome="ok")
        self.assertEqual(len(alerts), 2)  # nothing reached Zulip the first time:  try again
        self.run_guard([], [self.DEV], alerts, outcome="ok")
        self.assertEqual(len(alerts), 2)

    def test_load_guard_cooldown_shared_across_state_paths(self):
        alerts = []
        other_dir = os.path.join(ROOT, "state", "elsewhere")
        self.run_guard([], [self.DEV], alerts)
        self.run_guard([], [self.DEV], alerts, state_path=os.path.join(other_dir, "s.json"))
        self.assertEqual(len(alerts), 1)  # one machine-wide marker, whatever --state names
        self.assertTrue(os.path.exists(os.path.join(reaper.STATE_DIR, "guard-alert.json")))
        self.assertFalse(os.path.exists(os.path.join(other_dir, "guard-alert.json")))

    def test_load_guard_alert_house_format(self):
        alerts = []
        self.run_guard([], [self.DEV], alerts)
        content = alerts[0]
        self.assertTrue(content.startswith("SEVERE • sim-idle-reaper • "), content)
        self.assertIn("1-min load 900 >= 800", content)
        self.assertIn("shut down 1 of 1 simulator(s)", content)
        self.assertTrue(content.endswith(" • " + reaper.REAPER_LOG), content)

    def fleet_alert_with(self, result):
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(argv, result, "", "simple-chat: some reason\n")

        buf = io.StringIO()
        with patch.object(reaper.subprocess, "run", fake), redirect_stdout(buf):
            outcome = reaper.fleet_alert("SEVERE • sim-idle-reaper • x • y")
        return outcome, calls, buf.getvalue()

    def test_fleet_alert_posts_to_alerts_service_topic(self):
        outcome, calls, log = self.fleet_alert_with(0)
        self.assertEqual(outcome, "ok")
        argv = calls[0]
        self.assertEqual(argv[1:4], ["--as", "MA", "post"])
        self.assertIn("--no-tag", argv)  # the #Alerts line starts with its severity, not a seat tag
        self.assertEqual(argv[argv.index("--channel") + 1], "Alerts")
        self.assertEqual(argv[argv.index("--topic") + 1], "sim-idle-reaper")
        # The first line keeps the house format;  the second tags both Housekeepers and Jay (owner, Oct 29 2026).
        self.assertEqual(
            argv[-1],
            "SEVERE • sim-idle-reaper • x • y\n@**BF-Housekeeper|1216054** @**GB-Housekeeper|1215533** @**Jay Wedgeworth|1211974**",
        )
        self.assertNotIn("@**all**", argv[-1])  # load is never an out-of-space issue
        self.assertIn("fleet alert posted", log)

    def test_fleet_alert_explicit_cli_override_wins_over_path(self):
        with (
            patch.dict(os.environ, {"SIM_REAPER_SIMPLE_CHAT": "/opt/pinned/simple-chat"}),
            patch.object(reaper.shutil, "which", lambda name: "/usr/local/bin/simple-chat"),
        ):
            _, calls, _ = self.fleet_alert_with(0)
        self.assertEqual(calls[0][0], "/opt/pinned/simple-chat")
        with (
            patch.dict(os.environ, {"SIM_REAPER_SIMPLE_CHAT": ""}),
            patch.object(reaper.shutil, "which", lambda name: "/usr/local/bin/simple-chat"),
        ):
            _, calls, _ = self.fleet_alert_with(0)
        self.assertEqual(calls[0][0], "/usr/local/bin/simple-chat")
        with (
            patch.dict(os.environ, {"SIM_REAPER_SIMPLE_CHAT": ""}),
            patch.object(reaper.shutil, "which", lambda name: None),
        ):
            _, calls, _ = self.fleet_alert_with(0)
        self.assertEqual(calls[0][0], reaper.SIMPLE_CHAT)

    def test_fleet_alert_outcomes(self):
        cases = [
            (2, "unsent"),  # usage error:  refused before anything was sent
            (3, "unsent"),  # credential error:  refused before anything was sent
            (5, "unknown"),  # an API refusal, or a gateway status after the POST went out
            (6, "unknown"),  # network error:  the POST may have reached Zulip
            (1, "unknown"),
            (subprocess.TimeoutExpired(["simple-chat"], 60), "unknown"),
            (FileNotFoundError(), "unsent"),  # the CLI is not installed
        ]
        for result, want in cases:
            with self.subTest(result=result):
                outcome, _, log = self.fleet_alert_with(result)
                self.assertEqual(outcome, want)
                if isinstance(result, int):
                    self.assertIn(f"rc={result}: simple-chat: some reason", log)

    def test_list_booted_skips_malformed_udid_without_echoing_it(self):
        doc = {
            "devices": {
                "iOS-27-0": [
                    {"udid": A, "name": "iPhone 17", "state": "Booted"},
                    {"udid": "--standalone", "name": "bad", "state": "Booted"},
                    {"udid": A.lower() + "\n", "name": "bad2", "state": "Booted"},
                ]
            }
        }
        buf = io.StringIO()
        with (
            patch.object(reaper, "run", lambda *a, **k: (0, json.dumps(doc))),
            redirect_stdout(buf),
        ):
            devs = reaper.list_booted()
        self.assertEqual([d["udid"] for d in devs], [A])
        self.assertNotIn("--standalone", buf.getvalue())
        self.assertEqual(buf.getvalue().count("invalid simulator UDID"), 2)

    def test_bad_udid_rejected(self):
        for bad in ("not-a-udid", A + "\n"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as cm:
                reaper.main(["--udid", bad])
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
