import time
import unittest

from agent_sync.backup_mode import (
    HOLD,
    MUSE,
    OPENROUTER,
    MISCONFIGURED,
    REVIEW_DUE,
    BackupDeclaration,
    parse_declaration,
    select_consult_runtime,
)

NOW = 1_760_000_000.0
GOOD = {
    "runtime": "openrouter",
    "model": "@preset/operator-inbox-router",
    "reason": "owner declared Muse at cap 2026-11-06, OpenRouter backup directed",
    "declared_at": NOW - 60,
    "review_by": NOW + 3600,
}


class ParseTest(unittest.TestCase):
    def test_good_declaration_parses(self):
        decl = parse_declaration(GOOD)
        self.assertIsInstance(decl, BackupDeclaration)
        self.assertEqual(decl.runtime, "openrouter")
        self.assertEqual(decl.model, "@preset/operator-inbox-router")

    def test_a_plain_model_id_is_accepted(self):
        decl = parse_declaration({**GOOD, "model": "~deepseek/deepseek-flash-latest"})
        self.assertIsNotNone(decl)

    def test_missing_keys_rejected(self):
        for key in GOOD:
            broken = {k: v for k, v in GOOD.items() if k != key}
            self.assertIsNone(parse_declaration(broken), key)

    def test_the_retired_minimax_runtime_is_rejected(self):
        self.assertIsNone(parse_declaration({**GOOD, "runtime": "minimax"}))

    def test_bad_models_rejected(self):
        for bad in ("muse", "@preset/", "@preset/ x", "meta/", "$(id)/x", "", 5):
            self.assertIsNone(parse_declaration({**GOOD, "model": bad}), bad)

    def test_blank_reason_rejected(self):
        self.assertIsNone(parse_declaration({**GOOD, "reason": "   "}))

    def test_non_numeric_times_rejected(self):
        self.assertIsNone(parse_declaration({**GOOD, "review_by": "soon"}))
        self.assertIsNone(parse_declaration({**GOOD, "declared_at": True}))

    def test_backwards_window_rejected(self):
        self.assertIsNone(parse_declaration({**GOOD, "review_by": GOOD["declared_at"]}))

    def test_non_mapping_rejected(self):
        for raw in (None, "openrouter", ["openrouter"], 42):
            self.assertIsNone(parse_declaration(raw))

    def test_extra_fields_tolerated(self):
        self.assertIsNotNone(parse_declaration({**GOOD, "ticket": "AFC-649"}))


class SelectTest(unittest.TestCase):
    def test_never_enabled_selects_primary(self):
        # Absent or null declaration:  the mode was never enabled, primary runs as today.
        self.assertEqual(select_consult_runtime(None, now=NOW), (MUSE, None, None))

    def test_configured_but_malformed_fails_closed(self):
        # A configured declaration that is partial or malformed HOLDS:  it never silently
        # reverts to the capped primary and never consumes rows.
        for raw in ({}, {"runtime": "openrouter"}, {"runtime": "minimax"}, "openrouter", 42):
            runtime, decl, hold = select_consult_runtime(raw, now=NOW)
            self.assertEqual(runtime, HOLD, raw)
            self.assertIsNone(decl)
            self.assertEqual(hold, MISCONFIGURED)

    def test_active_window_selects_backup(self):
        runtime, decl, hold = select_consult_runtime(GOOD, now=NOW)
        self.assertEqual(runtime, OPENROUTER)
        self.assertIsNotNone(decl)
        self.assertIsNone(hold)

    def test_expired_holds_instead_of_reverting_or_spending(self):
        expired = {**GOOD, "review_by": NOW - 1}
        runtime, decl, hold = select_consult_runtime(expired, now=NOW)
        self.assertEqual(runtime, HOLD)
        self.assertIsNotNone(decl)
        self.assertEqual(hold, REVIEW_DUE)

    def test_real_clock_default(self):
        runtime, _, hold = select_consult_runtime(
            {**GOOD, "declared_at": time.time() - 1, "review_by": time.time() + 3600}
        )
        self.assertEqual(runtime, OPENROUTER)
        self.assertIsNone(hold)


if __name__ == "__main__":
    unittest.main()
