import tempfile
import unittest
from unittest.mock import patch

from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore, key_for
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Minter, Policy
from .test_workflow_verbs import Client as BaseClient
from .test_workflow_wiring import TOKEN, relay


class Client(BaseClient):
    def _call(self, method, path, payload=None, bearer=None):
        if method == "GET" and "/labels/" in path:
            from urllib.parse import unquote

            name = unquote(path.rsplit("/", 1)[1])
            return {"id": 7 if name.casefold() == "hold" else 8, "name": name}
        return super()._call(method, path, payload, bearer)


class _IssuesOff(BaseClient):
    """The label exists, and POST /issues/.../labels is the 404 Issues-disabled route."""

    def __init__(
        self, graphql, missing_pr=False, lose_mutation=False, lose_read=False, node_id="LA_kwDOAbc"
    ):
        super().__init__()
        self.graphql = graphql
        self.missing_pr = missing_pr
        self.lose_mutation = lose_mutation
        self.lose_read = lose_read
        self.node_id = node_id

    def _call(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path, payload, bearer))
        if method == "GET" and "/labels/" in path:
            row = {"id": 7, "name": path.rsplit("/", 1)[1]}
            if self.node_id is not None:
                row["node_id"] = self.node_id
            return row
        if method == "POST" and path.endswith("/labels"):
            raise RelayGitHubError(404, "Not Found")
        if method == "GET" and "/pulls/" in path:
            if self.missing_pr:
                raise RelayGitHubError(404, "Not Found")
            return {"node_id": "PR_kwDOAbc", "number": 2, "state": "open"}
        query = "" if not isinstance(payload, dict) else str(payload.get("query") or "")
        if query.startswith("mutation"):
            if self.lose_mutation:
                raise RelayGitHubError(502, "lost")
            return self.graphql
        if query.startswith("query"):
            if self.lose_read:
                raise RelayGitHubError(502, "lost")
            return self.graphql
        raise AssertionError((method, path))


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
                if method == "GET":
                    return super()._call(method, path, payload, bearer)
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

    def test_unknown_label_or_changed_identity_does_not_succeed(self):
        for pre in ({}, {"id": 0, "name": "hold"}, {"id": 7, "name": "other"}):
            c = BaseClient(pre)
            with self.assertRaises(Refused):
                self.handle(c).run("wf_labels_add", self.args())
            self.assertTrue(all(x[0] == "GET" for x in c.calls))
        c = BaseClient({"id": 8, "name": "hold"}, [{"id": 7, "name": "hold", "color": "000000"}])
        with self.assertRaises(RelayGitHubError):
            self.handle(c).run("wf_labels_add", self.args())

    def test_transient_existence_read_is_unsent_and_retryable(self):
        from git_relay.workflow_verbs import PreReadUnavailable

        class Down(BaseClient):
            def _call(self, method, path, payload=None, bearer=None):
                raise RelayGitHubError(502, "unavailable")

        with self.assertRaises(PreReadUnavailable):
            self.handle(Down()).run("wf_labels_add", self.args())

    def test_missing_label_is_a_definite_refusal_and_can_be_retried(self):
        class Missing(BaseClient):
            def _call(self, method, path, payload=None, bearer=None):
                self.calls.append((method, path, payload, bearer))
                raise RelayGitHubError(404, "Not Found")

        with self.assertRaises(Refused) as caught:
            self.handle(Missing()).run("wf_labels_add", self.args(labels=["needs-human-merge"]))
        self.assertEqual(str(caught.exception), "additive label does not exist")
        args = self.args(repo="example/x", labels=["needs-human-merge"])
        with tempfile.TemporaryDirectory() as tmp:
            c = Missing()
            r = relay(tmp, client=c)
            path = tmp + "/outcomes.json"
            with (
                patch.object(r.policy, "authorize", return_value=None),
                patch.object(
                    r.policy,
                    "app_identity",
                    return_value=("swu-wise", {"issues": "write", "metadata": "read"}),
                ),
            ):
                status, body = r.handle(TOKEN, "wf_labels_add", args)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "additive label does not exist")
                r.outcomes = OutcomeStore(path)
                self.assertEqual(r.handle(TOKEN, "wf_labels_add", args)[0], 400)
        self.assertTrue(all(call[0] == "GET" for call in c.calls))
        self.assertEqual(len(c.calls), 2)

    def test_issues_route_404_uses_the_pull_request_mutation(self):
        node = "LA_kwDOAbc"
        applied = {
            "data": {
                "addLabelsToLabelable": {
                    "labelable": {
                        "__typename": "PullRequest",
                        "number": 2,
                        "labels": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": node, "name": "needs-human-merge", "color": "FBCA04"}],
                        },
                    }
                }
            }
        }
        c = _IssuesOff(applied)
        out = self.handle(c).run("wf_labels_add", self.args(labels=["needs-human-merge"]))
        self.assertEqual(
            out,
            {
                "number": 2,
                "added": [{"id": 7, "name": "needs-human-merge", "color": "fbca04"}],
            },
        )
        self.assertEqual(
            [call[0] for call in c.calls],
            ["GET", "POST", "GET", "POST"],
        )
        self.assertIn("addLabelsToLabelable", c.calls[3][2]["query"])
        self.assertEqual(c.calls[3][2]["variables"]["labelIds"], [node])

    def test_issues_route_404_without_a_label_node_id_does_not_guess(self):
        c = _IssuesOff(None, node_id=None)
        with self.assertRaises(Refused) as caught:
            self.handle(c).run("wf_labels_add", self.args())
        self.assertEqual(str(caught.exception), "additive label was not applied")
        self.assertTrue(all(call[1] != "/graphql" for call in c.calls))

    def test_issues_route_404_on_a_missing_pull_request_names_the_target(self):
        c = _IssuesOff(None, missing_pr=True)
        with self.assertRaises(Refused) as caught:
            self.handle(c).run("wf_labels_add", self.args())
        self.assertEqual(str(caught.exception), "additive label target not found")
        self.assertTrue(all(call[1] != "/graphql" for call in c.calls))

    def test_uncertain_label_mutation_is_settled_by_one_read(self):
        present = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "labels": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "LA_kwDOAbc", "name": "hold", "color": "aabbcc"}],
                        }
                    }
                }
            }
        }
        absent = {
            "data": {
                "repository": {
                    "pullRequest": {"labels": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
                }
            }
        }
        applied = self.handle(_IssuesOff(present, lose_mutation=True)).run(
            "wf_labels_add", self.args()
        )
        self.assertEqual(applied["added"][0]["id"], 7)
        with self.assertRaises(Refused) as caught:
            self.handle(_IssuesOff(absent, lose_mutation=True)).run("wf_labels_add", self.args())
        self.assertEqual(str(caught.exception), "additive label was not applied")
        lost = _IssuesOff(None, lose_mutation=True, lose_read=True)
        with self.assertRaises(RelayGitHubError):
            self.handle(lost).run("wf_labels_add", self.args())
        mutations = [
            call
            for call in lost.calls
            if call[2] and "addLabelsToLabelable" in str(call[2].get("query"))
        ]
        self.assertEqual(len(mutations), 1)

    def test_label_path_dot_segments_refuse_before_read(self):
        for name in (".", "..", "a/../b"):
            c = Client()
            with self.assertRaises(Refused):
                self.handle(c).run("wf_labels_add", self.args(labels=[name]))
            self.assertEqual(c.calls, [])
