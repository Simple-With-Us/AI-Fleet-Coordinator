"""Real-ingress loaded-credential regressions: Relay.handle wiring, distinct tokens.

The caller-presented bearer and the minted installation token are different
values.  Each must refuse through the actual handle -> _dispatch ->
_dispatch_workflow -> for_action chain before any GitHub content mutation,
and neither value may appear in the relay audit log.
"""

import base64
import json
import os
import tempfile
import unittest
from hashlib import sha1

from git_relay.audit import AuditLog
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.tests import test_workflow_verbs as support

REPO = support.REPO
SHA = support.SHA
CALLER = "caller-" + "c" * 34
INSTALL = "install-" + "i" * 33

SEATS = json.dumps(
    {
        "swu-test": {
            "auth": "token",
            "token": CALLER,
            "identity": "TEST",
            "app_slug": "swu-test",
            "actions": ["wf_file_put", "wf_files_put", "wf_git_tree_create"],
            "repos": [REPO],
            "mint_permissions": {"contents": "write", "metadata": "read"},
        }
    }
)


class _Minter:
    def __init__(self, token):
        self.token = token

    def token_for(self, slug, repo, permissions):
        return self.token, 0.0


class _Client:
    """Records every call; fails the test if called with no scripted reply."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    def _call(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path, payload, bearer))
        if not self.replies:
            raise AssertionError("unexpected upstream call %s %s" % (method, path))
        return self.replies.pop(0)


def _blob(raw):
    sha = sha1(
        b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
    ).hexdigest()
    return sha, {
        "sha": sha,
        "encoding": "base64",
        "size": len(raw),
        "content": base64.b64encode(raw).decode(),
    }


class RealIngressLoadedTests(unittest.TestCase):
    def _relay(self, client, log):
        return Relay(
            SeatPolicy(SEATS),
            client,
            AuditLog(log),
            app_minter=_Minter(INSTALL),
            workflow_verbs_enabled=True,
        )

    def _args(self, action, content_b64):
        base = {"repo": REPO, "branch": "feat/x", "message": "test"}
        if action == "wf_file_put":
            return dict(base, path="tests/x.py", content_b64=content_b64)
        return dict(
            base,
            expected_head_sha=SHA,
            files=[{"path": "tests/x.py", "content_b64": content_b64}],
        )

    def test_caller_and_installation_refused_before_any_upstream_call(self):
        for token in (CALLER, INSTALL):
            content_b64 = base64.b64encode(("x = " + repr(token) + chr(10)).encode()).decode()
            for action in ("wf_file_put", "wf_files_put"):
                with self.subTest(token=token[:8], action=action), tempfile.TemporaryDirectory() as td:
                    log = os.path.join(td, "audit.jsonl")
                    client = _Client()
                    status, _payload = self._relay(client, log).handle(
                        CALLER, action, self._args(action, content_b64)
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(client.calls, [])
                    with open(log) as fh:
                        audit = fh.read()
                    self.assertNotIn(CALLER, audit)
                    self.assertNotIn(INSTALL, audit)

    def test_referenced_blob_refused_after_only_its_required_fetch(self):
        for token in (CALLER, INSTALL):
            with self.subTest(token=token[:8]), tempfile.TemporaryDirectory() as td:
                log = os.path.join(td, "audit.jsonl")
                sha, blob = _blob(("x = " + repr(token) + chr(10)).encode())
                client = _Client([blob])
                args = {
                    "repo": REPO,
                    "base_tree": SHA,
                    "files": [{"path": "x.txt", "sha": sha, "mode": "100644"}],
                }
                status, _payload = self._relay(client, log).handle(
                    CALLER, "wf_git_tree_create", args
                )
                self.assertEqual(status, 400)
                # Exactly the one required blob GET; no mutation of any kind.
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.calls[0][0], "GET")
                with open(log) as fh:
                    audit = fh.read()
                self.assertNotIn(CALLER, audit)
                self.assertNotIn(INSTALL, audit)

    def test_clean_content_passes_real_ingress(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "audit.jsonl")
            client = support.Tests()._git_data_client()
            clean = base64.b64encode(b"x = 1\n").decode()
            status, payload = self._relay(client, log).handle(
                CALLER, "wf_files_put", self._args("wf_files_put", clean)
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["commit"], "1" * 40)


if __name__ == "__main__":
    unittest.main()
