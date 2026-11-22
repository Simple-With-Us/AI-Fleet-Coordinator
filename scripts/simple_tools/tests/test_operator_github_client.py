"""The Operator GitHub client tells a broken reader from a broken credential.

The consumer's own scoped service token is the one credential path,  and every
failure names the source an operator must repair:  a hung lookup is 124,  a reader
that is not there is 127,  and a refused lookup is a client error.  A missing shared
client must never be reported as a credential problem.

    cd scripts && python3 -m unittest simple_tools.tests.test_operator_github_client -v

No network and no token store.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from simple_tools.operator_github_client import ClientError, caller_key


def fake_client(**behaviour):
    """A stand-in for the shared client,  as the function imports it."""
    fake = mock.Mock()
    fake.fetch_scoped.side_effect = behaviour.get("error")
    fake.fetch_scoped.return_value = behaviour.get("answer", ("value", "ok"))
    return fake


class CallerKeyTests(unittest.TestCase):
    def test_a_reader_that_is_not_there_exits_127(self) -> None:
        # A None in sys.modules makes the import raise,  which is what a host
        # without the shared client does.
        with mock.patch.dict(sys.modules, {"fleet_infisical": None}):
            with mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as caught:
                    caller_key("CODEX")
        self.assertEqual(caught.exception.code, 127)

    def test_a_hung_lookup_exits_124(self) -> None:
        with mock.patch.dict(sys.modules, {"fleet_infisical": fake_client(error=TimeoutError)}):
            with mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as caught:
                    caller_key("CODEX")
        self.assertEqual(caught.exception.code, 124)

    def test_a_refused_lookup_is_a_client_error(self) -> None:
        with mock.patch.dict(
            sys.modules, {"fleet_infisical": fake_client(error=RuntimeError("refused"))}
        ):
            with self.assertRaises(ClientError):
                caller_key("CODEX")

    def test_a_read_that_answers_returns_the_key(self) -> None:
        # The client refuses a key that is not a key:  the length is the check.
        key = "k" * 32
        client = fake_client(answer=(key, "ok"))
        with mock.patch.dict(sys.modules, {"fleet_infisical": client}):
            self.assertEqual(caller_key("CODEX"), key)


if __name__ == "__main__":
    unittest.main()
