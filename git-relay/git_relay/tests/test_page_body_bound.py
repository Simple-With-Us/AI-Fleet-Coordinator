"""A paged GitHub list read is bounded by its page size (8 MiB), every other read by 1 MiB.

GitHub's raw pull request objects run 20 to 25 KB each, so a page of 50 is 1.0 to 1.3 MB while the
relay returns about 400 bytes per row.  The 1 MiB bound refused such pages (BotFleet page 11 at
per_page 50, 1,257,416 bytes raw) as "unreadable", which broke lane retire and prune on the server.
"""

import json
import unittest

from git_relay.github_client import (
    MAX_BODY,
    MAX_PAGE_BODY,
    GitHubClient,
    RelayGitHubError,
    response_limit,
)
from git_relay.workflow_verbs import WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Policy


class Reply:
    status = 200

    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, amount=None):
        return self._data if amount is None else self._data[:amount]


def client_for(data, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request.get_method(), request.full_url))
        return Reply(data)

    return GitHubClient("token", opener=opener)


def big_list(rows, each):
    return json.dumps([{"id": i, "body": "x" * each} for i in range(rows)]).encode()


class ResponseLimit(unittest.TestCase):
    def test_only_a_get_with_per_page_in_its_query_gets_the_page_bound(self):
        paged = [
            "/repos/o/r/pulls?state=all&per_page=50&page=11",
            "/repos/o/r/issues/5/comments?per_page=30",
            "/repos/o/r/pulls?page=2&per_page=",
        ]
        for path in paged:
            self.assertEqual(response_limit("GET", path), MAX_PAGE_BODY, path)
        plain = [
            "/repos/o/r/pulls/5",
            "/repos/o/r/git/blobs/" + "a" * 40,
            "/repos/o/r/contents/dir%2Fper_page=1?ref=main",
            "/repos/o/r/contents/per_page=1",
            "/repos/o/r/pulls?xper_page=1",
            "/repos/o/r/pulls?per_page_x=1",
            "/repos/o/r/pulls",
        ]
        for path in plain:
            self.assertEqual(response_limit("GET", path), MAX_BODY, path)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(response_limit(method, "/repos/o/r/pulls?per_page=50"), MAX_BODY)


class PagedRead(unittest.TestCase):
    def test_a_paged_get_over_1_mib_is_read(self):
        data = big_list(50, 24000)
        self.assertGreater(len(data), MAX_BODY)
        value = client_for(data)._call("GET", "/repos/o/r/pulls?state=all&per_page=50&page=11")
        self.assertEqual(len(value), 50)

    def test_the_same_bytes_without_paging_are_still_refused(self):
        data = big_list(50, 24000)
        with self.assertRaises(RelayGitHubError) as caught:
            client_for(data)._call("GET", "/repos/o/r/pulls/5")
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertIn("1 MiB", caught.exception.message)

    def test_a_write_with_per_page_in_its_path_keeps_the_small_bound(self):
        with self.assertRaises(RelayGitHubError) as caught:
            client_for(big_list(50, 24000))._call("POST", "/repos/o/r/issues?per_page=1", {})
        self.assertEqual(caught.exception.code, "response_too_large")

    def test_a_paged_get_over_the_page_bound_is_refused_and_says_so(self):
        data = big_list(9, 1024 * 1024)
        self.assertGreater(len(data), MAX_PAGE_BODY)
        with self.assertRaises(RelayGitHubError) as caught:
            client_for(data)._call("GET", "/repos/o/r/pulls?per_page=50")
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertEqual(caught.exception.status, 0)
        self.assertIn("8 MiB", caught.exception.message)


class PrListPageEndToEnd(unittest.TestCase):
    def rows(self, count, body_size):
        return [
            {
                "number": 600 + i,
                "state": "closed",
                "draft": False,
                "merged_at": "2026-10-01T00:00:00Z" if i % 2 else None,
                "head": {"ref": f"claude/topic-{i}", "sha": SHA},
                "base": {"ref": "main", "sha": "b" * 40},
                "title": "not returned",
                "body": "x" * body_size,
            }
            for i in range(count)
        ]

    def test_a_page_of_50_pull_requests_over_1_mib_raw_comes_back_as_slim_rows(self):
        data = json.dumps(self.rows(50, 24000)).encode()
        self.assertGreater(len(data), MAX_BODY)
        seen = []
        handle = WorkflowVerbs(
            client_for(data, seen), Policy(), "swu-wise", "own", "swu-wise", "wf_pr_list_page", REPO
        )
        out = handle.run(
            "wf_pr_list_page", {"repo": REPO, "state": "all", "page": 11, "per_page": 50}
        )
        self.assertEqual(len(out["pulls"]), 50)
        self.assertTrue(out["has_more"])
        self.assertEqual(out["page"], 11)
        self.assertEqual(
            sorted(out["pulls"][1]),
            [
                "base_ref",
                "base_sha",
                "draft",
                "head_ref",
                "head_sha",
                "merged_at",
                "number",
                "state",
            ],
        )
        self.assertEqual(out["pulls"][1]["merged_at"], "2026-10-01T00:00:00Z")
        self.assertIsNone(out["pulls"][0]["merged_at"])
        self.assertLess(len(json.dumps(out)), MAX_BODY // 20)
        self.assertEqual(len(seen), 1)
        self.assertIn("per_page=50&page=11", seen[0][1])


if __name__ == "__main__":
    unittest.main()
