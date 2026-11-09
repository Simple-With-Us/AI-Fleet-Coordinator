import copy
import unittest

from git_relay import echo_review_read as m


class Tests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "swu-wise": {"actions": ["wf_review_threads"], "opaque": "preserve"},
            "swu-echo": {
                "app_slug": "swu-echo",
                "auth": "shared",
                "actions": ["wf_review_threads"],
                "mint_permissions": {"pull_requests": "write"},
                "repos": ["org/*"],
                "opaque": {"preserve": [1, 2]},
            },
        }

    def test_only_exact_action_appended(self):
        before = copy.deepcopy(self.policy)
        out = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(self.policy, before)
        self.assertEqual(out["swu-wise"], before["swu-wise"])
        self.assertEqual(out["swu-echo"]["actions"], ["wf_review_threads", m.ACTION])
        out["swu-echo"]["actions"].pop()
        self.assertEqual(out, before)

    def test_drift_refuses(self):
        with self.assertRaises(ValueError):
            m.prepare(self.policy, "wrong")

    def test_unsafe_or_wrong_seat_refuses(self):
        for field, value in [
            ("app_slug", "swu-wise"),
            ("auth", "token"),
            ("actions", []),
            ("actions", ["wf_review_threads"] * 2),
            ("actions", ["wf_review_threads", m.ACTION]),
            ("mint_permissions", {"contents": "write"}),
        ]:
            p = copy.deepcopy(self.policy)
            p["swu-echo"][field] = value
            with self.assertRaises(ValueError):
                m.prepare(p, m.digest(p))

    def test_missing_seat_refuses(self):
        del self.policy["swu-echo"]
        with self.assertRaises(ValueError):
            m.prepare(self.policy, m.digest(self.policy))


if __name__ == "__main__":
    unittest.main()
