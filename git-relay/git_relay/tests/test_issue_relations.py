import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy


class Relations(unittest.TestCase):
    def test_fixed_bounded_read_routes(self):
        issue = {
            "id": 7,
            "number": 8,
            "title": "Child",
            "state": "open",
            "repository_url": "https://api.github.com/repos/" + REPO,
        }
        for action, suffix in [
            ("wf_issue_subissues", "sub_issues"),
            ("wf_issue_blocked_by", "dependencies/blocked_by"),
            ("wf_issue_blocking", "dependencies/blocking"),
        ]:
            c = Client([issue])
            out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(
                action, {"repo": REPO, "number": 1, "per_page": 1, "page": 2}
            )
            self.assertEqual(
                c.calls,
                [
                    (
                        "GET",
                        f"/repos/{REPO}/issues/1/{suffix}?per_page=1&page=2",
                        None,
                        "own",
                    )
                ],
            )
            self.assertTrue(out["has_more"])
            self.assertFalse(SPECS[action][1])

    def test_parent_receipt_and_bad_shapes(self):
        a = "wf_issue_parent"
        for body in [
            [],
            None,
            {"id": True, "number": 8, "state": "open", "title": "x"},
            {"id": 1, "number": 8, "state": "open", "title": "x", "pull_request": {}},
        ]:
            with self.assertRaises(RelayGitHubError):
                WorkflowVerbs(Client(body), Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
                    a, {"repo": REPO, "number": 1}
                )

    def test_closed_page_bounds(self):
        for extra in [
            {"page": 0},
            {"page": 1001},
            {"per_page": 51},
            {"url": "https://evil.invalid"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    "wf_issue_subissues", {"repo": REPO, "number": 1, **extra}, REPO
                )

    def test_related_repository_and_oversized_page(self):
        value = {
            "id": 1,
            "number": 2,
            "title": "related",
            "state": "open",
            "repository_url": "https://api.github.com/repos/Other/Repo",
        }
        self.assertEqual(WorkflowVerbs._relation_issue(value)["repo"], "Other/Repo")
        self.assertIsNone(
            WorkflowVerbs._relation_issue(
                {**value, "repository_url": "https://evil.invalid/repos/Other/Repo"}
            )["repo"]
        )
        a = "wf_issue_subissues"
        c = Client([{**value, "repository_url": "https://api.github.com/repos/" + REPO}] * 2)
        out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, {"repo": REPO, "number": 1, "per_page": 1}
        )
        self.assertEqual(len(out["issues"]), 1)
        self.assertTrue(out["has_more"])

    def test_all_positive_shapes_and_title_secret_boundary(self):
        for a in (
            "wf_issue_parent",
            "wf_issue_subissues",
            "wf_issue_blocked_by",
            "wf_issue_blocking",
        ):
            args = {"repo": REPO, "number": 1}
            if a != "wf_issue_parent":
                args.update(page=2, per_page=1)
            self.assertIsNone(WorkflowVerbs.check_shape(a, args, REPO))
        with self.assertRaises(Refused):
            WorkflowVerbs._relation_issue(
                {"id": 1, "number": 2, "state": "open", "title": "ghp_" + "x" * 36}
            )

    def test_foreign_or_unverifiable_relation_refuses(self):
        for url in ("https://api.github.com/repos/Other/Repo", None):
            c = Client(
                [
                    {
                        "id": 1,
                        "number": 2,
                        "title": "private",
                        "state": "open",
                        "repository_url": url,
                    }
                ]
            )
            with self.assertRaises(Refused):
                WorkflowVerbs(
                    c,
                    Policy(),
                    "swu-wise",
                    "own",
                    "swu-wise",
                    "wf_issue_subissues",
                    REPO,
                ).run("wf_issue_subissues", {"repo": REPO, "number": 1})
