import base64
import unittest

from git_relay.workflow_verbs import (
    TOO_LARGE_TO_VERIFY,
    CeilingDenied,
    Refused,
    WorkflowVerbs,
)

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


class WorkflowPrElevation(unittest.TestCase):
    MERGE = {"repo": REPO, "number": 7, "method": "squash", "expected_head_sha": SHA}

    def run_merge(self, files, ceiling, action="wf_merge_guarded"):
        minter = Minter()
        if action == "wf_merge_guarded":
            client = Client(files, {"merged": True, "sha": "b" * 40})
        else:
            pr = {"head": {"sha": SHA}, "base": {"ref": "main"}}
            client = Client(pr, {"files": files}, {"message": "ok"})
        args = (
            self.MERGE
            if action == "wf_merge_guarded"
            else {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        )
        handle = WorkflowVerbs.for_action(
            client, Policy(ceiling), minter, "swu-wise", action, REPO, args=args
        )
        handle.run(action, args)
        return client, minter

    def test_workflow_pr_mints_second_token_with_workflows(self):
        for action in ("wf_merge_guarded", "wf_update_branch"):
            client, minter = self.run_merge(
                [{"filename": "docs/a.md"}, {"filename": ".github/workflows/ci.yml"}],
                {**CEILING, "workflows": "write"},
                action,
            )
            self.assertEqual(len(minter.calls), 2, action)
            self.assertEqual(minter.calls[1][2].get("workflows"), "write")
            self.assertNotIn("workflows", minter.calls[0][2])
            self.assertEqual(client.calls[-2][0], "GET")
            self.assertEqual(client.calls[-1][0], "PUT")
            if action == "wf_update_branch":
                self.assertIn(f"/compare/{SHA}...main", client.calls[1][1])

    def test_ordinary_pr_does_not_elevate(self):
        for action in ("wf_merge_guarded", "wf_update_branch"):
            _, minter = self.run_merge(
                [{"filename": "docs/a.md"}], {**CEILING, "workflows": "write"}, action
            )
            self.assertEqual(len(minter.calls), 1, action)
            self.assertNotIn("workflows", minter.calls[0][2])

    def test_renamed_out_of_workflows_elevates(self):
        _, minter = self.run_merge(
            [{"filename": "docs/ci.yml", "previous_filename": ".github/workflows/ci.yml"}],
            {**CEILING, "workflows": "write"},
        )
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")

    def test_ceiling_without_workflows_still_refused_before_write(self):
        minter = Minter()
        client = Client([{"filename": ".github/workflows/ci.yml"}])
        handle = WorkflowVerbs.for_action(
            client, Policy(CEILING), minter, "swu-wise", "wf_merge_guarded", REPO, args=self.MERGE
        )
        with self.assertRaises(CeilingDenied):
            handle.run("wf_merge_guarded", self.MERGE)
        self.assertEqual([c[0] for c in client.calls], ["GET"])
        self.assertEqual(len(minter.calls), 1)

    def test_preread_failure_blocks_write(self):
        from git_relay.github_client import RelayGitHubError
        from git_relay.server import PreReadUnavailable

        class Boom(Client):
            def _call(self, method, path, *a, **k):
                self.calls.append((method, path))
                raise RelayGitHubError(503, "down")

        minter, client = Minter(), Boom()
        handle = WorkflowVerbs.for_action(
            client, Policy(CEILING), minter, "swu-wise", "wf_merge_guarded", REPO, args=self.MERGE
        )
        with self.assertRaises(PreReadUnavailable):
            handle.run("wf_merge_guarded", self.MERGE)
        self.assertEqual([c[0] for c in client.calls], ["GET"])

    def test_update_branch_refuses_moved_head_before_any_mint_or_write(self):
        minter = Minter()
        client = Client({"head": {"sha": "c" * 40}, "base": {"ref": "main"}})
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        with self.assertRaises(Refused):
            handle.run("wf_update_branch", args)
        self.assertEqual([c[0] for c in client.calls], ["GET"])
        self.assertEqual(len(minter.calls), 1)

    def update_branch(self, files, ceiling=None):
        minter = Minter()
        client = Client(
            {"head": {"sha": SHA}, "base": {"ref": "release/2026 nov"}},
            {"files": files},
            {"message": "ok"},
        )
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy(ceiling or {**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        return client, minter, handle, args

    def test_update_branch_large_compare_is_one_read_and_elevates(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(150)]
        files[120] = {"filename": ".github/workflows/ci.yml"}
        client, minter, handle, args = self.update_branch(files)
        handle.run("wf_update_branch", args)
        self.assertEqual(len([c for c in client.calls if "/compare/" in c[1]]), 1)
        self.assertNotIn("per_page", client.calls[1][1])
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")
        self.assertEqual(client.calls[-1][0], "PUT")

    def test_update_branch_exactly_100_files_is_one_read_no_loop(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(100)]
        client, minter, handle, args = self.update_branch(files)
        handle.run("wf_update_branch", args)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(minter.calls), 1)

    def update_branch_trees(self, files, head_rows=(), base_rows=(), base_sha="b" * 40):
        minter = Minter()
        pr = {"head": {"sha": SHA}, "base": {"ref": "main", "sha": base_sha}}
        compare = {"files": files}
        head_tree = {
            "truncated": False,
            "tree": [{"type": "blob", "path": "src/a.py", "sha": "1" * 40}, *head_rows],
        }
        base_tree = {
            "truncated": False,
            "tree": [{"type": "blob", "path": "src/a.py", "sha": "1" * 40}, *base_rows],
        }
        client = Client(pr, compare, head_tree, base_tree, {"message": "ok"})
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        return client, minter, handle, args

    def test_update_branch_300_files_without_workflow_verified_via_trees(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        client, minter, handle, args = self.update_branch_trees(files)
        handle.run("wf_update_branch", args)
        tree_calls = [c for c in client.calls if "/git/trees/" in c[1]]
        self.assertEqual(len(tree_calls), 2)
        self.assertIn("recursive=1", tree_calls[0][1])
        self.assertEqual(client.calls[-1][0], "PUT")
        self.assertEqual(len(minter.calls), 1)
        self.assertNotIn("workflows", minter.calls[0][2])

    def test_update_branch_trees_fallback_detects_added_workflow_file(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        added = {"type": "blob", "path": ".github/workflows/ci.yml", "sha": "2" * 40}
        client, minter, handle, args = self.update_branch_trees(files, head_rows=[added])
        handle.run("wf_update_branch", args)
        self.assertEqual(len(minter.calls), 2)
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")
        self.assertEqual(client.calls[-1][0], "PUT")

    def test_update_branch_trees_fallback_detects_removed_workflow_file(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        removed = {"type": "blob", "path": ".github/workflows/old.yml", "sha": "3" * 40}
        client, minter, handle, args = self.update_branch_trees(files, base_rows=[removed])
        handle.run("wf_update_branch", args)
        self.assertEqual(len(minter.calls), 2)
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")

    def test_update_branch_trees_fallback_detects_changed_workflow_blob(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        head_row = {"type": "blob", "path": ".github/workflows/ci.yml", "sha": "2" * 40}
        base_row = {"type": "blob", "path": ".github/workflows/ci.yml", "sha": "3" * 40}
        client, minter, handle, args = self.update_branch_trees(
            files, head_rows=[head_row], base_rows=[base_row]
        )
        handle.run("wf_update_branch", args)
        self.assertEqual(len(minter.calls), 2)
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")

    def test_update_branch_trees_fallback_refuses_truncated_tree(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        client, minter, handle, args = self.update_branch_trees(files)
        client.replies[2] = {"truncated": True, "tree": []}
        with self.assertRaises(Refused):
            handle.run("wf_update_branch", args)
        self.assertNotIn("PUT", [c[0] for c in client.calls])
        self.assertEqual(len(minter.calls), 1)

    def test_update_branch_trees_fallback_refuses_missing_base_sha(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        client, minter, handle, args = self.update_branch_trees(files, base_sha="main")
        with self.assertRaises(Refused):
            handle.run("wf_update_branch", args)
        self.assertNotIn("PUT", [c[0] for c in client.calls])

    def test_update_branch_compare_without_files_falls_back_to_trees(self):
        minter = Minter()
        pr = {"head": {"sha": SHA}, "base": {"ref": "main", "sha": "b" * 40}}
        tree = {"truncated": False, "tree": [{"type": "blob", "path": "a.py", "sha": "1" * 40}]}
        client = Client(pr, {"total_commits": 5}, tree, tree, {"message": "ok"})
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        handle.run("wf_update_branch", args)
        self.assertEqual(client.calls[-1][0], "PUT")
        self.assertEqual(len(minter.calls), 1)

    def test_update_branch_300_files_with_workflow_still_elevates(self):
        files = [{"filename": f"src/f{i}.py"} for i in range(300)]
        files[5] = {"filename": ".github/workflows/ci.yml"}
        client, minter, handle, args = self.update_branch(files)
        handle.run("wf_update_branch", args)
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")

    def test_update_branch_slashed_base_ref_keeps_slash(self):
        client, _, handle, args = self.update_branch([{"filename": "a"}])
        handle.run("wf_update_branch", args)
        self.assertIn(f"/compare/{SHA}...release/2026%20nov", client.calls[1][1])

    def test_oversized_compare_is_a_nonretryable_refusal_not_a_503(self):
        from git_relay.github_client import RelayGitHubError
        from git_relay.server import PreReadUnavailable

        class Big(Client):
            def _call(self, method, path, *a, **k):
                self.calls.append((method, path))
                if "/compare/" in path or "/git/trees/" in path:
                    raise RelayGitHubError(0, "too large", code="response_too_large")
                return {"head": {"sha": SHA}, "base": {"ref": "main", "sha": "b" * 40}}

        minter, client = Minter(), Big()
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        with self.assertRaises(Refused) as ctx:
            handle.run("wf_update_branch", args)
        self.assertNotIsInstance(ctx.exception, PreReadUnavailable)
        self.assertEqual(str(ctx.exception), TOO_LARGE_TO_VERIFY)
        self.assertNotIn("PUT", [c[0] for c in client.calls])
        self.assertEqual(len(minter.calls), 1)

    def test_oversized_compare_falls_back_to_trees_and_elevates(self):
        from git_relay.github_client import RelayGitHubError

        class Big(Client):
            def _call(self, method, path, *a, **k):
                self.calls.append((method, path))
                if "/compare/" in path:
                    raise RelayGitHubError(0, "too large", code="response_too_large")
                return self.replies.pop(0) if self.replies else {}

        pr = {"head": {"sha": SHA}, "base": {"ref": "main", "sha": "b" * 40}}
        head_tree = {
            "truncated": False,
            "tree": [
                {"type": "blob", "path": "src/a.py", "sha": "1" * 40},
                {"type": "blob", "path": ".github/workflows/ci.yml", "sha": "2" * 40},
            ],
        }
        base_tree = {
            "truncated": False,
            "tree": [{"type": "blob", "path": "src/a.py", "sha": "1" * 40}],
        }
        minter, client = Minter(), Big(pr, head_tree, base_tree, {"message": "ok"})
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        handle.run("wf_update_branch", args)
        self.assertEqual(len([c for c in client.calls if "/git/trees/" in c[1]]), 2)
        self.assertEqual(len(minter.calls), 2)
        self.assertEqual(minter.calls[1][2].get("workflows"), "write")
        self.assertEqual(client.calls[-1][0], "PUT")

    def test_oversized_compare_trees_prove_no_workflow_touch(self):
        from git_relay.github_client import RelayGitHubError

        class Big(Client):
            def _call(self, method, path, *a, **k):
                self.calls.append((method, path))
                if "/compare/" in path:
                    raise RelayGitHubError(0, "too large", code="response_too_large")
                return self.replies.pop(0) if self.replies else {}

        pr = {"head": {"sha": SHA}, "base": {"ref": "main", "sha": "b" * 40}}
        tree = {"truncated": False, "tree": [{"type": "blob", "path": "a.py", "sha": "1" * 40}]}
        minter, client = Minter(), Big(pr, tree, tree, {"message": "ok"})
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        handle.run("wf_update_branch", args)
        self.assertEqual(client.calls[-1][0], "PUT")
        self.assertEqual(len(minter.calls), 1)
        self.assertNotIn("workflows", minter.calls[0][2])

    def test_transient_compare_failure_is_still_retryable_preread(self):
        from git_relay.github_client import RelayGitHubError
        from git_relay.server import PreReadUnavailable

        class Flaky(Client):
            def _call(self, method, path, *a, **k):
                self.calls.append((method, path))
                if "/compare/" in path:
                    raise RelayGitHubError(503, "down")
                return {"head": {"sha": SHA}, "base": {"ref": "main"}}

        client = Flaky()
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"}),
            Minter(),
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        with self.assertRaises(PreReadUnavailable):
            handle.run("wf_update_branch", args)
