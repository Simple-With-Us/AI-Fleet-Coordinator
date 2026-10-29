import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class RunPages(unittest.TestCase):
    def row(self, **kw):
        return {
            "id": 7,
            "run_attempt": 1,
            "workflow_id": 8,
            "head_sha": SHA,
            "head_branch": "wise/a",
            "status": "waiting",
            "conclusion": None,
            "pull_requests": [{"number": 2}],
            "logs_url": "omitted",
            "path": "omitted",
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_runs_page", REPO)

    def value(self, rows):
        return {"total_count": len(rows), "workflow_runs": rows}

    def test_status_page_and_pr_number_metadata(self):
        c = Client(self.value([self.row()]))
        out = self.handle(c).run(
            "wf_runs_page", {"repo": REPO, "status": "waiting", "page": 2, "per_page": 1}
        )
        self.assertFalse(out["snapshot"])
        self.assertEqual(out["search_cap"], 1000)
        self.assertTrue(out["has_more"])
        self.assertEqual(out["runs"][0]["pull_numbers"], [2])
        self.assertNotIn("logs_url", out["runs"][0])
        self.assertEqual(
            c.calls,
            [("GET", f"/repos/{REPO}/actions/runs?per_page=1&page=2&status=waiting", None, "own")],
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_runs_page", REPO)
        self.assertEqual(m.calls[0][2], {"actions": "read", "metadata": "read"})

    def test_caps_before_read(self):
        c = Client()
        with self.assertRaises(Refused):
            self.handle(c).run(
                "wf_runs_page", {"repo": REPO, "status": "queued", "page": 22, "per_page": 50}
            )
        self.assertEqual(c.calls, [])

    def test_bad_metadata_or_filter_refused(self):
        for rows, args in [
            ([self.row(id=True)], {}),
            ([self.row(), self.row()], {}),
            ([self.row(conclusion="success")], {}),
            ([self.row()], {"head_sha": "b" * 40}),
            ([self.row()], {"status": "queued"}),
            ([self.row(pull_requests=[{"number": True}])], {}),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(self.value(rows))).run("wf_runs_page", {"repo": REPO, **args})

    def test_unfiltered_exposes_no_search_cap(self):
        out = self.handle(Client(self.value([]))).run("wf_runs_page", {"repo": REPO, "page": 1000})
        self.assertIsNone(out["search_cap"])
        self.assertFalse(out["has_more"])

    def test_missing_nullable_metadata_key_refused_not_keyerror(self):
        row = self.row()
        del row["head_branch"]
        with self.assertRaises(RelayGitHubError):
            self.handle(Client(self.value([row]))).run("wf_runs_page", {"repo": REPO})

    def test_explicit_null_branch_kept_as_observation(self):
        out = self.handle(Client(self.value([self.row(head_branch=None)]))).run(
            "wf_runs_page", {"repo": REPO}
        )
        self.assertIsNone(out["runs"][0]["head_branch"])

    def test_each_required_run_field_missing_is_fixed_refusal(self):
        for key in (
            "id",
            "run_attempt",
            "workflow_id",
            "head_sha",
            "head_branch",
            "status",
            "conclusion",
            "pull_requests",
        ):
            row = self.row()
            del row[key]
            with self.subTest(key=key), self.assertRaises(RelayGitHubError):
                self.handle(Client(self.value([row]))).run("wf_runs_page", {"repo": REPO})

    def test_malformed_field_types_are_fixed_refusals(self):
        for key in (
            "id",
            "run_attempt",
            "workflow_id",
            "head_sha",
            "head_branch",
            "status",
            "conclusion",
            "pull_requests",
        ):
            for value in (True, {}, [], 17):
                if (
                    key in ("id", "run_attempt", "workflow_id")
                    and value == 17
                    or key == "pull_requests"
                    and value == []
                ):
                    continue
                with self.subTest(key=key, value=value), self.assertRaises(RelayGitHubError):
                    self.handle(Client(self.value([self.row(**{key: value})]))).run(
                        "wf_runs_page", {"repo": REPO}
                    )

    def test_completed_startup_failure_is_observed_and_filterable(self):
        row = self.row(status="completed", conclusion="startup_failure")
        for args in ({}, {"status": "completed"}):
            out = self.handle(Client(self.value([row]))).run("wf_runs_page", {"repo": REPO, **args})
            self.assertEqual(out["runs"][0]["conclusion"], "startup_failure")

    def test_provider_documents_conclusions_in_status_query_not_conclusion_key(self):
        c = Client(self.value([self.row(status="completed", conclusion="failure")]))
        out = self.handle(c).run("wf_runs_page", {"repo": REPO, "status": "failure"})
        self.assertEqual(out["runs"][0]["conclusion"], "failure")
        self.assertIn("&status=failure", c.calls[0][1])
        self.assertNotIn("&conclusion=", c.calls[0][1])
