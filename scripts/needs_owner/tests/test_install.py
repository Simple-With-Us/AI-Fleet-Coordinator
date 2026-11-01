"""install.sh and the packaging rule:  the stable copy runs with `python3 -I` and nothing but what it was given."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .world import SCRIPTS, World

INSTALL = SCRIPTS / "needs_owner" / "install.sh"


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="nowner-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dest = self.home / "apps" / "needs-owner"
        self.link = self.home / ".local" / "bin" / "needs-owner"

    def install(self, *args: str) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % Path(sys.executable).parent}
        return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=120)

    def isolated(self, *args: str, env: dict | None = None, cwd: str | None = None, managed: str | None = None) -> subprocess.CompletedProcess:
        # a stand-in `simple-board whoami`:  the seat is proven by bearer verification, never by a variable
        stub = self.home.parent / ("stubbin-" + self.home.name)
        stub.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, stub, True)
        (stub / "simple-board").write_text('#!/bin/sh\necho "seat:        CLAUDE (bearer-verified)"\n')
        (stub / "simple-board").chmod(0o755)
        base = {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % stub,
                "SIMPLE_BOARD_BEARER_LOCATOR": "/fleet-seats:CLAUDE_SEAT_BEARER"}
        base.update(env or {})
        base.pop("AGENT_SEAT", None)
        # the managed-board seam is import-only, so the isolated run sets it in-process before the entry runs
        entry = [sys.executable, "-I", str(self.dest / "needs-owner"), *args]
        if args and not args[0].startswith("--"):  # a verb reaches the seat probe;  --help/--version never do
            entry = None
        boot = ("import sys, runpy; sys.path.insert(0, %r); import needs_owner.seat as S; S.MANAGED_BIN_DIR = %r; "
                "sys.argv = [%r] + sys.argv[1:]; runpy.run_path(%r, run_name='__main__')"
                % (str(self.dest), str(managed or stub), str(self.dest / "needs-owner"), str(self.dest / "needs-owner")))
        return subprocess.run(entry or [sys.executable, "-I", "-c", boot, *args], capture_output=True, text=True,
                              env=base, cwd=cwd or str(self.home), timeout=120)

    def test_the_plan_changes_nothing(self) -> None:
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would: create", r.stdout)
        self.assertIn("plan only", r.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_apply_installs_a_stable_copy_that_runs_in_isolated_mode(self) -> None:
        r = self.install("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("installed:", r.stdout)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(self.dest / "needs-owner"))
        self.assertTrue(os.access(self.dest / "needs-owner", os.X_OK))
        self.assertEqual(sorted(p.name for p in (self.dest / "agent_sync").iterdir() if p.suffix == ".py"),
                         ["identity.py", "secretscan.py", "textsafe.py"])
        self.assertFalse((self.dest / "needs_owner" / "tests").exists())
        self.assertTrue((self.dest / "needs_owner" / "cli.py").exists())
        # the packaging rule:  from another directory, isolated, with nothing of the checkout on the path
        for argv in (["--help"], ["--version"], ["file", "--help"], ["list", "--help"]):
            ran = self.isolated(*argv)
            self.assertEqual(ran.returncode, 0, (argv, ran.stderr))
        self.assertIn("needs-owner", self.isolated("--version").stdout)

    def test_the_stable_copy_carries_the_knob_reader_the_notifier_needs(self) -> None:
        self.assertEqual(self.install("--apply").returncode, 0)
        source = sorted(p.name for p in (SCRIPTS / "fleet_knobs").glob("*.py"))
        self.assertEqual(sorted(p.name for p in (self.dest / "fleet_knobs").iterdir() if p.suffix == ".py"), source)
        self.assertFalse((self.dest / "fleet_knobs" / "tests").exists())
        self.assertIn("knobs.py", source)
        # Under python3 -I, with nothing of the checkout on the path, `notify` finds fleet_knobs next to the package.
        # A refused Infisical environment stops it before any fetch, so no real CLI runs:  exit 1 and the reason.
        ran = self.isolated("notify", "--dry-run", env={"INFISICAL_ENV": "staging"})
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn("the knobs cannot be read", ran.stderr)
        self.assertNotIn("fleet_knobs is missing", ran.stderr)
        self.assertNotIn("Traceback", ran.stderr)

    def test_a_copy_without_the_knob_reader_still_files_but_cannot_notify(self) -> None:
        self.assertEqual(self.install("--apply").returncode, 0)
        shutil.rmtree(self.dest / "fleet_knobs")
        self.assertEqual(self.isolated("--version").returncode, 0)
        ran = self.isolated("notify", "--dry-run", env={})
        self.assertEqual(ran.returncode, 1)
        self.assertIn("fleet_knobs is missing", ran.stderr)
        self.assertIn("install.sh --apply", ran.stderr)

    def test_a_stable_copy_missing_an_f3_file_does_not_pass_the_proof(self) -> None:
        """`--help` and `--version` are the install proof, so they must load the three F3 files:  a copy that lacks one
        fails them (it used to exit inside argparse, before the package reached its helpers)."""
        self.assertEqual(self.install("--apply").returncode, 0)
        for name in ("textsafe.py", "secretscan.py", "identity.py"):
            moved = self.dest / "agent_sync" / (name + ".gone")
            (self.dest / "agent_sync" / name).rename(moved)
            for argv in (["--help"], ["--version"], ["list", "--help"]):
                ran = self.isolated(*argv)
                self.assertEqual(ran.returncode, 1, (name, argv, ran.stdout))
                self.assertIn("incomplete", ran.stderr, (name, argv))
                self.assertIn(name, ran.stderr, (name, argv))
            moved.rename(self.dest / "agent_sync" / name)
        self.assertEqual(self.isolated("--help").returncode, 0)

    def test_a_damaged_f3_file_is_reported_not_a_traceback(self) -> None:
        self.assertEqual(self.install("--apply").returncode, 0)
        (self.dest / "agent_sync" / "secretscan.py").write_text("def broken(:\n")
        ran = self.isolated("--version")
        self.assertEqual(ran.returncode, 1)
        self.assertNotIn("Traceback", ran.stderr)
        self.assertIn("needs-owner:", ran.stderr)

    def test_the_stable_copy_does_real_work(self) -> None:
        """A dry run through the stable copy reaches the F3 helpers it loaded by path (identity, secretscan, textsafe)."""
        self.assertEqual(self.install("--apply").returncode, 0)
        world = World(self)
        env = world.env()
        env["HOME"] = str(self.home)
        ran = self.isolated("file", "--by", "CLAUDE", "--app", "AFC", "--question", "Is the copy complete?", "--option", "Yes",
                            "--option", "No", "--dry-run", "--json", env=env, managed=str(world.bin))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(json.loads(ran.stdout)["dry_run"])
        refused = self.isolated("file", "--by", "CLAUDE", "--app", "AFC", "--question", "Key lin_api_" + "Zx9Yw8Vv7U" * 3 + "?",
                                "--option", "Yes", "--option", "No", env=env, managed=str(world.bin))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("secret", refused.stderr)

    def test_a_second_apply_has_nothing_to_do_and_keeps_state(self) -> None:
        self.assertEqual(self.install("--apply").returncode, 0)
        (self.dest / "state").mkdir()
        (self.dest / "state" / "pending-CLAUDE.json").write_text("{}")
        again = self.install("--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("nothing to do", again.stdout)
        self.assertTrue((self.dest / "state" / "pending-CLAUDE.json").exists())

    def test_an_update_replaces_a_changed_file_and_removes_a_stale_one(self) -> None:
        self.assertEqual(self.install("--apply").returncode, 0)
        (self.dest / "needs_owner" / "old_module.py").write_text("# gone from the source\n")
        (self.dest / "needs_owner" / "text.py").write_text("# an old version\n")
        (self.dest / "state").mkdir()
        plan = self.install()
        self.assertIn("update %s" % (self.dest / "needs_owner" / "text.py"), plan.stdout.replace("would: ", ""))
        self.assertIn("remove", plan.stdout)
        self.assertTrue((self.dest / "needs_owner" / "old_module.py").exists(), "a plan removes nothing")
        applied = self.install("--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertFalse((self.dest / "needs_owner" / "old_module.py").exists())
        self.assertGreater((self.dest / "needs_owner" / "text.py").stat().st_size, 100)
        self.assertTrue((self.dest / "state").is_dir())
        self.assertEqual(self.isolated("--version").returncode, 0)

    def test_a_regular_file_where_the_link_goes_is_not_overwritten(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.write_text("#!/bin/sh\n")
        r = self.install("--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a symbolic link", r.stderr)
        self.assertEqual(self.link.read_text(), "#!/bin/sh\n")

    def test_unknown_option(self) -> None:
        self.assertEqual(self.install("--force").returncode, 2)


if __name__ == "__main__":
    unittest.main()
