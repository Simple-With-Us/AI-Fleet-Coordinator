"""The GitHub census:  paging by $endCursor, the repeated-cursor trap, fork PRs as counts, retries."""
import json
import unittest
from unittest import mock

from fleet_facts import github_prs as G
from fleet_facts.runner import Result

from .builders import gql_node, gql_page
from .fakes import FakeRunner, out


def paged(pages: dict, *, fail_502: int = 0, nodes=None):
    """A runner serving `pages` by the endCursor it was given, like the real search."""
    state = {"calls": 0, "nodes": 0}

    def handler(argv):
        state["calls"] += 1
        if state["calls"] <= fail_502:
            return out("", rc=1, err="gh: HTTP 502: Bad Gateway")
        fields = {}
        for i, a in enumerate(argv):
            if a == "-f" and i + 1 < len(argv):
                key, _, value = argv[i + 1].partition("=")
                fields.setdefault(key, []).append(value)
        if "ids[]" in fields:
            responses = nodes
            n = min(state["nodes"], len(responses) - 1)
            state["nodes"] += 1
            return out(json.dumps(responses[n]))
        return out(json.dumps(pages[fields.get("endCursor", [""])[0]]))
    return FakeRunner(handler)


def gather(runner, **kw):
    with mock.patch.object(G, "which", return_value="gh"):
        return G.gather(runner, sleep=lambda s: None, **kw)


class Paging(unittest.TestCase):
    def test_two_pages_by_end_cursor(self):
        runner = paged({"": gql_page([gql_node(1), gql_node(2)], "c1", 3), "c1": gql_page([gql_node(3)], None, 3)})
        result = gather(runner)
        self.assertEqual(result["status"], "ok", result["reason"])
        self.assertEqual([p["number"] for p in result["data"]["prs"]], [1, 2, 3])
        self.assertEqual(result["data"]["pages"], 2)
        first, second = runner.argvs
        self.assertNotIn("endCursor", " ".join(a for a in first if a.startswith("endCursor=")))
        self.assertIn("endCursor=c1", second)           # the variable is named exactly endCursor
        self.assertTrue(second[:3] == ["gh", "api", "graphql"])

    def test_the_query_declares_the_cursor_variable_and_first_30(self):
        self.assertIn("$endCursor: String", G.SEARCH_QUERY)
        self.assertIn("after: $endCursor", G.SEARCH_QUERY)
        self.assertIn("first: 30", G.SEARCH_QUERY)
        self.assertIn("org:Simple-With-Us is:pr is:open archived:false", G.SEARCH)
        self.assertNotIn("mutation", G.SEARCH_QUERY.lower())
        self.assertNotIn("mutation", G.NODES_QUERY.lower())

    def test_repeated_cursor_trap_same_page_again(self):
        # The server ignores the cursor (as it would if the variable were misnamed) and serves page 1 again.
        page1 = gql_page([gql_node(1), gql_node(2)], "c1", 40)
        runner = paged({"": page1, "c1": page1})
        result = gather(runner)
        self.assertEqual(result["status"], "failed")
        self.assertIn("repeated", result["reason"])
        self.assertLessEqual(len(runner.calls), 3)          # it stopped, it did not loop for ever

    def test_repeated_cursor_trap_same_cursor_new_nodes(self):
        runner = paged({"": gql_page([gql_node(1)], "c1", 9), "c1": gql_page([gql_node(2)], "c1", 9)})
        result = gather(runner)
        self.assertEqual(result["status"], "failed")
        self.assertIn("cursor repeated", result["reason"])

    def test_a_cursor_that_comes_back_later_also_fails(self):
        runner = paged({"": gql_page([gql_node(1)], "c1", 9), "c1": gql_page([gql_node(2)], "c2", 9),
                        "c2": gql_page([gql_node(3)], "c1", 9)})
        result = gather(runner)
        self.assertEqual(result["status"], "failed")

    def test_stops_at_the_page_cap_and_says_incomplete(self):
        pages = {"": gql_page([gql_node(1)], "p1", 9999)}
        for i in range(1, G.MAX_PAGES + 2):
            pages["p%d" % i] = gql_page([gql_node(i + 1)], "p%d" % (i + 1), 9999)
        result = gather(paged(pages))
        self.assertEqual(result["status"], "failed")
        self.assertIn("incomplete", result["reason"])
        self.assertEqual(len(result["data"]["prs"]), G.MAX_PAGES)


class Retries(unittest.TestCase):
    def test_a_502_is_retried(self):
        runner = paged({"": gql_page([gql_node(1)], None)}, fail_502=2)
        result = gather(runner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(runner.calls), 3)

    def test_three_502s_fail_the_source(self):
        result = gather(paged({"": gql_page([gql_node(1)], None)}, fail_502=5))
        self.assertEqual(result["status"], "failed")
        self.assertIn("gave up", result["reason"])

    def test_other_errors_are_not_retried(self):
        runner = FakeRunner(lambda argv: out("", rc=1, err="gh: HTTP 401: Bad credentials"))
        result = gather(runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(runner.calls), 1)

    def test_mergeable_unknown_is_fetched_again_until_known(self):
        first = gql_page([gql_node(1, mergeable="UNKNOWN", node_id="PR_a"), gql_node(2)], None)
        known = {"data": {"nodes": [gql_node(1, mergeable="CONFLICTING", node_id="PR_a")]}}
        runner = paged({"": first}, nodes=[known])
        result = gather(runner)
        self.assertEqual(result["status"], "ok")
        by_number = {p["number"]: p for p in result["data"]["prs"]}
        self.assertEqual(by_number[1]["mergeable"], "CONFLICTING")
        self.assertEqual(result["data"]["mergeable_pending"], 0)
        refetch = [a for a in runner.argvs if "ids[]=PR_a" in a]
        self.assertEqual(len(refetch), 1)

    def test_mergeable_that_stays_unknown_is_reported_not_failed(self):
        first = gql_page([gql_node(1, mergeable="UNKNOWN", node_id="PR_a")], None)
        again = {"data": {"nodes": [gql_node(1, mergeable="UNKNOWN", node_id="PR_a")]}}
        result = gather(paged({"": first}, nodes=[again]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["mergeable_pending"], 1)
        self.assertEqual(result["data"]["prs"][0]["mergeable"], "UNKNOWN")

    def test_a_failed_refetch_leaves_unknown(self):
        first = gql_page([gql_node(1, mergeable="UNKNOWN", node_id="PR_a")], None)
        calls = {"n": 0}

        def handler(argv):
            calls["n"] += 1
            if any(a.startswith("ids[]=") for a in argv):
                return out("", rc=1, err="gh: HTTP 401")
            return out(json.dumps(first))
        result = gather(FakeRunner(handler))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["mergeable_pending"], 1)

    def test_bad_output_fails(self):
        self.assertEqual(gather(FakeRunner(lambda a: out("not json")))["status"], "failed")
        self.assertEqual(gather(FakeRunner(lambda a: out("[]")))["status"], "failed")
        errors = {"errors": [{"type": "RATE_LIMITED", "message": "x"}]}
        result = gather(FakeRunner(lambda a: out(json.dumps(errors))))
        self.assertEqual(result["status"], "failed")
        self.assertIn("RATE_LIMITED", result["reason"])
        self.assertEqual(gather(FakeRunner(lambda a: Result(None, reason="timed out after 120s")))["status"], "failed")

    def test_no_gh_is_failed_not_a_crash(self):
        with mock.patch.object(G, "which", return_value=None):
            self.assertEqual(G.gather(FakeRunner(lambda a: out("")))["status"], "failed")


class Malformed(unittest.TestCase):
    """A reply that is not a whole page is a failed source, never a short list that reads as complete."""

    def test_no_node_list_fails(self):
        for nodes in (None, "x", {"a": 1}):
            page = gql_page([gql_node(1)], None)
            page["data"]["search"]["nodes"] = nodes
            result = gather(paged({"": page}))
            self.assertEqual(result["status"], "failed", nodes)
            self.assertIn("node list", result["reason"])

    def test_no_page_info_fails(self):
        for info in (None, {}, {"hasNextPage": "false"}, {"endCursor": "x"}):
            page = gql_page([gql_node(1)], None)
            if info is None:
                del page["data"]["search"]["pageInfo"]
            else:
                page["data"]["search"]["pageInfo"] = info
            result = gather(paged({"": page}))
            self.assertEqual(result["status"], "failed", info)
            self.assertIn("page info", result["reason"])

    def test_fewer_prs_than_the_search_counted_fails(self):
        result = gather(paged({"": gql_page([gql_node(1), gql_node(2)], None, 5)}))
        self.assertEqual(result["status"], "failed")
        self.assertIn("counted 5", result["reason"])

    def test_more_prs_than_counted_is_fine(self):
        # a PR opened while paging
        self.assertEqual(gather(paged({"": gql_page([gql_node(1), gql_node(2)], None, 1)}))["status"], "ok")

    def test_no_search_object_fails(self):
        self.assertEqual(gather(paged({"": {"data": {"viewer": {}}}}))["status"], "failed")


class Forks(unittest.TestCase):
    def test_a_fork_pr_is_reduced_to_a_count(self):
        evil = gql_node(7, cross=True, owner="outside-person", title="SECRET-LOOKING TITLE @**all**", ref="evil/branch-name",
                        author="outside-person", labels=("sneaky-label",), files=("path/to/leak.txt",))
        deleted = gql_node(8, cross=True, owner=None, title="ghost fork title")
        odd = gql_node(9, cross=False, owner="not-the-org", title="owner mismatch title")
        ok = gql_node(10)
        result = gather(paged({"": gql_page([evil, deleted, odd, ok], None)}))
        data = result["data"]
        self.assertEqual([p["number"] for p in data["prs"]], [10])
        self.assertEqual(data["outside_fork_prs"], {"count": 3, "by_repo": {"Simple-With-Us/example-repo": 3}})
        blob = json.dumps(result)
        for leaked in ("SECRET-LOOKING", "evil/branch-name", "outside-person", "sneaky-label", "leak.txt", "ghost fork", "owner mismatch"):
            self.assertNotIn(leaked, blob)

    def test_an_org_owned_cross_repository_pr_is_not_inside(self):
        result = gather(paged({"": gql_page([gql_node(5, cross=True, owner="Simple-With-Us")], None)}))
        self.assertEqual(result["data"]["prs"], [])
        self.assertEqual(result["data"]["outside_fork_prs"]["count"], 1)


class Records(unittest.TestCase):
    def record(self, node):
        return gather(paged({"": gql_page([node], None)}))["data"]["prs"][0]

    def test_fields(self):
        r = self.record(gql_node(12, ref="claude/FFX-1-example", threads=(True, False, False), files=("a.py", "b/c.py"),
                                 checks=("SUCCESS", "FAILURE", "TIMED_OUT"), auto_merge=True, labels=("one", "two")))
        self.assertEqual(r["repo"], "Simple-With-Us/example-repo")
        self.assertEqual(r["number"], 12)
        self.assertEqual(r["url"], "https://github.com/Simple-With-Us/example-repo/pull/12")
        self.assertEqual(r["head_ref"], "claude/FFX-1-example")
        self.assertTrue(r["head_ref_exact"] and r["same_repo"] and r["auto_merge"])
        self.assertEqual(r["auto_merge_method"], "SQUASH")
        self.assertEqual(r["review_threads"], {"total": 3, "unresolved": 2, "partial": False})
        self.assertEqual(r["files"], ["a.py", "b/c.py"])
        self.assertEqual(r["labels"], ["one", "two"])
        self.assertEqual(r["head_commit"], {"oid": "a" * 40, "committed_at": "2026-10-10T11:00:00Z"})
        self.assertEqual(r["checks"]["state"], "FAILURE")
        self.assertEqual(r["checks"]["total"], 3)
        self.assertEqual(sorted(r["checks"]["failing"]), ["check-2", "status-1"])
        self.assertEqual({c["state"] for c in r["checks"]["contexts"]}, {"SUCCESS", "FAILURE", "TIMED_OUT"})
        self.assertEqual(r["updated_at"], "2026-10-10T12:00:00Z")

    def test_url_is_built_here_not_taken_from_the_response(self):
        r = self.record(gql_node(3, url="https://evil.example/phish"))
        self.assertEqual(r["url"], "https://github.com/Simple-With-Us/example-repo/pull/3")

    def test_free_text_is_inert(self):
        r = self.record(gql_node(4, title="@**all** [click](https://evil.example/x) `rm -rf` /poll now", author="a@b",
                                 labels=("@**all**",), files=("dir/@**all**.txt",)))
        for text in (r["title"], r["labels"][0], r["files"][0]):
            self.assertNotIn("@", text)
            self.assertNotIn("`", text)
            self.assertNotIn("://", text)
        self.assertNotIn("/poll", r["title"])
        self.assertNotEqual(r["author"], "a@b")

    def test_an_odd_branch_name_is_cleaned_and_marked(self):
        r = self.record(gql_node(5, ref="claude/with space@and:odd"))
        self.assertFalse(r["head_ref_exact"])
        self.assertNotIn("@", r["head_ref"])
        r = self.record(gql_node(6, ref="claude/todo-list"))
        self.assertTrue(r["head_ref_exact"])
        self.assertEqual(r["head_ref"], "claude/todo-list")      # a join key is never mangled when it is plain

    def test_a_node_without_a_valid_repo_is_skipped(self):
        bad = gql_node(8, repo="not a repo")
        result = gather(paged({"": gql_page([bad, gql_node(9)], None)}))
        self.assertEqual([p["number"] for p in result["data"]["prs"]], [9])

    def test_prs_are_sorted(self):
        nodes = [gql_node(5, repo="Simple-With-Us/zz"), gql_node(2), gql_node(1, repo="Simple-With-Us/aa")]
        result = gather(paged({"": gql_page(nodes, None)}))
        self.assertEqual([(p["repo"].split("/")[1], p["number"]) for p in result["data"]["prs"]], [("aa", 1), ("example-repo", 2), ("zz", 5)])


if __name__ == "__main__":
    unittest.main()
