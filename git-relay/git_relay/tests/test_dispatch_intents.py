import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from git_relay.audit import AuditLog
from git_relay.github_client import (
    DispatchPreReadUnavailable,
    GitHubClient,
    RelayGitHubError,
    intent_branch_ref,
)
from git_relay.policy import SeatPolicy
from git_relay.server import Relay, _validate_args

from .test_git_relay import (
    APP_SEATS_JSON,
    SEAT_TOKEN,
    FakeMinter,
    FakeOpener,
    FakeResponse,
    StubClient,
)


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
            {"intent_id": None},
            {"intent_id": "short"},
            {"expected_head_sha": "HEAD"},
        ]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                _validate_args("workflow_dispatch", self.args(**updates))
        self.assertEqual(
            _validate_args("workflow_dispatch", self.args())["expected_head_sha"], "a" * 40
        )
        intent_only = _validate_args("workflow_dispatch", self.args(expected_head_sha=None))
        self.assertEqual(intent_only["intent_id"], "qualification-source-one")
        self.assertNotIn("expected_head_sha", intent_only)

    def test_intent_branch_ref_accepts_a_branch_and_refuses_a_sha_or_parent(self):
        self.assertTrue(intent_branch_ref("main"))
        self.assertTrue(intent_branch_ref("release/2026.11"))
        self.assertTrue(intent_branch_ref("v1.2.3"))
        self.assertFalse(intent_branch_ref("a" * 40))
        self.assertFalse(intent_branch_ref("release/../main"))
        self.assertFalse(intent_branch_ref(""))
        self.assertFalse(intent_branch_ref(None))
        for ref in ("a" * 40, "release/../main"):
            with self.subTest(ref=ref), self.assertRaises(ValueError) as caught:
                _validate_args("workflow_dispatch", self.args(expected_head_sha=None, ref=ref))
            self.assertEqual(
                str(caught.exception), "intent dispatch requires a canonical branch name"
            )
        tag = _validate_args("workflow_dispatch", self.args(expected_head_sha=None, ref="v1.2.3"))
        self.assertEqual(tag["ref"], "v1.2.3")
        legacy = _validate_args(
            "workflow_dispatch",
            {"repo": "example/x", "workflow": "ci.yml", "ref": "a" * 40},
        )
        self.assertEqual(legacy["ref"], "a" * 40)

    def test_intent_ref_shape_is_refused_before_mint(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(str(Path(tmp) / "audit.jsonl")),
                app_minter=FakeMinter(),
            )
            relay.client.workflow_dispatch = Mock(return_value={"accepted": True})
            status, body = relay.handle(
                SEAT_TOKEN,
                "workflow_dispatch",
                self.args(expected_head_sha=None, ref="a" * 40),
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "intent dispatch requires a canonical branch name")
            self.assertEqual(relay.app_minter.calls, [])
            self.assertEqual(relay.client.workflow_dispatch.call_count, 0)

    def test_intent_only_skips_expected_head_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(str(Path(tmp) / "audit.jsonl")),
                app_minter=FakeMinter(),
            )
            relay._dispatch = Mock(return_value={"dispatched": True, "accepted": True})
            args = self.args(expected_head_sha=None)
            status, first = relay.handle(SEAT_TOKEN, "workflow_dispatch", args)
            self.assertEqual(status, 200)
            self.assertFalse(first["replayed"])
            status, replay = relay.handle(SEAT_TOKEN, "workflow_dispatch", args)
            self.assertEqual(status, 200)
            self.assertTrue(replay["replayed"])
            self.assertEqual(relay._dispatch.call_count, 1)

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

    def test_intent_only_mints_actions_without_contents(self):
        policy = json.dumps(
            {
                "swu-echo": {
                    "token": SEAT_TOKEN,
                    "actions": ["workflow_dispatch"],
                    "repos": ["example/*"],
                    "app_slug": "swu-echo",
                    "mint_permissions": {"actions": "write"},
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(policy),
                StubClient(),
                AuditLog(str(Path(tmp) / "audit.jsonl")),
                app_minter=FakeMinter(),
            )
            relay.client.workflow_dispatch = Mock(return_value={"accepted": True})
            status, _ = relay.handle(
                SEAT_TOKEN, "workflow_dispatch", self.args(expected_head_sha=None)
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "example/x", {"actions": "write"})]
            )
            self.assertIsNone(
                relay.client.workflow_dispatch.call_args.kwargs.get("expected_head_sha")
            )
            status, body = relay.handle(
                SEAT_TOKEN,
                "workflow_dispatch",
                self.args(intent_id="qualification-source-fenced"),
            )
            self.assertEqual(status, 403)
            self.assertIn("contents", body["error"])
            self.assertEqual(len(relay.app_minter.calls), 1)

    def test_intent_only_dispatch_posts_without_a_branch_read(self):
        requests = []

        def opener(req, timeout):
            requests.append(req)
            if req.get_method() == "GET":
                raise AssertionError("intent-only dispatch must not read the branch")
            return FakeResponse(
                {
                    "workflow_run_id": 123,
                    "html_url": "https://github.com/example/x/actions/runs/123",
                }
            )

        result = GitHubClient("fixture", opener=opener).workflow_dispatch(
            "example/x", "ci.yml", "main", {}, "fixture"
        )
        self.assertEqual([request.get_method() for request in requests], ["POST"])
        self.assertNotIn("expected_head_sha", result)
        self.assertNotIn("source_checked_before_send", result)
        self.assertNotIn("source_fence_atomic", result)

    def test_paired_commit_sha_ref_is_refused_before_the_branch_read(self):
        opener = FakeOpener({})
        with self.assertRaises(ValueError) as caught:
            GitHubClient("fixture", opener=opener).workflow_dispatch(
                "example/x", "ci.yml", "a" * 40, {}, "fixture", expected_head_sha="b" * 40
            )
        self.assertEqual(str(caught.exception), "intent dispatch requires a canonical branch name")
        self.assertEqual(opener.requests, [])

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


class CommentIntentTests(unittest.TestCase):
    """comment_pr idempotency: identical text resends replay; an explicit
    intent_id makes a deliberate re-trigger a new operation; the metadata
    never reaches dispatch or the upstream comment."""

    def args(self, **updates):
        return {"repo": "example/x", "number": 5, "body": "@kody start-review", **updates}

    def relay(self, tmp, client=None):
        return Relay(
            SeatPolicy(APP_SEATS_JSON),
            client or StubClient(),
            AuditLog(str(Path(tmp) / "audit.jsonl")),
            app_minter=FakeMinter(),
        )

    def test_identical_resend_without_intent_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self.relay(tmp)
            relay._dispatch = Mock(return_value={"id": 3, "url": "u"})
            status, first = relay.handle(SEAT_TOKEN, "comment_pr", self.args())
            self.assertEqual(status, 200)
            status, replay = relay.handle(SEAT_TOKEN, "comment_pr", self.args())
            self.assertEqual(status, 200)
            self.assertEqual(replay, first)
            self.assertEqual(relay._dispatch.call_count, 1)

    def test_distinct_intent_ids_are_distinct_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self.relay(tmp)
            relay._dispatch = Mock(return_value={"id": 3, "url": "u"})
            relay.handle(SEAT_TOKEN, "comment_pr", self.args(intent_id="trigger-0700z"))
            relay.handle(SEAT_TOKEN, "comment_pr", self.args(intent_id="trigger-1300z"))
            self.assertEqual(relay._dispatch.call_count, 2)

    def test_same_intent_id_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self.relay(tmp)
            relay._dispatch = Mock(return_value={"id": 3, "url": "u"})
            relay.handle(SEAT_TOKEN, "comment_pr", self.args(intent_id="trigger-0700z"))
            status, replay = relay.handle(
                SEAT_TOKEN, "comment_pr", self.args(intent_id="trigger-0700z")
            )
            self.assertEqual(status, 200)
            self.assertEqual(relay._dispatch.call_count, 1)

    def test_intent_id_validation(self):
        for bad in (123, "", "   ", "x" * 40000):
            with self.subTest(bad=repr(bad)[:20]), self.assertRaises(ValueError):
                _validate_args("comment_pr", self.args(intent_id=bad))
        with tempfile.TemporaryDirectory() as tmp:
            relay = self.relay(tmp)
            status, body = relay.handle(SEAT_TOKEN, "comment_pr", self.args(intent_id=123))
            self.assertEqual(status, 400)

    def test_intent_id_never_reaches_dispatch_or_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = StubClient()
            relay = self.relay(tmp, client=client)
            status, _ = relay.handle(SEAT_TOKEN, "comment_pr", self.args(intent_id="trigger-0700z"))
            self.assertEqual(status, 200)
            # The upstream comment call carries exactly repo, number, body,
            # bearer:  no intent metadata.
            self.assertEqual(len(client.calls), 1)
            name, repo, number, body, bearer = client.calls[0]
            self.assertEqual(
                (name, repo, number, body), ("comment_pr", "example/x", 5, "@kody start-review")
            )
