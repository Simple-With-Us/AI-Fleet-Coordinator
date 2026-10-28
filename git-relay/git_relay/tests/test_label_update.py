import tempfile
import unittest
from unittest.mock import patch
from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore, key_for
from git_relay.server import PreReadUnavailable, _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import Refused, SPECS, WorkflowVerbs
from .test_workflow_verbs import REPO, Client, Policy, Minter
from .test_workflow_wiring import TOKEN, relay


class LabelUpdate(unittest.TestCase):
    def args(self, **kw):
        return {
            "repo": REPO,
            "name": "old/label",
            "expected_label_id": 7,
            "new_name": "New label",
            "color": "AaBbee",
            "description": "Description",
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_label_update", REPO)

    def test_exact_route_receipt_and_permission(self):
        c = Client(
            {"id": 7, "name": "old/label"},
            {
                "id": 7,
                "name": "New label",
                "color": "aabbee",
                "description": "Description",
                "url": "private-unneeded",
            },
        )
        out = self.handle(c).run("wf_label_update", self.args())
        self.assertEqual(
            out,
            {
                "label": {
                    "id": 7,
                    "name": "New label",
                    "color": "aabbee",
                    "description": "Description",
                }
            },
        )
        self.assertEqual(
            c.calls,
            [
                ("GET", f"/repos/{REPO}/labels/old%2Flabel", None, "own"),
                (
                    "PATCH",
                    f"/repos/{REPO}/labels/old%2Flabel",
                    {"new_name": "New label", "color": "AaBbee", "description": "Description"},
                    "own",
                ),
            ],
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_label_update", REPO)
        self.assertEqual(m.calls[0][2], {"issues": "write", "metadata": "read"})
        self.assertEqual(SPECS["wf_label_update"], ({"issues": "write"}, True))
        self.assertIn("wf_label_update", _REAPPLY_STATE_ACTIONS)

    def test_preflight_closed_bounded_scanned(self):
        # Synthetic assembled credential fixtures only, never actual credentials.
        for bad in [
            {"archived": True},
            {"expected_label_id": True},
            {"name": " "},
            {"new_name": "x\n"},
            {"description": "é" * 51},
            {"new_name": "\ud800"},
            {"new_name": "ghp_" + "x" * 36},
            {"description": "sk-proj-" + "x" * 24},
            {"color": "#aa0011"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_label_update", self.args(**bad), REPO)
        self.assertIsNone(
            WorkflowVerbs.check_shape("wf_label_update", self.args(description=""), REPO)
        )

    def test_identity_mismatch_no_write(self):
        for old in [
            {},
            {"id": True, "name": "old/label"},
            {"id": 8, "name": "old/label"},
            {"id": 7, "name": "wrong"},
        ]:
            c = Client(old)
            with self.assertRaises(Refused):
                self.handle(c).run("wf_label_update", self.args())
            self.assertEqual(len(c.calls), 1)

    def test_transient_preread_definitely_unsent(self):
        class Down(Client):
            def _call(self, *args, **kw):
                raise RelayGitHubError(502, "unavailable")

        with self.assertRaises(PreReadUnavailable):
            self.handle(Down()).run("wf_label_update", self.args())

    def test_malformed_success_unknown(self):
        for bad in [
            {},
            {"id": 7, "name": "wrong", "color": "aabbee", "description": "Description"},
            {"id": 7, "name": "New label", "color": "000000", "description": "Description"},
            {"id": 7, "name": "New label", "color": "aabbee", "description": None},
        ]:
            c = Client({"id": 7, "name": "old/label"}, bad)
            with self.assertRaises(RelayGitHubError) as err:
                self.handle(c).run("wf_label_update", self.args())
            self.assertEqual(err.exception.status, 502)

    def test_real_durable_unknown_renamed_lookup_no_replay(self):
        class Lost(Client):
            def _call(self, method, path, payload=None, bearer=None):
                if method == "PATCH":
                    self.calls.append((method, path, payload, bearer))
                    raise RelayGitHubError(502, "lost update receipt")
                return super()._call(method, path, payload, bearer)

        client = Lost({"id": 7, "name": "old/label"})
        a = self.args(repo="example/x")
        renamed = {**a, "name": "New label"}
        self.assertEqual(
            key_for("swu-wise", "wf_label_update", a),
            key_for("swu-wise", "wf_label_update", renamed),
        )
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp, client=client)
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
                status, body = r.handle(TOKEN, "wf_label_update", a)
                self.assertEqual(status, 502)
                self.assertEqual(body["outcome"], "unknown")
                self.assertFalse(body["retryable"])
                count = len(client.calls)
                r.outcomes = OutcomeStore(path)
                status, body = r.handle(TOKEN, "wf_label_update", renamed)
                self.assertEqual(status, 409)
                self.assertEqual(len(client.calls), count)

    def test_verified_success_allows_a_b_a_without_releasing_unknown(self):
        a = self.args(repo="example/x", name="stable", new_name="stable", color="111111")
        b = {**a, "color": "222222"}
        replies = []
        for args in [a, b, a]:
            replies.extend(
                [
                    {"id": 7, "name": "stable"},
                    {
                        "id": 7,
                        "name": "stable",
                        "color": args["color"],
                        "description": args["description"],
                    },
                ]
            )
        c = Client(*replies)
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp, client=c)
            r.outcomes = OutcomeStore(tmp + "/outcomes.json")
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"issues": "write", "metadata": "read"}),
                ),
            ):
                for args in [a, b, a]:
                    status, body = r.handle(TOKEN, "wf_label_update", args)
                    self.assertEqual(status, 200)
                    self.assertEqual(body["label"]["color"], args["color"])
            self.assertEqual(sum(x[0] == "PATCH" for x in c.calls), 3)
