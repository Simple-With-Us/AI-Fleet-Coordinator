"""The exceptions file schema, and the placement module's matching and mode parsing (AFC-618)."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import fleet_lanes
from fleet_lanes import lane_guard_hook as H
from fleet_lanes import placement as P
from fleet_lanes.tests import fixtures_guard as F

TODAY = dt.date(2026, 11, 4)


def roots(ci: bool = False) -> SimpleNamespace:
    return SimpleNamespace(home="/h", code_root="/h/Code", apps_root="/h/apps", lanes_root="/h/apps/lanes",
                           case_insensitive=ci)


def row(pattern: str, **kw) -> dict:
    base = {
        "id": "r1", "pattern": pattern, "class": "service", "creator": "x", "reason": "a reason",
        "decided_by": "CLAUDE", "decided_on": "2026-11-04", "expires": "2027-02-04", "hosts": ["mac"],
    }
    base.update(kw)
    return base


class ShippedFile(unittest.TestCase):
    def setUp(self) -> None:
        with open(P.default_exceptions_path(), encoding="utf-8") as fh:
            self.raw = fh.read()
        self.data = json.loads(self.raw)

    def test_the_shipped_file_is_valid(self) -> None:
        self.assertEqual(P.validate_rows(self.data), [])
        self.assertEqual(len(P.parse_rows(self.data)), len(self.data["exceptions"]))

    def test_every_row_has_an_owner_and_an_expiry(self) -> None:
        for r in self.data["exceptions"]:
            for key in ("class", "reason", "decided_by", "decided_on", "expires", "hosts"):
                self.assertTrue(r[key], f"{r['id']} lacks {key}")
            dt.date.fromisoformat(r["expires"])

    def test_no_private_path_anywhere_in_the_file(self) -> None:
        for mark in P.PRIVATE_PATH_MARKS:
            self.assertNotIn(mark, self.raw)
        self.assertIsNone(re.search(r'"pattern":\s*"/', self.raw))

    def test_the_plan_seeds_are_present(self) -> None:
        patterns = {r["pattern"] for r in self.data["exceptions"]}
        for want in ("{apps}/botfleet-server", "{apps}/clutch-runtime", "{lanes}/_managed/fleet/simple-tools-runtime",
                     "{home}/.codex/.tmp", "{home}/.claude/plugins", "{home}/.grok/marketplace-cache",
                     "**/SourcePackages/checkouts", "{home}/.botfleet/workspaces", "{home}/BotFleet-migration-backups",
                     "{code}/BotFleet-server"):
            self.assertIn(want, patterns)

    def test_a_service_pinned_clone_carries_its_reason(self) -> None:
        pinned = [r for r in self.data["exceptions"] if r["pattern"] == "{code}/BotFleet-server"][0]
        self.assertEqual((pinned["class"], pinned["hosts"]), ("service", ["server"]))
        self.assertIn("Pinned", pinned["reason"])

    def test_no_row_excuses_the_two_repo_local_leaks(self) -> None:
        for r in self.data["exceptions"]:
            self.assertNotIn(".muse", r["pattern"])
            self.assertNotIn(".claude/worktrees", r["pattern"])


class Validator(unittest.TestCase):
    def check(self, bad: dict, needle: str) -> None:
        problems = P.validate_rows({"exceptions": [bad]})
        self.assertTrue(any(needle in p for p in problems), problems)

    def test_refusals(self) -> None:
        self.check(row("{home}/x", expires=""), "expires")
        self.check(row("{home}/x", decided_by=""), "decided_by")
        self.check(row("/abs/path"), "pattern must start")
        self.check(row("{home}/x", **{"class": "nope"}), "class")
        self.check(row("{home}/x", hosts=["moon"]), "hosts")
        self.check(row("{home}/x", hosts=[]), "hosts")
        self.check(row("{home}/x", expires="soon"), "ISO date")
        self.check(row("{home}/x", expires="2026-01-01"), "after")
        self.check(row("{home}/x", reason="lives at /mnt/volume/Code"), "private path")
        self.check(row("{home}/x", extra=1), "unknown keys")

    def test_a_duplicate_id_is_refused(self) -> None:
        problems = P.validate_rows({"exceptions": [row("{home}/a"), row("{home}/b")]})
        self.assertTrue(any("duplicate" in p for p in problems), problems)

    def test_a_bad_top_level_is_refused(self) -> None:
        self.assertTrue(P.validate_rows([]))
        self.assertTrue(P.validate_rows({"exceptions": {}}))


class Matching(unittest.TestCase):
    def rows(self, *raw: dict) -> tuple:
        return P.parse_rows({"exceptions": list(raw)})

    def find(self, path: str, raw: dict, *, host: str = "mac", ci: bool = False):
        return P.find_exception([path], roots(ci), self.rows(raw), host=host, today=TODAY)

    def test_at_or_under(self) -> None:
        self.assertIsNotNone(self.find("/h/apps/svc", row("{apps}/svc")))
        self.assertIsNotNone(self.find("/h/apps/svc/inner", row("{apps}/svc")))
        self.assertIsNone(self.find("/h/apps/svc2", row("{apps}/svc")))

    def test_star_is_one_component_and_double_star_any(self) -> None:
        self.assertIsNotNone(self.find("/h/a/x/b", row("{home}/a/*/b")))
        self.assertIsNone(self.find("/h/a/x/y/b", row("{home}/a/*/b")))
        self.assertIsNotNone(self.find("/private/tmp/w/DD/SourcePackages/checkouts/s", row("**/SourcePackages/checkouts")))

    def test_host_is_respected(self) -> None:
        self.assertIsNone(self.find("/h/apps/svc", row("{apps}/svc"), host="server"))

    def test_case_folds_only_on_a_folding_volume(self) -> None:
        self.assertIsNone(self.find("/h/APPS/svc", row("{apps}/svc")))
        self.assertIsNotNone(self.find("/h/APPS/svc", row("{apps}/svc"), ci=True))

    def test_expired_is_reported_and_marked(self) -> None:
        hit = self.find("/h/apps/svc", row("{apps}/svc", decided_on="2025-01-01", expires="2025-02-01"))
        self.assertTrue(hit.expired)
        self.assertEqual(len(P.expired_rows(self.rows(row("{apps}/a", decided_on="2025-01-01", expires="2025-02-01")),
                                            host="mac", today=TODAY)), 1)

    def test_an_in_date_row_beats_an_expired_one(self) -> None:
        old = row("{apps}/svc", id="old", decided_on="2025-01-01", expires="2025-02-01")
        new = row("{apps}/svc", id="new")
        hit = P.find_exception(["/h/apps/svc"], roots(), self.rows(old, new), host="mac", today=TODAY)
        self.assertEqual((hit.row.id, hit.expired), ("new", False))

    def test_a_malformed_row_is_dropped_not_fatal(self) -> None:
        self.assertEqual(P.parse_rows({"exceptions": [{"id": "x"}, 3]}), ())
        self.assertEqual(P.parse_rows([]), ())

    def test_an_unreadable_file_is_no_rows(self) -> None:
        self.assertEqual(P.load_exceptions("/nonexistent/file.json"), ())


class Mode(unittest.TestCase):
    def test_defaults(self) -> None:
        m = P.parse_mode("")
        self.assertEqual((m.mode, m.block_classes, m.isolation), ("detect", (), "off"))

    def test_block_all_and_block_some(self) -> None:
        self.assertEqual(P.parse_mode("block").block_classes, P.RULE_CLASSES)
        m = P.parse_mode("block temp,code")
        self.assertEqual(m.block_classes, ("temp", "code"))
        self.assertTrue(m.blocks("temp"))
        self.assertFalse(m.blocks("home"))

    def test_isolation_is_its_own_line(self) -> None:
        m = P.parse_mode("# note\ndetect\nisolation block\n")
        self.assertEqual((m.mode, m.isolation), ("detect", "block"))

    def test_a_typo_never_turns_a_block_on(self) -> None:
        m = P.parse_mode("blok temp\nisolation blok\nblock tempp")
        self.assertEqual(m.mode, "detect")
        self.assertEqual(m.isolation, "off")
        self.assertTrue(m.problems)

    def test_the_environment_switch_wins(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "mode")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("block\nisolation block\n")
            env = {P.ENV_MODE_FILE: path}
            self.assertEqual(P.read_mode(home, env).mode, "block")
            off = P.read_mode(home, {**env, P.ENV_GUARD: "off"})
            self.assertEqual((off.mode, off.isolation), ("off", "off"))

    def test_a_missing_file_is_detect(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(P.read_mode(home, {}).mode, "detect")


class EventLog(unittest.TestCase):
    def test_round_trip_and_torn_lines(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = {P.ENV_STATE_DIR: os.path.join(home, "s")}
            P.log_event(home, {"a": 1}, env)
            path = P.event_log_path(home, env)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("{torn\n")
            P.log_event(home, {"a": 2}, env)
            self.assertEqual([e["a"] for e in P.read_events(path)], [1, 2])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_a_bad_directory_never_raises(self) -> None:
        P.log_event("/nonexistent", {"a": 1}, {P.ENV_STATE_DIR: "/dev/null/x"})


class TestRunNeverReachesTheRealLog(unittest.TestCase):
    """A run of the suite put 114 fixture events in the account's own log, which is the input to the seven-day clean-detect gate.  The
    account's home is faked here, so a test of this guard that fails cannot itself do the damage."""

    DETECT = "git -C ~/Code/DealDex worktree add -b claude/fix ~/apps/dealdex-claude-fix origin/main"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.log = os.path.join(self.home, P.STATE_DIR_REL, P.EVENT_LOG_NAME)
        patcher = mock.patch.object(P, "_account_home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_log_event_refuses_the_real_log_whatever_home_it_is_given(self) -> None:
        P.log_event(self.home, {"a": 1}, {"HOME": self.home})
        self.assertFalse(os.path.exists(self.log))

    def test_naming_the_real_folder_outright_is_refused_too(self) -> None:
        P.log_event("/elsewhere", {"a": 1}, {P.ENV_STATE_DIR: os.path.dirname(self.log)})
        self.assertFalse(os.path.exists(self.log))

    def test_a_log_a_test_points_elsewhere_still_works(self) -> None:
        env = {P.ENV_STATE_DIR: os.path.join(self.home, "s")}
        P.log_event(self.home, {"a": 1}, env)
        self.assertEqual([e["a"] for e in P.read_events(P.event_log_path(self.home, env))], [1])

    def test_the_hook_detect_path_cannot_reach_it(self) -> None:
        """The in-process path the suite used:  lane_guard_hook.main with the fixture HOME, which on the owner's Mac was the real one."""
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": self.DETECT}, "cwd": self.home})
        reg = os.path.join(self.home, "reg.json")
        with open(reg, "w", encoding="utf-8") as fh:
            json.dump(F.REGISTRY_DATA, fh)
        env = {"HOME": self.home, "AGENT_SEAT": "CLAUDE", "FLEET_APPS_JSON": reg, "TMPDIR": "/nonexistent-tmp/"}
        seen = []
        real_detect = H.record_detection

        def spy(decision):
            seen.append(getattr(decision, "detected_detail", None))
            real_detect(decision)

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(H, "record_detection", spy):
            H.main(["--format", "claude"], io.StringIO(payload), io.StringIO(), io.StringIO())
        self.assertTrue(seen and seen[0], "this command no longer detects, so the test proves nothing")
        self.assertFalse(os.path.exists(self.log))

    def test_outside_a_test_run_the_real_log_is_written(self) -> None:
        saved = {name: sys.modules.pop(name) for name in ("unittest", "pytest") if name in sys.modules}
        self.addCleanup(sys.modules.update, saved)
        P.log_event(self.home, {"a": 1}, {"HOME": self.home})
        self.assertTrue(os.path.exists(self.log))

    def test_the_marker_alone_guards_a_child_that_never_imported_unittest(self) -> None:
        saved = {name: sys.modules.pop(name) for name in ("unittest", "pytest") if name in sys.modules}
        self.addCleanup(sys.modules.update, saved)
        P.log_event(self.home, {"a": 1}, {"HOME": self.home, P.ENV_TEST_RUN: "1"})
        self.assertFalse(os.path.exists(self.log))


class TestRunReachesEveryChild(unittest.TestCase):
    def test_a_child_with_a_hand_built_environment_gets_the_marker(self) -> None:
        out = subprocess.run([sys.executable, "-I", "-c", f"import os; print(os.environ.get('{P.ENV_TEST_RUN}'))"],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "1")

    def test_a_real_hook_or_shim_process_is_not_taken_for_a_test_run(self) -> None:
        """What the guard keys on must never be true in production, or the real log would silently stop filling."""
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        code = ("import sys, os; sys.path.insert(0, sys.argv[1]); os.environ.pop('" + P.ENV_TEST_RUN + "', None)\n"
                "from fleet_lanes import lane_guard_hook, fleet_guard_hook, minimax_guard_hook, git_shim, guard, placement, placement_watch, doctor\n"
                "print('unittest' in sys.modules, 'pytest' in sys.modules, '" + P.ENV_TEST_RUN + "' in os.environ)")
        out = subprocess.run([sys.executable, "-I", "-c", code, here], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False False False")

    def test_a_test_runs_git_is_not_an_installed_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shimmed, plain = os.path.join(tmp, "a"), os.path.join(tmp, "b")
            os.makedirs(shimmed)
            os.makedirs(plain)
            with open(os.path.join(shimmed, "git"), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n# Fleet git shim:  a stand-in\n")
            with open(os.path.join(plain, "git"), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            self.assertEqual(fleet_lanes._without_git_shim(f"{shimmed}:{plain}:{tmp}"), f"{plain}:{tmp}")  # noqa: SLF001
        found = shutil.which("git")
        with open(found, "rb") as fh:
            self.assertNotIn(b"Fleet git shim", fh.read(4096), found)

    def test_the_fixture_home_is_not_this_account(self) -> None:
        self.assertNotEqual(os.path.realpath(F.HOME), os.path.realpath(P._account_home()))  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
