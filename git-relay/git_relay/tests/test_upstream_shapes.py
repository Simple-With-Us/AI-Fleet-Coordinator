import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import WorkflowVerbs, _arr, _dicts, _obj, _sub


class Shapes(unittest.TestCase):
    def test_upstream_shapes_are_never_definitely_unsent_refusals(self):
        for fn, args in [
            (_obj, ([],)),
            (_sub, ({"head": []}, "head")),
            (_sub, ([], "head")),
            (_sub, (None, "head")),
            (_arr, ({},)),
            (_dicts, ([1],)),
        ]:
            with self.assertRaises(RelayGitHubError) as error:
                fn(*args)
            self.assertEqual(error.exception.status, 502)

    def test_file_write_malformed_receipt_stays_unknown(self):
        # The replies are, in order:  the repository, the provider's own record of the branch (a
        # valid one, so the pre-read passes and the write is attempted), then the malformed write
        # receipt.  Without the second reply the pre-read would consume the malformed one and
        # refuse before any write, which is a different case with a different status.
        c = Client({"default_branch": "main"}, {"name": "feature", "commit": {"sha": SHA}}, [])
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
        self.assertEqual(c.calls[-1][0], "PUT")

    def test_malformed_review_preread_is_definitely_unsent(self):
        from git_relay.server import PreReadUnavailable

        for payload in (
            {},
            {"head": None},
            {"head": []},
            {"head": {}},
            {"head": {"sha": None}},
            {"head": {"sha": "bad"}},
        ):
            c = Client(payload)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_review_submit", REPO)
            with self.assertRaises(PreReadUnavailable):
                h.run(
                    "wf_review_submit",
                    {
                        "repo": REPO,
                        "number": 1,
                        "commit_id": SHA,
                        "event": "COMMENT",
                        "body": "Review",
                    },
                )
            self.assertEqual(len(c.calls), 1)
