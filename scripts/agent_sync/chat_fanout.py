"""The owner's `@all` or `@everyone` reaches the chats that are working now (owner 2026-10-19).

Zulip notifies every bot, but a bot is not a chat:  the chats doing the work (a Grok TUI, a Cursor cloud agent,
and so on) never see a channel-wide mention.  When the owner posts one, the listener picks the recently active
chats from the chat catalog and queues each a Zulip reminder through the existing chat-nudge queue.

Which chats, per platform:

* Only chats active in the last six hours.  Nothing older is ever picked.
* Four or fewer:  all of them.
* More than four:  the window shrinks toward now until two to four remain, cutting at the longest pause in
  activity.  Among the 2nd, 3rd and 4th most recent chats, the cut goes after the one followed by the widest gap
  to the next older chat;  a tie keeps more chats.

Which repo (owner 2026-10-19):  `@all for ST: message` or `ST @all message` (or `@everyone for Socratic Trade:`, `@all for ST-123:`,
`@all for CT, UM:`, a PR link or `Repo#12`) reaches ALL of those repos' chats active in the last six hours, with no
cap.  `@all for SWU-32:` (or `SWU-32 @all`) reaches the chats working on that issue:  its id is in the chat's
title, folder or git branch.  When no chat matches, it reaches the issue's repo instead.  So does an `@all` posted in
a work topic (`ST-123 subject`).  Any other `@all` reaches every platform's recent
chats, picked as above.  The body is never scanned for app names, so a passing mention cannot widen an alert.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_AGE_SECONDS = 6 * 3600
MIN_PICK = 2
MAX_PICK = 4
GIT_BUDGET_SECONDS = 10.0  # every git query in one alert, together
CLOCK_SKEW_SECONDS = 300.0  # a chat stamped a little in the future counts as just now

# Linear team key to the repos it covers (the same map as scripts/gh_linear_pairing.py, AFC-339).
TEAM_REPOS: dict[str, tuple[str, ...]] = {
    "AFC": ("AI-Fleet-Coordinator", "Kodus-Config", "homebrew-tap"),
    "AR": ("Autorotate",),
    "BF": ("BotFleet",),
    "C": ("Clutch",),
    "CC": ("CodeCaps",),
    "CT": ("Congress.Trade",),
    "CL": ("ContactLogo",),
    "CTS": ("congress-trading-shared",),
    "DD": ("DealDex",),
    "FL": ("FleetLink",),
    "HH": ("HogHunter",),
    "OPS": ("Fleet-OPS",),
    "PS": ("Personal-Site",),
    "ST": ("Socratic-Trade",),
    "SWU": ("Simple-With-Us",),
    "UM": ("Usage-Monitor",),
}
_SEP = r"[\s._-]?"
_REMOTE_RE = re.compile(r"[/:]([^/:]+?)(?:\.git)?/*$")
_ISSUE_RE = re.compile(r"(?<![\w-])([A-Z]{1,4})-\d+\b")
_PR_URL_RE = re.compile(r"github\.com/[^/\s]+/([^/\s#?]+)/(?:pull|issues)/\d+", re.IGNORECASE)
_PR_REF_RE = re.compile(r"(?<![\w/])([\w.-]+)#\d+\b")


def _words(repo: str) -> list[str]:
    """`Socratic-Trade` to ['socratic', 'trade'];  `BotFleet` to ['bot', 'fleet']."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", repo)
    return [w.casefold() for w in re.split(r"[\s._-]+", spaced) if w]


_REPO_TEAM = {repo.casefold(): team for team, repos in TEAM_REPOS.items() for repo in repos}
_NAME_RES = [
    (
        repo.casefold(),
        re.compile(
            r"(?<![\w-])" + _SEP.join(map(re.escape, _words(repo))) + r"(?![\w-])", re.IGNORECASE
        ),
    )
    for repos in TEAM_REPOS.values()
    for repo in repos
]
# A one-letter acronym (C for Clutch) is too common a word to count on its own;  an issue id (C-12) still does.
_ACRONYM_RE = re.compile(
    r"(?<![\w-])("
    + "|".join(sorted((t for t in TEAM_REPOS if len(t) > 1), key=len, reverse=True))
    + r")(?![\w-])"
)


_SCOPE_RE = re.compile(
    r"(?<![\w])@\*{0,2}(?:all|everyone|channel|stream)\*{0,2}\s+for\s+([^\n]{1,120}?):(?=\s|$)",
    re.IGNORECASE,
)
_PREFIX_RE = re.compile(
    r"(?m)^\s*([^\n@]{1,200}?)\s*@\*{0,2}(?:all|everyone|channel|stream)\*{0,2}(?![\w*])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Scope:
    """Who an alert is for.  No repos:  everyone (each platform's recent chats).  Issues:  the chats on those
    issues, else their repos."""

    repos: frozenset[str] | None = None
    issues: frozenset[str] = field(default_factory=frozenset)


def scope(visible: str, topic: str) -> Scope:
    """The scope of an owner's channel-wide mention:  the explicit `@all for X:` names, or `X @all` (the names just
    before the mention, starting the line), else the work topic's repo, else everyone."""
    for regex in (_SCOPE_RE, _PREFIX_RE):
        match = regex.search(visible)
        named = named_repos(match.group(1)) if match else None
        if named:
            return Scope(named, named_issues(match.group(1)))
    return Scope(named_repos(topic))


def named_issues(text: str) -> frozenset[str]:
    """Linear issue ids (`SWU-32`) of known teams, upper case."""
    return frozenset(
        m.group(0).upper() for m in _ISSUE_RE.finditer(str(text or "")) if m.group(1) in TEAM_REPOS
    )


def named_repos(*texts: str) -> frozenset[str] | None:
    """The repos a text names (lower case), or None when it names none."""
    found: set[str] = set()
    for text in texts:
        text = str(text or "")
        for match in _ISSUE_RE.finditer(text):
            found.update(r.casefold() for r in TEAM_REPOS.get(match.group(1), ()))
        for match in _ACRONYM_RE.finditer(text):
            found.update(r.casefold() for r in TEAM_REPOS[match.group(1)])
        for regex in (_PR_URL_RE, _PR_REF_RE):
            for match in regex.finditer(text):
                name = match.group(1).casefold().removesuffix(".git")
                if name in _REPO_TEAM:
                    found.add(name)
        for name, regex in _NAME_RES:
            if regex.search(text):
                found.add(name)
    return frozenset(found) or None


def repo_from_remote(url: str) -> str | None:
    """`git@github.com:Owner/Repo.git` or `https://github.com/Owner/Repo` to `repo` (lower case).  The owner is
    ignored, so a repo still cloned under its old owner matches."""
    match = _REMOTE_RE.search(str(url or "").strip())
    return match.group(1).casefold() if match else None


def git_out(cwd: str, *args: str) -> str:
    """One short git query in the chat's folder, or '' (not a repo, no git, too slow)."""
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


class ChatPlaces:
    """A chat's repo and branch from its folder, asked once per alert.  A Cursor cloud chat carries its repo URL;  a
    local chat's folder is asked for its git origin and current branch."""

    def __init__(
        self, git: Callable[..., str] = git_out, budget: float = GIT_BUDGET_SECONDS
    ) -> None:
        self.git = git
        self.cache: dict[tuple[str, str], str] = {}
        self.deadline = time.monotonic() + budget

    def _ask(self, cwd: str, what: str) -> str:
        """One git query per folder and question, cached.  Once the alert's shared budget is spent, an unasked folder
        answers '' (no repo, no branch), so a slow or network-mounted folder cannot delay the reminders."""
        if (cwd, what) not in self.cache:
            if time.monotonic() >= self.deadline:
                return ""
            args = (
                ("config", "--get", "remote.origin.url")
                if what == "remote"
                else ("branch", "--show-current")
            )
            self.cache[(cwd, what)] = self.git(cwd, *args)
        return self.cache[(cwd, what)]

    def repo(self, row: Mapping[str, Any]) -> str | None:
        """A Cursor cloud chat's repo comes from the catalog's `repo` URL (its cwd is `cursor-cloud://<id>`);  a local
        chat's from its folder's git origin."""
        if isinstance(row.get("repo"), str) and row["repo"]:
            return repo_from_remote(row["repo"])
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return None
        return repo_from_remote(self._ask(cwd, "remote"))

    def on_issue(self, row: Mapping[str, Any], issues: frozenset[str]) -> bool:
        cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else ""
        branch = self._ask(cwd, "branch") if cwd.startswith("/") else ""
        text = " ".join((str(row.get("title") or ""), cwd, branch))
        return any(
            re.search(rf"(?<![A-Za-z0-9]){re.escape(issue)}(?![0-9])", text, re.IGNORECASE)
            for issue in issues
        )


def pick_recent(rows: Iterable[Mapping[str, Any]], now: float) -> list[Mapping[str, Any]]:
    """One platform's chats to alert (see the module docstring), most recent first."""
    recent = sorted(
        (r for r in rows if _recent(r, now)),
        key=lambda r: min(float(r["updated_at"]), now),
        reverse=True,
    )
    if len(recent) <= MAX_PICK:
        return recent
    ages = [max(0.0, now - float(r["updated_at"])) for r in recent]
    # ages[k] - ages[k - 1] is the pause between the k-th most recent chat and the next older one.
    keep = max(range(MIN_PICK, MAX_PICK + 1), key=lambda k: (ages[k] - ages[k - 1], k))
    return recent[:keep]


def select(
    rows: Iterable[Mapping[str, Any]], now: float, where: Scope, places: ChatPlaces | None = None
) -> list[Mapping[str, Any]]:
    """The chats to alert, across platforms (see the module docstring)."""
    places = places or ChatPlaces()
    live = [
        r
        for r in rows
        if not r.get("stale")
        and r.get("status") != "unavailable"
        and r.get("session_id")
        and _recent(r, now)
    ]
    if where.repos is None:
        by_platform: dict[str, list[Mapping[str, Any]]] = {}
        for row in live:
            by_platform.setdefault(str(row.get("platform") or ""), []).append(row)
        return [
            row
            for platform in sorted(by_platform)
            for row in pick_recent(by_platform[platform], now)
        ]
    if where.issues:
        on_issue = [r for r in live if places.on_issue(r, where.issues)]
        if on_issue:
            return on_issue
    return [r for r in live if places.repo(r) in where.repos]


def _recent(row: Mapping[str, Any], now: float) -> bool:
    updated = row.get("updated_at")
    if not isinstance(updated, (int, float)) or isinstance(updated, bool):
        return False
    return -CLOCK_SKEW_SECONDS <= now - float(updated) <= MAX_AGE_SECONDS
