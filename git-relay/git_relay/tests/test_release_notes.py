import tempfile
import unittest
from unittest.mock import patch
from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore
from git_relay.policy import SeatPolicy
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
        self.assertEqual(SPECS["wf_release_notes_preview"], ({"contents": "write"}, True))
        self.assertTrue(SeatPolicy.is_write("wf_release_notes_preview"))
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

    def test_lost_preview_is_held_like_any_contents_write(self):
        class Lost(Client):
            def _call(self, method, path, payload=None, bearer=None):
                if method == "POST" and path.endswith("/releases/generate-notes"):
                    self.calls.append((method, path, payload, bearer))
                    raise RelayGitHubError(502, "unavailable generation")
                return super()._call(method, path, payload, bearer)

        a = self.args(repo="example/x")
        with tempfile.TemporaryDirectory() as tmp:
            c = Lost()
            r = relay(tmp, client=c)
            path = tmp + "/outcomes.json"
            r.outcomes = OutcomeStore(path)
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"contents": "write", "metadata": "read"}),
                ),
            ):
                status, body = r.handle(TOKEN, "wf_release_notes_preview", a)
                self.assertEqual(status, 502)
                self.assertEqual(body["outcome"], "unknown")
                count = len(c.calls)
                r.outcomes = OutcomeStore(path)
                self.assertEqual(r.handle(TOKEN, "wf_release_notes_preview", a)[0], 409)
                self.assertEqual(len(c.calls), count)

    def test_verified_preview_can_be_generated_again(self):
        a = self.args(repo="example/x")
        with tempfile.TemporaryDirectory() as tmp:
            c = Client({"name": "One", "body": "first"}, {"name": "Two", "body": "second"})
            r = relay(tmp, client=c)
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"contents": "write", "metadata": "read"}),
                ),
            ):
                first = r.handle(TOKEN, "wf_release_notes_preview", a)
                second = r.handle(TOKEN, "wf_release_notes_preview", a)
            self.assertEqual(first[1]["preview"]["name"], "One")
            self.assertEqual(second[1]["preview"]["name"], "Two")
