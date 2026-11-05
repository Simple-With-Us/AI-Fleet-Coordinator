#!/usr/bin/env python3
"""Tests for the account usage watchdog.  A fake GraphQL and a fake API;  no network, no host, no credential."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import watchdog as wd

UTC = dt.UTC
NOW = dt.datetime(2026, 11, 5, 12, 30, 20, tzinfo=UTC)
ACCOUNT = "aaaabbbbccccddddeeeeffff00001111"  # Synthetic.
BASE = f"/accounts/{ACCOUNT}"
WORKER = "billing-worker"
DB = "db-1"
INVENTORY = {
    "at": NOW.timestamp(),
    "workers": [WORKER, "tools-worker", "alerts-worker"],
    "details": {
        WORKER: {"limits": {"cpu_ms": 100}, "workers_dev": False, "previews_enabled": False},
        "tools-worker": {"limits": {}, "workers_dev": False, "previews_enabled": False},
        "alerts-worker": {"limits": {}, "workers_dev": True, "previews_enabled": True},
    },
    "owners": {"d1": {DB: [WORKER]}, "kv": {}, "r2": {"bucket-a": ["inbox-worker"]}},
    "ns_to_script": {"ns-1": "tools-worker"},
}
BACKGROUND = {
    "sum": {"requests": 1, "cpuTimeUs": 0},
    "dimensions": {"scriptName": "alerts-worker", "datetimeMinute": "2026-11-05T12:25:00Z"},
}
EMPTY_ACCOUNT = {**{alias: [] for alias, *_ in wd.DATASETS}, "r2s": []}


def minute(offset: int, base: dt.datetime = NOW) -> str:
    return wd.minute_key(wd.floor_minute(base) + dt.timedelta(minutes=offset))


def d1_rows(day: int = 0, per_minute: dict | None = None) -> dict:
    account: dict = {}
    if day:
        account["d1d"] = [
            {"sum": {"rowsWritten": day, "rowsRead": 0}, "dimensions": {"databaseId": DB}}
        ]
    if per_minute:
        account["d1m"] = [
            {
                "sum": {"rowsWritten": value, "rowsRead": 0},
                "dimensions": {"databaseId": DB, "datetimeMinute": key},
            }
            for key, value in per_minute.items()
        ]
    return account


class Fake:
    """Stands in for Connections and records the order of everything."""

    def __init__(self, inventory: bool = True) -> None:
        self.calls: list = []
        self.queries: list = []
        self.account: dict | Exception = {}
        self.fail: dict = {}
        self.zulip_ok = True
        self.page_result: bool | None = True
        self.beat: bool | None = True
        self.configured: bool | None = True
        self.pages: list = []
        self.posts: list = []
        self.secrets = ["GUARD_MODE"]
        self.scripts = [{"id": name} for name in INVENTORY["workers"]] if inventory else []

    def graphql(self, query, variables):
        self.queries.append((query, variables))
        if isinstance(self.account, Exception):
            raise self.account
        # The account always has some traffic in the window;  an empty one is its own test.
        return {**EMPTY_ACCOUNT, "wm": [BACKGROUND], **self.account}

    def account_id(self):
        return ACCOUNT

    def api(self, method, path, body=None):
        self.calls.append((method, path.replace(BASE, ""), body))
        for pattern, error in self.fail.items():
            if re.search(pattern, f"{method} {path}"):
                raise error
        if path.endswith("/secrets") and method == "GET":
            return [{"name": name, "type": "secret_text"} for name in self.secrets]
        if path.endswith("/deployments") and method == "GET":
            return {
                "deployments": [{"versions": [{"version_id": "old-version", "percentage": 100}]}]
            }
        if path.endswith("/schedules") and method == "GET":
            return {"schedules": [{"cron": "*/5 * * * *", "created_on": "x"}]}
        if path.endswith("/subdomain") and method == "GET":
            return {"enabled": True, "previews_enabled": False}
        if path.endswith("/workers/domains") and method == "GET":
            return [
                {"id": "dom-1", "hostname": "expenses.example", "service": WORKER, "zone_id": "z1"},
                {"id": "dom-2", "hostname": "other.example", "service": "other", "zone_id": "z1"},
            ]
        if path.endswith("/workers/scripts") and method == "GET":
            return self.scripts
        return None

    def zulip(self, text):
        self.calls.append(("ZULIP", text, None))
        self.posts.append(text)
        return self.zulip_ok

    def page(self, title, text):
        self.calls.append(("PAGE", title, None))
        self.pages.append(title)
        return self.page_result

    def page_configured(self):
        return self.configured

    def heartbeat(self):
        self.calls.append(("HEARTBEAT", "", None))
        return self.beat

    def writes(self):
        return [call for call in self.calls if call[0] in ("PUT", "POST", "DELETE")]


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = wd.load_config()
        self.cfg["account_id"] = ACCOUNT  # What a run resolves from the environment or Infisical.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = wd.Store(Path(self.tmp.name))
        self.fake = Fake()
        self.state = self.store.load()
        self.state["inventory"] = json.loads(json.dumps(INVENTORY))
        self.state["daily_check_day"] = NOW.strftime("%Y-%m-%d")

    def poll(self, account=None, at=NOW, mode="enforce", profile="paid") -> int:
        if account is not None:
            self.fake.account = account
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = wd.Watchdog(self.cfg, self.fake, self.store, self.state, at, mode, profile).run()
        self.last_output = out.getvalue()
        return code

    def events(self, name=None):
        rows: list = []
        for path in (self.store.events_path.with_suffix(".jsonl.1"), self.store.events_path):
            if path.exists():
                rows += [json.loads(line) for line in path.read_text().splitlines()]
        return [row for row in rows if name is None or row["event"] == name]


class QueryTests(Base):
    def test_one_query_covers_every_dataset(self):
        self.poll({})
        self.assertEqual(len(self.fake.queries), 1)
        query = self.fake.queries[0][0]
        for dataset in (
            "workersInvocationsAdaptive",
            "durableObjectsInvocationsAdaptiveGroups",
            "durableObjectsPeriodicGroups",
            "d1AnalyticsAdaptiveGroups",
            "kvOperationsAdaptiveGroups",
            "r2OperationsAdaptiveGroups",
            "r2StorageAdaptiveGroups",
        ):
            self.assertIn(dataset, query)

    def test_windows_start_at_midnight_utc_and_skip_the_lagging_minutes(self):
        variables = wd.query_variables(self.cfg, NOW)
        self.assertEqual(variables["dayStart"], "2026-11-05T00:00:00Z")
        self.assertEqual(variables["monthStart"], "2026-11-01T00:00:00Z")
        self.assertEqual(variables["rateEnd"], "2026-11-05T12:28:00Z")
        self.assertEqual(variables["rateStart"], "2026-11-05T12:23:00Z")

    def test_r2_classes(self):
        self.assertEqual(wd.r2_class("PutObject"), "r2.class_a")
        self.assertEqual(wd.r2_class("ListObjects"), "r2.class_a")
        self.assertEqual(wd.r2_class("SomethingNew"), "r2.class_a")
        self.assertEqual(wd.r2_class("GetObject"), "r2.class_b")
        self.assertEqual(wd.r2_class("HeadBucket"), "r2.class_b")
        self.assertEqual(wd.r2_class("ReportUsageSummary"), "r2.class_b")
        self.assertIsNone(wd.r2_class("DeleteObject"))

    def test_parse_units(self):
        usage = wd.parse_usage(
            {
                "wd": [
                    {"sum": {"requests": 5, "cpuTimeUs": 2000}, "dimensions": {"scriptName": "w"}}
                ],
                "pd": [
                    {
                        "sum": {"activeTime": 1_000_000, "rowsRead": 3, "rowsWritten": 4},
                        "dimensions": {"namespaceId": "ns-1"},
                    }
                ],
                "kvd": [
                    {
                        "sum": {"requests": 7},
                        "dimensions": {"namespaceId": "k", "actionType": "list"},
                    }
                ],
                "r2d": [
                    {
                        "sum": {"requests": 2},
                        "dimensions": {"bucketName": "", "actionType": "ListBuckets"},
                    }
                ],
                "r2s": [
                    {
                        "max": {"payloadSize": 2e9, "metadataSize": 0},
                        "dimensions": {"bucketName": "b", "datetime": "new"},
                    },
                    {
                        "max": {"payloadSize": 9e9, "metadataSize": 0},
                        "dimensions": {"bucketName": "b", "datetime": "old"},
                    },
                ],
            },
            {"ns-1": "script"},
        )
        self.assertEqual(usage["day"]["workers.cpu_ms"]["w"], 2)
        self.assertAlmostEqual(usage["day"]["do.gb_s"]["script"], 0.128)
        self.assertEqual(usage["day"]["do.rows_written"]["script"], 4)
        self.assertEqual(usage["day"]["kv.list"]["k"], 7)
        self.assertEqual(usage["day"]["r2.class_a"]["(account)"], 2)
        self.assertEqual(usage["level"]["r2.storage_gb"]["b"], 2)


class ConfigTests(Base):
    def test_free_profile_is_80_percent_of_the_caps(self):
        free = self.cfg["profiles"]["free"]["account"]
        for metric, spec in self.cfg["metrics"].items():
            if "free_cap_day" in spec:
                self.assertEqual(free[metric]["day"], spec["free_cap_day"] * 0.8, metric)
                self.assertEqual(free[metric]["rate_5m"], spec["free_cap_day"] * 0.1, metric)
            if "free_cap_month" in spec:
                self.assertEqual(free[metric]["month"], spec["free_cap_month"] * 0.8, metric)
            if "free_cap_level" in spec:
                self.assertEqual(free[metric]["level"], spec["free_cap_level"] * 0.8, metric)

    def test_paid_thresholds_sit_well_above_the_measured_peaks_and_carry_dollars(self):
        for metric, rule in self.cfg["profiles"]["paid"]["per_resource"].items():
            self.assertGreaterEqual(rule["day"], 5 * rule["measured_peak_day"], metric)
            self.assertGreaterEqual(rule["rate_5m"], 5 * rule["measured_peak_5m"], metric)
            self.assertAlmostEqual(
                rule["usd_day"],
                wd.usd_estimate(self.cfg, metric, rule["day"]),
                places=2,
                msg=metric,
            )
            self.assertAlmostEqual(
                rule["usd_rate_hour"],
                wd.usd_estimate(self.cfg, metric, rule["rate_5m"] * 12),
                places=3,
                msg=metric,
            )

    def test_paid_covers_every_metric(self):
        paid = self.cfg["profiles"]["paid"]
        self.assertEqual(set(paid["per_resource"]) | set(paid["account"]), set(self.cfg["metrics"]))

    def test_validate_refuses_an_unknown_metric(self):
        bad = json.loads(json.dumps(self.cfg))
        bad["profiles"]["paid"]["account"]["nope"] = {"day": 1}
        self.assertTrue(wd.validate_config(bad))

    def test_thresholds_prints(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wd.print_thresholds(self.cfg)
        self.assertIn("D1 rows written", out.getvalue())
        self.assertIn("$1.00", out.getvalue())


class ObserveTests(Base):
    def test_below_everything_is_silent(self):
        self.assertEqual(self.poll(d1_rows(day=1000)), 0)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.fake.writes(), [])

    def test_day_breach_in_observe_alerts_and_never_writes(self):
        self.poll(d1_rows(day=2_000_000), mode="observe")
        self.assertEqual(len(self.fake.posts), 1)
        self.assertIn("D1 rows written", self.fake.posts[0])
        self.assertIn("old-version", self.fake.posts[0])
        self.assertIn("  ", self.fake.posts[0])
        self.assertEqual(self.fake.pages, ["Cloudflare usage over a limit"])
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.state["trips"], {})
        would = self.events("would_trip")
        self.assertEqual(would[0]["worker"], WORKER)
        self.assertEqual(would[0]["previous_version"], "old-version")

    def test_rate_breach_uses_the_sum_of_the_window(self):
        minutes = {minute(-3 - i): 5000 for i in range(5)}
        self.poll(d1_rows(per_minute=minutes), mode="observe")
        self.assertIn("in 5 minutes", self.fake.posts[0])

    def test_alert_repeats_only_after_the_realert_window(self):
        account = d1_rows(day=2_000_000)
        self.poll(account, mode="observe")
        self.poll(account, at=NOW + dt.timedelta(minutes=1), mode="observe")
        self.assertEqual(len(self.fake.posts), 1)
        self.poll(account, at=NOW + dt.timedelta(minutes=61), mode="observe")
        self.assertEqual(len(self.fake.posts), 2)

    def test_undelivered_alert_retries_and_exits_nonzero(self):
        self.fake.zulip_ok = False
        self.fake.page_result = False
        account = d1_rows(day=2_000_000)
        self.assertEqual(self.poll(account, mode="observe"), 1)
        self.fake.zulip_ok = True
        self.poll(account, at=NOW + dt.timedelta(minutes=1), mode="observe")
        self.assertEqual(len(self.fake.posts), 2)

    def test_page_alone_counts_as_delivered(self):
        self.fake.zulip_ok = False
        self.assertEqual(self.poll(d1_rows(day=2_000_000), mode="observe"), 0)


class EnforceTests(Base):
    def test_alert_comes_before_the_off_switch_and_logs_the_previous_version(self):
        self.poll(d1_rows(day=2_000_000))
        kinds = [call[0] for call in self.fake.calls]
        self.assertLess(kinds.index("ZULIP"), kinds.index("PUT"))
        self.assertLess(kinds.index("PAGE"), kinds.index("PUT"))
        put = next(call for call in self.fake.calls if call[0] == "PUT")
        self.assertEqual(put[1], f"/workers/scripts/{WORKER}/secrets")
        self.assertEqual(put[2], {"name": "GUARD_MODE", "type": "secret_text", "text": "off"})
        action = next(row for row in self.events("action") if row["action"] == "guard_mode_off")
        self.assertEqual(action["previous_version"], "old-version")
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)

    def test_live_version_is_labelled_for_the_deployers(self):
        self.poll(d1_rows(day=2_000_000))
        post = next(
            call
            for call in self.fake.calls
            if call[0] == "POST" and call[1].endswith("/deployments")
        )
        self.assertTrue(post[2]["annotations"]["workers/message"].startswith("GUARD-TRIPPED"))
        self.assertEqual(post[2]["versions"], [{"version_id": "old-version", "percentage": 100}])

    def test_a_breach_with_no_owner_only_alerts(self):
        account = {
            "r2d": [
                {
                    "sum": {"requests": 900_000},
                    "dimensions": {"bucketName": "s3-client-bucket", "actionType": "PutObject"},
                }
            ]
        }
        self.poll(account)
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.writes(), [])

    def test_storage_never_trips(self):
        account = {
            "r2s": [
                {
                    "max": {"payloadSize": 20e9, "metadataSize": 0},
                    "dimensions": {"bucketName": "bucket-a", "datetime": "x"},
                }
            ]
        }
        self.poll(account)
        self.assertIn("R2 storage GB", self.fake.posts[0])
        self.assertEqual(self.fake.writes(), [])

    def test_unknown_script_never_trips(self):
        account = {
            "wd": [
                {
                    "sum": {"requests": 9_000_000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": "__unknown__"},
                }
            ]
        }
        self.poll(account)
        self.assertEqual(self.fake.writes(), [])

    def test_trip_caps(self):
        account = {
            "wd": [
                {"sum": {"requests": 9_000_000, "cpuTimeUs": 0}, "dimensions": {"scriptName": name}}
                for name in (WORKER, "tools-worker", "alerts-worker")
            ]
        }
        self.poll(account)
        self.assertEqual(len(self.state["trips"]), 1, "one trip per run")
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len(self.state["trips"]), 2)
        self.state["trips_by_day"][NOW.strftime("%Y-%m-%d")] = 3
        self.poll(at=NOW + dt.timedelta(minutes=2))
        self.assertEqual(len(self.state["trips"]), 2, "the daily cap holds")
        self.assertTrue(any("Trip cap reached" in post for post in self.fake.posts))

    def test_account_breach_is_alert_only_on_paid(self):
        self.cfg["profiles"]["paid"]["account"]["d1.rows_written"] = {"day": 10}
        self.poll(d1_rows(day=100))
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.writes(), [])

    def test_account_breach_on_free_trips_the_top_contributor(self):
        account = {
            "wd": [
                {
                    "sum": {"requests": 70_000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": "tools-worker"},
                },
                {
                    "sum": {"requests": 15_000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": "alerts-worker"},
                },
            ]
        }
        self.poll(account, profile="free")
        self.assertIn("Worker requests on (account)", self.fake.posts[0])
        self.assertIn("Largest contributor:  tools-worker", self.fake.posts[0])
        self.assertEqual(list(self.state["trips"]), ["tools-worker"])

    def test_free_profile_uses_month_to_date_for_r2(self):
        account = {
            "r2o": [
                {
                    "sum": {"requests": 850_000},
                    "dimensions": {"bucketName": "bucket-a", "actionType": "PutObject"},
                }
            ]
        }
        self.poll(account, profile="free", mode="observe")
        self.assertIn("this month", self.fake.posts[0])


class EscalationTests(Base):
    def trip_then(self, per_minute_after: int, polls: int = 6) -> None:
        self.poll(d1_rows(per_minute={minute(-3 - i): 5000 for i in range(5)}))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        for count in range(1, polls + 1):
            at = NOW + dt.timedelta(minutes=count)
            # Only minutes that have left the lag window exist.  Those before the trip stay high.
            series = {}
            for back in range(3, 8):
                key = minute(-back, at)
                series[key] = per_minute_after if key > minute(0) else 5000
            self.poll(d1_rows(per_minute=series), at=at)

    def test_rate_that_falls_stays_at_stage_2(self):
        self.trip_then(per_minute_after=10)
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.assertFalse([call for call in self.fake.writes() if call[1].endswith("/schedules")])

    def test_rate_that_holds_reaches_the_fallback_stop(self):
        self.trip_then(per_minute_after=5000)
        trip = self.state["trips"][WORKER]
        self.assertEqual(trip["stage"], 3)
        writes = self.fake.writes()
        self.assertIn(("PUT", f"/workers/scripts/{WORKER}/schedules", []), writes)
        self.assertIn(
            (
                "POST",
                f"/workers/scripts/{WORKER}/subdomain",
                {"enabled": False, "previews_enabled": False},
            ),
            writes,
        )
        self.assertIn(("DELETE", "/workers/domains/dom-1", None), writes)
        self.assertNotIn(("DELETE", "/workers/domains/dom-2", None), writes)
        self.assertEqual(trip["previous"]["schedules"][0]["cron"], "*/5 * * * *")
        self.assertEqual([d["id"] for d in trip["previous"]["domains"]], ["dom-1"])
        self.assertTrue(
            all(row["previous_version"] == "old-version" for row in self.events("action"))
        )
        self.assertTrue(any("Fallback stop on" in post for post in self.fake.posts))

    def test_minutes_from_before_the_trip_never_escalate(self):
        # Polls right after the trip still read only pre-trip minutes because of the lag.
        self.poll(d1_rows(per_minute={minute(-3 - i): 5000 for i in range(5)}))
        for count in (1, 2, 3):
            at = NOW + dt.timedelta(minutes=count)
            self.poll(
                d1_rows(
                    per_minute={
                        minute(-back, at): 5000
                        for back in range(3, 8)
                        if minute(-back, at) <= minute(0)
                    }
                ),
                at=at,
            )
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)

    def test_day_trip_measures_against_half_the_pre_trip_rate(self):
        before = {minute(-3 - i): 100 for i in range(5)}
        self.poll({**d1_rows(day=2_000_000), **d1_rows(per_minute=before)})
        trip = self.state["trips"][WORKER]
        self.assertAlmostEqual(trip["target_per_minute"], max(50, 1_000_000 / 1440))

    def test_failed_off_switch_goes_straight_to_the_fallback(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(400, [10215])
        self.poll(d1_rows(day=2_000_000))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 3)
        self.assertTrue(
            any(row["action"] == "guard_mode_off" for row in self.events("action_failed"))
        )

    def test_partial_fallback_failure_is_reported(self):
        self.fake.fail[r"PUT .*/schedules"] = wd.ApiError(500, [1])
        self.trip_then(per_minute_after=5000)
        self.assertTrue(any("1 step(s) failed" in post for post in self.fake.posts))
        self.assertTrue(
            any(call[0] == "DELETE" for call in self.fake.calls), "other steps still ran"
        )

    def test_unreadable_previous_state_retries_without_changing_anything(self):
        self.fake.fail[r"GET .*/schedules"] = wd.ApiError(500, [1])
        self.trip_then(per_minute_after=5000)
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.assertFalse([call for call in self.fake.writes() if call[1].endswith("/schedules")])
        failures = [post for post in self.fake.posts if "Fallback stop failed" in post]
        self.assertEqual(len(failures), 1, "one message an hour, not one a minute")

    def test_escalation_takes_two_polls_that_hold_minutes_from_after_the_trip(self):
        self.trip_then(per_minute_after=5000, polls=4)
        self.assertEqual(self.state["trips"][WORKER]["polls"], 1)
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2, "one reading is not enough")
        at = NOW + dt.timedelta(minutes=5)
        self.poll(
            d1_rows(
                per_minute={
                    minute(-back, at): (5000 if minute(-back, at) > minute(0) else 1)
                    for back in range(3, 8)
                }
            ),
            at=at,
        )
        self.assertEqual(self.state["trips"][WORKER]["stage"], 3)

    def test_a_failed_stop_step_runs_again_until_it_lands(self):
        self.fake.fail[r"PUT .*/schedules"] = wd.ApiError(500, [1])
        self.trip_then(per_minute_after=5000, polls=5)
        trip = self.state["trips"][WORKER]
        self.assertEqual(trip["pending"], ["empty_schedules"])
        recorded = list(trip["previous"]["schedules"])
        self.fake.fail.clear()
        self.poll({}, at=NOW + dt.timedelta(minutes=6))
        self.assertEqual(trip["pending"], [])
        self.assertIn(("PUT", f"/workers/scripts/{WORKER}/schedules", []), self.fake.writes())
        self.assertEqual(trip["previous"]["schedules"], recorded, "re-arm still has the original")
        subdomain_writes = [c for c in self.fake.writes() if c[1].endswith("/subdomain")]
        self.assertEqual(len(subdomain_writes), 1, "what worked does not run again")

    def test_a_stop_step_that_stays_failed_alerts_hourly_not_each_minute(self):
        self.fake.fail[r"PUT .*/schedules"] = wd.ApiError(500, [1])
        self.trip_then(per_minute_after=5000, polls=8)
        stuck = [post for post in self.fake.posts if "Fallback stop is incomplete" in post]
        self.assertEqual(len(stuck), 1)

    def test_a_failed_off_switch_is_retried_and_the_label_follows_it(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(500, [1])
        self.poll(d1_rows(day=2_000_000))
        trip = self.state["trips"][WORKER]
        self.assertFalse(trip["guard_set"])
        self.assertTrue(trip["marked"], "the label also covers a stop with no secret")
        labels = [c for c in self.fake.writes() if c[1].endswith("/deployments")]
        self.assertEqual(len(labels), 1)
        self.fake.fail.clear()
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.assertTrue(trip["guard_set"])
        labels = [c for c in self.fake.writes() if c[1].endswith("/deployments")]
        self.assertEqual(len(labels), 2, "the secret's new deployment cleared the first label")

    def test_a_passing_fault_on_the_off_switch_does_not_take_hostnames_down(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(503, [])
        self.poll(d1_rows(day=2_000_000))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.fake.fail.clear()
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.assertTrue(self.state["trips"][WORKER]["guard_set"])
        self.assertFalse([c for c in self.fake.writes() if c[1].endswith("/schedules")])

    def test_a_fault_that_keeps_failing_reaches_the_fallback(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(503, [])
        self.poll(d1_rows(day=2_000_000))
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.poll(at=NOW + dt.timedelta(minutes=2))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 3)

    def test_a_failed_label_is_retried(self):
        self.fake.fail[r"POST .*/deployments"] = wd.ApiError(500, [1])
        self.poll(d1_rows(day=2_000_000))
        trip = self.state["trips"][WORKER]
        self.assertFalse(trip["marked"])
        self.fake.fail.clear()
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.assertTrue(trip["marked"])

    def test_the_label_repeats_a_gradual_split(self):
        original = self.fake.api
        split = [
            {"version_id": "a", "percentage": 90},
            {"version_id": "b", "percentage": 10},
        ]

        def api(method, path, body=None):
            if path.endswith("/deployments") and method == "GET":
                self.fake.calls.append((method, path.replace(BASE, ""), body))
                return {"deployments": [{"versions": split}]}
            return original(method, path, body)

        self.fake.api = api
        self.poll(d1_rows(day=2_000_000))
        post = next(c for c in self.fake.writes() if c[1].endswith("/deployments"))
        self.assertEqual(post[2]["versions"], split)
        self.assertTrue(self.state["trips"][WORKER]["marked"])

    def test_observe_after_a_trip_does_not_escalate(self):
        self.poll(d1_rows(per_minute={minute(-3 - i): 5000 for i in range(5)}))
        at = NOW + dt.timedelta(minutes=6)
        self.poll(
            d1_rows(per_minute={minute(-back, at): 5000 for back in range(3, 8)}),
            at=at,
            mode="observe",
        )
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)


class PollFailureTests(Base):
    def test_failed_poll_is_quiet_for_five_minutes_then_alerts_and_never_trips(self):
        self.fake.account = wd.PollError("graphql-http-503")
        self.assertEqual(self.poll(), 1)
        self.poll(at=NOW + dt.timedelta(minutes=4))
        self.assertEqual(self.fake.posts, [])
        self.poll(at=NOW + dt.timedelta(minutes=5))
        self.assertEqual(len(self.fake.posts), 1)
        self.assertIn("blind", self.fake.posts[0])
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.state["trips"], {})

    def test_blind_alert_repeats_hourly_and_recovery_is_announced_once(self):
        self.fake.account = wd.PollError("x")
        self.poll()
        self.poll(at=NOW + dt.timedelta(minutes=5))
        self.poll(at=NOW + dt.timedelta(minutes=6))
        self.assertEqual(len(self.fake.posts), 1)
        self.poll(at=NOW + dt.timedelta(minutes=66))
        self.assertEqual(len(self.fake.posts), 2)
        self.poll({}, at=NOW + dt.timedelta(minutes=67))
        self.assertIn("can read again", self.fake.posts[-1])
        self.poll({}, at=NOW + dt.timedelta(minutes=68))
        self.assertEqual(len(self.fake.posts), 3)

    def test_short_failure_needs_no_recovery_message(self):
        self.fake.account = wd.PollError("x")
        self.poll()
        self.poll({}, at=NOW + dt.timedelta(minutes=1))
        self.assertEqual(self.fake.posts, [])
        self.assertNotIn("poll_failed_since", self.state)

    def test_heartbeat_only_after_a_good_poll(self):
        self.fake.account = wd.PollError("x")
        self.poll()
        self.assertFalse([call for call in self.fake.calls if call[0] == "HEARTBEAT"])
        self.poll({}, at=NOW + dt.timedelta(minutes=1))
        self.assertEqual(len([call for call in self.fake.calls if call[0] == "HEARTBEAT"]), 1)

    def test_no_heartbeat_configured_is_tolerated(self):
        self.fake.beat = None
        self.assertEqual(self.poll({}), 0)

    def test_an_empty_analytics_answer_is_blind_not_all_clear(self):
        original = self.fake.graphql
        self.fake.graphql = lambda query, variables: {**original(query, variables), "wm": []}
        self.assertEqual(self.poll({}), 1)
        self.assertFalse([call for call in self.fake.calls if call[0] == "HEARTBEAT"])
        self.poll({}, at=NOW + dt.timedelta(minutes=5))
        self.assertIn("analytics-empty", self.fake.posts[0])

    def test_a_surprise_exception_reaches_an_owner_and_never_trips(self):
        self.fake.heartbeat = lambda: 1 / 0
        self.assertEqual(self.poll({}), 1)
        self.poll(at=NOW + dt.timedelta(minutes=5))
        self.assertIn("internal-ZeroDivisionError", self.fake.posts[0])
        self.assertEqual(self.fake.writes(), [])


class InventoryAgeTests(Base):
    def rows(self, script="brand-new-worker"):
        return {
            "wd": [
                {
                    "sum": {"requests": 9_000_000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": script},
                }
            ]
        }

    def test_a_worker_newer_than_the_inventory_can_still_trip(self):
        self.poll(self.rows())
        self.assertIn("brand-new-worker", self.state["trips"])

    def test_a_binding_newer_than_the_inventory_refreshes_it_and_then_trips(self):
        original = self.fake.api
        self.state["inventory"]["at"] = NOW.timestamp() - 3600

        def api(method, path, body=None):
            if path.endswith(f"{WORKER}/settings"):
                return {"bindings": [{"type": "d1", "id": "db-new"}]}
            if path.endswith("/durable_objects/namespaces"):
                return []
            return original(method, path, body)

        self.fake.api = api
        account = {
            "d1d": [
                {
                    "sum": {"rowsWritten": 2_000_000, "rowsRead": 0},
                    "dimensions": {"databaseId": "db-new"},
                }
            ]
        }
        self.poll(account)
        self.assertEqual(self.state["inventory"]["owners"]["d1"]["db-new"], [WORKER])
        self.assertIn(WORKER, self.state["trips"])

    def test_an_unbound_resource_does_not_reread_the_inventory_every_minute(self):
        self.state["inventory"]["at"] = NOW.timestamp() - 3600
        account = {
            "r2d": [
                {
                    "sum": {"requests": 900_000},
                    "dimensions": {"bucketName": "s3-client-bucket", "actionType": "PutObject"},
                }
            ]
        }
        self.poll(account)
        self.poll(at=NOW + dt.timedelta(minutes=1))
        reads = [c for c in self.fake.calls if c[0] == "GET" and c[1] == "/workers/scripts"]
        self.assertEqual(len(reads), 1)


class DailyCheckTests(Base):
    def setUp(self):
        super().setUp()
        self.state.pop("daily_check_day")

    def check(self, registry) -> None:
        path = Path(self.tmp.name) / "registry.json"
        if registry is not None:
            path.write_text(json.dumps(registry))
        dog = wd.Watchdog(self.cfg, self.fake, self.store, self.state, NOW, "observe", "paid")
        dog.beat = True
        with contextlib.redirect_stdout(io.StringIO()):
            dog.daily_check_if_due(self.state["inventory"], path)

    def test_absent_registry_is_tolerated(self):
        self.check(None)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.state["daily_check_day"], NOW.strftime("%Y-%m-%d"))
        self.assertEqual(self.events("daily_check")[0]["drift"], "no-registry")

    def test_drift_is_reported_once(self):
        registry = {
            "workers": {
                WORKER: {"limits": {"cpu_ms": 50}},
                "alerts-worker": {"preview_urls": False, "workers_dev": False},
                "gone-worker": {},
            }
        }
        self.check(registry)
        text = self.fake.posts[0]
        self.assertIn("tools-worker is live but not in the registry", text)
        self.assertIn("gone-worker is in the registry but not live", text)
        self.assertIn("limits.cpu_ms is 100, the registry says 50", text)
        self.assertIn("alerts-worker has preview URLs on", text)
        self.assertIn("alerts-worker has workers.dev on", text)
        self.assertEqual(self.fake.pages, [], "drift is not a page")
        self.check(registry)
        self.assertEqual(len(self.fake.posts), 1)

    def test_preview_urls_count_even_when_a_registry_row_does_not_name_them(self):
        registry = {"workers": {"alerts-worker": {"status": "auto", "limits": {}}}}
        self.check(registry)
        self.assertIn("alerts-worker has preview URLs on", self.fake.posts[0])

    def test_list_shaped_registry_reads(self):
        path = Path(self.tmp.name) / "list.json"
        path.write_text(json.dumps([{"name": "a", "status": "auto"}]))
        self.assertEqual(wd.load_registry(path), {"a": {"name": "a", "status": "auto"}})

    def test_broken_registry_reads_as_absent(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{nope")
        self.assertIsNone(wd.load_registry(path))

    def test_missing_page_path_and_heartbeat_are_named(self):
        self.fake.configured = False
        dog = wd.Watchdog(self.cfg, self.fake, self.store, self.state, NOW, "observe", "paid")
        dog.beat = None
        with contextlib.redirect_stdout(io.StringIO()):
            dog.daily_check_if_due(self.state["inventory"], Path(self.tmp.name) / "none.json")
        self.assertIn("No direct page path", self.fake.posts[0])
        self.assertIn("No dead-man heartbeat", self.fake.posts[0])
        self.assertEqual(self.fake.pages, [], "the check must not page anyone")

    def test_inventory_maps_bindings_to_owners(self):
        self.state["inventory"] = {}
        self.fake.api_settings = None
        original = self.fake.api

        def api(method, path, body=None):
            if path.endswith(f"{WORKER}/settings"):
                return {
                    "limits": {"cpu_ms": 100},
                    "bindings": [
                        {"type": "d1", "name": "DB", "id": DB},
                        {"type": "kv_namespace", "name": "K", "namespace_id": "kv-1"},
                        {"type": "r2_bucket", "name": "B", "bucket_name": "bucket-a"},
                        {"type": "secret_text", "name": "S"},
                    ],
                }
            if path.endswith("/durable_objects/namespaces"):
                return [{"id": "ns-1", "script": "tools-worker"}]
            return original(method, path, body)

        self.fake.api = api
        self.poll({})
        inventory = self.state["inventory"]
        self.assertEqual(inventory["owners"]["d1"][DB], [WORKER])
        self.assertEqual(inventory["owners"]["kv"]["kv-1"], [WORKER])
        self.assertEqual(inventory["owners"]["r2"]["bucket-a"], [WORKER])
        self.assertEqual(inventory["ns_to_script"], {"ns-1": "tools-worker"})

    def test_failed_inventory_keeps_the_last_good_one(self):
        self.state["inventory"]["at"] = 0
        self.fake.fail[r"GET .*/workers/scripts$"] = wd.ApiError(500, [])
        self.poll({})
        self.assertEqual(self.state["inventory"]["workers"], INVENTORY["workers"])


class RearmTests(Base):
    def rearm(self, apply: bool) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return wd.rearm(self.cfg, self.fake, self.store, self.state, WORKER, apply)

    def tripped(self) -> None:
        self.state["trips"][WORKER] = {
            "stage": 3,
            "previous_version": "old-version",
            "previous": {
                "schedules": [{"cron": "*/15 * * * *", "created_on": "x"}],
                "subdomain": {"enabled": True, "previews_enabled": False},
                "domains": [
                    {
                        "id": "dom-1",
                        "hostname": "h",
                        "service": WORKER,
                        "zone_id": "z",
                        "zone_name": "n",
                    }
                ],
            },
        }

    def test_dry_run_changes_nothing(self):
        self.tripped()
        self.assertEqual(self.rearm(False), 0)
        self.assertEqual(self.fake.calls, [])
        self.assertIn(WORKER, self.state["trips"])

    def test_apply_restores_what_was_recorded(self):
        self.tripped()
        self.assertEqual(self.rearm(True), 0)
        self.assertIn(
            ("DELETE", f"/workers/scripts/{WORKER}/secrets/GUARD_MODE", None), self.fake.calls
        )
        self.assertIn(
            ("PUT", f"/workers/scripts/{WORKER}/schedules", [{"cron": "*/15 * * * *"}]),
            self.fake.calls,
        )
        self.assertIn(
            (
                "POST",
                f"/workers/scripts/{WORKER}/subdomain",
                {"enabled": True, "previews_enabled": False},
            ),
            self.fake.calls,
        )
        self.assertIn(
            ("PUT", "/workers/domains", {"hostname": "h", "service": WORKER, "zone_id": "z"}),
            self.fake.calls,
        )
        self.assertNotIn(WORKER, self.state["trips"])

    def test_a_failed_step_keeps_the_trip(self):
        self.tripped()
        self.fake.fail[r"PUT .*/schedules"] = wd.ApiError(500, [])
        self.assertEqual(self.rearm(True), 1)
        self.assertIn(WORKER, self.state["trips"])

    def test_not_tripped(self):
        self.assertEqual(self.rearm(True), 2)

    def test_a_secret_that_is_already_gone_does_not_block_it_and_the_label_is_cleared(self):
        self.tripped()
        self.fake.secrets = []
        self.assertEqual(self.rearm(True), 0)
        self.assertFalse([c for c in self.fake.calls if c[0] == "DELETE" and "secrets" in c[1]])
        post = next(c for c in self.fake.writes() if c[1].endswith("/deployments"))
        self.assertFalse(post[2]["annotations"]["workers/message"].startswith("GUARD-TRIPPED"))
        self.assertNotIn(WORKER, self.state["trips"])

    def test_an_unreadable_secret_list_keeps_the_trip(self):
        self.tripped()
        self.fake.fail[r"GET .*/secrets$"] = wd.ApiError(500, [])
        self.assertEqual(self.rearm(True), 1)
        self.assertIn(WORKER, self.state["trips"])

    def test_day_total_does_not_trip_straight_back_but_a_rate_still_does(self):
        self.tripped()
        with contextlib.redirect_stdout(io.StringIO()):
            wd.rearm(self.cfg, self.fake, self.store, self.state, WORKER, True, now=NOW)
        at = NOW + dt.timedelta(minutes=1)
        self.poll(d1_rows(day=2_000_000), at=at)
        self.assertNotIn(WORKER, self.state["trips"])
        self.assertTrue(any("D1 rows written" in post for post in self.fake.posts), "still alerts")
        at = NOW + dt.timedelta(minutes=2)
        self.poll(
            {
                **d1_rows(day=2_000_000),
                **d1_rows(per_minute={minute(-3 - i, at): 5000 for i in range(5)}),
            },
            at=at,
        )
        self.assertIn(WORKER, self.state["trips"])

    def test_day_total_trips_again_the_next_day(self):
        self.tripped()
        with contextlib.redirect_stdout(io.StringIO()):
            wd.rearm(self.cfg, self.fake, self.store, self.state, WORKER, True, now=NOW)
        tomorrow = NOW + dt.timedelta(days=1)
        self.state["daily_check_day"] = tomorrow.strftime("%Y-%m-%d")
        self.state["inventory"]["at"] = tomorrow.timestamp()
        self.poll(d1_rows(day=2_000_000), at=tomorrow)
        self.assertIn(WORKER, self.state["trips"])


class ConnectionTests(Base):
    TOKEN = "credential-value-never-logged"

    def runner(self, values: dict, codes: dict | None = None):
        self.commands = []

        def run(command, **kwargs):
            self.commands.append((command, kwargs))
            if command[0] == "fleet-secrets":
                name = command[2]
                code = (codes or {}).get(name, 0 if name in values else 3)
                return SimpleNamespace(returncode=code, stdout=values.get(name, ""))
            return SimpleNamespace(returncode=0, stdout="")

        return run

    def opener(self, responses: list):
        self.requests = []

        def open_(request, timeout=None):
            self.requests.append(request)
            answer = responses.pop(0)
            if isinstance(answer, Exception):
                raise answer

            class Response:
                status = 200

                def read(self_inner, size=-1):
                    return answer

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return Response()

        return open_

    def conn(self, values, responses=(), codes=None, env=None):
        return wd.Connections(
            self.cfg,
            runner=self.runner(values, codes),
            opener=self.opener(list(responses)),
            env=env or {},
        )

    def test_secrets_come_from_fleet_secrets_by_the_named_key_and_folder(self):
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN})
        self.assertEqual(conn.token(), self.TOKEN)
        command = self.commands[0][0]
        self.assertEqual(
            command,
            [
                "fleet-secrets",
                "get",
                "CF_USAGE_WATCHDOG_API_TOKEN",
                "--path",
                "/cf-usage-watchdog",
                "--project",
                "ai-fleet-coordinator",
            ],
        )

    def test_absent_or_failed_credential_is_a_poll_failure(self):
        with self.assertRaises(wd.PollError):
            self.conn({}).graphql("q", {})
        with self.assertRaises(wd.PollError) as caught:
            self.conn({}, codes={"CF_USAGE_WATCHDOG_API_TOKEN": 4}).graphql("q", {})
        self.assertIn("exit-4", str(caught.exception))

    def test_graphql_errors_are_a_poll_failure_even_with_data(self):
        answer = json.dumps(
            {"data": {"viewer": {"accounts": [{}]}}, "errors": [{"message": "rate limited"}]}
        ).encode()
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [answer])
        with self.assertRaises(wd.PollError):
            conn.graphql("q", {})

    def test_a_quota_error_is_named(self):
        answer = json.dumps(
            {"data": None, "errors": [{"message": "x", "extensions": {"code": "budget"}}]}
        ).encode()
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [answer])
        with self.assertRaises(wd.PollError) as caught:
            conn.graphql("q", {})
        self.assertEqual(str(caught.exception), "graphql-errors-1-budget")

    def test_a_broken_http_answer_is_a_poll_failure_not_a_crash(self):
        conn = self.conn(
            {"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [http.client.IncompleteRead(b"")]
        )
        with self.assertRaises(wd.PollError):
            conn.graphql("q", {})

    def test_a_json_answer_that_is_not_an_object_is_a_poll_failure(self):
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [b"[1]"])
        with self.assertRaises(wd.PollError):
            conn.graphql("q", {})

    def test_graphql_sends_the_token_and_returns_the_account(self):
        answer = json.dumps(
            {"data": {"viewer": {"accounts": [{"wd": []}]}}, "errors": None}
        ).encode()
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [answer])
        self.assertEqual(conn.graphql("q", {}), {"wd": []})
        self.assertEqual(self.requests[0].get_header("Authorization"), f"Bearer {self.TOKEN}")
        self.assertEqual(self.requests[0].full_url, "https://api.cloudflare.com/client/v4/graphql")

    def test_empty_success_body_is_not_a_failure(self):
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [b""])
        self.assertIsNone(conn.api("DELETE", "/x"))

    def test_api_error_carries_no_credential(self):
        error = urllib.error.HTTPError(
            "https://x", 403, "no", {}, io.BytesIO(b'{"errors":[{"code":10000}]}')
        )
        conn = self.conn({"CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN}, [error])
        with self.assertRaises(wd.ApiError) as caught:
            conn.api("PUT", "/x", {})
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.codes, [10000])
        self.assertNotIn(self.TOKEN, str(caught.exception))

    def test_zulip_uses_simple_chat_as_the_seat_without_infisical_in_its_environment(self):
        conn = self.conn({}, env={"INFISICAL_TOKEN": "t", "HOME": "/h"})
        self.assertTrue(conn.zulip("hello"))
        command, kwargs = self.commands[0]
        self.assertEqual(
            command,
            [
                "simple-chat",
                "post",
                "--channel",
                "Work-In-Progress",
                "--topic",
                "AFC-617 cf usage watchdog",
                "-",
            ],
        )
        self.assertEqual(kwargs["input"], "hello")
        self.assertEqual(kwargs["env"]["AGENT_SEAT"], "CLAUDE")
        self.assertEqual(kwargs["env"]["SIMPLE_CHAT_REMOTE_WRITES"], "1")
        self.assertNotIn("INFISICAL_TOKEN", kwargs["env"])
        self.assertLessEqual(len(self.cfg["zulip"]["topic"]), 60)

    def test_zulip_failure_is_false_not_a_crash(self):
        def boom(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 30)

        conn = wd.Connections(self.cfg, runner=boom, env={})
        self.assertFalse(conn.zulip("hello"))

    def test_page_goes_directly_to_pushover(self):
        conn = self.conn({"PUSHOVER_APP_TOKEN": "pt", "PUSHOVER_USER_KEY": "pu"}, [b"{}"])
        self.assertTrue(conn.page("title", "text"))
        self.assertEqual(self.requests[0].full_url, "https://api.pushover.net/1/messages.json")
        self.assertIn(b"token=pt", self.requests[0].data)

    def test_page_not_configured_is_none_and_sends_nothing(self):
        conn = self.conn({})
        self.assertIsNone(conn.page("title", "text"))
        self.assertEqual(self.requests, [])
        self.assertFalse(conn.page_configured())

    def test_heartbeat_reads_its_url_once_and_keeps_it_out_of_the_log(self):
        url = "https://heartbeat.example/secret-path"
        conn = self.conn({"CF_USAGE_WATCHDOG_HEARTBEAT_URL": url}, [b"", b""])
        self.assertTrue(conn.heartbeat())
        self.assertTrue(conn.heartbeat())
        self.assertEqual(len([c for c in self.commands if c[0][0] == "fleet-secrets"]), 1)
        self.assertEqual(self.requests[0].full_url, url)

    def test_heartbeat_absent_is_none(self):
        self.assertIsNone(self.conn({}).heartbeat())

    def test_no_credential_reaches_the_log_on_a_real_failure_path(self):
        answer = json.dumps({"errors": [{"message": "nope"}]}).encode()
        conn = self.conn(
            {
                "CF_USAGE_WATCHDOG_API_TOKEN": self.TOKEN,
                "PUSHOVER_APP_TOKEN": "pushover-credential",
                "PUSHOVER_USER_KEY": "pushover-user",
            },
            [answer, b"{}"],
        )
        conn.zulip = lambda text: False
        state = self.store.load()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wd.Watchdog(self.cfg, conn, self.store, state, NOW, "enforce", "paid").run()
        logged = (
            out.getvalue() + self.store.events_path.read_text() + self.store.state_path.read_text()
        )
        for secret in (self.TOKEN, "pushover-credential", "pushover-user"):
            self.assertNotIn(secret, logged)


class StoreTests(Base):
    def test_state_is_private_and_round_trips(self):
        self.store.save({"trips": {"a": 1}})
        self.assertEqual(os.stat(self.store.state_path).st_mode & 0o777, 0o600)
        self.assertEqual(self.store.load()["trips"], {"a": 1})

    def test_a_symlinked_state_directory_is_refused(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.tmp.name)
        with self.assertRaises(ValueError):
            wd.Store(link)

    def test_events_lookup_also_reads_the_rotated_copy(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(wd, "EVENTS_MAX_BYTES", 1),
        ):
            self.store.event("before_rotation")
            self.store.event("after_rotation")
        self.assertTrue(self.store.events_path.with_suffix(".jsonl.1").exists())
        self.assertEqual(len(self.events("before_rotation")), 1)
        self.assertEqual(len(self.events("after_rotation")), 1)

    def test_the_lock_excludes_a_second_writer(self):
        with self.store.lock():
            with open(self.store.directory / ".lock", encoding="utf-8") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(self.store.directory / ".lock", encoding="utf-8") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_events_are_private_and_rotate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.store.event("one")
            self.assertEqual(os.stat(self.store.events_path).st_mode & 0o777, 0o600)
            self.store.events_path.write_bytes(b"x" * (wd.EVENTS_MAX_BYTES + 1))
            self.store.event("two")
        self.assertTrue(self.store.events_path.with_suffix(".jsonl.1").exists())
        self.assertEqual(len(self.store.events_path.read_text().splitlines()), 1)


class AccountIdTests(Base):
    def conn(self, values, env=None):
        def run(command, **kwargs):
            name = command[2]
            return SimpleNamespace(
                returncode=0 if name in values else 3, stdout=values.get(name, "")
            )

        return wd.Connections(self.cfg, runner=run, env=env or {})

    def test_no_account_id_is_committed(self):
        raw = wd.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b[0-9a-f]{32}\b", raw))
        self.assertNotIn("account_id", wd.load_config())

    def test_it_comes_from_the_environment_or_infisical(self):
        self.assertEqual(self.conn({}, {wd.ACCOUNT_ID_ENV: ACCOUNT}).account_id(), ACCOUNT)
        self.assertEqual(self.conn({wd.ACCOUNT_ID_ENV: ACCOUNT}).account_id(), ACCOUNT)

    def test_absent_or_malformed_names_the_variable(self):
        for conn in (self.conn({}), self.conn({}, {wd.ACCOUNT_ID_ENV: "../../other"})):
            with self.assertRaises(wd.PollError) as caught:
                conn.account_id()
            self.assertIn(wd.ACCOUNT_ID_ENV, str(caught.exception))

    def test_a_run_without_one_is_blind_and_alerts(self):
        del self.cfg["account_id"]
        self.fake.account_id = lambda: (_ for _ in ()).throw(wd.PollError("account-id-absent-X"))
        self.assertEqual(self.poll({}), 1)
        self.poll(at=NOW + dt.timedelta(minutes=5))
        self.assertIn("account-id-absent-X", self.fake.posts[0])
        self.assertEqual(self.fake.queries, [])

    def test_a_run_resolves_it_once_and_uses_it(self):
        del self.cfg["account_id"]
        self.poll(d1_rows(day=2_000_000))
        self.assertEqual(self.fake.queries[0][1]["account"], ACCOUNT)
        self.assertIn(WORKER, self.state["trips"])

    def test_rearm_without_one_stops_before_any_call(self):
        del self.cfg["account_id"]
        self.state["trips"][WORKER] = {"stage": 2}
        self.fake.account_id = lambda: (_ for _ in ()).throw(wd.PollError("x"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wd.rearm(self.cfg, self.fake, self.store, self.state, WORKER, True), 2)
        self.assertEqual(self.fake.calls, [])


class HttpSafetyTests(Base):
    def test_a_redirect_is_refused_and_the_token_goes_nowhere(self):
        seen: list = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("/target"):
                    seen.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
                self.end_headers()

            def log_message(self, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        conn = wd.Connections(self.cfg, env={})
        conn._token = "token-under-test"
        with mock.patch.object(wd, "API_BASE", f"http://127.0.0.1:{server.server_port}"):
            with self.assertRaises(wd.ApiError) as caught:
                conn.api("GET", "/start")
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(seen, [])

    def test_an_oversized_answer_is_refused_before_it_is_parsed(self):
        def opener(request, timeout=None):
            class Response:
                status = 200

                def read(self, size=-1):
                    return b"x" * size

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()

        conn = wd.Connections(self.cfg, opener=opener, env={})
        conn._token = "token-under-test"
        with mock.patch.object(wd, "MAX_RESPONSE_BYTES", 8):
            with self.assertRaises(wd.ApiError) as caught:
                conn.api("GET", "/x")
        self.assertEqual(caught.exception.codes, ["ResponseTooLarge"])

    def test_a_timeout_is_transient_and_a_refusal_is_not(self):
        for status in (0, 408, 429, 500, 503):
            self.assertTrue(wd.is_transient(status), status)
        for status in (200, 302, 400, 403, 404):
            self.assertFalse(wd.is_transient(status), status)

    def test_a_408_on_the_off_switch_does_not_take_hostnames_down(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(408, [])
        self.poll(d1_rows(day=2_000_000))
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.assertFalse([c for c in self.fake.writes() if c[1].endswith("/schedules")])

    def test_a_failed_page_credential_read_logs_why_and_stays_false(self):
        def run(command, **kwargs):
            return SimpleNamespace(returncode=4, stdout="")

        conn = wd.Connections(self.cfg, runner=run, env={})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(conn.page("title", "text"))
        self.assertIn("credential-fetch-exit-4", out.getvalue())

    def test_only_machine_codes_survive_from_an_error_body(self):
        errors = [
            {"code": 10000},
            {"code": "budget"},
            {"code": "free text here"},
            {"code": True},
            "x",
        ]
        self.assertEqual(wd.error_codes(errors), [10000, "budget"])
        self.assertEqual(wd.error_codes("not-a-list"), [])

    def test_a_failed_zulip_post_logs_why_and_stays_false(self):
        conn = wd.Connections(
            self.cfg, runner=lambda command, **kwargs: SimpleNamespace(returncode=2), env={}
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(conn.zulip("hello"))
        self.assertIn('"exit": 2', out.getvalue())


class MalformedAnswerTests(Base):
    def row(self, **overrides):
        return {
            "sum": {"requests": 9_000_000, "cpuTimeUs": 0},
            "dimensions": {"scriptName": WORKER},
            **overrides,
        }

    def assert_blind(self, account, reason):
        self.fake.graphql = lambda query, variables: account
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.events("poll_failed")[-1]["reason"], reason)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.state["trips"], {})

    def test_each_kind_of_bad_data_fails_the_poll_and_never_trips(self):
        good = {**EMPTY_ACCOUNT, "wm": [BACKGROUND]}
        cases = (
            ("graphql-malformed-account", ["not", "a", "dict"]),
            ("graphql-malformed-wd", {**good, "wd": "not-a-list"}),
            ("graphql-malformed-wd", {**good, "wd": ["not-a-row"]}),
            ("graphql-malformed-wd", {**good, "wd": [self.row(sum="not-an-object")]}),
            ("graphql-malformed-wd", {**good, "wd": [self.row(dimensions=None)]}),
            ("graphql-malformed-wd", {**good, "wd": [self.row(sum={"requests": 1})]}),
            ("graphql-malformed-wd", {**good, "wd": [self.row(dimensions={"scriptName": 7})]}),
            ("graphql-malformed-wd", {**good, "wd": [self.row(dimensions={})]}),
            (
                "graphql-malformed-wd",
                {**good, "wd": [self.row(sum={"requests": "9", "cpuTimeUs": 0})]},
            ),
            (
                "graphql-malformed-wd",
                {**good, "wd": [self.row(sum={"requests": True, "cpuTimeUs": 0})]},
            ),
            (
                "graphql-malformed-wd",
                {**good, "wd": [self.row(sum={"requests": -1, "cpuTimeUs": 0})]},
            ),
            (
                "graphql-malformed-wd",
                {**good, "wd": [self.row(sum={"requests": float("inf"), "cpuTimeUs": 0})]},
            ),
            (
                "graphql-malformed-wm",
                {
                    **good,
                    "wm": [
                        self.row(dimensions={"scriptName": WORKER, "datetimeMinute": "yesterday"})
                    ],
                },
            ),
            ("graphql-malformed-r2s", {**good, "r2s": None}),
            ("graphql-malformed-kvd", {k: v for k, v in good.items() if k != "kvd"}),
            ("graphql-malformed-wd", {**good, "wd": [self.row()] * (wd.ROW_LIMIT + 1)}),
        )
        for reason, account in cases:
            with self.subTest(reason=reason, account=str(account)[:80]):
                self.assert_blind(account, reason)

    def test_a_null_counts_as_nothing_rather_than_crashing(self):
        account = {"wd": [self.row(sum={"requests": None, "cpuTimeUs": None})]}
        self.assertEqual(self.poll(account), 0)
        self.assertEqual(self.fake.posts, [])

    def test_the_real_query_shape_passes(self):
        wd.validate_account({**EMPTY_ACCOUNT, "wm": [BACKGROUND]})


class CappedReadTests(Base):
    def capped(self, rows=wd.ROW_LIMIT):
        fillers = [
            {"sum": {"requests": 1, "cpuTimeUs": 0}, "dimensions": {"scriptName": f"w{i}"}}
            for i in range(rows - 1)
        ]
        return {
            "wd": [
                {
                    "sum": {"requests": 9_000_000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": WORKER},
                },
                *fillers,
            ]
        }

    def test_a_full_dataset_alerts_and_trips_nothing(self):
        self.poll(self.capped())
        self.assertEqual(self.state["trips"], {})
        self.assertEqual(self.fake.writes(), [])
        self.assertTrue(any("usage read is incomplete" in post for post in self.fake.posts))
        self.assertTrue(any("over a limit" in post for post in self.fake.posts), "it still alerts")
        self.assertEqual(self.events("trip_withheld")[0]["reason"], "capped-read")
        self.assertTrue(any("Not stopping" in post for post in self.fake.posts))

    def test_the_incomplete_alert_is_hourly(self):
        self.poll(self.capped())
        self.poll(at=NOW + dt.timedelta(minutes=1))
        posts = [post for post in self.fake.posts if "usage read is incomplete" in post]
        self.assertEqual(len(posts), 1)

    def test_one_row_under_the_limit_trips_as_usual(self):
        self.poll(self.capped(wd.ROW_LIMIT - 1))
        self.assertIn(WORKER, self.state["trips"])

    def test_a_full_storage_dataset_does_not_hold_trips(self):
        row = {
            "max": {"payloadSize": 1, "metadataSize": 0},
            "dimensions": {"bucketName": "b", "datetime": "x"},
        }
        account = {**d1_rows(day=2_000_000), "r2s": [row] * wd.STORAGE_ROW_LIMIT}
        self.poll(account)
        self.assertIn(WORKER, self.state["trips"])

    def test_a_capped_read_still_retries_what_failed_but_does_not_escalate(self):
        self.state["trips"][WORKER] = {
            "stage": 2,
            "trip_minute": minute(-30),
            "metric": "workers.requests",
            "window": "day",
            "resource": WORKER,
            "target_per_minute": 0,
            "polls": 5,
            "guard_set": False,
            "guard_failure_transient": True,
            "marked": True,
        }
        self.poll(self.capped())
        trip = self.state["trips"][WORKER]
        self.assertTrue(trip["guard_set"], "the retry does not depend on the read")
        self.assertEqual(trip["stage"], 2)
        self.assertEqual(trip["polls"], 5)


class CorruptStateTests(Base):
    def corrupt(self, text="{nope"):
        self.store.state_path.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            self.state = wd.load_or_quarantine(self.store, NOW)
        self.state["inventory"] = json.loads(json.dumps(INVENTORY))
        self.state["daily_check_day"] = NOW.strftime("%Y-%m-%d")

    def test_every_kind_of_bad_file_raises_rather_than_reading_as_empty(self):
        for text in ("{nope", "[]", '{"trips": []}', '{"alerted": 1}', ""):
            self.store.state_path.write_text(text)
            with self.subTest(text=text), self.assertRaises(wd.StateCorrupt):
                self.store.load()

    def test_a_missing_file_is_a_first_run(self):
        self.assertFalse(self.store.state_path.exists())
        self.assertEqual(self.store.load()["trips"], {})

    def test_a_copy_that_could_not_be_moved_is_not_named_as_kept(self):
        self.store.state_path.write_text("{nope")
        real = os.replace
        failures = [OSError()]

        def replace(source, target):
            if failures:
                raise failures.pop()
            real(source, target)

        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(wd.os, "replace", replace),
        ):
            state = wd.load_or_quarantine(self.store, NOW)
        self.assertEqual(state["state_lost_kept"], "no copy, it could not be moved")
        self.assertFalse(list(self.store.directory.glob("state.corrupt-*")))
        self.assertTrue(self.store.load()["state_lost_at"])

    def test_the_bad_file_is_kept_and_the_loss_survives_the_next_load(self):
        self.corrupt("{torn")
        kept = self.store.directory / self.state["state_lost_kept"]
        self.assertEqual(kept.read_text(), "{torn")
        self.assertTrue(self.store.load()["state_lost_at"])

    def test_enforcement_stops_and_an_owner_is_told_hourly(self):
        self.corrupt()
        account = d1_rows(day=2_000_000)
        self.poll(account)
        self.assertEqual(self.state["trips"], {})
        self.assertEqual(self.fake.writes(), [])
        self.assertTrue(self.events("would_trip"))
        self.poll(account, at=NOW + dt.timedelta(minutes=1))
        lost = [post for post in self.fake.posts if "lost its state" in post]
        self.assertEqual(len(lost), 1)
        self.assertTrue(any("over a limit" in post for post in self.fake.posts), "still alerts")
        self.poll(account, at=NOW + dt.timedelta(minutes=61))
        self.assertEqual(len([p for p in self.fake.posts if "lost its state" in p]), 2)

    def test_the_alert_goes_out_even_while_blind(self):
        self.corrupt()
        self.fake.account = wd.PollError("x")
        self.poll()
        self.assertTrue(any("lost its state" in post for post in self.fake.posts))

    def test_state_ack_lets_it_enforce_again(self):
        self.corrupt()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wd.state_ack(self.store, self.state), 0)
        self.assertNotIn("state_lost_at", self.store.load())
        self.poll(d1_rows(day=2_000_000), at=NOW + dt.timedelta(minutes=1))
        self.assertIn(WORKER, self.state["trips"])


class IsolationTests(Base):
    def stuck(self):
        return {
            "stage": 2,
            "trip_minute": minute(-1),
            "metric": "workers.requests",
            "window": "day",
            "resource": "second",
            "target_per_minute": 1_000_000,
            "guard_set": False,
            "guard_failure_transient": True,
            "marked": True,
        }

    def test_one_workers_surprise_does_not_stop_the_next_workers_retry(self):
        self.state["trips"]["broken"] = {"stage": 2, "guard_set": True, "marked": True}
        self.state["trips"]["second"] = self.stuck()
        self.assertEqual(self.poll({}), 1)
        self.assertTrue(self.state["trips"]["second"]["guard_set"])
        self.assertEqual(self.events("worker_failed")[0]["worker"], "broken")
        self.assertEqual(self.events("worker_failed")[0]["error"], "KeyError")
        self.assertTrue(any("Watchdog error on broken" in post for post in self.fake.posts))
        self.poll({}, at=NOW + dt.timedelta(minutes=1))
        errors = [post for post in self.fake.posts if "Watchdog error" in post]
        self.assertEqual(len(errors), 1, "one message an hour")

    def test_one_failed_trip_does_not_stop_the_next_trip(self):
        self.cfg["limits"]["max_trips_per_run"] = 2
        account = {
            "wd": [
                {"sum": {"requests": 9_000_000, "cpuTimeUs": 0}, "dimensions": {"scriptName": name}}
                for name in ("first", "second")
            ]
        }
        real = wd.Watchdog.trip

        def trip(dog, worker, breach, previous):
            if worker == "first":
                raise RuntimeError("response body text")
            real(dog, worker, breach, previous)

        with mock.patch.object(wd.Watchdog, "trip", trip):
            self.assertEqual(self.poll(account), 1)
        self.assertIn("second", self.state["trips"])
        self.assertEqual(self.events("worker_failed")[0]["error"], "RuntimeError")
        self.assertNotIn("response body text", self.last_output)


class AuditRowTests(Base):
    def test_an_unexpected_error_is_one_failed_row_with_the_type_only(self):
        self.fake.fail[r"PUT .*/secrets"] = RuntimeError("response body text")
        self.poll(d1_rows(day=2_000_000))
        rows = [r for r in self.events() if r.get("action") == "guard_mode_off"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "action_failed")
        self.assertEqual(rows[0]["codes"], ["RuntimeError"])
        self.assertNotIn("response body text", self.last_output)
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2, "a retry, not a refusal")

    def test_success_and_refusal_are_also_one_row(self):
        self.poll(d1_rows(day=2_000_000))
        self.assertEqual(len([r for r in self.events() if r.get("action") == "guard_mode_off"]), 1)
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(400, [1])
        self.state["trips"][WORKER]["guard_set"] = False
        self.poll({}, at=NOW + dt.timedelta(minutes=1))
        rows = [r for r in self.events() if r.get("action") == "guard_mode_off"]
        self.assertEqual([r["event"] for r in rows], ["action", "action_failed"])

    def test_a_rearm_step_is_one_row_on_an_unexpected_error(self):
        self.state["trips"][WORKER] = {
            "stage": 3,
            "previous": {"schedules": [{"cron": "* * * * *"}]},
        }
        self.fake.fail[r"PUT .*/schedules"] = RuntimeError("response body text")
        with contextlib.redirect_stdout(io.StringIO()):
            code = wd.rearm(self.cfg, self.fake, self.store, self.state, WORKER, True)
        self.assertEqual(code, 1)
        rows = [r for r in self.events() if str(r.get("path", "")).endswith("/schedules")]
        self.assertEqual([r["event"] for r in rows], ["rearm_failed"])
        self.assertEqual(rows[0]["codes"], ["RuntimeError"])
        self.assertIn(WORKER, self.state["trips"])


class StaleInventoryTests(Base):
    def bound_api(self):
        original = self.fake.api

        def api(method, path, body=None):
            if path.endswith(f"{WORKER}/settings"):
                return {"bindings": [{"type": "d1", "id": DB}]}
            return original(method, path, body)

        self.fake.api = api

    def test_a_failed_read_withholds_a_trip_that_needs_ownership(self):
        self.state["inventory"]["at"] = 0
        self.fake.fail[r"GET .*/workers/scripts$"] = wd.ApiError(500, [])
        self.assertEqual(self.poll(d1_rows(day=2_000_000)), 1)
        self.assertEqual(self.state["trips"], {})
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.events("trip_withheld")[0]["reason"], "stale-inventory")
        self.assertEqual(self.events("inventory_failed")[0]["source"], "cloudflare-workers-api")
        self.assertTrue(any("Not stopping" in post for post in self.fake.posts))
        self.assertTrue(any("over a limit" in post for post in self.fake.posts), "it still alerts")

    def test_the_next_good_read_authorizes_it_within_a_minute(self):
        self.state["inventory"]["at"] = 0
        self.fake.fail[r"GET .*/workers/scripts$"] = wd.ApiError(500, [])
        self.bound_api()
        self.poll(d1_rows(day=2_000_000))
        self.fake.fail.clear()
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.assertNotIn("inventory_failed", self.state)
        self.assertIn(WORKER, self.state["trips"])

    def test_a_metric_that_names_its_script_needs_no_inventory(self):
        self.state["inventory"]["at"] = 0
        self.fake.fail[r"GET .*/workers/scripts$"] = wd.ApiError(500, [])
        account = {
            "wd": [
                {
                    "sum": {"requests": 9_000_000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": WORKER},
                }
            ]
        }
        self.poll(account)
        self.assertIn(WORKER, self.state["trips"])

    def test_old_ownership_withholds_even_without_a_failed_read(self):
        self.state["inventory"]["at"] = NOW.timestamp() - wd.INVENTORY_MAX_AGE_SECONDS
        self.state["inventory_tried"] = NOW.timestamp()
        self.poll(d1_rows(day=2_000_000))
        self.assertEqual(self.state["trips"], {})
        self.assertEqual(self.events("trip_withheld")[0]["reason"], "stale-inventory")

    def test_the_daily_check_waits_for_a_real_read(self):
        self.state["inventory"]["at"] = 0
        self.state.pop("daily_check_day")
        self.fake.fail[r"GET .*/workers/scripts$"] = wd.ApiError(500, [])
        self.poll({})
        self.assertNotIn("daily_check_day", self.state)
        self.assertEqual(self.events("daily_check"), [])


class FallbackRecordTests(Base):
    trip_then = EscalationTests.trip_then

    def test_each_finished_step_is_on_disk_before_the_next_runs(self):
        original = self.fake.api
        seen: list = []

        def api(method, path, body=None):
            if method == "DELETE" and "/workers/domains/" in path:
                saved = json.loads(self.store.state_path.read_text())
                seen.append(saved["trips"][WORKER]["pending"])
            return original(method, path, body)

        self.fake.api = api
        self.trip_then(per_minute_after=5000)
        self.assertEqual(seen, [["detach:dom-1"]])

    def test_a_domain_a_dead_run_already_removed_counts_as_done(self):
        self.fake.fail[r"DELETE .*/workers/domains/"] = wd.ApiError(404, [])
        self.trip_then(per_minute_after=5000)
        self.assertEqual(self.state["trips"][WORKER]["pending"], [])

    def test_a_refused_domain_stays_pending(self):
        self.fake.fail[r"DELETE .*/workers/domains/"] = wd.ApiError(403, [])
        self.trip_then(per_minute_after=5000)
        self.assertEqual(self.state["trips"][WORKER]["pending"], ["detach:dom-1"])


class TripLabelBeforeHardeningTests(Base):
    def test_a_refused_secret_and_a_failed_label_do_not_harden(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(400, [1])
        self.fake.fail[r"POST .*/deployments"] = wd.ApiError(500, [])
        self.poll(d1_rows(day=2_000_000))
        trip = self.state["trips"][WORKER]
        self.assertEqual(trip["stage"], 2)
        self.assertFalse(trip["marked"])
        self.assertFalse([c for c in self.fake.writes() if c[1].endswith("/schedules")])
        self.assertTrue(any("Fallback stop is held" in post for post in self.fake.posts))
        self.assertEqual(self.events("fallback_held")[0]["reason"], "no-trip-label")

    def test_it_hardens_on_the_poll_where_the_label_lands(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(400, [1])
        self.fake.fail[r"POST .*/deployments"] = wd.ApiError(500, [])
        self.poll(d1_rows(day=2_000_000))
        del self.fake.fail[r"POST .*/deployments"]
        self.poll(at=NOW + dt.timedelta(minutes=1))
        trip = self.state["trips"][WORKER]
        self.assertTrue(trip["marked"])
        self.assertEqual(trip["stage"], 3)
        writes = [c[1] for c in self.fake.writes()]
        label = next(i for i, path in enumerate(writes) if path.endswith("/deployments"))
        stop = next(i for i, path in enumerate(writes) if path.endswith("/schedules"))
        self.assertLess(label, stop)

    def test_the_held_alert_is_hourly(self):
        self.fake.fail[r"PUT .*/secrets"] = wd.ApiError(400, [1])
        self.fake.fail[r"POST .*/deployments"] = wd.ApiError(500, [])
        self.poll(d1_rows(day=2_000_000))
        self.poll(at=NOW + dt.timedelta(minutes=1))
        self.poll(at=NOW + dt.timedelta(minutes=2))
        held = [post for post in self.fake.posts if "Fallback stop is held" in post]
        self.assertEqual(len(held), 1)

    def test_a_rate_escalation_without_the_label_also_holds(self):
        self.fake.fail[r"POST .*/deployments"] = wd.ApiError(500, [])
        self.poll(d1_rows(per_minute={minute(-3 - i): 5000 for i in range(5)}))
        for count in range(1, 7):
            at = NOW + dt.timedelta(minutes=count)
            series = {minute(-back, at): 5000 for back in range(3, 8)}
            self.poll(d1_rows(per_minute=series), at=at)
        self.assertEqual(self.state["trips"][WORKER]["stage"], 2)
        self.assertFalse([c for c in self.fake.writes() if c[1].endswith("/schedules")])


class AccountScopeTargetTests(Base):
    def rows(self, at):
        return {
            "wm": [
                {
                    "sum": {"requests": 1000, "cpuTimeUs": 0},
                    "dimensions": {"scriptName": name, "datetimeMinute": minute(-back, at)},
                }
                for name in ("half-a", "half-b")
                for back in range(3, 8)
            ]
        }

    def test_two_workers_holding_an_account_over_still_escalate(self):
        self.poll(self.rows(NOW), profile="free")
        trip = self.state["trips"]["half-a"]
        self.assertEqual(trip["target_per_minute"], 500)
        for count in range(1, 6):
            at = NOW + dt.timedelta(minutes=count)
            self.poll(self.rows(at), at=at, profile="free")
        self.assertEqual(self.state["trips"]["half-a"]["stage"], 3)

    def test_a_per_worker_rate_rule_keeps_its_own_threshold(self):
        self.poll(d1_rows(per_minute={minute(-3 - i): 5000 for i in range(5)}))
        self.assertEqual(self.state["trips"][WORKER]["target_per_minute"], 4000)


class RegistryFlagTests(Base):
    def drift(self, entry, **live):
        inventory = {"details": {"w": live}}
        return wd.compare_registry(inventory, {"w": entry})

    def test_preview_urls_zero_means_off(self):
        self.assertTrue(self.drift({"preview_urls": 0}, previews_enabled=True))
        self.assertFalse(self.drift({"preview_urls": True}, previews_enabled=True))

    def test_workers_dev_needs_a_row_that_says_off(self):
        self.assertFalse(self.drift({}, workers_dev=True))
        self.assertFalse(self.drift({"workers_dev": True}, workers_dev=True))
        self.assertTrue(self.drift({"workers_dev": False}, workers_dev=True))
        self.assertTrue(self.drift({"workers_dev": 0}, workers_dev=True))


class KnobTests(unittest.TestCase):
    def test_a_stale_enforce_stays_enforce_and_the_log_says_it_is_stale(self):
        answers = {"CF_USAGE_WATCHDOG_MODE": "enforce", "CF_USAGE_WATCHDOG_PROFILE": "paid"}
        report = {"schema": 1, "fetch": "failed", "stale": True, "knobs": {}}
        knobs = SimpleNamespace(get=lambda name, **kwargs: answers[name], report=lambda: report)
        real = sys.modules.get("fleet_knobs")
        sys.modules["fleet_knobs"] = SimpleNamespace(knobs=knobs)
        try:
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                store = wd.Store(Path(tmp))
                self.assertEqual(wd.read_knobs(Path(tmp) / "STOP", store), ("enforce", "paid"))
                rows = [json.loads(line) for line in store.events_path.read_text().splitlines()]
        finally:
            if real is None:
                del sys.modules["fleet_knobs"]
            else:
                sys.modules["fleet_knobs"] = real
        self.assertEqual(rows[0]["event"], "knobs")
        self.assertTrue(rows[0]["report"]["stale"])

    def test_a_failed_report_does_not_turn_enforce_into_observe(self):
        def report():
            raise RuntimeError("log failure")

        answers = {"CF_USAGE_WATCHDOG_MODE": "enforce", "CF_USAGE_WATCHDOG_PROFILE": "paid"}
        knobs = SimpleNamespace(get=lambda name, **kwargs: answers[name], report=report)
        real = sys.modules.get("fleet_knobs")
        sys.modules["fleet_knobs"] = SimpleNamespace(knobs=knobs)
        try:
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    wd.read_knobs(Path(tmp) / "STOP", wd.Store(Path(tmp))), ("enforce", "paid")
                )
        finally:
            if real is None:
                del sys.modules["fleet_knobs"]
            else:
                sys.modules["fleet_knobs"] = real

    def test_registry_defaults_are_the_safe_ones(self):
        from fleet_knobs.registry import BY_NAME

        self.assertEqual(BY_NAME["CF_USAGE_WATCHDOG_MODE"].default, "observe")
        self.assertEqual(BY_NAME["CF_USAGE_WATCHDOG_MODE"].choices[0], "observe")
        self.assertEqual(BY_NAME["CF_USAGE_WATCHDOG_PROFILE"].default, "paid")

    def test_broken_knob_read_means_observe(self):
        real = sys.modules.get("fleet_knobs")
        sys.modules["fleet_knobs"] = None  # Importing it now raises.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wd.read_knobs(Path("/nonexistent")), ("observe", "paid"))
        finally:
            if real is None:
                del sys.modules["fleet_knobs"]
            else:
                sys.modules["fleet_knobs"] = real


class UnitFileTests(unittest.TestCase):
    def read(self, name):
        return (HERE.parent / "systemd" / name).read_text(encoding="utf-8")

    def test_service_runs_the_managed_checkout_as_the_seat(self):
        text = self.read("cf-usage-watchdog.service")
        self.assertIn("User=jay", text)
        self.assertIn("Environment=AGENT_SEAT=CLAUDE", text)
        self.assertIn(
            "/home/jay/.local/share/fleet-cli/repo/scripts/cf-usage-watchdog/watchdog.py run", text
        )
        self.assertIn("EnvironmentFile=/etc/cf-usage-watchdog/infisical.env", text)
        self.assertNotIn("%h", text)

    def test_no_condition_in_the_service_section(self):
        # systemd ignores one there, and a skipped run of a watchdog would be silent anyway.
        text = self.read("cf-usage-watchdog.service")
        self.assertNotIn("Condition", text)

    def test_timer_is_every_minute(self):
        text = self.read("cf-usage-watchdog.timer")
        self.assertIn("OnCalendar=minutely", text)


if __name__ == "__main__":
    unittest.main()
