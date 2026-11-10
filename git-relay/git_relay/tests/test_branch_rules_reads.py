"""Ruleset reads (wf_branch_rules, wf_rulesets) and wf_repo_get merge settings (AFC-664)."""

import json
import unittest
from pathlib import Path

try:
    from .test_workflow_verbs import CEILING, REPO, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import CEILING, REPO, Client, Minter, Policy

from git_relay.policy import SeatPolicy
from git_relay.workflow_verbs import SPECS, Refused, RelayGitHubError, WorkflowVerbs

PR_RULE = {
    "type": "pull_request",
    "parameters": {
        "allowed_merge_methods": ["squash"],
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_review_thread_resolution": True,
    },
    "ruleset_source_type": "Repository",
    "ruleset_source": REPO,
    "ruleset_id": 42,
}
DELETION_RULE = {
    "type": "deletion",
    "ruleset_source_type": "Organization",
    "ruleset_source": "Simple-With-Us",
    "ruleset_id": 7,
}
RULESET = {
    "id": 42,
    "name": "main protection",
    "target": "branch",
    "source_type": "Repository",
    "source": REPO,
    "enforcement": "active",
    "node_id": "RRS_x",
    "_links": {"self": {"href": "https://api.github.com/repos/x/rulesets/42"}},
    "created_at": "2026-11-01T00:00:00Z",
    "updated_at": "2026-11-02T00:00:00Z",
}
REPO_BODY = {
    "id": 9,
    "name": "AI-Fleet-Coordinator",
    "full_name": REPO,
    "default_branch": "main",
    "private": False,
}
SETTINGS = {
    "allow_merge_commit": False,
    "allow_squash_merge": True,
    "allow_rebase_merge": False,
    "allow_auto_merge": True,
    "delete_branch_on_merge": True,
}


class ScopedMinter(Minter):
    """Minter whose token names the permission set it was minted for."""

    def token_for(self, slug, repo, perms):
        super().token_for(slug, repo, perms)
        return ("settings-bearer" if "administration" in perms else "base-bearer"), 0


class RuleReadTests(unittest.TestCase):
    def run_action(self, action, args, *replies, ceiling=CEILING, minter=None):
        client = Client(*replies)
        minter = minter or Minter()
        handle = WorkflowVerbs.for_action(client, Policy(ceiling), minter, "swu-wise", action, REPO)
        return handle.run(action, {"repo": REPO, **args}), client, minter

    def test_verbs_are_metadata_reads(self):
        for action in ("wf_branch_rules", "wf_rulesets"):
            self.assertEqual(SPECS[action], ({"metadata": "read"}, False), action)
            self.assertIn(action, SeatPolicy.WORKFLOW_ACTIONS, action)

    def test_branch_rules(self):
        out, client, minter = self.run_action(
            "wf_branch_rules", {"branch": "main"}, [PR_RULE, DELETION_RULE]
        )
        self.assertEqual(
            client.calls[0][1], "/repos/" + REPO + "/rules/branches/main?per_page=30&page=1"
        )
        self.assertEqual(minter.calls[0][2], {"metadata": "read"})
        self.assertEqual(out["branch"], "main")
        self.assertEqual(out["rules"][0]["parameters"]["allowed_merge_methods"], ["squash"])
        self.assertIsNone(out["rules"][1]["parameters"])
        self.assertEqual(out["rules"][1]["ruleset_source_type"], "Organization")
        self.assertFalse(out["has_more"])

    def test_branch_rules_quotes_slash_branch_and_pages(self):
        out, client, _ = self.run_action(
            "wf_branch_rules", {"branch": "claude/x", "per_page": 1, "page": 3}, [PR_RULE]
        )
        self.assertEqual(
            client.calls[0][1],
            "/repos/" + REPO + "/rules/branches/claude%2Fx?per_page=1&page=3",
        )
        self.assertTrue(out["has_more"])
        self.assertEqual(out["page"], 3)

    def test_branch_rules_refuses_bad_args_before_network(self):
        for args in (
            {"repo": REPO},
            {"repo": REPO, "branch": "../x"},
            {"repo": REPO, "branch": "main", "per_page": 51},
            {"repo": REPO, "branch": "main", "page": 0},
            {"repo": REPO, "branch": "main", "extra": 1},
        ):
            with self.subTest(args=args), self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_branch_rules", args, REPO)

    def test_branch_rules_rejects_unverifiable_rows(self):
        for row in (
            dict(PR_RULE, type="Bad Type"),
            dict(PR_RULE, ruleset_id=0),
            dict(PR_RULE, ruleset_id=True),
            dict(PR_RULE, ruleset_source=""),
            dict(PR_RULE, parameters=["x"]),
            {k: v for k, v in PR_RULE.items() if k != "ruleset_id"},
            "rule",
        ):
            with self.subTest(row=row), self.assertRaises(RelayGitHubError):
                self.run_action("wf_branch_rules", {"branch": "main"}, [row])
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_branch_rules", {"branch": "main"}, {"rules": []})
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_branch_rules", {"branch": "main", "per_page": 1}, [PR_RULE] * 2)

    def test_branch_rules_refuses_credential_shaped_parameters(self):
        row = dict(
            DELETION_RULE,
            type="required_status_checks",
            parameters={"required_status_checks": [{"context": "ghp_" + "x" * 36}]},
        )
        with self.assertRaises(Refused):
            self.run_action("wf_branch_rules", {"branch": "main"}, [row])

    def test_branch_rules_rejects_oversized_parameters(self):
        row = dict(PR_RULE, parameters={"pattern": "a" * 20000})
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_branch_rules", {"branch": "main"}, [row])

    def test_rulesets(self):
        out, client, minter = self.run_action("wf_rulesets", {}, [RULESET])
        self.assertEqual(
            client.calls[0][1],
            "/repos/" + REPO + "/rulesets?per_page=30&page=1&includes_parents=true",
        )
        self.assertEqual(minter.calls[0][2], {"metadata": "read"})
        self.assertEqual(
            out["rulesets"],
            [
                {
                    "id": 42,
                    "name": "main protection",
                    "target": "branch",
                    "source_type": "Repository",
                    "source": REPO,
                    "enforcement": "active",
                }
            ],
        )

    def test_rulesets_without_parents(self):
        _, client, _ = self.run_action(
            "wf_rulesets", {"includes_parents": False, "per_page": 5, "page": 2}, []
        )
        self.assertEqual(
            client.calls[0][1],
            "/repos/" + REPO + "/rulesets?per_page=5&page=2&includes_parents=false",
        )

    def test_rulesets_refuses_bad_args_before_network(self):
        for args in (
            {"repo": REPO, "includes_parents": "yes"},
            {"repo": REPO, "includes_parents": 1},
            {"repo": REPO, "per_page": 0},
            {"repo": REPO, "branch": "main"},
        ):
            with self.subTest(args=args), self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_rulesets", args, REPO)

    def test_rulesets_rejects_unverifiable_rows(self):
        for row in (
            dict(RULESET, id=-1),
            dict(RULESET, enforcement="on"),
            dict(RULESET, target="Branch Target"),
            dict(RULESET, source_type=""),
            dict(RULESET, name=""),
            dict(RULESET, name=None),
        ):
            with self.subTest(row=row), self.assertRaises(RelayGitHubError):
                self.run_action("wf_rulesets", {}, [row])

    def test_rulesets_refuses_credential_shaped_name(self):
        with self.assertRaises(Refused):
            self.run_action("wf_rulesets", {}, [dict(RULESET, name="ghp_" + "x" * 36)])

    def test_rulesets_null_target_kept(self):
        out, _, _ = self.run_action("wf_rulesets", {}, [dict(RULESET, target=None)])
        self.assertIsNone(out["rulesets"][0]["target"])


class RepoSettingsTests(unittest.TestCase):
    def run_get(self, reply, ceiling, minter=None):
        client = Client(reply)
        minter = minter or Minter()
        handle = WorkflowVerbs.for_action(
            client, Policy(ceiling), minter, "swu-wise", "wf_repo_get", REPO
        )
        return handle.run("wf_repo_get", {"repo": REPO}), client, minter

    def test_settings_read_minted_when_ceiling_holds_administration(self):
        out, client, minter = self.run_get(dict(REPO_BODY, **SETTINGS), CEILING, ScopedMinter())
        self.assertEqual(
            [m[2] for m in minter.calls],
            [{"metadata": "read"}, {"metadata": "read", "administration": "read"}],
        )
        self.assertEqual(client.calls[0][1], "/repos/" + REPO)
        self.assertEqual(client.calls[0][3], "settings-bearer")
        for key, value in SETTINGS.items():
            self.assertIs(out["repository"][key], value, key)

    def test_least_permission_kept_without_administration_ceiling(self):
        ceiling = {k: v for k, v in CEILING.items() if k != "administration"}
        out, client, minter = self.run_get(dict(REPO_BODY), ceiling, ScopedMinter())
        self.assertEqual([m[2] for m in minter.calls], [{"metadata": "read"}])
        self.assertEqual(client.calls[0][3], "base-bearer")
        self.assertNotIn("allow_squash_merge", out["repository"])

    def test_settings_mint_failure_falls_back_to_least_permission(self):
        class HalfMinter(ScopedMinter):
            def token_for(self, slug, repo, perms):
                if "administration" in perms:
                    self.calls.append((slug, repo, perms))
                    raise RuntimeError("not accepted")
                return super().token_for(slug, repo, perms)

        out, client, minter = self.run_get(dict(REPO_BODY), CEILING, HalfMinter())
        self.assertEqual(len(minter.calls), 2)
        self.assertEqual(client.calls[0][3], "base-bearer")
        self.assertEqual(out["repository"]["full_name"], REPO)

    def test_temp_clone_token_never_returned(self):
        out, _, _ = self.run_get(dict(REPO_BODY, temp_clone_token="AABBCC"), CEILING)
        self.assertNotIn("temp_clone_token", out["repository"])
        out, _, _ = self.run_get(dict(REPO_BODY, temp_clone_token=""), CEILING)
        self.assertEqual(out["repository"]["temp_clone_token"], "")

    def test_wide_object_drops_hypermedia_templates_only(self):
        body = dict(REPO_BODY, **SETTINGS)
        body["url"] = "https://api.github.com/repos/" + REPO
        body["html_url"] = "https://github.com/" + REPO
        for i in range(100):
            body[f"x{i}_url"] = "https://api.github.com/repos/" + REPO + f"/x{i}"
        out, _, _ = self.run_get(body, CEILING)
        repo = out["repository"]
        self.assertFalse(any(k.startswith("x") and k.endswith("_url") for k in repo))
        self.assertEqual(repo["url"], body["url"])
        self.assertEqual(repo["html_url"], body["html_url"])
        self.assertIs(repo["allow_squash_merge"], True)

    def test_check_shape_needs_no_minter(self):
        WorkflowVerbs.check_shape("wf_repo_get", {"repo": REPO}, REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_repo_get", {"repo": REPO, "x": 1}, REPO)


class SeatPolicySourceTests(unittest.TestCase):
    def test_every_explicit_profile_lists_the_rule_reads(self):
        path = Path(__file__).resolve().parents[2] / "deploy" / "seat-policy.json"
        doc = json.loads(path.read_text())
        for name, profile in doc["profiles"].items():
            if isinstance(profile["actions"], list):
                for action in ("wf_branch_rules", "wf_rulesets"):
                    self.assertIn(action, profile["actions"], name)


if __name__ == "__main__":
    unittest.main()
