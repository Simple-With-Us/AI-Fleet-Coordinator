import json
import time
import unittest

from agent_sync import adapters as A

from agent_sync.muse_bridge import (
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
EXPECTED = ["meta/muse-spark-1.3-contributor", "minimax/minimax-m3.1"]
GOOD = {
    "runtime": "openrouter",
    "model": "@preset/operator-inbox-router",
    "expected_models": EXPECTED,
    "reason": "owner reported Muse at cap 2026-11-06; OpenRouter selected for this case",
    "declared_at": NOW - 60,
    "review_by": NOW + 3600,
}


class ParseTest(unittest.TestCase):
    def test_good_declaration_parses(self):
        decl = parse_declaration(GOOD)
        self.assertIsInstance(decl, BackupDeclaration)
        self.assertEqual(decl.runtime, "openrouter")
        self.assertEqual(decl.model, "@preset/operator-inbox-router")
        self.assertEqual(decl.expected_models, tuple(EXPECTED))

    def test_a_preset_declaration_requires_expected_models(self):
        # The served-model expectation is the only code-side fingerprint of what a mutable
        # preset serves, so a preset declaration without it is misconfigured (fail closed).
        for broken in (
            {k: v for k, v in GOOD.items() if k != "expected_models"},
            {**GOOD, "expected_models": []},
            {**GOOD, "expected_models": "meta/muse-spark-1.3-contributor"},
            {**GOOD, "expected_models": ["@preset/other"]},
            {**GOOD, "expected_models": ["$(id)/x"]},
            {**GOOD, "expected_models": [""]},
            {**GOOD, "expected_models": [5]},
        ):
            self.assertIsNone(parse_declaration(broken), broken)

    def test_a_plain_model_id_is_accepted(self):
        plain = {k: v for k, v in GOOD.items() if k != "expected_models"}
        decl = parse_declaration({**plain, "model": "~deepseek/deepseek-flash-latest"})
        self.assertIsNotNone(decl)
        # A plain model's expectation defaults to itself;  an explicit list is also accepted.
        self.assertEqual(decl.expected_models, ("~deepseek/deepseek-flash-latest",))
        decl = parse_declaration(
            {**GOOD, "model": "~deepseek/deepseek-flash-latest", "expected_models": EXPECTED}
        )
        self.assertEqual(decl.expected_models, tuple(EXPECTED))
        self.assertIsNone(
            parse_declaration(
                {**GOOD, "model": "~deepseek/deepseek-flash-latest", "expected_models": ["bad"]}
            )
        )

    def test_missing_keys_rejected(self):
        # review_by is optional (no expiry by default);  every other key is still required.
        for key in GOOD:
            if key == "review_by":
                continue
            broken = {k: v for k, v in GOOD.items() if k != key}
            self.assertIsNone(parse_declaration(broken), key)

    def test_review_by_is_optional_and_means_no_expiry(self):
        absent = {k: v for k, v in GOOD.items() if k != "review_by"}
        for raw in (absent, {**GOOD, "review_by": None}):
            decl = parse_declaration(raw)
            self.assertIsInstance(decl, BackupDeclaration)
            self.assertIsNone(decl.review_by)

    def test_a_bad_explicit_review_by_still_fails_closed(self):
        # A given-but-wrong date must never silently become "no expiry".
        for bad in ("soon", True, False, [], {}, float("nan"), float("inf"), GOOD["declared_at"], GOOD["declared_at"] - 1):
            self.assertIsNone(parse_declaration({**GOOD, "review_by": bad}), repr(bad))

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

    def test_non_finite_times_rejected(self):
        # NaN compares false against everything, so it would never reach the review-by hold.
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(parse_declaration({**GOOD, "review_by": bad}))
            self.assertIsNone(parse_declaration({**GOOD, "declared_at": bad}))

    def test_huge_int_times_fail_closed_not_raise(self):
        # math.isfinite on an int beyond float range raises OverflowError instead of returning False.
        huge = 10**400
        for key in ("review_by", "declared_at"):
            self.assertIsNone(parse_declaration({**GOOD, key: huge}))
            self.assertIsNone(parse_declaration({**GOOD, key: -huge}))

    def test_empty_expected_models_rejected_for_plain_and_preset(self):
        plain = {**GOOD, "model": "provider/name", "expected_models": []}
        self.assertIsNone(parse_declaration(plain))
        self.assertIsNone(parse_declaration({**GOOD, "expected_models": []}))

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

    def test_no_review_by_never_expires(self):
        absent = {k: v for k, v in GOOD.items() if k != "review_by"}
        for raw in (absent, {**GOOD, "review_by": None}):
            for moment in (NOW, NOW + 10 * 365 * 86400, 4_000_000_000.0):
                runtime, decl, hold = select_consult_runtime(raw, now=moment)
                self.assertEqual(runtime, OPENROUTER)
                self.assertIsNotNone(decl)
                self.assertIsNone(decl.review_by)
                self.assertIsNone(hold)

    def test_explicit_review_by_still_holds_at_the_boundary(self):
        explicit = {**GOOD, "review_by": NOW + 10}
        self.assertEqual(select_consult_runtime(explicit, now=NOW + 9)[0], OPENROUTER)
        runtime, decl, hold = select_consult_runtime(explicit, now=NOW + 10)
        self.assertEqual((runtime, hold), (HOLD, REVIEW_DUE))
        self.assertEqual(decl.review_by, NOW + 10)

    def test_real_clock_default(self):
        runtime, _, hold = select_consult_runtime(
            {**GOOD, "declared_at": time.time() - 1, "review_by": time.time() + 3600}
        )
        self.assertEqual(runtime, OPENROUTER)
        self.assertIsNone(hold)


class PresetRequestShapeTests(unittest.TestCase):
    """What the request does and does not say about a preset's tools (see the adapters comment)."""

    def test_a_preset_request_sends_an_empty_tools_array_and_tool_choice_none(self):
        body = A.openrouter_request_body("@preset/some-slug", "sys", "user", "{}")
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["tool_choice"], "none")

    def test_a_preset_request_sends_no_provider_object(self):
        # `provider` is one top-level field of a shallow merge: sending any of it would be expected
        # to replace the dashboard preset's whole provider block.
        body = A.openrouter_request_body("@preset/some-slug", "sys", "user", "{}")
        self.assertNotIn("provider", body)

    def test_a_malformed_preset_like_id_keeps_the_plain_request_shape(self):
        for bad in ("@preset/", "@preset/-bad", "@preset/has space", "@preset/" + "a" * 81):
            body = A.openrouter_request_body(bad, "sys", "user", "{}")
            self.assertEqual(body["provider"], {"require_parameters": True}, bad)
            self.assertNotIn("tools", body)
            self.assertNotIn("tool_choice", body)

    def test_a_plain_model_request_keeps_require_parameters(self):
        body = A.openrouter_request_body("provider/name", "sys", "user", "{}")
        self.assertEqual(body["provider"], {"require_parameters": True})

    def test_a_plain_model_request_sends_neither(self):
        body = A.openrouter_request_body("provider/name", "sys", "user", "{}")
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_an_answer_carrying_a_tool_call_is_refused_with_no_text(self):
        raw = json.dumps(
            {
                "model": "provider/name",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": "{}", "tool_calls": [{"id": "x"}]},
                    }
                ],
            }
        ).encode()
        text, _usage, _served, why = A.openrouter_answer(raw)
        self.assertIsNone(text)
        self.assertIn("tool call", why)


if __name__ == "__main__":
    unittest.main()
