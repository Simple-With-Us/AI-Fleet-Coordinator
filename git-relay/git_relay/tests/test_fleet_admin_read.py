import copy
import unittest

from git_relay import fleet_admin_read as m


def seat(name, actions=None, ceilings=None, **extra):
    row = {
        "app_slug": name,
        "auth": "shared",
        "actions": actions if actions is not None else ["wf_repo_get"],
        "mint_permissions": ceilings
        if ceilings is not None
        else {
            "metadata": "read",
            "pull_requests": "write",
            "repository_hooks": "write",
            "administration": "write",
            "organization_administration": "write",
        },
    }
    row.update(extra)
    return row


class Tests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "swu-wise": seat("swu-wise", actions=["wf_review_threads"], opaque="preserve"),
            "swu-echo": seat("swu-echo", actions=["wf_repo_get", "wf_pr_get"]),
            "swu-mm": seat("swu-mm"),
        }

    def test_every_qualified_seat_extended_exactly(self):
        before = copy.deepcopy(self.policy)
        out, report = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(self.policy, before)  # input untouched
        self.assertEqual(list(out), list(self.policy))  # seat order kept
        for name, row in before.items():
            self.assertEqual(out[name]["actions"], row["actions"] + m.ACTIONS)
            for key in row:
                if key != "actions":
                    self.assertEqual(out[name][key], row[key])
        self.assertEqual(
            report,
            {name: {"added": m.ACTIONS, "already_present": [], "excluded": []}
             for name in before},
        )

    def test_missing_ceiling_excludes_only_that_verb(self):
        del self.policy["swu-mm"]["mint_permissions"]["administration"]
        self.policy["swu-mm"]["mint_permissions"]["organization_administration"] = "none"
        out, report = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(
            out["swu-mm"]["actions"],
            ["wf_repo_get", "wf_repo_list", "wf_pr_search", "wf_repo_hooks"],
        )
        self.assertEqual(
            report["swu-mm"]["excluded"],
            [
                "wf_deploy_keys (no administration read ceiling)",
                "wf_org_installations (no organization_administration read ceiling)",
            ],
        )
        self.assertEqual(out["swu-echo"]["actions"], ["wf_repo_get", "wf_pr_get"] + m.ACTIONS)

    def test_fully_incompatible_seat_skipped_unchanged(self):
        self.policy["swu-zc"] = seat("swu-zc", ceilings={"contents": "write"})
        before = copy.deepcopy(self.policy["swu-zc"])
        out, report = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(out["swu-zc"], before)
        self.assertEqual(len(report["swu-zc"]["excluded"]), 5)
        self.assertEqual(report["swu-zc"]["added"], [])

    def test_seat_shape_problems_skip_with_reason(self):
        self.policy["swu-bad-slug"] = seat("swu-other")
        self.policy["swu-bad-auth"] = seat("swu-bad-auth", auth="token")
        self.policy["swu-dup"] = seat("swu-dup", actions=["wf_repo_get", "wf_repo_get"])
        self.policy["swu-empty"] = seat("swu-empty", actions=[])
        self.policy["swu-noperm"] = seat("swu-noperm", mint_permissions=None)
        before = copy.deepcopy(self.policy)
        out, report = m.prepare(self.policy, m.digest(self.policy))
        for name in ("swu-bad-slug", "swu-bad-auth", "swu-dup", "swu-empty", "swu-noperm"):
            self.assertIn("skipped", report[name], name)
            self.assertEqual(out[name], before[name], name)
        self.assertEqual(out["swu-echo"]["actions"], ["wf_repo_get", "wf_pr_get"] + m.ACTIONS)

    def test_non_dict_seat_row_skipped_without_crash(self):
        self.policy["swu-legacy"] = "legacy string row"
        out, report = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(out["swu-legacy"], "legacy string row")
        self.assertIn("skipped", report["swu-legacy"])
        self.assertEqual(
            out["swu-echo"]["actions"], ["wf_repo_get", "wf_pr_get"] + m.ACTIONS
        )

    def test_partially_granted_seat_gets_only_missing(self):
        # As if the ECHO-only transaction already landed: no duplicate verbs.
        self.policy["swu-echo"]["actions"] = ["wf_repo_get", "wf_pr_get"] + m.ACTIONS
        out, report = m.prepare(self.policy, m.digest(self.policy))
        self.assertEqual(
            out["swu-echo"]["actions"], ["wf_repo_get", "wf_pr_get"] + m.ACTIONS
        )
        self.assertEqual(report["swu-echo"]["added"], [])
        self.assertEqual(report["swu-echo"]["already_present"], m.ACTIONS)
        self.assertEqual(report["swu-wise"]["added"], m.ACTIONS)

    def test_noop_candidate_refuses(self):
        for name in self.policy:
            self.policy[name]["actions"] = self.policy[name]["actions"] + m.ACTIONS
        with self.assertRaisesRegex(ValueError, "no-op"):
            m.prepare(self.policy, m.digest(self.policy))

    def test_drift_refuses(self):
        with self.assertRaises(ValueError):
            m.prepare(self.policy, "wrong")
        with self.assertRaises(ValueError):
            m.prepare({}, "cf83e1357eefb8bdf1542850d66d8007d620e4050b5714dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e")


if __name__ == "__main__":
    unittest.main()
