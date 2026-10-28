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
