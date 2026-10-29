"""Synthetic union qualification; no live fleet/accounts or outcome-grant claim."""

import unittest

from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Minter, Policy

ACTIONS = (
    "wf_pr_list_page",
    "wf_compare_commits_page",
    "wf_pr_reviews_page",
    "wf_pr_comments_page",
    "wf_labels_add",
    "wf_label_update",
)


class PracticalPageBoundaries(unittest.TestCase):
    def test_missing_ceiling_denies_before_any_mint_or_provider_read(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                c, m = Client(), Minter()
                with self.assertRaises(Refused):
                    WorkflowVerbs.for_action(
                        c, Policy({"metadata": "read"}), m, "swu-wise", action, REPO
                    )
                self.assertEqual(m.calls, [])
                self.assertEqual(c.calls, [])

    def test_denied_policy_no_mint_no_read(self):
        for action in ACTIONS:
            c, m = Client(), Minter()
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(c, Policy(allowed=False), m, "swu-wise", action, REPO)
            self.assertEqual(m.calls, [])
            self.assertEqual(c.calls, [])

    def test_cross_app_identity_refused_before_provider_read(self):
        for action in ACTIONS:
            c = Client()
            with self.assertRaises(Refused):
                WorkflowVerbs(c, Policy(), "swu-wise", "opaque", "swu-echo", action, REPO)
            self.assertEqual(c.calls, [])

    def test_bound_read_handle_cannot_change_action_or_repo(self):
        for action in ACTIONS:
            c, m = Client(), Minter()
            h = WorkflowVerbs.for_action(
                c,
                Policy(
                    {
                        "metadata": "read",
                        "pull_requests": "read",
                        "contents": "read",
                        "checks": "read",
                        "issues": "write",
                        "actions": "read",
                    }
                ),
                m,
                "swu-wise",
                action,
                REPO,
            )
            with self.assertRaises(Refused):
                h.run("wf_file_put", {"repo": REPO})
            with self.assertRaises(Refused):
                h.run(action, {"repo": "Simple-With-Us/Other"})
            self.assertEqual(len(m.calls), 1)
            self.assertEqual(c.calls, [])


class PracticalPageRelayIngress(unittest.TestCase):
    """Exercise real relay auth/routing, not only directly constructed handlers."""

    def relay(self, tmp, enabled=True, listed=True):
        import json
        import os

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        seats = {
            "swu-wise": {
                "token": "w" * 32,
                "actions": list(ACTIONS) if listed else ["wf_runs"],
                "repos": [REPO],
                "app_slug": "swu-wise",
                "mint_permissions": {
                    "metadata": "read",
                    "contents": "read",
                    "pull_requests": "read",
                    "actions": "read",
                    "checks": "read",
                    "issues": "write",
                },
            }
        }
        return Relay(
            SeatPolicy(json.dumps(seats)),
            Client(
                {"total_count": 0, "workflow_runs": []}, {"total_count": 0, "workflow_runs": []}
            ),
            AuditLog(os.path.join(tmp, "audit.jsonl")),
            app_minter=Minter(),
            workflow_verbs_enabled=enabled,
        )

    def test_all_page_actions_disabled_unlisted_wrong_repo_or_wrong_caller_no_effect(self):
        import tempfile

        for action in ACTIONS:
            for case in ("disabled", "unlisted", "wrongrepo", "wrongcaller", "badargs"):
                with self.subTest(action=action, case=case), tempfile.TemporaryDirectory() as tmp:
                    r = self.relay(tmp, enabled=case != "disabled", listed=case != "unlisted")
                    status, _ = r.handle(
                        "x" * 32 if case == "wrongcaller" else "w" * 32,
                        action,
                        {
                            "repo": "Simple-With-Us/Other" if case == "wrongrepo" else REPO,
                            **({"unexpected": "fixed"} if case == "badargs" else {}),
                        },
                    )
                    self.assertGreaterEqual(status, 400)
                    self.assertEqual(r.client.calls, [])
                    self.assertEqual(r.app_minter.calls, [])

    def test_each_read_and_write_dispatches_with_own_scoped_token(self):
        import tempfile

        sha = "a" * 40
        pr = {"number": 2, "head": {"sha": sha}}
        label = {"id": 7, "name": "hold", "color": "000000", "description": "safe"}
        cases = [
            ("wf_pr_list_page", {}, [[]], "pull_requests"),
            (
                "wf_compare_commits_page",
                {"base_sha": sha, "head_sha": sha},
                [
                    {
                        "status": "identical",
                        "base_commit": {"sha": sha},
                        "merge_base_commit": {"sha": sha},
                        "ahead_by": 0,
                        "behind_by": 0,
                        "total_commits": 0,
                        "commits": [],
                    }
                ],
                "contents",
            ),
            (
                "wf_pr_reviews_page",
                {"number": 2, "expected_head_sha": sha},
                [pr, [], pr],
                "pull_requests",
            ),
            (
                "wf_pr_comments_page",
                {"number": 2, "expected_head_sha": sha},
                [pr, [], pr],
                "pull_requests",
            ),
            ("wf_labels_add", {"number": 2, "labels": ["hold"]}, [[label]], "issues"),
            (
                "wf_label_update",
                {
                    "name": "hold",
                    "expected_label_id": 7,
                    "new_name": "hold",
                    "color": "000000",
                    "description": "safe",
                },
                [label, label],
                "issues",
            ),
        ]
        for action, args, replies, perm in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                r = self.relay(tmp)
                r.client = Client(*replies)
                status, body = r.handle("w" * 32, action, {"repo": REPO, **args})
                self.assertEqual(status, 200, body)
                self.assertTrue(all(c[3] == "opaque-installation-bearer" for c in r.client.calls))
                self.assertEqual(
                    r.app_minter.calls[0][2],
                    {perm: "write" if perm == "issues" else "read", "metadata": "read"},
                )
