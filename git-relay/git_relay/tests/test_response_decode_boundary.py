"""Decode failures after an upstream write remain unknown, never local refusal."""
import unittest
from git_relay.github_client import GitHubClient
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
        client = GitHubClient('fixture-not-a-credential', opener=lambda *a, **k: Response(body))
        return client._call('POST', '/repos/fixture/example/issues', {'title': 'fixture'})

    def test_invalid_bodies_are_transport_unknown_not_valueerror(self):
        cases = [b'{', b'\xff', b'{"n":' + b'1' * 5000 + b'}',
                 b'[' * 100000 + b'0' + b']' * 100000,
                 b'{"s":"\\ud800"}', b'{"\\udfff":"value"}',
                 b'{"nested":[{"s":"\\ud800"}]}']
        for body in cases:
            with self.subTest(prefix=body[:35]):
                with self.assertRaises(RelayGitHubError) as raised:
                    self.call(body)
                self.assertEqual(raised.exception.status, 0)

    def test_valid_scalar_array_and_unicode_shapes_are_preserved(self):
        for body, expected in [(b'{"value":"hello"}', {'value': 'hello'}),
                               (b'[1,true,null]', [1, True, None]),
                               (b'"\\ud83d\\ude00"', '\U0001f600')]:
            self.assertEqual(self.call(body), expected)

    def test_empty_completion_still_works(self):
        self.assertEqual(self.call(b''), {})


if __name__ == '__main__':
    unittest.main()
