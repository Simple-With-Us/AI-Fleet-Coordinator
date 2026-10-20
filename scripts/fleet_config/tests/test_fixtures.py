"""The committed test fixtures:  synthetic names,  and private directories.

Two rules are pinned here,  because both are easy to break by accident:

  * a committed fixture names no real organisation,  no real repository and no real
    path from a production machine.  The names are obviously fake,  so a reader can
    tell at a glance that nothing in a test is a live resource.
  * a directory a test creates is created with owner-only permissions,  and a symlink
    in its place is refused.  ``os.makedirs`` would follow the link and would use the
    permissive default mode bits.

Every test here works in a temporary directory,  and none of them writes to the real
home or to a real repository.
"""

from __future__ import annotations

import os
import stat
import unittest

from ._fake import FleetTestCase, make_private_dir

# The names a committed fixture must never carry.  A fixture that names a real
# organisation,  a real repository or a real path from a production machine leaks
# something about the fleet into the history of a public repository.
#
# The names are spelled in two pieces on purpose:  a fixture that spelled them whole
# would carry them itself,  and the scan below would then find this file.
FORBIDDEN_NAMES = (
    "Simple" + "-With-Us",
    "simple" + "withus",
    "AI" + "-Fleet-Coordinator",
    "Bot" + "Fleet",
    "Congress" + ".Trade",
    "congress" + "-trading",
    "/Users" + "/jay",
    "~" + "/Code",
)


class FixtureNameTests(unittest.TestCase):
    """No committed fixture names a real organisation,  repository or machine path."""

    def fixture_files(self):
        """Every committed file of this test package."""
        here = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(here)):
            if name.endswith(".py"):
                yield os.path.join(here, name)

    def test_no_fixture_names_a_real_organisation_or_repository(self):
        for path in self.fixture_files():
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for forbidden in FORBIDDEN_NAMES:
                with self.subTest(file=os.path.basename(path), name=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_the_fixture_registry_holds_an_obviously_fake_repository(self):
        from . import test_registry_check

        self.assertEqual(test_registry_check.FIXTURE_SEATS, ("CLAUDE", "CODEX"))


class PrivateDirectoryTests(FleetTestCase):
    """A directory a test owns is private,  and a symlink in its place is refused."""

    def test_the_helper_creates_an_owner_only_directory(self):
        path = str(self.root / "private")
        make_private_dir(path)
        info = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)

    def test_the_helper_refuses_a_symlinked_directory(self):
        real = str(self.root / "real")
        make_private_dir(real)
        link = str(self.root / "link")
        os.symlink(real, link)
        with self.assertRaises(ValueError):
            make_private_dir(link)

    def test_the_helper_refuses_a_path_that_is_not_a_directory(self):
        path = self.root / "a-file"
        path.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            make_private_dir(str(path))

    def test_the_test_home_is_private(self):
        info = os.lstat(str(self.home))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
