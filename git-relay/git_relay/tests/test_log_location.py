import io
import unittest
import urllib.error
from email.message import Message

from git_relay.github_client import GitHubClient, RelayGitHubError


def _http_error(code, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", headers, io.BytesIO(b"{}"))


class LogLocationTests(unittest.TestCase):
    def client(self, opener):
        return GitHubClient("owner-token", opener=opener)

    def test_redirect_location_is_returned_and_bearer_stays_on_the_first_hop(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req.full_url, req.get_header("Authorization")))
            raise _http_error(302, "https://x.blob.core.windows.net/log?sig=1")

        c = self.client(opener)
        self.assertEqual(
            c.log_location("/repos/o/r/actions/jobs/1/logs", "app-bearer"),
            "https://x.blob.core.windows.net/log?sig=1",
        )
        self.assertEqual(
            seen, [("https://api.github.com/repos/o/r/actions/jobs/1/logs", "Bearer app-bearer")]
        )

    def test_non_redirect_and_empty_bearer_are_refused(self):
        def opener(req, timeout=None):
            raise _http_error(404)

        with self.assertRaises(RelayGitHubError):
            self.client(opener).log_location("/p", "b")
        with self.assertRaises(ValueError):
            self.client(opener).log_location("/p", "")

    def test_public_fetch_sends_range_no_credential_and_returns_the_true_tail(self):
        seen = []

        class Resp(io.BytesIO):
            def __init__(self, data, status=200, headers=None):
                super().__init__(data)
                self.status, self.headers = status, headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def full(req, timeout=None):  # server ignores Range
            seen.append((req.get_header("Authorization"), req.get_header("Range")))
            return Resp(b"0123456789" * 10)

        data, truncated = self.client(full).fetch_public_tail(
            "https://x.blob.core.windows.net/l", 15
        )
        self.assertEqual((data, truncated), (b"567890123456789"[-15:], True))
        self.assertEqual(seen, [(None, "bytes=-15")])

        def ranged(req, timeout=None):
            return Resp(b"tail-bytes", 206, {"Content-Range": "bytes 90-99/100"})

        self.assertEqual(
            self.client(ranged).fetch_public_tail("https://x.blob.core.windows.net/l", 10),
            (b"tail-bytes", True),
        )

        def whole(req, timeout=None):
            return Resp(b"all", 206, {"Content-Range": "bytes 0-2/3"})

        self.assertEqual(
            self.client(whole).fetch_public_tail("https://x.blob.core.windows.net/l", 10),
            (b"all", False),
        )

    def test_a_server_that_ignores_range_on_a_huge_log_is_refused_not_misread(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            return Resp(b"z" * 100)

        with self.assertRaises(RelayGitHubError):
            self.client(opener).fetch_public_tail(
                "https://x.blob.core.windows.net/l", 10, full_cap=50
            )


    def test_status_falls_back_to_getcode_and_416_is_a_refusal(self):
        class Resp(io.BytesIO):
            def __init__(self, data, code, headers):
                super().__init__(data)
                self._code, self.headers = code, headers

            def getcode(self):
                return self._code

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def ranged(req, timeout=None):
            return Resp(b"tail", 206, {"Content-Range": "bytes 90-93/94"})

        self.assertEqual(
            self.client(ranged).fetch_public_tail("https://x.blob.core.windows.net/l", 10),
            (b"tail", True),
        )

        def unsatisfiable(req, timeout=None):
            raise urllib.error.HTTPError("https://x", 416, "range", {}, None)

        with self.assertRaises(RelayGitHubError):
            self.client(unsatisfiable).fetch_public_tail("https://x.blob.core.windows.net/l", 10)


if __name__ == "__main__":
    unittest.main()
