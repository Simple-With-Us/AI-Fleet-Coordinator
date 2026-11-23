import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.hook_verbs import HOOK_SPECS
from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy

ORG = REPO.split("/")[0]
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH7VILvQ5Z8jfqRY8QYhbNwlHDxgyEURmOuwYdu609ff probe"


def run(action, args, *replies):
    c = Client(*replies)
    out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(action, args)
    return c, out


def hook(**kw):
    base = {
        "id": 9,
        "name": "web",
        "active": True,
        "events": ["push"],
        "config": {"url": "https://example.invalid/h", "content_type": "json", "secret": "********"},
    }
    base.update(kw)
    return base


class HookVerbs(unittest.TestCase):
    def test_every_verb_registered(self):
        for action, (_perm, write) in HOOK_SPECS.items():
            self.assertIn(action, SPECS)
            self.assertTrue(hasattr(WorkflowVerbs, action), action)
            if write:
                self.assertIn(action, _REAPPLY_STATE_ACTIONS)

    def test_create_sends_secret_but_never_returns_it(self):
        c, out = run(
            "wf_hook_create",
            {"repo": REPO, "url": "https://example.invalid/h", "secret": "s3cr3t-value", "events": ["push"], "active": False},
            hook(active=False),
        )
        self.assertEqual(c.calls[0][0:2], ("POST", f"/repos/{REPO}/hooks"))
        self.assertEqual(c.calls[0][2]["config"]["secret"], "s3cr3t-value")
        self.assertEqual(c.calls[0][2]["name"], "web")
        self.assertNotIn("s3cr3t-value", repr(out))
        self.assertTrue(out["hook"]["has_secret"])
        self.assertEqual(out["hook"]["url"], "https://example.invalid/h")

    def test_refusals(self):
        good = {"repo": REPO, "url": "https://example.invalid/h"}
        for bad in (
            {"url": "https://user:pw@example.invalid/h"},
            {"url": "ftp://example.invalid/h"},
            {"content_type": "xml"},
            {"events": []},
            {"events": ["Push!"]},
            {"active": "yes"},
            {"extra": 1},
        ):
            with self.assertRaises(Refused):
                run("wf_hook_create", {**good, **bad})
        with self.assertRaises(Refused):
            run("wf_hook_update", {"repo": REPO, "id": 9})
        with self.assertRaises(Refused):
            run("wf_hook_get", {"repo": REPO, "id": True})

    def test_update_delete_ping_paths(self):
        c, out = run("wf_hook_update", {"repo": REPO, "id": 9, "active": False}, hook(active=False))
        self.assertEqual(c.calls[0][0:2], ("PATCH", f"/repos/{REPO}/hooks/9"))
        self.assertEqual(c.calls[0][2], {"active": False})
        c, out = run("wf_hook_ping", {"repo": REPO, "id": 9}, {})
        self.assertEqual(c.calls[0][0:2], ("POST", f"/repos/{REPO}/hooks/9/pings"))
        self.assertEqual(out, {"completed": True, "id": 9})
        c, out = run("wf_hook_delete", {"repo": REPO, "id": 9}, {})
        self.assertEqual(c.calls[0][0:2], ("DELETE", f"/repos/{REPO}/hooks/9"))
        self.assertEqual(out, {"completed": True, "id": 9})

    def test_org_hooks_use_the_owner(self):
        c, out = run("wf_org_hooks", {"repo": REPO}, [hook()])
        self.assertEqual(c.calls[0][1].split("?")[0], f"/orgs/{ORG}/hooks")
        self.assertEqual(out["hooks"][0]["id"], 9)
        c, _ = run("wf_org_hook_create", {"repo": REPO, "url": "https://example.invalid/h"}, hook())
        self.assertEqual(c.calls[0][0:2], ("POST", f"/orgs/{ORG}/hooks"))

    def test_malformed_hook_receipt_is_refused(self):
        with self.assertRaises(RelayGitHubError):
            run("wf_hook_get", {"repo": REPO, "id": 9}, {"id": 9})

    def test_deploy_key_takes_public_key_only(self):
        item = {"id": 5, "title": "t", "key": KEY, "read_only": True, "verified": True}
        c, out = run("wf_deploy_key_add", {"repo": REPO, "title": "t", "key": KEY}, item)
        self.assertEqual(c.calls[0][2]["read_only"], True)
        self.assertEqual(out["deploy_key"]["id"], 5)
        for bad in ("-----BEGIN OPENSSH " + "PRIVATE" + " KEY-----\nabc", "not a key", ""):
            with self.assertRaises(Refused):
                run("wf_deploy_key_add", {"repo": REPO, "title": "t", "key": bad})

    def test_environment_and_policies(self):
        env = {"id": 3, "name": "prod env", "created_at": "a", "updated_at": "b"}
        c, out = run("wf_environment_put", {"repo": REPO, "name": "prod env", "wait_timer": 5}, env)
        self.assertEqual(c.calls[0][0:2], ("PUT", f"/repos/{REPO}/environments/prod%20env"))
        self.assertEqual(c.calls[0][2], {"wait_timer": 5})
        with self.assertRaises(Refused):
            run("wf_environment_put", {"repo": REPO, "name": "x", "wait_timer": 99999})
        with self.assertRaises(Refused):
            run("wf_environment_put", {"repo": REPO, "name": "x", "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": True}})
        c, out = run("wf_env_branch_policy_create", {"repo": REPO, "name": "prod", "policy_name": "main"}, {"id": 4, "name": "main", "type": "branch"})
        self.assertEqual(out["policy"], {"id": 4, "name": "main", "type": "branch"})

    def test_env_variables(self):
        c, out = run("wf_env_variable_set", {"repo": REPO, "name": "prod", "variable": "FOO", "value": "1"}, {})
        self.assertEqual(c.calls[0][0:2], ("PATCH", f"/repos/{REPO}/environments/prod/variables/FOO"))
        self.assertEqual(out, {"completed": True, "name": "prod", "variable": "FOO"})
        with self.assertRaises(Refused):
            run("wf_env_variable_set", {"repo": REPO, "name": "prod", "variable": "GITHUB_X", "value": "1"})


if __name__ == "__main__":
    unittest.main()
