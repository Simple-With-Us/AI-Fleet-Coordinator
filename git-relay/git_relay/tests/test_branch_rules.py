"""wf_branch_rules keeps rule type and parameters, and fails closed otherwise."""

import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs, project_branch_rules

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy


def _run(reply, args):
    client = Client(reply)
    handle = WorkflowVerbs(client, Policy(), "swu-wise", "private", "swu-wise", "wf_branch_rules", REPO)
    return handle.run("wf_branch_rules", {"repo": REPO, **args}), client


class BranchRules(unittest.TestCase):
    def test_projects_thread_resolution_and_drops_ruleset_source(self):
        out, client = _run(
            [
                {
                    "type": "pull_request",
                    "parameters": {"required_review_thread_resolution": True, "required_approving_review_count": 1},
                    "ruleset_id": 42,
                    "ruleset_source": "Organization",
                    "ruleset_source_type": "Organization",
                }
            ],
            {"branch": "main"},
        )
        self.assertEqual(
            out,
            {
                "rules": [
                    {
                        "type": "pull_request",
                        "parameters": {
                            "required_approving_review_count": 1,
                            "required_review_thread_resolution": True,
                        },
                    }
                ]
            },
        )
        self.assertEqual(client.calls, [("GET", f"/repos/{REPO}/rules/branches/main", None, "private")])
        self.assertNotIn("ruleset", str(out))

    def test_unknown_argument_does_not_call_the_network(self):
        client = Client([{"type": "creation"}])
        handle = WorkflowVerbs(client, Policy(), "swu-wise", "private", "swu-wise", "wf_branch_rules", REPO)
        with self.assertRaises(Refused):
            handle.run("wf_branch_rules", {"repo": REPO, "branch": "main", "url": "https://evil.invalid"})
        self.assertFalse(client.calls)

    def test_refuses_a_rule_it_cannot_project(self):
        secret = "github_pat_" + "a" * 22
        samples = [
            {"not": "a list"},
            [{"type": "creation"}] * 51,
            [{"type": "made_up", "parameters": {}}],
            [{"type": "pull_request", "parameters": {"surprise": True}}],
            [{"type": "commit_message_pattern", "parameters": {"operator": "regex", "pattern": secret}}],
            [None],
        ]
        for sample in samples:
            with self.subTest(sample=type(sample).__name__):
                with self.assertRaises(RelayGitHubError) as caught:
                    project_branch_rules(sample)
                self.assertEqual(caught.exception.status, 502)
                self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
