import unittest
from git_relay.workflow_verbs import WorkflowVerbs, Refused
from git_relay.github_client import RelayGitHubError
from .test_workflow_verbs import REPO, Client, Policy, Minter


class Metadata(unittest.TestCase):
    def runverb(self, c, a, args):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(a, args)

    def test_tags_and_languages_fixed_routes(self):
        c = Client([{"name": "v1", "commit": {"sha": "a" * 40}, "zipball_url": "omit"}] * 2)
        out = self.runverb(c, "wf_repo_tags", {"repo": REPO, "page": 2, "per_page": 1})
        self.assertEqual(
            out, {"tags": [{"name": "v1", "sha": "a" * 40}], "page": 2, "has_more": True}
        )
        self.assertEqual(c.calls, [("GET", f"/repos/{REPO}/tags?per_page=1&page=2", None, "own")])
        c = Client({"Python": 8, "C++": 2})
        self.assertEqual(
            self.runverb(c, "wf_repo_languages", {"repo": REPO}),
            {"languages": [{"name": "C++", "bytes": 2}, {"name": "Python", "bytes": 8}]},
        )

    def test_bounds_types_and_credential_names(self):
        for value in [
            {"Python": True},
            {"Python": -1},
            {"x": 9007199254740992},
            [],
            {str(i): 1 for i in range(101)},
        ]:
            with self.assertRaises(RelayGitHubError):
                self.runverb(Client(value), "wf_repo_languages", {"repo": REPO})
        with self.assertRaises(Refused):
            self.runverb(Client({"ghp_" + "x" * 36: 1}), "wf_repo_languages", {"repo": REPO})
        with self.assertRaises(RelayGitHubError):
            self.runverb(Client([{}]), "wf_repo_tags", {"repo": REPO})

    def test_positive_closed_shapes_and_least_permissions(self):
        for a, perms in [
            ("wf_repo_tags", {"contents": "read", "metadata": "read"}),
            ("wf_repo_languages", {"metadata": "read"}),
        ]:
            self.assertIsNone(WorkflowVerbs.check_shape(a, {"repo": REPO}, REPO))
            m = Minter()
            WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", a, REPO)
            self.assertEqual(m.calls[0][2], perms)
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(a, {"repo": REPO, "url": "x"}, REPO)
