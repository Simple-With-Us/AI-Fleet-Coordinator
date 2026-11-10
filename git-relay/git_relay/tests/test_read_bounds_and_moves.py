"""Real-size read answers are not refused, and a moved repository is named, not an outage.

Bug B (Fleet GitOps deficiency rows 22, 31, 35, 47, 107, 109, 137, 138):  gr_wf_pr_reviews,
gr_wf_issue_comments, gr_wf_issue_timeline, gr_wf_compare and gr_wf_pr_get answered "git-relay
answered in a way this server could not read" on ordinary pull requests.  The relay's read bound
(500 nodes, depth 6, 64 KiB) refused them:  AFC PR 1494's 14th review crossed 500 nodes, CodeCaps
#319's pull request carried 72 KiB of text, and a compare of a few commits crossed both.

Bug D (row 84, OPS-1036):  a call on a transferred repository's old slug answered "unavailable".
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error

from git_relay.app_tokens import AppTokenMinter
from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient, RelayGitHubError, moved_repository_id
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.workflow_verbs import (
    READ_MAX_BYTES,
    READ_TOO_LARGE,
    Refused,
    WorkflowVerbs,
    _fit_comparison,
    _read_size,
    _validate_read,
)

from .test_workflow_verbs import REPO, SHA, Client, Policy

USER = {
    "login": "kody-ai[bot]",
    "id": 1,
    "node_id": "BOT_kgDO",
    "avatar_url": "https://avatars.githubusercontent.com/in/1?v=4",
    "gravatar_id": "",
    "url": "https://api.github.com/users/kody-ai%5Bbot%5D",
    "html_url": "https://github.com/apps/kody-ai",
    "followers_url": "https://api.github.com/users/kody-ai%5Bbot%5D/followers",
    "following_url": "https://api.github.com/users/kody-ai%5Bbot%5D/following{/other_user}",
    "gists_url": "https://api.github.com/users/kody-ai%5Bbot%5D/gists{/gist_id}",
    "starred_url": "https://api.github.com/users/kody-ai%5Bbot%5D/starred{/owner}{/repo}",
    "subscriptions_url": "https://api.github.com/users/kody-ai%5Bbot%5D/subscriptions",
    "organizations_url": "https://api.github.com/users/kody-ai%5Bbot%5D/orgs",
    "repos_url": "https://api.github.com/users/kody-ai%5Bbot%5D/repos",
    "events_url": "https://api.github.com/users/kody-ai%5Bbot%5D/events{/privacy}",
    "received_events_url": "https://api.github.com/users/kody-ai%5Bbot%5D/received_events",
    "type": "Bot",
    "user_view_type": "public",
    "site_admin": False,
}


def review(i, body=""):
    """A review shaped exactly like GitHub's (37 JSON nodes, as on AFC PR 1494)."""
    return {
        "id": 5816160000 + i,
        "node_id": "PRR_kwDO" + str(i),
        "user": dict(USER),
        "body": body,
        "state": "COMMENTED",
        "html_url": f"https://github.com/{REPO}/pull/1494#pullrequestreview-{i}",
        "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/1494",
        "author_association": "NONE",
        "_links": {
            "html": {"href": f"https://github.com/{REPO}/pull/1494#pullrequestreview-{i}"},
            "pull_request": {"href": f"https://api.github.com/repos/{REPO}/pulls/1494"},
        },
        "submitted_at": "2026-11-08T02:42:01Z",
        "commit_id": SHA,
    }


def commit(i):
    return {
        "sha": f"{i:040x}",
        "node_id": "C_" + str(i),
        "commit": {
            "author": {"name": "A", "email": "a@example.invalid", "date": "2026-11-01T00:00:00Z"},
            "committer": {
                "name": "A",
                "email": "a@example.invalid",
                "date": "2026-11-01T00:00:00Z",
            },
            "message": "change %d\n\n" % i + "detail " * 200,
            "tree": {"sha": SHA, "url": "https://api.github.com/x"},
            "comment_count": 0,
        },
        "author": dict(USER),
        "committer": dict(USER),
        "parents": [
            {"sha": SHA, "url": "https://api.github.com/x", "html_url": "https://github.com/x"}
        ],
    }


def run(action, reply, args):
    client = Client(reply)
    out = WorkflowVerbs(client, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(
        action, {"repo": REPO, **args}
    )
    return out, client


class RealSizeReads(unittest.TestCase):
    def test_twenty_eight_reviews_come_back_with_bodies_and_timestamps(self):
        rows = [
            review(i, body="" if i % 3 else "# Found critical issues\n" * 40) for i in range(28)
        ]
        out, _ = run("wf_pr_reviews", rows, {"number": 1494})
        self.assertEqual(len(out["reviews"]), 28)
        self.assertEqual(out["reviews"][0]["submitted_at"], "2026-11-08T02:42:01Z")
        self.assertTrue(out["reviews"][0]["body"].startswith("# Found critical issues"))
        # The old bound refused the 14th review;  50 (the page maximum) fit too.
        out, _ = run(
            "wf_pr_reviews", [review(i) for i in range(50)], {"number": 1494, "per_page": 50}
        )
        self.assertEqual(len(out["reviews"]), 50)

    def test_large_pull_request_and_long_comments_come_back(self):
        pull = {
            "id": 4,
            "number": 319,
            "state": "closed",
            "title": "t",
            "body": "release notes " * 5000,  # ~70 KB, as CodeCaps #319
            "user": dict(USER),
            "head": {"sha": SHA, "repo": {"owner": dict(USER), "full_name": REPO}},
            "base": {"sha": SHA, "repo": {"owner": dict(USER), "full_name": REPO}},
        }
        out, _ = run("wf_pr_get", pull, {"number": 319})
        self.assertEqual(out["pull"]["body"], pull["body"])
        comments = [{"id": i + 1, "body": "k" * 65536, "user": dict(USER)} for i in range(5)]
        out, _ = run("wf_issue_comments", comments, {"number": 1494})
        self.assertEqual(len(out["comments"]), 5)

    def test_compare_keeps_counts_and_says_what_it_left_out(self):
        files = [
            {"filename": f"f{i}", "patch": "+" * 4000, "status": "modified"} for i in range(300)
        ]
        data = {
            "status": "diverged",
            "ahead_by": 3,
            "behind_by": 700,
            "total_commits": 250,
            "commits": [commit(i) for i in range(250)],
            "files": files,
        }
        out, _ = run("wf_compare", data, {"base": "main", "head": "feature"})
        c = out["comparison"]
        self.assertEqual((c["status"], c["ahead_by"], c["behind_by"]), ("diverged", 3, 700))
        self.assertTrue(c["files_omitted"])
        self.assertNotIn("files", c)
        self.assertTrue(c.get("commits_truncated"))
        self.assertGreater(len(c["commits"]), 0)
        self.assertLessEqual(_read_size(out), READ_MAX_BYTES)
        # A small compare is untouched.
        small = dict(data, commits=[commit(1)], files=files[:2])
        self.assertEqual(_fit_comparison(small), small)

    def test_still_too_large_is_a_definite_refusal_not_an_unreadable_502(self):
        with self.assertRaises(Refused) as ctx:
            _validate_read(
                "wf_pr_get",
                {
                    "pull": {
                        "id": 1,
                        "number": 1,
                        "state": "open",
                        "head": {"sha": SHA},
                        "base": {"sha": SHA},
                        "body": "x" * (READ_MAX_BYTES + 1),
                    }
                },
            )
        self.assertEqual(str(ctx.exception), READ_TOO_LARGE)
        self.assertNotIsInstance(ctx.exception, RelayGitHubError)

    def test_malformed_shapes_stay_unverifiable(self):
        deep = 1
        for _ in range(13):
            deep = [deep]
        for result in (
            {
                "reviews": [{"id": 1, "state": "COMMENTED", "commit_id": SHA, "x": deep}],
                "complete": False,
            },
            {
                "reviews": [{"id": 1, "state": "COMMENTED", "commit_id": SHA, "x": float("inf")}],
                "complete": False,
            },
        ):
            with self.assertRaises(RelayGitHubError):
                _validate_read("wf_pr_reviews", result)


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def redirect(status, location):
    def opener(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            status,
            "Moved Permanently",
            Headers({"Location": location}),
            io.BytesIO(json.dumps({"message": "Moved Permanently", "url": location}).encode()),
        )

    return opener


class MovedRepository(unittest.TestCase):
    def test_client_names_the_redirect_and_never_follows_it(self):
        for status, path in ((301, ""), (301, "/pulls/183"), (307, "/issues/183/comments")):
            client = GitHubClient(
                "fixture",
                opener=redirect(status, f"https://api.github.com/repositories/1341401578{path}"),
            )
            with self.assertRaises(RelayGitHubError) as ctx:
                client._call("GET", f"/repos/jaywedgeworth22/Fleet-OPS{path}")
            self.assertEqual(ctx.exception.code, "repository_moved")
            self.assertEqual(ctx.exception.status, status)
            self.assertEqual(ctx.exception.moved_repository_id, 1341401578)

    def test_other_redirects_are_not_a_move(self):
        base = "https://api.github.com"
        for status, location in (
            (301, "https://evil.invalid/repositories/1"),
            (301, base + "/repositories/x"),
            (301, base + "/repositories/0"),
            (301, base + "/users/1"),
            (304, base + "/repositories/1"),
            (301, None),
        ):
            self.assertIsNone(moved_repository_id(base, status, Headers({"Location": location})))
        client = GitHubClient("fixture", opener=redirect(301, "https://evil.invalid/x"))
        with self.assertRaises(RelayGitHubError) as ctx:
            client._call("GET", "/repos/a/b")
        self.assertIsNone(ctx.exception.code)


TOKEN = "t" * 32
OLD = "jaywedgeworth22/Fleet-OPS"
NEW = "Simple-With-Us/Fleet-OPS"
REPO_ID = 1341401578


def seats(repos):
    return json.dumps(
        {
            "swu-test": {
                "auth": "token",
                "token": TOKEN,
                "identity": "TEST",
                "app_slug": "swu-test",
                "actions": ["wf_repo_get", "wf_pr_get"],
                "repos": repos,
                "mint_permissions": {"metadata": "read", "pull_requests": "read"},
            }
        }
    )


class Source:
    def secrets(self, names):
        return {names[0]: "42", names[1]: "fixture-pem"}


class FakeGitHub:
    """GitHub as the App sees a transferred repo:  the old slug redirects to its id."""

    def __init__(self, resolvable=True):
        self.calls, self.resolvable = [], resolvable

    def __call__(self, method, path, bearer, payload=None):
        self.calls.append((method, path, payload))
        if path == "/app":
            return {"id": 42, "slug": "swu-test"}
        if path == f"/repos/{OLD}/installation":
            raise RelayGitHubError(
                301, "repository moved", code="repository_moved", moved_repository_id=REPO_ID
            )
        if path == f"/repositories/{REPO_ID}/installation":
            if not self.resolvable:
                raise RelayGitHubError(404, "Not Found")
            return {"id": 7, "app_id": 42, "suspended_at": None}
        if path == "/app/installations/7/access_tokens":
            return {"token": "minted-fixture", "expires_at": "2026-11-10T23:00:00Z"}
        if path == f"/repositories/{REPO_ID}":
            return {"id": REPO_ID, "full_name": NEW}
        if path == "/installation/token":
            return {}
        raise AssertionError(path)


class MovedThroughTheRelay(unittest.TestCase):
    def relay(self, tmp, repos, api):
        minter = AppTokenMinter(Source(), api, signer=lambda app_id, pem, now: "jwt-fixture")
        return Relay(
            SeatPolicy(seats(repos)),
            Client(),
            AuditLog(os.path.join(tmp, "audit")),
            app_minter=minter,
            workflow_verbs_enabled=True,
        )

    def test_old_slug_names_the_new_one(self):
        api = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            status, body = self.relay(tmp, ["jaywedgeworth22/*", "Simple-With-Us/*"], api).handle(
                TOKEN, "wf_pr_get", {"repo": OLD, "number": 183}
            )
        self.assertEqual(status, 400)
        self.assertEqual(
            body,
            {
                "error": "repository moved",
                "github_status": 301,
                "retryable": False,
                "repository_id": REPO_ID,
                "moved_to": NEW,
            },
        )
        mint = [c for c in api.calls if c[1] == "/app/installations/7/access_tokens"]
        # Least privilege:  one repository, metadata read, revoked after the one read.
        self.assertEqual(
            mint,
            [
                (
                    "POST",
                    "/app/installations/7/access_tokens",
                    {"repository_ids": [REPO_ID], "permissions": {"metadata": "read"}},
                )
            ],
        )
        self.assertEqual(api.calls[-1][:2], ("DELETE", "/installation/token"))
        self.assertNotIn("minted-fixture", json.dumps(body))

    def test_new_slug_outside_the_seat_policy_is_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, body = self.relay(tmp, ["jaywedgeworth22/*"], FakeGitHub()).handle(
                TOKEN, "wf_repo_get", {"repo": OLD}
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "repository moved")
        self.assertEqual(body["repository_id"], REPO_ID)
        self.assertNotIn("moved_to", body)

    def test_unresolvable_new_name_still_reports_the_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, body = self.relay(
                tmp, ["jaywedgeworth22/*", "Simple-With-Us/*"], FakeGitHub(False)
            ).handle(TOKEN, "wf_repo_get", {"repo": OLD})
        self.assertEqual(
            (status, body["error"], body["retryable"]), (400, "repository moved", False)
        )
        self.assertNotIn("moved_to", body)


if __name__ == "__main__":
    unittest.main()
