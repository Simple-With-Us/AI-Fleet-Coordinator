"""Exact additive seat insertion, independent of whole-file fixtures."""

import copy
import unittest

from git_relay.seat_add_policy import digest, prepare
from git_relay.tests.test_seat_policy_file import SEATS

ZC = "swu-zc"


def synthetic_row(slug="swu-new", identity="NEW"):
    return {
        "actions": ["git_read", "open_pr"],
        "app_slug": slug,
        "auth": "shared",
        "identity": identity,
        "mint_permissions": {"contents": "write"},
        "repos": ["Simple-With-Us/*"],
    }


def synthetic_policy():
    return {"swu-one": synthetic_row("swu-one", "ONE")}


class SeatAddPolicyTests(unittest.TestCase):
    def test_actual_policy_reproduces_the_reviewed_zc_row_byte_for_byte(self):
        base = {name: row for name, row in SEATS.items() if name != ZC}
        self.assertEqual(len(base), len(SEATS) - 1, "the fixture already names the seat")
        original = copy.deepcopy(base)
        result = prepare(base, ZC, copy.deepcopy(SEATS[ZC]), digest(base))
        self.assertEqual(base, original, "the input is never mutated")
        self.assertEqual(result, SEATS)
        # Digest and the deployed line both canonicalize with sorted keys, so the
        # only order contract is that original seats keep their relative order.
        self.assertEqual(list(result), list(base) + [ZC], "the new seat appends; nothing moves")

    def test_synthetic_insertion_is_exactly_additive_and_idempotent_in_digest(self):
        policy = synthetic_policy()
        row = synthetic_row()
        result = prepare(policy, "swu-new", row, digest(policy))
        self.assertEqual(set(result), {"swu-one", "swu-new"})
        self.assertEqual(result["swu-one"], policy["swu-one"])
        self.assertEqual(result["swu-new"], row)
        with self.assertRaises(ValueError):
            prepare(result, "swu-new", row, digest(result))

    def test_refuses_stale_digest_and_existing_seat_without_mutation(self):
        policy = synthetic_policy()
        before = copy.deepcopy(policy)
        with self.assertRaises(ValueError):
            prepare(policy, "swu-new", synthetic_row(), "stale")
        with self.assertRaises(ValueError):
            prepare(policy, "swu-one", synthetic_row("swu-one", "ONE"), digest(policy))
        self.assertEqual(policy, before)

    def test_refuses_malformed_rows(self):
        policy = synthetic_policy()
        good = synthetic_row()
        bad_rows = [
            None,
            [],
            {**good, "app_slug": "swu-other"},  # row must name its key
            {**good, "auth": "static"},  # shared-auth only
            {**good, "identity": ""},
            {**good, "actions": []},
            {**good, "actions": ["git_read", "git_read"]},
            {**good, "actions": ["git_read", 1]},
            {**good, "repos": []},
            {**good, "mint_permissions": {}},
            {**good, "mint_permissions": {"contents": 1}},
            {**good, "oauth_assert": False},  # renders only as literal true
            {**good, "token": "x"},  # unknown key
            {k: v for k, v in good.items() if k != "repos"},  # missing key
        ]
        for row in bad_rows:
            before = copy.deepcopy(policy)
            with self.assertRaises((ValueError, TypeError), msg=repr(row)[:80]):
                prepare(policy, "swu-new", row, digest(policy))
            self.assertEqual(policy, before)

    def test_refuses_malformed_policies(self):
        # Existing seats are pinned by the digest fence and the invariance proof;
        # the deployment transaction validates them with the relay's own SeatPolicy,
        # so this module refuses only a non-mapping or empty policy.
        for candidate in [None, {}, [], "x"]:
            with self.assertRaises((ValueError, TypeError)):
                prepare(candidate, "swu-new", synthetic_row(), digest(candidate))


if __name__ == "__main__":
    unittest.main()
