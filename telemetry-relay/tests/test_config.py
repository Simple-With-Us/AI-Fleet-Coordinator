"""Structural pins for fleet-telemetry-relay: ports, env names, sinks, image pin."""
from __future__ import annotations

import unittest

from relay_common import (
    COOLIFY,
    ENV_NAMES,
    PRODUCERS,
    block,
    config_text,
    env_refs,
)


def _pipeline(cfg: str, header: str) -> str:
    return block(cfg, header)


class ReceiverAuthTests(unittest.TestCase):
    def test_one_http_port_and_bearer_per_producer(self) -> None:
        cfg = config_text()
        for producer, spec in PRODUCERS.items():
            self.assertIn('endpoint: "0.0.0.0:%d"' % spec["port"], cfg)
            self.assertIn("bearertokenauth/%s:" % producer, cfg)
            self.assertIn("${env:%s}" % spec["bearer"], cfg)
            self.assertIn("authenticator: bearertokenauth/%s" % producer, cfg)


class ExporterTests(unittest.TestCase):
    def test_sentry_fleet_infra_otlp_uses_named_env(self) -> None:
        cfg = config_text()
        self.assertIn("otlphttp/sentry:", cfg)
        self.assertIn("${env:SENTRY_FLEET_OTLP_ENDPOINT}", cfg)
        self.assertIn("sentry sentry_key=${env:SENTRY_FLEET_OTLP_KEY}", cfg)
        self.assertIn('set(attributes["signal"], "agent")', cfg)

    def test_usage_monitor_metrics_and_traces_are_separate_env_urls(self) -> None:
        cfg = config_text()
        self.assertIn("otlphttp/usage-monitor-metrics:", cfg)
        self.assertIn("otlphttp/usage-monitor-traces:", cfg)
        self.assertIn("${env:USAGE_MONITOR_OTLP_METRICS_ENDPOINT}", cfg)
        self.assertIn("${env:USAGE_MONITOR_OTLP_TRACES_ENDPOINT}", cfg)
        self.assertIn("${env:USAGE_MONITOR_INGEST_TOKEN}", cfg)
        self.assertNotIn("${env:USAGE_MONITOR_OTLP_ENDPOINT}", cfg)

    def test_every_env_ref_is_listed_in_coolify_doc(self) -> None:
        cfg = config_text()
        doc = COOLIFY.read_text(encoding="utf-8")
        refs = env_refs(cfg)
        self.assertEqual(refs, set(ENV_NAMES))
        for name in ENV_NAMES:
            self.assertIn("`%s`" % name, doc)

    def test_no_hardcoded_dsn_or_token_literals(self) -> None:
        cfg = config_text()
        self.assertNotIn("https://o", cfg)
        self.assertNotIn("ingest.sentry.io", cfg)
        self.assertNotIn("sntrys_", cfg)
        self.assertNotIn("sntryu_", cfg)


class PipelineTests(unittest.TestCase):
    def test_logs_go_to_sentry_not_usage_monitor(self) -> None:
        cfg = config_text()
        for producer in PRODUCERS:
            chunk = _pipeline(cfg, "logs/%s" % producer)
            self.assertIn("otlphttp/sentry", chunk)
            self.assertNotIn("usage-monitor", chunk)
            self.assertIn("filter/drop-noise", chunk)

    def test_traces_fan_out_to_sentry_and_usage_monitor(self) -> None:
        cfg = config_text()
        for producer in PRODUCERS:
            chunk = _pipeline(cfg, "traces/%s" % producer)
            self.assertIn("otlphttp/sentry", chunk)
            self.assertIn("otlphttp/usage-monitor-traces", chunk)

    def test_metrics_go_to_usage_monitor_only(self) -> None:
        cfg = config_text()
        for producer in PRODUCERS:
            chunk = _pipeline(cfg, "metrics/%s" % producer)
            self.assertIn("otlphttp/usage-monitor-metrics", chunk)
            self.assertNotIn("otlphttp/sentry", chunk)


class DockerfilePinTests(unittest.TestCase):
    def test_image_is_a_stable_contrib_pin_not_nightly(self) -> None:
        text = (COOLIFY.parent / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("OTELCOL_VERSION=0.162.0", text)
        from_lines = [ln for ln in text.splitlines() if ln.startswith("FROM ")]
        self.assertTrue(from_lines)
        self.assertNotIn("nightly", from_lines[0])
        self.assertIn("opentelemetry-collector-contrib", from_lines[0])


if __name__ == "__main__":
    unittest.main()
