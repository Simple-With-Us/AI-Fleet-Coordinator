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
from datetime import datetime, timezone

_HERE = pathlib.Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("fleet_api_health", _HERE / "fleet-api-health.py")
api_health = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(api_health)

# One fixed clock for every fixture:  2026-11-17T12:00:00Z.
NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc).timestamp()
FRESH_STAMP = "2026-11-17T11:59:30Z"  # 30 seconds before NOW:  fresh
STALE_STAMP = "2026-11-17T11:50:00Z"  # 10 minutes before NOW:  stale


def st_payload(**overrides):
    """A healthy ST /api/proxy-use body, with any field replaced per test."""
    body = {
        "ok": True,
        "mango": {"reachable": True, "checkedAt": FRESH_STAMP},
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
        body = st_payload(mango={"reachable": False, "checkedAt": FRESH_STAMP})
        doc = collect_with((200, body, None), (200, ct_payload(), None))
        row = app_row(doc, "ST")
        self.assertEqual(row["state"], "fail")
        self.assertEqual(row["reason"], "mango_unreachable")
        self.assertIs(row["mangoReachable"], False)
        self.assertEqual(doc["worst"], "fail")

    def test_st_stale_observation_is_unknown_never_green(self):
        body = st_payload(mango={"reachable": True, "checkedAt": STALE_STAMP})
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

    def test_an_ingestion_failure_is_fail_even_with_a_check_unreported(self):
        body = ct_payload(components={"house": "ok", "senate_efd": "ok", "ingestion": "fail"})
        doc = collect_with((200, st_payload(), None), (200, body, None))
        row = app_row(doc, "CT")
        self.assertEqual(row["state"], "fail")
        self.assertEqual(row["reason"], "ingestion_failed")
        self.assertIs(row["ingestionAlive"], False)
        self.assertEqual(doc["worst"], "fail")

    def test_an_unreadable_ingestion_value_proves_neither_alive_nor_stopped(self):
        body = ct_payload(
            components={
                "house": "ok",
                "senate_efd": "ok",
                "oge": "ok",
                "ingestion": "sideways",
            }
        )
        doc = collect_with((200, st_payload(), None), (200, body, None))
        row = app_row(doc, "CT")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "ingestion_unproved")
        self.assertIsNone(row["ingestionAlive"])


class LaunchAgentTests(unittest.TestCase):
    def test_the_agent_runs_the_collector_from_the_managed_runtime(self):
        plist = pathlib.Path(_HERE, "launchd", "com.jay.fleet-api-health.plist").read_text()
        sibling = pathlib.Path(_HERE, "launchd", "com.jay.fleet-mac-services.plist").read_text()
        runtime = "/Users/jay/apps/lanes/_managed/fleet/simple-tools-runtime/scripts/"
        self.assertIn(runtime + "fleet-mac-services.py", sibling)
        self.assertIn(runtime + "fleet-api-health.py", plist)


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

    def test_a_read_timeout_is_a_timeout_on_every_python(self):
        # Sentry, PR 2302:  on Python 3.9 a read timeout is a bare `socket.timeout`, not a
        # `TimeoutError`, and must still read as `timeout`, not `unreachable`.
        import socket

        class SlowResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                raise socket.timeout("timed out")

        class Opener:
            def open(self, request, timeout=None):
                return SlowResponse()

        for raised in (socket.timeout("timed out"), TimeoutError("timed out")):
            with self.subTest(raised=type(raised).__name__):

                class Raising:
                    def open(self, request, timeout=None, raised=raised):
                        raise raised

                self.assertEqual(
                    api_health.fetch_json(api_health.ST_LIVE_URL, 5, opener=Raising()),
                    (0, None, "timeout"),
                )
        self.assertEqual(
            api_health.fetch_json(api_health.ST_LIVE_URL, 5, opener=Opener()),
            (0, None, "timeout"),
        )
        self.assertIn(socket.timeout, api_health.TIMEOUTS)

    def test_deeply_nested_json_is_unparseable_not_a_crash(self):
        # A body under the size cap can still nest past the interpreter
        # recursion limit.  That is an unparseable answer, not a reason
        # to abort the run before the store file is written.
        class NestedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                return b"[" * 2000 + b"]" * 2000

        class Opener:
            def open(self, request, timeout=None):
                return NestedResponse()

        self.assertEqual(
            api_health.fetch_json(api_health.ST_LIVE_URL, 5, opener=Opener()),
            (200, None, "unparseable"),
        )

    def test_one_app_failing_does_not_hide_the_other(self):
        doc = collect_with((0, None, "unreachable"), (200, ct_payload(), None))
        self.assertEqual(app_row(doc, "ST")["state"], "unknown")
        self.assertEqual(app_row(doc, "CT")["state"], "ok")
        self.assertEqual(doc["worst"], "unknown")


class Python39Tests(unittest.TestCase):
    def test_the_collector_and_its_tests_import_on_python_3_9(self):
        # The Mac's LaunchAgent runs the collector under /usr/bin/python3 (3.9), where the
        # 3.11+ UTC constant of the datetime module does not exist.  An automated fix rewrote
        # `timezone.utc` to it on PR 2302;  ruff.toml now keeps both files at py39, and this
        # catches any other route.
        for name in ("fleet-api-health.py", "test_fleet_api_health.py"):
            with self.subTest(name=name):
                source = (_HERE / name).read_text(encoding="utf-8")
                self.assertNotIn("datetime" + ".UTC", source)
                self.assertNotIn("import" + " UTC", source)
                self.assertNotIn("UTC" + ", datetime", source)


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


class ActualPipelineMappingTests(unittest.TestCase):
    def native(self):
        return json.loads((_HERE / "fixtures/api-health/ct-actual-20261118.json").read_text())

    def test_actual_st_checked_at(self):
        payload = json.loads((_HERE / "fixtures/api-health/st-actual-20261118.json").read_text())
        now = api_health.parse_iso("2026-11-18T17:23:23Z")
        row = api_health.map_st_live(payload, 200, now)
        self.assertEqual(row["state"], "ok")
        # The fixture's checkedAt carries .512s, so the true age is
        # 37.488s and the stored age is the ceiled 38 the freshness
        # gate compares (see the rounds-once test below).
        self.assertEqual(row["observationAgeSeconds"], 38)
        payload["mango"]["checkedAt"] = "2026-11-18T17:00:00Z"
        self.assertEqual(api_health.map_st_live(payload, 200, now)["state"], "unknown")

    def test_st_legacy_observation_does_not_substitute_for_native_probe(self):
        payload = {
            "ok": True,
            "mango": {"reachable": True, "observedAt": FRESH_STAMP},
            "observationAgeSeconds": 0,
        }
        self.assertEqual(api_health.map_st_live(payload, 200, NOW)["state"], "unknown")
        for stamp in ("bad", "2026-11-17T11:59:30", "2026-11-17T12:00:30Z"):
            payload["mango"]["checkedAt"] = stamp
            self.assertEqual(api_health.map_st_live(payload, 200, NOW)["state"], "unknown")

    def test_st_fractional_gate_boundaries(self):
        payload = {"ok": True, "mango": {"reachable": True, "checkedAt": FRESH_STAMP}}
        stamp = api_health.parse_iso(FRESH_STAMP)
        for elapsed, expected in (
            (-0.001, "unknown"),
            (0, "ok"),
            (120, "ok"),
            (120.001, "unknown"),
        ):
            self.assertEqual(
                api_health.map_st_live(payload, 200, stamp + elapsed)["state"], expected
            )
        payload["mango"]["reachable"] = False
        self.assertEqual(api_health.map_st_live(payload, 200, stamp)["state"], "fail")

    def test_present_invalid_pipeline_never_falls_back(self):
        for native in (None, [], "bad", {}, {"checks": None}):
            self.assertEqual(
                api_health.map_ct_health(ct_payload(pipeline=native), 200, NOW)["state"],
                "unknown",
            )

    def test_native_statuses_are_exact_and_poll_degraded_is_unproved(self):
        for value in (
            True,
            {"ok": True},
            "healthy",
            "warn",
            "OK",
            " ok ",
            "degraded",
            None,
        ):
            payload = self.native()
            next(c for c in payload["pipeline"]["checks"] if c["id"] == "polling_senate")[
                "status"
            ] = value
            row = api_health.map_ct_health(payload, 200, NOW)
            self.assertIsNone(row["ingestionAlive"])
            self.assertEqual(row["state"], "unknown")

    def test_known_poll_failure_not_hidden_by_missing_other_poll(self):
        payload = self.native()
        payload["pipeline"]["checks"] = [
            c for c in payload["pipeline"]["checks"] if c["id"] != "polling_house"
        ]
        next(c for c in payload["pipeline"]["checks"] if c["id"] == "polling_senate")["status"] = (
            "stalled"
        )
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertFalse(row["ingestionAlive"])
        self.assertEqual(row["state"], "fail")

    def test_actual_ct_canary_degraded_while_polling_alive(self):
        row = api_health.map_ct_health(self.native(), 200, NOW)
        self.assertEqual(row["state"], "warn")
        self.assertTrue(row["ingestionAlive"])
        self.assertEqual(row["upstreamStatus"], "degraded")
        self.assertEqual(
            next(c for c in row["components"] if c["name"] == "senate_efd")["state"],
            "warn",
        )

    def test_each_chamber_poll_required_for_ingestion(self):
        for source in ("polling_house", "polling_senate", "polling_executive"):
            payload = self.native()
            payload["pipeline"]["checks"] = [
                c for c in payload["pipeline"]["checks"] if c["id"] != source
            ]
            row = api_health.map_ct_health(payload, 200, NOW)
            self.assertIsNone(row["ingestionAlive"])
            self.assertEqual(row["state"], "unknown")

    def test_stalled_poll_is_failure_not_unknown_or_canary_warning(self):
        for source in ("polling_house", "polling_senate", "polling_executive"):
            payload = self.native()
            next(c for c in payload["pipeline"]["checks"] if c["id"] == source)["status"] = (
                "stalled"
            )
            row = api_health.map_ct_health(payload, 200, NOW)
            self.assertEqual(row["state"], "fail")
            self.assertFalse(row["ingestionAlive"])

    def test_clear_backlog_alone_never_proves_ingestion_alive(self):
        payload = {
            "status": "ok",
            "pipeline": {"checks": [{"id": "ingestion_backlog", "status": "ok"}]},
        }
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertIsNone(row["ingestionAlive"])
        self.assertEqual(row["state"], "unknown")

    def test_duplicate_poll_unknown_not_first_wins(self):
        payload = self.native()
        payload["pipeline"]["checks"].append({"id": "polling_senate", "status": "ok"})
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertIsNone(row["ingestionAlive"])
        self.assertEqual(row["state"], "unknown")

    def test_native_malformed_does_not_fall_back_to_legacy_green(self):
        payload = ct_payload(pipeline={"checks": None})
        self.assertEqual(api_health.map_ct_health(payload, 200, NOW)["state"], "unknown")

    def test_other_upstream_degradation_is_not_hidden_by_display_subset(self):
        payload = self.native()
        for check in payload["pipeline"]["checks"]:
            check["status"] = "ok"
        self.assertEqual(api_health.map_ct_health(payload, 200, NOW)["state"], "warn")
        payload["status"] = "ok"
        self.assertEqual(api_health.map_ct_health(payload, 200, NOW)["state"], "ok")

    def test_native_canary_failure_does_not_mean_ingestion_stopped(self):
        payload = self.native()
        next(c for c in payload["pipeline"]["checks"] if c["id"] == "senate_efd")["status"] = (
            "stalled"
        )
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertEqual(row["state"], "warn")
        self.assertTrue(row["ingestionAlive"])

    def test_st_observation_age_rounds_once_before_freshness_gate(self):
        # 120.9s must be stored and compared as 121 (ceil), not truncated
        # to 120 and then still treated as stale against a 120s cap.
        stamp = NOW - 120.9
        payload = st_payload(
            mango={
                "reachable": True,
                "checkedAt": datetime.fromtimestamp(stamp, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        row = api_health.map_st_live(payload, 200, NOW)
        self.assertEqual(row["observationAgeSeconds"], 121)
        self.assertEqual(row["reason"], "observation_stale")
        self.assertEqual(row["state"], "unknown")

    def test_legacy_components_ok_without_aggregate_stay_ok(self):
        payload = {
            "components": {
                "house": "ok",
                "oge": "ok",
                "senate_efd": "ok",
                "ingestion": "ok",
            }
        }
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertIsNone(row["upstreamStatus"])
        self.assertEqual(row["state"], "ok")

    def test_critical_upstream_aggregate_is_fail_not_warn(self):
        payload = self.native()
        for check in payload["pipeline"]["checks"]:
            check["status"] = "ok"
        payload["status"] = "critical"
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertEqual(row["state"], "fail")
        self.assertEqual(row["reason"], "upstream_aggregate_not_ok")

    def test_cold_cache_probe_judged_against_post_fetch_clock(self):
        # ST stamps checkedAt while answering when its probe cache is
        # cold, so the stamp lands after a pre-request clock reading.
        # Judged against that reading the healthy probe is an
        # observation from the future;  judged against the clock read
        # when the answer arrived, it is fresh.
        payload = json.loads((_HERE / "fixtures/api-health/st-actual-20261118.json").read_text())
        checked = api_health.parse_iso(payload["mango"]["checkedAt"])
        fetcher = fetcher_for((200, payload, None), (200, ct_payload(), None))
        row = api_health._app_row(
            api_health.map_st_live, api_health.ST_LIVE_URL, 5, checked - 0.8, fetcher
        )
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "observation_in_future")
        row = api_health._app_row(
            api_health.map_st_live,
            api_health.ST_LIVE_URL,
            5,
            checked - 0.8,
            fetcher,
            clock=lambda: checked + 0.2,
        )
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["observationAgeSeconds"], 1)

    def test_native_aggregate_uses_the_exact_enum_not_legacy_aliases(self):
        payload = self.native()
        for check in payload["pipeline"]["checks"]:
            check["status"] = "ok"
        for alias in ("Healthy", "UP", "pass", "operational"):
            payload["status"] = alias
            row = api_health.map_ct_health(payload, 200, NOW)
            self.assertEqual(row["state"], "unknown", alias)
            self.assertEqual(row["reason"], "upstream_aggregate_not_ok", alias)
        payload["status"] = "down"
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertEqual(row["state"], "fail")

    def test_no_poll_data_shape_leaves_ingestion_unreported(self):
        # CT's no-poll-data shape carries a single polling_liveness
        # marker and no polling_* checks:  ingestion was not reported,
        # so it cannot sit in the row as reported-but-unknown.
        payload = {
            "status": "unknown",
            "pipeline": {"checks": [{"id": "polling_liveness", "status": "unknown"}]},
        }
        row = api_health.map_ct_health(payload, 200, NOW)
        ingestion = next(c for c in row["components"] if c["name"] == "ingestion")
        self.assertFalse(ingestion["reported"])
        self.assertIsNone(row["ingestionAlive"])
        self.assertEqual(row["state"], "unknown")

    def test_critical_poll_is_ingestion_failure(self):
        payload = self.native()
        next(c for c in payload["pipeline"]["checks"] if c["id"] == "polling_senate")["status"] = (
            "critical"
        )
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertEqual(row["state"], "fail")
        self.assertFalse(row["ingestionAlive"])

    def test_canary_unknown_is_a_warning_not_ingestion_failure(self):
        payload = self.native()
        next(c for c in payload["pipeline"]["checks"] if c["id"] == "senate_efd")["status"] = (
            "unknown"
        )
        row = api_health.map_ct_health(payload, 200, NOW)
        self.assertEqual(row["state"], "warn")
        self.assertTrue(row["ingestionAlive"])


if __name__ == "__main__":
    unittest.main()
