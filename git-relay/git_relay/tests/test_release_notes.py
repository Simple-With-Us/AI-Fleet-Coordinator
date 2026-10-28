import tempfile
import unittest
from unittest.mock import patch
from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore
from git_relay.workflow_verbs import Refused, SPECS, WorkflowVerbs
from .test_workflow_verbs import REPO, SHA, Client, Policy, Minter
from .test_workflow_wiring import relay, TOKEN


class Notes(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "tag": "v2.0", "target_sha": SHA, "previous_tag": "v1.0", **kw}

    def handle(self, c):
        return WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_release_notes_preview", REPO
        )

    def test_fixed_nonmutating_endpoint_least_permission(self):
        c = Client({"name": "Release2", "body": "Changes", "url": "unused"})
        out = self.handle(c).run("wf_release_notes_preview", self.args())
        self.assertEqual(out, {"preview": {"name": "Release2", "body": "Changes"}, "saved": False})
        self.assertEqual(
            c.calls,
            [
                (
                    "POST",
                    f"/repos/{REPO}/releases/generate-notes",
                    {"tag_name": "v2.0", "target_commitish": SHA, "previous_tag_name": "v1.0"},
                    "own",
                )
            ],
        )
        self.assertEqual(SPECS["wf_release_notes_preview"], ({"contents": "write"}, False))
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_release_notes_preview", REPO)
        self.assertEqual(m.calls[0][2], {"contents": "write", "metadata": "read"})

    def test_closed_range_and_no_configuration_path(self):
        for bad in [
            {"tag": "v1.0"},
            {"tag": "refs/tags/v2.0"},
            {"configuration_file_path": "evil.yml"},
            {"target_sha": True},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_release_notes_preview", self.args(**bad), REPO)

    def test_untrusted_output_bounded_scanned_not_truncated(self):
        # Synthetic assembled scanner fixtures, never credentials.
        for body in ["ghp_" + "x" * 36, "sk-proj-" + "x" * 24, "é" * 16385]:
            with self.assertRaises(Refused):
                self.handle(Client({"name": "x", "body": body})).run(
                    "wf_release_notes_preview", self.args()
                )
        for value in [{}, {"name": 1, "body": ""}, {"name": "x", "body": "\ud800"}]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(value)).run("wf_release_notes_preview", self.args())

    def test_real_relay_post_does_not_create_outcome_hold(self):
        class Down(Client):
            def _call(self, *a, **kw):
                raise RelayGitHubError(502, "unavailable generation")

        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp, client=Down())
            r.outcomes = OutcomeStore(tmp + "/outcomes.json")
            with patch.object(r.policy, "authorize", return_value=None):
                status, body = r.handle(
                    TOKEN, "wf_release_notes_preview", self.args(repo="example/x")
                )
                self.assertEqual(status, 502)
                self.assertTrue(body["retryable"])
                self.assertNotIn("outcome", body)
                self.assertEqual(len(r.outcomes._rows), 0)
