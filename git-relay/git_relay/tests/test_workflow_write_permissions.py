import base64
import unittest

from git_relay.workflow_verbs import CeilingDenied, Refused, WorkflowVerbs

from .test_workflow_verbs import CEILING, REPO, SHA, Client, Minter, Policy


class WorkflowPermissions(unittest.TestCase):
    def full_args(self, action, args):
        full = {"repo": REPO, "branch": "wise/test", "message": "workflow", **args}
        if action in ("wf_file_delete", "wf_file_rename"):
            full.setdefault("prior_sha", SHA)
        if action in ("wf_files_put", "wf_file_rename"):
            full.setdefault("expected_head_sha", SHA)
        if action == "wf_file_put":
            full.setdefault("content_b64", "eA==")
        if action == "wf_files_put":
            full["files"] = [{"content_b64": "eA==", **f} for f in full["files"]]
        return full

    def handle(self, action, args, ceiling=None):
        minter = Minter()
        handle = WorkflowVerbs.for_action(
            Client(),
            Policy(ceiling or {**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            action,
            REPO,
            args=self.full_args(action, args),
        )
        return handle, minter

    def test_workflow_edits_mint_workflow_scope(self):
        for action, args in [
            ("wf_file_put", {"path": ".github/workflows/ci.yml"}),
            ("wf_file_delete", {"path": ".github/workflows/ci.yml"}),
            (
                "wf_file_rename",
                {"source_path": "docs/ci.yml", "destination_path": ".github/workflows/ci.yml"},
            ),
            ("wf_files_put", {"files": [{"path": ".github/workflows/ci.yml"}]}),
        ]:
            handle, minter = self.handle(action, args)
            self.assertEqual(
                minter.calls[0][2], {"contents": "write", "metadata": "read", "workflows": "write"}
            )
            self.assertFalse(handle._write_path_denied(".github/workflows/ci.yml"))

    def test_ordinary_write_does_not_mint_workflows(self):
        handle, minter = self.handle("wf_files_put", {"files": [{"path": "docs/ci.yml"}]})
        self.assertNotIn("workflows", minter.calls[0][2])
        self.assertTrue(handle._write_path_denied(".github/workflows/ci.yml"))

    def test_ceiling_missing_fails_before_mint(self):
        minter = Minter()
        with self.assertRaises(CeilingDenied):
            WorkflowVerbs.for_action(
                Client(),
                Policy(CEILING),
                minter,
                "swu-wise",
                "wf_files_put",
                REPO,
                args=self.full_args(
                    "wf_files_put", {"files": [{"path": ".github/workflows/ci.yml"}]}
                ),
            )
        self.assertEqual(minter.calls, [])

    def test_parent_dirs_still_refused_with_permission(self):
        handle, _ = self.handle("wf_file_delete", {"path": ".github/workflows/ci.yml"})
        for p in [".github", ".github/workflows"]:
            self.assertTrue(handle._write_path_denied(p))

    def test_shape_accepts_workflow_without_mint(self):
        WorkflowVerbs.check_shape(
            "wf_files_put",
            {
                "repo": REPO,
                "branch": "wise/test",
                "message": "workflow",
                "expected_head_sha": SHA,
                "files": [
                    {
                        "path": ".github/workflows/release.yml",
                        "content_b64": base64.b64encode(b"name: release").decode(),
                    }
                ],
            },
            REPO,
        )

    def test_default_branch_still_refused(self):
        handle, _ = self.handle("wf_file_put", {"path": ".github/workflows/ci.yml"})
        handle.client = Client({"default_branch": "main"})
        with self.assertRaises(Refused):
            handle.run(
                "wf_file_put",
                {
                    "repo": REPO,
                    "branch": "main",
                    "message": "m",
                    "path": ".github/workflows/ci.yml",
                    "content_b64": "eA==",
                },
            )

    def test_secret_scan_still_refuses(self):
        handle, _ = self.handle("wf_file_put", {"path": ".github/workflows/ci.yml"})
        with self.assertRaises(Refused):
            handle.run(
                "wf_file_put",
                {
                    "repo": REPO,
                    "branch": "wise/test",
                    "message": "m",
                    "path": ".github/workflows/ci.yml",
                    "content_b64": base64.b64encode(("ghp_" + "A" * 40).encode()).decode(),
                },
            )

    def test_invalid_shape_secret_or_large_list_never_mints(self):
        for args in (
            [1],
            {"repo": REPO, "files": [{}] * 21},
            self.full_args(
                "wf_file_put",
                {
                    "path": ".github/workflows/x.yml",
                    "content_b64": base64.b64encode(("ghp_" + "A" * 40).encode()).decode(),
                },
            ),
        ):
            action = "wf_files_put" if isinstance(args, dict) and "files" in args else "wf_file_put"
            minter = Minter()
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(
                    Client(),
                    Policy({**CEILING, "workflows": "write"}),
                    minter,
                    "swu-wise",
                    action,
                    REPO,
                    args=args,
                )
            self.assertEqual(minter.calls, [])

    def test_denied_ceiling_precedes_expensive_secret_shape_scan(self):
        from unittest.mock import patch

        args = self.full_args(
            "wf_file_put", {"path": ".github/workflows/x.yml", "content_b64": "!!!"}
        )
        with patch.object(WorkflowVerbs, "check_shape") as scan:
            with self.assertRaises(CeilingDenied):
                WorkflowVerbs.for_action(
                    Client(), Policy(CEILING), Minter(), "swu-wise", "wf_file_put", REPO, args=args
                )
            scan.assert_not_called()

    def test_preauthorized_dispatch_reuses_gateway_shape_validation(self):
        from unittest.mock import patch

        args = self.full_args("wf_file_put", {"path": ".github/workflows/x.yml"})
        with patch.object(WorkflowVerbs, "check_shape") as scan:
            WorkflowVerbs.for_action(
                Client(),
                Policy({**CEILING, "workflows": "write"}),
                Minter(),
                "swu-wise",
                "wf_file_put",
                REPO,
                args=args,
                preauthorized=True,
            )
            scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
