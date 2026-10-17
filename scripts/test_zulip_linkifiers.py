"""Tests for zulip_linkifiers.py:  the plan diff, and that TARGET's patterns link what CHECKS expect.

Zulip runs re2 and wraps each pattern so a match starts after whitespace or `'"(,:<` (or at the
start) and is not followed by a word character;  `linkify` mimics that wrapper and realm order with
Python's `re`, which agrees with re2 on these patterns (no backreferences or lookarounds inside).
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zulip_linkifiers as L


def expand(template: str, groups: dict) -> str:
    return re.sub(r"\{\+?(\w+)\}", lambda m: groups[m.group(1)], template)


def linkify(text: str) -> str | None:
    """The href of the first TARGET rule that matches `text`, in realm order after `order`, or None."""
    rules = [r for r in L.TARGET if r[0] != L.GENERIC_ID] + [
        r for r in L.TARGET if r[0] == L.GENERIC_ID
    ]
    for _, pattern, url in rules:
        match = re.search(r"(?:^|(?<=[\s'\"(,:<]))(?:" + pattern + r")(?!\w)", text)
        if match:
            return expand(url, match.groupdict())
    return None


class TargetTest(unittest.TestCase):
    def test_checks_render_as_expected(self):
        for text, href in L.CHECKS:
            with self.subTest(text=text):
                self.assertEqual(linkify(text), href)

    def test_no_false_links(self):
        for text in ("UTF-8", "SHA-256", "C-3PO", "fleetlink/", "see CC-12x", "bc-12"):
            with self.subTest(text=text):
                self.assertIsNone(linkify(text))

    def test_linear_rule_does_not_claim_sentry_ids(self):
        for project in L.SENTRY_PROJECTS:
            with self.subTest(project=project):
                self.assertIn("sentry.io", linkify(f"{project}-1A"))

    def test_existing_ids_are_unique(self):
        ids = [rule_id for rule_id, _, _ in L.TARGET if rule_id is not None]
        self.assertEqual(len(ids), len(set(ids)))


class OrderTest(unittest.TestCase):
    def test_generic_rule_moves_last(self):
        live = [{"id": i} for i in (5117, L.GENERIC_ID, 6001)]
        self.assertEqual(L.order(live), [5117, 6001, L.GENERIC_ID])

    def test_already_last_needs_nothing(self):
        self.assertIsNone(L.order([{"id": 5117}, {"id": L.GENERIC_ID}]))


class TeamDriftTest(unittest.TestCase):
    def test_no_drift(self):
        self.assertEqual(L.team_drift(list(L.LINEAR_TEAMS)), ([], []))

    def test_new_and_removed_teams(self):
        keys = [k for k in L.LINEAR_TEAMS if k != "AR"] + ["NEW"]
        self.assertEqual(L.team_drift(keys), (["NEW"], ["AR"]))


class PlanTest(unittest.TestCase):
    def live(self, **overrides):
        rules = [
            {"id": i, "pattern": p, "url_template": u} for i, p, u in L.TARGET if i is not None
        ]
        for rule in rules:
            rule.update(overrides.get(str(rule["id"]), {}))
        return rules

    def test_matching_live_rules_need_nothing(self):
        self.assertEqual(L.plan(self.live()), ([], []))

    def test_missing_rule_is_added(self):
        live = [r for r in self.live() if r["id"] != 5143]
        self.assertEqual([s[0] for s in L.plan(live)[0]], ["add"])

    def test_changed_rule_is_edited_and_extra_rule_reported(self):
        live = self.live(**{"5122": {"url_template": "https://example.invalid/{id}"}})
        live.append({"id": 9999, "pattern": "X#(?P<id>[0-9]+)", "url_template": "https://x/{id}"})
        steps, unlisted = L.plan(live)
        self.assertIn(("edit", 5122, *[(p, u) for i, p, u in L.TARGET if i == 5122][0]), steps)
        self.assertEqual([r["id"] for r in unlisted], [9999])


if __name__ == "__main__":
    unittest.main()
