import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Minter, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Queue(unittest.TestCase):
    def handle(self, c, a):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)

    def test_fixed_mutations_own_bearer_and_atomic_enqueue_fence(self):
        for a, field in [
            ("wf_merge_queue_enqueue", "enqueuePullRequest"),
            ("wf_merge_queue_dequeue", "dequeuePullRequest"),
        ]:
            c = Client(
                {"node_id": "PR1", "head": {"sha": SHA}, "state": "open"},
                {"data": {field: {"mergeQueueEntry": {"id": "MQ1", "pullRequest": {"id": "PR1"}}}}},
            )
            self.assertEqual(
                self.handle(c, a).run(a, {"repo": REPO, "number": 1, "expected_sha": SHA})["entry"][
                    "id"
                ],
                "MQ1",
            )
            self.assertTrue(all(x[3] == "own" for x in c.calls))
            self.assertEqual(c.calls[1][1], "/graphql")
            if a.endswith("enqueue"):
                self.assertIn("expectedHeadOid:$sha,jump:false", c.calls[1][2]["query"])
                self.assertEqual(c.calls[1][2]["variables"]["sha"], SHA)

    def test_changed_head_refuses_before_mutation(self):
        for a in ("wf_merge_queue_enqueue", "wf_merge_queue_dequeue"):
            c = Client({"node_id": "PR1", "head": {"sha": "b" * 40}, "state": "open"})
            with self.assertRaises(Refused):
                self.handle(c, a).run(a, {"repo": REPO, "number": 1, "expected_sha": SHA})
            self.assertEqual(len(c.calls), 1)
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    a, {"repo": REPO, "number": 1, "expected_sha": SHA, "jump": True}, REPO
                )

    def test_graphql_partial_errors_hold_unknown(self):
        c = Client(
            {"node_id": "PR1", "head": {"sha": SHA}, "state": "open"},
            {"data": {"enqueuePullRequest": {}}, "errors": [{"message": "forbidden"}]},
        )
        with self.assertRaises(RelayGitHubError):
            self.handle(c, "wf_merge_queue_enqueue").run(
                "wf_merge_queue_enqueue", {"repo": REPO, "number": 1, "expected_sha": SHA}
            )

    def test_read_none_and_closed_projection(self):
        for entry in [
            None,
            {
                "id": "MQ1",
                "state": "QUEUED",
                "position": 1,
                "jump": False,
                "enqueuedAt": "2026-10-28T00:00:00Z",
                "private": "omit",
            },
        ]:
            c = Client(
                {"data": {"repository": {"pullRequest": {"id": "PR1", "mergeQueueEntry": entry}}}}
            )
            out = self.handle(c, "wf_merge_queue_entry").run(
                "wf_merge_queue_entry", {"repo": REPO, "number": 1}
            )
            self.assertNotIn("private", str(out))

    def test_own_permissions(self):
        m = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", "wf_merge_queue_enqueue", REPO)
        self.assertEqual(
            m.calls[0][2], {"contents": "write", "pull_requests": "write", "metadata": "read"}
        )

    def test_graphql_transport_on_mutation_is_outcome_unverifiable(self):
        class GraphQLDown(Client):
            def __init__(self, status):
                super().__init__(
                    {"node_id": "PR1", "head": {"sha": SHA}, "state": "open"}
                )
                self.graphql_status = status

            def _call(self, method, path, payload=None, bearer=None):
                if path == "/graphql":
                    self.calls.append((method, path, payload, bearer))
                    raise RelayGitHubError(self.graphql_status, "upstream")
                return super()._call(method, path, payload, bearer)

        for status in (0, 408, 429, 502):
            with self.subTest(status=status):
                c = GraphQLDown(status)
                with self.assertRaises(RelayGitHubError) as error:
                    self.handle(c, "wf_merge_queue_enqueue").run(
                        "wf_merge_queue_enqueue",
                        {"repo": REPO, "number": 1, "expected_sha": SHA},
                    )
                self.assertEqual(error.exception.status, 502)
                self.assertIn("GraphQL outcome unverifiable", str(error.exception))
                self.assertEqual(len(c.calls), 2)

    def test_null_or_malformed_postmutation_receipt_is_unknown(self):
        for value in (None, [], {}, "bad"):
            c = Client(
                {"node_id": "PR1", "head": {"sha": SHA}, "state": "open"},
                {"data": {"enqueuePullRequest": {"mergeQueueEntry": value}}},
            )
            with self.assertRaises(RelayGitHubError) as error:
                self.handle(c, "wf_merge_queue_enqueue").run(
                    "wf_merge_queue_enqueue", {"repo": REPO, "number": 1, "expected_sha": SHA}
                )
            self.assertEqual(error.exception.status, 502)
            self.assertEqual(len(c.calls), 2)

    def test_thread_read_bounded_and_cursor_forwarded(self):
        c = Client(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            }
        )
        self.handle(c, "wf_review_threads").run(
            "wf_review_threads", {"repo": REPO, "number": 1, "cursor": "abc="}
        )
        self.assertIn("reviewThreads(first:5, after:$cursor)", c.calls[0][2]["query"])
        self.assertIn("comments(first:2)", c.calls[0][2]["query"])
        self.assertEqual(c.calls[0][2]["variables"]["cursor"], "abc=")
