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

    def test_public_fetch_sends_no_credential_and_caps_the_read(self):
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            return Resp(b"y" * 100)

        data = self.client(opener).fetch_public_tail("https://x.blob.core.windows.net/l", 10)
        self.assertEqual(len(data), 11)
        self.assertEqual(seen, [None])


if __name__ == "__main__":
    unittest.main()
