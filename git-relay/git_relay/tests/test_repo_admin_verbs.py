import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.repo_admin_verbs import REPO_ADMIN_SPECS
from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy


def run(action, args, *replies):
    c = Client(*replies)
    out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(action, args)
    return c, out


def repo_obj(**kw):
    base = {
        "id": 5,
        "full_name": REPO,
        "description": "d",
        "homepage": "https://x.invalid/",
        "has_wiki": False,
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "allow_auto_merge": True,
        "delete_branch_on_merge": True,
        "topics": ["a"],
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
        },
    }
    base.update(kw)
    return base


class RepoAdminVerbs(unittest.TestCase):
    def test_registered(self):
        for action, (_p, write) in REPO_ADMIN_SPECS.items():
            self.assertIn(action, SPECS)
            self.assertTrue(hasattr(WorkflowVerbs, action))
            if write:
                self.assertIn(action, _REAPPLY_STATE_ACTIONS)

    def test_update_sends_only_named_fields_and_verifies(self):
        c, out = run(
            "wf_repo_update",
            {
                "repo": REPO,
                "has_wiki": False,
                "allow_auto_merge": True,
                "security_and_analysis": {"secret_scanning": "enabled"},
            },
            repo_obj(),
        )
        self.assertEqual(c.calls[0][0:2], ("PATCH", f"/repos/{REPO}"))
        self.assertEqual(
            set(c.calls[0][2]), {"has_wiki", "allow_auto_merge", "security_and_analysis"}
        )
        self.assertEqual(
            c.calls[0][2]["security_and_analysis"], {"secret_scanning": {"status": "enabled"}}
        )
        self.assertFalse(out["settings"]["has_wiki"])

    def test_update_unverified_echo_is_an_error(self):
        with self.assertRaises(RelayGitHubError):
            run("wf_repo_update", {"repo": REPO, "has_wiki": True}, repo_obj(has_wiki=False))
        with self.assertRaises(RelayGitHubError):
            run(
                "wf_repo_update",
                {"repo": REPO, "security_and_analysis": {"secret_scanning": "disabled"}},
                repo_obj(),
            )

    def test_cleared_homepage_accepts_null(self):
        _c, out = run("wf_repo_update", {"repo": REPO, "homepage": ""}, repo_obj(homepage=None))
        self.assertIsNone(out["settings"]["homepage"])

    def test_refusals(self):
        for bad in (
            {},
            {"has_wiki": "no"},
            {"homepage": "javascript:x"},
            {"homepage": "https://u:p@h.invalid"},
            {"description": "x" * 2000},
            {"private": True},
            {"name": "x"},
            {"archived": True},
            {"squash_merge_commit_title": "NOPE"},
            {"security_and_analysis": {}},
            {"security_and_analysis": {"advanced_security": "enabled"}},
            {"security_and_analysis": {"secret_scanning": "on"}},
        ):
            with self.assertRaises(Refused, msg=str(bad)):
                run("wf_repo_update", {"repo": REPO, **bad})

    def test_settings_read(self):
        c, out = run("wf_repo_settings", {"repo": REPO}, repo_obj())
        self.assertEqual(c.calls[0][0:2], ("GET", f"/repos/{REPO}"))
        self.assertEqual(out["settings"]["security_and_analysis"]["secret_scanning"], "enabled")
        self.assertNotIn("temp_clone_token", out["settings"])

    def test_topics(self):
        c, _out = run(
            "wf_repo_topics_set",
            {"repo": REPO, "topics": ["ios", "swift"]},
            {"names": ["swift", "ios"]},
        )
        self.assertEqual(
            c.calls[0][0:3], ("PUT", f"/repos/{REPO}/topics", {"names": ["ios", "swift"]})
        )
        for bad in (["Bad"], ["a", "a"], ["x" * 51], [1], "ios", ["-a"], ["a"] * 21):
            with self.assertRaises(Refused):
                run("wf_repo_topics_set", {"repo": REPO, "topics": bad})
        with self.assertRaises(RelayGitHubError):
            run("wf_repo_topics_set", {"repo": REPO, "topics": ["a"]}, {"names": ["b"]})
