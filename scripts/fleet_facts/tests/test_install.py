"""install.sh:  a plan by default, the stable copy carries what it needs, `python3 -I` runs it, nothing else is touched."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
INSTALL = SCRIPTS / "fleet_facts" / "install.sh"


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.stable = self.home / "apps" / "fleet-facts"
        self.link = self.home / ".local" / "bin" / "fleet-facts"

    def install(self, *args):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        return subprocess.run(["/bin/sh", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60)

    def isolated(self, program, *args):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-I", str(program), *args], capture_output=True, text=True, env=env, timeout=60)

    def tree(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_default_is_a_plan_and_changes_nothing(self):
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would:", proc.stdout)
        self.assertIn("plan only", proc.stdout)
        self.assertEqual(self.tree(), [])

    def test_apply_builds_the_stable_copy(self):
        proc = self.install("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for rel in ("fleet-facts", "mac-facts-publish", "fleet_facts/__init__.py", "fleet_facts/activity.py", "fleet_facts/cli.py",
                    "fleet_facts/github_prs.py", "fleet_facts/lanes.py", "fleet_facts/board.py", "fleet_facts/backup.py",
                    "fleet_facts/interfaces.py", "fleet_facts/safety.py", "fleet_facts/runner.py", "fleet_facts/gather.py",
                    "agent_sync/__init__.py", "agent_sync/textsafe.py", "agent_sync/secretscan.py"):
            self.assertTrue((self.stable / rel).is_file(), rel)
        self.assertEqual((self.stable / "agent_sync" / "__init__.py").read_text(), "")
        self.assertFalse((self.stable / "fleet_facts" / "tests").exists())            # no tests in the stable copy
        self.assertFalse((self.stable / "fleet_facts" / "check_fixtures.py").exists())   # nor the CI checker
        self.assertFalse((self.stable / "fleet_facts" / "README.md").exists())
        for rel in ("fleet-facts", "mac-facts-publish"):
            self.assertTrue((self.stable / rel).stat().st_mode & stat.S_IXUSR, rel)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(self.stable / "fleet-facts"))

    def test_the_stable_copy_runs_under_python_dash_I(self):
        self.install("--apply")
        for program in (self.stable / "fleet-facts", self.link):
            proc = self.isolated(program, "--help")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("gather", proc.stdout)
        # A real gather, which loads the F3 helpers by path from the stable copy's own agent_sync folder.
        proc = self.isolated(self.link, "gather", "--no-mac", "--part", "lanes", "--part", "backup", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["sources"]["lanes"]["status"], "absent")
        self.assertEqual(doc["redacted"], [])

    def test_the_stable_copy_does_not_import_the_checkout(self):
        self.install("--apply")
        proc = self.isolated(self.stable / "fleet-facts", "gather", "--no-mac", "--part", "lanes", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code = ("import sys; sys.path.insert(0, %r); import fleet_facts.safety as s, fleet_facts.cli; "
                "print(s._helper_dir()); print(fleet_facts.cli.__file__)" % str(self.stable))
        proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.split()
        for line in lines:
            self.assertTrue(str(Path(line).resolve()).startswith(str(self.stable.resolve())), lines)

    def test_idempotent_and_replaces_a_stale_copy(self):
        self.install("--apply")
        (self.stable / "fleet_facts" / "stale_module.py").write_text("# left over\n")
        again = self.install("--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("symlink already correct", again.stdout)
        self.assertFalse((self.stable / "fleet_facts" / "stale_module.py").exists())
        self.assertFalse((self.home / "apps" / "fleet-facts.prev").exists())

    def test_it_will_not_overwrite_a_regular_file_at_the_link(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("mine\n")
        proc = self.install("--apply")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a symlink", proc.stderr)
        self.assertEqual(self.link.read_text(), "mine\n")

    def test_it_touches_nothing_else_in_home(self):
        self.install("--apply")
        top = {p.relative_to(self.home).parts[0] for p in self.home.rglob("*")}
        self.assertEqual(top, {"apps", ".local"})
        self.assertFalse((self.home / "Library").exists())          # no LaunchAgent
        self.assertFalse((self.home / ".secrets").exists())

    def test_the_launcher_is_a_stub_until_f7(self):
        self.install("--apply")
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        proc = subprocess.run(["/bin/sh", str(self.stable / "mac-facts-publish")], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("F7", proc.stderr)

    def test_unknown_option_is_refused(self):
        proc = self.install("--force")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.tree(), [])

    def test_a_relative_home_is_refused(self):
        env = {"HOME": "relative/home", "PATH": "/usr/bin:/bin"}
        proc = subprocess.run(["/bin/sh", str(INSTALL), "--apply"], capture_output=True, text=True, env=env, timeout=60, cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
