"""Merge-queue mint permission: every layer must agree.

The 2026-11-27 enqueue refusals came from the merge_queues permission
being absent at every layer. These tests pin the two layers that are
pure data: the minter's hard ceiling and the reviewed seat-policy file.
"""

import json
import unittest
from pathlib import Path

from git_relay.app_tokens import mint_permission_allowed

DEPLOY_POLICY = Path(__file__).resolve().parents[2] / "deploy" / "seat-policy.json"


class MergeQueuePermissionTests(unittest.TestCase):
    def test_mint_ceiling_allows_merge_queues_write(self):
        self.assertTrue(mint_permission_allowed("merge_queues", "write"))
        self.assertTrue(mint_permission_allowed("merge_queues", "read"))

    def test_fleet_ceiling_grants_merge_queues_write(self):
        policy = json.loads(DEPLOY_POLICY.read_text())
        fleet = policy["profiles"]["fleet"]
        self.assertEqual(fleet["mint_permissions"].get("merge_queues"), "write")


if __name__ == "__main__":
    unittest.main()
