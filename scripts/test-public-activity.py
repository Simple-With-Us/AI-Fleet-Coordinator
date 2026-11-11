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
                if key not in {"FLEET_OWNER", "SITE_BASE_URL", "FLEET_REPOS", "GITHUB_TOKEN"}
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

    def test_main_copies_only_referenced_logo_basenames(self) -> None:
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
                if key not in {"FLEET_OWNER", "SITE_BASE_URL", "FLEET_REPOS"}
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
                r"--(st|ct|um|dd|ps|ar|cl|shared|fleet|bf|hh|clutch|cc):\s*(#[0-9a-fA-F]{6})",
                rendered,
            )
        )
        self.assertEqual(len(palette), 13)
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
        """A manual run with no FLEET_OWNER or SITE_BASE_URL must not emit jaywedgeworth22.github.io."""
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
                if key not in {"FLEET_OWNER", "SITE_BASE_URL", "FLEET_REPOS"}
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


if __name__ == "__main__":
    unittest.main()
