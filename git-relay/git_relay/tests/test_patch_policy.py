"""Durable exact-delta bootstrap coverage, independent of whole-file fixtures."""

import copy
import unittest

from git_relay.patch_policy import ACTION, digest, prepare
from git_relay.tests.test_seat_policy_file import SEATS


class PatchPolicyTests(unittest.TestCase):
    def test_actual_canonical_policy_preserves_every_field_and_order(self):
        original = copy.deepcopy(SEATS)
        result = prepare(SEATS, digest(SEATS))
        self.assertEqual(SEATS, original)
        self.assertEqual(set(result), set(SEATS))
        for name, before in SEATS.items():
            expected = copy.deepcopy(before)
            # Idempotent: the canonical file now grants wf_file_patch already.
            if ACTION not in expected["actions"]:
                expected["actions"].append(ACTION)
            self.assertEqual(result[name], expected)
        self.assertEqual(prepare(result, digest(result)), result)

    def test_appends_exactly_one_action_to_a_seat_without_it(self):
        seat = {"actions": ["wf_file_put", "comment_pr"], "auth": "shared"}
        policy = {"seat": seat}
        before = copy.deepcopy(policy)
        result = prepare(policy, digest(policy))
        self.assertEqual(policy, before)
        self.assertEqual(result["seat"]["actions"], ["wf_file_put", "comment_pr", ACTION])
        self.assertEqual(result["seat"]["auth"], "shared")
        self.assertEqual(prepare(result, digest(result)), result)

    def test_refuses_stale_digest_and_invalid_shapes_without_mutation(self):
        for candidate in [
            None,
            {},
            [],
            {"seat": []},
            {"seat": {"actions": []}},
            {"seat": {"actions": ["wf_file_put", "wf_file_put"]}},
            {"seat": {"actions": ["wf_file_put", 1]}},
        ]:
            before = copy.deepcopy(candidate)
            with self.assertRaises((ValueError, TypeError)):
                prepare(candidate, digest(candidate))
            self.assertEqual(candidate, before)
        with self.assertRaises(ValueError):
            prepare(SEATS, "stale")
