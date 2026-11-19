#!/usr/bin/env python3
"""The consumer's own store: one scoped token, read from where that host keeps it.

The store reader replaces the retired shared reader.  These tests cover the refusals as
carefully as the success: a missing item, a locked keychain, a file that is too open, a
file that belongs to somebody else and a value that is not a service token must all yield
no token, and the status must say which refusal it was.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_infisical as fi


def synthetic_token() -> str:
    """A service-token-shaped value, made up and assembled here so no token-shaped literal sits.

    The client checks the shape, never the value: the "st." prefix, the length and the
    absence of whitespace.
    """
    return ".".join(["st", "synthetic", "notarealvalue", "notarealvalue"])


class FileStoreTests(unittest.TestCase):
    """The server's store: a private file inside a private directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.dir = self.home / ".config" / "infisical" / "tokens"
        if self.dir.is_symlink():
            raise ValueError(f"refusing symlink: {self.dir}")
        self.dir.mkdir(mode=0o700, parents=True)
        self.dir.chmod(0o700)
        self.path = self.dir / "consumer-ro.token"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, text: str, mode: int = 0o600) -> None:
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(mode)

    def read(self) -> tuple[str, str]:
        with mock.patch.object(fi, "_is_macos", return_value=False):
            return fi.token_from_store("consumer-ro", home=self.home)

    def test_a_private_file_is_the_token(self) -> None:
        self.write(synthetic_token() + "\n")
        token, status = self.read()
        self.assertEqual(status, "ok")
        self.assertTrue(token.startswith("st."))

    def test_a_missing_file_is_no_token(self) -> None:
        token, status = self.read()
        self.assertEqual((token, status), ("", "no-token"))

    def test_an_open_file_is_refused(self) -> None:
        self.write(synthetic_token() + "\n", mode=0o644)
        token, status = self.read()
        self.assertEqual(token, "")
        self.assertEqual(status, "token-unreadable")

    def test_an_open_directory_is_refused(self) -> None:
        self.write(synthetic_token() + "\n")
        self.dir.chmod(0o755)
        token, status = self.read()
        self.assertEqual(token, "")
        self.assertEqual(status, "token-unreadable")

    def test_a_symlinked_file_is_refused(self) -> None:
        other = self.home / "elsewhere.token"
        other.write_text(synthetic_token() + "\n", encoding="utf-8")
        other.chmod(0o600)
        os.symlink(other, self.path)
        token, status = self.read()
        self.assertEqual(token, "")
        self.assertEqual(status, "token-unreadable")

    def test_a_value_that_is_not_a_service_token_is_refused(self) -> None:
        self.write("not-a-service-token\n")
        token, status = self.read()
        self.assertEqual(token, "")
        self.assertEqual(status, "token-unreadable")

    def test_an_empty_file_is_no_token(self) -> None:
        self.write("\n")
        token, status = self.read()
        self.assertEqual((token, status), ("", "no-token"))

    def test_a_traversal_name_is_refused(self) -> None:
        with mock.patch.object(fi, "_is_macos", return_value=False):
            token, status = fi.token_from_store("../escape", home=self.home)
        self.assertEqual((token, status), ("", "no-token"))


class KeychainStoreTests(unittest.TestCase):
    """The Mac's store: one generic-password item per token."""

    def read(self, result) -> tuple[str, str]:
        with mock.patch.object(fi, "_is_macos", return_value=True):
            with mock.patch.object(fi.subprocess, "run", return_value=result):
                return fi.token_from_store("consumer-ro")

    def test_the_item_is_the_token(self) -> None:
        result = mock.Mock(returncode=0, stdout=synthetic_token() + "\n", stderr="")
        token, status = self.read(result)
        self.assertEqual(status, "ok")
        self.assertTrue(token.startswith("st."))

    def test_a_missing_item_is_no_token(self) -> None:
        result = mock.Mock(returncode=44, stdout="", stderr="SecKeychainSearchCopyNext")
        self.assertEqual(self.read(result), ("", "no-token"))

    def test_a_locked_keychain_says_so(self) -> None:
        result = mock.Mock(returncode=36, stdout="", stderr="User interaction is not allowed.")
        self.assertEqual(self.read(result), ("", "keychain-locked"))

    def test_an_unreadable_item_says_so(self) -> None:
        result = mock.Mock(returncode=1, stdout="", stderr="something else")
        self.assertEqual(self.read(result), ("", "token-unreadable"))

    def test_a_value_that_is_not_a_service_token_is_refused(self) -> None:
        result = mock.Mock(returncode=0, stdout="nope\n", stderr="")
        self.assertEqual(self.read(result), ("", "token-unreadable"))


class FetchScopedTests(unittest.TestCase):
    """The read goes through the consumer's own token, and the grant is checked."""

    def test_a_missing_token_stops_before_the_network(self) -> None:
        with mock.patch.object(fi, "token_from_store", return_value=("", "no-token")):
            with mock.patch.object(fi, "fetch_verified") as fetch:
                value, status = fi.fetch_scoped("NAME", "/folder", "consumer-ro")
        self.assertEqual((value, status), ("", "no-token"))
        fetch.assert_not_called()

    def test_a_good_read_returns_the_value(self) -> None:
        with (
            mock.patch.object(fi, "token_from_store", return_value=(synthetic_token(), "ok")),
            mock.patch.object(fi, "fetch_verified", return_value="value") as fetch,
        ):
            value, status = fi.fetch_scoped("NAME", "/folder", "consumer-ro")
        self.assertEqual((value, status), ("value", "ok"))
        self.assertEqual(fetch.call_args.args[:2], ("NAME", "/folder"))

    def test_an_absent_name_says_absent(self) -> None:
        error = fi.SecretsError("key NAME is not in Infisical at /folder", 3)
        with (
            mock.patch.object(fi, "token_from_store", return_value=(synthetic_token(), "ok")),
            mock.patch.object(fi, "fetch_verified", side_effect=error),
        ):
            value, status = fi.fetch_scoped("NAME", "/folder", "consumer-ro")
        self.assertEqual((value, status), ("", "absent"))

    def test_a_refused_grant_says_forbidden(self) -> None:
        error = fi.SecretsError("token-verification-failed")
        with (
            mock.patch.object(fi, "token_from_store", return_value=(synthetic_token(), "ok")),
            mock.patch.object(fi, "fetch_verified", side_effect=error),
        ):
            value, status = fi.fetch_scoped("NAME", "/folder", "consumer-ro")
        self.assertEqual((value, status), ("", "forbidden"))


class TimeoutTests(unittest.TestCase):
    """A hung lookup is transient,  never a broken credential."""

    def api(self) -> tuple[int, object]:
        return fi._api_json("GET", "/v2/service-token", synthetic_token(), None, {})

    def test_a_read_timeout_is_a_timeout_error(self) -> None:
        hung = socket.timeout("timed out")  # noqa: UP041 - not TimeoutError on Python 3.9
        with mock.patch.object(fi, "_opener", side_effect=hung):
            with self.assertRaises(fi.SecretsTimeout):
                self.api()

    def test_a_wrapped_connect_timeout_is_a_timeout_error(self) -> None:
        reason = socket.timeout("timed out")  # noqa: UP041 - not TimeoutError on Python 3.9
        with mock.patch.object(fi, "_opener", side_effect=urllib.error.URLError(reason)):
            with self.assertRaises(fi.SecretsTimeout):
                self.api()

    def test_a_timeout_is_both_a_secrets_error_and_a_timeout_error(self) -> None:
        self.assertTrue(issubclass(fi.SecretsTimeout, fi.SecretsError))
        self.assertTrue(issubclass(fi.SecretsTimeout, TimeoutError))

    def test_a_connection_error_is_still_a_plain_failure(self) -> None:
        with mock.patch.object(fi, "_opener", side_effect=OSError("connection refused")):
            self.assertEqual(self.api(), (0, None))

    def test_fetch_scoped_lets_a_timeout_through(self) -> None:
        with mock.patch.object(fi, "token_from_store", return_value=(synthetic_token(), "ok")):
            with mock.patch.object(fi, "fetch_verified", side_effect=fi.SecretsTimeout()):
                with self.assertRaises(TimeoutError):
                    fi.fetch_scoped("NAME", "/folder", "consumer-ro", {})

    def test_fetch_scoped_still_reports_a_refusal_as_a_status(self) -> None:
        with mock.patch.object(fi, "token_from_store", return_value=(synthetic_token(), "ok")):
            with mock.patch.object(
                fi, "fetch_verified", side_effect=fi.SecretsError("token-verification-failed")
            ):
                self.assertEqual(
                    fi.fetch_scoped("NAME", "/folder", "consumer-ro", {}), ("", "forbidden")
                )


if __name__ == "__main__":
    unittest.main()
