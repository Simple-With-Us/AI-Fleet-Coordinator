import tempfile
import unittest
from unittest.mock import patch
from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore
from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import Refused, WorkflowVerbs
from .test_workflow_verbs import REPO, Client, Policy, Minter
from .test_workflow_wiring import TOKEN, relay


class IssueMilestone(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "number": 2, "milestone_number": 3, **kw}

    def handle(self, c):
        return WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_issue_milestone_set", REPO
        )

    def test_set_exact_id_receipt_and_least_permission(self):
        c = Client(
            {"id": 7, "number": 2},
            {"id": 8, "number": 3},
            {"id": 7, "number": 2, "milestone": {"id": 8, "number": 3}, "title": "not returned"},
        )
        self.assertEqual(
            self.handle(c).run("wf_issue_milestone_set", self.args()),
            {"issue_number": 2, "milestone_number": 3},
        )
        self.assertEqual(c.calls[-1], ("PATCH", f"/repos/{REPO}/issues/2", {"milestone": 3}, "own"))
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_issue_milestone_set", REPO)
        self.assertEqual(m.calls[0][2], {"issues": "write", "metadata": "read"})
        self.assertIn("wf_issue_milestone_set", _REAPPLY_STATE_ACTIONS)

    def test_remove_requires_explicit_null(self):
        c = Client({"id": 7, "number": 2}, {"id": 7, "number": 2, "milestone": None})
        self.assertEqual(
            self.handle(c).run("wf_issue_milestone_set", self.args(milestone_number=0)),
            {"issue_number": 2, "milestone_number": None},
        )
        self.assertEqual(len(c.calls), 2)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_issue_milestone_set", {"repo": REPO, "number": 2}, REPO)

    def test_closed_pr_and_wrong_identity_preread(self):
        for bad in [
            {"milestone_number": True},
            {"milestone_number": -1},
            {"number": True},
            {"force": True},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_issue_milestone_set", self.args(**bad), REPO)
        for issue in [
            {"id": 7, "number": 2, "pull_request": {}},
            {"id": 7, "number": 3},
            {"id": True, "number": 2},
        ]:
            c = Client(issue)
            with self.assertRaises(Refused):
                self.handle(c).run("wf_issue_milestone_set", self.args())
            self.assertEqual(len(c.calls), 1)

    def test_silently_ignored_update_is_not_success(self):
        for actual in [None, {"id": 8, "number": 4}, {"id": 9, "number": 3}]:
            c = Client(
                {"id": 7, "number": 2},
                {"id": 8, "number": 3},
                {"id": 7, "number": 2, "milestone": actual},
            )
            with self.assertRaises(RelayGitHubError):
                self.handle(c).run("wf_issue_milestone_set", self.args())

    def test_real_unknown_restart_and_verified_success_set_remove_set(self):
        class Lost(Client):
            def _call(self, method, path, payload=None, bearer=None):
                if method == "PATCH":
                    self.calls.append((method, path, payload, bearer))
                    raise RelayGitHubError(502, "lost")
                return super()._call(method, path, payload, bearer)

        a = self.args(repo="example/x")
        with tempfile.TemporaryDirectory() as tmp:
            c = Lost({"id": 7, "number": 2}, {"id": 8, "number": 3})
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
                status, body = r.handle(TOKEN, "wf_issue_milestone_set", a)
                self.assertEqual(status, 502)
                self.assertEqual(body["outcome"], "unknown")
                count = len(c.calls)
                r.outcomes = OutcomeStore(path)
                status, body = r.handle(TOKEN, "wf_issue_milestone_set", a)
                self.assertEqual(status, 409)
                self.assertEqual(len(c.calls), count)
        replies = []
        for n in [3, None, 3]:
            replies.append({"id": 7, "number": 2})
            if n is not None:
                replies.append({"id": 8, "number": 3})
            replies.append(
                {"id": 7, "number": 2, "milestone": None if n is None else {"id": 8, "number": 3}}
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
                for n in [3, None, 3]:
                    self.assertEqual(
                        r.handle(
                            TOKEN,
                            "wf_issue_milestone_set",
                            {**a, "milestone_number": 0 if n is None else n},
                        )[0],
                        200,
                    )
            self.assertEqual(sum(x[0] == "PATCH" for x in c.calls), 3)
