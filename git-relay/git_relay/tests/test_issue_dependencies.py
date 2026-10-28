import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Minter, Policy


def item(n):
    return {
        "id": n + 100,
        "number": n,
        "title": "issue",
        "state": "open",
        "repository_url": "https://api.github.com/repos/" + REPO,
    }


class Dependencies(unittest.TestCase):
    def runverb(self, c, a, args):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, args
        )

    def test_fixed_routes_prereads_and_receipts(self):
        for a in ("wf_issue_dependency_add", "wf_issue_dependency_remove"):
            c = Client(item(1), item(2), item(2))
            out = self.runverb(c, a, {"repo": REPO, "number": 1, "blocker_number": 2})
            self.assertEqual(
                c.calls[:2],
                [("GET", f"/repos/{REPO}/issues/{n}", None, "own") for n in [1, 2]],
            )
            remove = a.endswith("remove")
            self.assertEqual(
                c.calls[-1],
                (
                    "DELETE" if remove else "POST",
                    f"/repos/{REPO}/issues/1/dependencies/blocked_by"
                    + ("/102" if remove else ""),
                    None if remove else {"issue_id": 102},
                    "own",
                ),
            )
            self.assertEqual(out["blocking"], not remove)
            self.assertIn(a, _REAPPLY_STATE_ACTIONS)
            self.assertEqual(SPECS[a], ({"issues": "write"}, True))
            self.assertIsNone(
                WorkflowVerbs.check_shape(
                    a, {"repo": REPO, "number": 1, "blocker_number": 2}, REPO
                )
            )

    def test_bad_receipts_and_identity_are_not_success(self):
        a = "wf_issue_dependency_add"
        args = {"repo": REPO, "number": 1, "blocker_number": 2}
        for bad in [
            {},
            item(3),
            {**item(2), "repository_url": "https://api.github.com/repos/Else/Repo"},
        ]:
            c = Client(item(1), item(2), bad)
            with self.assertRaises(RelayGitHubError):
                self.runverb(c, a, args)
        for bad in [
            {**item(2), "pull_request": {}},
            {**item(2), "number": 3},
            {**item(2), "repository_url": "https://evil.invalid/repos/" + REPO},
        ]:
            c = Client(item(1), bad)
            with self.assertRaises((Refused, RelayGitHubError)):
                self.runverb(c, a, args)
            self.assertTrue(all(x[0] == "GET" for x in c.calls))

    def test_closed_args_least_permission_and_ceiling(self):
        a = "wf_issue_dependency_remove"
        args = {"repo": REPO, "number": 1, "blocker_number": 2}
        for extra in [
            {"blocker_number": 1},
            {"blocker_number": True},
            {"blocker_number": 0},
            {"issue_id": 102},
            {"target_repo": "Else/Repo"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(a, {**args, **extra}, REPO)
        m = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", a, REPO)
        self.assertEqual(m.calls[0][2], {"issues": "write", "metadata": "read"})
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(
                Client(),
                Policy({"issues": "read", "metadata": "read"}),
                Minter(),
                "swu-wise",
                a,
                REPO,
            )

    def test_preread_malformed_is_definitely_unsent_and_rate_metadata_preserved(self):
        a = "wf_issue_dependency_add"
        args = {"repo": REPO, "number": 1, "blocker_number": 2}
        c = Client({})
        with self.assertRaises(Refused):
            self.runverb(c, a, args)
        self.assertTrue(all(x[0] == "GET" for x in c.calls))

        class RateClient(Client):
            def _call(self, *a, **kw):
                raise RelayGitHubError(429, "rate", retry_after=42)

        with self.assertRaises(RelayGitHubError) as ctx:
            self.runverb(RateClient(), a, args)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after, 42)
        from git_relay.server import _WORKFLOW_AUDIT_SCALARS

        self.assertIn("blocker_number", _WORKFLOW_AUDIT_SCALARS)

    def test_real_relay_mutation_unknown_holds_without_secret_diagnostics(self):
        import json
        import tempfile
        from pathlib import Path
        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        # Synthetic runtime-only fixture, never a credential.
        seat_token = "x" * 32
        config = json.dumps(
            {
                "swu-wise": {
                    "token": seat_token,
                    "app_slug": "swu-wise",
                    "mint_permissions": {"issues": "write", "metadata": "read"},
                    "actions": ["wf_issue_dependency_add"],
                    "repos": [REPO],
                }
            }
        )

        class Mint:
            def token_for(self, *args):
                return "synthetic-own-bearer-no-diagnostics", 0

        class Failing(Client):
            def _call(self, method, path, data=None, bearer=None):
                self.calls.append((method, path, data, bearer))
                if method == "GET":
                    return item(int(path.rsplit("/", 1)[1]))
                raise RelayGitHubError(502, "untrusted upstream detail")

        args = {"repo": REPO, "number": 1, "blocker_number": 2}
        with tempfile.TemporaryDirectory() as tmp:
            client = Failing()

            def make():
                return Relay(
                    SeatPolicy(config),
                    client,
                    AuditLog(str(Path(tmp) / "audit.jsonl")),
                    app_minter=Mint(),
                    workflow_verbs_enabled=True,
                )

            r = make()
            status, body = r.handle(seat_token, "wf_issue_dependency_add", args)
            self.assertEqual(status, 502, body)
            self.assertEqual(body["outcome"], "unknown")
            self.assertFalse(body["retryable"])
            self.assertNotIn("synthetic-own", json.dumps(body))
            self.assertNotIn("untrusted", json.dumps(body))
            self.assertEqual(
                r.handle(seat_token, "wf_issue_dependency_add", args)[0], 409
            )
            self.assertEqual(len([x for x in client.calls if x[0] == "POST"]), 1)
