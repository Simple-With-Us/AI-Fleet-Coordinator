#!/usr/bin/env python3
"""Build a public ICS feed of recent fleet-repo commits for calendar subscription.

stdlib only (no third-party deps). Reads GitHub commits for configured repos
and writes calendar/agent-activity.ics.

Auth:
  GITHUB_TOKEN          — default Actions token (current repo + public repos)
  FLEET_GITHUB_TOKEN    — optional token for GitHub API rate limits

Env:
  FLEET_OWNER           — GitHub owner (default: jaywedgeworth22)
  FLEET_REPOS           — comma list of repo names (default fleet set)
  CALENDAR_LOOKBACK_DAYS — how far back to fetch (default: 14)
  CALENDAR_PER_REPO     — max commits per repo (default: 40)
  CALENDAR_OUT          — output path (default: calendar/agent-activity.ics)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ics_utils import fold_line, ics_escape, join_ics  # noqa: E402
from public_activity_repos import PUBLIC_REPOS, select_public_repos  # noqa: E402

DEFAULT_REPOS = PUBLIC_REPOS


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def first_line(msg: str) -> str:
    return msg.split("\n", 1)[0].strip()[:140]


def gh_get(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AI-Fleet-Coordinator-agent-calendar",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_commits(
    owner: str,
    repo: str,
    token: str,
    since: datetime,
    per_page: int,
) -> list[dict[str, Any]]:
    q = urllib.parse.urlencode(
        {
            "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "per_page": str(per_page),
        }
    )
    url = f"https://api.github.com/repos/{owner}/{repo}/commits?{q}"
    try:
        data = gh_get(url, token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"warn: {owner}/{repo} commits HTTP {e.code}: {body}", file=sys.stderr)
        return []
    except Exception as e:  # noqa: BLE001
        print(f"warn: {owner}/{repo} commits failed: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        return []
    return data


def event_duration_minutes(message: str) -> int:
    """Heuristic block length from commit subject prefix."""
    m = message.lower()
    if m.startswith("docs") or m.startswith("chore"):
        return 20
    if m.startswith("fix(ci)") or m.startswith("ci:"):
        return 25
    if m.startswith("feat"):
        return 45
    if m.startswith("fix"):
        return 35
    return 30


def build_ics(events: list[dict[str, Any]], now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Jay Wedgeworth//AI Fleet Agent Activity//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:AI Fleet — Agent Coding Activity",
        "X-WR-CALDESC:Merged commits across fleet repos. Auto-refreshed by GitHub Actions in AI-Fleet-Coordinator.",
        "X-WR-TIMEZONE:America/Chicago",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for ev in events:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{ev['uid']}",
                f"DTSTAMP:{fmt_utc(now)}",
                f"DTSTART:{fmt_utc(ev['start'])}",
                f"DTEND:{fmt_utc(ev['end'])}",
                f"SUMMARY:{ics_escape(ev['summary'])}",
                f"DESCRIPTION:{ics_escape(ev['description'])}",
                f"URL:{ev['url']}",
                f"CATEGORIES:{ics_escape(ev['repo'])},agent-activity",
                "STATUS:CONFIRMED",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return join_ics(lines)


def main() -> int:
    owner = os.environ.get("FLEET_OWNER", "jaywedgeworth22").strip() or "jaywedgeworth22"
    repos_raw = os.environ.get("FLEET_REPOS", "").strip()
    requested = [r.strip() for r in repos_raw.split(",") if r.strip()] or list(DEFAULT_REPOS)
    lookback = env_int("CALENDAR_LOOKBACK_DAYS", 14)
    per_repo = env_int("CALENDAR_PER_REPO", 40)
    out = Path(os.environ.get("CALENDAR_OUT", "calendar/agent-activity.ics"))

    token = (
        os.environ.get("FLEET_GITHUB_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
    )
    if not token:
        print("error: set GITHUB_TOKEN or FLEET_GITHUB_TOKEN", file=sys.stderr)
        return 2
    repos = select_public_repos(owner, requested, token)

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback)

    events: list[dict[str, Any]] = []
    for repo in repos:
        commits = list_commits(owner, repo, token, since, per_repo)
        print(f"{repo}: {len(commits)} commits since {since.date()}")
        for c in commits:
            sha = c.get("sha", "")[:8]
            full_sha = c.get("sha", "")
            commit = c.get("commit") or {}
            msg = (commit.get("message") or "(no message)").strip()
            author_date = ((commit.get("author") or {}).get("date")) or (
                (commit.get("committer") or {}).get("date")
            )
            if not author_date:
                continue
            start = datetime.fromisoformat(author_date.replace("Z", "+00:00"))
            mins = event_duration_minutes(msg)
            end = start + timedelta(minutes=mins)
            url = f"https://github.com/{owner}/{repo}/commit/{full_sha}"
            subject = first_line(msg)
            events.append(
                {
                    "uid": f"{full_sha or sha}-{repo}@AI-Fleet-Coordinator",
                    "start": start,
                    "end": end,
                    "summary": f"[{repo}] {subject}",
                    "description": (
                        f"Repo: {repo}\n"
                        f"Commit: {sha}\n"
                        f"{subject}\n"
                        f"{url}"
                    ),
                    "url": url,
                    "repo": repo,
                }
            )

    # Stable sort newest first in file is fine; calendars index by UID.
    events.sort(key=lambda e: e["start"], reverse=True)

    ics = build_ics(events, now)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ics, encoding="utf-8")
    print(f"wrote {out} ({len(events)} events, {len(ics)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
