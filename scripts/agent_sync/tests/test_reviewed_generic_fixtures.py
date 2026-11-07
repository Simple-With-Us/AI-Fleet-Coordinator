"""Exact full-content review boundary, generic assignments only."""

import hashlib
import unittest
from unittest.mock import patch

from agent_sync import secretscan


class ReviewedGenericTests(unittest.TestCase):
    def setUp(self):
        self.repo = "example-org/example-repo"
        self.path = "test/fixture.ts"
        self.body = "const token = '" + "a" * 24 + "';\n"

    def approve(self, body):
        return patch.object(secretscan, "_REVIEWED_GENERIC_FIXTURES", frozenset({
            (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
        }))

    def test_release_registry_only_has_reviewed_exact_botfleet_entry(self):
        self.assertEqual(secretscan._REVIEWED_GENERIC_FIXTURES, frozenset({
            ("Simple-With-Us/BotFleet", "server/config.test.ts",
             "466868a395827bb284e392b277dff7e0f16adad66fe68306a5dfc22fff28bf19")
        }))
        self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, self.body))

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
