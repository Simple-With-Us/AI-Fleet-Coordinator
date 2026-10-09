"""install.sh: dry run changes nothing, a real run links and makes ~/.agent-sync 700, reruns are idempotent.

Always runs with HOME pointed at a temp directory, against a fake checkout holding only
scripts/agent-sync, so the real home, LaunchAgents and ~/.secrets are never involved."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
ENTRY = Path(__file__).resolve().parents[2] / "agent-sync"


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sync-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.checkout = self.tmp / "checkout"
        (self.checkout / "scripts").mkdir(parents=True)
        shutil.copy(ENTRY, self.checkout / "scripts" / "agent-sync")
        os.chmod(self.checkout / "scripts" / "agent-sync", 0o755)

    def run_install(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=30,
                              cwd=str(cwd) if cwd else None)

    def home_listing(self) -> list[str]:
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_the_entry_point_and_installer_are_executable(self) -> None:
        self.assertTrue(os.access(ENTRY, os.X_OK))
        self.assertTrue(os.access(INSTALL, os.X_OK))
        self.assertTrue(ENTRY.read_text().startswith("#!/usr/bin/env python3"))

    def test_the_default_checkout_is_the_managed_runtime_checkout_never_the_human_tree(self) -> None:
        text = INSTALL.read_text()
        self.assertIn('DEFAULT_CHECKOUT="/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime"', text)
        self.assertNotIn('DEFAULT_CHECKOUT="/Users/jay/Code', text)

    def test_the_runtime_sync_script_is_executable_and_parses(self) -> None:
        script = Path(__file__).resolve().parents[2] / "agent-sync-runtime-sync.sh"
        self.assertTrue(os.access(script, os.X_OK))
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dry_run_changes_nothing(self) -> None:
        result = self.run_install("--dry-run", str(self.checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.home_listing(), [])
        self.assertIn("would: link", result.stdout)
        self.assertIn("would: create", result.stdout)
        self.assertIn("nothing was changed", result.stdout)

    def test_real_run_links_and_creates_a_private_state_dir(self) -> None:
        result = self.run_install(str(self.checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.home / ".local" / "bin" / "agent-sync"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.checkout / "scripts" / "agent-sync"))
        self.assertEqual(stat.S_IMODE(os.stat(self.home / ".agent-sync").st_mode), 0o700)
        self.assertEqual(self.home_listing(), [".agent-sync", ".local", ".local/bin", ".local/bin/agent-sync"])

    def test_a_relative_checkout_path_still_makes_an_absolute_link_that_works(self) -> None:
        link = self.home / ".local" / "bin" / "agent-sync"
        wanted = os.path.realpath(self.checkout / "scripts" / "agent-sync")
        for argument, cwd in (("checkout", self.tmp), (".", self.checkout), ("../checkout", self.home)):
            with self.subTest(argument=argument):
                if link.is_symlink():
                    link.unlink()
                result = self.run_install(argument, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                target = os.readlink(link)
                self.assertTrue(os.path.isabs(target), target)
                self.assertTrue(os.path.exists(link), "the link dangles: " + target)  # exists() follows the link
                self.assertEqual(os.path.realpath(link), wanted)

    def test_a_checkout_that_is_a_file_or_missing_is_refused(self) -> None:
        (self.tmp / "afile").write_text("x")
        for argument in ("afile", "no/such/dir"):
            with self.subTest(argument=argument):
                result = self.run_install(argument, cwd=self.tmp)
                self.assertEqual(result.returncode, 1)
                self.assertIn("not found", result.stderr)
        self.assertEqual(self.home_listing(), [])

    def test_the_symlink_runs_through_its_own_real_path(self) -> None:
        self.run_install(str(Path(__file__).resolve().parents[3]))  # the real checkout this file lives in
        link = self.home / ".local" / "bin" / "agent-sync"
        done = subprocess.run([str(link), "--version"], capture_output=True, text=True, timeout=30,
                              env={"HOME": str(self.home), "PATH": os.environ.get("PATH", "")})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("agent-sync", done.stdout)

    def test_second_run_is_idempotent_and_repairs_modes(self) -> None:
        self.run_install(str(self.checkout))
        os.chmod(self.home / ".agent-sync", 0o755)
        again = self.run_install(str(self.checkout))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("symlink already correct", again.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(self.home / ".agent-sync").st_mode), 0o700)

    def test_a_stale_link_is_repointed_but_a_regular_file_is_never_overwritten(self) -> None:
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "agent-sync").symlink_to(self.tmp / "old-place")
        self.assertEqual(self.run_install(str(self.checkout)).returncode, 0)
        self.assertEqual(os.readlink(bin_dir / "agent-sync"), str(self.checkout / "scripts" / "agent-sync"))
        (bin_dir / "agent-sync").unlink()
        (bin_dir / "agent-sync").write_text("mine")
        refused = self.run_install(str(self.checkout))
        self.assertEqual(refused.returncode, 1)
        self.assertEqual((bin_dir / "agent-sync").read_text(), "mine")

    def test_missing_checkout_file_fails_without_touching_home(self) -> None:
        result = self.run_install(str(self.tmp / "nowhere"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.home_listing(), [])

    def test_unknown_option_is_a_usage_error(self) -> None:
        self.assertEqual(self.run_install("--frobnicate").returncode, 2)


if __name__ == "__main__":
    unittest.main()
