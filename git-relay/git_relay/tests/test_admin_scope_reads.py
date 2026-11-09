"""Administration-scope read verbs and the read-only mint ceiling extension."""

import unittest

try:
    from .test_workflow_verbs import CEILING, REPO, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import CEILING, REPO, Client, Minter, Policy

from git_relay.app_tokens import (
    MINT_PERMISSION_CEILING,
    MINT_READ_ONLY_PERMISSIONS,
    mint_permission_allowed,
)
from git_relay.workflow_verbs import SPECS, Refused, RelayGitHubError, WorkflowVerbs

HOOK = {
    "id": 5,
    "name": "web",
    "active": True,
    "events": ["push"],
    "config": {"url": "https://hooks.example.invalid/abc"},
}
KEY = {
    "id": 7,
    "title": "deploy",
    "verified": True,
    "read_only": True,
    "key": "ssh-ed25519 " + "A" * 68,
}
INSTALL = {"id": 21, "app_slug": "swu-wise", "account": {"login": "Simple-With-Us"}}
OTHER_INSTALL = {"id": 22, "app_slug": "swu-echo", "account": {"login": "Simple-With-Us"}}


class MintCeilingTests(unittest.TestCase):
    def test_existing_write_capable_keys_unchanged(self):
        for key in MINT_PERMISSION_CEILING:
            self.assertTrue(mint_permission_allowed(key, "read"), key)
            self.assertTrue(mint_permission_allowed(key, "write"), key)

    def test_new_keys_are_read_only(self):
        for key in MINT_READ_ONLY_PERMISSIONS:
            self.assertTrue(mint_permission_allowed(key, "read"), key)
            self.assertFalse(mint_permission_allowed(key, "write"), key)

    def test_unknown_key_rejected(self):
        self.assertFalse(mint_permission_allowed("bogus", "read"))

    def test_new_verbs_request_exactly_read(self):
        expected = {
            "wf_repo_hooks": {"repository_hooks": "read"},
            "wf_deploy_keys": {"administration": "read"},
            "wf_org_installations": {"organization_administration": "read"},
        }
        for action, perms in expected.items():
            self.assertEqual(dict(SPECS[action][0]), perms, action)
            for key, level in perms.items():
                self.assertTrue(mint_permission_allowed(key, level), action)

    def test_no_write_verb_may_use_read_only_keys(self):
        for action, (perms, is_write) in SPECS.items():
            for key, level in perms.items():
                if key in MINT_READ_ONLY_PERMISSIONS:
                    self.assertEqual(level, "read", action)
                    self.assertFalse(is_write, action)


class AdminScopeReadTests(unittest.TestCase):
    def run_action(self, action, args, *replies, allowed=True):
        client = Client(*replies)
        ceiling = {
            **CEILING,
            "administration": "read",
            "repository_hooks": "read",
            "organization_administration": "read",
        }
        handle = WorkflowVerbs.for_action(
            client, Policy(ceiling, allowed), Minter(), "swu-wise", action, REPO
        )
        return handle.run(action, {"repo": REPO, **args}), client

    def test_repo_hooks(self):
        out, client = self.run_action("wf_repo_hooks", {}, [HOOK])
        self.assertEqual(client.calls[0][1], "/repos/" + REPO + "/hooks?per_page=30&page=1")
        self.assertEqual(out["hooks"][0]["url"], "https://hooks.example.invalid/abc")
        self.assertFalse(out["hooks"][0]["url_withheld"])

    def test_repo_hooks_withholds_credential_shaped_url(self):
        hook = dict(HOOK, config={"url": "https://x.invalid/" + "AKIA" + "0" * 16})
        out, _ = self.run_action("wf_repo_hooks", {}, [hook])
        self.assertEqual(out["hooks"][0]["url"], "<withheld: credential-shaped text>")
        self.assertTrue(out["hooks"][0]["url_withheld"])

    def test_repo_hooks_refuses_bad_events(self):
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_repo_hooks", {}, [dict(HOOK, events=[42])])

    def test_deploy_keys(self):
        out, client = self.run_action("wf_deploy_keys", {}, [KEY])
        self.assertEqual(client.calls[0][1], "/repos/" + REPO + "/keys?per_page=30&page=1")
        self.assertEqual(out["deploy_keys"][0]["id"], 7)

    def test_deploy_keys_refuses_bad_material(self):
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_deploy_keys", {}, [dict(KEY, key="not-a-key")])

    def test_org_installations(self):
        out, client = self.run_action(
            "wf_org_installations", {}, {"total_count": 1, "installations": [INSTALL]}
        )
        self.assertEqual(client.calls[0][1], "/orgs/Simple-With-Us/installations?per_page=50")
        self.assertEqual(out["installations"][0]["app_slug"], "swu-wise")

    def test_org_installations_refuses_bad_slug(self):
        bad = dict(INSTALL, app_slug="Bad Slug!")
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_org_installations", {}, {"total_count": 1, "installations": [bad]})

    def test_seat_isolation_denied_seat_gets_no_token(self):
        minter = Minter()
        client = Client([HOOK])
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(
                client, Policy(CEILING, False), minter, "swu-wise", "wf_repo_hooks", REPO
            )
        self.assertEqual(minter.calls, [])
        self.assertEqual(client.calls, [])

    def test_org_installations_excludes_other_apps(self):
        out, _ = self.run_action(
            "wf_org_installations",
            {},
            {"total_count": 2, "installations": [INSTALL, OTHER_INSTALL]},
        )
        self.assertEqual([i["app_slug"] for i in out["installations"]], ["swu-wise"])
        self.assertEqual(out["total_count"], 1)


if __name__ == "__main__":
    unittest.main()
