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
