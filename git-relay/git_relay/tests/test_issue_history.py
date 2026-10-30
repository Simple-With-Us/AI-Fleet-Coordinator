import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Minter, Policy


class History(unittest.TestCase):
    def test_fixed_routes_summarization_and_shape(self):
        for a, suffix in [("wf_issue_events", "events"), ("wf_issue_timeline", "timeline")]:
            c = Client(
                [{"id": 1, "event": "commented", "body": "private body", "actor": {"login": "x"}}]
                * 2
            )
            args = {"repo": REPO, "number": 1, "page": 2, "per_page": 1}
            out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(a, args)
            self.assertEqual(
                out,
                {
                    "events": [
                        {"id": 1, "event": "commented", "created_at": None, "commit_id": None}
                    ],
                    "page": 2,
                    "has_more": True,
                },
            )
            self.assertEqual(
                c.calls,
                [("GET", f"/repos/{REPO}/issues/1/{suffix}?per_page=1&page=2", None, "own")],
            )
            self.assertEqual(SPECS[a], ({"issues": "read"}, False))
            self.assertIsNone(WorkflowVerbs.check_shape(a, args, REPO))

    def test_bad_metadata_and_closed_pages(self):
        for x in [
            {},
            {"event": "x", "id": True},
            {"event": "x", "commit_id": "main"},
            {"event": "x", "created_at": 2},
            {"event": "../../path"},
        ]:
            with self.assertRaises(RelayGitHubError):
                WorkflowVerbs._issue_event(x)
        for extra in [{"page": 0}, {"page": 1001}, {"per_page": 51}, {"body": True}]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    "wf_issue_timeline", {"repo": REPO, "number": 1, **extra}, REPO
                )

    def test_own_app_least_permission(self):
        m = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", "wf_issue_events", REPO)
        self.assertEqual(m.calls[0][2], {"issues": "read", "metadata": "read"})

    def test_documented_hyphenated_timeline_names(self):
        for name in ("cross-referenced", "line-commented", "commit-commented"):
            self.assertEqual(
                WorkflowVerbs._issue_event({"event": name}, timeline=True)["event"], name
            )

    def test_event_ids_required_but_documented_timeline_missing_id_is_preserved(self):
        for row in (
            {"event": "closed"},
            {"event": "closed", "id": None},
            {"event": "closed", "id": True},
            {"event": "closed", "id": 0},
        ):
            with self.assertRaises(RelayGitHubError):
                WorkflowVerbs._issue_event(row)
        self.assertIsNone(
            WorkflowVerbs._issue_event({"event": "cross-referenced"}, timeline=True)["id"]
        )

    def test_raw_history_json_is_bounded_before_projection(self):
        for extra in (
            {"body": "x" * 65537},
            {"nested": [[[[[[[1]]]]]]]},
            {"nodes": list(range(501))},
        ):
            c = Client([{"id": 1, "event": "closed", **extra}])
            a = "wf_issue_events"
            with self.assertRaises(RelayGitHubError):
                WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
                    a, {"repo": REPO, "number": 1}
                )
