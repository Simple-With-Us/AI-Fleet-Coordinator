import tempfile
import unittest
from unittest.mock import patch

from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore, key_for
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Minter, Policy
from .test_workflow_wiring import TOKEN, relay


class AddLabels(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "number": 2, "labels": ["hold"], **kw}

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_labels_add", REPO)

    def test_post_preserves_unrelated_labels_returns_requested_only(self):
        c = Client(
            [
                {"id": 7, "name": "hold", "color": "AABBcc"},
                {
                    "id": 8,
                    "name": "other lane",
                    "color": "000000",
                    "description": "private-unneeded",
                },
            ]
        )
        out = self.handle(c).run("wf_labels_add", self.args())
        self.assertEqual(
            out, {"number": 2, "added": [{"id": 7, "name": "hold", "color": "aabbcc"}]}
        )
        self.assertEqual(
            c.calls, [("POST", f"/repos/{REPO}/issues/2/labels", {"labels": ["hold"]}, "own")]
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_labels_add", REPO)
        self.assertEqual(m.calls[0][2], {"issues": "write", "metadata": "read"})

    def test_closed_duplicates_and_bounds(self):
        for bad in [
            {"labels": []},
            {"labels": ["hold", "HOLD"]},
            {"labels": [" "]},
            {"labels": ["x"] * 21},
            {"labels": [{}]},
            {"force": True},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_labels_add", self.args(**bad), REPO)

    def test_missing_or_malformed_requested_label_not_success(self):
        for value in [
            [],
            [{"id": 7, "name": "other", "color": "000000"}],
            [{"id": True, "name": "hold", "color": "000000"}],
            [{"id": 7, "name": "hold", "color": "wrong"}],
            [{"id": 7, "name": "hold", "color": "000000"}] * 2,
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(value)).run("wf_labels_add", self.args())

    def test_real_unknown_reordered_case_changed_no_replay(self):
        class Lost(Client):
            def _call(self, method, path, payload=None, bearer=None):
                self.calls.append((method, path, payload, bearer))
                raise RelayGitHubError(502, "lost")

        a = self.args(repo="example/x", labels=["hold", "review"])
        other = {**a, "labels": ["REVIEW", "Hold"]}
        self.assertEqual(
            key_for("swu-wise", "wf_labels_add", a), key_for("swu-wise", "wf_labels_add", other)
        )
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
                    return_value=("swu-wise", {"issues": "write", "metadata": "read"}),
                ),
            ):
                status, body = r.handle(TOKEN, "wf_labels_add", a)
                self.assertEqual(status, 502)
                self.assertEqual(body["outcome"], "unknown")
                r.outcomes = OutcomeStore(path)
                self.assertEqual(r.handle(TOKEN, "wf_labels_add", other)[0], 409)
                self.assertEqual(len(c.calls), 1)

    def test_verified_success_reapplies_if_label_removed_externally(self):
        row = [{"id": 7, "name": "hold", "color": "000000"}]
        with tempfile.TemporaryDirectory() as tmp:
            c = Client(row, row)
            r = relay(tmp, client=c)
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"issues": "write", "metadata": "read"}),
                ),
            ):
                for _ in range(2):
                    self.assertEqual(
                        r.handle(TOKEN, "wf_labels_add", self.args(repo="example/x"))[0], 200
                    )
            self.assertEqual(len(c.calls), 2)
