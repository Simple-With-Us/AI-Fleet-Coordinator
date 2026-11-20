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
            (
                "Simple-With-Us/CodeCaps",
                "ios/CodeCapsCompanion/App/Models/CompanionQuotaModel.swift",
                "1e44f5721cf37b067a3a1a2091fc316d2a9f95546effc22d756126eca687894b",
            ),
            (
                "Simple-With-Us/Usage-Monitor",
                "scripts/lib/run-session-token-collector.mjs",
                "e96f73542ff0a5923af4d53981f96693bb67f80444e54cf94a520737ac5418f4",
            ),
            (
                "Simple-With-Us/AI-Fleet-Coordinator",
                "scripts/linear-board/test_board.py",
                "9e96737087b55d3ba6f82f7fb8e5e0a5b17828d10f2759e73dae65063211cfe3",
            ),
            (
                "Simple-With-Us/AI-Fleet-Coordinator",
                "git-relay/git_relay/server.py",
                "82b53da38ce0a110a3f25b947fbcabcfcef031a8295795d72f46f48d55d79305",
            ),
        }))
        self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, self.body))

    def test_approved_path_does_not_approve_other_bytes_or_quoted_credentials(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            self.body,
            "let token = '" + "b" * 32 + "'\n",
            "let token = Widget" + "Presentation.shortCadence9(cadence: cadence)\n",
            "let token = widget" + "presentation.shortcadence(cadence: cadence)\n",
            "let token = Correct" + ".Horse.Battery\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_letters_only_dotted_calls_are_not_secrets(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        self.assertIsNone(secretscan.scan_file(
            repo, path,
            "let token = Widget" + "Presentation.shortCadence(cadence: cadence, label: label)\n"))

    def test_labeled_call_argument_chains_are_not_secrets(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "let s = Reset" + "AlarmCadence.periodSeconds(token: snapshot.wind" + "ow.window, label: snapshot.wind" + "ow.label)\n",
            "foo(token: config.api" + "Key, label: label)\n",
        ):
            self.assertIsNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_form_still_refuses_nonsource_values(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "token: snapshot.window" + ".window\n",
            "foo(token: \"snapshot.window" + ".window\")\n",
            "let token = snapshot.window" + ".window\n",
            "foo(token: " + "a" * 24 + ")\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_multiline_and_second_position(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "foo(\n    token: snapshot.wind" + "ow.window,\n    label: label)\n",
            "foo(a: 1, token: snapshot.wind" + "ow.window)\n",
        ):
            self.assertIsNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_requires_call_paren(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "Hello, token: snapshot.wind" + "ow.window\n",
            "(done), token: snapshot.wind" + "ow.window\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_earlier_string_arg_and_closure(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "foo(label: \"x\", token: snapshot.wind" + "ow.window)\n",
            "foo({ bar in\n    baz(x) }, token: snapshot.wind" + "ow.window)\n",
        ):
            self.assertIsNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_balanced_call_before_comma_refuses(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        body = "bar(x), token: snapshot.wind" + "ow.window\n"
        self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_requires_colon_separator(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        body = "foo(token=snapshot.wind" + "ow.window)\n"
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
