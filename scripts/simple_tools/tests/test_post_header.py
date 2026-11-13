"""Byte-level header serialization and legacy routing compatibility for OPS-1086."""

import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest import mock

from simple_tools import cli, post_header, router, wake_router

FIXTURES = json.loads(Path(__file__).with_name("header-spacing.json").read_text())


class HeaderSpacingTests(unittest.TestCase):
    def test_shared_golden_headers_survive_json_utf8_and_legacy_parsing(self):
        for case in FIXTURES:
            with self.subTest(case=case["name"]):
                result = post_header.normalize_header(case["input"], seat=case.get("seat"))
                self.assertEqual(result, case["expected"])
                self.assertEqual(post_header.normalize_header(result, seat=case.get("seat")), result)
                wire = json.dumps({"content": result}, ensure_ascii=False).encode("utf-8")
                self.assertEqual(json.loads(wire)["content"], case["expected"])
                if "repos" in case:
                    for header in (case["input"], result):
                        # The legacy transport fragment is not part of compact-header syntax.
                        if "·" in header:
                            continue
                        parsed = router.parse_leading_compact_intro(header)
                        self.assertIsNotNone(parsed)
                        self.assertEqual([r.strip() for r in parsed["repos"].split(",")], case["repos"])
                        self.assertEqual(parsed["peers"], case["peers"])
                    inner = result[result.index("[") + 1:result.index("]")]
                    self.assertTrue(wake_router._is_post_header(inner))

    def test_actual_native_post_has_exact_counts_and_no_runtime_suffix(self):
        agent = SimpleNamespace(seat="CODEX", tag_prefix="[CODEX·deadbeef")
        body = cli._compose(agent, "[CODEX: EE.1.S 📁AFC, OPS → CODEX EE] Body.\u00a0 Next.",
                            labels=["AG"], mentions=[], no_tag=False)
        expected = FIXTURES[0]["expected"]
        self.assertEqual(body, expected)
        self.assertEqual(re.search(r":(\u200a+)EE", body).group(1), "\u200a" * 3)
        self.assertEqual(re.search(r"S(\u200a+)📁", body).group(1), "\u200a" * 2)
        self.assertEqual(re.search(r"📁(\u200a+)AFC", body).group(1), "\u200a")
        self.assertEqual(re.search(r"AFC,(\u200a+)OPS", body).group(1), "\u200a")
        self.assertEqual(re.search(r"CODEX EE,(\u200a+)AG", body).group(1), "\u200a" * 2)
        self.assertNotIn("deadbeef", body)
        self.assertIn(" → CODEX EE,", body)
        self.assertTrue(body.endswith("Body.\u00a0 Next."))
        legacy = cli._compose(agent, "[CODEX: EE.1.S 📁AFC, OPS -> CODEX EE] Body.\u00a0 Next.",
                              labels=["AG"], mentions=[], no_tag=False)
        self.assertEqual(legacy, expected)

    def test_native_limit_applies_after_header_serialization(self):
        text = "[CODEX: EE.1.S 📁AFC] body"
        agent = SimpleNamespace(seat="CODEX", tag_prefix="[CODEX")
        with mock.patch.object(cli, "MAX_CONTENT_LENGTH", len(text)):
            with self.assertRaises(cli.UsageError):
                cli._compose(agent, text, labels=[], mentions=[], no_tag=False)

    def test_quiet_native_posts_and_edits_keep_canonical_header_bytes(self):
        agent = SimpleNamespace(seat="CODEX", tag_prefix="[CODEX·deadbeef")
        expected = router.NO_WAKE_MARKER + " " + FIXTURES[0]["expected"]
        for prefix in ("", router.NO_WAKE_MARKER + " "):
            with self.subTest(prefix=prefix):
                body = cli._compose(agent, prefix + FIXTURES[0]["input"], labels=[],
                                    mentions=[], no_tag=False, quiet=True)
                self.assertEqual(body, expected)
