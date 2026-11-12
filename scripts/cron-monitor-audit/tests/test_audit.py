"""Cron monitor audit. Fixtures only: nothing here calls Sentry or GitHub."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from cron_monitor_audit.classify import build_catalog, classify_monitor
from cron_monitor_audit.cli import main
from cron_monitor_audit.errors import AuditError, SentryForbidden, load_bounded_json
from cron_monitor_audit.parse import literal_slugs, operator_senders, parse_reporter, slugify
from cron_monitor_audit.sentry import (
    build_request,
    fetch_monitors,
    monitors_collection_url,
    parse_monitor_page,
)
from cron_monitor_audit.sources import (
    CodeHit,
    CodeSearch,
    RepoSource,
    build_github_request,
    fetch_repos,
    include_local_path,
    load_apps,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SMALL = FIXTURES / "monitors-small.json"
REPO = ROOT.parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "cron-monitor-audit.yml"
APPS = REPO / "fleet-apps.json"


class Response:
    def __init__(self, body: bytes, link: str = "", status: int = 200):
        self.status = status
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


def _repo() -> RepoSource:
    return RepoSource(
        full_name="Simple-With-Us/Socratic-Trade",
        readable=True,
        names_known=True,
        workflow_names=("CI", "Security"),
        files=(
            ("scripts/sentry-ci-report.py", (FIXTURES / "reporter.py").read_text(encoding="utf-8")),
            ("scripts/heartbeat.py", (FIXTURES / "heartbeat.py").read_text(encoding="utf-8")),
        ),
    )


def _catalog(extra: list[RepoSource] | None = None, search: CodeSearch | None = None):
    identities = {"socratic-trade": {"Simple-With-Us/Socratic-Trade"}}
    return build_catalog([_repo(), *(extra or [])], identities, search)


class ParseTests(unittest.TestCase):
    def test_unrelated_slug_constants_are_not_senders(self):
        text = 'AFC_SLUG = "ai-fleet-coordinator"\nMONITOR_SLUG = "fleet-host-monitor"\n'
        self.assertEqual(literal_slugs(text), {"fleet-host-monitor"})

    def test_slugify_matches_the_reporter(self):
        self.assertEqual(slugify("Vercel Production Catch-up"), "vercel-production-catch-up")
        self.assertEqual(slugify("Fleet daily digest + calendars"), "fleet-daily-digest-calendars")
        self.assertEqual(slugify("AI-Fleet-Coordinator"), "ai-fleet-coordinator")

    def test_vendored_reporter_and_reusable_workflow(self):
        script = parse_reporter(
            "scripts/sentry-ci-report.py", (FIXTURES / "reporter.py").read_text()
        )
        self.assertIsNotNone(script)
        assert script is not None
        self.assertEqual(script.identity, "socratic-trade")
        self.assertEqual(set(script.schedules or {}), {"CI", "Security", "Gone Workflow"})
        workflow = parse_reporter(
            ".github/workflows/sentry-ci-report.yml", (FIXTURES / "reusable.yml").read_text()
        )
        self.assertIsNotNone(workflow)
        assert workflow is not None
        self.assertEqual(workflow.identity, "ai-fleet-coordinator")
        self.assertIn("Backup fleet GitHub repositories", workflow.schedules or {})

    def test_operator_default_is_pinned(self):
        pinned, prefix = operator_senders((FIXTURES / "heartbeat.py").read_text(encoding="utf-8"))
        self.assertEqual(prefix, "operator-")
        self.assertEqual(pinned, {"operator-mac"})

    def test_real_coordinator_reporter_uses_the_workflow_map(self):
        text = (REPO / ".github/workflows/sentry-ci-report.yml").read_text(encoding="utf-8")
        reporter = parse_reporter(".github/workflows/sentry-ci-report.yml", text)
        self.assertIsNotNone(reporter)
        assert reporter is not None
        self.assertEqual(reporter.identity, "ai-fleet-coordinator")
        self.assertEqual(
            set(reporter.schedules or {}),
            {"Backup fleet GitHub repositories", "Fleet daily digest + calendars"},
        )


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.monitors = {
            item.slug: item for item in parse_monitor_page(json.loads(SMALL.read_text()))
        }
        self.catalog = _catalog()

    def test_categories(self):
        orphan = classify_monitor(
            self.monitors["ci-socratic-trade-vercel-production-catch-up"], self.catalog
        )
        self.assertEqual(orphan.category, "orphan")
        failing = classify_monitor(self.monitors["ci-socratic-trade-ci"], self.catalog)
        self.assertEqual(failing.category, "matched_failing")
        self.assertIn("Socratic-Trade", failing.sender)
        healthy = classify_monitor(self.monitors["ci-socratic-trade-security"], self.catalog)
        self.assertEqual(healthy.category, "matched_healthy")
        stale = classify_monitor(self.monitors["ci-socratic-trade-gone-workflow"], self.catalog)
        self.assertEqual(stale.category, "orphan")
        mac = classify_monitor(self.monitors["operator-mac"], self.catalog)
        self.assertEqual(mac.category, "matched_failing")
        server = classify_monitor(self.monitors["operator-server"], self.catalog)
        self.assertEqual(server.category, "uncertain")
        other = classify_monitor(self.monitors["botfleet-overnight-board-95f6ac6a"], self.catalog)
        self.assertEqual(other.category, "uncertain")
        disabled_sender = classify_monitor(self.monitors["fleet-host-monitor"], self.catalog)
        self.assertEqual(disabled_sender.category, "uncertain")

    def test_literal_sender_outside_the_ci_map(self):
        extra = RepoSource(
            full_name="Simple-With-Us/AI-Fleet-Coordinator",
            readable=True,
            names_known=True,
            workflow_names=(),
            files=(("scripts/monitor.py", 'MONITOR_SLUG = "fleet-host-monitor"\n'),),
        )
        catalog = _catalog([extra])
        finding = classify_monitor(self.monitors["fleet-host-monitor"], catalog)
        self.assertEqual(finding.category, "uncertain")
        self.assertIn("disabled", finding.reason)

    def test_code_search_hit_is_a_sender_and_a_doc_is_not(self):
        search = CodeSearch(
            "ok",
            "",
            {
                "botfleet-overnight-board-95f6ac6a": (
                    CodeHit(
                        "scripts/board.py",
                        'monitor_slug = "botfleet-overnight-board-95f6ac6a"',
                    ),
                )
            },
        )
        catalog = _catalog(search=search)
        finding = classify_monitor(self.monitors["botfleet-overnight-board-95f6ac6a"], catalog)
        self.assertEqual(finding.category, "matched_failing")
        mention = CodeSearch(
            "ok",
            "",
            {
                "botfleet-overnight-board-95f6ac6a": (
                    CodeHit("docs/note.md", "see botfleet-overnight-board-95f6ac6a"),
                )
            },
        )
        catalog = _catalog(search=mention)
        finding = classify_monitor(self.monitors["botfleet-overnight-board-95f6ac6a"], catalog)
        self.assertEqual(finding.category, "uncertain")

    def test_sentry_stores_a_50_character_prefix(self):
        long = "ci-ai-fleet-coordinator-backup-fleet-github-repositories"
        stored = long[:50]
        extra = RepoSource(
            full_name="Simple-With-Us/AI-Fleet-Coordinator",
            readable=True,
            names_known=True,
            workflow_names=("Backup fleet GitHub repositories",),
            files=(
                (
                    ".github/workflows/sentry-ci-report.yml",
                    (FIXTURES / "reusable.yml").read_text(encoding="utf-8"),
                ),
            ),
        )
        catalog = build_catalog(
            [extra],
            {"ai-fleet-coordinator": {"Simple-With-Us/AI-Fleet-Coordinator"}},
        )
        monitor = parse_monitor_page(
            [
                {
                    "slug": stored,
                    "status": "active",
                    "project": {"slug": "fleet-infra"},
                    "environments": [{"status": "error", "lastCheckIn": "2026-11-11T14:53:17Z"}],
                }
            ]
        )[0]
        self.assertEqual(classify_monitor(monitor, catalog).category, "matched_failing")
        other = "ci-ai-fleet-coordinator-backup-fleet-github-repository-b"
        self.assertTrue(other.startswith(stored))
        crowded = RepoSource(
            full_name="Simple-With-Us/AI-Fleet-Coordinator",
            readable=True,
            names_known=False,
            workflow_names=(),
            files=(),
        )
        catalog = build_catalog([crowded], {})
        catalog.produced[long] = "Simple-With-Us/AI-Fleet-Coordinator"
        catalog.produced[other] = "Simple-With-Us/AI-Fleet-Coordinator"
        self.assertEqual(classify_monitor(monitor, catalog).category, "uncertain")

    def test_no_sender_file_is_not_an_empty_schedule(self):
        repo = RepoSource(
            full_name="Simple-With-Us/Example",
            readable=True,
            names_known=True,
            workflow_names=("CI",),
            files=(),
        )
        catalog = build_catalog([repo], {"example": {"Simple-With-Us/Example"}})
        monitor = parse_monitor_page(
            [
                {
                    "slug": "ci-example-nightly",
                    "status": "active",
                    "project": {"slug": "fleet-infra"},
                    "environments": [{"status": "error", "lastCheckIn": "2026-11-11T00:00:00Z"}],
                }
            ]
        )[0]
        self.assertEqual(classify_monitor(monitor, catalog).category, "uncertain")

    def test_unknown_ci_needs_a_finished_code_search(self):
        monitor = parse_monitor_page(
            [
                {
                    "slug": "ci-not-a-fleet-app-nightly",
                    "status": "active",
                    "project": {"slug": "fleet-infra"},
                    "environments": [{"status": "error", "lastCheckIn": "2026-11-11T00:00:00Z"}],
                }
            ]
        )[0]
        self.assertEqual(classify_monitor(monitor, _catalog()).category, "uncertain")
        finished = _catalog(search=CodeSearch("ok", "", {}))
        self.assertEqual(classify_monitor(monitor, finished).category, "orphan")
        mentioned = _catalog(
            search=CodeSearch(
                "ok",
                "",
                {
                    "ci-not-a-fleet-app-nightly": (
                        CodeHit("scripts/board.py", 'slug = "ci-not-a-fleet-app-nightly"'),
                    )
                },
            )
        )
        self.assertEqual(classify_monitor(monitor, mentioned).category, "uncertain")

    def test_bare_input_does_not_erase_a_schedule_map(self):
        bare = "on:\n  workflow_call:\n    inputs:\n      cron_schedules:\n      app:\n"
        self.assertIsNone(parse_reporter(".github/workflows/sentry-ci-report-reusable.yml", bare))
        caller = (FIXTURES / "reusable.yml").read_text(encoding="utf-8")
        extra = RepoSource(
            full_name="Simple-With-Us/AI-Fleet-Coordinator",
            readable=True,
            names_known=True,
            workflow_names=("Backup fleet GitHub repositories",),
            files=(
                (".github/workflows/sentry-ci-report.yml", caller),
                (".github/workflows/sentry-ci-report-reusable.yml", bare),
            ),
        )
        catalog = build_catalog(
            [extra],
            {"ai-fleet-coordinator": {"Simple-With-Us/AI-Fleet-Coordinator"}},
        )
        self.assertIn(
            "ci-ai-fleet-coordinator-backup-fleet-github-repositories",
            catalog.produced,
        )

    def test_unreadable_identity_is_not_an_orphan(self):
        unread = RepoSource(
            full_name="Simple-With-Us/DealDex",
            readable=False,
            names_known=False,
            workflow_names=(),
            files=(),
            reason="GitHub workflows for Simple-With-Us/DealDex returned HTTP 404",
        )
        identities = {
            "socratic-trade": {"Simple-With-Us/Socratic-Trade"},
            "dealdex": {"Simple-With-Us/DealDex"},
        }
        catalog = build_catalog(
            [_repo(), unread], identities, CodeSearch("unavailable", "no GitHub token", {})
        )
        monitor = parse_monitor_page(
            [
                {
                    "slug": "ci-dealdex-vercel-production-catch-up",
                    "status": "active",
                    "project": {"slug": "fleet-infra"},
                    "environments": [{"status": "error", "lastCheckIn": "2026-11-11T22:15:11Z"}],
                }
            ]
        )[0]
        self.assertEqual(classify_monitor(monitor, catalog).category, "uncertain")

    def test_audit_package_is_not_a_source(self):
        self.assertFalse(include_local_path("scripts/cron-monitor-audit/cron_monitor_audit/cli.py"))
        self.assertFalse(include_local_path("scripts/simple_tools/tests/test_heartbeat.py"))
        self.assertTrue(include_local_path("scripts/simple_tools/heartbeat.py"))
        self.assertTrue(include_local_path(".github/workflows/sentry-ci-report.yml"))


class SentryClientTests(unittest.TestCase):
    def test_request_is_get_and_token_stays_out_of_the_url(self):
        url = monitors_collection_url("https://us.sentry.io", "simple-with-us")
        self.assertEqual(url, "https://us.sentry.io/api/0/organizations/simple-with-us/monitors/")
        request = build_request(url, "test-token")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("test-token", request.full_url)
        github = build_github_request(
            "https://api.github.com/repos/a/b", "test-token", "application/json"
        )
        self.assertEqual(github.get_method(), "GET")
        self.assertNotIn("test-token", github.full_url)

    def test_pagination_follows_next_and_stops(self):
        pages: list[str] = []

        class Opener:
            def open(self, request, timeout=None):
                pages.append(request.full_url)
                if len(pages) == 1:
                    link = (
                        "<https://us.sentry.io/api/0/organizations/example-org/monitors/?cursor=next>; "
                        'rel="next"; results="true"'
                    )
                    return Response(b"[]", link)
                return Response(SMALL.read_bytes())

        monitors = fetch_monitors("example-org", "test-token", opener=Opener())
        self.assertEqual(len(pages), 2)
        self.assertEqual(monitors[0].slug, "ci-socratic-trade-vercel-production-catch-up")
        self.assertNotIn("test-token", pages[1])

    def test_http_error_hides_the_body(self):
        class Opener:
            def open(self, request, timeout=None):
                raise HTTPError(
                    request.full_url, 403, "no", EmailMessage(), io.BytesIO(b"secret-body")
                )

        with self.assertRaises(SentryForbidden) as caught:
            fetch_monitors("example-org", "test-token", opener=Opener())
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("SENTRY_AUTH_TOKEN_FULLSCOPE", str(caught.exception))
        self.assertNotIn("secret-body", str(caught.exception))
        self.assertNotIn("test-token", str(caught.exception))

    def test_next_link_off_the_collection_is_refused(self):
        class Opener:
            def open(self, request, timeout=None):
                link = '<https://evil.example/api/0/organizations/example-org/monitors/>; rel="next"; results="true"'
                return Response(b"[]", link)

        with self.assertRaises(AuditError) as caught:
            fetch_monitors("example-org", "test-token", opener=Opener())
        self.assertIn("monitors collection", str(caught.exception))
        self.assertNotIn("test-token", str(caught.exception))

    def test_integer_mute_flag_counts(self):
        monitor = parse_monitor_page(
            [
                {
                    "slug": "ci-example-nightly",
                    "status": "active",
                    "isMuted": 1,
                    "project": {"slug": "fleet-infra"},
                    "environments": [{"status": "ok", "lastCheckIn": "2026-11-11T00:00:00Z", "isMuted": 0}],
                }
            ]
        )[0]
        self.assertTrue(monitor.muted)

    def test_list_shares_one_deadline(self):
        class Opener:
            def open(self, request, timeout=None):
                raise AssertionError("network")

        with mock.patch("cron_monitor_audit.sentry._LIST_BUDGET_SECONDS", 0):
            with self.assertRaises(AuditError) as caught:
                fetch_monitors("example-org", "test-token", opener=Opener())
        self.assertIn("deadline", str(caught.exception))

    def test_nested_json_is_refused(self):
        value: object = 0
        for _ in range(40):
            value = [value]
        body = json.dumps(value).encode()
        with self.assertRaises(AuditError) as caught:
            load_bounded_json(body, "Sentry monitor list")
        self.assertIn("depth", str(caught.exception))

    def test_results_false_does_not_follow(self):
        class Opener:
            def open(self, request, timeout=None):
                link = '<https://us.sentry.io/api/0/organizations/example-org/monitors/?cursor=x>; rel="next"; results="false"'
                return Response(b"[]", link)

        self.assertEqual(fetch_monitors("example-org", "test-token", opener=Opener()), [])


class GithubClientTests(unittest.TestCase):
    def test_401_drops_the_token_and_a_missing_script_is_not_fatal(self):
        calls: list[str] = []

        class Opener:
            def open(self, request, timeout=None):
                calls.append(request.full_url)
                if "Authorization" in request.headers:
                    raise HTTPError(request.full_url, 401, "no", EmailMessage(), io.BytesIO(b"bad"))
                if request.full_url.endswith("/actions/workflows?per_page=100"):
                    body = json.dumps(
                        {
                            "workflows": [
                                {
                                    "name": "Sentry CI Report",
                                    "path": ".github/workflows/sentry-ci-report.yml",
                                    "state": "active",
                                },
                                {
                                    "name": "iOS TestFlight ship",
                                    "path": ".github/workflows/ios-ship.yml",
                                    "state": "disabled_manually",
                                },
                            ]
                        }
                    ).encode()
                    return Response(body)
                if request.full_url.endswith("sentry-ci-report.py"):
                    raise HTTPError(
                        request.full_url, 404, "missing", EmailMessage(), io.BytesIO(b"")
                    )
                return Response((FIXTURES / "reusable.yml").read_bytes())

        apps = load_apps(APPS)
        one = [app for app in apps if app.repo == "Socratic-Trade"]
        repos, token = fetch_repos(one, "bad-token", opener=Opener())
        self.assertEqual(token, "")
        self.assertTrue(repos[0].readable)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(
            repos[0].workflow_names,
            ("Sentry CI Report", "iOS TestFlight ship"),
        )
        self.assertTrue(any(path.endswith("sentry-ci-report.yml") for path, _ in repos[0].files))
        self.assertNotIn("bad-token", " ".join(calls))


class CliTests(unittest.TestCase):
    def test_fixture_does_not_touch_the_network(self):
        def boom(*args, **kwargs):
            raise AssertionError("network")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "sources"
            script = source / "scripts"
            script.mkdir(parents=True)
            (script / "sentry-ci-report.py").write_text(
                (FIXTURES / "reporter.py").read_text(), encoding="utf-8"
            )
            (source / "repos.json").write_text(
                json.dumps(
                    [
                        {
                            "full_name": "Simple-With-Us/Socratic-Trade",
                            "readable": True,
                            "names_known": True,
                            "workflow_names": ["CI", "Security"],
                            "files": [{"path": "scripts/sentry-ci-report.py"}],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            apps = directory / "apps.json"
            apps.write_text(
                json.dumps(
                    {
                        "owner": "Simple-With-Us",
                        "apps": [{"repo": "Socratic-Trade", "displayName": "Socratic Trade"}],
                    }
                ),
                encoding="utf-8",
            )
            json_path = directory / "out.json"
            markdown_path = directory / "out.md"
            with mock.patch("urllib.request.urlopen", boom):
                code = main(
                    [
                        "--apps",
                        str(apps),
                        "--monitors",
                        str(SMALL),
                        "--sources",
                        str(source),
                        "--json",
                        str(json_path),
                        "--markdown",
                        str(markdown_path),
                    ]
                )
            self.assertEqual(code, 0)
            report = json.loads(json_path.read_text(encoding="utf-8"))
            slugs = {row["slug"] for row in report["orphans"]}
            self.assertIn("ci-socratic-trade-vercel-production-catch-up", slugs)
            self.assertIn("ci-socratic-trade-gone-workflow", slugs)
            self.assertTrue(report["read_only"])
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("## Orphans", markdown)
            self.assertIn("\u00a0 ", markdown)

    def test_missing_token_exits_2_without_a_call(self):
        def boom(*args, **kwargs):
            raise AssertionError("network")

        env = {"SENTRY_AUTH_TOKEN_FULLSCOPE": "", "SENTRY_AUTH_TOKEN": ""}
        with mock.patch.dict("os.environ", env, clear=False):
            with mock.patch("cron_monitor_audit.sentry.build_opener", boom):
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    code = main(["--apps", str(APPS)])
        self.assertEqual(code, 2)
        self.assertIn("SENTRY_AUTH_TOKEN_FULLSCOPE", err.getvalue())
        self.assertNotIn("Bearer", err.getvalue())

    def test_bad_fixtures_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            apps = directory / "apps.json"
            apps.write_text(
                json.dumps({"owner": "Simple-With-Us", "apps": []}),
                encoding="utf-8",
            )
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = main(["--apps", str(apps), "--monitors", str(SMALL)])
            self.assertEqual(code, 2)
            self.assertIn("no apps", err.getvalue())

            apps.write_text(
                json.dumps(
                    {
                        "owner": "Simple-With-Us",
                        "apps": [{"repo": "Socratic-Trade", "displayName": "Socratic Trade"}],
                    }
                ),
                encoding="utf-8",
            )
            source = directory / "sources"
            source.mkdir()
            (source / "repos.json").write_text(
                json.dumps(
                    [
                        {
                            "full_name": "Simple-With-Us/Socratic-Trade",
                            "files": "scripts/sentry-ci-report.py",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = main(
                    ["--apps", str(apps), "--monitors", str(SMALL), "--sources", str(source)]
                )
            self.assertEqual(code, 2)
            self.assertIn("not a list", err.getvalue())

            (source / "repos.json").write_text(
                json.dumps(
                    [
                        {
                            "full_name": "Simple-With-Us/Socratic-Trade",
                            "files": [{"path": "scripts/missing.py"}],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = main(
                    ["--apps", str(apps), "--monitors", str(SMALL), "--sources", str(source)]
                )
            self.assertEqual(code, 2)
            self.assertIn("could not read", err.getvalue())

            (source / "repos.json").write_text(
                json.dumps(
                    [
                        {
                            "full_name": "Simple-With-Us/Socratic-Trade",
                            "workflow_names": [],
                            "files": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            link = directory / "linked"
            link.symlink_to(directory)
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = main(
                    [
                        "--apps",
                        str(apps),
                        "--monitors",
                        str(SMALL),
                        "--sources",
                        str(source),
                        "--json",
                        str(link / "out.json"),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("symlinked", err.getvalue())


class WorkflowTests(unittest.TestCase):
    def test_weekly_workflow_skips_without_the_read_secret(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("pull_request:", text)
        self.assertIn("cron:", text)
        self.assertIn("SENTRY_AUTH_TOKEN_FULLSCOPE", text)
        self.assertIn("github.event_name != 'pull_request'", text)
        self.assertIn("::notice::", text)
        self.assertIn("python3 -m cron_monitor_audit", text)
        self.assertIn('"$GITHUB_STEP_SUMMARY"', text)
        self.assertNotIn("POST", text)
        self.assertNotIn("PUT", text)
        self.assertNotIn("DELETE", text)


if __name__ == "__main__":
    unittest.main()
