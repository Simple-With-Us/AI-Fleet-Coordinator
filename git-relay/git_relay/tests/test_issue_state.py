import tempfile
import unittest
from unittest.mock import patch
from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore
from git_relay.workflow_verbs import Refused, WorkflowVerbs
from .test_workflow_verbs import REPO, Client, Policy, Minter
from .test_workflow_wiring import TOKEN, relay


class State(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "number": 2, "state": "closed", "reason": "not_planned", **kw}

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_issue_state_set", REPO)

    def test_closed_reason_and_exact_receipt(self):
        for reason in ["completed", "not_planned", "duplicate"]:
            c = Client(
                {"id": 7, "number": 2, "state": "open"},
                {
                    "id": 7,
                    "number": 2,
                    "state": "closed",
                    "state_reason": reason,
                    "title": "not returned",
                },
            )
            self.assertEqual(
                self.handle(c).run("wf_issue_state_set", self.args(reason=reason)),
                {"issue_number": 2, "state": "closed", "reason": reason},
            )
            self.assertEqual(
                c.calls[-1],
                (
                    "PATCH",
                    f"/repos/{REPO}/issues/2",
                    {"state": "closed", "state_reason": reason},
                    "own",
                ),
            )
        m = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", "wf_issue_state_set", REPO)
        self.assertEqual(m.calls[0][2], {"issues": "write", "metadata": "read"})

    def test_same_state_mismatch_no_hidden_reopen(self):
        c = Client({"id": 7, "number": 2, "state": "closed", "state_reason": "completed"})
        with self.assertRaises(Refused):
            self.handle(c).run("wf_issue_state_set", self.args())
        self.assertEqual(len(c.calls), 1)
        c = Client({"id": 7, "number": 2, "state": "closed", "state_reason": "not_planned"})
        self.assertEqual(
            self.handle(c).run("wf_issue_state_set", self.args())["reason"], "not_planned"
        )
        self.assertEqual(len(c.calls), 1)

    def test_invalid_or_pr_target_before_write(self):
        for bad in [
            {"state": "open", "reason": "completed"},
            {"reason": "reopened"},
            {"reason": "unknown"},
            {"number": True},
            {"force": True},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_issue_state_set", self.args(**bad), REPO)
        c = Client({"id": 7, "number": 2, "state": "open", "pull_request": {}})
        with self.assertRaises(Refused):
            self.handle(c).run("wf_issue_state_set", self.args())

    def test_ignored_reason_not_success(self):
        c = Client(
            {"id": 7, "number": 2, "state": "open"},
            {"id": 7, "number": 2, "state": "closed", "state_reason": "completed"},
        )
        with self.assertRaises(RelayGitHubError):
            self.handle(c).run("wf_issue_state_set", self.args())

    def test_real_unknown_restart_and_a_b_a(self):
        class Lost(Client):
            def _call(self, method, path, payload=None, bearer=None):
                if method == "PATCH":
                    self.calls.append((method, path, payload, bearer))
                    raise RelayGitHubError(502, "lost")
                return super()._call(method, path, payload, bearer)

        a = self.args(repo="example/x")
        with tempfile.TemporaryDirectory() as tmp:
            c = Lost({"id": 7, "number": 2, "state": "open"})
            r = relay(tmp, client=c)
            path = tmp + "/outcomes.json"
            r.outcomes = OutcomeStore(path)
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"issues": "write", "metadata": "read"}),
                ),
            ):
                status, body = r.handle(TOKEN, "wf_issue_state_set", a)
                self.assertEqual(status, 502)
                self.assertEqual(body["outcome"], "unknown")
                count = len(c.calls)
                r.outcomes = OutcomeStore(path)
                self.assertEqual(r.handle(TOKEN, "wf_issue_state_set", a)[0], 409)
                self.assertEqual(len(c.calls), count)
        replies = []
        for state, reason, before in [
            ("closed", "not_planned", "open"),
            ("open", "reopened", "closed"),
            ("closed", "not_planned", "open"),
        ]:
            replies.extend(
                [
                    {"id": 7, "number": 2, "state": before},
                    {"id": 7, "number": 2, "state": state, "state_reason": reason},
                ]
            )
        with tempfile.TemporaryDirectory() as tmp:
            c = Client(*replies)
            r = relay(tmp, client=c)
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"issues": "write", "metadata": "read"}),
                ),
            ):
                for args in [a, {**a, "state": "open", "reason": "reopened"}, a]:
                    self.assertEqual(r.handle(TOKEN, "wf_issue_state_set", args)[0], 200)
            self.assertEqual(sum(x[0] == "PATCH" for x in c.calls), 3)
