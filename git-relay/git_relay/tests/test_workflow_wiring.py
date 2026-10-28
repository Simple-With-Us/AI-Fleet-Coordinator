import json
import os
import tempfile
import time
import unittest

from git_relay.audit import AuditLog
from git_relay.github_client import RelayGitHubError
from git_relay.policy import SeatPolicy
from git_relay.server import Relay

TOKEN = "w" * 32
SEATS = json.dumps(
    {
        "swu-wise": {
            "token": TOKEN,
            "actions": ["wf_runs", "wf_run_rerun", "wf_file_put", "wf_checks"],
            "repos": ["example/*"],
            "app_slug": "swu-wise",
            "mint_permissions": {
                "actions": "write",
                "contents": "write",
                "metadata": "read",
            },
        }
    }
)


class Minter:
    def __init__(self):
        self.calls = []

    def token_for(self, slug, repo, permissions):
        self.calls.append((slug, repo, dict(permissions)))
        return "MINTED-FAKE-TOKEN", time.time() + 3600


class Client:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def _call(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path, payload, bearer))
        if self.fail:
            raise self.fail
        if method == "GET":
            return {"workflow_runs": [], "total_count": 0}
        if method == "PUT":
            return {"commit": {"sha": "a" * 40}, "content": {"sha": "b" * 40}}
        return {}


def relay(tmp, enabled=True, client=None):
    return Relay(
        SeatPolicy(SEATS),
        client or Client(),
        AuditLog(os.path.join(tmp, "a.jsonl")),
        app_minter=Minter(),
        workflow_verbs_enabled=enabled,
    )


class WorkflowWiringTests(unittest.TestCase):
    def test_off_by_default_refuses_as_not_wired(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp, enabled=False)
            status, body = r.handle(TOKEN, "wf_runs", {"repo": "example/x"})
            self.assertEqual(status, 400)
            self.assertIn("not wired", body["error"])
            self.assertEqual((r.client.calls, r.app_minter.calls), ([], []))

    def test_read_runs_with_least_permission_and_needs_no_outcome_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp)
            for _ in range(2):
                status, _ = r.handle(TOKEN, "wf_runs", {"repo": "example/x"})
                self.assertEqual(status, 200)
            self.assertEqual(
                len(r.client.calls), 2, "reads are not replayed from the outcome store"
            )
            self.assertEqual(r.app_minter.calls[0][2], {"actions": "read", "metadata": "read"})
            self.assertEqual(r.client.calls[0][3], "MINTED-FAKE-TOKEN")

    def test_quota_is_counted_once_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = SeatPolicy(SEATS, rate_per_hour=2)
            r = Relay(
                policy,
                Client(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                app_minter=Minter(),
                workflow_verbs_enabled=True,
            )
            self.assertEqual(r.handle(TOKEN, "wf_runs", {"repo": "example/x"})[0], 200)
            self.assertEqual(r.handle(TOKEN, "wf_runs", {"repo": "example/x"})[0], 200)
            self.assertEqual(r.handle(TOKEN, "wf_runs", {"repo": "example/x"})[0], 403)

    def test_unlisted_action_and_other_repo_are_denied_before_any_mint(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp)
            self.assertEqual(r.handle(TOKEN, "wf_pr_list", {"repo": "example/x"})[0], 403)
            self.assertEqual(r.handle(TOKEN, "wf_runs", {"repo": "other/x"})[0], 403)
            self.assertEqual(r.app_minter.calls, [])

    def test_ceiling_shortfall_is_a_403_not_a_mint(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp)
            status, body = r.handle(TOKEN, "wf_checks", {"repo": "example/x", "ref": "c" * 40})
            self.assertEqual(status, 403)
            self.assertEqual(r.app_minter.calls, [])

    def test_bad_shape_is_a_400_before_any_upstream_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp)
            status, _ = r.handle(TOKEN, "wf_runs", {"repo": "example/x", "surprise": 1})
            self.assertEqual(status, 400)
            self.assertEqual(r.client.calls, [])

    def test_write_is_idempotent_and_audit_holds_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp)
            args = {
                "repo": "example/x",
                "path": "docs/a.md",
                "branch": "wise/x",
                "message": "SECRET-LOOKING-COMMIT-MESSAGE",
                "content_b64": "aGVsbG8gd29ybGQ=",
            }
            r.client._call = lambda m, p, payload=None, bearer=None: (
                r.client.calls.append((m, p, payload, bearer))
                or ({"default_branch": "main"} if m == "GET" else {"commit": {"sha": "a" * 40}})
            )
            first = r.handle(TOKEN, "wf_file_put", args)
            second = r.handle(TOKEN, "wf_file_put", args)
            self.assertEqual((first[0], second[0]), (200, 200))
            puts = [c for c in r.client.calls if c[0] == "PUT"]
            self.assertEqual(len(puts), 1, "the replay is served from the outcome store")
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                logged = fh.read()
            self.assertNotIn("SECRET-LOOKING-COMMIT-MESSAGE", logged)
            self.assertNotIn("aGVsbG8gd29ybGQ=", logged)
            self.assertNotIn("MINTED-FAKE-TOKEN", logged)
            self.assertIn("wf_file_put", logged)

    def test_upstream_5xx_on_a_write_holds_the_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp, client=Client(fail=RelayGitHubError(502, "boom")))
            status, body = r.handle(TOKEN, "wf_run_rerun", {"repo": "example/x", "run_id": 5})
            self.assertEqual(status, 502)
            self.assertEqual(body["outcome"], "unknown")
            again = r.handle(TOKEN, "wf_run_rerun", {"repo": "example/x", "run_id": 5})
            self.assertEqual(again[0], 409)

    def test_malformed_write_leaves_no_outcome_row_and_no_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "o.json")
            from git_relay.outcomes import OutcomeStore

            r = relay(tmp)
            r.outcomes = OutcomeStore(store_path)
            bad = {"repo": "example/x", "run_id": "five", "junk": 1}
            self.assertEqual(r.handle(TOKEN, "wf_run_rerun", bad)[0], 400)
            self.assertFalse(os.path.exists(store_path) and open(store_path).read().strip("{} \n"))
            self.assertEqual((r.client.calls, r.app_minter.calls), ([], []))
            self.assertEqual(
                r.handle(TOKEN, "wf_run_rerun", {"repo": "example/x", "run_id": 5})[0], 200
            )

    def test_read_upstream_failure_is_retryable_not_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp, client=Client(fail=RelayGitHubError(503, "x")))
            status, body = r.handle(TOKEN, "wf_runs", {"repo": "example/x"})
            self.assertEqual(status, 502)
            self.assertTrue(body["retryable"])
            self.assertNotIn("outcome", body)
            self.assertEqual(r.handle(TOKEN, "wf_runs", {"repo": "example/x"})[0], 502)

    def test_read_429_keeps_the_retry_after_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            exc = RelayGitHubError(429, "slow down")
            exc.retry_after = 17
            r = relay(tmp, client=Client(fail=exc))
            status, body = r.handle(TOKEN, "wf_runs", {"repo": "example/x"})
            self.assertEqual(
                (status, body["retry_after_seconds"], body["retryable"]), (429, 17, True)
            )

    def test_malformed_prior_sha_is_refused_before_any_upstream_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = relay(tmp)
            args = {
                "repo": "example/x",
                "path": "docs/a.md",
                "branch": "wise/x",
                "message": "m",
                "content_b64": "aGVsbG8=",
                "prior_sha": "not-a-sha",
            }
            self.assertEqual(r.handle(TOKEN, "wf_file_put", args)[0], 400)
            self.assertEqual((r.client.calls, r.app_minter.calls), ([], []))


if __name__ == "__main__":
    unittest.main()
