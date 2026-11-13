"""The shared consumer bootstrap must not borrow credentials or follow unsafe files."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from simple_tools.platform_letter_bootstrap import TOKEN_ENV, bind, report
from unittest import mock


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {"HOME": str(self.home)}
        self.directory = self.home / ".config/platform-letter"
        self.directory.mkdir(parents=True, mode=0o700)
        self.token = self.directory / "infisical-registry.token"
        self.value = "st." + "synthetic-bootstrap-" * 3

    def write(self, value=None, mode=0o600):
        self.token.write_text(self.value if value is None else value)
        self.token.chmod(mode)

    def test_private_consumer_token_is_bound_only_as_bootstrap(self):
        self.write()
        self.assertEqual(bind(self.env), "bootstrap-bound")
        self.assertEqual(self.env, {"HOME": str(self.home), TOKEN_ENV: self.value})

    def test_explicit_binding_is_never_replaced(self):
        self.write()
        for explicit in ("", "invalid", self.value):
            self.env[TOKEN_ENV] = explicit
            self.assertEqual(bind(self.env), "explicit-binding")
            self.assertEqual(self.env[TOKEN_ENV], explicit)

    def test_missing_token_does_not_create_a_store(self):
        self.assertEqual(bind(self.env), "bootstrap-missing")
        self.assertEqual(self.env.get(TOKEN_ENV), "")
        self.assertFalse(self.token.exists())

    def test_public_token_file_is_refused(self):
        self.write(mode=0o644)
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_public_directory_is_refused(self):
        self.write()
        self.directory.chmod(0o755)
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_symlink_is_refused(self):
        other = self.home / "other"
        other.write_text(self.value)
        other.chmod(0o600)
        self.token.symlink_to(other)
        self.assertEqual(bind(self.env), "bootstrap-unreadable")
        self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_hard_link_is_refused(self):
        self.write()
        os.link(self.token, self.home / "other")
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_fifo_does_not_block_the_hook(self):
        os.mkfifo(self.token, mode=0o600)
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_non_token_and_oversized_values_are_refused(self):
        for value in ("cached-registry-signing-key", "st." + "x" * 9000,
                      "st." + "a" * 15 + " " + "a" * 15, "st." + "a" * 30 + "\x00"):
            self.env.pop(TOKEN_ENV, None)
            self.write(value)
            self.assertIn(bind(self.env), {"bootstrap-custody-refused", "bootstrap-shape-refused"})
            self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_surrounding_whitespace_is_harmless(self):
        self.write("\n " + self.value + "\r\n")
        self.assertEqual(bind(self.env), "bootstrap-bound")
        self.assertEqual(self.env[TOKEN_ENV], self.value)

    def test_symlinked_consumer_directory_is_refused(self):
        self.directory.rmdir()
        other = self.home / "other"
        other.mkdir(mode=0o700)
        self.directory.symlink_to(other, target_is_directory=True)
        self.assertEqual(bind(self.env), "bootstrap-unreadable")
        self.assertEqual(self.env.get(TOKEN_ENV), "")

    def test_failure_diagnostic_names_binding_without_values(self):
        with mock.patch("sys.stderr") as stderr:
            report(bind(self.env))
        message = stderr.write.call_args.args[0]
        self.assertIn(TOKEN_ENV, message)
        self.assertIn("bootstrap-missing", message)
        self.assertNotIn(self.value, message)

    def test_partial_release_hook_exits_cleanly_before_loading_client(self):
        scripts = Path(__file__).resolve().parents[2]
        entry = self.home / "simple-chat"
        shutil.copyfile(scripts / "simple-chat", entry)
        package = self.home / "simple_tools"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "claude_letter.py").write_text("raise AssertionError('partial client ran')\n")
        result = subprocess.run([sys.executable, str(entry), "letter", "hook"],
                                capture_output=True, timeout=5, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TOKEN_ENV.encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
