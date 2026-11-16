#!/usr/bin/env python3
"""Regenerate the pinned visual fixture using synthetic public activity only."""

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "digest", ROOT / "scripts/build-fleet-daily-digest.py"
)
assert spec and spec.loader
digest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = digest
spec.loader.exec_module(digest)


def item(repo: str, number: int, title: str, kind: str = "pull") -> dict:
    return {
        "repo": repo,
        "number": number,
        "title": title,
        "url": f"https://github.com/Simple-With-Us/{repo}/{kind}/{number}",
        "user": "fixture",
    }


days = [
    digest.DayBucket(
        day=date(2026, 10, 28),
        merged_prs=[
            item("BotFleet", 700, "[CODEX] Fix composer paste"),
            item("Clutch", 31, "[AG] Shellular path fix"),
            item("FleetLink", 42, "[OPENCODE/ZC] Refresh public sharing"),
            item("Simple-With-Us", 43, "[ECHO/FX] Update product catalog"),
        ],
        issues_opened=[
            item("AI-Fleet-Coordinator", 600, "[WISE/JET] Refresh digest pipeline", "issues"),
        ],
    ),
    digest.DayBucket(
        day=date(2026, 10, 27),
        issues_closed=[
            item("Socratic-Trade", 3900, "Tighten public links", "issues"),
        ],
    ),
]
rendered = digest.build_html(
    days, datetime(2026, 10, 28, 18, tzinfo=UTC), ZoneInfo("America/Chicago"), "https://example.com"
)
rendered = rendered.replace("<head>\n", '<head>\n  <base href="/" />\n', 1)
(ROOT / "visual-tests/tests/fixtures/digest-2day.html").write_text(rendered)
