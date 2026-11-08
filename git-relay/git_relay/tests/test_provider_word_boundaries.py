"""Synthetic shape tests for provider words, never live credentials."""

import unittest

from git_relay.provenance_patch import apply_patch
from git_relay.secret_scan import find_secret
from git_relay.workflow_verbs import WorkflowVerbs


class ProviderWords(unittest.TestCase):
    def test_ordinary_longer_words_and_filenames_do_not_match(self):
        for value in [
            "scripts/test-disk-janitor-cleanmymac-safety.sh",
            "mac-disk-reclaim-policy-current",
            "risk-bunchofotherthings",
            "disk-admin-guide-for-the-server",
            "task-ant-guide-for-the-server",
            "docs/disk-proj-ordinary-guide-for-the-server.md",
            "docs/task-svcacct-ordinary-guide-for-the-server.txt",
            "disk-" + "ordinaryfilename" * 5,
            "risk-" + "ordinaryfilename" * 5,
        ]:
            with self.subTest(value=value):
                self.assertFalse(WorkflowVerbs._provider_token_text(value))

    def test_boundary_bare_provider_inputs_still_match(self):
        token = "sk-" + "a" * 32
        for value in [
            token,
            '"' + token + '"',
            "API_KEY=" + token,
            "token: " + token,
            "/" + token,
            "(" + token + ")",
            "Authorization: Bearer " + token,
        ]:
            self.assertTrue(WorkflowVerbs._provider_token_text(value))

    def test_complete_supported_formats_detected_with_unseparated_prefixes(self):
        tokens = ["sk_" + scope + "_" + "a" * 32 for scope in ["live", "test"]]
        tokens += ["sk-ant-" + kind + "-" + "a" * 93 + "AA" for kind in ["api03", "admin01"]]
        tokens += [
            "sk-" + kind + "-" + "a" * left + "T3BlbkFJ" + "b" * right
            for kind in ["proj", "svcacct", "admin"]
            for left in [58, 74]
            for right in [58, 74]
        ]
        for token in tokens:
            for context in ["", "prefix", "value=", "Bearer ", "https://invalid/"]:
                with self.subTest(token_prefix=token[:20], context=context):
                    self.assertTrue(WorkflowVerbs._provider_token_text(context + token))

    def test_ambiguous_scoped_heuristic_requires_boundary_or_credential_context(self):
        for prefix in ["sk-ant-", "sk-proj-", "sk-svcacct-", "sk-admin-"]:
            token = prefix + "a" * 32
            for context in ["", "/", "(", "API_KEY=prefix", "token: prefix", '{"SECRET": "prefix']:
                with self.subTest(prefix=prefix, context=context):
                    self.assertTrue(WorkflowVerbs._provider_token_text(context + token))
            self.assertFalse(WorkflowVerbs._provider_token_text("prefix" + token))
            self.assertIsNotNone(find_secret("Authorization: Bearer prefix" + token))
            raw = ("docs/prefix" + token + ".txt\n").encode()
            with self.assertRaises(ValueError):
                apply_patch(raw, [dict(start=len(raw), end=len(raw), text="safe\n")], known=[token])

    def test_complete_legacy_shape_detected_with_unseparated_prefix(self):
        token = "sk-" + "a" * 20 + "T3BlbkFJ" + "b" * 20
        self.assertTrue(WorkflowVerbs._provider_token_text("prefix" + token))

    def test_bearer_layer_covers_ambiguous_embedded_bare_shape(self):
        token = "prefix" + "sk-" + "a" * 32
        self.assertIsNotNone(find_secret("Authorization: Bearer " + token))
        self.assertFalse(WorkflowVerbs._provider_token_text(token))

    def test_assignment_context_catches_unseparated_ambiguous_values(self):
        value = "prefix" + "sk-" + "a" * 32
        for name in ["API_KEY", "OPENAI_API_KEY", "token", "SECRET", "password"]:
            for separator in ["=", ": ", " = "]:
                for quote in ["", "'", '"']:
                    with self.subTest(name=name, separator=separator, quote=quote):
                        self.assertTrue(
                            WorkflowVerbs._provider_token_text(name + separator + quote + value)
                        )
        self.assertFalse(WorkflowVerbs._provider_token_text("filename=" + value))

    def test_existing_loaded_value_guard_ignores_lexical_context(self):
        value = "embedded" + "sk-" + "a" * 32
        self.assertFalse(WorkflowVerbs._provider_token_text(value))
        raw = ("docs/" + value + ".txt\n").encode()
        with self.assertRaises(ValueError):
            apply_patch(raw, [dict(start=len(raw), end=len(raw), text="safe\n")], known=[value])

    def test_quoted_json_assignment_with_embedded_legacy_value(self):
        value = "prefix" + "sk-" + "a" * 32
        self.assertTrue(WorkflowVerbs._provider_token_text('{"API_KEY": "' + value + '"}'))
