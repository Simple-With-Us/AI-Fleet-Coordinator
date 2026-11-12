import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient
from git_relay.outcomes import OutcomeStore, key_for
from git_relay.policy import SeatPolicy
from git_relay.server import make_handler
from git_relay.tests.test_typed_disarm import ARGS, Client, state
from git_relay.typed_gateway import TypedGateway

TOKEN = "test-opaque-" + "bearer-123456"


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/outcomes.json"
        self.policy = SeatPolicy(
            json.dumps(
                {
                    "swu-wise": {
                        "token": TOKEN,
                        "actions": ["pr_state", "pr_hold_state", "pr_automerge_disable"],
                        "repos": [ARGS["repo"]],
                        "app_slug": "swu-wise",
                        "mint_permissions": {"pull_requests": "write", "issues": "read"},
                    }
                }
            ),
            rate_per_hour=1,
        )

        class Minter:
            def token_for(self, *args):
                return "test-installation", 999999999

        self.client = Client([state(), {"data": {}}, state(False)])
        self.client._check_repo = GitHubClient._check_repo
        self.audit = AuditLog(self.tmp.name + "/audit.jsonl")
        self.gateway = TypedGateway(
            self.policy, self.client, Minter(), self.audit, OutcomeStore(self.path)
        )

    def test_one_policy_charge(self):
        status, result = self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.policy._calls["swu-wise"]), 1)
        with open(self.tmp.name + "/audit.jsonl") as stream:
            rows = [json.loads(r) for r in stream]
        self.assertEqual([r["result"] for r in rows], ["intent", "confirmed"])
        self.assertNotIn("test-installation", json.dumps(rows))

    def test_preflight_failure_releases_key_without_poisoning(self):
        self.policy._rate_per_hour = 10
        self.client.replies = [TimeoutError(), state(), {"data": {}}, state(False)]
        status, body = self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)
        self.assertEqual(status, 503)
        self.assertTrue(body["retryable"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)[0], 200)

    def test_capacity_refusal_does_not_finish_unreserved_key(self):
        from git_relay.outcomes import OutcomeStoreError

        def full(key):
            raise OutcomeStoreError("full")

        self.gateway.outcomes.begin = full
        self.gateway.outcomes.finish = lambda *args: self.fail("unreserved key finished")
        status, body = self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)
        self.assertEqual(status, 503)
        self.assertTrue(body["retryable"])
        self.assertEqual(self.client.calls, [])

    def test_real_relay_dispatch_default_off_then_on(self):
        from git_relay.server import Relay

        class Minter:
            def token_for(self, *args):
                return "test-installation", 999999999

        for enabled in (False, True):
            relay = Relay(
                self.policy,
                self.client,
                self.audit,
                app_minter=Minter(),
                outcomes=OutcomeStore(self.path),
                typed_disarm_enabled=enabled,
            )
            status, body = relay.handle(TOKEN, "pr_state", ARGS)
            self.assertEqual(status, 200 if enabled else 400)
        self.assertEqual(len(self.client.calls), 1)

    def test_quota_denial_retryable_before_any_effect(self):
        self.assertEqual(self.gateway.handle(TOKEN, "pr_state", ARGS)[0], 200)
        count = len(self.client.calls)
        status, body = self.gateway.handle(TOKEN, "pr_state", ARGS)
        self.assertEqual(status, 429)
        self.assertTrue(body["retryable"])
        self.assertEqual(len(self.client.calls), count)

    def test_release_failure_returns_sanitized_unknown(self):
        self.client.replies = [TimeoutError()]
        self.gateway.outcomes.finish = lambda *args: (_ for _ in ()).throw(OSError("private path"))
        status, body = self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)
        self.assertEqual(status, 502)
        self.assertEqual(body["outcome"], "unknown")
        self.assertNotIn("private path", json.dumps(body))
        with open(self.tmp.name + "/audit.jsonl") as stream:
            rows = [json.loads(r) for r in stream]
        self.assertEqual(rows[-1]["result"], "unknown")

    def test_rate_limited_after_reservation_releases_it(self):
        from unittest.mock import patch

        from git_relay.typed_disarm import RateLimited

        with patch("git_relay.typed_disarm.TypedDisarm.disable", side_effect=RateLimited()):
            status, body = self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)
        self.assertEqual(status, 429)
        self.assertTrue(body["retryable"])
        row = OutcomeStore(self.path).begin(key_for("swu-wise", "pr_automerge_disable", ARGS))
        self.assertIsNone(row)

    def test_unknown_survives_restart(self):
        self.client.replies = [state(), TimeoutError(), TimeoutError()]
        self.assertEqual(self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)[0], 502)
        self.assertEqual(
            OutcomeStore(self.path).begin(key_for("swu-wise", "pr_automerge_disable", ARGS))[
                "state"
            ],
            "unknown",
        )

    def test_no_stale_success_replay(self):
        self.policy._rate_per_hour = 10
        self.assertEqual(self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)[0], 200)
        self.assertEqual(self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)[0], 409)
        self.assertEqual(len(self.client.calls), 3)

    def test_auth_before_reads(self):
        self.assertEqual(self.gateway.handle("wrong", "pr_state", ARGS)[0], 401)
        self.assertEqual(self.client.calls, [])

    def test_extra_proxy_arg_no_charge_or_write(self):
        self.assertEqual(self.gateway.handle(TOKEN, "pr_state", {**ARGS, "query": "evil"})[0], 403)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.policy._calls, {})

    def test_intent_audit_failure_prevents_write(self):
        self.audit.record = lambda *args: (_ for _ in ()).throw(OSError())
        self.assertEqual(self.gateway.handle(TOKEN, "pr_automerge_disable", ARGS)[0], 502)
        self.assertEqual(self.client.calls, [])

    def test_http_contract_offline_injection_only(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.gateway))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/v1/pr_automerge_disable" % server.server_port,
                data=json.dumps(ARGS).encode(),
                headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                body = json.load(response)
            self.assertEqual(body["outcome"], "confirmed_disabled")
            self.assertNotIn("test-installation", json.dumps(body))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class TypedStartupTests(unittest.TestCase):
    def test_typed_only_missing_app_rejected(self):
        from git_relay.policy import PolicyError

        for action in ("pr_state", "pr_hold_state", "pr_automerge_disable"):
            with self.subTest(action=action), self.assertRaises(PolicyError):
                SeatPolicy(
                    json.dumps(
                        {"swu-wise": {"token": TOKEN, "actions": [action], "repos": [ARGS["repo"]]}}
                    )
                )

    def test_typed_only_missing_ceiling_rejected(self):
        from git_relay.policy import PolicyError

        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps(
                    {
                        "swu-wise": {
                            "token": TOKEN,
                            "actions": ["pr_state"],
                            "repos": [ARGS["repo"]],
                            "app_slug": "swu-wise",
                        }
                    }
                )
            )
