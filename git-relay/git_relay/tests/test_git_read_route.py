"""The read-only git route refuses credential-shaped repo names and audits unexpected exceptions honestly."""

import io
import json
import unittest
from unittest import mock

from git_relay import git_read


class FakeHandler:
    def __init__(self, path="/git/o/r/info/refs?service=git-upload-pack", headers=None):
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.wfile = io.BytesIO()
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def body(self):
        return json.loads(self.wfile.getvalue().decode())


class FakeAudit:
    def __init__(self):
        self.rows = []

    def record(self, seat, action, repo, outcome, detail):
        self.rows.append({"seat": seat, "repo": repo, "outcome": outcome, "detail": detail})


class FakePolicy:
    def authenticate(self, token, proof):
        return "seat-one"

    def authenticate_with_reason(self, token, proof=None):
        return "seat-one", ""

    def authorize(self, seat, action, repo):
        return None

    def app_identity(self, seat):
        return "app", {"contents": "read"}


class FakeMinter:
    def __init__(self):
        self.calls = []

    def token_for(self, slug, repo, perms):
        self.calls.append(repo)
        return "minted", 0


class FakeRelay:
    def __init__(self):
        self.policy = FakePolicy()
        self.app_minter = FakeMinter()
        self.audit = FakeAudit()
        self._unauth_auditor = FakeAudit()


class GitReadRoute(unittest.TestCase):
    def test_a_credential_shaped_repo_is_refused_before_the_mint(self):
        relay = FakeRelay()
        handler = FakeHandler()
        detail = {"kind": "advertise", "bytes": 0}
        # find_secret is the scanner's own verdict, mocked here so no credential-shaped literal sits in
        # the fixture (the pattern family itself is covered by the scanner's tests).
        with mock.patch.object(git_read, "find_secret", return_value="repo"):
            outcome = git_read._serve(
                relay, handler, "seat-one", "o/some-repo", "/x", "advertise", b"", detail, None
            )
        self.assertEqual(outcome, "refused")
        self.assertEqual(handler.status, 400)
        self.assertEqual(relay.app_minter.calls, [])
        self.assertEqual(detail["reason"], "credential-shaped repo name")

    def test_an_unexpected_exception_is_audited_as_an_error_not_ok(self):
        relay = FakeRelay()

        def broken_authorize(seat, action, repo):
            raise RuntimeError("policy store blew up")

        relay.policy.authorize = broken_authorize
        handler = FakeHandler(headers={"Authorization": "Bearer seat-token"})
        with self.assertRaises(RuntimeError):
            git_read.handle(relay, handler, "GET")
        self.assertEqual(relay.audit.rows[-1]["outcome"], "error")

    def test_an_unreachable_upstream_is_still_audited_unavailable(self):
        relay = FakeRelay()

        def refused_connect():
            raise OSError("no route")

        handler = FakeHandler(headers={"Authorization": "Bearer seat-token"})
        git_read.handle(relay, handler, "GET", connect=refused_connect)
        self.assertEqual(relay.audit.rows[-1]["outcome"], "unavailable")


if __name__ == "__main__":
    unittest.main()
