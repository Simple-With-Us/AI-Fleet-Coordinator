import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.server import _workflow_audit_detail
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy


class EnvironmentReads(unittest.TestCase):
    def item(self, **kw):
        return {"id": 7, "name": "staging/eu", "created_at": "x", "updated_at": "x", **kw}

    def test_exact_encoded_route_and_unknown_protection(self):
        a = "wf_environment_get"
        c = Client(self.item())
        out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, {"repo": REPO, "name": "staging/eu"}
        )
        self.assertEqual(
            c.calls, [("GET", f"/repos/{REPO}/environments/staging%2Feu", None, "own")]
        )
        self.assertIsNone(out["environment"]["protection_rule_types"])
        self.assertFalse(SPECS[a][1])

    def test_bounded_page_known_rules(self):
        a = "wf_environments"
        c = Client(
            {
                "total_count": 2,
                "environments": [
                    self.item(protection_rules=[{"type": "required_reviewers"}]),
                    self.item(),
                ],
            }
        )
        out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, {"repo": REPO, "page": 2, "per_page": 1}
        )
        self.assertEqual(len(out["environments"]), 1)
        self.assertFalse(out["has_more"])
        self.assertEqual(out["environments"][0]["protection_rule_types"], ["required_reviewers"])

    def test_malformed_refused(self):
        for extra in [
            {"id": True},
            {"protection_rules": {}},
            {
                "deployment_branch_policy": {
                    "protected_branches": 1,
                    "custom_branch_policies": False,
                }
            },
        ]:
            with self.assertRaises(RelayGitHubError):
                WorkflowVerbs._environment_summary(self.item(**extra))
        for name in ["../x", "a//b", "/x"]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_environment_get", {"repo": REPO, "name": name}, REPO)

    def test_exact_full_last_page_and_credential_names_fail_closed(self):
        item = {"id": 1, "name": "staging", "created_at": "x", "updated_at": "x"}
        a = "wf_environments"
        args = {"repo": REPO, "page": 2, "per_page": 1}
        for total, expected in [(2, False), (3, True)]:
            c = Client({"total_count": total, "environments": [item]})
            result = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(a, args)
            self.assertEqual(result["has_more"], expected)
        with self.assertRaises(Refused):
            WorkflowVerbs._environment_summary({**item, "name": "ghp_" + "x" * 36})

    def test_page_is_audited(self):
        self.assertEqual(_workflow_audit_detail({"page": 2}, {})["page"], 2)
