"""Apply recorded producer payloads through the relay transform mappings."""
from __future__ import annotations

import unittest

from relay_common import (
    MAPPINGS,
    PRODUCERS,
    apply_sets,
    config_text,
    load_fixture,
    statements,
)


class RecordedPayloadMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = config_text()

    def test_each_fixture_maps_vendor_attrs_onto_gen_ai(self) -> None:
        for producer, spec in PRODUCERS.items():
            log_stmts = statements(self.cfg, producer, "log")
            resource_stmts = [
                s
                for s in log_stmts
                if '["signal"]' in s or '["platform"]' in s and "gen_ai" not in s
            ]
            # Resource statements live in the resource context block, which
            # statements() includes because it scans the whole log_statements
            # section.  Apply every set() in that section to the record attrs
            # plus the resource attrs so platform/signal land too.
            self.assertTrue(log_stmts, producer)
            for name in spec["fixtures"]:
                rec = load_fixture(name)
                attrs = dict(rec["attributes"])
                attrs.update(rec.get("resource", {}))
                out = apply_sets(log_stmts, attrs)
                with self.subTest(producer=producer, fixture=name):
                    self.assertEqual(out.get("signal"), "agent")
                    if spec["agent"] is not None:
                        self.assertEqual(out.get("gen_ai.agent.name"), spec["agent"])
                        self.assertEqual(out.get("platform"), spec["platform"])
                    else:
                        # Shim: producer-set platform wins and becomes agent.name.
                        self.assertEqual(out.get("platform"), attrs.get("platform") or spec["platform"])
                        self.assertEqual(out.get("gen_ai.agent.name"), out.get("platform"))
                    for src, dest in MAPPINGS[producer]:
                        if src in rec["attributes"]:
                            self.assertEqual(
                                out.get(dest),
                                rec["attributes"][src],
                                "%s: %s -> %s" % (name, src, dest),
                            )
                _ = resource_stmts  # kept so a future split stays obvious

    def test_prompt_and_response_are_not_deleted(self) -> None:
        rec = load_fixture("claude-code-user-prompt.json")
        out = apply_sets(statements(self.cfg, "claude", "log"), dict(rec["attributes"]))
        self.assertEqual(out["prompt"], "Summarize the quarterly report in three bullets.")
        shim = load_fixture("hook-shim-after-response.json")
        shim_out = apply_sets(statements(self.cfg, "shim", "log"), dict(shim["attributes"]))
        self.assertIn("Done:", shim_out["response"])
        for producer in PRODUCERS:
            text = "\n".join(statements(self.cfg, producer, "log"))
            self.assertNotIn("delete(attributes[\"prompt\"])", text)
            self.assertNotIn("delete(attributes[\"response\"])", text)

    def test_trace_mappings_match_log_mappings(self) -> None:
        for producer in PRODUCERS:
            log_sets = {s.split(" where")[0] for s in statements(self.cfg, producer, "log")}
            span_sets = {s.split(" where")[0] for s in statements(self.cfg, producer, "trace")}
            self.assertTrue(log_sets, producer)
            self.assertEqual(log_sets, span_sets, producer)

    def test_hook_execution_noise_is_dropped(self) -> None:
        rec = load_fixture("claude-code-hook-execution.json")
        filt = self.cfg
        self.assertIn('IsMatch(attributes["event.name"], "^hook_execution_")', filt)
        self.assertIn('attributes["event.name"] == "idle_poll.command_class"', filt)
        self.assertTrue(rec["attributes"]["event.name"].startswith("hook_execution_"))

    def test_reserved_sentry_resource_attrs_are_never_set(self) -> None:
        text = self.cfg
        self.assertNotIn('set(attributes["project"]', text)
        self.assertNotIn("set(attributes['project']", text)
        self.assertNotIn('set(attributes["organization.id"]', text)
        self.assertNotIn('set(attributes["organization_id"]', text)


class FixtureCoverageTests(unittest.TestCase):
    def test_every_producer_has_at_least_one_recorded_payload(self) -> None:
        for producer, spec in PRODUCERS.items():
            self.assertTrue(spec["fixtures"], producer)
            for name in spec["fixtures"]:
                rec = load_fixture(name)
                self.assertIn("attributes", rec, name)
                self.assertTrue(rec["attributes"], name)


if __name__ == "__main__":
    unittest.main()
