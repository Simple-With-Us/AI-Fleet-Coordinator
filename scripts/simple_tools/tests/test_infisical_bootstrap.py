"""Consumer custody stays private, bounded and explicit without reading other stores."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_infisical_bootstrap import bind_private_token


class PrivateTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "declared-consumer"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "bootstrap.token"
        self.env = {}
        self.token_env = "EXAMPLE_CONSUMER_INFISICAL_TOKEN"
        self.value = "st." + "synthetic-bootstrap-" * 3

    def bind(self, **overrides):
        args = dict(
            token_env=self.token_env,
            store_directory=self.directory,
            store_name=self.path.name,
        )
        args.update(overrides)
        return bind_private_token(self.env, **args)

    def write(self, value=None, mode=0o600):
        self.path.write_text(self.value if value is None else value)
        self.path.chmod(mode)

    def test_only_the_declared_process_binding_is_written(self):
        self.write()
        self.assertEqual(self.bind(), "bootstrap-bound")
        self.assertEqual(self.env, {self.token_env: self.value})

    def test_explicit_binding_is_preserved_even_when_empty_or_invalid(self):
        for value in ("", "invalid", self.value):
            self.env[self.token_env] = value
            with mock.patch("fleet_infisical_bootstrap.os.open") as opened:
                self.assertEqual(self.bind(), "explicit-binding")
            opened.assert_not_called()
            self.assertEqual(self.env[self.token_env], value)

    def test_missing_store_does_not_create_or_discover_a_replacement(self):
        self.directory.rmdir()
        self.assertEqual(self.bind(), "bootstrap-missing")
        self.assertEqual(self.env[self.token_env], "")
        self.assertFalse(self.directory.exists())

    def test_missing_file_leaves_empty_marker(self):
        self.assertEqual(self.bind(), "bootstrap-missing")
        self.assertEqual(self.env[self.token_env], "")
        self.assertFalse(self.path.exists())

    def test_public_directory_is_refused(self):
        self.write()
        self.directory.chmod(0o755)
        self.assertEqual(self.bind(), "bootstrap-custody-refused")

    def test_public_file_is_refused(self):
        self.write(mode=0o644)
        self.assertEqual(self.bind(), "bootstrap-custody-refused")

    def test_directory_owner_must_match_consumer(self):
        self.write()
        with mock.patch(
            "fleet_infisical_bootstrap.os.getuid", return_value=os.getuid() + 1
        ):
            self.assertEqual(self.bind(), "bootstrap-custody-refused")

    def test_file_owner_must_match_consumer(self):
        self.write()
        current = os.getuid()
        with mock.patch(
            "fleet_infisical_bootstrap.os.getuid", side_effect=[current, current + 1]
        ):
            self.assertEqual(self.bind(), "bootstrap-custody-refused")

    def test_token_symlink_is_never_followed(self):
        target = self.directory / "other"
        target.write_text(self.value)
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assertEqual(self.bind(), "bootstrap-unreadable")

    def test_directory_symlink_is_never_followed(self):
        self.directory.rmdir()
        target = Path(self.temp.name) / "other"
        target.mkdir(mode=0o700)
        self.directory.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.bind(), "bootstrap-unreadable")

    def test_hardlink_is_refused(self):
        self.write()
        os.link(self.path, self.directory / "other")
        self.assertEqual(self.bind(), "bootstrap-custody-refused")

    def test_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.path, mode=0o600)
        self.assertEqual(self.bind(), "bootstrap-custody-refused")

    def test_non_token_oversized_and_embedded_whitespace_are_refused(self):
        for value in (
            "cached-registry-signing-key",
            "st." + "x" * 9000,
            "st." + "a" * 15 + " " + "a" * 15,
            "st." + "a" * 30 + "\x00",
        ):
            self.env.clear()
            self.write(value)
            self.assertIn(self.bind(), {"bootstrap-custody-refused", "bootstrap-shape-refused"})
            self.assertEqual(self.env[self.token_env], "")

    def test_non_ascii_is_refused(self):
        self.write("st." + "x" * 30 + "é")
        self.assertEqual(self.bind(), "bootstrap-unreadable")

    def test_surrounding_whitespace_is_removed(self):
        self.write("\n " + self.value + "\r\n")
        self.assertEqual(self.bind(), "bootstrap-bound")
        self.assertEqual(self.env[self.token_env], self.value)

    def test_relative_directory_or_traversing_filename_is_refused_before_open(self):
        for overrides in (
            {"store_directory": "relative"},
            {"store_name": "../other"},
            {"store_name": "/other"},
            {"store_name": ".."},
        ):
            self.env.clear()
            with mock.patch("fleet_infisical_bootstrap.os.open") as opened:
                self.assertEqual(self.bind(**overrides), "bootstrap-config-refused")
            opened.assert_not_called()
            self.assertEqual(self.env[self.token_env], "")


if __name__ == "__main__":
    unittest.main()
