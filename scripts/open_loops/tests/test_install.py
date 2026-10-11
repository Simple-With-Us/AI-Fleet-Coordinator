"""install.sh:  a plan by default, the stable copy carries what it needs, `python3 -I` runs it, nothing else is touched."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2]
INSTALL = SCRIPTS / "open_loops" / "install.sh"


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.stable = self.home / "apps" / "open-loops"
        self.link = self.home / ".local" / "bin" / "open-loops"

    def install(self, *args):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
        return subprocess.run(["/bin/sh", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60)

    def isolated(self, program, *args):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C"}
        return subprocess.run([sys.executable, "-I", str(program), *args], capture_output=True, text=True, env=env, timeout=60)

    def tree(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_default_is_a_plan_and_changes_nothing(self):
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would:", proc.stdout)
        self.assertIn("plan only", proc.stdout)
        self.assertEqual(self.tree(), [])

    def test_apply_builds_the_stable_copy_with_everything_it_imports(self):
        proc = self.install("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for rel in ("open-loops", "open_loops/__init__.py", "open_loops/cli.py", "open_loops/facts.py", "open_loops/send.py",
                    "open_loops/lint.py", "open_loops/render.py", "open_loops/macfacts.py", "open_loops/claims.py",
                    "open_loops/zulip_topics.py", "open_loops/central.py", "open_loops/urls.py", "open_loops/views.py",
                    "open_loops/sections.py", "open_loops/files.py",
                    "fleet_facts/__init__.py", "fleet_facts/activity.py", "fleet_facts/gather.py", "fleet_facts/safety.py",
                    "fleet_facts/runner.py", "fleet_facts/board.py", "fleet_facts/github_prs.py",
                    "agent_sync/__init__.py", "agent_sync/textsafe.py", "agent_sync/secretscan.py", "agent_sync/textfmt.py"):
            self.assertTrue((self.stable / rel).is_file(), rel)
        self.assertEqual(sorted(p.name for p in (self.stable / "agent_sync").iterdir()),
                         ["__init__.py", "secretscan.py", "textfmt.py", "textsafe.py"])
        self.assertEqual((self.stable / "agent_sync" / "__init__.py").read_text(), "")
        for name in ("open_loops", "fleet_facts"):
            self.assertFalse((self.stable / name / "tests").exists(), name)
            self.assertFalse((self.stable / name / "README.md").exists(), name)
            self.assertFalse((self.stable / name / "install.sh").exists() and name == "fleet_facts", name)
        self.assertFalse((self.stable / "fleet_facts" / "check_fixtures.py").exists())
        self.assertTrue((self.stable / "open-loops").stat().st_mode & stat.S_IXUSR)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(self.stable / "open-loops"))

    def test_the_stable_copy_runs_under_python_dash_I_from_a_temp_home(self):
        self.install("--apply")
        for program in (self.stable / "open-loops", self.link):
            proc = self.isolated(program, "--help")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for word in ("gather", "render", "send"):
                self.assertIn(word, proc.stdout)
        # render loads the F3 renderer by path from the stable copy's own agent_sync folder
        proc = self.isolated(self.link, "render", "--facts", str(self.facts_file()), "--markdown")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("## 3. Work Not on GitHub Yet", proc.stdout)

    def facts_file(self) -> Path:
        """Facts built in-process from a scripted world (a real gather would reach for the network)."""
        from open_loops.facts import build_facts
        from .builders import NOW
        from .scenes import standard_world
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % bin_dir}):
            world = standard_world(self.home, bin_dir)
            facts = build_facts(run=world.runner(), now=NOW, sleep=lambda s: None)
        path = Path(self.tmp.name) / "facts.json"
        path.write_text(json.dumps(facts))
        path.chmod(0o600)
        return path

    def test_the_stable_copy_does_not_import_the_checkout(self):
        self.install("--apply")
        code = ("import sys; sys.path.insert(0, %r); import open_loops.cli, fleet_facts.safety as s; "
                "print(open_loops.cli.__file__); print(s._helper_dir()); print(s.textfmt().__file__); print(s.secretscan().scan('x'))" % str(self.stable))
        proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.split("\n")[:3]
        self.assertEqual(len(lines), 3)
        self.assertEqual(proc.stdout.split("\n")[3], "None")
        for line in lines:
            self.assertTrue(str(Path(line).resolve()).startswith(str(self.stable.resolve())), lines)

    def test_the_entry_script_runs_isolated_by_its_own_first_line(self):
        self.install("--apply")
        first = (self.stable / "open-loops").read_text().splitlines()[0]
        if first.startswith("#!/usr/bin/env"):
            self.skipTest("no Python 3.11 on this machine")
        self.assertRegex(first, r"^#!/\S+python\S* -I$")
        proc = subprocess.run([str(self.link), "--version"], capture_output=True, text=True, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("open-loops", proc.stdout)

    def test_c2_id_is_written_kept_across_a_reinstall_and_replaced(self):
        self.assertEqual(self.install("--apply").returncode, 0)
        self.assertFalse((self.stable / "c2-id").exists())
        proc = self.install("--apply")
        self.assertIn("no C2 id is set", proc.stdout)
        self.install("--apply", "--c2-id", "FFX-123")
        self.assertEqual((self.stable / "c2-id").read_text(), "FFX-123\n")
        self.install("--apply")                                              # no flag:  the id already there is kept
        self.assertEqual((self.stable / "c2-id").read_text(), "FFX-123\n")
        self.install("--apply", "--c2-id", "FFX-456")
        self.assertEqual((self.stable / "c2-id").read_text(), "FFX-456\n")

    def test_a_c2_id_that_is_not_an_issue_id_is_refused_before_anything_changes(self):
        for bad in ("FFX", "123", "ffx-1", "FFX-1x", "FFX-", "-1", "FFX-1\nx", "FFX-12345678", "TOOLONGKEY-1", ""):
            proc = self.install("--apply", "--c2-id", bad)
            self.assertEqual(proc.returncode, 2, repr(bad))
        self.assertEqual(self.tree(), [])
        self.assertEqual(self.install("--apply", "--c2-id").returncode, 2)

    def test_the_plan_names_the_c2_id_but_writes_nothing(self):
        proc = self.install("--c2-id", "FFX-123")
        self.assertIn("would: write the C2 id FFX-123", proc.stdout)
        self.assertEqual(self.tree(), [])

    def test_idempotent_and_replaces_a_stale_copy(self):
        self.install("--apply")
        (self.stable / "open_loops" / "stale_module.py").write_text("# left over\n")
        again = self.install("--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("symlink already correct", again.stdout)
        self.assertFalse((self.stable / "open_loops" / "stale_module.py").exists())
        self.assertFalse((self.home / "apps" / "open-loops.prev").exists())

    def test_it_will_not_overwrite_a_regular_file_at_the_link(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("mine\n")
        proc = self.install("--apply")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a symlink", proc.stderr)
        self.assertEqual(self.link.read_text(), "mine\n")

    def test_it_touches_nothing_else_in_home(self):
        self.install("--apply", "--c2-id", "FFX-1")
        top = {p.relative_to(self.home).parts[0] for p in self.home.rglob("*")}
        self.assertEqual(top, {"apps", ".local"})
        self.assertFalse((self.home / "Library").exists())          # no LaunchAgent
        self.assertFalse((self.home / ".secrets").exists())

    def test_unknown_option_and_relative_home_are_refused(self):
        proc = self.install("--force")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.tree(), [])
        env = {"HOME": "relative/home", "PATH": "/usr/bin:/bin"}
        proc = subprocess.run(["/bin/sh", str(INSTALL), "--apply"], capture_output=True, text=True, env=env, timeout=60, cwd=self.tmp.name)
        self.assertEqual(proc.returncode, 1)

    def test_help_prints_the_header(self):
        proc = self.install("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Stable copy:", proc.stdout)
        self.assertIn("c2-id", proc.stdout)


if __name__ == "__main__":
    unittest.main()
