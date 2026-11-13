"""The shared consumer bootstrap must not borrow credentials or follow unsafe files."""

import os
from pathlib import Path
import tempfile
import unittest

from simple_tools.platform_letter_bootstrap import TOKEN_ENV, bind


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {"HOME": str(self.home)}
        self.directory = self.home / ".config/platform-letter"
        self.directory.mkdir(parents=True, mode=0o700)
        self.token = self.directory / "infisical.token"
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
        self.assertNotIn(TOKEN_ENV, self.env)
        self.assertFalse(self.token.exists())

    def test_public_token_file_is_refused(self):
        self.write(mode=0o644)
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertNotIn(TOKEN_ENV, self.env)

    def test_public_directory_is_refused(self):
        self.write()
        self.directory.chmod(0o755)
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertNotIn(TOKEN_ENV, self.env)

    def test_symlink_is_refused(self):
        other = self.home / "other"
        other.write_text(self.value)
        other.chmod(0o600)
        self.token.symlink_to(other)
        self.assertEqual(bind(self.env), "bootstrap-unreadable")
        self.assertNotIn(TOKEN_ENV, self.env)

    def test_hard_link_is_refused(self):
        self.write()
        os.link(self.token, self.home / "other")
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertNotIn(TOKEN_ENV, self.env)

    def test_fifo_does_not_block_the_hook(self):
        os.mkfifo(self.token, mode=0o600)
        self.assertEqual(bind(self.env), "bootstrap-custody-refused")
        self.assertNotIn(TOKEN_ENV, self.env)

    def test_non_token_and_oversized_values_are_refused(self):
        for value in ("cached-registry-signing-key", "st." + "x" * 9000,
                      "st." + "a" * 30 + "\n", "st." + "a" * 30 + "\x00"):
            self.write(value)
            self.assertIn(bind(self.env), {"bootstrap-custody-refused", "bootstrap-shape-refused"})
            self.assertNotIn(TOKEN_ENV, self.env)
