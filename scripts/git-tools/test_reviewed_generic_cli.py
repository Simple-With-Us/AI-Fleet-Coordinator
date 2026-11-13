"""Exercise real CLI, real scanner, exact registry and ordinary write scope."""

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
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


class CliPublicIntegrityTests(unittest.TestCase):
    def test_normal_decoded_file_route_preserves_full_scan_and_message_gates(self):
        scanner = git._scanner()
        encoded_sri = "Ab1" * 10 + "C2" + "/" + "A" * 53 + "=="
        body = json.dumps({"integrity": "sha512-" + encoded_sri}) + "\n"
        repo, path = "example-org/example-repo", "app/package-lock.json"
        registry = frozenset({(repo, path, hashlib.sha256(body.encode()).hexdigest())})
        encoded = base64.b64encode(body.encode()).decode()
        common = {"repo": repo, "branch": "feature/x", "message": "fix"}
        single = dict(common, path=path, content_b64=encoded)
        multi = dict(common, files=[{"path": path, "content_b64": encoded}])
        for tool, args in (("gr_wf_file_put", single), ("gr_wf_files_put", multi)):
            with self.subTest(tool=tool):
                self.assertIsNotNone(git._scan_arguments(args, "", tool))
                with patch.object(scanner, "_REVIEWED_PUBLIC_INTEGRITIES", registry):
                    self.assertIsNone(git._scan_arguments(args, "", tool))
                    self.assertIsNotNone(git._scan_arguments(args, "Ab1", tool))
                    self.assertEqual(git._scan_arguments(dict(args, message=body), "", tool),
                                     "secret_scan:message")
                    self.assertIsNotNone(git._scan_arguments(dict(args, repo=repo + "2"), "", tool))
                    self.assertIsNotNone(git._scan_arguments(args, "", "gr_wf_issue_create"))
        with patch.object(scanner, "_REVIEWED_PUBLIC_INTEGRITIES", registry):
            for text in (body + " ", body + "gh" + "p_" + "a" * 40):
                changed = dict(single, content_b64=base64.b64encode(text.encode()).decode())
                self.assertIsNotNone(git._scan_arguments(changed, "", "gr_wf_file_put"))
            # A second unreviewed file cannot borrow the first file's admission.
            other = dict(multi, files=multi["files"] + [{"path": "other.json", "content_b64": encoded}])
            self.assertIsNotNone(git._scan_arguments(other, "", "gr_wf_files_put"))
