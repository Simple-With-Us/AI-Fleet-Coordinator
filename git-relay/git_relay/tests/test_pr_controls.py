import json
import tempfile
import unittest
from pathlib import Path

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class PrControls(unittest.TestCase):
    def test_fixed_graphql_and_repo_preread(self):
        for action, field in [
            ("wf_pr_draft", "convertPullRequestToDraft"),
            ("wf_pr_ready", "markPullRequestReadyForReview"),
            ("wf_automerge_disable", "disablePullRequestAutoMerge"),
            ("wf_automerge_enable", "enablePullRequestAutoMerge"),
        ]:
            pull = {
                "id": "PR_abc",
                "isDraft": action == "wf_pr_draft",
                "autoMergeRequest": {"enabledAt": "now"}
                if action == "wf_automerge_enable"
                else None,
            }
            c = Client({"node_id": "PR_abc"}, {"data": {field: {"pullRequest": pull}}})
            h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            args = {
                "repo": REPO,
                "number": 1,
                **({"method": "SQUASH"} if action == "wf_automerge_enable" else {}),
            }
            self.assertEqual(h.run(action, args), {"pull": pull})
            self.assertEqual(c.calls[0][1], "/repos/" + REPO + "/pulls/1")
            self.assertEqual(c.calls[1][1], "/graphql")
            self.assertEqual(c.calls[1][2]["variables"]["id"], "PR_abc")
            self.assertEqual(c.calls[1][3], "private")
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {**args, "query": "arbitrary"}, REPO)

    def test_graphql_errors_are_unknown_not_definitely_unsent(self):
        c = Client({"node_id": "PR_abc"}, {"data": {}, "errors": [{"message": "private"}]})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", "wf_pr_ready", REPO)
        with self.assertRaises(RelayGitHubError) as error:
            h.run("wf_pr_ready", {"repo": REPO, "number": 1})
        self.assertEqual(error.exception.status, 502)

    def test_graphql_error_types_are_bounded_without_reflected_prose(self):
        for kind in ("FORBIDDEN", "NOT_FOUND", "UNPROCESSABLE", "RATE_LIMITED"):
            with self.subTest(kind=kind):
                c = Client(
                    {"node_id": "PR_abc"},
                    {
                        "data": {
                            "markPullRequestReadyForReview": {
                                "pullRequest": {"id": "PR_abc", "isDraft": False}
                            }
                        },
                        "errors": [
                            {
                                "type": kind,
                                "message": "private provider text",
                                "extensions": {"token": "private provider text"},
                            }
                        ],
                    },
                )
                h = WorkflowVerbs(
                    c, Policy(), "swu-wise", "private", "swu-wise", "wf_pr_ready", REPO
                )
                with self.assertRaises(RelayGitHubError) as error:
                    h.run("wf_pr_ready", {"repo": REPO, "number": 1})
                self.assertEqual(error.exception.status, 502)
                self.assertEqual(error.exception.code, "graphql_" + kind.lower())
                self.assertNotIn("private provider text", str(error.exception))

    def test_graphql_malformed_mixed_and_hostile_types_are_omitted(self):
        for errors in (
            [{"type": "private provider text"}],
            [{"type": ["FORBIDDEN"]}],
            [{"type": "FORBIDDEN"}, {"type": "NOT_FOUND"}],
            [{"type": "FORBIDDEN"}, {}],
            [None],
            "FORBIDDEN",
            [{"type": "FORBIDDEN"}] * 21,
        ):
            with self.subTest(errors=errors):
                c = Client({"node_id": "PR_abc"}, {"data": None, "errors": errors})
                h = WorkflowVerbs(
                    c, Policy(), "swu-wise", "private", "swu-wise", "wf_pr_draft", REPO
                )
                with self.assertRaises(RelayGitHubError) as error:
                    h.run("wf_pr_draft", {"repo": REPO, "number": 1})
                self.assertEqual(error.exception.status, 502)
                self.assertIsNone(error.exception.code)

    def test_graphql_diagnostic_survives_relay_while_unknown_replay_stays_blocked(self):
        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        class Minter:
            def token_for(self, *args):
                return "synthetic-own-app-bearer", 0

        token = "s" * 32
        policy = SeatPolicy(
            json.dumps(
                {
                    "swu-codex": {
                        "token": token,
                        "app_slug": "swu-codex",
                        "repos": [REPO],
                        "actions": ["wf_pr_draft"],
                        "mint_permissions": {
                            "pull_requests": "write",
                            "contents": "write",
                            "metadata": "read",
                        },
                    }
                }
            )
        )
        client = Client(
            {"node_id": "PR_abc"},
            {
                "data": None,
                "errors": [{"type": "FORBIDDEN", "message": "private provider text"}],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            relay = Relay(
                policy,
                client,
                AuditLog(str(path)),
                app_minter=Minter(),
                workflow_verbs_enabled=True,
            )
            args = {"repo": REPO, "number": 1}
            status, result = relay.handle(token, "wf_pr_draft", args)
            self.assertEqual(status, 502, result)
            self.assertEqual(result["github_code"], "graphql_forbidden")
            self.assertEqual(result["outcome"], "unknown")
            self.assertFalse(result["retryable"])
            self.assertEqual(relay.handle(token, "wf_pr_draft", args)[0], 409)
            self.assertEqual(len(client.calls), 2)
            rows = path.read_text()
            self.assertIn("graphql_forbidden", rows)
            self.assertNotIn("private provider text", json.dumps(result) + rows)

    def test_draft_transition_seat_without_contents_write_is_denied(self):
        # A draft transition mints contents:write on top of pull_requests:write,
        # so a seat that only carries the pull-request ceiling is refused before
        # any provider call.  The fixture never proves GitHub needs it; the
        # relay's own ceiling gate does.
        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        class Minter:
            def token_for(self, *args):
                return "synthetic-own-app-bearer", 0

        token = "s" * 32
        policy = SeatPolicy(
            json.dumps(
                {
                    "swu-codex": {
                        "token": token,
                        "app_slug": "swu-codex",
                        "repos": [REPO],
                        "actions": ["wf_pr_draft"],
                        "mint_permissions": {
                            "pull_requests": "write",
                            "metadata": "read",
                        },
                    }
                }
            )
        )
        client = Client(
            {"node_id": "PR_abc"},
            {
                "data": None,
                "errors": [{"type": "FORBIDDEN", "message": "private provider text"}],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            relay = Relay(
                policy,
                client,
                AuditLog(str(path)),
                app_minter=Minter(),
                workflow_verbs_enabled=True,
            )
            status, result = relay.handle(token, "wf_pr_draft", {"repo": REPO, "number": 1})
            self.assertEqual(status, 403, result)
            self.assertEqual(result["error"], "seat permission ceiling insufficient")
            self.assertEqual(len(client.calls), 0)

    def test_threads_query_fixed_and_read_only(self):
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
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "private", "swu-wise", "wf_review_threads", "Example/demo"
        )
        self.assertIn("threads", h.run("wf_review_threads", {"repo": "Example/demo", "number": 1}))
        self.assertEqual(
            c.calls[0][2]["variables"],
            {"owner": "Example", "name": "demo", "number": 1, "cursor": None},
        )

    def test_invalid_method_refused_before_preread(self):
        c = Client()
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "private", "swu-wise", "wf_automerge_enable", REPO
        )
        with self.assertRaises(Refused):
            h.run("wf_automerge_enable", {"repo": REPO, "number": 1, "method": "INVALID"})
        self.assertEqual(c.calls, [])

    def test_base64_node_slash_and_wrong_draft_receipt(self):
        c = Client(
            {"node_id": "PR/abc="},
            {
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {"id": "PR/abc=", "isDraft": True}
                    }
                }
            },
        )
        h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", "wf_pr_ready", REPO)
        with self.assertRaises(RelayGitHubError):
            h.run("wf_pr_ready", {"repo": REPO, "number": 1})
        self.assertEqual(len(c.calls), 2)
