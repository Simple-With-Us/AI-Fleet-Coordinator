import tempfile
import unittest
from pathlib import Path

from git_relay.outcomes import OutcomeStore
from git_relay.server import _REAPPLY_STATE_ACTIONS, _workflow_audit_detail


class StateReapply(unittest.TestCase):
    def test_success_reapplies_but_unknown_pending_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "outcomes.json")
            store = OutcomeStore(path)
            key = "a" * 64
            self.assertIsNone(store.begin(key))
            store.finish(key, "ok", {"completed": True})
            self.assertEqual(store.begin(key)["state"], "ok")
            self.assertIsNone(store.begin(key, reapply_ok=True))
            self.assertEqual(store.begin(key, reapply_ok=True)["state"], "pending")
            loaded = OutcomeStore(path)
            self.assertEqual(loaded.begin(key, reapply_ok=True)["state"], "unknown")
            loaded.finish(key, "unknown")
            self.assertEqual(loaded.begin(key, reapply_ok=True)["state"], "unknown")

    def test_alternating_known_success_and_scoped_actions(self):
        store = OutcomeStore()
        for key in ("a" * 64, "b" * 64, "a" * 64):
            self.assertIsNone(store.begin(key, reapply_ok=True))
            store.finish(key, "ok", {"completed": True})
        for action in ("wf_labels_set", "wf_workflow_enable", "wf_pr_draft"):
            self.assertIn(action, _REAPPLY_STATE_ACTIONS)
        for action in ("wf_release_create", "wf_file_put", "wf_merge_guarded", "open_pr"):
            self.assertNotIn(action, _REAPPLY_STATE_ACTIONS)

    def test_audit_ids_not_bodies(self):
        detail = _workflow_audit_detail(
            {
                "comment_id": 2,
                "release_id": 3,
                "workflow_id": 4,
                "artifact_id": 5,
                "tag": "v1",
                "sha": "a" * 40,
                "body": "private",
            },
            {},
        )
        self.assertEqual(detail["comment_id"], 2)
        self.assertEqual(detail["artifact_id"], 5)
        self.assertNotIn("body", detail)

    def test_relay_known_state_reapply_but_unknown_holds(self):
        import json
        from unittest.mock import patch

        from git_relay.audit import AuditLog
        from git_relay.github_client import RelayGitHubError
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay
        from git_relay.workflow_verbs import SPECS

        seat_token = "s" * 32
        config = json.dumps(
            {
                "swu-wise": {
                    "token": seat_token,
                    "app_slug": "swu-wise",
                    "mint_permissions": {"actions": "write"},
                    "actions": ["wf_run_cancel"],
                    "repos": ["example/repo"],
                }
            }
        )

        class Minter:
            def token_for(self, *args):
                return "synthetic-own-token", 0

        with tempfile.TemporaryDirectory() as directory:
            relay = Relay(
                SeatPolicy(config),
                object(),
                AuditLog(str(Path(directory) / "audit.jsonl")),
                app_minter=Minter(),
                workflow_verbs_enabled=True,
            )
            calls = []
            fail = [False]

            def dispatch(seat, action, args, repo):
                calls.append(args["state"])
                if fail[0]:
                    raise RelayGitHubError(502, "unknown")
                return {"state": args["state"]}

            relay._dispatch = dispatch
            # This shared fix may land before the issue-lifecycle registration.
            with (
                patch("git_relay.server._REAPPLY_STATE_ACTIONS", {"wf_run_cancel"}),
                patch.dict(SPECS, {"wf_run_cancel": ({"actions": "write"}, True)}),
                patch.object(
                    __import__(
                        "git_relay.workflow_verbs", fromlist=["WorkflowVerbs"]
                    ).WorkflowVerbs,
                    "check_shape",
                    return_value=None,
                ),
            ):
                for state in ("open", "closed", "open"):
                    self.assertEqual(
                        relay.handle(
                            seat_token, "wf_run_cancel", {"repo": "example/repo", "state": state}
                        )[0],
                        200,
                    )
                self.assertEqual(calls, ["open", "closed", "open"])
                fail[0] = True
                args = {"repo": "example/repo", "state": "closed"}
                self.assertEqual(relay.handle(seat_token, "wf_run_cancel", args)[0], 502)
                count = len(calls)
                self.assertEqual(relay.handle(seat_token, "wf_run_cancel", args)[0], 409)
                self.assertEqual(len(calls), count)
