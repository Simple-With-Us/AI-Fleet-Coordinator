"""Tests for fleet-cli-git-update.py:  a local bare repo stands in for GitHub, with stub tools."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "fleet_cli_git_update", HERE / "fleet-cli-git-update.py"
)
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

PY_OK = "#!/usr/bin/python3\nimport sys\nsys.exit(0)\n"
PY_BROKEN = "#!/usr/bin/python3\nimport sys\nsys.exit(1)\n"
BOARD = "#!/usr/bin/python3\nimport sys\nsys.exit(2 if sys.argv[1:] == ['show'] else 0)\n"
BASH_OK = "#!/bin/bash\nexit 0\n"
# A stand-in for fleet-cli-release.py:  only what the updater imports.
RELEASE = """from pathlib import Path
def new_wrapper_plan(home):
    path = Path(home) / ".local/bin/recall"
    return {} if path.exists() else {path: (b"#!/bin/sh\\n", 0o755)}
def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
"""


def run(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class PrivateStateTests(unittest.TestCase):
    def test_state_is_private_and_both_file_and_directory_are_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private/state.json"
            with mock.patch.object(U.os, "fsync", wraps=os.fsync) as synced:
                U.save_state(path, {"commit": "a" * 40})
            self.assertEqual(synced.call_count, 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(U.load_state(path), {"commit": "a" * 40})

    def test_presend_sync_failure_preserves_old_state_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            U.save_state(path, {"commit": "a" * 40})
            with mock.patch.object(U.os, "fsync", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    U.save_state(path, {"commit": "b" * 40})
            self.assertEqual(U.load_state(path), {"commit": "a" * 40})
            self.assertEqual(list(Path(tmp).glob(".git-state-*")), [])

    def test_malformed_and_symlink_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(U.load_state(path), {})
            for text in ("broken", "[]", '{"commit":"--option"}'):
                path.write_text(text)
                with self.assertRaisesRegex(U.Failed, "invalid_state_file"):
                    U.load_state(path)
            target = Path(tmp) / "target"
            path.rename(target)
            path.symlink_to(target)
            with self.assertRaisesRegex(U.Failed, "unsafe_state_file"):
                U.load_state(path)

    def test_directory_symlink_rejected_and_flock_still_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            with U.locked(base), self.assertRaisesRegex(U.Failed, "update_locked"):
                U.locked(base)
            link = Path(tmp) / "link"
            link.symlink_to(base)
            with self.assertRaisesRegex(U.Failed, "unsafe_state_directory"):
                U.locked(link)
            (base / "update.lock").unlink()
            target = Path(tmp) / "untouched"
            target.write_text("synthetic")
            (base / "update.lock").symlink_to(target)
            with self.assertRaisesRegex(U.Failed, "unsafe_lock_file"):
                U.locked(base)
            self.assertEqual(target.read_text(), "synthetic")

    def test_dash_home_clone_uses_absolute_destination_and_option_terminator(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(U, "locked"),
            mock.patch.object(
                U.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git")
            ) as run_clone,
        ):
            with self.assertRaisesRegex(U.Failed, "clone_failed"):
                U.setup(Path("-synthetic-home"))
            argv = run_clone.call_args.args[0]
            self.assertEqual(argv[4], "--")
            self.assertTrue(Path(argv[-1]).is_absolute())


class GitUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.origin = root / "origin.git"
        self.work = root / "work"
        run(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        run(root, "clone", "-q", str(self.origin), str(self.work))
        self.commit(agent_sync=PY_OK)
        self.remote = U.REMOTE
        U.REMOTE = str(self.origin)

    def tearDown(self) -> None:
        U.REMOTE = self.remote
        self.tmp.cleanup()

    def commit(self, agent_sync: str = PY_OK) -> str:
        files = {
            "scripts/simple-chat": agent_sync,
            "scripts/simple-notes": PY_OK,
            "scripts/simple-board": BOARD,
            "scripts/recall": PY_OK,
            "scripts/fleet_lanes/bin/lane": BASH_OK,
            "scripts/fleet-mode": BASH_OK,
            "scripts/fleet-cli-release.py": RELEASE,
            "scripts/fleet-cli-git-update.py": (HERE / "fleet-cli-git-update.py").read_text(),
        }
        for rel, text in files.items():
            path = self.work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(0o755)
        self.commits = getattr(self, "commits", 0) + 1
        (self.work / "stamp").write_text(str(self.commits))  # every commit differs
        run(self.work, "add", "-A")
        run(self.work, "commit", "-q", "-m", "c")
        run(self.work, "push", "-q", "origin", "HEAD:main")
        return run(self.work, "rev-parse", "HEAD")

    def base(self) -> Path:
        return self.home / ".local/share/fleet-cli"

    def test_setup_points_current_at_the_checkout_pauses_the_receiver_and_adds_missing_wrappers(
        self,
    ) -> None:
        result = U.setup(self.home)
        head = run(self.work, "rev-parse", "HEAD")
        self.assertEqual(U.status(self.home)["commit"], head)
        self.assertEqual(os.readlink(self.base() / "current"), "repo")
        self.assertTrue(U.status(self.home)["current_is_checkout"])
        self.assertIn(b"retired", (self.base() / "paused").read_bytes())
        self.assertEqual(result["wrappers_written"], ["recall"])
        self.assertTrue((self.home / ".local/bin/recall").exists())

    def test_update_moves_to_a_good_commit_and_reports_unchanged_after(self) -> None:
        U.setup(self.home)
        old = run(self.work, "rev-parse", "HEAD")
        new = self.commit(agent_sync=PY_OK)
        result = U.update(self.home)
        self.assertEqual(result, {"status": "updated", "commit": new, "previous": old})
        self.assertEqual(U.update(self.home)["status"], "unchanged")

    def test_a_commit_that_fails_the_smoke_test_is_skipped_until_main_moves(self) -> None:
        U.setup(self.home)
        good = run(self.work, "rev-parse", "HEAD")
        bad = self.commit(agent_sync=PY_BROKEN)
        result = U.update(self.home)
        self.assertEqual(result["status"], "smoke_failed")
        self.assertEqual(result["reason"], "smoke_exit_mismatch_simple_chat_version")
        self.assertEqual(U.status(self.home)["commit"], good)
        self.assertEqual(U.status(self.home)["bad"], bad)
        self.assertEqual(U.update(self.home)["status"], "skipped_bad")
        self.commit(agent_sync=PY_BROKEN)
        self.assertEqual(U.main(["update", "--home", str(self.home)]), 1)
        fixed = self.commit(agent_sync=PY_OK)
        self.assertEqual(U.update(self.home)["status"], "updated")
        self.assertEqual(U.status(self.home)["commit"], fixed)
        self.assertIsNone(U.status(self.home)["bad"])

    def test_a_target_that_dropped_a_cli_is_smoked_with_the_targets_own_case_list(self) -> None:
        # The Oct 22, 2026 cutover removed a CLI while the installed updater still smoked it:
        # the host then sat on an old commit for hours, refusing to move.  The case list has to
        # come from the checkout being tested, so a CLI that is gone cannot fail the smoke.
        U.setup(self.home)
        (self.work / "scripts/simple-notes").unlink()
        updater = (HERE / "fleet-cli-git-update.py").read_text()
        trimmed = updater.replace(
            '    ("simple_notes_version", "scripts/simple-notes", ["--version"], 0),\n', ""
        )
        self.assertNotEqual(trimmed, updater)
        (self.work / "scripts/fleet-cli-git-update.py").write_text(trimmed)
        run(self.work, "add", "-A")
        run(self.work, "commit", "-q", "-m", "drop simple-notes")
        run(self.work, "push", "-q", "origin", "HEAD:main")
        new = run(self.work, "rev-parse", "HEAD")
        result = U.update(self.home)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["commit"], new)
        self.assertEqual(U.status(self.home)["commit"], new)
        self.assertIsNone(U.status(self.home)["bad"])
        # The smoke really did read the target's own list, not the running updater's.
        cases, _scripts, _timeout = U.smoke_settings(self.base() / "repo")
        self.assertNotIn("simple_notes_version", [case[0] for case in cases])

    def test_the_running_settings_are_used_when_the_target_has_no_updater_to_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "checkout"
            repo.mkdir()
            cases, scripts, timeout = U.smoke_settings(repo)
            self.assertEqual(cases, U.SMOKE_CASES)
            self.assertEqual(scripts, U.BASH_SCRIPTS)
            self.assertEqual(timeout, U.SMOKE_TIMEOUT)

    def test_rollback_returns_to_the_previous_commit_and_skips_the_current_one(self) -> None:
        U.setup(self.home)
        first = run(self.work, "rev-parse", "HEAD")
        second = self.commit(agent_sync=PY_OK)
        U.update(self.home)
        self.assertEqual(
            U.rollback(self.home), {"status": "rolled_back", "commit": first, "bad": second}
        )
        self.assertEqual(U.update(self.home)["status"], "skipped_bad")
        with self.assertRaises(U.Failed):
            U.rollback(self.home)

    def test_a_run_killed_mid_smoke_never_leaves_an_unvalidated_commit_counted_as_good(
        self,
    ) -> None:
        U.setup(self.home)
        good = run(self.work, "rev-parse", "HEAD")
        new = self.commit(agent_sync=PY_OK)
        repo = self.base() / "repo"
        run(repo, "fetch", "-q", "origin", "main")
        run(repo, "checkout", "-q", "--detach", new)  # as if a run died during its smoke test
        result = U.update(self.home)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["previous"], good)
        self.assertEqual(U.status(self.home)["commit"], new)

    def test_a_missing_wrapper_is_written_on_the_next_run_even_when_nothing_changed(self) -> None:
        U.setup(self.home)
        (self.home / ".local/bin/recall").unlink()
        result = U.update(self.home)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["wrappers_written"], ["recall"])

    def test_setup_with_a_broken_main_leaves_current_alone(self) -> None:
        self.commit(agent_sync=PY_BROKEN)
        with self.assertRaises(U.Failed):
            U.setup(self.home)
        self.assertFalse((self.base() / "current").exists())

    def test_setup_re_run_with_a_broken_main_keeps_the_validated_commit(self) -> None:
        # A prior validated commit is a working host:  setup keeps it, still records the bad one, and
        # still points `current`, so only the never-validated case above is a failure.
        U.setup(self.home)
        good = run(self.work, "rev-parse", "HEAD")
        bad = self.commit(agent_sync=PY_BROKEN)
        result = U.setup(self.home)
        self.assertEqual(result["status"], "smoke_failed")
        self.assertEqual(U.status(self.home)["commit"], good)
        self.assertEqual(U.status(self.home)["bad"], bad)
        self.assertTrue(U.status(self.home)["current_is_checkout"])
        self.assertEqual(U.main(["setup", "--home", str(self.home)]), 1)  # smoke_failed

    def test_update_before_setup_is_refused(self) -> None:
        with self.assertRaises(U.Failed):
            U.update(self.home)
        self.assertEqual(U.main(["update", "--home", str(self.home)]), 2)

    def test_status_json_is_printable(self) -> None:
        U.setup(self.home)
        json.dumps(U.status(self.home))


class SmokeCaseTests(unittest.TestCase):
    def test_the_board_smoke_case_gives_the_same_exit_on_every_host(self) -> None:
        # The case used to be `simple-board whoami`, expecting exit 4 (missing credentials).  A host that has the seat
        # helper and a bearer for the smoke seat authenticates and exits 0, and the updater then refused every
        # release.  A usage error is decided by the argument parser before any credential or helper is consulted.
        case = next(
            c
            for c in U.SMOKE_CASES
            if c[1] == "scripts/simple-board" and c[0] != "simple_board_help"
        )
        self.assertEqual((case[2], case[3]), (["show"], 2))
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "AGENT_SEAT": "CODEX"}
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(HERE / "simple-board"), *case[2]],
                env=env,
                cwd=home,
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, case[3])


if __name__ == "__main__":
    unittest.main()
