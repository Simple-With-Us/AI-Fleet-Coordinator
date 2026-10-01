"""Repository boundary for published activity feeds.

Internal fleet registries and effort boards may include private repositories.  A
repo must be explicitly allowlisted here and currently public on GitHub before
its activity can enter the public HTML, Markdown, or calendar feeds.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PUBLIC_REPOS = (
    "Socratic-Trade",
    "Congress.Trade",
    "Usage-Monitor",
    "congress-trading-shared",
    "DealDex",
    "Personal-Site",
    "Autorotate",
    "ContactLogo",
    "AI-Fleet-Coordinator",
    "BotFleet",
    "HogHunter",
    "Clutch",
    "codecaps",
)

HISTORICAL_PUBLIC_REPO_ALIASES = {
    "socratic.trade": "Socratic-Trade",
    "harness": "Clutch",  # repo renamed 2026-09-30; generated history still links the old name
}


def select_public_repos(owner: str, requested: list[str], token: str) -> list[str]:
    """Fail closed if a requested repo is unlisted, private, or unverified."""
    allowed = {name.casefold() for name in PUBLIC_REPOS}
    selected: list[str] = []
    for repo in dict.fromkeys(requested):
        if repo.casefold() not in allowed:
            print(f"skip unlisted public activity repo: {repo}", file=sys.stderr)
            continue
        url = f"https://api.github.com/repos/{owner}/{repo}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "AI-Fleet-Coordinator-public-activity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                info = json.load(response)
        except (OSError, ValueError) as exc:
            print(f"skip unverified public activity repo: {repo} ({type(exc).__name__})", file=sys.stderr)
            continue
        if not isinstance(info, dict) or info.get("private") is not False or info.get("visibility") != "public":
            print(f"skip non-public activity repo: {repo}", file=sys.stderr)
            continue
        if (info.get("owner") or {}).get("login", "").casefold() != owner.casefold():
            print(f"skip unexpected activity owner: {repo}", file=sys.stderr)
            continue
        selected.append(repo)
    if not selected:
        raise RuntimeError("No configured repositories could be verified public; refusing to publish an empty feed")
    return selected
