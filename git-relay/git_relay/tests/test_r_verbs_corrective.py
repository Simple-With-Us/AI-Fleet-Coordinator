"""Corrective coverage for the R-verb source patch v2:

- custody write verbs wf_pr_create / wf_pr_edit / wf_pr_close with preflight pins
- truthful census pagination in wf_pr_list and wf_checks (tri-state complete)
- wf_merge_guarded optional commit_title / commit_message and tightened receipt
- opt-in larger-text class (allow_large_text is True) on blob/tree verbs
"""

import base64
import hashlib
import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.policy import SeatPolicy
from git_relay.server import PreReadUnavailable
from git_relay.workflow_verbs import ACTIONS, SPECS, Refused

try:
    from test_workflow_verbs import REPO, SHA, Client, Multi, Policy, svc
except ImportError:
    from .test_workflow_verbs import REPO, SHA, Client, Multi, svc

SECRET_TITLE = "ghp_" + "x" * 24
BRANCH_OK = {"name": "wise/x", "commit": {"sha": SHA}}


def pr(
    number=7,
    state="open",
    head_sha=SHA,
    head_ref="wise/x",
    base_ref="main",
    title="t",
    body="b",
    head_repo=REPO,
    user_type="User",
):
    return {
        "number": number,
        "state": state,
        "head": {
            "ref": head_ref,
            "sha": head_sha,
            "repo": ({"full_name": head_repo} if head_repo else None),
        },
        "base": {"ref": base_ref, "sha": SHA},
        "title": title,
        "body": body,
        "draft": False,
        "user": {"login": "jay", "type": user_type},
        "created_at": "2026-11-02T00:00:00Z",
        "updated_at": "2026-11-03T00:00:00Z",
        "merged_at": None,
    }


def check_run(i, head_sha=SHA, app_id=123, app_slug="kody-ai"):
    return {
        "id": i,
        "name": "kody",
        "status": "completed",
        "conclusion": "success",
        "completed_at": "2026-11-03T00:00:00Z",
        "head_sha": head_sha,
        "app": {"id": app_id, "slug": app_slug},
    }


def checks_page(runs, total):
    return {"total_count": total, "check_runs": runs}


class FailingClient(Client):
    def __init__(self, status, *replies):
        super().__init__(*replies)
        self.status = status

    def _call(
        self,
        method,
        path,
        payload=None,
        bearer=None,
        expected_status=None,
        allow_empty_object=False,
    ):
        self.calls.append((method, path, payload, bearer))
        raise RelayGitHubError(self.status, "upstream down")


def failing_svc(status):
    client = FailingClient(status)
    return Multi(client), client


class SpecTests(unittest.TestCase):
    def test_new_verbs_registered_with_pull_requests_write(self):
        for action in ("wf_pr_create", "wf_pr_edit", "wf_pr_close"):
            self.assertIn(action, ACTIONS)
            self.assertIn(action, SeatPolicy.WORKFLOW_ACTIONS)
            self.assertEqual(SPECS[action], ({"pull_requests": "write"}, True))


class PrCreateTests(unittest.TestCase):
    ARGS = {
        "repo": REPO,
        "base": "main",
        "head": "wise/x",
        "title": "t",
        "body": "b",
        "expected_head_sha": SHA,
    }

    def test_happy_path_receipt_verified(self):
        s, c = svc(BRANCH_OK, pr())
        out = s.run("wf_pr_create", dict(self.ARGS))
        self.assertEqual(out["pull"]["number"], 7)
        self.assertEqual(out["pull"]["head_sha"], SHA)
        self.assertEqual(c.calls[0][0], "GET")  # branch preflight first
        method, path, payload, _ = c.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(payload, {"title": "t", "body": "b", "base": "main", "head": "wise/x"})

    def test_tampered_receipt_is_502(self):
        s, _ = svc(BRANCH_OK, pr(title="DIFFERENT"))
        with self.assertRaises(RelayGitHubError):
            s.run("wf_pr_create", dict(self.ARGS))

    def test_secret_shaped_title_refused_before_any_call(self):
        s, c = svc()
        with self.assertRaises(Refused):
            s.run("wf_pr_create", {**self.ARGS, "title": SECRET_TITLE})
        self.assertEqual(c.calls, [])

    def test_branch_preflight_unavailable_blocks_write(self):
        s, c = failing_svc(500)
        with self.assertRaises(PreReadUnavailable):
            s.run("wf_pr_create", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)  # only the GET, no POST

    def test_branch_not_at_expected_sha_refused_with_zero_writes(self):
        s, c = svc({"name": "wise/x", "commit": {"sha": "b" * 40}})
        with self.assertRaises(Refused):
            s.run("wf_pr_create", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)  # only the GET, no POST


class PrEditTests(unittest.TestCase):
    ARGS = {
        "repo": REPO,
        "number": 7,
        "title": "n",
        "expected_head_sha": SHA,
        "expected_base_ref": "main",
    }

    def test_nothing_to_edit_refused(self):
        s, c = svc()
        with self.assertRaises(Refused):
            s.run(
                "wf_pr_edit",
                {"repo": REPO, "number": 7, "expected_head_sha": SHA, "expected_base_ref": "main"},
            )
        self.assertEqual(c.calls, [])

    def test_pre_read_unavailable_blocks_write(self):
        s, c = failing_svc(500)
        with self.assertRaises(PreReadUnavailable):
            s.run("wf_pr_edit", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)  # only the GET, no PATCH

    def test_head_mismatch_refused_with_zero_writes(self):
        s, c = svc(pr(head_sha="b" * 40))
        with self.assertRaises(Refused):
            s.run("wf_pr_edit", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)

    def test_closed_pr_refused(self):
        s, c = svc(pr(state="closed"))
        with self.assertRaises(Refused):
            s.run("wf_pr_edit", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)

    def test_base_mismatch_refused(self):
        s, c = svc(pr(base_ref="release"))
        with self.assertRaises(Refused):
            s.run("wf_pr_edit", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)

    def test_fork_or_deleted_head_repo_refused(self):
        for head_repo in ("other/repo", None):
            s, c = svc(pr(head_repo=head_repo))
            with self.assertRaises(Refused, msg=head_repo):
                s.run("wf_pr_edit", dict(self.ARGS))
            self.assertEqual(len(c.calls), 1)

    def test_happy_path(self):
        s, c = svc(pr(), pr(title="n"))
        out = s.run("wf_pr_edit", dict(self.ARGS))
        self.assertEqual(out["pull"]["title"], "n")
        self.assertEqual(c.calls[1][0], "PATCH")
        self.assertEqual(c.calls[1][2], {"title": "n"})

    def test_receipt_head_moved_is_502(self):
        s, _ = svc(pr(), pr(head_sha="c" * 40, title="n"))
        with self.assertRaises(RelayGitHubError):
            s.run("wf_pr_edit", dict(self.ARGS))


class PrCloseTests(unittest.TestCase):
    ARGS = {"repo": REPO, "number": 7, "expected_head_sha": SHA, "expected_base_ref": "main"}

    def test_not_open_refused(self):
        s, c = svc(pr(state="closed"))
        with self.assertRaises(Refused):
            s.run("wf_pr_close", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)

    def test_base_mismatch_refused(self):
        s, c = svc(pr(base_ref="release"))
        with self.assertRaises(Refused):
            s.run("wf_pr_close", dict(self.ARGS))
        self.assertEqual(len(c.calls), 1)

    def test_happy_path(self):
        s, c = svc(pr(), pr(state="closed"))
        out = s.run("wf_pr_close", dict(self.ARGS))
        self.assertEqual(out["pull"]["state"], "closed")
        self.assertEqual(c.calls[1][2], {"state": "closed"})

    def test_still_open_receipt_is_502(self):
        s, _ = svc(pr(), pr())
        with self.assertRaises(RelayGitHubError):
            s.run("wf_pr_close", dict(self.ARGS))


class PrListTests(unittest.TestCase):
    def test_two_pages_complete_with_metadata_projection(self):
        s, _ = svc([pr(number=i) for i in range(1, 31)], [pr(number=i) for i in range(31, 36)])
        out = s.run("wf_pr_list", {"repo": REPO})
        self.assertEqual(len(out["pulls"]), 35)
        self.assertTrue(out["complete"])
        row = out["pulls"][0]
        for key in (
            "number",
            "title",
            "draft",
            "head_ref",
            "head_sha",
            "base_ref",
            "base_sha",
            "author",
            "author_type",
            "created_at",
            "updated_at",
            "merged_at",
            "auto_merge",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["author"], "jay")
        self.assertEqual(row["author_type"], "User")

    def test_duplicate_number_is_502(self):
        s, _ = svc([pr(number=1), pr(number=1)])
        with self.assertRaises(RelayGitHubError):
            s.run("wf_pr_list", {"repo": REPO})


class ChecksTests(unittest.TestCase):
    def test_mutable_ref_refused(self):
        s, c = svc()
        with self.assertRaises(Refused):
            s.run("wf_checks", {"repo": REPO, "ref": "main"})
        self.assertEqual(c.calls, [])

    def test_missing_required_fields_is_502_not_empty_complete(self):
        for bad in (
            {},
            {"check_runs": []},
            {"total_count": 0},
            {"total_count": "0", "check_runs": []},
        ):
            s, _ = svc(bad)
            with self.assertRaises(RelayGitHubError, msg=repr(bad)):
                s.run("wf_checks", {"repo": REPO, "ref": SHA})

    def test_short_page_census_mismatch_is_unknown_not_complete(self):
        # total_count 50 but the stream ends after one short page: FALSE GREEN
        s, _ = svc(checks_page([check_run(1)], 50))
        out = s.run("wf_checks", {"repo": REPO, "ref": SHA})
        self.assertEqual(out["complete"], "unknown")

    def test_census_shift_mid_read_is_unknown(self):
        s, _ = svc(
            checks_page([check_run(i) for i in range(1, 31)], 33),
            checks_page([check_run(i) for i in range(31, 34)], 50),
        )
        out = s.run("wf_checks", {"repo": REPO, "ref": SHA})
        self.assertEqual(out["complete"], "unknown")

    def test_two_pages_complete_true(self):
        s, _ = svc(
            checks_page([check_run(i) for i in range(1, 31)], 33),
            checks_page([check_run(i) for i in range(31, 34)], 33),
        )
        out = s.run("wf_checks", {"repo": REPO, "ref": SHA})
        self.assertEqual(len(out["checks"]), 33)
        self.assertTrue(out["complete"])
        row = out["checks"][0]
        self.assertEqual(row["head_sha"], SHA)
        self.assertEqual(row["app_slug"], "kody-ai")
        self.assertEqual(row["app_id"], 123)

    def test_capped_is_incomplete(self):
        s, _ = svc(
            *[checks_page([check_run(30 * p + i + 1) for i in range(30)], 1000) for p in range(10)]
        )
        out = s.run("wf_checks", {"repo": REPO, "ref": SHA})
        self.assertEqual(len(out["checks"]), 300)
        self.assertFalse(out["complete"])

    def test_duplicate_id_is_502(self):
        s, _ = svc(checks_page([check_run(1), check_run(1)], 2))
        with self.assertRaises(RelayGitHubError):
            s.run("wf_checks", {"repo": REPO, "ref": SHA})

    def test_foreign_head_sha_row_is_502(self):
        s, _ = svc(checks_page([check_run(1, head_sha="b" * 40)], 1))
        with self.assertRaises(RelayGitHubError):
            s.run("wf_checks", {"repo": REPO, "ref": SHA})

    def test_missing_app_metadata_is_502(self):
        row = check_run(1)
        del row["app"]
        s, _ = svc(checks_page([row], 1))
        with self.assertRaises(RelayGitHubError):
            s.run("wf_checks", {"repo": REPO, "ref": SHA})


class MergeGuardedTests(unittest.TestCase):
    def test_title_and_message_passthrough(self):
        s, c = svc({"merged": True, "sha": "b" * 40})
        s.run(
            "wf_merge_guarded",
            {
                "repo": REPO,
                "number": 7,
                "method": "squash",
                "expected_head_sha": SHA,
                "commit_title": "t",
                "commit_message": "m",
            },
        )
        self.assertEqual(c.calls[0][2]["commit_title"], "t")
        self.assertEqual(c.calls[0][2]["commit_message"], "m")

    def test_empty_message_preserved(self):
        s, c = svc({"merged": True, "sha": "b" * 40})
        s.run(
            "wf_merge_guarded",
            {
                "repo": REPO,
                "number": 7,
                "method": "squash",
                "expected_head_sha": SHA,
                "commit_message": "",
            },
        )
        self.assertEqual(c.calls[0][2]["commit_message"], "")

    def test_merged_false_is_502(self):
        s, _ = svc({"merged": False, "message": "Not mergeable"})
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_merge_guarded",
                {"repo": REPO, "number": 7, "method": "squash", "expected_head_sha": SHA},
            )

    def test_short_receipt_sha_is_502(self):
        s, _ = svc({"merged": True, "sha": "abc123"})
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_merge_guarded",
                {"repo": REPO, "number": 7, "method": "squash", "expected_head_sha": SHA},
            )


class LargeTextTests(unittest.TestCase):
    TEXT = "x" * 83854  # over the 40000 base cap, under the 100000 opt-in cap

    def blob_sha(self, raw):
        return hashlib.sha1(
            b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
        ).hexdigest()

    def test_blob_create_large_refused_without_flag(self):
        s, c = svc()
        with self.assertRaises(Refused):
            s.run("wf_git_blob_create", {"repo": REPO, "text": self.TEXT})
        self.assertEqual(c.calls, [])

    def test_blob_create_large_with_flag(self):
        sha = self.blob_sha(self.TEXT.encode())
        s, _ = svc({"sha": sha})
        out = s.run(
            "wf_git_blob_create", {"repo": REPO, "text": self.TEXT, "allow_large_text": True}
        )
        self.assertEqual(out["blob"]["sha"], sha)

    def test_non_bool_flag_refused(self):
        s, _ = svc()
        for verb in ("wf_git_blob_create", "wf_git_blob_get"):
            with self.assertRaises(Refused, msg=verb):
                args = {"repo": REPO, "allow_large_text": "yes"}
                if verb == "wf_git_blob_create":
                    args["text"] = "x"
                else:
                    args["sha"] = SHA
                s.run(verb, args)
        with self.assertRaises(Refused):
            s.run(
                "wf_git_tree_create",
                {
                    "repo": REPO,
                    "base_tree": SHA,
                    "files": [{"path": "a", "text": "x", "mode": "100644"}],
                    "allow_large_text": "yes",
                },
            )

    def test_explicit_false_flag_means_base_cap(self):
        small_tree = [{"path": "a", "text": "x", "mode": "100644"}]
        s, _ = svc({"sha": "b" * 40, "truncated": False, "tree": []})
        out = s.run(
            "wf_git_tree_create",
            {"repo": REPO, "base_tree": SHA, "files": small_tree, "allow_large_text": False},
        )
        self.assertEqual(out["tree"]["files"], 1)
        s, _ = svc({"sha": self.blob_sha(b"x")})
        out = s.run("wf_git_blob_create", {"repo": REPO, "text": "x", "allow_large_text": False})
        self.assertEqual(out["blob"]["sha"], self.blob_sha(b"x"))
        raw = b"x"
        reply = {
            "sha": self.blob_sha(raw),
            "encoding": "base64",
            "size": 1,
            "content": base64.b64encode(raw).decode(),
        }
        s, _ = svc(reply)
        out = s.run(
            "wf_git_blob_get", {"repo": REPO, "sha": self.blob_sha(raw), "allow_large_text": False}
        )
        self.assertEqual(out["blob"]["text"], "x")
        s, c = svc()
        with self.assertRaises(Refused):  # large text still held to the base cap
            s.run(
                "wf_git_blob_create",
                {"repo": REPO, "text": self.TEXT, "allow_large_text": False},
            )
        self.assertEqual(c.calls, [])

    def test_tree_cumulative_cap_with_flag(self):
        files = [{"path": f"f{i}.txt", "text": "y" * 20000, "mode": "100644"} for i in range(3)]
        s, c = svc()
        with self.assertRaises(Refused):  # 60000 > 40000 base cumulative cap
            s.run("wf_git_tree_create", {"repo": REPO, "base_tree": SHA, "files": files})
        self.assertEqual(c.calls, [])
        s, _ = svc({"sha": "b" * 40, "truncated": False, "tree": []})
        out = s.run(
            "wf_git_tree_create",
            {"repo": REPO, "base_tree": SHA, "files": files, "allow_large_text": True},
        )
        self.assertEqual(out["tree"]["files"], 3)

    def test_blob_get_large_refused_without_flag(self):
        raw = self.TEXT.encode()
        reply = {
            "sha": self.blob_sha(raw),
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode(),
        }
        s, _ = svc(reply)
        with self.assertRaises(Refused):
            s.run("wf_git_blob_get", {"repo": REPO, "sha": self.blob_sha(raw)})

    def test_blob_get_large_with_flag(self):
        raw = self.TEXT.encode()
        sha = self.blob_sha(raw)
        reply = {
            "sha": sha,
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode(),
        }
        s, _ = svc(reply)
        out = s.run("wf_git_blob_get", {"repo": REPO, "sha": sha, "allow_large_text": True})
        self.assertEqual(out["blob"]["text"], self.TEXT)

    def test_large_text_envelope_actions_and_full_scan(self):
        from git_relay.secret_scan import MAX_SCAN_NODES, SecretInputError, scan_args
        from git_relay.server import (
            LARGE_TEXT_REQUEST_ACTIONS,
            MAX_LARGE_TEXT_REQUEST_BYTES,
        )

        self.assertEqual(
            LARGE_TEXT_REQUEST_ACTIONS,
            frozenset({"wf_git_blob_create", "wf_git_tree_create"}),
        )
        self.assertGreaterEqual(MAX_LARGE_TEXT_REQUEST_BYTES, 100000)
        text = "a" * 150000 + "\n-----BEGIN " + "PRIVATE KEY-----"
        with self.assertRaises(SecretInputError):
            scan_args({"text": text}, _budget=[MAX_SCAN_NODES, MAX_LARGE_TEXT_REQUEST_BYTES])


class MutablePrClient:
    """Mutable upstream double: external actors can rename, reopen or move the head."""

    def __init__(self):
        self.state = {
            "number": 7,
            "state": "open",
            "title": "old",
            "body": "b",
            "head": {"sha": SHA, "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        }
        self.calls = []
        self.fail_patch = False

    def _call(self, method, path, payload=None, bearer=None):
        import json as _json

        self.calls.append((method, path))
        if method == "GET" and path == f"/repos/{REPO}/pulls/7":
            return _json.loads(_json.dumps(self.state))
        if method == "PATCH" and path == f"/repos/{REPO}/pulls/7":
            if self.fail_patch:
                raise RelayGitHubError(502, "unknown")
            self.state.update(payload or {})
            return _json.loads(_json.dumps(self.state))
        raise AssertionError(path)


class ReapplyMutableUpstreamTests(unittest.TestCase):
    """Known-success setters re-dispatch on resubmission; held writes never do."""

    def _relay(self, client, directory):
        import json as _json
        from pathlib import Path

        from git_relay.audit import AuditLog
        from git_relay.server import Relay

        config = _json.dumps(
            {
                "swu-wise": {
                    "token": "s" * 32,
                    "app_slug": "swu-wise",
                    "mint_permissions": {"pull_requests": "write", "metadata": "read"},
                    "actions": ["wf_pr_edit", "wf_pr_close"],
                    "repos": [REPO],
                }
            }
        )

        class Minter:
            def token_for(self, *args):
                return "synthetic-own-token", 0

        return Relay(
            SeatPolicy(config),
            client,
            AuditLog(str(Path(directory) / "audit.jsonl")),
            app_minter=Minter(),
            workflow_verbs_enabled=True,
        )

    def test_edit_reapply_reasserts_after_external_rename(self):
        import tempfile

        client = MutablePrClient()
        args = {
            "repo": REPO,
            "number": 7,
            "title": "new",
            "expected_head_sha": SHA,
            "expected_base_ref": "main",
        }
        with tempfile.TemporaryDirectory() as d:
            relay = self._relay(client, d)
            status, _ = relay.handle("s" * 32, "wf_pr_edit", args)
            self.assertEqual(status, 200)
            client.state["title"] = "foreign"
            status, _ = relay.handle("s" * 32, "wf_pr_edit", args)
            self.assertEqual(status, 200)
        self.assertEqual(
            [c for c in client.calls if c[0] == "PATCH"], [("PATCH", f"/repos/{REPO}/pulls/7")] * 2
        )
        # Setter behavior: the external title is overwritten by the re-asserted state.
        self.assertEqual(client.state["title"], "new")

    def test_close_reapply_reasserts_after_external_reopen(self):
        import tempfile

        client = MutablePrClient()
        args = {
            "repo": REPO,
            "number": 7,
            "expected_head_sha": SHA,
            "expected_base_ref": "main",
        }
        with tempfile.TemporaryDirectory() as d:
            relay = self._relay(client, d)
            status, _ = relay.handle("s" * 32, "wf_pr_close", args)
            self.assertEqual(status, 200)
            self.assertEqual(client.state["state"], "closed")
            client.state["state"] = "open"  # external reopen at the same sha/base
            status, _ = relay.handle("s" * 32, "wf_pr_close", args)
            self.assertEqual(status, 200)
        self.assertEqual(len([c for c in client.calls if c[0] == "PATCH"]), 2)
        self.assertEqual(client.state["state"], "closed")

    def test_head_move_refused_with_pre_read_get_only(self):
        import tempfile

        client = MutablePrClient()
        args = {
            "repo": REPO,
            "number": 7,
            "title": "new",
            "expected_head_sha": SHA,
            "expected_base_ref": "main",
        }
        with tempfile.TemporaryDirectory() as d:
            relay = self._relay(client, d)
            status, _ = relay.handle("s" * 32, "wf_pr_edit", args)
            self.assertEqual(status, 200)
            client.state["head"]["sha"] = "c" * 40
            before = list(client.calls)
            status, _ = relay.handle("s" * 32, "wf_pr_edit", args)
            self.assertEqual(status, 400)
        self.assertEqual(client.calls[len(before) :], [("GET", f"/repos/{REPO}/pulls/7")])
        self.assertEqual(client.state["title"], "new")  # no write attempted

    def test_uncertain_patch_retry_held_with_zero_network(self):
        import tempfile

        client = MutablePrClient()
        client.fail_patch = True
        args = {
            "repo": REPO,
            "number": 7,
            "expected_head_sha": SHA,
            "expected_base_ref": "main",
        }
        with tempfile.TemporaryDirectory() as d:
            relay = self._relay(client, d)
            status, _ = relay.handle("s" * 32, "wf_pr_close", args)
            self.assertEqual(status, 502)
            before = list(client.calls)
            status, _ = relay.handle("s" * 32, "wf_pr_close", args)
            self.assertEqual(status, 409)
        self.assertEqual(client.calls, before)  # zero upstream calls on the held retry


class PrRaceFixtureTests(unittest.TestCase):
    """Reviewer-reproduced races: the receipt must catch a foreign move that
    lands BETWEEN the pre-read pin and the write, or the pin is decorative."""

    def test_create_receipt_foreign_head_move_is_502(self):
        # Branch pre-read at expected sha, but the created PR's head moved.
        s, c = svc(BRANCH_OK, pr(head_sha="c" * 40))
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_pr_create",
                {
                    "repo": REPO,
                    "base": "main",
                    "head": "wise/x",
                    "title": "t",
                    "body": "b",
                    "expected_head_sha": SHA,
                },
            )

    def test_edit_receipt_base_retarget_is_502(self):
        # Pre-read base main; PATCH returns a PR retargeted to release.
        s, _ = svc(pr(), pr(title="n", base_ref="release"))
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_pr_edit",
                {
                    "repo": REPO,
                    "number": 7,
                    "title": "n",
                    "expected_head_sha": SHA,
                    "expected_base_ref": "main",
                },
            )

    def test_edit_receipt_head_repo_changed_is_502(self):
        s, _ = svc(pr(), pr(title="n", head_repo="other/repo"))
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_pr_edit",
                {
                    "repo": REPO,
                    "number": 7,
                    "title": "n",
                    "expected_head_sha": SHA,
                    "expected_base_ref": "main",
                },
            )

    def test_edit_receipt_state_flipped_is_502(self):
        s, _ = svc(pr(), pr(title="n", state="closed"))
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_pr_edit",
                {
                    "repo": REPO,
                    "number": 7,
                    "title": "n",
                    "expected_head_sha": SHA,
                    "expected_base_ref": "main",
                },
            )

    def test_close_receipt_base_retarget_is_502(self):
        s, _ = svc(pr(), pr(state="closed", base_ref="release"))
        with self.assertRaises(RelayGitHubError):
            s.run(
                "wf_pr_close",
                {"repo": REPO, "number": 7, "expected_head_sha": SHA, "expected_base_ref": "main"},
            )


class AuthorMetadataTests(unittest.TestCase):
    def test_pr_edit_and_close_are_reapply_state_actions(self):
        from git_relay.server import _REAPPLY_STATE_ACTIONS

        self.assertIn("wf_pr_edit", _REAPPLY_STATE_ACTIONS)
        self.assertIn("wf_pr_close", _REAPPLY_STATE_ACTIONS)
        self.assertNotIn("wf_pr_create", _REAPPLY_STATE_ACTIONS)

    def test_pr_list_author_type_missing_refuses_not_defaults(self):
        row = pr()
        row["user"] = {"login": "ghost"}  # no type
        s, _ = svc([row])
        with self.assertRaises(RelayGitHubError):
            s.run("wf_pr_list", {"repo": REPO, "state": "open"})

    def test_pr_list_null_author_stays_null(self):
        row = pr()
        row["user"] = None
        s, _ = svc([row])
        out = s.run("wf_pr_list", {"repo": REPO, "state": "open"})
        self.assertIsNone(out["pulls"][0]["author"])
        self.assertIsNone(out["pulls"][0]["author_type"])

    def test_commit_read_returns_native_and_account_metadata(self):
        who = {"name": "n", "email": "e", "date": "d"}
        reply = {
            "sha": SHA,
            "commit": {"tree": {"sha": SHA}, "message": "m", "author": who, "committer": who},
            "parents": [],
            "author": {"login": "jay", "type": "User"},
            "committer": {"login": "kody", "type": "Bot"},
        }
        s, _ = svc(reply)
        out = s.run("wf_git_commit_get", {"repo": REPO, "sha": SHA})
        c = out["commit"]
        self.assertEqual(c["author"], who)
        self.assertEqual(c["author_account"], {"login": "jay", "type": "User"})
        self.assertEqual(c["committer_account"], {"login": "kody", "type": "Bot"})

    def test_commit_read_null_account_is_true_unknown(self):
        who = {"name": "n", "email": "e", "date": "d"}
        reply = {
            "sha": SHA,
            "commit": {"tree": {"sha": SHA}, "message": "m", "author": who, "committer": who},
            "parents": [],
            "author": None,
            "committer": None,
        }
        s, _ = svc(reply)
        out = s.run("wf_git_commit_get", {"repo": REPO, "sha": SHA})
        self.assertIsNone(out["commit"]["author_account"])
        self.assertIsNone(out["commit"]["committer_account"])

    def test_commit_read_bad_account_refuses(self):
        who = {"name": "n", "email": "e", "date": "d"}
        for bad in ({"login": "x"}, {"login": "x", "type": "Svc"}, {"type": "User"}, "x"):
            reply = {
                "sha": SHA,
                "commit": {"tree": {"sha": SHA}, "message": "m", "author": who, "committer": who},
                "parents": [],
                "author": bad,
                "committer": None,
            }
            s, _ = svc(reply)
            with self.assertRaises(RelayGitHubError, msg=repr(bad)):
                s.run("wf_git_commit_get", {"repo": REPO, "sha": SHA})

    def test_commit_read_bad_native_author_refuses(self):
        who = {"name": "n", "email": "e", "date": "d"}
        reply = {
            "sha": SHA,
            "commit": {
                "tree": {"sha": SHA},
                "message": "m",
                "author": {"name": 7},
                "committer": who,
            },
            "parents": [],
            "author": None,
            "committer": None,
        }
        s, _ = svc(reply)
        with self.assertRaises(RelayGitHubError):
            s.run("wf_git_commit_get", {"repo": REPO, "sha": SHA})


if __name__ == "__main__":
    unittest.main()
