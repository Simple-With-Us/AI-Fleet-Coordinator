"""Exercise real CLI, real scanner, exact registry and ordinary write scope."""

import base64
import hashlib
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader(
    "fixture_git_cli", str(ROOT / "scripts/git-tools/git")
)
spec = importlib.util.spec_from_loader("fixture_git_cli", loader)
git = importlib.util.module_from_spec(spec)
loader.exec_module(git)


class CliFixtureTests(unittest.TestCase):
    def test_only_scoped_file_content_can_use_exact_review(self):
        scanner = git._scanner()
        body = "const token = '" + "a" * 24 + "';\n"
        repo, path = "example-org/example-repo", "tests/example.ts"
        encoded = base64.b64encode(body.encode()).decode()
        registry = frozenset({(repo, path, hashlib.sha256(body.encode()).hexdigest())})
        args = {
            "repo": repo,
            "branch": "feature/x",
            "message": "fix",
            "files": [{"path": path, "content_b64": encoded}],
        }
        with patch.object(scanner, "_REVIEWED_GENERIC_FIXTURES", registry):
            self.assertIsNone(git._scan_arguments(args, "", "gr_wf_files_put"))
            self.assertIsNotNone(git._scan_arguments(args, "", "gr_wf_issue_create"))
            self.assertIsNotNone(git._scan_arguments(args, ""))
            args["message"] = body
            self.assertEqual(
                git._scan_arguments(args, "", "gr_wf_files_put"), "secret_scan:message"
            )
            args["message"] = "fix"
            args["repo"] = repo + "2"
            self.assertIsNotNone(git._scan_arguments(args, "", "gr_wf_files_put"))
            args["repo"] = repo
            self.assertIsNotNone(git._scan_arguments(args, "aaa", "gr_wf_files_put"))
