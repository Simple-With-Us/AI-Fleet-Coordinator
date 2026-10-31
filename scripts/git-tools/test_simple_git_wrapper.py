#!/usr/bin/env python3
"""Tests for the simple-git entry wrapper (scripts/simple-git when installed).

Covers:  the installed basename survives (including through a symlink and through the Python-version
re-exec, via SIMPLE_GIT_ARGV0);  the 3.9 interpreter fallback;  the host seat-helper trust protocol
(absent = optional, present-but-untrustworthy = refusal, trusted = runs with the command key);  a missing
implementation file is a clean error.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
WRAPPER = HERE.parent / "simple-git"  # the installed entry point (scripts/simple-git in the repo layout)

FAKE_IMPL = (
    "import os, sys\n"
    "print('ARGV0=' + os.path.basename(sys.argv[0]))\n"
    "print('ENV0=' + os.environ.get('SIMPLE_GIT_ARGV0', '-'))\n"
)

CLEAN_KEYS = (
    "SIMPLE_TOOLS_BEARER", "SIMPLE_GIT_REMOTE", "SIMPLE_GIT_NATIVE_ECHO", "SIMPLE_GIT_ARGV0",
    "AGENT_SEAT", "AGENT_TAG", "AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT",
)


def make_tree(with_impl=True):
    tmp = Path(tempfile.mkdtemp(prefix="git-wrapper-test-"))
    (tmp / "scripts" / "git-tools").mkdir(parents=True)
    shutil.copy(WRAPPER, tmp / "scripts" / "simple-git")
    if with_impl:
        (tmp / "scripts" / "git-tools" / "git").write_text(FAKE_IMPL)
    (tmp / "home").mkdir()
    return tmp


def run_wrapper(tmp, script, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in CLEAN_KEYS}
    env["HOME"] = str(tmp / "home")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )


class TestInstalledName(unittest.TestCase):
    def test_plain_invocation_carries_the_installed_name(self):
        tmp = make_tree()
        try:
            result = run_wrapper(tmp, tmp / "scripts" / "simple-git")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ARGV0=simple-git", result.stdout)
            self.assertIn("ENV0=simple-git", result.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_symlink_name_is_the_name_that_survives(self):
        tmp = make_tree()
        try:
            (tmp / "bin").mkdir()
            link = tmp / "bin" / "mygit"
            link.symlink_to(tmp / "scripts" / "simple-git")
            result = run_wrapper(tmp, link)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ARGV0=mygit", result.stdout)
            self.assertIn("ENV0=mygit", result.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_version_reexec_preserves_the_installed_name(self):
        # Simulate Python 3.9:  the wrapper must re-exec a newer interpreter AND hand the installed name
        # across via SIMPLE_GIT_ARGV0 (the re-exec replaces argv[0] with the real script path).
        source = WRAPPER.read_text()
        sentinel = {}
        def fake_execv(path, argv):
            sentinel["path"] = path
            sentinel["argv"] = argv
            raise RuntimeError("execv recorded")
        env = {k: v for k, v in os.environ.items() if k not in CLEAN_KEYS}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            sys, "version_info", (3, 9, 18)
        ), mock.patch.object(os, "access", lambda p, m: p == "/usr/local/bin/python3"), mock.patch.object(
            os, "execv", fake_execv
        ), mock.patch.object(
            sys, "argv", ["/usr/local/bin/simple-git", "list"]
        ):
            try:
                exec(compile(source, str(WRAPPER), "exec"), {"__name__": "__main__", "__file__": str(WRAPPER)})
            except RuntimeError:
                pass
        self.assertEqual(sentinel.get("path"), "/usr/local/bin/python3")
        self.assertEqual(os.environ.get("SIMPLE_GIT_ARGV0"), "simple-git")

    def test_no_newer_interpreter_is_a_clean_error(self):
        source = WRAPPER.read_text()
        with mock.patch.object(sys, "version_info", (3, 9, 18)), mock.patch.object(
            os, "access", lambda p, m: False
        ), mock.patch.object(sys, "argv", ["simple-git"]):
            try:
                exec(compile(source, str(WRAPPER), "exec"), {"__name__": "__main__", "__file__": str(WRAPPER)})
                self.fail("expected SystemExit")
            except SystemExit as exc:
                self.assertEqual(exc.code, 1)


class TestHelperTrust(unittest.TestCase):
    """The helper path comes from the account database (pwd), never $HOME, so these tests stage the file in
    the real sandbox home and remove it again.  They skip if a real helper is installed there."""

    def setUp(self):
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.helper_dir = home / ".config" / "fleet-cli"
        self.helper = self.helper_dir / "seat-remote-defaults.py"
        if self.helper.exists():
            self.skipTest("a real seat helper is installed; refusing to touch it")
        self._made = []

    def tearDown(self):
        try:
            self.helper.unlink()
        except FileNotFoundError:
            pass
        for directory in sorted(self._made, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

    def _install_helper(self, tmp, body, mode):
        for directory in (self.helper_dir.parent, self.helper_dir):
            if not directory.exists():
                self._made.append(directory)
        self.helper_dir.mkdir(parents=True, exist_ok=True)
        self.helper.write_text(body)
        self.helper.chmod(mode)
        return self.helper

    def test_absent_helper_is_optional(self):
        tmp = make_tree()
        try:
            result = run_wrapper(tmp, tmp / "scripts" / "simple-git")
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_present_but_untrustworthy_helper_is_a_refusal(self):
        tmp = make_tree()
        try:
            self._install_helper(tmp, "pass\n", 0o664)  # group-writable: never trusted
            result = run_wrapper(tmp, tmp / "scripts" / "simple-git")
            self.assertEqual(result.returncode, 3)
            self.assertIn("could not be trusted", result.stderr)
            self.assertNotIn("ARGV0", result.stdout)  # the tool never ran
        finally:
            shutil.rmtree(tmp)

    def test_trusted_helper_runs_with_the_command_key(self):
        tmp = make_tree()
        try:
            marker = tmp / "marker"
            self._install_helper(
                tmp,
                "import os\nopen(os.environ['MARKER'], 'w').write(fleet_command)\n",
                0o600,
            )
            result = run_wrapper(tmp, tmp / "scripts" / "simple-git", {"MARKER": str(marker)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(), "simple-git")
        finally:
            shutil.rmtree(tmp)

    def test_failing_helper_is_a_refusal(self):
        tmp = make_tree()
        try:
            self._install_helper(tmp, "raise ValueError('boom')\n", 0o600)
            result = run_wrapper(tmp, tmp / "scripts" / "simple-git")
            self.assertEqual(result.returncode, 3)
            self.assertIn("helper failed", result.stderr)
        finally:
            shutil.rmtree(tmp)


class TestMissingImplementation(unittest.TestCase):
    def test_missing_impl_is_a_clean_error(self):
        tmp = make_tree(with_impl=False)
        try:
            result = run_wrapper(tmp, tmp / "scripts" / "simple-git")
            self.assertEqual(result.returncode, 1)
            self.assertIn("is missing", result.stderr)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
