import copy
import unittest

from git_relay import echo_admin_read as m


class Tests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "swu-wise": {"actions": ["wf_review_threads"], "opaque": "preserve"},
            "swu-echo": {
                "app_slug": "swu-echo",
                "auth": "shared",
                "actions": ["wf_repo_get", "wf_pr_get"],
                "mint_permissions": {
                    "metadata": "read",
                    "pull_requests": "write",
                    "repository_hooks": "write",
                    "administration": "write",
                    "organization_administration": "write",
                },
                "repos": ["org/*"],
                "opaque": {"preserve": [1, 2]},
            },
        }

    def test_only_exact_actions_appended(self):
        before = copy.deepcopy(self.policy)
        out = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(self.policy, before)
        self.assertEqual(out["swu-wise"], before["swu-wise"])
        self.assertEqual(
            out["swu-echo"]["actions"],
            ["wf_repo_get", "wf_pr_get"] + m.ACTIONS,
        )
        for a in m.ACTIONS:
            out["swu-echo"]["actions"].remove(a)
        self.assertEqual(out, before)

    def test_drift_refuses(self):
        with self.assertRaises(ValueError):
            m.prepare(self.policy, "wrong")

    def test_unsafe_or_wrong_seat_refuses(self):
        for field, value in [
            ("app_slug", "swu-wise"),
            ("auth", "token"),
            ("actions", []),
            ("actions", ["wf_repo_get"] * 2),
            ("actions", ["wf_repo_get", m.ACTIONS[0]]),
            ("mint_permissions", {"contents": "write"}),
            ("mint_permissions", None),
            ("mint_permissions", []),
        ]:
            p = copy.deepcopy(self.policy)
            p["swu-echo"][field] = value
            with self.assertRaises(ValueError):
                m.prepare(p, m.digest(p))

    def test_each_missing_ceiling_refuses(self):
        for key in set(m.CEILINGS.values()):
            p = copy.deepcopy(self.policy)
            del p["swu-echo"]["mint_permissions"][key]
            with self.assertRaises(ValueError):
                m.prepare(p, m.digest(p))

    def test_none_ceiling_refuses(self):
        p = copy.deepcopy(self.policy)
        p["swu-echo"]["mint_permissions"]["administration"] = "none"
        with self.assertRaises(ValueError):
            m.prepare(p, m.digest(p))

    def test_missing_seat_refuses(self):
        del self.policy["swu-echo"]
        with self.assertRaises(ValueError):
            m.prepare(self.policy, m.digest(self.policy))

    def test_other_seat_unchanged_even_with_same_shape(self):
        self.policy["swu-mm"] = copy.deepcopy(self.policy["swu-echo"])
        self.policy["swu-mm"]["app_slug"] = "swu-mm"
        before = copy.deepcopy(self.policy)
        out = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(out["swu-mm"], before["swu-mm"])
        self.assertEqual(list(out), list(self.policy))


if __name__ == "__main__":
    unittest.main()
