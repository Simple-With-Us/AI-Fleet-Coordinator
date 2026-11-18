"""Decode failures after an upstream write remain unknown, never local refusal."""

import unittest

from git_relay.github_client import (
    MAX_RESPONSE_DEPTH,
    MAX_RESPONSE_NODES,
    GitHubClient,
    _reject_surrogates,
)
from git_relay.server import RelayGitHubError


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class ResponseDecodeBoundaryTests(unittest.TestCase):
    def call(self, body):
        client = GitHubClient("fixture-not-a-credential", opener=lambda *a, **k: Response(body))
        return client._call("POST", "/repos/fixture/example/issues", {"title": "fixture"})

    def test_invalid_bodies_are_transport_unknown_not_valueerror(self):
        cases = [
            b"{",
            b"\xff",
            b'{"n":' + b"1" * 5000 + b"}",
            b"[" * 100000 + b"0" + b"]" * 100000,
            b'{"s":"\\ud800"}',
            b'{"\\udfff":"value"}',
            b'{"nested":[{"s":"\\ud800"}]}',
        ]
        for body in cases:
            with self.subTest(prefix=body[:35]):
                with self.assertRaises(RelayGitHubError) as raised:
                    self.call(body)
                self.assertEqual(raised.exception.status, 0)

    def test_valid_scalar_array_and_unicode_shapes_are_preserved(self):
        for body, expected in [
            (b'{"value":"hello"}', {"value": "hello"}),
            (b"[1,true,null]", [1, True, None]),
            (b'"\\ud83d\\ude00"', "\U0001f600"),
        ]:
            self.assertEqual(self.call(body), expected)

    def test_explicit_depth_bound_independent_of_json_decoder(self):
        value = 0
        for _ in range(MAX_RESPONSE_DEPTH):
            value = [value]
        _reject_surrogates(value)
        with self.assertRaises(RelayGitHubError) as raised:
            _reject_surrogates([value])
        self.assertEqual(raised.exception.status, 0)

    def test_total_nodes_count_array_elements_and_object_keys(self):
        _reject_surrogates([0] * (MAX_RESPONSE_NODES - 1))
        for value in (
            [0] * MAX_RESPONSE_NODES,
            {str(i): 0 for i in range(MAX_RESPONSE_NODES // 2)},
        ):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(RelayGitHubError) as raised:
                    _reject_surrogates(value)
                self.assertEqual(raised.exception.status, 0)

    def test_empty_completion_still_works(self):
        self.assertEqual(self.call(b""), {})


if __name__ == "__main__":
    unittest.main()
