"""Offline collaborator inventory: exact least scope, bounds and projection."""

import copy
import unittest
from .test_workflow_verbs import REPO, Client, Minter, Policy
from git_relay.workflow_verbs import WorkflowVerbs, Refused, RelayGitHubError, SPECS

ROW = {
    "id": 1,
    "login": "fixture-user",
    "type": "User",
    "role_name": "custom reviewer",
    "permissions": dict(pull=True, triage=False, push=False, maintain=False, admin=False),
    "email": "never-return@example.invalid",
    "node_id": "omitted",
}
ACTION = "wf_repo_collaborators"


class CollaboratorTests(unittest.TestCase):
    def run_action(self, rows, **args):
        client, minter = Client(rows), Minter()
        handle = WorkflowVerbs.for_action(
            client, Policy({"metadata": "read"}), minter, "swu-wise", ACTION, REPO
        )
        return handle.run(ACTION, dict(repo=REPO, **args)), client, minter

    def test_exact_path_minimal_scope_and_projection(self):
        out, client, minter = self.run_action(
            [ROW], page=2, per_page=1, affiliation="direct", permission="moderate"
        )
        self.assertEqual(
            client.calls[0][:2],
            (
                "GET",
                "/repos/"
                + REPO
                + "/collaborators?per_page=1&page=2&affiliation=direct&permission=moderate",
            ),
        )
        self.assertEqual(minter.calls[0][2], {"metadata": "read"})
        self.assertFalse(SPECS[ACTION][1])
        self.assertEqual(
            set(out["collaborators"][0]), {"id", "login", "type", "role_name", "permissions"}
        )
        self.assertTrue(out["has_more"])
        self.assertEqual(out["page"], 2)
        self.assertEqual(out["affiliation"], "direct")

    def test_empty_page(self):
        self.assertEqual(
            self.run_action([])[0],
            dict(collaborators=[], page=1, has_more=False, affiliation="all"),
        )

    def test_bad_inputs_fail_shape_without_network(self):
        for args in [
            {"page": True},
            {"page": 0},
            {"page": 1001},
            {"per_page": 51},
            {"affiliation": []},
            {"permission": None},
            {"permission": "owner"},
            {"url": "https://elsewhere.invalid"},
        ]:
            with self.subTest(args=args), self.assertRaises(Refused):
                WorkflowVerbs.check_shape(ACTION, dict(repo=REPO, **args), REPO)

    def test_malformed_projection_and_duplicates_refused(self):
        for change in [
            {"id": True},
            {"id": 9007199254740992},
            {"login": "bad/name"},
            {"login": ""},
            {"type": "unknown"},
            {"role_name": " "},
            {"role_name": "a" * 101},
            {"role_name": "bad\nrole"},
            {"permissions": {"pull": True}},
            {"permissions": []},
        ]:
            row = copy.deepcopy(ROW)
            row.update(change)
            with self.subTest(change=change), self.assertRaises((Refused, RelayGitHubError)):
                self.run_action([row])
        for rows in [
            [ROW, ROW],
            [ROW, dict(ROW, id=2, login="FIXTURE-USER")],
            [None],
            {},
            [ROW] * 51,
        ]:
            with self.subTest(rows=type(rows).__name__), self.assertRaises(RelayGitHubError):
                self.run_action(rows)

    def test_scope_denial_never_mints(self):
        minter = Minter()
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(Client(), Policy({}), minter, "swu-wise", ACTION, REPO)
        self.assertEqual(minter.calls, [])

    def test_bot_custom_role_and_bounded_page(self):
        row = copy.deepcopy(ROW)
        row.update(login="fixture-helper[bot]", type="Bot")
        self.assertEqual(
            self.run_action([row])[0]["collaborators"][0]["role_name"], "custom reviewer"
        )
        with self.assertRaises(RelayGitHubError):
            self.run_action([ROW, dict(ROW, id=2, login="other")], per_page=1)

    def test_credential_shaped_role_is_not_returned(self):
        from unittest.mock import patch

        row = copy.deepcopy(ROW)
        with patch("git_relay.workflow_verbs.find_secret", return_value="fixture category"):
            with self.assertRaises(Refused):
                self.run_action([row])
