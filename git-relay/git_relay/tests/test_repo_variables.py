import unittest

from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy


class Variables(unittest.TestCase):
    def test_fixed_methods_status_and_no_value_receipt(self):
        class Strict(Client):
            def _call(self, *a, expected_status=None, **k):
                self.expected = expected_status
                return super()._call(*a, expected_status=expected_status, **k)

        for action, method, status, path in [
            ("wf_repo_variable_create", "POST", 201, "actions/variables"),
            ("wf_repo_variable_update", "PATCH", 204, "actions/variables/BUILD_MODE"),
            ("wf_repo_variable_delete", "DELETE", 204, "actions/variables/BUILD_MODE"),
        ]:
            c = Strict({})
            args = {"repo": REPO, "name": "BUILD_MODE"}
            if not action.endswith("delete"):
                args["value"] = "release"
            out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(
                action, args
            )
            self.assertEqual(c.calls[0][0:2], (method, f"/repos/{REPO}/{path}"))
            self.assertEqual(c.calls[0][3], "own")
            self.assertEqual(c.expected, status)
            self.assertNotIn("value", out)
            if action.endswith("create"):
                self.assertNotIn(action, _REAPPLY_STATE_ACTIONS)
            else:
                self.assertIn(action, _REAPPLY_STATE_ACTIONS)
            self.assertEqual(SPECS[action][0], {"actions_variables": "write"})

    def test_nonsecret_closed_bounded(self):
        a = {"repo": REPO, "name": "BUILD_MODE", "value": "release"}
        for extra in [
            {"name": "GITHUB_TOKEN"},
            {"name": "../evil"},
            {"value": "é" * 4097},
            {"value": "ghp_" + "a" * 36},
            {"secret": True},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_repo_variable_create", {**a, **extra}, REPO)

    def test_list_compact_page(self):
        c = Client(
            {
                "total_count": 1,
                "variables": [
                    {"name": "MODE", "value": "dev", "created_at": "x", "updated_at": "x"}
                ],
            }
        )
        a = "wf_repo_variables"
        out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, {"repo": REPO, "page": 2, "per_page": 1}
        )
        self.assertEqual(out["page"], 2)

    def test_permission_never_assumed_from_shared_ceiling(self):
        from .test_workflow_verbs import Minter

        for a in [
            "wf_repo_variables",
            "wf_repo_variable_get",
            "wf_repo_variable_create",
            "wf_repo_variable_update",
            "wf_repo_variable_delete",
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(Client(), Policy(), Minter(), "swu-wise", a, REPO)

    def test_variable_update_A_B_A_reapplies_only_known_success(self):
        from git_relay.outcomes import OutcomeStore, key_for

        store = OutcomeStore()
        for action in ("wf_repo_variable_update", "wf_repo_variable_delete"):
            keys = [
                key_for("swu-wise", action, {"repo": REPO, "name": "MODE", "value": v})
                for v in ("A", "B", "A")
            ]
            for key in keys:
                self.assertIsNone(store.begin(key, reapply_ok=action in _REAPPLY_STATE_ACTIONS))
                store.finish(key, "ok", {"completed": True, "name": "MODE"})
            self.assertIsNone(store.begin(keys[0], reapply_ok=True))
            store.finish(keys[0], "unknown")
            self.assertEqual(store.begin(keys[0], reapply_ok=True)["state"], "unknown")

    def test_supported_permission_key_not_a_configured_grant(self):
        from git_relay.app_tokens import MINT_PERMISSION_CEILING

        self.assertIn("actions_variables", MINT_PERMISSION_CEILING)
        self.test_permission_never_assumed_from_shared_ceiling()

    def test_read_blocks_external_credential_and_create_201_empty_object(self):
        import io

        from git_relay.github_client import GitHubClient

        class Response(io.BytesIO):
            status = 201

        client = GitHubClient("synthetic", opener=lambda *a, **k: Response(b"{}"))
        self.assertEqual(
            client._call(
                "POST", "/repos/a/b/actions/variables", expected_status=201, allow_empty_object=True
            ),
            {},
        )
        with self.assertRaises(Refused):
            WorkflowVerbs._variable_receipt(
                {"name": "MODE", "value": "ghp_" + "x" * 36, "created_at": "x", "updated_at": "x"}
            )

    def test_delete_absent_is_completed_other_errors_preserved(self):
        from git_relay.github_client import RelayGitHubError

        class ErrorClient(Client):
            def _call(self, method, path, *args, **kwargs):
                if method == "GET":
                    return self.repository
                raise RelayGitHubError(self.status, "synthetic")

        action = "wf_repo_variable_delete"
        c = ErrorClient()
        c.status = 404
        c.repository = {"id": 1, "full_name": REPO}
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
        self.assertEqual(
            h.run(action, {"repo": REPO, "name": "MODE"}), {"completed": True, "name": "MODE"}
        )
        for repository in (
            {},
            {"id": True, "full_name": REPO},
            {"id": 1, "full_name": "wrong/repo"},
        ):
            c.repository = repository
            with self.assertRaises(RelayGitHubError):
                h.run(action, {"repo": REPO, "name": "MODE"})
        c.repository = {"id": 1, "full_name": REPO}
        for status in (403, 429, 502):
            c.status = status
            with self.assertRaises(RelayGitHubError):
                h.run(action, {"repo": REPO, "name": "MODE"})

    def test_lone_surrogates_fail_closed_without_upstream_write(self):
        import json

        from git_relay.github_client import RelayGitHubError

        for value in (json.loads('"\\ud800"'), json.loads('"\\udfff"'), "valid" + chr(0xD800)):
            for action in ("wf_repo_variable_create", "wf_repo_variable_update"):
                with self.assertRaises(Refused):
                    WorkflowVerbs.check_shape(
                        action, {"repo": REPO, "name": "MODE", "value": value}, REPO
                    )
            with self.assertRaises(RelayGitHubError) as caught:
                WorkflowVerbs._variable_receipt(
                    {"name": "MODE", "value": value, "created_at": "x", "updated_at": "x"}
                )
            self.assertEqual(caught.exception.status, 502)
            self.assertNotIn(value, str(caught.exception))

    def test_utf8_byte_boundary_is_preserved(self):
        from git_relay.github_client import RelayGitHubError

        for value in ("a" * 8192, "é" * 4096, "😀" * 2048):
            self.assertEqual(WorkflowVerbs._variable_value({"value": value}), value)
            receipt = {"name": "MODE", "value": value, "created_at": "x", "updated_at": "x"}
            self.assertEqual(WorkflowVerbs._variable_receipt(receipt), receipt)
            with self.assertRaises(Refused):
                WorkflowVerbs._variable_value({"value": value + "a"})
            with self.assertRaises(RelayGitHubError):
                WorkflowVerbs._variable_receipt({**receipt, "value": value + "a"})
