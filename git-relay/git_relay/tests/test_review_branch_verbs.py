import json
import os
import tempfile
import unittest

try:
    from test_workflow_wiring import TOKEN, Minter
except ImportError:
    from .test_workflow_wiring import TOKEN, Minter

from git_relay.audit import AuditLog
from git_relay.github_client import RelayGitHubError
from git_relay.policy import SeatPolicy
from git_relay.server import Relay, _workflow_audit_detail

try:
    from test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class ReviewBranchTests(unittest.TestCase):
    def run_action(self, action, args, *replies):
        client = Client(*replies)
        h = WorkflowVerbs(client, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
        return h.run(action, {"repo": REPO, **args}), client

    def test_review_and_inline_bind_commit_and_fixed_path(self):
        for action, args, key in [
            (
                "wf_review_submit",
                {"number": 2, "commit_id": SHA, "event": "COMMENT", "body": "hello"},
                "review",
            ),
            (
                "wf_review_inline",
                {
                    "number": 2,
                    "commit_id": SHA,
                    "path": "src/a.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "hello",
                },
                "comment",
            ),
        ]:
            out, c = self.run_action(
                action,
                args,
                {"head": {"sha": SHA}},
                {"id": 3, "commit_id": SHA, "state": "COMMENTED"},
            )
            self.assertEqual(out[key]["id"], 3)
            self.assertEqual([x[0] for x in c.calls], ["GET", "POST"])
            self.assertEqual(c.calls[-1][2]["commit_id"], SHA)
            with self.assertRaises(Refused):
                self.run_action(action, args, {"head": {"sha": "b" * 40}})
            with self.assertRaises(RelayGitHubError):
                self.run_action(action, args, {"head": {"sha": SHA}}, None)

    def test_branch_only_creates_fixed_heads_reference(self):
        out, c = self.run_action(
            "wf_branch_create",
            {"branch": "wise/new", "sha": SHA},
            {"ref": "refs/heads/wise/new", "object": {"sha": SHA}},
        )
        self.assertEqual(c.calls[0][2], {"ref": "refs/heads/wise/new", "sha": SHA})
        self.assertEqual(out["sha"], SHA)
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_branch_create", {"branch": "wise/new", "sha": SHA}, {})

    def test_least_permissions_and_no_ceiling_widening(self):
        for action, perms in [
            ("wf_review_submit", {"pull_requests": "write", "metadata": "read"}),
            ("wf_review_inline", {"pull_requests": "write", "metadata": "read"}),
            ("wf_branch_create", {"contents": "write", "metadata": "read"}),
        ]:
            minter = Minter()
            WorkflowVerbs.for_action(Client(), Policy(), minter, "swu-wise", action, REPO)
            self.assertEqual(minter.calls[0][2], perms)
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(
                    Client(), Policy({"metadata": "read"}), Minter(), "swu-wise", action, REPO
                )

    def test_validation_before_network(self):
        for action, args in [
            (
                "wf_review_submit",
                {"number": True, "commit_id": SHA, "event": "COMMENT", "body": "x"},
            ),
            (
                "wf_review_inline",
                {
                    "number": 2,
                    "commit_id": SHA,
                    "path": "../x",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "x",
                },
            ),
            ("wf_review_submit", {"number": 2, "commit_id": SHA, "event": "DELETE", "body": "x"}),
            ("wf_branch_create", {"branch": "x", "sha": SHA, "force": True}),
            ("wf_branch_create", {"branch": "../x", "sha": SHA}),
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {"repo": REPO, **args}, REPO)


class ReviewAuditDetailTests(unittest.TestCase):
    def test_review_and_inline_audit_keeps_ids_not_bodies(self):
        detail = _workflow_audit_detail(
            {"repo": REPO, "number": 2, "commit_id": SHA, "event": "APPROVE", "body": "hello"},
            {"review": {"id": 3, "state": "APPROVED", "commit_id": SHA}},
        )
        self.assertEqual(detail["result_review_id"], 3)
        self.assertEqual(detail["event"], "APPROVE")
        self.assertEqual(detail["commit_id"], SHA)
        self.assertNotIn("body", detail)
        detail = _workflow_audit_detail(
            {
                "repo": REPO,
                "number": 2,
                "commit_id": SHA,
                "path": "src/a.py",
                "line": 3,
                "side": "RIGHT",
                "body": "hello",
            },
            {"comment": {"id": 4, "commit_id": SHA}},
        )
        self.assertEqual(detail["result_comment_id"], 4)
        self.assertEqual(detail["line"], 3)
        self.assertEqual(detail["side"], "RIGHT")
        self.assertNotIn("body", detail)


class ReviewBranchWiringTests(unittest.TestCase):
    def test_malformed_or_uncertain_write_is_held_without_replay(self):
        for reply in [{}, {"id": True}, RelayGitHubError(503, "private")]:
            with tempfile.TemporaryDirectory() as tmp:
                seats = json.dumps(
                    {
                        "swu-wise": {
                            "token": TOKEN,
                            "actions": ["wf_branch_create"],
                            "repos": [REPO],
                            "app_slug": "swu-wise",
                            "mint_permissions": {"contents": "write", "metadata": "read"},
                        }
                    }
                )
                client = Client()

                def call(method, path, payload=None, bearer=None):
                    client.calls.append((method, path, payload, bearer))
                    if isinstance(reply, Exception):
                        raise reply
                    return reply

                client._call = call
                r = Relay(
                    SeatPolicy(seats),
                    client,
                    AuditLog(os.path.join(tmp, "a.jsonl")),
                    app_minter=Minter(),
                    workflow_verbs_enabled=True,
                )
                args = {"repo": REPO, "branch": "wise/x", "sha": SHA}
                status, body = r.handle(TOKEN, "wf_branch_create", args)
                self.assertEqual(
                    (status, body["outcome"], body["retryable"]), (502, "unknown", False)
                )
                self.assertEqual(r.handle(TOKEN, "wf_branch_create", args)[0], 409)
                self.assertEqual(len(client.calls), 1)
