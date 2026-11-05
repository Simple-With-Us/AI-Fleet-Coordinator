"""The build stamp:  the number, the commit, its time and the channel, all read from git.

Every test works in a throwaway repository, so no checkout, keychain or app is touched.
"""

import datetime
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_support as support

SCRIPT = Path(__file__).resolve().parent
UTC = datetime.timezone.utc


def seconds(*parts):
    return int(datetime.datetime(*parts, tzinfo=UTC).timestamp())


# Thu, Nov 5, 2026 at 3:30 PM UTC, which is 9:30 AM in America/Chicago.
COMMIT_SECONDS = seconds(2026, 11, 5, 15, 30)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class BuildIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.muster = self.repo / "muster"
        self.muster.mkdir(parents=True)
        (self.muster / "README.md").write_text("Muster\n")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.commit("first", COMMIT_SECONDS)

    def git(self, *arguments, at=COMMIT_SECONDS):
        env = dict(os.environ)
        stamp = "@%d +0000" % at
        env.update(
            {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "GIT_AUTHOR_DATE": stamp,
                "GIT_COMMITTER_DATE": stamp,
            }
        )
        return subprocess.run(
            ["git", "-C", str(self.repo)] + list(arguments),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    def commit(self, message, at):
        self.git("commit", "--quiet", "--allow-empty", "-m", message, at=at)

    def head(self):
        return self.git("rev-parse", "HEAD")

    def test_the_number_is_the_commit_time_in_utc(self):
        identity = support.build_identity(self.muster, "Release")
        self.assertEqual(identity["BUILD_NUMBER"], "202611051530")
        self.assertEqual(identity["COMMIT_TIME"], "2026-11-05T15:30:00Z")
        self.assertEqual(identity["COMMIT"], self.head())

    def test_a_later_merge_is_a_larger_number_across_the_november_fall_back(self):
        # 1:30 AM CDT and then 1:10 AM CST on Sun, Nov 1, 2026:  the clock on the wall goes
        # back, and the number must not.
        first, _ = support.build_number_for(seconds(2026, 11, 1, 6, 30))
        second, _ = support.build_number_for(seconds(2026, 11, 1, 7, 10))
        self.assertEqual(first, "202611010630")
        self.assertLess(int(first), int(second))

    def test_a_commit_on_origin_main_with_a_clean_tree_is_main(self):
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(support.build_identity(self.muster, "Release")["CHANNEL"], "main")

    def test_a_commit_that_is_not_on_main_is_a_lane(self):
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.commit("lane work", COMMIT_SECONDS + 60)
        self.assertEqual(support.build_identity(self.muster, "Release")["CHANNEL"], "lane")

    def test_uncommitted_changes_make_a_lane_even_on_main(self):
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        (self.muster / "README.md").write_text("Muster, edited\n")
        self.assertEqual(support.build_identity(self.muster, "Release")["CHANNEL"], "lane")

    def test_no_origin_main_is_a_lane(self):
        self.assertEqual(support.build_identity(self.muster, "Release")["CHANNEL"], "lane")

    def test_the_debug_configuration_is_development(self):
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(support.build_identity(self.muster, "Debug")["CHANNEL"], "development")

    def test_the_release_workflow_names_its_channel(self):
        identity = support.build_identity(self.muster, "Release", channel="release")
        self.assertEqual(identity["CHANNEL"], "release")
        with self.assertRaises(support.BuildFailure):
            support.build_identity(self.muster, "Release", channel="nightly")

    def test_an_override_replaces_the_number_and_a_bad_one_refuses(self):
        identity = support.build_identity(self.muster, "Release", override="202611051600")
        self.assertEqual(identity["BUILD_NUMBER"], "202611051600")
        with self.assertRaises(support.BuildFailure) as raised:
            support.build_identity(self.muster, "Release", override="000")
        self.assertEqual(raised.exception.status, 2)

    def test_a_folder_that_is_not_a_checkout_refuses(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        with self.assertRaises(support.BuildFailure) as raised:
            support.build_identity(outside, "Release")
        self.assertEqual(raised.exception.status, 2)

    def test_the_command_prints_one_line_per_key(self):
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPT)!r})\n"
            "import build_support as support\n"
            "sys.exit(support.main(['build_support', 'build-identity', 'Release']))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        keys = [line.split("=", 1)[0] for line in result.stdout.splitlines()]
        self.assertEqual(keys, ["BUILD_NUMBER", "COMMIT", "COMMIT_TIME", "CHANNEL"])


class GitFailureTests(unittest.TestCase):
    """A git that is missing or does not answer names its own exit status, like security."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (support.GIT, support.GIT_TIMEOUT)
        self.addCleanup(self.restore)

    def restore(self):
        support.GIT, support.GIT_TIMEOUT = self.saved

    def test_a_missing_git_is_127(self):
        support.GIT = str(Path(self.tmp.name) / "not-git")
        with self.assertRaises(support.BuildFailure) as raised:
            support.build_identity(self.tmp.name, "Release")
        self.assertEqual(raised.exception.status, 127)
        self.assertIn("git is missing", str(raised.exception))

    def test_a_git_that_does_not_answer_is_124(self):
        slow = Path(self.tmp.name) / "slow-git"
        slow.write_text("#!/bin/sh\nexec sleep 30\n")
        slow.chmod(0o755)
        support.GIT, support.GIT_TIMEOUT = str(slow), 0.5
        with self.assertRaises(support.BuildFailure) as raised:
            support.build_identity(self.tmp.name, "Release")
        self.assertEqual(raised.exception.status, 124)


class RefuseOlderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "Muster.app"
        (self.app / "Contents").mkdir(parents=True)

    def install(self, version):
        with (self.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleVersion": version}, stream)

    def test_an_older_build_is_refused_unless_allowed(self):
        self.install("202611051530")
        with self.assertRaises(support.BuildFailure) as raised:
            support.refuse_older(self.app, "202611041200", False)
        self.assertEqual(raised.exception.status, 4)
        self.assertIn("--allow-older", str(raised.exception))
        support.refuse_older(self.app, "202611041200", True)

    def test_a_newer_or_equal_build_passes(self):
        self.install("202611041200")
        support.refuse_older(self.app, "202611041200", False)
        support.refuse_older(self.app, "202611051530", False)

    def test_no_installed_copy_or_an_unreadable_number_passes(self):
        support.refuse_older(self.app, "202611041200", False)
        self.install("1001")
        support.refuse_older(self.app, "202611041200", False)


if __name__ == "__main__":
    unittest.main()
