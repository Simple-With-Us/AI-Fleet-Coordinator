import unittest

from git_relay.typed_disarm import DISABLE_MUTATION, Refused, TypedDisarm, Unknown

HEAD = "a" * 40
ARGS = {"repo": "Simple-With-Us/Socratic-Trade", "number": 9, "expected_head": HEAD}


def state(enabled=True):
    return {
        "data": {
            "viewer": {"login": "swu-wise[bot]"},
            "repository": {
                "nameWithOwner": ARGS["repo"],
                "pullRequest": {
                    "id": "PR_abc123",
                    "number": 9,
                    "state": "OPEN",
                    "headRefOid": HEAD,
                    "autoMergeRequest": {"enabledAt": "2026-10-26T00:00:00Z"} if enabled else None,
                    "labels": {
                        "nodes": [{"name": "hold/do-not-merge"}],
                        "pageInfo": {"hasNextPage": False},
                    },
                    "timelineItems": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                },
            },
        }
    }


class Policy:
    def app_identity(self, seat):
        return "swu-wise", {"pull_requests": "write", "issues": "read"}

    def authorize(self, seat, action, repo):
        return None if repo == ARGS["repo"] else "denied"


class Client:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def _call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Tests(unittest.TestCase):
    def svc(self, replies, scope=("pr_automerge_disable", ARGS["repo"])):
        self.client = Client(replies)
        self.client._check_repo = lambda repo: None

        class Minter:
            def token_for(self, *args):
                return "test-opaque-bearer", 999999999

        if not isinstance(scope, tuple) or len(scope) != 2 or not isinstance(scope[1], str):
            raise Refused("invalid scope")
        return TypedDisarm.for_action(self.client, Policy(), Minter(), "swu-wise", *scope)

    def test_direct_construction_requires_scope_and_ceiling(self):
        for scope in (None, ("pr_merge", ARGS["repo"]), ("pr_state",), ("pr_state", 3)):
            with self.assertRaises(Refused):
                self.svc([], scope)

        class ReadOnly(Policy):
            def app_identity(self, seat):
                return "swu-wise", {"pull_requests": "read", "issues": "read"}

        with self.assertRaises(Refused):
            TypedDisarm(
                Client([]),
                ReadOnly(),
                "swu-wise",
                "t",
                "swu-wise",
                ("pr_automerge_disable", ARGS["repo"]),
            )

    def test_arbitrary_bearer_cannot_construct_even_with_scope(self):
        with self.assertRaises(Refused):
            TypedDisarm(
                Client([]),
                Policy(),
                "swu-wise",
                "caller-token",
                "swu-wise",
                ("pr_automerge_disable", ARGS["repo"]),
            )

    def test_unknown_seat_lookup_maps_to_refused(self):
        class Missing(Policy):
            def app_identity(self, seat):
                raise KeyError(seat)

        client = Client([])
        client._check_repo = lambda repo: None
        with self.assertRaises(Refused):
            TypedDisarm.for_action(client, Missing(), None, "missing", "pr_state", ARGS["repo"])

    def test_scope_mismatch_refused(self):
        with self.assertRaises(Refused):
            self.svc([], ("pr_state", ARGS["repo"])).disable(ARGS)

    def test_pr_closed_between_reads_is_unknown(self):
        closed = state(False)
        closed["data"]["repository"]["pullRequest"]["state"] = "CLOSED"
        with self.assertRaises(Unknown):
            self.svc([state(), {"data": {}}, closed]).disable(ARGS)

    def test_confirmed_disable_preserves_hold(self):
        result = self.svc(
            [
                state(),
                {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"id": "PR_abc123"}}}},
                state(False),
            ]
        ).disable(ARGS)
        self.assertEqual(result["outcome"], "confirmed_disabled")
        self.assertEqual(result["labels"], ["hold/do-not-merge"])
        self.assertFalse(result["atomic_head_guard"])
        calls = self.client.calls
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c[0][:2] == ("POST", "/graphql") for c in calls))
        self.assertEqual(calls[1][0][2]["query"], DISABLE_MUTATION)

    def test_already_disabled_still_reads_back(self):
        self.assertFalse(self.svc([state(False), state(False)]).disable(ARGS)["write_attempted"])
        self.assertEqual(len(self.client.calls), 2)

    def test_write_timeout_reconciles_no_retry(self):
        self.assertEqual(
            self.svc([state(), TimeoutError("secret"), state(False)]).disable(ARGS)["outcome"],
            "confirmed_disabled",
        )
        self.assertEqual(len(self.client.calls), 3)

    def test_still_armed_unknown(self):
        with self.assertRaises(Unknown):
            self.svc([state(), {"data": {}}, state()]).disable(ARGS)

    def test_readback_failure_unknown(self):
        with self.assertRaisesRegex(Unknown, "disable outcome unknown"):
            self.svc([state(), {"data": {}}, TimeoutError("secret")]).disable(ARGS)

    def test_head_drift_preflight_no_write(self):
        reply = state()
        reply["data"]["repository"]["pullRequest"]["headRefOid"] = "b" * 40
        with self.assertRaises(Refused):
            self.svc([reply]).disable(ARGS)
        self.assertEqual(len(self.client.calls), 1)

    def test_head_drift_readback_unknown(self):
        reply = state(False)
        reply["data"]["repository"]["pullRequest"]["headRefOid"] = "b" * 40
        with self.assertRaises(Unknown):
            self.svc([state(), {"data": {}}, reply]).disable(ARGS)

    def test_wrong_principal_never_mutates(self):
        reply = state()
        reply["data"]["viewer"]["login"] = "swu-echo[bot]"
        with self.assertRaises(Unknown):
            self.svc([reply]).disable(ARGS)
        self.assertEqual(len(self.client.calls), 1)

    def test_pagination_not_clean_hold(self):
        reply = state()
        reply["data"]["repository"]["pullRequest"]["timelineItems"]["pageInfo"]["hasNextPage"] = (
            True
        )
        result = self.svc([reply], ("pr_hold_state", ARGS["repo"])).hold_state(ARGS)
        self.assertFalse(result["complete"])
        self.assertEqual(result["hold_decision"], "not_evaluated")

    def test_schema_refusals(self):
        for args in (
            {**ARGS, "url": "https://evil.invalid"},
            {**ARGS, "number": True},
            {**ARGS, "expected_head": "x"},
        ):
            with self.subTest(args=args), self.assertRaises(Refused):
                self.svc([]).state(args)

    def test_repo_denied(self):
        with self.assertRaises(Refused):
            self.svc([]).state({**ARGS, "repo": "jaywedgeworth22/Fleet-OPS"})

    def test_no_app_impersonation(self):
        with self.assertRaises(Refused):
            TypedDisarm(
                Client([]), Policy(), "swu-wise", "test", "swu-echo", ("pr_state", ARGS["repo"])
            )

    def test_untrusted_failure_redacted(self):
        with self.assertRaisesRegex(Unknown, "^upstream result unavailable$"):
            self.svc([ValueError("ghs_do_not_disclose")], ("pr_state", ARGS["repo"])).state(ARGS)

    def test_closed_no_write(self):
        reply = state()
        reply["data"]["repository"]["pullRequest"]["state"] = "CLOSED"
        with self.assertRaises(Refused):
            self.svc([reply]).disable(ARGS)

    def test_oversize_projection_unknown(self):
        reply = state()
        reply["data"]["repository"]["pullRequest"]["labels"]["nodes"] = [{"name": "x"}] * 51
        with self.assertRaises(Unknown):
            self.svc([reply], ("pr_state", ARGS["repo"])).state(ARGS)


if __name__ == "__main__":
    unittest.main()


class RealPolicyTests(unittest.TestCase):
    def make(self, permissions=None):
        import json

        from git_relay.github_client import GitHubClient
        from git_relay.policy import SeatPolicy

        policy = SeatPolicy(
            json.dumps(
                {
                    "swu-wise": {
                        "token": "test-bearer-at-least-16",
                        "actions": ["pr_state", "pr_hold_state", "pr_automerge_disable"],
                        "repos": [ARGS["repo"]],
                        "app_slug": "swu-wise",
                        "mint_permissions": permissions
                        or {"pull_requests": "write", "issues": "read"},
                    }
                }
            )
        )

        class Minter:
            def __init__(self):
                self.calls = []

            def token_for(self, *args):
                self.calls.append(args)
                return "test-installation", 9999999999

        client = Client([state(), {"data": {}}, state(False)])
        client._check_repo = GitHubClient._check_repo
        return client, policy, Minter()

    def test_real_policy_minimum_mint(self):
        client, policy, minter = self.make()
        svc = TypedDisarm.for_action(
            client, policy, minter, "swu-wise", "pr_automerge_disable", ARGS["repo"]
        )
        self.assertEqual(svc.disable(ARGS)["outcome"], "confirmed_disabled")
        self.assertEqual(
            minter.calls, [("swu-wise", ARGS["repo"], {"pull_requests": "write", "issues": "read"})]
        )

    def test_read_credential_cannot_disable(self):
        client, policy, minter = self.make()
        svc = TypedDisarm.for_action(client, policy, minter, "swu-wise", "pr_state", ARGS["repo"])
        with self.assertRaises(Refused):
            svc.disable(ARGS)
        self.assertEqual(client.calls, [])

    def test_ceiling_refusal_before_mint(self):
        client, policy, minter = self.make({"pull_requests": "read", "issues": "read"})
        with self.assertRaises(Refused):
            TypedDisarm.for_action(
                client, policy, minter, "swu-wise", "pr_automerge_disable", ARGS["repo"]
            )
        self.assertEqual(minter.calls, [])

    def test_live_handler_still_inert(self):
        from git_relay.server import _validate_args

        with self.assertRaisesRegex(ValueError, "not implemented"):
            _validate_args("pr_automerge_disable", ARGS)
