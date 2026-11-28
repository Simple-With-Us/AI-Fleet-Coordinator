"""Both folder glyphs parse everywhere; writers emit the open folder (v4.15).

The fleet guide moved the post intro from closed-folder to open-folder, with
the folder segment optional.  Readers accept both glyphs so older posts keep
working; writers emit the open folder.  Bare `[SEAT arrow peer]` intros (no
folder at all) route recipients through the same paths they always used.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

from simple_tools import post_header, router, wake_router, zulip_cli

OLD = "📁"
NEW = "📂"


def _load_hyphenated(name: str):
    path = Path(__file__).resolve().parents[2] / name
    modname = name.replace("/", "_").replace("-", "_").removesuffix(".py")
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


class FolderGlyphDualityTests(unittest.TestCase):
    def test_normalize_emits_new_glyph_for_both_inputs(self):
        for glyph in (OLD, NEW):
            with self.subTest(glyph=glyph):
                body = post_header.normalize_header(f"[MM: HH.1.M {glyph}AFC, OPS] Body")
                self.assertIn(NEW, body)
                self.assertNotIn(OLD, body)
                self.assertEqual(post_header.normalize_header(body), body)

    def test_normalize_keeps_bare_arrow_recipients(self):
        body = post_header.normalize_header("[MM: HH.1.M → CODEX] Body")
        self.assertIn("→ CODEX", body)
        self.assertNotIn(OLD, body)
        self.assertNotIn(NEW, body)

    def test_router_parses_both_glyphs(self):
        for glyph in (OLD, NEW):
            with self.subTest(glyph=glyph):
                parsed = router.parse_leading_compact_intro(f"[MM: HH.1.M {glyph}AFC, OPS → CODEX] Body")
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["seat"], "MM")
                self.assertEqual([r.strip() for r in parsed["repos"].split(",")], ["AFC", "OPS"])
                self.assertEqual(parsed["peers"], ["CODEX"])

    def test_router_peers_survive_bare_arrow_intro(self):
        self.assertEqual(router.header_peers("[MM: HH.1.M → CODEX, CURSOR] Body"), ["CODEX", "CURSOR"])

    def test_wake_router_accepts_either_glyph(self):
        self.assertTrue(wake_router._is_post_header(f"MM  {NEW}AFC"))
        self.assertTrue(wake_router._is_post_header(f"MM  {OLD}AFC"))

    def test_outbox_bridge_reads_both_glyphs(self):
        bridge = _load_hyphenated("github-outbox-bridge.py")
        for glyph in (OLD, NEW):
            with self.subTest(glyph=glyph):
                intro = f"[MC: LL.1.S {glyph}AFC] Body"
                self.assertIsNotNone(bridge._own_intro_re("MC").match(intro))
                self.assertEqual(bridge._folder_repo_codes(intro), ["AFC"])
        self.assertIsNone(bridge._folder_repo_codes("[MC → CODEX] Body"))

    def test_board_opens_with_either_glyph(self):
        board = _load_hyphenated("linear-board/board")
        for glyph in (OLD, NEW):
            with self.subTest(glyph=glyph):
                self.assertTrue(board.opens_with_own_intro("MC", f"[MC: LL.1.S {glyph}AFC] Body"))
        self.assertTrue(board.opens_with_own_intro("MC", "[MC → CODEX] Body"))
        self.assertFalse(board.opens_with_own_intro("MC", "[MM: HH.1.M 📂AFC] Body"))

    def test_digest_envelope_matches_both_glyphs_and_bare_arrows(self):
        digest = _load_hyphenated("build-fleet-daily-digest.py")
        for text in ("[MM: HH.1.M 📂AFC] did x", "[MM: HH.1.M 📁AFC] did x", "[MM → CODEX] did x"):
            with self.subTest(text=text):
                match = digest._AGENT_ENVELOPE.search(text)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1).upper(), "MM")

    def test_opening_tag_seat_reads_either_glyph(self):
        for glyph in (OLD, NEW):
            with self.subTest(glyph=glyph):
                self.assertEqual(zulip_cli._opening_tag_seat(f"MC {glyph}AFC"), ("MC", False))
                self.assertEqual(
                    zulip_cli._opening_tag_seat(f"MC: LL.1.S {glyph}AFC"), ("MC", False)
                )


if __name__ == "__main__":
    unittest.main()
