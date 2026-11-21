"""Scoped token-store boundaries used by the shared reader, without live auth."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import fleet_infisical as fi


def synthetic_token():
    return ".".join(("st", "synthetic", "notarealvalue", "notarealvalue"))


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="scoped-store-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.folder = self.home / ".config" / "infisical" / "tokens"
        self.folder.mkdir(parents=True, mode=0o700)
        self.folder.chmod(0o700)
        self.path = self.folder / "consumer-ro.token"

    def read(self, env=None):
        with mock.patch.object(fi, "_is_macos", return_value=False):
            return fi.token_from_store("consumer-ro", env or {}, self.home)

    def test_private_file_reads_exact_existing_store(self):
        self.path.write_text(synthetic_token() + "\n", encoding="utf-8")
        self.path.chmod(0o600)
        self.assertEqual(self.read(), (synthetic_token(), "ok"))

    def test_missing_store_never_uses_ambient_token(self):
        self.assertEqual(self.read({fi.ENV_TOKEN: synthetic_token()}), ("", "no-token"))

    def test_open_or_symlinked_file_is_refused(self):
        self.path.write_text(synthetic_token(), encoding="utf-8")
        self.path.chmod(0o644)
        self.assertEqual(self.read(), ("", "token-unreadable"))
        self.path.unlink()
        other = self.home / "other.token"
        other.write_text(synthetic_token(), encoding="utf-8")
        other.chmod(0o600)
        os.symlink(other, self.path)
        self.assertEqual(self.read(), ("", "token-unreadable"))

    def test_leaf_replacement_before_open_cannot_follow_a_symlink(self):
        self.path.write_text(synthetic_token(), encoding="utf-8")
        self.path.chmod(0o600)
        target = self.home / "other.token"
        target.write_text(synthetic_token(), encoding="utf-8")
        target.chmod(0o600)
        real_open = os.open
        replaced = False

        def replace_then_open(name, flags, *args, **kwargs):
            nonlocal replaced
            if name == self.path.name and not replaced:
                replaced = True
                self.path.unlink()
                os.symlink(target, self.path)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(fi.os, "open", side_effect=replace_then_open):
            self.assertEqual(self.read({fi.ENV_TOKEN: synthetic_token()}), ("", "token-unreadable"))
        self.assertTrue(replaced)

    def test_leaf_replacement_after_open_fails_path_identity(self):
        self.path.write_text(synthetic_token(), encoding="utf-8")
        self.path.chmod(0o600)
        real_read = os.read
        replaced = False

        def replace_then_read(fd, count):
            nonlocal replaced
            if not replaced:
                replaced = True
                self.path.rename(self.folder / "old.token")
                self.path.write_text(synthetic_token(), encoding="utf-8")
                self.path.chmod(0o600)
            return real_read(fd, count)

        with mock.patch.object(fi.os, "read", side_effect=replace_then_read):
            self.assertEqual(self.read(), ("", "token-unreadable"))
        self.assertTrue(replaced)

    def test_symlinked_parent_is_refused_before_file_open(self):
        self.path.write_text(synthetic_token(), encoding="utf-8")
        self.path.chmod(0o600)
        original = self.home / ".config" / "infisical"
        moved = self.home / "moved-infisical"
        original.rename(moved)
        os.symlink(moved, original)
        self.assertEqual(self.read(), ("", "token-unreadable"))

    def test_parent_replacement_after_open_fails_path_identity(self):
        self.path.write_text(synthetic_token(), encoding="utf-8")
        self.path.chmod(0o600)
        original = self.home / ".config" / "infisical"
        moved = self.home / "moved-infisical"
        real_read = os.read
        replaced = False

        def replace_parent_then_read(fd, count):
            nonlocal replaced
            if not replaced:
                replaced = True
                original.rename(moved)
                os.symlink(moved, original)
            return real_read(fd, count)

        with mock.patch.object(fi.os, "read", side_effect=replace_parent_then_read):
            self.assertEqual(self.read(), ("", "token-unreadable"))
        self.assertTrue(replaced)

    def test_invalid_account_refuses_traversal(self):
        with mock.patch.object(fi, "_is_macos", return_value=False):
            self.assertEqual(fi.token_from_store("../escape", {}, self.home), ("", "no-token"))
            self.assertEqual(fi.token_from_store("bad\x00account", {}, self.home), ("", "no-token"))
            self.assertEqual(fi.token_from_store("bad\naccount", {}, self.home), ("", "no-token"))


class KeychainStoreTests(unittest.TestCase):
    def read(self, result):
        with mock.patch.object(fi, "_is_macos", return_value=True):
            with mock.patch.object(fi.subprocess, "run", return_value=result) as child:
                value = fi.token_from_store("consumer-ro", {fi.ENV_TOKEN: synthetic_token()})
        self.assertEqual(child.call_args.args[0], [
            fi.SECURITY_BIN, "find-generic-password", "-s", fi.SCOPED_KEYCHAIN_SERVICE,
            "-a", "consumer-ro", "-w",
        ])
        return value

    def test_existing_item_is_authority_over_ambient_token(self):
        result = mock.Mock(returncode=0, stdout=synthetic_token() + "\n", stderr="")
        self.assertEqual(self.read(result), (synthetic_token(), "ok"))

    def test_locked_item_refuses_without_ambient_fallback(self):
        result = mock.Mock(returncode=36, stdout="", stderr="interaction is not allowed")
        self.assertEqual(self.read(result), ("", "keychain-locked"))

    def test_hung_keychain_has_distinct_timeout_code(self):
        with mock.patch.object(fi, "_is_macos", return_value=True):
            with mock.patch.object(fi.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 30)):
                with self.assertRaises(fi.SecretsError) as raised:
                    fi.token_from_store("consumer-ro", {fi.ENV_TOKEN: synthetic_token()})
        self.assertEqual(raised.exception.code, 124)


if __name__ == "__main__":
    unittest.main()
