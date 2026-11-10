"""The fleet_lanes tests must never reach the live folders.

On Nov 8, 2026 a full run of this suite on the server deleted and rebuilt the host's live lane-tools folder and replaced its
apps/lane shim, because the fleet roots in the server's environment (FLEET_APPS_ROOT and friends) won over the fake home the install
tests built.  hermetic.py is the fix;  these tests prove it:  the process starts without the roots, a root set by the caller is
cleared when the package is imported, a root put back in the environment is ignored by an install, and a path outside the temp
tree is refused before anything is written.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes.tests import hermetic
from fleet_lanes.tests import test_install_tools as TI  # attribute use only: its tests are collected from their own module

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
DECOY = "/nonexistent/live-fleet-root"


class ScrubTests(unittest.TestCase):
    def test_the_test_process_starts_without_any_fleet_root(self) -> None:
        self.assertEqual(hermetic.leaked(), [], "a fleet root is set in the environment the tests run in")

    def test_importing_the_package_clears_roots_set_by_the_caller(self) -> None:
        # A fresh interpreter, started the way a server shell starts one:  every root names a "live" folder.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
        env.update({name: DECOY for name in hermetic.FLEET_ROOT_VARS})
        code = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import fleet_lanes.tests as t\n"
            "print(sorted(t.hermetic.CLEARED_AT_IMPORT))\n"
            "print(sorted(k for k in t.hermetic.FLEET_ROOT_VARS if k in os.environ))\n"
        )
        run = subprocess.run(
            [sys.executable, "-B", "-c", code, str(SCRIPTS_DIR)], env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        cleared, remaining = run.stdout.strip().splitlines()
        self.assertEqual(cleared, str(sorted(hermetic.FLEET_ROOT_VARS)), "the roots were not set before the import")
        self.assertEqual(remaining, "[]")

    def test_without_fleet_roots_hides_them_and_restores_the_callers(self) -> None:
        with mock.patch.dict(os.environ, {"FLEET_APPS_ROOT": DECOY, "FLEET_LANES_ROOT": DECOY}):
            with hermetic.without_fleet_roots():
                self.assertEqual(hermetic.leaked(), [])
            self.assertEqual(os.environ["FLEET_APPS_ROOT"], DECOY)
            self.assertEqual(os.environ["FLEET_LANES_ROOT"], DECOY)
        self.assertEqual(hermetic.leaked(), [])

    def test_clear_names_what_it_removed_and_leaves_the_rest(self) -> None:
        env = {"FLEET_CODE_ROOT": DECOY, "AGENT_SEAT": "CLAUDE", "HOME": "/h"}
        self.assertEqual(hermetic.clear(env), ["FLEET_CODE_ROOT"])
        self.assertEqual(env, {"AGENT_SEAT": "CLAUDE", "HOME": "/h"})
        self.assertEqual(hermetic.clear(env), [])


class AssertInsideTests(unittest.TestCase):
    def test_a_path_in_the_tree_passes_and_one_outside_or_none_is_judged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hermetic-test-") as raw, tempfile.TemporaryDirectory(prefix="elsewhere-") as other:
            root = Path(os.path.realpath(raw))
            (root / "a").mkdir()
            hermetic.assert_inside(root, root, root / "a", root / "not-yet" / "made", None)
            with self.assertRaises(AssertionError) as caught:
                hermetic.assert_inside(root, root / "a", other)
            self.assertIn("outside the test's temp tree", str(caught.exception))

    def test_a_link_inside_the_tree_that_points_outside_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hermetic-test-") as raw, tempfile.TemporaryDirectory(prefix="elsewhere-") as other:
            root = Path(os.path.realpath(raw))
            os.symlink(other, root / "apps")
            with self.assertRaises(AssertionError):
                hermetic.assert_inside(root, root / "apps" / "lane-tools")

    def test_a_sibling_with_the_same_prefix_is_not_inside(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hermetic-test-") as raw:
            root = Path(os.path.realpath(raw))
            with self.assertRaises(AssertionError):
                hermetic.assert_inside(root, str(root) + "-sibling")


class InstallIgnoresRootsTests(TI.World):
    """The install tests build a fake home;  a root in the environment must not move the install out of it."""

    def decoys(self) -> dict[str, str]:
        live = self.tmp / "live"
        return {
            "FLEET_APPS_ROOT": str(live / "apps"),
            "FLEET_CODE_ROOT": str(live / "Code"),
            "FLEET_LANES_ROOT": str(live / "apps" / "lanes"),
            "FLEET_SERVER_VOLUME": str(live),
            "FLEET_APPS_JSON": str(live / "fleet-apps.json"),
        }

    def test_the_old_way_of_building_paths_follows_the_environment_and_the_guard_refuses_it(self) -> None:
        # What World.setUp did before hermetic.py, with the roots in the environment:  the "fake home" install pointed at them.
        with mock.patch.dict(os.environ, self.decoys()):
            leaky = TI.T.make_paths(str(self.home))
        self.assertEqual(leaky.apps_dir, self.decoys()["FLEET_APPS_ROOT"], "the hazard this guards against is gone from install_tools")
        with self.assertRaises(AssertionError):
            hermetic.assert_inside(self.home, leaky.stable, leaky.lane_shim)

    def test_an_apply_with_roots_in_the_environment_installs_under_the_fake_home_only(self) -> None:
        live = self.tmp / "live"
        live.mkdir()
        with mock.patch.dict(os.environ, self.decoys()):
            rc, out, err = TI.run_main(
                "apply",
                "--home",
                str(self.home),
                "--source",
                self.source.scripts_dir,
                "--sha",
                TI.SHA,
                "--timeout",
                str(TI.TIMEOUT),
                "--no-minimal-path",
                "--no-verify",
                "tools",
            )
            # Still inside the patch:  the roots really were in the environment while the install ran.
            self.assertEqual(os.environ["FLEET_APPS_ROOT"], self.decoys()["FLEET_APPS_ROOT"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((self.home / "apps" / "lane-tools" / "VERSION").is_file(), "nothing was installed under the fake home")
        self.assertEqual(os.listdir(live), [], "the install wrote into a folder named only by the environment")

    def test_the_fixture_itself_starts_with_paths_inside_its_temp_tree(self) -> None:
        hermetic.assert_inside(self.tmp, self.paths.home, self.paths.stable, self.paths.apps_dir, self.paths.lane_shim)
        self.assertEqual(hermetic.leaked(), [])


if __name__ == "__main__":
    unittest.main()
