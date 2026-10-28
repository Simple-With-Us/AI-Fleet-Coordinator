"""The App Store sheet's sentence-gap rule must agree with the canonical rule.

The rules tab of the Fleet App Store + TestFlight sheet is generated from
``scripts/ios-fleet/sheet-rules.json``, and that file carries CPY-1, the
fleet's sentence-gap rule as it applies to product copy.  Nothing else checks
it, so a rule edit there can drift away from the one home of the rule without
anything failing.

The requirements are read from the canonical skill, not copied out of the sheet
rule, so this gate checks the sheet rule against the one home of the rule
rather than against a second copy of the rule.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = ROOT / "docs" / "fleet-skills" / "sentence-gap" / "SKILL.md"
SHEET_RULES = ROOT / "scripts" / "ios-fleet" / "sheet-rules.json"

RULE_ID = "CPY-1"

# The character the mechanism names, as the skill writes it:  "U+00A0".
CODEPOINT_RE = re.compile(r"U\+00[0-9A-Fa-f]{2}")


def _canonical() -> str:
    return CANONICAL_SKILL.read_text(encoding="utf-8")


def _rule() -> dict:
    data = json.loads(SHEET_RULES.read_text(encoding="utf-8"))
    for section in data["sections"]:
        for rule in section["rules"]:
            if rule["id"] == RULE_ID:
                return rule
    raise AssertionError(f"{RULE_ID} is not in {SHEET_RULES}")


def _sentences(text: str) -> list[str]:
    return text.replace("\u00a0", " ").split(". ")


def _canonical_mechanism_codepoint() -> str:
    """The character the one home of the rule names as the mechanism."""
    for sentence in _sentences(_canonical()):
        if "Mechanism" in sentence and "normal space" in sentence:
            found = CODEPOINT_RE.search(sentence)
            if found:
                return found.group(0)
    raise AssertionError("the canonical skill states no mechanism sentence")


def _sheet_mechanism_sentence() -> str:
    """The sentence of the sheet rule that states the mechanism."""
    for sentence in _sentences(_rule()["detail"]):
        if "followed by a normal space" in sentence:
            return sentence
    raise AssertionError("the sheet rule states no mechanism sentence")


class SheetRuleSentenceGapTests(unittest.TestCase):
    """CPY-1 and the canonical sentence-gap skill say the same thing."""

    def test_the_rule_is_there(self) -> None:
        rule = _rule()
        self.assertTrue(rule["rule"].strip())
        self.assertTrue(rule["detail"].strip())

    def test_the_mechanism_is_the_one_the_canonical_skill_states(self) -> None:
        codepoint = _canonical_mechanism_codepoint()
        sentence = _sheet_mechanism_sentence()
        self.assertIn(
            codepoint,
            sentence,
            f"the sheet rule must state the mechanism the canonical skill states ({codepoint})",
        )
        self.assertIn("normal space", sentence)

    def test_the_two_chat_pane_exceptions_are_named(self) -> None:
        canonical = _canonical()
        detail = _rule()["detail"]
        for pane in ("Claude Code", "MiniMax Code"):
            self.assertIn(pane, canonical, "the canonical skill changed shape")
            self.assertIn(pane, detail)
        self.assertIn("&nbsp;", detail)

    def test_the_apple_notes_exception_is_named(self) -> None:
        canonical = _canonical()
        detail = _rule()["detail"]
        self.assertIn("&#8239;", canonical, "the canonical skill changed shape")
        self.assertIn("&#8239;", detail)
        self.assertIn("Apple Note", detail)

    def test_the_chat_panes_are_not_listed_as_taking_the_real_character(self) -> None:
        listing = [s for s in _sentences(_rule()["detail"]) if "alike" in s]
        self.assertTrue(listing, "the surface list is gone; update this gate")
        for sentence in listing:
            self.assertNotIn("chat pane", sentence)

    def test_the_entity_is_refused_outside_the_two_panes(self) -> None:
        detail = _rule()["detail"]
        refusing = [s for s in _sentences(detail) if "entity" in s and "Never" in s]
        self.assertTrue(refusing, "the entity refusal is gone; update this gate")
        for sentence in refusing:
            self.assertIn("outside the two chat panes", sentence)


if __name__ == "__main__":
    unittest.main()
