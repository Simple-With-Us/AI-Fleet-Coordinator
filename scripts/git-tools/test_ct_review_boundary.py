"""Synthetic policy invariants, never the frozen CT file or its value."""

import base64
import hashlib
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
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
    repo = "Fixture-Repo/Fixture-Congress.Trade"
    path = "tests/fixture-origin-lock.test.ts"
    digest = "cc1f0a0d5565309e9268ecd4e37d15d09c5c2e85f4de3cd54638be87bee555c5"

    def test_exact_source_mirrors_and_only_expected_registry_delta(self):
        self.assertEqual(
            (ROOT / "scripts/simple_tools/secretscan.py").read_bytes(),
            (ROOT / "git-relay/git_relay/patch_secretscan.py").read_bytes(),
        )
        self.assertEqual(sum(row[2] == self.digest for row in relay._REVIEWED_GENERIC_MATCHES), 1)
        self.assertEqual(len(relay._REVIEWED_GENERIC_FIXTURES), 1)
        self.assertFalse(
            any(
                row[0] == "Fixture-Repo/Fixture-Congress-Trade"
                for row in relay._REVIEWED_GENERIC_FIXTURES
            )
        )

    def test_actual_registry_does_not_admit_unreviewed_synthetic_bytes(self):
        body = "const secret = '" + "a" * 24 + "';"
        self.assertIsNotNone(relay.scan_file(self.repo, self.path, body))

    def test_synthetic_exact_scope_and_absolute_guards(self):
        # Temporary synthetic registry replaces release code only in test memory.
        # This does not claim acceptance of the actual CT content.
        for scanner in (cli._scanner(), relay):
            for suffix, known in [
                ("", []),
                (" gh" + "p_" + "b" * 40, []),
                (" Aa1" + "b" * 29, []),
                ("", ["aaa"]),
            ]:
                body = "const secret = '" + "a" * 24 + "';" + suffix
                reg = frozenset({(self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())})
                with patch.object(scanner, "_REVIEWED_GENERIC_FIXTURES", reg):
                    if suffix or known:
                        self.assertIsNotNone(scanner.scan_file(self.repo, self.path, body, known))
                    else:
                        self.assertIsNone(scanner.scan_file(self.repo, self.path, body))
                    self.assertIsNotNone(scanner.scan(body))
                    for repo, path, text in [
                        ("Fixture-Repo/Fixture-Congress-Trade", self.path, body),
                        (self.repo, self.path + ".other", body),
                        (self.repo, self.path, body + "\n"),
                    ]:
                        self.assertIsNotNone(scanner.scan_file(repo, path, text))

    def test_synthetic_cli_context_not_prose_or_unrelated_verb(self):
        body = "const secret = '" + "a" * 24 + "';"
        args = {
            "repo": self.repo,
            "files": [{"path": self.path, "content_b64": base64.b64encode(body.encode()).decode()}],
        }
        registry = frozenset({(self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())})
        with patch.object(cli._scanner(), "_REVIEWED_GENERIC_FIXTURES", registry):
            self.assertIsNone(cli._scan_arguments(args, "", "gr_wf_files_put"))
            self.assertIsNotNone(cli._scan_arguments(args, "", "gr_wf_issue_create"))
            args["message"] = body
            self.assertIsNotNone(cli._scan_arguments(args, "", "gr_wf_files_put"))


class MatchPinnedReview(unittest.TestCase):
    repo, path = "fixture-org/fixture-repo", "tests/fixture.ts"

    def metadata(self, scanner, body):
        return frozenset(
            (label, *m.span(), *m.span("value"))
            for label, pattern in scanner._PATTERNS
            if label in scanner._GENERIC_ASSIGNMENTS
            for m in pattern.finditer(body)
            if not scanner._source_assignment(body, m)
        )

    def test_only_exact_match_census_can_be_excluded(self):
        for scanner in (cli._scanner(), relay):
            body = "const secret = '" + "a" * 24 + "';"
            reviewed = self.metadata(scanner, body)
            self.assertEqual(len(reviewed), 1)
            for text, metadata, accepted in [
                (body, reviewed, True),
                (body, frozenset(), False),
                (body, frozenset({("assigned secret", 0, 1, 0, 1)}), False),
                (body + " const token = '" + "b" * 24 + "';", reviewed, False),
                ("//shift\n" + body, reviewed, False),
                (body.replace("secret", "password"), reviewed, False),
            ]:
                # Deliberately update digest but NOT pinned match metadata.
                key = (self.repo, self.path, hashlib.sha256(text.encode()).hexdigest())
                with patch.object(scanner, "_REVIEWED_GENERIC_MATCHES", {key: metadata}):
                    hit = scanner.scan_file(self.repo, self.path, text)
                    if accepted:
                        self.assertIsNone(hit)
                    else:
                        self.assertIsNotNone(hit)

    def test_exact_repo_path_digest_and_absolute_dedicated_loaded_guards(self):
        for scanner in (cli._scanner(), relay):
            base = "const secret = '" + "a" * 24 + "';"
            for suffix, known in [
                ("", []),
                (" gh" + "p_" + "c" * 40, []),
                (" Aa1" + "b" * 29, []),
                ("", ["aaa"]),
            ]:
                body = base + suffix
                key = (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
                with patch.object(
                    scanner, "_REVIEWED_GENERIC_MATCHES", {key: self.metadata(scanner, base)}
                ):
                    hit = scanner.scan_file(self.repo, self.path, body, known)
                    if suffix or known:
                        self.assertIsNotNone(hit)
                    else:
                        self.assertIsNone(hit)
                    for repo, path, text in [
                        (self.repo + "2", self.path, body),
                        (self.repo, self.path + "2", body),
                        (self.repo, self.path, body + "\n"),
                    ]:
                        self.assertIsNotNone(scanner.scan_file(repo, path, text))
                    self.assertIsNotNone(scanner.scan(body))

    def test_match_pinned_cli_scope_and_empty_overlap_fail_closed(self):
        scanner = cli._scanner()
        body = "const secret = '" + "a" * 24 + "';"
        key = (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
        args = {
            "repo": self.repo,
            "files": [{"path": self.path, "content_b64": base64.b64encode(body.encode()).decode()}],
        }
        with patch.object(
            scanner, "_REVIEWED_GENERIC_MATCHES", {key: self.metadata(scanner, body)}
        ):
            self.assertIsNone(cli._scan_arguments(args, "", "gr_wf_files_put"))
            self.assertIsNotNone(cli._scan_arguments(args, "aaa", "gr_wf_files_put"))
            self.assertIsNotNone(cli._scan_arguments(args, "", "gr_wf_issue_create"))
            args["message"] = body
            self.assertIsNotNone(cli._scan_arguments(args, "", "gr_wf_files_put"))
        # A malformed match record must not fall back to a broad legacy entry.
        with patch.object(scanner, "_REVIEWED_GENERIC_MATCHES", {key: frozenset()}):
            with patch.object(scanner, "_REVIEWED_GENERIC_FIXTURES", frozenset({key})):
                self.assertIsNotNone(scanner.scan_file(self.repo, self.path, body))
