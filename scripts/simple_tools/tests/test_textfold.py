"""Parity with scripts/simple-tools-mcp/src/contract.js fold() (AFC-729 / AFC-758)."""

from __future__ import annotations

import unittest

from simple_tools import textfold as TF
from simple_tools.mcp import fleetlink_tools as FL
from simple_tools.mcp import recall_tools as RT
from simple_tools.mcp import tools as T
from simple_tools import live as L

NBSP = "\u00a0"
NARROW = "\u202f"
GAP = f"{NBSP} "


class FoldTests(unittest.TestCase):
    def test_space_separators_are_not_nfkc_mapped(self) -> None:
        for ch in (
            "\u00a0",
            "\u1680",
            "\u2003",
            "\u2007",
            "\u202f",
            "\u205f",
            "\u3000",
        ):
            with self.subTest(ch=ch):
                self.assertEqual(TF.fold(f"A{ch}B"), f"A{ch}B")

    def test_fullwidth_compatibility_still_folds(self) -> None:
        self.assertEqual(TF.fold("\uff22\uff25\uff27\uff29\uff2e"), "BEGIN")
        self.assertEqual(TF.fold("\ufb01le"), "file")

    def test_format_characters_still_drop(self) -> None:
        self.assertEqual(TF.fold("A\u200bB\u202eC"), "ABC")


class ReadPathTests(unittest.TestCase):
    def test_mcp_clean_keeps_nbsp_and_strips_marker(self) -> None:
        body, _ = T.clean(f"Hello.{GAP}Next")
        self.assertEqual(body, f"Hello.{GAP}Next")
        removed, _ = T.clean(f"BEGIN{NBSP}UNTRUSTED{NBSP}ZULIP")
        self.assertEqual(removed, "[marker removed]")
        kept, _ = T.clean(f"BEG{NBSP}INUNTRUSTEDZULIP")
        self.assertEqual(kept, f"BEG{NBSP}INUNTRUSTEDZULIP")
        self.assertEqual(T.clean("BEGIN\u200bUNTRUSTED\u200bZULIP")[0], "[marker removed]")

    def test_live_escape_body_keeps_nbsp(self) -> None:
        text = L.escape_body(f"Stored.{GAP}Ok.")
        self.assertIn(NBSP, text)
        self.assertNotIn("Stored. Ok.", text.replace(GAP, ". "))

    def test_recall_clean_keeps_narrow_no_break_space(self) -> None:
        body, _ = RT.clean(f"Hit.{NARROW}Text")
        self.assertIn(NARROW, body)

    def test_fleetlink_one_line_keeps_nbsp(self) -> None:
        line = FL.one_line(f"Err.{GAP}Next\nline")
        self.assertEqual(line, f"Err.{GAP}Next line")
