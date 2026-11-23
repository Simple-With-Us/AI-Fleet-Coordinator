import base64
import os
import unittest

from git_relay.secret_scan import scan_args
from git_relay.secret_verbs import SCOPES, SECRET_SPECS
from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy

ORG = REPO.split("/")[0]
SEALED = base64.b64encode(os.urandom(80)).decode()


def run(action, args, *replies):
    c = Client(*replies)
    out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(action, args)
    return c, out


class SecretVerbs(unittest.TestCase):
    def test_every_verb_registered_with_scope_permission(self):
        for scope, (perm, _tpl, org) in SCOPES.items():
            self.assertEqual(SPECS[f"wf_secret_put_{scope}"], ({perm: "write"}, True))
            self.assertEqual(SPECS[f"wf_secrets_{scope}"], ({perm: "read"}, False))
            self.assertEqual(f"wf_secret_repos_{scope}" in SPECS, org)
        for action, (_p, write) in SECRET_SPECS.items():
            self.assertTrue(hasattr(WorkflowVerbs, action), action)
            if write:
                self.assertIn(action, _REAPPLY_STATE_ACTIONS)

    def test_put_sends_only_ciphertext_to_fixed_path(self):
        c, out = run(
            "wf_secret_put_org_actions",
            {
                "repo": REPO,
                "name": "UM_TOKEN",
                "encrypted_value": SEALED,
                "key_id": "568250167242549743",
                "visibility": "selected",
                "selected_repository_ids": [1, 2],
            },
        )
        self.assertEqual(c.calls[0][0:2], ("PUT", f"/orgs/{ORG}/actions/secrets/UM_TOKEN"))
        self.assertEqual(
            c.calls[0][2],
            {
                "encrypted_value": SEALED,
                "key_id": "568250167242549743",
                "visibility": "selected",
                "selected_repository_ids": [1, 2],
            },
        )
        self.assertEqual(out, {"completed": True, "name": "UM_TOKEN"})

    def test_repo_and_dependabot_paths(self):
        a = {"repo": REPO, "name": "X_1", "encrypted_value": SEALED, "key_id": "k1"}
        c, _ = run("wf_secret_put_repo_actions", a)
        self.assertEqual(c.calls[0][1], f"/repos/{REPO}/actions/secrets/X_1")
        c, _ = run("wf_secret_put_repo_dependabot", a)
        self.assertEqual(c.calls[0][1], f"/repos/{REPO}/dependabot/secrets/X_1")
        c, _ = run("wf_secret_put_env_actions", {**a, "environment": "prod env"}, {"id": 77})
        self.assertEqual(c.calls[1][1], "/repositories/77/environments/prod%20env/secrets/X_1")

    def test_refusals(self):
        good = {"repo": REPO, "name": "X_1", "encrypted_value": SEALED, "key_id": "k1"}
        for bad in [
            {"name": "GITHUB_X"},
            {"name": "../x"},
            {"encrypted_value": "plain text secret value here!!"},
            {"encrypted_value": base64.b64encode(b"short").decode()},
            {"key_id": "a b"},
            {"extra": 1},
            {"visibility": "selected"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_secret_put_repo_actions", {**good, **bad}, REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(
                "wf_secret_put_org_actions", {**good, "visibility": "bad"}, REPO
            )

    def test_ciphertext_passes_request_scan(self):
        for _ in range(500):
            scan_args({"encrypted_value": base64.b64encode(os.urandom(120)).decode()})

    def test_list_is_names_only(self):
        c, out = run(
            "wf_secrets_org_actions",
            {"repo": REPO},
            {
                "total_count": 1,
                "secrets": [
                    {
                        "name": "A",
                        "created_at": "x",
                        "updated_at": "y",
                        "visibility": "all",
                        "value": "leak",
                    }
                ],
            },
        )
        self.assertEqual(
            out["secrets"],
            [{"name": "A", "created_at": "x", "updated_at": "y", "visibility": "all"}],
        )

    def test_key_and_delete(self):
        c, out = run("wf_secret_key_repo_actions", {"repo": REPO}, {"key_id": "12", "key": "QUJD"})
        self.assertEqual(out, {"key_id": "12", "key": "QUJD"})
        c, out = run("wf_secret_delete_repo_actions", {"repo": REPO, "name": "X_1"})
        self.assertEqual(c.calls[0][0:2], ("DELETE", f"/repos/{REPO}/actions/secrets/X_1"))

    def test_org_variable_upsert(self):
        c, out = run("wf_org_variable_set", {"repo": REPO, "name": "UM_MODE", "value": "on"})
        self.assertEqual(
            c.calls[0][0:2], ("PATCH", f"/orgs/{ORG}/actions/variables/UM_MODE")
        )
        self.assertEqual(out["name"], "UM_MODE")

class _Seq(Client):
    def _call(self, method, path, payload=None, bearer=None, expected_status=None, allow_empty_object=False):
        from git_relay.github_client import RelayGitHubError

        self.calls.append((method, path, payload, bearer))
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            raise RelayGitHubError(reply, "x")
        return reply


class Review(unittest.TestCase):
    def test_variable_upsert_converges_after_409(self):
        c = _Seq(404, 409, {})
        out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_org_variable_set", REPO).run(
            "wf_org_variable_set", {"repo": REPO, "name": "UM_MODE", "value": "on"}
        )
        self.assertEqual([m for m, *_ in c.calls], ["PATCH", "POST", "PATCH"])
        self.assertEqual(out["name"], "UM_MODE")

    def test_secret_repos_rejects_oversized_page(self):
        rows = [{"id": i, "full_name": "o/r"} for i in range(3)]
        with self.assertRaises(Exception):
            run("wf_secret_repos_org_actions", {"repo": REPO, "name": "X_1", "per_page": 1}, {"repositories": rows, "total_count": 3})

