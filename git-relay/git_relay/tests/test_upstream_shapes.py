import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import WorkflowVerbs, _arr, _dicts, _obj, _sub


class Shapes(unittest.TestCase):
    def test_upstream_shapes_are_never_definitely_unsent_refusals(self):
        for fn, args in [
            (_obj, ([],)),
            (_sub, ({"head": []}, "head")),
            (_arr, ({},)),
            (_dicts, ([1],)),
        ]:
            with self.assertRaises(RelayGitHubError) as error:
                fn(*args)
            self.assertEqual(error.exception.status, 502)

    def test_file_write_malformed_receipt_stays_unknown(self):
        c = Client({"default_branch": "main"}, [])
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_file_put", REPO)
        with self.assertRaises(RelayGitHubError) as error:
            h.run(
                "wf_file_put",
                {
                    "repo": REPO,
                    "path": "x.txt",
                    "branch": "feature",
                    "message": "edit",
                    "content_b64": "aGk=",
                },
            )
        self.assertEqual(error.exception.status, 502)
