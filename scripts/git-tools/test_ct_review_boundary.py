"""Synthetic policy invariants, never the frozen CT file or its value."""
import base64
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

cli = load("ct_boundary_cli", ROOT / "scripts/git-tools/git")
relay = load("ct_boundary_relay", ROOT / "git-relay/git_relay/patch_secretscan.py")

class CtReviewBoundary(unittest.TestCase):
    repo = "Simple-With-Us/Congress.Trade"
    path = "app/src/security/__tests__/originLock.test.ts"
    digest = "cc1f0a0d5565309e9268ecd4e37d15d09c5c2e85f4de3cd54638be87bee555c5"

    def test_exact_source_mirrors_and_only_expected_registry_delta(self):
        self.assertEqual((ROOT / "scripts/simple_tools/secretscan.py").read_bytes(),
                         (ROOT / "git-relay/git_relay/patch_secretscan.py").read_bytes())
        self.assertIn((self.repo, self.path, self.digest), relay._REVIEWED_GENERIC_FIXTURES)
        self.assertEqual(len(relay._REVIEWED_GENERIC_FIXTURES), 2)
        self.assertFalse(any(row[0] == "Simple-With-Us/Congress-Trade"
                             for row in relay._REVIEWED_GENERIC_FIXTURES))

    def test_actual_registry_does_not_admit_unreviewed_synthetic_bytes(self):
        body = "const secret = '" + "a" * 24 + "';"
        self.assertIsNotNone(relay.scan_file(self.repo, self.path, body))

    def test_synthetic_exact_scope_and_absolute_guards(self):
        # Temporary synthetic registry replaces release code only in test memory.
        # This does not claim acceptance of the actual CT content.
        for scanner in (cli._scanner(), relay):
            for suffix, known in [("", []), (" gh" + "p_" + "b" * 40, []),
                                  (" Aa1" + "b" * 29, []), ("", ["aaa"])]:
                body = "const secret = '" + "a" * 24 + "';" + suffix
                reg = frozenset({(self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())})
                with patch.object(scanner, "_REVIEWED_GENERIC_FIXTURES", reg):
                    if suffix or known:
                        self.assertIsNotNone(scanner.scan_file(self.repo, self.path, body, known))
                    else:
                        self.assertIsNone(scanner.scan_file(self.repo, self.path, body))
                    self.assertIsNotNone(scanner.scan(body))
                    for repo, path, text in [("Simple-With-Us/Congress-Trade", self.path, body),
                                             (self.repo, self.path + ".other", body),
                                             (self.repo, self.path, body + "\n")]:
                        self.assertIsNotNone(scanner.scan_file(repo, path, text))

    def test_synthetic_cli_context_not_prose_or_unrelated_verb(self):
        body = "const secret = '" + "a" * 24 + "';"
        args = {"repo": self.repo, "files": [{"path": self.path,
                "content_b64": base64.b64encode(body.encode()).decode()}]}
        registry = frozenset({(self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())})
        with patch.object(cli._scanner(), "_REVIEWED_GENERIC_FIXTURES", registry):
            self.assertIsNone(cli._scan_arguments(args, "", "gr_wf_files_put"))
            self.assertIsNotNone(cli._scan_arguments(args, "", "gr_wf_issue_create"))
            args["message"] = body
            self.assertIsNotNone(cli._scan_arguments(args, "", "gr_wf_files_put"))
