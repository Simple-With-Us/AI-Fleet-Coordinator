import unittest

from test_workflow_verbs import REPO, Client, Minter, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs

ISSUE = {"number": 2, "state": "open", "title": "x", "html_url": "https://example.invalid/2"}


class IssueTests(unittest.TestCase):
    def run_action(self, action, args, *replies):
        client = Client(*replies)
        handle = WorkflowVerbs.for_action(client, Policy(), Minter(), "swu-wise", action, REPO)
        return handle.run(action, {"repo": REPO, **args}), client

    def test_list_filters_pull_requests_and_marks_partial(self):
        out, c = self.run_action("wf_issue_list", {}, [ISSUE, {**ISSUE, "pull_request": {}}])
        self.assertEqual(len(out["issues"]), 1)
        self.assertFalse(out["complete"])
        self.assertIn("state=open&per_page=30", c.calls[0][1])

    def test_create_uses_own_app_fixed_endpoint(self):
        out, c = self.run_action("wf_issue_create", {"title": "fix", "body": "details"}, ISSUE)
        self.assertEqual(out["issue"]["number"], 2)
        self.assertEqual(
            c.calls[0][:3], ("POST", f"/repos/{REPO}/issues", {"title": "fix", "body": "details"})
        )
        self.assertEqual(c.calls[0][3], "opaque-installation-bearer")

    def test_update_and_comment_refuse_pr_targets_before_write(self):
        for action, args in [
            ("wf_issue_update", {"number": 2, "state": "closed"}),
            ("wf_issue_comment", {"number": 2, "body": "hello"}),
        ]:
            client = Client({**ISSUE, "pull_request": {}})
            h = WorkflowVerbs(client, Policy(), "swu-wise", "opaque", "swu-wise", action, REPO)
            with self.assertRaises(Refused):
                h.run(action, {"repo": REPO, **args})
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0][0], "GET")

    def test_update_state_and_comment(self):
        out, c = self.run_action(
            "wf_issue_update", {"number": 2, "state": "closed"}, ISSUE, {**ISSUE, "state": "closed"}
        )
        self.assertEqual(out["issue"]["state"], "closed")
        self.assertEqual(c.calls[-1][0], "PATCH")
        out, c = self.run_action("wf_issue_comment", {"number": 2, "body": "ok"}, ISSUE, {"id": 3})
        self.assertEqual(out["comment"]["id"], 3)

    def test_bad_shapes_have_no_network_or_mint(self):
        for action, args in [
            ("wf_issue_update", {"number": 2}),
            ("wf_issue_create", {"title": "x", "body": "x", "assignees": []}),
            ("wf_issue_comment", {"number": True, "body": "x"}),
            ("wf_issue_create", {"title": "x", "body": "ghp_" + "a" * 30}),
            ("wf_issue_update", {"number": 2, "state": "deleted"}),
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {"repo": REPO, **args}, REPO)

    def test_issue_ceiling_does_not_borrow_contents_permission(self):
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(
                Client(),
                Policy({"contents": "write", "metadata": "read"}),
                Minter(),
                "swu-wise",
                "wf_issue_create",
                REPO,
            )
        minter = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), minter, "swu-wise", "wf_issue_get", REPO)
        self.assertEqual(minter.calls[0][2], {"issues": "read", "metadata": "read"})

    def test_unverifiable_write_reply_is_unknown_not_refused(self):
        for action, args, replies in [
            ("wf_issue_create", {"title": "x", "body": "details"}, [None]),
            ("wf_issue_update", {"number": 2, "state": "closed"}, [ISSUE, []]),
            ("wf_issue_comment", {"number": 2, "body": "x"}, [ISSUE, {}]),
        ]:
            with self.assertRaises(RuntimeError):
                self.run_action(action, args, *replies)

    def test_broker_write_idempotency_unknown_and_audit(self):
        import json
        import os
        import tempfile

        from git_relay.audit import AuditLog
        from git_relay.outcomes import OutcomeStore
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        token = "w" * 32
        seats = json.dumps(
            {
                "swu-wise": {
                    "token": token,
                    "actions": ["wf_issue_create"],
                    "repos": [REPO],
                    "app_slug": "swu-wise",
                    "mint_permissions": {"issues": "write", "metadata": "read"},
                }
            }
        )
        args = {"repo": REPO, "title": "fix", "body": "details"}
        for reply, expected in [(ISSUE, 200), (None, 500)]:
            with tempfile.TemporaryDirectory() as tmp:
                client = Client(reply)
                r = Relay(
                    SeatPolicy(seats),
                    client,
                    AuditLog(os.path.join(tmp, "audit")),
                    app_minter=Minter(),
                    outcomes=OutcomeStore(os.path.join(tmp, "outcomes")),
                    workflow_verbs_enabled=True,
                )
                status, _body = r.handle(token, "wf_issue_create", args)
                self.assertEqual(status, expected)
                status2, body2 = r.handle(token, "wf_issue_create", args)
                self.assertEqual(status2, 200 if expected == 200 else 409)
                self.assertEqual(len(client.calls), 1)
                if expected != 200:
                    self.assertFalse(body2["retryable"])
                with open(os.path.join(tmp, "audit")) as f:
                    rows = [json.loads(x) for x in f]
                self.assertEqual(len(rows), 2)
                self.assertNotIn("details", json.dumps(rows))
