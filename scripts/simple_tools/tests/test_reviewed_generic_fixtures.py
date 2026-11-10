"""Exact full-content review boundary, generic assignments only."""

import hashlib
import unittest
from unittest.mock import patch

from simple_tools import secretscan


class ReviewedGenericTests(unittest.TestCase):
    def setUp(self):
        self.repo = "example-org/example-repo"
        self.path = "test/fixture.ts"
        self.body = "const token = '" + "a" * 24 + "';\n"

    def approve(self, body):
        return patch.object(secretscan, "_REVIEWED_GENERIC_FIXTURES", frozenset({
            (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
        }))

    def test_registry_contains_only_the_full_content_review(self):
        self.assertEqual(secretscan._REVIEWED_GENERIC_FIXTURES, frozenset({
            (
                "Simple-With-Us/CodeCaps",
                "ios/CodeCapsCompanion/Widgets/WidgetPresentation.swift",
                "d6bd7e7ff0a69af9cfd4002f1e21db90f66b87315a4dc69cbd2fc1c585be0b2b",
            ),
        }))
        self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, self.body))

    def test_approved_path_does_not_approve_other_bytes_or_quoted_credentials(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            self.body,
            "let token = Widget" + "Presentation.shortCadence(cadence: cadence, label: label)\n",
            "let token = '" + "b" * 32 + "'\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_only_exact_bytes_repo_path(self):
        with self.approve(self.body):
            self.assertIsNone(secretscan.scan_file(self.repo, self.path, self.body))
            self.assertIsNotNone(secretscan.scan(self.body))
            for repo, path, body in [(self.repo + "2", self.path, self.body),
                                     (self.repo, "other.ts", self.body),
                                     (self.repo, self.path, self.body + "\n")]:
                self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_dedicated_provider_loaded_values_absolute(self):
        for body, known in [(self.body + "gh" + "p_" + "a" * 40, []),
                            (self.body + "sk-" + "b" * 30, []),
                            (self.body, ["aaa"]),
                            (self.body + "Authorization: Bearer " + "c" * 30, [])]:
            with self.approve(body):
                self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, body, known))
