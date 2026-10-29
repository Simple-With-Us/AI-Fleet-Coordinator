"""Synthetic credential-shape fixtures only, never real credentials."""

import unittest

from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import Client, Policy

PREFIXES = ("sk-", "sk-ant-", "sk-proj-", "sk_live_", "sk_test_")
REPO = "o/r"


class IssueOutboundProviderTests(unittest.TestCase):
    def handle(self, client, action):
        return WorkflowVerbs(client, Policy(), "swu-wise", "opaque", "swu-wise", action, REPO)

    def test_create_update_provider_shapes_refuse_before_any_client_call(self):
        count = 0
        for prefix in PREFIXES:
            for field in ("title", "body"):
                for action in ("wf_issue_create", "wf_issue_update"):
                    with self.subTest(prefix=prefix, field=field, action=action):
                        client = Client()
                        args = {"repo": REPO, "title": "safe issue", "body": "safe body"}
                        if action == "wf_issue_update":
                            args["number"] = 1
                        args[field] = prefix + "X" * 32
                        with self.assertRaises(Refused):
                            self.handle(client, action).run(action, args)
                        self.assertEqual(client.calls, [])
                        count += 1
        self.assertEqual(count, 20)

    def test_existing_github_shape_still_refused_without_client_call(self):
        client = Client()
        with self.assertRaises(Refused):
            self.handle(client, "wf_issue_create").run(
                "wf_issue_create", {"repo": REPO, "title": "ghp_" + "X" * 32, "body": "safe body"}
            )
        self.assertEqual(client.calls, [])

    def test_clean_create_and_state_only_update_keep_existing_behavior(self):
        for action, args, expected in [
            (
                "wf_issue_create",
                {"repo": REPO, "title": "safe issue", "body": "safe body"},
                ("POST", "/repos/o/r/issues"),
            ),
            (
                "wf_issue_update",
                {"repo": REPO, "number": 1, "state": "closed"},
                ("PATCH", "/repos/o/r/issues/1"),
            ),
        ]:
            row = {"number": 1, "state": "closed", "title": "safe issue", "body": "safe body"}
            client = Client(row, row)
            self.handle(client, action).run(action, args)
            self.assertEqual(client.calls[-1][:2], expected)

    def test_comment_create_shares_issue_text_guard(self):
        for prefix in PREFIXES:
            client = Client()
            with self.assertRaises(Refused):
                self.handle(client, "wf_issue_comment").run(
                    "wf_issue_comment", {"repo": REPO, "number": 1, "body": prefix + "X" * 32}
                )
            self.assertEqual(client.calls, [])

    def test_embedded_provider_shapes_also_refused_before_client_call(self):
        for prefix in PREFIXES:
            for field in ("title", "body"):
                for action in ("wf_issue_create", "wf_issue_update"):
                    client = Client()
                    args = {"repo": REPO, "title": "safe issue", "body": "safe body"}
                    if action == "wf_issue_update":
                        args["number"] = 1
                    args[field] = "embedded" + prefix + "X" * 32
                    with (
                        self.subTest(prefix=prefix, field=field, action=action),
                        self.assertRaises(Refused),
                    ):
                        self.handle(client, action).run(action, args)
                    self.assertEqual(client.calls, [])

    def test_comment_update_provider_shapes_refused_before_any_client_call(self):
        # Synthetic scanner-shape fixtures only; never credentials.
        for action in ("wf_issue_comment_update", "wf_review_comment_update"):
            for prefix in PREFIXES:
                for leading in ("", "embedded"):
                    client = Client()
                    args = {"repo": REPO, "comment_id": 1, "body": leading + prefix + "X" * 32}
                    with self.subTest(action=action, prefix=prefix, leading=leading):
                        with self.assertRaises(Refused):
                            self.handle(client, action).run(action, args)
                        self.assertEqual(client.calls, [])

    def test_clean_comment_updates_preserve_patch_behavior(self):
        for action, path in (
            ("wf_issue_comment_update", "/repos/o/r/issues/comments/1"),
            ("wf_review_comment_update", "/repos/o/r/pulls/comments/1"),
        ):
            client = Client({"id": 1, "body": "safe body"})
            result = self.handle(client, action).run(
                action, {"repo": REPO, "comment_id": 1, "body": "safe body"}
            )
            self.assertEqual(result["result"]["id"], 1)
            self.assertEqual(client.calls[-1][:2], ("PATCH", path))

    def test_git_writers_provider_shapes_refused_before_client_call(self):
        for prefix in PREFIXES:
            for leading in ("", "embedded"):
                text = leading + prefix + "X" * 32
                for action, args in (
                    ("wf_git_blob_create", {"text": text}),
                    (
                        "wf_git_commit_create",
                        {"tree_sha": "a" * 40, "parents": ["b" * 40], "message": text},
                    ),
                    (
                        "wf_git_tree_create",
                        {
                            "base_tree": "a" * 40,
                            "files": [{"path": "safe.txt", "mode": "100644", "text": text}],
                        },
                    ),
                ):
                    client = Client()
                    with self.subTest(action=action, prefix=prefix, leading=leading):
                        with self.assertRaises(Refused):
                            self.handle(client, action).run(action, {"repo": REPO, **args})
                        self.assertEqual(client.calls, [])

    def test_relation_receipt_taint_is_upstream_unverifiable(self):
        from git_relay.github_client import RelayGitHubError

        client = Client()
        row = {
            "id": 1,
            "number": 1,
            "state": "open",
            "title": "safe",
            "repository_url": "https://api.github.com/repos/sk-" + "X" * 32 + "/r",
        }
        with self.assertRaises(RelayGitHubError) as caught:
            WorkflowVerbs._relation_issue(row)
        self.assertEqual(caught.exception.status, 502)
