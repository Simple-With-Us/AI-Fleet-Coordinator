"""Tests for fleet-cli-git-update.py:  a local bare repo stands in for GitHub, with stub tools."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "fleet_cli_git_update", HERE / "fleet-cli-git-update.py"
)
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

PY_OK = "#!/usr/bin/python3\nimport sys\nsys.exit(0)\n"
PY_BROKEN = "#!/usr/bin/python3\nimport sys\nsys.exit(1)\n"
BOARD = "#!/usr/bin/python3\nimport sys\nsys.exit(4 if sys.argv[1:] == ['whoami'] else 0)\n"
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

    def commit(self, agent_sync: str) -> str:
        files = {
            "scripts/agent-sync": agent_sync,
            "scripts/linear-board/board": BOARD,
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
        self.assertEqual(result["reason"], "smoke_exit_mismatch_agent_sync_version")
        self.assertEqual(U.status(self.home)["commit"], good)
        self.assertEqual(U.status(self.home)["bad"], bad)
        self.assertEqual(U.update(self.home)["status"], "skipped_bad")
        self.commit(agent_sync=PY_BROKEN)
        self.assertEqual(U.main(["update", "--home", str(self.home)]), 1)
        fixed = self.commit(agent_sync=PY_OK)
        self.assertEqual(U.update(self.home)["status"], "updated")
        self.assertEqual(U.status(self.home)["commit"], fixed)
        self.assertIsNone(U.status(self.home)["bad"])

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

    def test_update_before_setup_is_refused(self) -> None:
        with self.assertRaises(U.Failed):
            U.update(self.home)
        self.assertEqual(U.main(["update", "--home", str(self.home)]), 2)

    def test_status_json_is_printable(self) -> None:
        U.setup(self.home)
        json.dumps(U.status(self.home))


if __name__ == "__main__":
    unittest.main()
