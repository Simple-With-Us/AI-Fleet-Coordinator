"""Pure parse, narrow and render rules."""
from __future__ import annotations

import unittest

from fleet_knobs import values
from fleet_knobs.registry import BY_NAME, REGISTRY


class ParseTests(unittest.TestCase):
    def test_every_kind_parses_and_rejects_junk(self):
        spec = BY_NAME
        ok = [
            ("FLEET_AUTOPUSH_MODE", " Report ", "report"),
            ("FLEET_AUTOPUSH_IDLE_HOURS", "12", 12),
            ("CONSULT_ENABLED", "YES", True),
            ("CONSULT_ENABLED", "off", False),
            ("FLEET_AUTOPUSH_REPOS", "a/b, c/d\ne/f a/b", ("a/b", "c/d", "e/f")),
            ("FLEET_AUTOPUSH_REPOS", '["a/b", "c/d"]', ("a/b", "c/d")),
            ("FLEET_AUTOPUSH_REPOS", "", ()),
            ("ASK_CAPTURE_CAPS", "per_session=5, per_seat_day=30", {"per_session": 5, "per_seat_day": 30}),
            ("ASK_CAPTURE_CAPS", '{"per_session": 5}', {"per_session": 5}),
            ("OWNER_LINEAR_USER_ID", "11111111-2222-3333-4444-555555555555",
             "11111111-2222-3333-4444-555555555555"),
            ("CLAIM_EXPIRY_LAUNCH_AT", "2026-10-12T05:00:00Z", "2026-10-12T05:00:00Z"),
            ("CLAIM_EXPIRY_LAUNCH_AT", "2026-10-12T00:00:00-05:00", "2026-10-12T00:00:00-05:00"),
        ]
        for name, raw, expect in ok:
            with self.subTest(name=name, raw=raw):
                self.assertEqual(values.parse(spec[name], raw), expect)
        bad = [
            ("FLEET_AUTOPUSH_MODE", "everything"), ("FLEET_AUTOPUSH_MODE", ""),
            ("FLEET_AUTOPUSH_IDLE_HOURS", "0"), ("FLEET_AUTOPUSH_IDLE_HOURS", "999"),
            ("FLEET_AUTOPUSH_IDLE_HOURS", "-1"), ("FLEET_AUTOPUSH_IDLE_HOURS", "2.5"),
            ("CONSULT_ENABLED", "maybe"),
            ("FLEET_AUTOPUSH_REPOS", "a/b;rm -rf"), ("FLEET_AUTOPUSH_REPOS", "[1, 2]"),
            ("FLEET_AUTOPUSH_REPOS", "[not json"),
            ("ASK_CAPTURE_CAPS", "per_session"), ("ASK_CAPTURE_CAPS", "per_session=-1"),
            ("ASK_CAPTURE_CAPS", "Per_Session=1"), ("ASK_CAPTURE_CAPS", "a=1,a=2"),
            ("ASK_CAPTURE_CAPS", '{"a": true}'), ("ASK_CAPTURE_CAPS", '["a"]'),
            ("OWNER_LINEAR_USER_ID", "jay"), ("CLAIM_EXPIRY_LAUNCH_AT", "tomorrow"),
            ("FLEET_FACTS_SIGNING_KEY", "short"),
            ("CLAIM_EXPIRY_MODE", "act\nping-only"),
        ]
        for name, raw in bad:
            with self.subTest(name=name, raw=raw):
                with self.assertRaises(ValueError):
                    values.parse(spec[name], raw)

    def test_the_error_never_carries_the_value(self):
        for raw in ("sentinel-value-xyz", "per_session=sentinel-value-xyz"):
            for name in ("CLAIM_EXPIRY_MODE", "ASK_CAPTURE_CAPS", "FLEET_AUTOPUSH_IDLE_HOURS"):
                try:
                    values.parse(BY_NAME[name], raw)
                except ValueError as exc:
                    self.assertNotIn("sentinel", str(exc))


class NarrowTests(unittest.TestCase):
    def test_enum_order_is_the_meaning_of_narrower(self):
        spec = BY_NAME["FLEET_AUTOPUSH_MODE"]
        for base, local, expect in (("full", "report", "report"), ("report", "full", "report"),
                                    ("off", "snapshot", "off"), ("snapshot", "snapshot", "snapshot")):
            self.assertEqual(values.narrow(spec, base, local)[0], expect)

    def test_int_direction(self):
        hours = BY_NAME["FLEET_AUTOPUSH_IDLE_HOURS"]          # higher is narrower
        self.assertEqual(values.narrow(hours, 2, 8)[0], 8)
        self.assertEqual(values.narrow(hours, 8, 2)[0], 8)

    def test_caps_union_and_per_key_minimum(self):
        spec = BY_NAME["NEEDS_OWNER_CAPS"]
        out, narrowed, ignored = values.narrow(spec, {"a": 5, "b": 9}, {"a": 7, "b": 2, "c": 1})
        self.assertEqual(out, {"a": 5, "b": 2, "c": 1})
        self.assertTrue(narrowed)
        self.assertTrue(ignored)

    def test_narrow_is_idempotent_and_never_widens(self):
        for spec in REGISTRY:
            if spec.secret or spec.local == "deny":
                continue
            base = values.default_value(spec)
            stop = values.most_conservative(spec, base)
            again, narrowed, _ = values.narrow(spec, stop, base)
            self.assertEqual(again, stop, spec.name)
            self.assertFalse(narrowed, spec.name)

    def test_defaults_are_valid_and_render_round_trips(self):
        for spec in REGISTRY:
            if spec.secret:
                self.assertIsNone(spec.default)
                continue
            default = values.default_value(spec)
            if spec.kind == "str":
                self.assertEqual(default, spec.default or "")  # most are empty;  NOTES_RELAY_CONSUMER defaults to CLAUDE
                if default:
                    self.assertEqual(values.parse(spec, default), default, spec.name)
                continue
            text = values.render(spec, default)
            self.assertEqual(values.parse(spec, text), default, spec.name)
