"""Fixture tests for fleet-api-health (AFC-398):  the ST/CT mapping contract.

Every test drives the collector with fixture payloads and a fake fetcher:
no network, no real home, no live Socratic Trade or Congress.Trade.  The
fixtures pin Jet's approved mapping rules:

  * ST green is mango.reachable AND a fresh observation, never HTTP 200
    or the body's ``ok`` alone.
  * A direct senate_efd failure does not mean CT ingestion stopped.
  * A missing optional check is unknown, never zero and never green.
  * A timeout or any fetch failure fails closed to unknown, and the
    store document is still written.

Run from scripts/:  python3 -m unittest test_fleet_api_health -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import unittest
from datetime import UTC, datetime

_HERE = pathlib.Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("fleet_api_health", _HERE / "fleet-api-health.py")
api_health = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(api_health)

# One fixed clock for every fixture:  2026-11-17T12:00:00Z.
NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC).timestamp()
FRESH_STAMP = "2026-11-17T11:59:30Z"  # 30 seconds before NOW:  fresh
STALE_STAMP = "2026-11-17T11:50:00Z"  # 10 minutes before NOW:  stale


def st_payload(**overrides):
    """A healthy ST /api/live body, with any field replaced per test."""
    body = {
        "ok": True,
        "mango": {"reachable": True, "observedAt": FRESH_STAMP},
    }
    body.update(overrides)
    return body


def ct_payload(**overrides):
    """A healthy CT /api/health body, with any field replaced per test."""
    body = {
        "status": "ok",
        "components": {
            "house": "ok",
            "senate_efd": "ok",
            "oge": "ok",
            "ingestion": "ok",
        },
    }
    body.update(overrides)
    return body


def fetcher_for(st_result, ct_result):
    """A fake fetcher returning the given (status, payload, error) per app."""

    def fetch(url, timeout):
        if url == api_health.ST_LIVE_URL:
            return st_result
        if url == api_health.CT_HEALTH_URL:
            return ct_result
        raise AssertionError("unpinned url fetched: %r" % (url,))

    return fetch


def collect_with(st_result, ct_result):
    return api_health.collect(now=NOW, fetcher=fetcher_for(st_result, ct_result))


def app_row(doc, app):
    return next(row for row in doc["apps"] if row["app"] == app)


class SocraticTradeMappingTests(unittest.TestCase):
    def test_st_green_when_mango_reachable_and_observation_fresh(self):
        doc = collect_with((200, st_payload(), None), (200, ct_payload(), None))
        row = app_row(doc, "ST")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["reason"], "mango_reachable_and_observation_fresh")
        self.assertIs(row["mangoReachable"], True)
        self.assertEqual(row["observationAgeSeconds"], 30)
        self.assertEqual(row["provenance"]["source"], api_health.ST_LIVE_URL)
        self.assertEqual(row["provenance"]["httpStatus"], 200)
        self.assertEqual(doc["worst"], "ok")

    def test_st_http_200_and_body_ok_alone_are_never_green(self):
        # The body says ok and the route answered 200, but Mango is not
        # reported at all:  liveness is unproved, so the row is unknown.
        doc = collect_with((200, {"ok": True}, None), (200, ct_payload(), None))
        row = app_row(doc, "ST")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "mango_unreported")
        self.assertIsNone(row["mangoReachable"])

    def test_st_mango_unreachable_is_fail(self):
        body = st_payload(mango={"reachable": False, "observedAt": FRESH_STAMP})
        doc = collect_with((200, body, None), (200, ct_payload(), None))
        row = app_row(doc, "ST")
        self.assertEqual(row["state"], "fail")
        self.assertEqual(row["reason"], "mango_unreachable")
        self.assertIs(row["mangoReachable"], False)
        self.assertEqual(doc["worst"], "fail")

    def test_st_stale_observation_is_unknown_never_green(self):
        body = st_payload(mango={"reachable": True, "observedAt": STALE_STAMP})
        doc = collect_with((200, body, None), (200, ct_payload(), None))
        row = app_row(doc, "ST")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "observation_stale")
        self.assertEqual(row["observationAgeSeconds"], 600)

    def test_st_missing_observation_is_unknown(self):
        body = st_payload(mango={"reachable": True})
        doc = collect_with((200, body, None), (200, ct_payload(), None))
        row = app_row(doc, "ST")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "observation_unreported")


class CongressTradeMappingTests(unittest.TestCase):
    def test_ct_healthy_is_ok_and_keeps_the_aggregate(self):
        doc = collect_with((200, st_payload(), None), (200, ct_payload(), None))
        row = app_row(doc, "CT")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["reason"], "all_components_ok")
        self.assertEqual(row["upstreamStatus"], "ok")
        self.assertIs(row["ingestionAlive"], True)
        self.assertTrue(all(entry["reported"] for entry in row["components"]))

    def test_ct_senate_efd_down_but_ingestion_alive_is_warn_not_fail(self):
        body = ct_payload(
            status="degraded",
            components={
                "house": "ok",
                "senate_efd": "fail",
                "oge": "ok",
                "ingestion": "ok",
            },
        )
        doc = collect_with((200, st_payload(), None), (200, body, None))
        row = app_row(doc, "CT")
        # The pinned rule:  a direct senate_efd failure is an upstream
        # outage.  Ingestion is observed alive, so CT degrades, and the
        # document's worst is the warning, not a failure.
        self.assertEqual(row["state"], "warn")
        self.assertEqual(row["reason"], "upstream_failed_ingestion_alive:senate_efd")
        self.assertIs(row["ingestionAlive"], True)
        self.assertEqual(row["upstreamStatus"], "degraded")
        senate = next(e for e in row["components"] if e["name"] == "senate_efd")
        self.assertEqual(senate["state"], "fail")
        self.assertEqual(doc["worst"], "warn")

    def test_ct_ingestion_failure_is_fail(self):
        body = ct_payload(
            components={
                "house": "ok",
                "senate_efd": "ok",
                "oge": "ok",
                "ingestion": "fail",
            }
        )
        doc = collect_with((200, st_payload(), None), (200, body, None))
        row = app_row(doc, "CT")
        self.assertEqual(row["state"], "fail")
        self.assertEqual(row["reason"], "ingestion_failed")
        self.assertIs(row["ingestionAlive"], False)

    def test_ct_missing_optional_check_is_unknown_never_green(self):
        body = ct_payload(components={"house": "ok", "senate_efd": "ok", "ingestion": "ok"})
        doc = collect_with((200, st_payload(), None), (200, body, None))
        row = app_row(doc, "CT")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "components_unreported:oge")
        oge = next(e for e in row["components"] if e["name"] == "oge")
        self.assertEqual(oge["state"], "unknown")
        self.assertIs(oge["reported"], False)
        self.assertNotEqual(doc["worst"], "ok")


class FailClosedTests(unittest.TestCase):
    def test_timeout_fails_closed_to_unknown_and_still_writes(self):
        doc = collect_with((0, None, "timeout"), (0, None, "timeout"))
        for app in ("ST", "CT"):
            row = app_row(doc, app)
            self.assertEqual(row["state"], "unknown")
            self.assertEqual(row["error"], "timeout")
            self.assertEqual(row["provenance"]["httpStatus"], 0)
        self.assertEqual(doc["worst"], "unknown")
        # Fail closed still leaves a store file for Muster to read.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "api-health.json")
            api_health.write_atomic(path, doc)
            with open(path, encoding="utf-8") as handle:
                written = json.load(handle)
        self.assertEqual(written["worst"], "unknown")
        self.assertEqual(api_health.store_reading(written, now=NOW), "unknown")

    def test_unparseable_and_http_error_fail_closed(self):
        doc = collect_with((200, None, "unparseable"), (503, None, "http_503"))
        self.assertEqual(app_row(doc, "ST")["state"], "unknown")
        self.assertEqual(app_row(doc, "ST")["error"], "unparseable")
        self.assertEqual(app_row(doc, "CT")["state"], "unknown")
        self.assertEqual(app_row(doc, "CT")["error"], "http_503")

    def test_one_app_failing_does_not_hide_the_other(self):
        doc = collect_with((0, None, "unreachable"), (200, ct_payload(), None))
        self.assertEqual(app_row(doc, "ST")["state"], "unknown")
        self.assertEqual(app_row(doc, "CT")["state"], "ok")
        self.assertEqual(doc["worst"], "unknown")


class BoundAndStoreTests(unittest.TestCase):
    def test_only_the_two_pinned_urls_are_allowed(self):
        self.assertTrue(api_health.allowed_url(api_health.ST_LIVE_URL))
        self.assertTrue(api_health.allowed_url(api_health.CT_HEALTH_URL))
        for url in (
            "https://socratictrade.com/api/health",
            "https://congress.trade/api/live",
            "http://socratictrade.com/api/live",
            "https://socratictrade.com.evil.example/api/live",
            "https://socratictrade.com/api/live?next=https://evil.example",
            "https://example.com/api/live",
        ):
            self.assertFalse(api_health.allowed_url(url), url)
            with self.assertRaises(ValueError):
                api_health.fetch_json(url)

    def test_store_document_shape_and_permissions(self):
        doc = collect_with((200, st_payload(), None), (200, ct_payload(), None))
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["tool"], "fleet-api-health")
        self.assertEqual(doc["collector"], "api")
        self.assertEqual(doc["generatedAt"], "2026-11-17T12:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet-tools", "api-health.json")
            api_health.write_atomic(path, doc)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            with open(path, encoding="utf-8") as handle:
                written = json.load(handle)
        self.assertEqual(mode, 0o600)
        self.assertEqual(api_health.store_reading(written, now=NOW), "ok")

    def test_store_reading_rejects_stale_wrong_version_and_wrong_tool(self):
        doc = collect_with((200, st_payload(), None), (200, ct_payload(), None))
        later = NOW + api_health.STORE_MAX_AGE_SECONDS + 1
        self.assertEqual(api_health.store_reading(doc, now=later), "unknown")
        self.assertEqual(api_health.store_reading(dict(doc, schemaVersion=2), now=NOW), "unknown")
        self.assertEqual(api_health.store_reading(dict(doc, tool="other"), now=NOW), "unknown")
        self.assertEqual(api_health.store_reading(None, now=NOW), "unknown")
        self.assertEqual(api_health.store_reading({}, now=NOW), "unknown")


if __name__ == "__main__":
    unittest.main()
