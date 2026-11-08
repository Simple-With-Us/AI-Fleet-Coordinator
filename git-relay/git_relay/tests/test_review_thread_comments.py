import copy
import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Policy

ACTION = "wf_review_thread_comments"
THREAD = "PRRT_fixture"
ARGS = {"repo": REPO, "number": 1, "thread_id": THREAD, "expected_head_sha": SHA}


def thread():
    return {
        "id": THREAD,
        "isResolved": True,
        "isOutdated": False,
        "pullRequest": {"number": 1, "repository": {"nameWithOwner": REPO}},
        "comments": {
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor"},
            "nodes": [
                {
                    "databaseId": "4505651651",
                    "body": "external text",
                    "url": f"https://github.com/{REPO}/pull/1#discussion_r4505651651",
                    "author": {"login": "kody-ai"},
                    "replyTo": {"databaseId": "4505627149"},
                    "pullRequestReview": {"databaseId": "5817290486", "commit": {"oid": SHA}},
                }
            ],
        },
    }


def run(value, args=ARGS, final=SHA):
    client = Client({"head": {"sha": SHA}}, {"data": {"node": value}}, {"head": {"sha": final}})
    handle = WorkflowVerbs(
        client, Policy(), "swu-wise", "proof-bearer-placeholder", "swu-wise", ACTION, REPO
    )
    return handle.run(ACTION, args), client


class ReviewReplies(unittest.TestCase):
    def test_page_provenance_and_no_mutations(self):
        out, client = run(thread())
        self.assertEqual(out["comments"][0]["review_id"], 5817290486)
        self.assertEqual(out["comments"][0]["reply_to_id"], 4505627149)
        self.assertEqual(out["comments"][0]["body"], "external text")
        self.assertFalse(out["has_more"])
        self.assertFalse(out["snapshot"])
        self.assertEqual([x[0] for x in client.calls], ["GET", "POST", "GET"])
        query = client.calls[1][2]["query"]
        self.assertTrue(query.startswith("query("))
        self.assertNotIn("mutation", query)
        self.assertIn("fullDatabaseId", query)
        self.assertEqual(client.calls[1][3], "proof-bearer-placeholder")

    def test_cursor_continuation_and_changed_head(self):
        value = thread()
        value["comments"]["pageInfo"]["hasNextPage"] = True
        out, client = run(value, {**ARGS, "cursor": "previous"})
        self.assertTrue(out["has_more"])
        self.assertEqual(client.calls[1][2]["variables"]["cursor"], "previous")
        with self.assertRaises(Refused):
            run(value, final="b" * 40)
        value["comments"]["pageInfo"]["endCursor"] = "previous"
        with self.assertRaises(RelayGitHubError):
            run(value, {**ARGS, "cursor": "previous"})

    def test_cross_repository_or_pull_or_thread_refused(self):
        for key in ("repo", "number", "thread"):
            value = thread()
            if key == "repo":
                value["pullRequest"]["repository"]["nameWithOwner"] = "other/repo"
            elif key == "number":
                value["pullRequest"]["number"] = 2
            else:
                value["id"] = "PRRT_other"
            with self.assertRaises(RelayGitHubError):
                run(value)

    def test_malformed_and_bounded_receipts(self):
        variants = []
        for field, value in [
            ("body", "x" * 65537),
            ("databaseId", True),
            ("databaseId", "123abc"),
            ("url", "https://wrong.invalid/"),
        ]:
            row = thread()
            row["comments"]["nodes"][0][field] = value
            variants.append(row)
        row = thread()
        row["comments"]["nodes"] *= 6
        variants.append(row)
        row = thread()
        row["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
        variants.append(row)
        variants.extend([None, {}, []])
        for value in variants:
            with self.assertRaises(RelayGitHubError):
                run(copy.deepcopy(value))

    def test_invalid_args_do_not_dispatch(self):
        for extra in [{"thread_id": "foreign"}, {"cursor": "bad cursor"}, {"other": True}]:
            client = Client()
            handle = WorkflowVerbs(
                client, Policy(), "swu-wise", "proof-bearer-placeholder", "swu-wise", ACTION, REPO
            )
            with self.assertRaises(Refused):
                handle.run(ACTION, {**ARGS, **extra})
            self.assertEqual(client.calls, [])

    def test_serialized_escape_budget_and_malformed_unicode(self):
        value = thread()
        value["comments"]["nodes"][0]["body"] = "\x01" * 65536
        with self.assertRaises(Refused):
            run(value)
        for body in ["\ud800", "bad\x00body"]:
            value = thread()
            value["comments"]["nodes"][0]["body"] = body
            with self.assertRaises(RelayGitHubError):
                run(value)

    def test_loaded_credential_withheld(self):
        value = thread()
        value["comments"]["nodes"][0]["body"] = "proof-bearer-placeholder"
        out, _ = run(value)
        self.assertEqual(out["comments"][0]["body"], "<withheld: credential-shaped text>")
        self.assertTrue(out["comments"][0]["body_withheld"])

    def test_all_read_surfaces_register_action(self):
        root = __import__("pathlib").Path(__file__).resolve().parents[3]
        for name in [
            "scripts/git-tools/git",
            "scripts/git-tools/export_catalog.py",
            "scripts/agent-sync-mcp/src/gr-workflow-v1.js",
        ]:
            self.assertIn(ACTION, (root / name).read_text())
