import base64
import unittest

from git_relay.workflow_verbs import ACTIONS, SPECS, Refused, Unknown, WorkflowVerbs

REPO = "Simple-With-Us/AI-Fleet-Coordinator"
SHA = "a" * 40
CEILING = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "actions": "write",
    "metadata": "read",
}


class Policy:
    def __init__(self, ceiling=CEILING, allowed=True):
        self.ceiling, self.allowed = ceiling, allowed

    def app_identity(self, seat):
        return "swu-wise", dict(self.ceiling)

    def authorize(self, seat, action, repo):
        return None if self.allowed and repo == REPO else "denied"


class Client:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def _call(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path, payload, bearer))
        return self.replies.pop(0) if self.replies else {}

    @staticmethod
    def _check_repo(repo):
        if repo.count("/") != 1 or ".." in repo:
            raise ValueError("bad repo")


class Minter:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def token_for(self, slug, repo, perms):
        self.calls.append((slug, repo, perms))
        if self.fail:
            raise RuntimeError("secret detail")
        return "opaque-installation-bearer", 0


class Multi:
    """Test helper: one bound handle per action, all sharing one client."""

    def __init__(self, client):
        self.client = client

    def run(self, action, args):
        handle = WorkflowVerbs(
            self.client, Policy(), "swu-wise", "opaque", "swu-wise", action, args.get("repo", REPO)
        )
        return handle.run(action, args)


def svc(*replies):
    client = Client(*replies)
    return Multi(client), client


class Tests(unittest.TestCase):
    def test_every_action_has_a_handler_and_spec(self):
        for action in ACTIONS:
            self.assertTrue(callable(getattr(WorkflowVerbs, action)))
        self.assertEqual(len(ACTIONS), 15)

    def test_least_permission_minted_and_ceiling_enforced(self):
        minter = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), minter, "swu-wise", "wf_run_rerun", REPO)
        self.assertEqual(minter.calls[0][2], {"actions": "write", "metadata": "read"})
        read_only = {"contents": "read", "metadata": "read"}
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(
                Client(), Policy(read_only), Minter(), "swu-wise", "wf_run_rerun", REPO
            )
        WorkflowVerbs.for_action(
            Client(), Policy(read_only), Minter(), "swu-wise", "wf_file_get", REPO
        )

    def test_denied_repo_action_and_unknown_action_and_mint_failure(self):
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(Client(), Policy(), Minter(), "swu-wise", "wf_tree", "x/y")
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(Client(), Policy(), Minter(), "swu-wise", "mint_token", REPO)
        with self.assertRaises(Unknown) as ctx:
            WorkflowVerbs.for_action(
                Client(), Policy(), Minter(fail=True), "swu-wise", "wf_tree", REPO
            )
        self.assertNotIn("secret", str(ctx.exception))

    def test_own_app_required(self):
        with self.assertRaises(Refused):
            WorkflowVerbs(Client(), Policy(), "swu-wise", "", "swu-wise", "wf_tree", REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs(Client(), Policy(), "swu-wise", "b", "swu-echo", "wf_tree", REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs(Client(), Policy(), "swu-wise", "b", "swu-wise", "mint_token", REPO)

    def test_handle_is_bound_to_its_action_and_repo(self):
        minter = Minter()
        handle = WorkflowVerbs.for_action(
            Client({}), Policy(), minter, "swu-wise", "wf_tree", REPO
        )
        handle.run("wf_tree", {"repo": REPO, "ref": "main"})
        with self.assertRaises(Refused):
            handle.run("wf_file_put", {"repo": REPO})
        with self.assertRaises(Refused):
            handle.run("wf_tree", {"repo": "Simple-With-Us/Other", "ref": "main"})
        self.assertEqual(len(minter.calls), 1)

    def test_path_and_repo_cannot_break_out_of_the_url(self):
        s, c = svc({"type": "file", "path": "a", "size": 1})
        s.run("wf_file_get", {"repo": REPO, "path": "docs/a b.md".replace(" ", "_"), "ref": "main"})
        for bad in ("a?x=1", "a#f", "a%2e", "a b", "a\n", "a&b=1", "a\\b"):
            with self.assertRaises(Refused, msg=bad):
                s.run("wf_file_get", {"repo": REPO, "path": bad, "ref": "main"})
        for bad in ("x/y?z", "x/y/z", "x", "../y/z"):
            with self.assertRaises(Refused, msg=bad):
                WorkflowVerbs.for_action(Client(), Policy(), Minter(), "swu-wise", "wf_tree", bad)

    def test_pr_list_head_filter_is_sent(self):
        s, c = svc([])
        s.run("wf_pr_list", {"repo": REPO, "head": "wise/x", "base": "main"})
        self.assertIn("head=Simple-With-Us%3Awise/x", c.calls[0][1].replace("%2F", "/"))
        self.assertIn("base=main", c.calls[0][1])

    def test_unexpected_upstream_shape_is_a_controlled_refusal(self):
        for action, args in (
            ("wf_runs", {"repo": REPO}),
            ("wf_run_jobs", {"repo": REPO, "run_id": 1}),
            ("wf_code_search", {"repo": REPO, "term": "x"}),
            ("wf_tree", {"repo": REPO, "ref": "main"}),
            ("wf_pr_list", {"repo": REPO}),
        ):
            s, _ = svc(["not", "an", "object"] if action != "wf_pr_list" else {"a": 1})
            with self.assertRaises(Refused, msg=action):
                s.run(action, args)

    def test_update_branch_and_merge_carry_atomic_head(self):
        s, c = svc({"message": "ok"}, {"merged": True, "sha": "b" * 40})
        s.run("wf_update_branch", {"repo": REPO, "number": 7, "expected_head_sha": SHA})
        self.assertEqual(c.calls[0][2], {"expected_head_sha": SHA})
        s.run(
            "wf_merge_guarded",
            {"repo": REPO, "number": 7, "method": "squash", "expected_head_sha": SHA},
        )
        self.assertEqual(c.calls[1][2], {"merge_method": "squash", "sha": SHA})
        for bad in ({}, {"expected_head_sha": "abc"}):
            with self.assertRaises(Refused):
                s.run("wf_merge_guarded", {"repo": REPO, "number": 7, "method": "squash", **bad})

    def test_file_put_refuses_default_branch_workflows_secrets_and_bad_input(self):
        good = {
            "repo": REPO,
            "path": "docs/a.md",
            "branch": "wise/x",
            "message": "m",
            "content_b64": base64.b64encode(b"hello").decode(),
        }
        s, c = svc({"default_branch": "main"}, {"commit": {"sha": "c" * 40}})
        self.assertEqual(s.run("wf_file_put", good)["commit"], "c" * 40)
        self.assertEqual(c.calls[1][2]["branch"], "wise/x")
        s, _ = svc({"default_branch": "main"})
        with self.assertRaises(Refused):
            s.run("wf_file_put", {**good, "branch": "main"})
        s, _ = svc()
        for bad in (
            {"path": ".github/workflows/a.yml"},
            {"path": "../a"},
            {"path": "/a"},
            {"branch": "a..b"},
            {"content_b64": "!!!"},
            {"content_b64": base64.b64encode(b"ghp_" + b"A" * 40).decode()},
            {"message": ""},
            {"extra": 1},
        ):
            with self.assertRaises(Refused, msg=str(bad)):
                s.run("wf_file_put", {**good, **bad})

    def test_search_term_cannot_widen_scope(self):
        s, c = svc({"total_count": 1, "items": [{"path": "a", "sha": "s"}]})
        s.run("wf_code_search", {"repo": REPO, "term": "relay"})
        self.assertIn(f"repo:{REPO}", c.calls[0][1])
        for term in ("org:x relay", "relay repo:other/x", "a\nb", "x" * 200, "a&b"):
            with self.assertRaises(Refused):
                s.run("wf_code_search", {"repo": REPO, "term": term})

    def test_reads_are_bounded_projections_and_incomplete(self):
        pulls = [{"number": i, "head": {"ref": "h", "sha": SHA}, "title": "t"} for i in range(500)]
        s, _ = svc(pulls)
        out = s.run("wf_pr_list", {"repo": REPO})
        self.assertEqual(len(out["pulls"]), 100)
        self.assertFalse(out["complete"])
        s, c = svc({"workflow_runs": [{"id": 1, "conclusion": "failure", "secret": "x"}]})
        out = s.run("wf_runs", {"repo": REPO, "head_sha": SHA, "status": "completed"})
        self.assertNotIn("secret", out["runs"][0])
        self.assertIn("head_sha=" + SHA, c.calls[0][1])
        for bad in ({"per_page": 500}, {"state": "x"}, {"x": 1}):
            s, _ = svc()
            with self.assertRaises(Refused):
                s.run("wf_pr_list", {"repo": REPO, **bad})

    def test_run_verbs_and_bad_ids(self):
        s, c = svc()
        s.run("wf_run_rerun", {"repo": REPO, "run_id": 5})
        s.run("wf_run_cancel", {"repo": REPO, "run_id": 5})
        s.run("wf_run_approve", {"repo": REPO, "run_id": 5})
        self.assertEqual(
            [p for _, p, _, _ in c.calls],
            [
                f"/repos/{REPO}/actions/runs/5/{x}"
                for x in ("rerun-failed-jobs", "cancel", "approve")
            ],
        )
        for bad in (0, -1, True, "5", 1.5):
            with self.assertRaises(Refused):
                s.run("wf_run_cancel", {"repo": REPO, "run_id": bad})

    def test_all_calls_use_the_minted_bearer_only(self):
        s, c = svc({"tree": []}, [], [])
        s.run("wf_tree", {"repo": REPO, "ref": "main"})
        s.run("wf_branches", {"repo": REPO})
        s.run("wf_commits", {"repo": REPO, "ref": "main", "path": "a/b.md"})
        self.assertTrue(all(b == "opaque" for *_, b in c.calls))
        self.assertIn("path=a%2Fb.md", c.calls[2][1])

    def test_spec_permissions_stay_inside_shared_ceiling(self):
        for action, (perms, _) in SPECS.items():
            self.assertEqual(set(perms) - set(CEILING), set(), action)


if __name__ == "__main__":
    unittest.main()
