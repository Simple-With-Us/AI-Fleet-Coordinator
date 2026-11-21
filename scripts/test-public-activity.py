#!/usr/bin/env python3
"""Regression checks for the public activity publication boundary."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_activity_repos import (
    DEFAULT_FLEET_OWNER,
    HISTORICAL_PUBLIC_REPO_ALIASES,
    PUBLIC_REPOS,
    select_public_repos,
)

spec = importlib.util.spec_from_file_location(
    "digest", Path(__file__).resolve().parent / "build-fleet-daily-digest.py"
)
assert spec and spec.loader
digest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = digest
spec.loader.exec_module(digest)

calendar_spec = importlib.util.spec_from_file_location(
    "agent_calendar", Path(__file__).resolve().parent / "build-agent-calendar.py"
)
assert calendar_spec and calendar_spec.loader
agent_calendar = importlib.util.module_from_spec(calendar_spec)
sys.modules[calendar_spec.name] = agent_calendar
calendar_spec.loader.exec_module(agent_calendar)


class Reply:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    def __enter__(self) -> Reply:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.data).encode()


def assert_public_artifact_boundary(root: Path, names: tuple[str, ...]) -> None:
    allowed = {repo.casefold() for repo in PUBLIC_REPOS} | set(
        HISTORICAL_PUBLIC_REPO_ALIASES.keys()
    )
    for name in names:
        body = (root / name).read_text()
        # RFC 5545 line folding can split a URL across CRLF + space.
        body = re.sub(r"\r?\n[ \t]", "", body)
        linked_repos = re.findall(
            r"https://github\.com/Simple-With-Us/([^/\s?#)\"<>]+)", body, re.IGNORECASE
        )
        if not all(repo.casefold() in allowed for repo in linked_repos):
            raise AssertionError(f"disallowed repository link in {name}")
        if "### Effort board" in body or "<h3>Effort board</h3>" in body:
            raise AssertionError(f"effort section in {name}")


class PublicActivityTests(unittest.TestCase):
    def test_current_public_surfaces_keep_visibility_checks(self) -> None:
        def api(request: object, timeout: int) -> Reply:
            repo = request.full_url.rsplit("/", 1)[-1]  # type: ignore[attr-defined]
            return Reply(
                {
                    "private": repo == "FleetLink",
                    "visibility": "private" if repo == "FleetLink" else "public",
                    "owner": {"login": "Simple-With-Us"},
                }
            )

        with patch("public_activity_repos.urllib.request.urlopen", side_effect=api) as get:
            selected = select_public_repos(
                "Simple-With-Us",
                ["FleetLink", "Simple-With-Us", "Fleet-OPS", "fleetlink-legacy"],
                "fixture",
            )
        self.assertEqual(selected, ["Simple-With-Us"])
        # Private inventory is rejected before even querying its visibility.
        self.assertEqual(get.call_count, 2)

    def test_workflows_use_shared_repository_defaults(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("fleet-activity-site.yml", "agent-calendar.yml"):
            workflow = (root / ".github" / "workflows" / name).read_text()
            self.assertNotIn("FLEET_REPOS:", workflow)
        self.assertEqual(digest.DEFAULT_REPOS, PUBLIC_REPOS)
        self.assertEqual(agent_calendar.DEFAULT_REPOS, PUBLIC_REPOS)

    def test_live_seats_render_attribution_without_missing_marks(self) -> None:
        registry = json.loads((Path(__file__).resolve().parents[1] / "fleet-apps.json").read_text())
        for seat in registry["seats"]:
            if seat.get("retired"):
                continue
            with self.subTest(seat=seat["tag"]):
                agents, clean = digest.extract_agents_and_clean(
                    f"[{seat['tag']}] Refresh public feed"
                )
                self.assertTrue(agents)
                self.assertEqual(clean, "Refresh public feed")
                rendered = digest.agent_icons_html(agents)
                for filename in re.findall(r'agent-logos/([^"<>]+)', rendered):
                    self.assertTrue((digest.LOGO_DIR / filename).is_file(), filename)

    def test_new_platforms_and_current_envelopes(self) -> None:
        cases = {
            "[OPENCODE/ZC] Refresh public feed": ["opencode", "zcode"],
            "[ECHO/WISE] Refresh public feed": ["instinct"],
            "[BF-ENGINEER/GB-FIXER] Refresh public feed": ["app-bf", "grok-bot"],
            "[JET-CHAT] Refresh public feed": ["chatgpt"],
            "[MiniMax Code/Muse Code/Kimi Code/Z Code] Refresh public feed": [
                "minimax",
                "muse",
                "kimi",
                "zcode",
            ],
            "[OPENCODE:\u200a\u200a\u200aHH.1.S\u200a\u200a📁\u200aAFC] Refresh public feed": [
                "opencode"
            ],
            "👀[CODEX:\u200a\u200a\u200aHH.1.S] Refresh public feed": ["codex"],
            "👀[ECHO\u200a\u200a📁\u200aAFC] Refresh public feed": ["instinct"],
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                agents, clean = digest.extract_agents_and_clean(title)
                self.assertEqual(agents, expected)
                self.assertEqual(clean, "Refresh public feed")
        fallback = digest.agent_icons_html(["opencode"])
        self.assertIn("OpenCode</span>", fallback)
        self.assertNotIn("<img", fallback)
        for title in (
            "Fix Clutch connection settings",
            "Clutch remote sessions",
            "Support OpenCode and ChatGPT clients",
        ):
            self.assertEqual(digest.extract_agents_and_clean(title), ([], title))

    def test_historical_chained_envelopes_are_cleaned_as_one_header(self) -> None:
        for header in (
            "[OPENCODE/ZC: HH.1.S]",
            "[OPENCODE/ZC: HH.1.S 📁AFC]",
            "👀[OPENCODE/ZC:\u200a\u200a\u200aHH.1.S\u200a\u200a📁\u200aAFC]",
            "[OPENCODE/ZC\u200a\u200a📁\u200aAFC]",
        ):
            with self.subTest(header=header):
                agents, clean = digest.extract_agents_and_clean(f"{header} Refresh public feed")
                self.assertEqual(agents, ["opencode", "zcode"])
                self.assertEqual(clean, "Refresh public feed")

    def test_numeric_bot_family_issue_refs_are_not_agent_attribution(self) -> None:
        for issue in ("BF-2419", "GB-123456"):
            for title in (
                f"Fix {issue} runtime",
                f"[{issue}] Refresh public feed",
                f"{issue}: Refresh public feed",
                f"Fix {issue}-followup runtime",
            ):
                with self.subTest(title=title):
                    self.assertEqual(digest.extract_agents_and_clean(title), ([], title))
            title = f"Fix {issue} runtime"
            self.assertEqual(
                digest.extract_agents_and_clean(f"[CODEX] {title}"), (["codex"], title)
            )

    def test_legend_and_favicon_use_current_public_sources(self) -> None:
        rendered = digest.build_html(
            [],
            datetime(2026, 11, 13, tzinfo=UTC),
            ZoneInfo("America/Chicago"),
            "https://example.com",
        )
        self.assertIn("Muster / AI Fleet Coordinator", rendered)
        self.assertIn("FleetLink", rendered)
        self.assertIn("Simple With Us", rendered)
        self.assertIn("--swu: #4b5563;", rendered)
        self.assertIn(".repo-swu { background: var(--swu); }", rendered)
        self.assertIn('<span class="repo repo-swu">SWU</span>', rendered)
        self.assertIn('href="agent-logos/app-afc.png"', rendered)
        self.assertIn(
            'href="agent-logos/app-afc-dark.webp" media="(prefers-color-scheme: dark)"', rendered
        )
        self.assertNotIn("app-fleet.", rendered)
        self.assertNotIn("app-swu.", rendered)
        for filename in re.findall(r'(?:src|href)="agent-logos/([^"<>]+)"', rendered):
            self.assertTrue((digest.LOGO_DIR / filename).is_file(), filename)

        fixture_path = (
            Path(__file__).resolve().parent.parent
            / "visual-tests"
            / "tests"
            / "fixtures"
            / "digest-2day.html"
        )
        fixture_html = fixture_path.read_text(encoding="utf-8")
        self.assertIn("--swu: #4b5563;", fixture_html)
        self.assertIn(".repo-swu { background: var(--swu); }", fixture_html)
        self.assertIn(
            '<span class="repo repo-swu">SWU</span><span class="legend-label">Simple With Us</span>',
            fixture_html,
        )
        self.assertIn('href="agent-logos/app-afc.png"', fixture_html)
        self.assertNotIn("app-fleet.", fixture_html)
        self.assertNotIn("app-swu.", fixture_html)

    def test_built_public_artifacts_contain_only_allowlisted_links(self) -> None:
        """Published snapshots live on published/fleet-activity, not stale main copies."""
        day = date(2026, 9, 26)
        pr = {
            "repo": "BotFleet",
            "number": 123,
            "title": "Fix onboarding",
            "url": "https://github.com/Simple-With-Us/BotFleet/pull/123",
            "user": "jay",
        }
        commit = {
            "sha": "a" * 40,
            "commit": {"message": "feat: fixture", "author": {"date": "2026-09-26T12:00:00Z"}},
        }
        artifact_names = (
            "site/index.html",
            "site/digest.md",
            "calendar/daily-digest.ics",
            "calendar/agent-activity.ics",
            "site/calendar/daily-digest.ics",
            "site/calendar/agent-activity.ics",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "FLEET_OWNER",
                    "SITE_BASE_URL",
                    "FLEET_REPOS",
                    "GITHUB_TOKEN",
                    "LINEAR_API_KEY",
                    "FLEET_LINEAR_API_KEY",
                }
            }
            env.update(
                {
                    "SITE_OUT": str(root / "site"),
                    "ICS_OUT": str(root / "calendar" / "daily-digest.ics"),
                    "CALENDAR_OUT": str(root / "calendar" / "agent-activity.ics"),
                    "GITHUB_TOKEN": "fixture",
                }
            )
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(digest, "token", return_value="fixture"),
                    patch.object(digest, "select_public_repos", return_value=["BotFleet"]),
                    patch.object(digest, "fetch_merged_prs", return_value=[(day, pr)]),
                    patch.object(digest, "fetch_issue_churn", return_value=([], [])),
                    patch.object(agent_calendar, "select_public_repos", return_value=["BotFleet"]),
                    patch.object(agent_calendar, "list_commits", return_value=[commit]),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(agent_calendar.main(), 0)
                    self.assertEqual(digest.main(), 0)
            finally:
                os.chdir(cwd)
            assert_public_artifact_boundary(root, artifact_names)

    def test_private_and_unlisted_repos_never_enter_feed(self) -> None:
        def api(request: object, timeout: int) -> Reply:
            url = request.full_url  # type: ignore[attr-defined]
            if url.endswith("/BotFleet"):
                return Reply(
                    {"private": False, "visibility": "public", "owner": {"login": "Simple-With-Us"}}
                )
            return Reply(
                {"private": True, "visibility": "private", "owner": {"login": "Simple-With-Us"}}
            )

        with patch("public_activity_repos.urllib.request.urlopen", side_effect=api):
            selected = select_public_repos(
                "Simple-With-Us", ["BotFleet", "fleet-ops", "Clutch", "ContactLogo"], "fixture"
            )
        self.assertEqual(selected, ["BotFleet"])

    def test_visibility_api_failure_is_fail_closed(self) -> None:
        with (
            patch(
                "public_activity_repos.urllib.request.urlopen",
                side_effect=urllib.error.URLError("fixture"),
            ),
            self.assertRaises(RuntimeError),
        ):
            select_public_repos("Simple-With-Us", ["BotFleet"], "fixture")

    def test_public_pr_survives_without_effort_board_material(self) -> None:
        day = date(2026, 9, 26)
        pr = {
            "repo": "BotFleet",
            "number": 123,
            "title": "Fix onboarding",
            "url": "https://github.com/Simple-With-Us/BotFleet/pull/123",
            "user": "jay",
        }
        buckets = digest.bucket_all([(day, pr)], [], [])
        days = list(buckets.values())
        now = datetime(2026, 9, 26, tzinfo=UTC)
        tz = ZoneInfo("America/Chicago")
        rendered = (
            digest.build_markdown(days, now, tz, "https://example.com"),
            digest.build_html(days, now, tz, "https://example.com"),
            digest.build_daily_ics(days, now, "https://example.com"),
        )
        for body in rendered:
            self.assertIn("BotFleet", body)
            self.assertNotIn("Effort board", body)
            self.assertNotIn("effort rows", body)
            self.assertNotIn("fleet-ops", body.casefold())
        self.assertIn(pr["url"], rendered[0])
        self.assertIn(pr["url"], rendered[1])

    def test_html_day_cards_use_content_visibility_and_details_for_older_days(self) -> None:
        days = [
            digest.DayBucket(
                day=date(2026, 9, 27),
                merged_prs=[
                    {
                        "repo": "BotFleet",
                        "number": 1,
                        "title": "One",
                        "url": "https://example.com/1",
                        "user": "jay",
                    }
                ],
            ),
            digest.DayBucket(
                day=date(2026, 9, 26),
                merged_prs=[
                    {
                        "repo": "BotFleet",
                        "number": 2,
                        "title": "Two",
                        "url": "https://example.com/2",
                        "user": "jay",
                    }
                ],
            ),
        ]
        rendered = digest.build_html(
            days,
            datetime(2026, 9, 27, tzinfo=UTC),
            ZoneInfo("America/Chicago"),
            "https://example.com",
        )
        self.assertIn("content-visibility", rendered)
        self.assertIn("<details", rendered)
        # newest day stays a plain expanded section; older days collapse
        self.assertIn('<section class="day"', rendered)
        self.assertIn('id="2026-09-26"', rendered)

    def test_html_links_underlined_focus_visible_and_dark_mode(self) -> None:
        rendered = digest.build_html(
            [],
            datetime(2026, 9, 27, tzinfo=UTC),
            ZoneInfo("America/Chicago"),
            "https://example.com",
        )
        self.assertIn("a:focus-visible", rendered)
        self.assertIn("prefers-color-scheme: dark", rendered)
        self.assertNotIn("text-decoration: none", rendered)

    def test_build_output_only_referenced_logo_basenames(self) -> None:
        day = date(2026, 9, 26)
        pr = {
            "repo": "BotFleet",
            "number": 123,
            "title": "[CODEX] Fix onboarding",
            "url": "https://github.com/Simple-With-Us/BotFleet/pull/123",
            "user": "jay",
        }
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "FLEET_OWNER",
                    "SITE_BASE_URL",
                    "FLEET_REPOS",
                    "LINEAR_API_KEY",
                    "FLEET_LINEAR_API_KEY",
                }
            }
            env.update(
                {"SITE_OUT": str(Path(tmp) / "site"), "ICS_OUT": str(Path(tmp) / "daily.ics")}
            )
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(digest, "token", return_value="fixture"),
                    patch.object(digest, "select_public_repos", return_value=["BotFleet"]),
                    patch.object(digest, "fetch_merged_prs", return_value=[(day, pr)]),
                    patch.object(digest, "fetch_issue_churn", return_value=([], [])),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(digest.main(), 0)
            finally:
                os.chdir(cwd)
            copied = {q.name for q in (Path(tmp) / "site" / "agent-logos").iterdir()}
            self.assertIn("app-bf.png", copied)
            self.assertIn("codex.svg", copied)
            # the legend references every repo icon, so app-dd.png ships too
            self.assertIn("app-dd.png", copied)
            self.assertIn("app-afc.png", copied)
            self.assertIn("app-afc-dark.webp", copied)
            # non-image extras and unused seat marks stay out of the published tree
            self.assertNotIn("README.md", copied)
            self.assertNotIn("badges", copied)
            self.assertNotIn("owner.png", copied)
            self.assertNotIn("xai.svg", copied)

    def test_badge_backgrounds_meet_aa_contrast_with_white_text(self) -> None:
        def lum(hexcolor: str) -> float:
            hexcolor = hexcolor.lstrip("#")
            r, g, b = (int(hexcolor[i : i + 2], 16) / 255 for i in (0, 2, 4))
            f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

        rendered = digest.build_html(
            [],
            datetime(2026, 9, 26, tzinfo=UTC),
            ZoneInfo("America/Chicago"),
            "https://example.com",
        )
        palette = dict(
            re.findall(
                r"--(st|ct|um|dd|ps|ar|cl|shared|fleet|bf|hh|clutch|cc|fl|swu):\s*(#[0-9a-fA-F]{6})",
                rendered,
            )
        )
        self.assertEqual(len(palette), 15)
        white = lum("#ffffff")
        for name, bg in sorted(palette.items()):
            ratio = (white + 0.05) / (lum(bg) + 0.05)
            self.assertGreaterEqual(ratio, 4.5, f"--{name} {bg} fails AA with white text")

    def test_day_cards_wrap_long_tokens(self) -> None:
        rendered = digest.build_html(
            [],
            datetime(2026, 9, 26, tzinfo=UTC),
            ZoneInfo("America/Chicago"),
            "https://example.com",
        )
        self.assertIn("overflow-wrap", rendered)

    def test_default_owner_is_the_org_and_never_the_retired_pages_host(self) -> None:
        """A manual run with no FLEET_OWNER or SITE_BASE_URL must not emit simple-with-us.github.io."""
        self.assertEqual(DEFAULT_FLEET_OWNER, "Simple-With-Us")
        day = date(2026, 9, 26)
        pr = {
            "repo": "BotFleet",
            "number": 123,
            "title": "Fix onboarding",
            "url": "https://github.com/Simple-With-Us/BotFleet/pull/123",
            "user": "jay",
        }
        seen: list[str] = []

        def select(owner: str, requested: list[str], _token: str) -> list[str]:
            seen.append(owner)
            return ["BotFleet"]

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "FLEET_OWNER",
                    "SITE_BASE_URL",
                    "FLEET_REPOS",
                    "LINEAR_API_KEY",
                    "FLEET_LINEAR_API_KEY",
                }
            }
            env.update(
                {"SITE_OUT": str(Path(tmp) / "site"), "ICS_OUT": str(Path(tmp) / "daily.ics")}
            )
            cwd = os.getcwd()
            os.chdir(
                tmp
            )  # the builder reads calendar/agent-activity.ics from the working directory
            try:
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(digest, "token", return_value="fixture"),
                    patch.object(digest, "select_public_repos", side_effect=select),
                    patch.object(digest, "fetch_merged_prs", return_value=[(day, pr)]),
                    patch.object(digest, "fetch_issue_churn", return_value=([], [])),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(digest.main(), 0)
            finally:
                os.chdir(cwd)
            outputs = {
                "digest.md": (Path(tmp) / "site" / "digest.md").read_text(),
                "index.html": (Path(tmp) / "site" / "index.html").read_text(),
                "daily.ics": (Path(tmp) / "daily.ics").read_text(),
            }
        self.assertEqual(seen, ["Simple-With-Us"])
        for name, body in outputs.items():
            with self.subTest(output=name):
                self.assertNotIn("jaywedgeworth22", body.casefold())
                self.assertIn("https://simple-with-us.github.io/AI-Fleet-Coordinator", body)

    def test_agent_calendar_default_owner_is_the_org(self) -> None:
        commit = {
            "sha": "a" * 40,
            "commit": {"message": "feat: fixture", "author": {"date": "2026-09-26T12:00:00Z"}},
        }
        seen: list[str] = []

        def select(owner: str, requested: list[str], _token: str) -> list[str]:
            seen.append(owner)
            return ["BotFleet"]

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "agent-activity.ics"
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"FLEET_OWNER", "FLEET_REPOS"}
            }
            env.update({"GITHUB_TOKEN": "fixture", "CALENDAR_OUT": str(out)})
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(agent_calendar, "select_public_repos", side_effect=select),
                patch.object(agent_calendar, "list_commits", return_value=[commit]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(agent_calendar.main(), 0)
            body = out.read_text()
        self.assertEqual(seen, ["Simple-With-Us"])
        self.assertIn("https://github.com/Simple-With-Us/BotFleet/commit/", body)
        self.assertNotIn("jaywedgeworth22", body.casefold())

    def test_retired_harness_prefixes_strip_under_clutch(self) -> None:
        for title in (
            "Harness: fix startup",
            "[Harness] fix startup",
            "HR \u2014 fix startup",
            "[Clutch] fix startup",
            "CLUTCH: fix startup",
            "CK: fix startup",
        ):
            with self.subTest(title=title):
                self.assertEqual(digest.strip_redundant_repo_label(title, "Clutch"), "fix startup")

    def test_public_team_map_excludes_private_teams(self) -> None:
        mapping = digest.load_public_team_map(["BotFleet", "codecaps", "AI-Fleet-Coordinator"])
        self.assertEqual(
            mapping,
            {
                "BF": "BotFleet",
                "CC": "codecaps",
                "AFC": "AI-Fleet-Coordinator",
            },
        )
        self.assertNotIn("OPS", mapping)
        self.assertNotIn("FLL", mapping)

    def test_linear_issue_churn_enforces_public_boundary_and_pagination(self) -> None:
        tz = ZoneInfo("America/Chicago")
        since = date(2026, 11, 17)
        pages = [
            {
                "issues": {
                    "nodes": [
                        {
                            "identifier": "AFC-9901",
                            "number": 9901,
                            "title": "Fixture status endpoint: pass wake outcomes",
                            "url": "https://linear.example.test/workspace/issue/AFC-9901/fixture",
                            "createdAt": "2026-11-18T15:16:09Z",
                            "completedAt": "2026-11-18T17:39:05Z",
                            "canceledAt": None,
                            "state": {"name": "Done", "type": "completed"},
                            "team": {"key": "AFC"},
                            "delegate": {"name": "Claude"},
                            "creator": {"name": "Claude"},
                            "labels": {"nodes": [{"name": "claimed:CLAUDE"}]},
                        },
                        {
                            "identifier": "OPS-9902",
                            "number": 9902,
                            "title": "Private ops fixture must never appear",
                            "url": "https://linear.example.test/workspace/issue/OPS-9902/private",
                            "createdAt": "2026-11-18T15:00:00Z",
                            "completedAt": "2026-11-18T16:00:00Z",
                            "canceledAt": None,
                            "state": {"name": "Done", "type": "completed"},
                            "team": {"key": "OPS"},
                            "delegate": {"name": "Claude"},
                            "creator": {"name": "Claude"},
                            "labels": {"nodes": []},
                        },
                        {
                            "identifier": "AFC-9903",
                            "number": 9903,
                            "title": "Old fixture issue updated today",
                            "url": "https://linear.example.test/workspace/issue/AFC-9903/old",
                            "createdAt": "2026-08-20T18:05:54Z",
                            "completedAt": "2026-09-13T07:50:39Z",
                            "canceledAt": None,
                            "state": {"name": "Done", "type": "completed"},
                            "team": {"key": "AFC"},
                            "delegate": None,
                            "creator": {"name": "Project Owner"},
                            "labels": {"nodes": []},
                        },
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"},
                }
            },
            {
                "issues": {
                    "nodes": [
                        {
                            "identifier": "CC-9904",
                            "number": 9904,
                            "title": "Fix fixture timeline",
                            "url": "https://linear.example.test/workspace/issue/CC-9904/fix",
                            "createdAt": "2026-11-18T02:00:00Z",  # 2026-11-17 20:00 CST
                            "completedAt": None,
                            "canceledAt": None,
                            "state": {"name": "Todo", "type": "unstarted"},
                            "team": {"key": "CC"},
                            "delegate": None,
                            "creator": {"name": "Antigravity (AG)"},
                            "labels": {"nodes": [{"name": "seat:AG"}]},
                        },
                        {
                            "identifier": "BF-9905",
                            "number": 9905,
                            "title": "BF-9900 Scope 3: Editable fixture threshold",
                            "url": "https://linear.example.test/workspace/issue/BF-9905/scope-3",
                            "createdAt": "2026-11-10T12:00:00Z",
                            "completedAt": None,
                            "canceledAt": "2026-11-18T14:00:00Z",
                            "state": {"name": "Duplicate", "type": "duplicate"},
                            "team": {"key": "BF"},
                            "delegate": {"name": "Echo (Instinct)"},
                            "creator": {"name": "Project Owner"},
                            "labels": {"nodes": [{"name": "claimed:ECHO"}]},
                        },
                        {
                            "identifier": "BF-9906",
                            "number": 9906,
                            "title": "Owner filed and closed fixture",
                            "url": "https://linear.example.test/workspace/issue/BF-9906/owner-filed",
                            "createdAt": "2026-11-18T12:00:00Z",
                            "completedAt": "2026-11-18T13:00:00Z",
                            "canceledAt": None,
                            "state": {"name": "In Progress", "type": "started"},
                            "team": {"key": "BF"},
                            "delegate": None,
                            "creator": {"name": "Project Owner"},
                            "labels": {"nodes": [{"name": "claimed:CODEX"}]},
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": "cur-2"},
                }
            },
        ]
        seen_vars: list[dict[str, object]] = []
        seen_deadlines: list[float | None] = []

        def fake_graphql(
            _query: str,
            variables: dict[str, object],
            _tok: str,
            *,
            deadline: float | None = None,
        ) -> dict[str, object]:
            seen_vars.append(variables)
            seen_deadlines.append(deadline)
            return pages[len(seen_vars) - 1]

        with patch.object(digest, "linear_graphql", side_effect=fake_graphql):
            opened, closed = digest.fetch_linear_issue_churn(
                ["AI-Fleet-Coordinator", "BotFleet", "codecaps"], since, "fixture-linear-tok", tz
            )

        self.assertEqual(len(seen_vars), 2)
        self.assertEqual(seen_vars[0]["teamKeys"], ["AFC", "BF", "CC"])
        self.assertEqual(seen_vars[1]["cursor"], "cur-1")
        # Both pages share the exact same monotonic deadline.
        self.assertIsNotNone(seen_deadlines[0])
        self.assertEqual(seen_deadlines[0], seen_deadlines[1])

        opened_ids = [(d, it["identifier"], it["repo"], it["agents"]) for d, it in opened]
        closed_ids = [
            (d, it["identifier"], it["repo"], it["agents"], it["state"]) for d, it in closed
        ]
        self.assertEqual(
            opened_ids,
            [
                (date(2026, 11, 18), "AFC-9901", "AI-Fleet-Coordinator", ["claude"]),
                (date(2026, 11, 17), "CC-9904", "codecaps", ["ag"]),
                (date(2026, 11, 18), "BF-9906", "BotFleet", []),
            ],
        )
        # Includes BF-9906 even though its live state is reopened (In Progress) with completedAt set.
        self.assertEqual(
            closed_ids,
            [
                (date(2026, 11, 18), "AFC-9901", "AI-Fleet-Coordinator", ["claude"], "Done"),
                (date(2026, 11, 18), "BF-9905", "BotFleet", ["instinct"], "Duplicate"),
                (date(2026, 11, 18), "BF-9906", "BotFleet", ["codex"], "In Progress"),
            ],
        )

    def test_linear_pagination_fails_closed_on_incomplete_or_malformed_continuation(self) -> None:
        tz = ZoneInfo("America/Chicago")
        since = date(2026, 11, 17)

        def page_with(page_info: object) -> dict[str, object]:
            return {"issues": {"nodes": [], "pageInfo": page_info}}

        # 1. Malformed hasNextPage (missing / non-bool)
        for bad_info in ({"hasNextPage": None}, {"hasNextPage": "false"}, {}, "bad"):
            with (
                self.subTest(page_info=bad_info),
                patch.object(digest, "linear_graphql", return_value=page_with(bad_info)),
                self.assertRaises(RuntimeError),
            ):
                digest.fetch_linear_issue_churn(["BotFleet"], since, "fixture-tok", tz)

        # 2. hasNextPage=True with missing/empty/non-string endCursor
        for bad_cursor in (None, "", "   ", 123):
            with (
                self.subTest(cursor=bad_cursor),
                patch.object(
                    digest,
                    "linear_graphql",
                    return_value=page_with({"hasNextPage": True, "endCursor": bad_cursor}),
                ),
                self.assertRaises(RuntimeError),
            ):
                digest.fetch_linear_issue_churn(["BotFleet"], since, "fixture-tok", tz)

        # 3. Repeated cursor across pages
        repeated = [
            page_with({"hasNextPage": True, "endCursor": "cur-loop"}),
            page_with({"hasNextPage": True, "endCursor": "cur-loop"}),
        ]
        with (
            patch.object(digest, "linear_graphql", side_effect=repeated),
            self.assertRaisesRegex(RuntimeError, "repeated cursor"),
        ):
            digest.fetch_linear_issue_churn(["BotFleet"], since, "fixture-tok", tz)

        # 4. Exhausted max_pages budget while hasNextPage remains True
        budget_pages = [
            page_with({"hasNextPage": True, "endCursor": "cur-1"}),
            page_with({"hasNextPage": True, "endCursor": "cur-2"}),
        ]
        with (
            patch.object(digest, "linear_graphql", side_effect=budget_pages),
            self.assertRaisesRegex(RuntimeError, "max_pages"),
        ):
            digest.fetch_linear_issue_churn(["BotFleet"], since, "fixture-tok", tz, max_pages=2)

        # 5. Exhausted shared monotonic deadline
        with (
            patch.object(digest.time, "monotonic", side_effect=[100.0, 401.0]),
            self.assertRaisesRegex(RuntimeError, "deadline"),
        ):
            digest.fetch_linear_issue_churn(
                ["BotFleet"], since, "fixture-tok", tz, timeout_budget=300.0
            )

    def test_unmapped_malformed_linear_nodes_do_not_abort_public_digest(self) -> None:
        nodes = [
            {"team": {"key": "OPS"}, "state": None, "number": "bad"},
            {"team": None},
            None,
            {"team": {"key": 1}},
        ]
        with patch.object(
            digest,
            "linear_graphql",
            return_value={
                "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
            },
        ):
            self.assertEqual(
                digest.fetch_linear_issue_churn(
                    ["BotFleet"], date(2026, 11, 17), "fixture-tok", ZoneInfo("America/Chicago")
                ),
                ([], []),
            )

    def test_linear_issue_node_shape_validation_and_error_redaction(self) -> None:
        tz = ZoneInfo("America/Chicago")
        since = date(2026, 11, 17)
        bad_node = {
            "identifier": "BF-9901",
            "number": "not-an-int",
            "title": "Fixture",
            "url": "https://linear.example.test/workspace/issue/BF-9901/fixture",
            "createdAt": "2026-11-18T12:00:00Z",
            "state": {"name": "Done", "type": "completed"},
            "team": {"key": "BF"},
        }
        with (
            patch.object(
                digest,
                "linear_graphql",
                return_value={
                    "issues": {
                        "nodes": [bad_node],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                },
            ),
            self.assertRaises(ValueError),
        ):
            digest.fetch_linear_issue_churn(["BotFleet"], since, "fixture-tok", tz)

        # HTTP error must never leak third-party body, only status + allowlisted request id.
        http_err = urllib.error.HTTPError(
            digest.LINEAR_GRAPHQL,
            401,
            "Unauthorized",
            {"x-linear-request-id": "req-abc-123"},
            io.BytesIO(b'{"secret_pii":"sensitive-upstream-body"}'),
        )
        with (
            patch("urllib.request.urlopen", side_effect=http_err),
            self.assertRaises(RuntimeError) as ctx,
        ):
            digest.linear_graphql("query { viewer { id } }", {}, "fixture-tok")
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("req-abc-123", str(ctx.exception))
        self.assertNotIn("sensitive-upstream-body", str(ctx.exception))

        # GraphQL error array logs only error count and allowlisted codes, not raw messages.
        gql_reply = Reply(
            {
                "errors": [
                    {
                        "message": "sensitive upstream detail",
                        "extensions": {"code": "AUTHENTICATION_ERROR"},
                    }
                ]
            }
        )
        with (
            patch("urllib.request.urlopen", return_value=gql_reply),
            self.assertRaises(RuntimeError) as gql_ctx,
        ):
            digest.linear_graphql("query { viewer { id } }", {}, "fixture-tok")
        self.assertIn("AUTHENTICATION_ERROR", str(gql_ctx.exception))
        self.assertNotIn("sensitive upstream detail", str(gql_ctx.exception))

    def test_linear_issues_render_in_html_markdown_and_ics(self) -> None:
        day = date(2026, 11, 18)
        opened = [
            (
                day,
                {
                    "repo": "codecaps",
                    "number": 9904,
                    "identifier": "CC-9904",
                    "title": "Fix fixture timeline",
                    "url": "https://linear.example.test/workspace/issue/CC-9904/fix",
                    "agents": ["ag"],
                    "source": "linear",
                },
            )
        ]
        closed = [
            (
                day,
                {
                    "repo": "AI-Fleet-Coordinator",
                    "number": 9901,
                    "identifier": "AFC-9901",
                    "title": "Fixture status endpoint: pass wake outcomes",
                    "url": "https://linear.example.test/workspace/issue/AFC-9901/fixture",
                    "state": "Done",
                    "state_type": "completed",
                    "agents": ["claude"],
                    "source": "linear",
                },
            ),
            (
                day,
                {
                    "repo": "BotFleet",
                    "number": 9905,
                    "identifier": "BF-9905",
                    "title": "BF-9900 Scope 3: Editable fixture threshold",
                    "url": "https://linear.example.test/workspace/issue/BF-9905/scope-3",
                    "state": "Duplicate",
                    "state_type": "duplicate",
                    "agents": ["instinct"],
                    "source": "linear",
                },
            ),
        ]
        days = sorted(
            digest.bucket_all([], opened, closed).values(), key=lambda d: d.day, reverse=True
        )
        now = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
        tz = ZoneInfo("America/Chicago")

        md = digest.build_markdown(days, now, tz, "https://example.com")
        html_doc = digest.build_html(days, now, tz, "https://example.com")
        ics_doc = digest.build_daily_ics(days, now, "https://example.com")

        self.assertIn("0 PRs merged · 1 issues opened · 2 issues closed", md)
        self.assertIn(
            "**AFC** `Claude` [AFC-9901](https://linear.example.test/workspace/issue/AFC-9901/fixture):",
            md,
        )
        self.assertIn("BF-9900 Scope 3: Editable fixture threshold _(Duplicate)_", md)

        self.assertIn(
            '<a href="https://linear.example.test/workspace/issue/AFC-9901/fixture">AFC-9901</a>',
            html_doc,
        )
        self.assertIn("agent-logos/claude.svg", html_doc)
        self.assertIn("agent-logos/ag.svg", html_doc)
        self.assertIn('title="Instinct">Instinct</span>', html_doc)
        self.assertIn("agent-logos/app-cc.png", html_doc)
        self.assertIn("BF-9900 Scope 3: Editable fixture threshold", html_doc)
        self.assertIn('<span class="issue-state">(Duplicate)</span>', html_doc)
        self.assertNotIn("#9901", html_doc)

        unfolded_ics = re.sub(r"\r?\n[ \t]", "", ics_doc)
        self.assertIn("SUMMARY:Fleet: 0 merged · 1 opened · 2 closed", unfolded_ics)
        self.assertIn("[AFC-9901] (claude) Fixture status endpoint", unfolded_ics)
        self.assertIn(
            "[BF-9905] (instinct) BF-9900 Scope 3: Editable fixture threshold (Duplicate)",
            unfolded_ics,
        )

    def test_past_linear_close_keeps_event_and_labels_current_reopened_state(self) -> None:
        item = {
            "repo": "BotFleet",
            "number": 9906,
            "identifier": "BF-9906",
            "title": "Past closure fixture",
            "url": "https://linear.example.test/issue/BF-9906",
            "state": "In Progress <review>",
            "state_type": "started",
            "source": "linear",
        }
        day = digest.DayBucket(day=date(2026, 11, 18), issues_closed=[item])
        now = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
        note = "closed that day; now In Progress <review>"
        self.assertEqual(digest._issue_state_note(item), note)
        md = digest.build_markdown([day], now, ZoneInfo("America/Chicago"), "https://example.com")
        page = digest.build_html([day], now, ZoneInfo("America/Chicago"), "https://example.com")
        cal = re.sub(r"\r?\n[ \t]", "", digest.build_daily_ics([day], now, "https://example.com"))
        self.assertIn(note, md)
        self.assertIn("closed that day; now In Progress &lt;review&gt;", page)
        self.assertNotIn("In Progress <review>", page)
        self.assertIn(note.replace(";", r"\;"), cal)
        self.assertIn("1 issues closed", md)
        self.assertEqual(digest._issue_state_note({**item, "source": "github"}), "")
        self.assertEqual(digest._issue_state_note({**item, "state_type": "completed"}), "")

    def test_main_fetches_linear_issues_when_key_set_and_creates_no_output_on_incomplete_fetch(
        self,
    ) -> None:
        day = date(2026, 11, 18)
        lin_item = {
            "repo": "BotFleet",
            "number": 9905,
            "identifier": "BF-9905",
            "title": "Fix fixture build",
            "url": "https://linear.example.test/workspace/issue/BF-9905/fix",
            "state": "Done",
            "state_type": "completed",
            "agents": ["claude"],
            "source": "linear",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "SITE_OUT": str(root / "site"),
                "ICS_OUT": str(root / "calendar" / "daily-digest.ics"),
                "GITHUB_TOKEN": "fixture",
                "LINEAR_API_KEY": "fixture-linear-tok",
            }
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(digest, "select_public_repos", return_value=["BotFleet"]),
                    patch.object(digest, "fetch_merged_prs", return_value=[]),
                    patch.object(digest, "fetch_issue_churn", return_value=([], [])),
                    patch.object(
                        digest,
                        "fetch_linear_issue_churn",
                        return_value=([(day, lin_item)], [(day, lin_item)]),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(digest.main(), 0)
            finally:
                os.chdir(cwd)
            assert_public_artifact_boundary(root, ("site/index.html", "site/digest.md"))
            self.assertIn("BF-9905", (root / "site" / "index.html").read_text())
            self.assertIn(
                "1 issues opened · 1 issues closed", (root / "site" / "digest.md").read_text()
            )

        # Incomplete Linear pagination must abort main() before creating any output tree.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site_dir = root / "site"
            ics_file = root / "calendar" / "daily-digest.ics"
            env = {
                "SITE_OUT": str(site_dir),
                "ICS_OUT": str(ics_file),
                "GITHUB_TOKEN": "fixture",
                "LINEAR_API_KEY": "fixture-linear-tok",
            }
            incomplete_page = {
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": None},
                }
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(digest, "select_public_repos", return_value=["BotFleet"]),
                patch.object(digest, "fetch_merged_prs", return_value=[]),
                patch.object(digest, "fetch_issue_churn", return_value=([], [])),
                patch.object(digest, "linear_graphql", return_value=incomplete_page),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(RuntimeError),
            ):
                digest.main()
            self.assertFalse(site_dir.exists())
            self.assertFalse(ics_file.exists())

    def test_fleet_activity_workflow_wires_scoped_infisical_linear_fetch(self) -> None:
        wf = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "fleet-activity-site.yml"
        ).read_text(encoding="utf-8")
        steps = [m.group(0) for m in re.finditer(r"(?ms)^ {6}-[ \t]+.*?(?=^ {6}-[ \t]+|\Z)", wf)]
        infisical_steps = [
            step
            for step in steps
            if "node scripts/infisical-fetch.mjs" in step
            and "INFISICAL_SECRET_PATH: /fleet-activity" in step
        ]
        self.assertEqual(len(infisical_steps), 1)
        fetch_step = infisical_steps[0]
        self.assertIn("INFISICAL_NAMES: LINEAR_API_KEY", fetch_step)
        self.assertIn("INFISICAL_FLEET_ACTIVITY_LINEAR_READ_TOKEN", fetch_step)
        self.assertNotIn("INFISICAL_REQUIRED", fetch_step)
        self.assertNotIn("GH_FALLBACK_LINEAR_API_KEY", fetch_step)
        self.assertNotIn("secrets.LINEAR_API_KEY", wf)

        digest_steps = [
            step
            for step in steps
            if re.search(r"(?m)^\s*run:\s*python3 scripts/build-fleet-daily-digest\.py\s*$", step)
        ]
        self.assertEqual(len(digest_steps), 1)
        self.assertRegex(
            digest_steps[0],
            r"(?m)^\s*LINEAR_API_KEY:\s*\$\{\{\s*env\.LINEAR_API_KEY\s*\}\}\s*$",
        )


if __name__ == "__main__":
    unittest.main()
