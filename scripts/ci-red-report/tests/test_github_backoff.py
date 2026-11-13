"""Retry/backoff tests for GitHubClient."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_GH = Path(__file__).resolve().parents[1] / "github.py"
_spec = importlib.util.spec_from_file_location("ci_red_report_github", _GH)
assert _spec and _spec.loader
gh = importlib.util.module_from_spec(_spec)
sys.modules["ci_red_report_github"] = gh
_spec.loader.exec_module(gh)


class BackoffTests(unittest.TestCase):
    def _client(self, replies, sleeps):
        def request_fn(method, url, headers):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return gh.GitHubClient("t", sleep=sleeps.append, max_retries=2, request_fn=request_fn)

    def test_exhausted_403_does_not_delay_next_request(self) -> None:
        sleeps: list[float] = []
        replies = [
            gh.GitHubError("x", status=403),
            gh.GitHubError("x", status=403),
            (b"{}", {}),
        ]
        client = self._client(replies, sleeps)
        with self.assertRaises(gh.GitHubError):
            client._request_json("GET", "https://api.github.com/x")
        before = len(sleeps)
        self.assertEqual(client._request_json("GET", "https://api.github.com/y"), {})
        self.assertEqual(len(sleeps), before)

    def test_403_retry_sleeps_once_per_attempt(self) -> None:
        sleeps: list[float] = []
        replies = [gh.GitHubError("x", status=403), (b"{}", {})]
        client = self._client(replies, sleeps)
        self.assertEqual(client._request_json("GET", "https://api.github.com/x"), {})
        self.assertEqual(len(sleeps), 1)


if __name__ == "__main__":
    unittest.main()
