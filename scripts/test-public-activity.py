#!/usr/bin/env python3
"""Regression checks for the public activity publication boundary."""
from __future__ import annotations

import json
import importlib.util
import re
import sys
import unittest
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_activity_repos import HISTORICAL_PUBLIC_REPO_ALIASES, PUBLIC_REPOS, select_public_repos  # noqa: E402

spec = importlib.util.spec_from_file_location("digest", Path(__file__).resolve().parent / "build-fleet-daily-digest.py")
assert spec and spec.loader
digest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = digest
spec.loader.exec_module(digest)


class Reply:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.data).encode()


class PublicActivityTests(unittest.TestCase):
    def test_checked_in_public_artifacts_contain_only_allowlisted_links(self) -> None:
        root = Path(__file__).resolve().parents[1]
        names = (
            "site/index.html", "site/digest.md", "calendar/daily-digest.ics",
            "calendar/agent-activity.ics", "site/calendar/daily-digest.ics",
            "site/calendar/agent-activity.ics",
        )
        allowed = {repo.casefold() for repo in PUBLIC_REPOS} | set(HISTORICAL_PUBLIC_REPO_ALIASES.keys())
        for name in names:
            body = (root / name).read_text()
            # RFC 5545 line folding can split a URL across CRLF + space.
            body = re.sub(r"\r?\n[ \t]", "", body)
            with self.subTest(name=name):
                linked_repos = re.findall(r"https://github\.com/jaywedgeworth22/([^/\s?#)\"<>]+)", body, re.I)
                self.assertTrue(all(repo.casefold() in allowed for repo in linked_repos), f"disallowed repository link in {name}")
                self.assertFalse("### Effort board" in body, f"effort section in {name}")
                self.assertFalse("<h3>Effort board</h3>" in body, f"effort section in {name}")

    def test_private_and_unlisted_repos_never_enter_feed(self) -> None:
        def api(request: object, timeout: int) -> Reply:
            url = request.full_url  # type: ignore[attr-defined]
            if url.endswith("/BotFleet"):
                return Reply({"private": False, "visibility": "public", "owner": {"login": "jaywedgeworth22"}})
            return Reply({"private": True, "visibility": "private", "owner": {"login": "jaywedgeworth22"}})

        with patch("public_activity_repos.urllib.request.urlopen", side_effect=api):
            selected = select_public_repos("jaywedgeworth22", ["BotFleet", "fleet-ops", "Clutch", "ContactLogo"], "fixture")
        self.assertEqual(selected, ["BotFleet"])

    def test_visibility_api_failure_is_fail_closed(self) -> None:
        with patch("public_activity_repos.urllib.request.urlopen", side_effect=urllib.error.URLError("fixture")):
            with self.assertRaises(RuntimeError):
                select_public_repos("jaywedgeworth22", ["BotFleet"], "fixture")

    def test_public_pr_survives_without_effort_board_material(self) -> None:
        day = date(2026, 9, 26)
        pr = {"repo": "BotFleet", "number": 123, "title": "Fix onboarding", "url": "https://github.com/jaywedgeworth22/BotFleet/pull/123", "user": "jay"}
        buckets = digest.bucket_all([(day, pr)], [], [])
        days = list(buckets.values())
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
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


if __name__ == "__main__":
    unittest.main()
