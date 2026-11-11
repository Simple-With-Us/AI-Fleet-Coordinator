from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from simple_tools import chat_nudges as CN
from simple_tools import operator_registry as OR


def _session_id(seed: str = "a") -> str:
    # Passes the fleet's own session id checker (letters, digits, . _ : -).
    return "sess-" + seed * 8


class WakeCapabilityTableTest(unittest.TestCase):
    def test_nudge_transports_come_from_the_same_table(self) -> None:
        self.assertEqual(set(OR.implemented_wake_platforms()), CN.SUPPORTED_PLATFORMS)
        self.assertIn("grok-leader", CN.SUPPORTED_PLATFORMS)
        self.assertIn("cursor-cloud", CN.SUPPORTED_PLATFORMS)
        self.assertIn("codex", CN.SUPPORTED_PLATFORMS)

    def test_assessed_platforms_without_adapters_are_none_not_unknown(self) -> None:
        for platform in (
            "claude",
            "cursor-local",
            "muse",
            "minimax",
            "fx",
            "antigravity",
            "opencode",
            "chatgpt",
        ):
            self.assertEqual(OR.wake_capability_for_platform(platform), OR.WAKE_NONE, platform)

    def test_chatgpt_none_means_no_adapter_here_not_no_wake(self) -> None:
        # The owner-asked parity record:  JET wakes by his own hosted route,
        # outside this tree, so he never enters this tree's delivery adapters.
        self.assertNotIn("chatgpt", OR.implemented_wake_platforms())

    def test_unlisted_platform_is_unknown_never_none(self) -> None:
        for platform in ("zcode", "kimi", "amp", "clutch", "grok-web", ""):
            self.assertEqual(OR.wake_capability_for_platform(platform), OR.WAKE_UNKNOWN, platform)
        self.assertEqual(OR.wake_capability_for_platform(None), OR.WAKE_UNKNOWN)

    def test_lookup_trims_and_folds_case(self) -> None:
        self.assertEqual(OR.wake_capability_for_platform(" Grok-Leader "), OR.WAKE_SUPPORTED)


class RegisterDerivationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.db")

    def _register(self, platform: str, **kwargs) -> dict:
        args = dict(
            platform=platform,
            verified_host="mac",
            profile_scope="default",
            native_session_id=_session_id(platform.replace("-", "x")),
            seat="CODEX",
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )
        args.update(kwargs)
        return OR.register_session(self.db, **args)

    def _force_stored(self, platform: str, value: str) -> None:
        # Simulate a row written before the derivation existed.
        row = self._stored(platform)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE sessions SET wake_capability = ? WHERE record_id = ?",
                (value, row["record_id"]),
            )

    def _stored(self, platform: str) -> dict:
        registry = OR.SessionRegistry(self.db)
        try:
            row = registry._get_by_identity(
                OR.normalize_platform(platform),
                "mac",
                "default",
                _session_id(platform.replace("-", "x")),
            )
        finally:
            registry.close_db()
        assert row is not None
        return row

    def test_default_claim_derives_support_for_transport_platform(self) -> None:
        out = self._register("codex")
        self.assertEqual(out["outcome"], "registered")
        self.assertEqual(self._stored("codex")["wake_capability"], OR.WAKE_SUPPORTED)

    def test_default_claim_derives_none_for_assessed_platform(self) -> None:
        self._register("claude")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_NONE)

    def test_default_claim_stays_unknown_for_unlisted_platform(self) -> None:
        self._register("zcode")
        self.assertEqual(self._stored("zcode")["wake_capability"], OR.WAKE_UNKNOWN)

    def test_explicit_caller_claim_wins_over_derivation(self) -> None:
        self._register("codex", wake_capability="live-verified")
        self.assertEqual(self._stored("codex")["wake_capability"], "live-verified")

    def test_resume_refreshes_only_the_unknown_sentinel(self) -> None:
        # A row written before the table existed (or by a caller with no claim)
        # takes the derived value when its own session next registers.
        self._register("claude")
        self._force_stored("claude", "unknown")
        out = self._register("claude")
        self.assertEqual(out["outcome"], "resumed")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_NONE)

    def test_resume_never_overrides_an_explicit_stored_claim(self) -> None:
        self._register("codex", wake_capability="live-verified")
        self._register("codex")
        self.assertEqual(self._stored("codex")["wake_capability"], "live-verified")

    def test_resume_of_closed_row_keeps_its_history(self) -> None:
        self._register("claude")
        self._force_stored("claude", "unknown")
        row = self._stored("claude")
        registry = OR.SessionRegistry(self.db)
        try:
            registry.close(row["record_id"], actor="test", source="test", reason="done")
        finally:
            registry.close_db()
        out = self._register("claude")
        self.assertEqual(out["outcome"], "resumed_closed")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_UNKNOWN)


    def test_derived_claim_survives_a_later_explicit_claim(self) -> None:
        # Stored claims are immutable: a later registration carrying its own
        # claim does not rewrite the row's original claim or provenance.
        self._register("claude")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_NONE)
        out = self._register("claude", wake_capability="unavailable")
        self.assertEqual(out["outcome"], "resumed")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_NONE)

    def test_derived_claim_survives_a_later_live_verified_claim(self) -> None:
        self._register("codex")
        self.assertEqual(self._stored("codex")["wake_capability"], OR.WAKE_SUPPORTED)
        self._register("codex", wake_capability="live-verified")
        self.assertEqual(self._stored("codex")["wake_capability"], OR.WAKE_SUPPORTED)

    def test_derivation_uses_adapter_platform_before_namespace_folding(self) -> None:
        # cursor-local folds to the cursor letter namespace, but a local-only chat
        # must not inherit cursor-cloud's delivery claim.
        self._register("cursor-local")
        row = self._stored("cursor-local")
        self.assertEqual(row["platform"], "cursor")
        self.assertEqual(row["wake_capability"], OR.WAKE_NONE)
        self._register("cursor-cloud")
        row = self._stored("cursor-cloud")
        self.assertEqual(row["platform"], "cursor")
        self.assertEqual(row["wake_capability"], OR.WAKE_SUPPORTED)


if __name__ == "__main__":
    unittest.main()

class SentinelNormalizationTest(unittest.TestCase):
    """Kody #1348: the no-claim sentinel is case/whitespace-insensitive."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.db")

    def _register(self, platform: str, **kwargs) -> dict:
        args = dict(
            platform=platform,
            verified_host="mac",
            profile_scope="default",
            native_session_id=_session_id(platform.replace("-", "x")),
            seat="CODEX",
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )
        args.update(kwargs)
        return OR.register_session(self.db, **args)

    def _force_stored(self, platform: str, value: str) -> None:
        row = self._stored(platform)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE sessions SET wake_capability = ? WHERE record_id = ?",
                (value, row["record_id"]),
            )

    def _stored(self, platform: str) -> dict:
        registry = OR.SessionRegistry(self.db)
        try:
            row = registry._get_by_identity(
                OR.normalize_platform(platform),
                "mac",
                "default",
                _session_id(platform.replace("-", "x")),
            )
        finally:
            registry.close_db()
        assert row is not None
        return row

    def test_stored_uppercase_sentinel_takes_the_resume_update(self) -> None:
        self._register("claude")
        self._force_stored("claude", "UNKNOWN")
        out = self._register("claude")
        self.assertEqual(out["outcome"], "resumed")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_NONE)

    def test_stored_padded_sentinel_takes_the_resume_update(self) -> None:
        self._register("claude")
        self._force_stored("claude", " unknown ")
        self._register("claude")
        self.assertEqual(self._stored("claude")["wake_capability"], OR.WAKE_NONE)

    def test_case_variant_of_a_real_value_is_a_stored_claim(self) -> None:
        self._register("claude")
        self._force_stored("claude", "None")
        self._register("claude")
        self.assertEqual(self._stored("claude")["wake_capability"], "None")

    def test_unknownish_incoming_claim_is_never_stored(self) -> None:
        self._register("zcode")
        self._force_stored("zcode", "unknown")
        self._register("zcode", wake_capability="UNKNOWN")
        self.assertEqual(self._stored("zcode")["wake_capability"], OR.WAKE_UNKNOWN)

    def test_unknownish_incoming_never_overwrites_a_real_claim(self) -> None:
        self._register("codex", wake_capability="live-verified")
        self._register("codex", wake_capability=" unknown ")
        self.assertEqual(self._stored("codex")["wake_capability"], "live-verified")
