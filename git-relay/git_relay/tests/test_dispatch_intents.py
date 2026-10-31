import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from git_relay.audit import AuditLog
from git_relay.github_client import DispatchPreReadUnavailable, GitHubClient, RelayGitHubError
from git_relay.policy import SeatPolicy
from git_relay.server import Relay, _validate_args

from .test_git_relay import APP_SEATS_JSON, SEAT_TOKEN, FakeMinter, FakeOpener, StubClient


class DispatchIntents(unittest.TestCase):
    def args(self, **updates):
        return {
            "repo": "example/x",
            "workflow": "ci.yml",
            "ref": "main",
            "inputs": {"qualify_only": True},
            "intent_id": "qualification-source-one",
            "expected_head_sha": "a" * 40,
            **updates,
        }

    def test_paired_intent_and_sha_shape(self):
        for updates in [
            {"expected_head_sha": None},
            {"intent_id": None},
            {"intent_id": "short"},
            {"expected_head_sha": "HEAD"},
        ]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                _validate_args("workflow_dispatch", self.args(**updates))
        self.assertEqual(
            _validate_args("workflow_dispatch", self.args())["expected_head_sha"], "a" * 40
        )

    def test_same_intent_retries_cached_new_intent_sends_changed_payload_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(str(Path(tmp) / "audit.jsonl")),
                app_minter=FakeMinter(),
            )
            relay._dispatch = Mock(return_value={"dispatched": True, "accepted": True})
            status, first = relay.handle(SEAT_TOKEN, "workflow_dispatch", self.args())
            self.assertEqual(status, 200)
            self.assertFalse(first["replayed"])
            self.assertTrue(first["upstream_request_sent"])
            status, replay = relay.handle(SEAT_TOKEN, "workflow_dispatch", self.args())
            self.assertEqual(status, 200)
            self.assertTrue(replay["replayed"])
            self.assertFalse(replay["upstream_request_sent"])
            self.assertFalse(replay["execution_verified"])
            self.assertEqual(relay._dispatch.call_count, 1)
            status, _ = relay.handle(
                SEAT_TOKEN, "workflow_dispatch", self.args(expected_head_sha="b" * 40)
            )
            self.assertEqual(status, 409)
            audit = [
                json.loads(line) for line in (Path(tmp) / "audit.jsonl").read_text().splitlines()
            ]
            self.assertEqual(audit[-1]["result"], "digest_mismatch")
            self.assertNotIn("idempotent_replay", audit[-1].get("detail", {}))
            self.assertEqual(relay._dispatch.call_count, 1)
            status, _ = relay.handle(
                SEAT_TOKEN, "workflow_dispatch", self.args(intent_id="qualification-source-two")
            )
            self.assertEqual(status, 200)
            self.assertEqual(relay._dispatch.call_count, 2)

    def test_source_mismatch_makes_no_post(self):
        opener = FakeOpener({"object": {"sha": "b" * 40}})
        client = GitHubClient("fixture", opener=opener)
        with self.assertRaises(ValueError):
            client.workflow_dispatch(
                "example/x", "ci.yml", "main", {}, "fixture", expected_head_sha="a" * 40
            )
        self.assertEqual([r.get_method() for r in opener.requests], ["GET"])

    def test_source_check_and_projected_receipt_no_atomic_claim(self):
        requests = []

        def opener(req, timeout):
            from .test_git_relay import FakeResponse

            requests.append(req)
            return FakeResponse(
                {"object": {"sha": "a" * 40}}
                if req.get_method() == "GET"
                else {
                    "workflow_run_id": 123,
                    "html_url": "https://github.com/example/x/actions/runs/123",
                    "unexpected": "private",
                }
            )

        result = GitHubClient("fixture", opener=opener).workflow_dispatch(
            "example/x", "ci.yml", "main", {}, "fixture", expected_head_sha="a" * 40
        )
        self.assertEqual([r.get_method() for r in requests], ["GET", "POST"])
        self.assertFalse(result["source_fence_atomic"])
        self.assertEqual(result["workflow_run_id"], 123)
        self.assertNotIn("unexpected", result)
        self.assertEqual(json.loads(requests[1].data), {"ref": "main", "inputs": {}})

    def test_intent_dispatch_mints_only_existing_action_and_source_read_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(str(Path(tmp) / "audit.jsonl")),
                app_minter=FakeMinter(),
            )
            relay.client.workflow_dispatch = Mock(return_value={"accepted": True})
            status, _ = relay.handle(SEAT_TOKEN, "workflow_dispatch", self.args())
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls[-1][2], {"actions": "write", "contents": "read"}
            )
            self.assertEqual(
                relay.client.workflow_dispatch.call_args.kwargs["expected_head_sha"], "a" * 40
            )

    def test_transient_source_read_is_safe_retry_not_unknown(self):
        for status in (0, 408, 500, 503):
            with self.subTest(status=status):
                client = GitHubClient("fixture")
                client._call = Mock(side_effect=RelayGitHubError(status, "fixture"))
                with self.assertRaises(DispatchPreReadUnavailable):
                    client.workflow_dispatch(
                        "example/x", "ci.yml", "main", {}, "fixture", expected_head_sha="a" * 40
                    )
                self.assertEqual(client._call.call_count, 1)
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(str(Path(tmp) / "audit.jsonl")),
                app_minter=FakeMinter(),
            )
            relay._dispatch = Mock(side_effect=[DispatchPreReadUnavailable(), {"accepted": True}])
            status, body = relay.handle(SEAT_TOKEN, "workflow_dispatch", self.args())
            self.assertEqual(status, 503)
            self.assertTrue(body["retryable"])
            status, _ = relay.handle(SEAT_TOKEN, "workflow_dispatch", self.args())
            self.assertEqual(status, 200)
