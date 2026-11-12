"""Uptime coverage audit. Fixtures only: nothing here calls Sentry."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from uptime_audit.cli import main
from uptime_audit.errors import AuditError
from uptime_audit.inventory import load_inventory
from uptime_audit.report import compare, normalize_url
from uptime_audit.sentry import (
    build_request,
    fetch_monitors,
    is_covering,
    parse_monitor_page,
    uptime_collection_url,
)

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "expected-hosts.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SMALL = FIXTURES / "expected-small.yaml"
SMALL_MONITORS = FIXTURES / "monitors-small.json"
LIVE = FIXTURES / "monitors-live-shape.json"
WORKFLOW = ROOT.parents[1] / ".github" / "workflows" / "uptime-audit.yml"

CERTAIN_URLS = {
    "https://socratictrade.com/api/health",
    "https://congress.trade/api/health",
    "https://usage.jays.services/api/health",
    "https://usage.jays.services/api/ready?strict=1",
    "https://dealdex.net/",
    "https://jays.services/",
    "https://simple-with-us.github.io/AI-Fleet-Coordinator/",
    "https://botfleet.app/",
    "https://contactlogo.com/",
}


class Response:
    def __init__(self, body: bytes, link: str = ""):
        self.status = 200
        self._body = body
        self.headers = EmailMessage()
        if link:
            self.headers["Link"] = link

    def read(self, size):
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class InventoryTests(unittest.TestCase):
    def test_reviewed_inventory(self):
        inventory = load_inventory(INVENTORY)
        self.assertEqual(inventory.org, "simple-with-us")
        certain = {entry.health_url for entry in inventory.entries if entry.certainty == "certain"}
        self.assertEqual(certain, CERTAIN_URLS)
        hosts = inventory.host_set()
        self.assertIn("autorotate.codes", hosts)
        self.assertIn("fleetlink.online", hosts)
        self.assertIn("fleetlink.app", hosts)
        self.assertNotIn("dealdex.online", hosts)
        self.assertNotIn("mac.jays.services", hosts)
        excluded = {item.host for item in inventory.excluded}
        self.assertIn("dealdex.online", excluded)
        self.assertIn("mac.jays.services", excluded)
        self.assertIn("agents.jays.services", excluded)
        catalog = next(entry for entry in inventory.entries if entry.id == "simple-with-us-catalog")
        self.assertEqual(catalog.host, "")
        self.assertEqual(catalog.health_url, "")
        self.assertEqual(catalog.certainty, "uncertain")
        dashboard = next(entry for entry in inventory.entries if entry.id == "coolify-dashboard")
        self.assertEqual(dashboard.health_url, "")
        for entry in inventory.entries:
            self.assertTrue(entry.source)
            self.assertTrue(entry.owner)
            self.assertNotIn("\u00a0", entry.note)

    def test_rejects_anchors(self):
        text = "version: 1\norg: example-org\nentries: &a []\nexcluded: []\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(AuditError):
                load_inventory(path)


class CompareTests(unittest.TestCase):
    def test_small_fixture_categories(self):
        inventory = load_inventory(SMALL)
        monitors = parse_monitor_page(json.loads(SMALL_MONITORS.read_text(encoding="utf-8")))
        report = compare(inventory, monitors)
        self.assertEqual([row["id"] for row in report["covered"]], ["app", "maybe"])
        self.assertEqual([row["id"] for row in report["uncovered"]], ["app-ready"])
        self.assertEqual(report["uncovered_uncertain"], [])
        self.assertEqual(report["unexpected_hosts"][0]["host"], "gone.example")
        self.assertEqual(report["excluded_hosts"][0]["host"], "old.example")
        self.assertEqual(report["health_url_mismatches"][0]["url"], "https://app.example/other")
        self.assertIn("https://app.example/api/health", report["health_url_mismatches"][0]["expected_urls"])
        self.assertEqual(report["health_path_unspecified"][0]["host"], "dash.example")
        self.assertEqual([row["id"] for row in report["unresolved"]], ["dashboard", "redacted"])
        disabled_ids = {row["id"] for row in report["disabled_or_muted"]}
        self.assertEqual(disabled_ids, {"2", "7"})

    def test_trailing_slash_and_query_normalize(self):
        self.assertEqual(
            normalize_url("https://APP.example/api/health/"),
            normalize_url("https://app.example/api/health"),
        )
        self.assertNotEqual(
            normalize_url("https://app.example/api/ready?strict=1"),
            normalize_url("https://app.example/api/ready"),
        )

    def test_saved_monitor_shape_against_inventory(self):
        inventory = load_inventory(INVENTORY)
        monitors = parse_monitor_page(json.loads(LIVE.read_text(encoding="utf-8")))
        report = compare(inventory, monitors)
        covered = {row["id"] for row in report["covered"]}
        self.assertTrue(
            {
                "socratic-trade",
                "congress-trade",
                "usage-monitor-ready",
                "botfleet",
                "contactlogo",
                "autorotate-vercel",
                "usage-fleet-aggregator",
            }
            <= covered
        )
        uncovered = {row["id"] for row in report["uncovered"]}
        self.assertTrue(
            {"usage-monitor-health", "dealdex", "personal-site", "fleet-digest"} <= uncovered
        )
        self.assertIn("autorotate-apex", {row["id"] for row in report["uncovered_uncertain"]})
        self.assertEqual(report["unexpected_hosts"], [])
        self.assertEqual(report["excluded_hosts"][0]["host"], "dealdex.online")
        self.assertEqual(report["health_path_unspecified"][0]["host"], "host.jays.services")
        self.assertEqual(report["health_url_mismatches"], [])
        self.assertEqual(report["disabled_or_muted"], [])


class SentryClientTests(unittest.TestCase):
    def test_request_is_get_and_token_stays_out_of_the_url(self):
        url = uptime_collection_url("https://us.sentry.io", "simple-with-us")
        self.assertEqual(url, "https://us.sentry.io/api/0/organizations/simple-with-us/uptime/")
        request = build_request(url, "test-token")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("test-token", request.full_url)
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))

    def test_status_rules(self):
        self.assertTrue(is_covering({"status": "active", "url": "https://example.com"}))
        self.assertTrue(is_covering({"status": 0, "url": "https://example.com"}))
        self.assertFalse(is_covering({"status": "disabled", "url": "https://example.com"}))
        self.assertFalse(is_covering({"status": 1, "url": "https://example.com"}))
        self.assertFalse(is_covering({"status": "active", "muted": True, "url": "https://example.com"}))
        self.assertFalse(is_covering({"enabled": False, "url": "https://example.com"}))
        self.assertFalse(is_covering({"url": "https://example.com"}))

    def test_pagination_follows_next_and_stops(self):
        pages = []

        class Opener:
            def open(self, request, timeout=None):
                self.last = request
                pages.append(request.full_url)
                if len(pages) == 1:
                    link = (
                        '<https://us.sentry.io/api/0/organizations/example-org/uptime/?cursor=next>; '
                        'rel="next"; results="true"'
                    )
                    return Response(b"[]", link)
                return Response(
                    b'[{"id":"9","name":"n","status":"active","url":"https://app.example/api/health"}]'
                )

        opener = Opener()
        monitors = fetch_monitors("example-org", "test-token", opener=opener)
        self.assertEqual(len(pages), 2)
        self.assertEqual(monitors[0].url, "https://app.example/api/health")
        self.assertEqual(opener.last.get_method(), "GET")
        self.assertNotIn("test-token", pages[1])

    def test_http_error_hides_the_body(self):
        class Opener:
            def open(self, request, timeout=None):
                raise HTTPError(request.full_url, 403, "no", EmailMessage(), io.BytesIO(b"secret-body"))

        with self.assertRaises(AuditError) as caught:
            fetch_monitors("example-org", "test-token", opener=Opener())
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("secret-body", str(caught.exception))
        self.assertNotIn("test-token", str(caught.exception))

    def test_next_link_off_the_collection_is_refused(self):
        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout=None):
                self.calls += 1
                link = '<https://evil.example/api/0/organizations/example-org/uptime/>; rel="next"; results="true"'
                return Response(b"[]", link)

        opener = Opener()
        with self.assertRaises(AuditError) as caught:
            fetch_monitors("example-org", "test-token", opener=opener)
        self.assertEqual(opener.calls, 1)
        self.assertIn("uptime collection", str(caught.exception))
        self.assertNotIn("test-token", str(caught.exception))

    def test_results_false_does_not_follow(self):
        class Opener:
            def open(self, request, timeout=None):
                link = '<https://us.sentry.io/api/0/organizations/example-org/uptime/?cursor=x>; rel="next"; results="false"'
                return Response(b"[]", link)

        self.assertEqual(fetch_monitors("example-org", "test-token", opener=Opener()), [])


class CliTests(unittest.TestCase):
    def test_fixture_does_not_touch_the_network(self):
        def boom(*args, **kwargs):
            raise AssertionError("network")

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "out.json"
            markdown_path = Path(tmp) / "out.md"
            with mock.patch("urllib.request.urlopen", boom):
                with mock.patch("uptime_audit.cli.fetch_monitors", boom):
                    code = main(
                        [
                            "--expected",
                            str(SMALL),
                            "--monitors",
                            str(SMALL_MONITORS),
                            "--json",
                            str(json_path),
                            "--markdown",
                            str(markdown_path),
                        ]
                    )
            self.assertEqual(code, 0)
            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["uncovered"][0]["id"], "app-ready")
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("## Uncovered Hosts", markdown)
            self.assertIn("\u00a0 ", markdown)

    def test_fail_on_uncovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(
                [
                    "--expected",
                    str(SMALL),
                    "--monitors",
                    str(SMALL_MONITORS),
                    "--fail-on-uncovered",
                    "--markdown",
                    str(Path(tmp) / "out.md"),
                ]
            )
        self.assertEqual(code, 1)

    def test_missing_token_exits_2_without_a_call(self):
        def boom(*args, **kwargs):
            raise AssertionError("network")

        with mock.patch.dict("os.environ", {"SENTRY_AUTH_TOKEN": ""}, clear=False):
            with mock.patch("uptime_audit.sentry.build_opener", boom):
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    code = main(["--expected", str(SMALL)])
        self.assertEqual(code, 2)
        self.assertIn("SENTRY_AUTH_TOKEN", err.getvalue())
        self.assertNotIn("Bearer", err.getvalue())


class WorkflowTests(unittest.TestCase):
    def test_weekly_workflow_skips_without_a_secret(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("cron:", text)
        self.assertIn("SENTRY_AUTH_TOKEN", text)
        self.assertIn("::notice::", text)
        self.assertIn("--fail-on-uncovered", text)
        self.assertIn("python3 -m uptime_audit", text)
        self.assertNotIn("POST", text)
        self.assertNotIn("uptime/", text.split("python3 -m uptime_audit", 1)[-1])


if __name__ == "__main__":
    unittest.main()
