"""The three collectors:  lanes, pull requests and Linear claims.

Each collector takes a ``runner`` so a test can drive it with fake output and
no network, no real home and no real ``gh`` or ``board``.  The runner is the
only place a child process starts, it carries a timeout, and it refuses any
executable outside the allowlist before a process exists.

Failure rule (fail closed, mirroring fleet_config section 4):  a source that
errors, times out or returns unparseable output yields an ``UNKNOWN``
section with a failing reason, never an empty ``OK`` one.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from .types import (
    ClaimRecord,
    LaneRecord,
    PullRequestRecord,
    RequestBudget,
    Section,
)

__all__ = [
    "ALLOWED_EXECUTABLES",
    "Runner",
    "SeatIndex",
    "blocker_for",
    "collect_claims",
    "collect_lanes",
    "collect_pull_requests",
    "make_runner",
    "parse_iso",
    "seat_for_branch",
    "seat_for_lane",
]

# The only executables a collector may start.  A command outside this list is
# refused before any process exists.
ALLOWED_EXECUTABLES = frozenset({"git", "gh", "board"})

# runner(argv) -> (exit_code, stdout).  It raises TimeoutError on a timeout.
Runner = Callable[[list], tuple]


def make_runner(timeout_seconds: int = 20) -> Runner:
    """The real runner:  one allowlisted subprocess per call, with a timeout."""

    def run(argv: list) -> tuple:
        if not argv or pathlib.Path(argv[0]).name not in ALLOWED_EXECUTABLES:
            raise ValueError("refused executable: %r" % (argv[0] if argv else ""))
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(str(exc)) from exc
        except OSError as exc:
            raise RuntimeError(str(exc)) from exc
        return proc.returncode, proc.stdout

    return run


def parse_iso(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp (``Z`` or offset) into an aware datetime."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class SeatIndex:
    """Registry seat lookup by lane suffix and by branch prefix.

    Built once from ``fleet-apps.json`` rows (tag, worktreeSuffix,
    branchPrefixes, retired).  Retired seats never match, so a retired lane
    reads ``UNKNOWN`` instead of resurrecting a retired seat.
    """

    def __init__(self, seats: Sequence[dict]) -> None:
        self._by_suffix: list = []  # (suffix, tag), longest suffix first
        self._by_prefix: list = []  # (prefix, tag), longest prefix first
        for row in seats:
            if row.get("retired"):
                continue
            tag = row.get("tag", "")
            suffix = row.get("worktreeSuffix", "")
            if tag and suffix:
                self._by_suffix.append((suffix, tag))
            for prefix in row.get("branchPrefixes", []) or []:
                if tag and prefix:
                    self._by_prefix.append((prefix, tag))
        self._by_suffix.sort(key=lambda pair: -len(pair[0]))
        self._by_prefix.sort(key=lambda pair: -len(pair[0]))

    def seat_for_lane_name(self, lane_name: str) -> str:
        """The seat that owns a lane directory name, or ``UNKNOWN``."""
        for suffix, tag in self._by_suffix:
            if lane_name == suffix or lane_name.startswith(suffix + "-"):
                return tag
        return "UNKNOWN"

    def seat_for_branch_name(self, branch: str) -> str:
        """The seat that owns a branch name, or ``UNKNOWN``."""
        for prefix, tag in self._by_prefix:
            if branch.startswith(prefix):
                return tag
        return "UNKNOWN"


def seat_for_lane(index: SeatIndex, lane_name: str) -> str:
    """Module-level spelling of the lane lookup, for tests and the CLI."""
    return index.seat_for_lane_name(lane_name)


def seat_for_branch(index: SeatIndex, branch: str) -> str:
    """Module-level spelling of the branch lookup, for tests and the CLI."""
    return index.seat_for_branch_name(branch)


def collect_lanes(
    lanes_root: pathlib.Path,
    index: SeatIndex,
    runner: Runner,
    now: datetime,
) -> Section:
    """Scan ``<lanes_root>/<Repo>/<lane>`` and ask git about each lane.

    Read only.  A lane whose git calls fail keeps its record with state
    ``UNKNOWN``;  a lanes root that cannot be listed fails the whole section.
    A tree that cannot be listed is named and fails the section closed, so an
    unreadable tree never reads as an empty one.
    """
    try:
        repo_dirs = sorted(p for p in lanes_root.iterdir() if p.is_dir())
    except OSError as exc:
        return Section(state="UNKNOWN", reason="lanes root unreadable: %s" % exc)
    items: list = []
    failures: list = []
    for repo_dir in repo_dirs:
        if repo_dir.name.startswith("_"):
            continue  # tool-owned trees (_codex) are not seat lanes
        try:
            lane_dirs = sorted(p for p in repo_dir.iterdir() if p.is_dir())
        except OSError as exc:
            failures.append("lanes unreadable for %s: %s" % (repo_dir.name, exc))
            continue
        for lane_dir in lane_dirs:
            items.append(_lane_record(repo_dir.name, lane_dir, index, runner, now))
    if failures:
        return Section(
            state="UNKNOWN",
            reason="%d lane(s); %s" % (len(items), "; ".join(failures)),
            items=tuple(items),
        )
    return Section(state="OK", reason="%d lane(s)" % len(items), items=tuple(items))


def _lane_record(repo, lane_dir, index, runner, now) -> LaneRecord:
    seat = index.seat_for_lane_name(lane_dir.name)
    try:
        age = max(0, int(now.timestamp() - lane_dir.stat().st_mtime))
    except OSError:
        age = 0
    try:
        code, out = runner(["git", "-C", str(lane_dir), "rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.strip() if code == 0 else ""
        # --no-optional-locks:  a read-only scan of hundreds of live lanes
        # must never contend for an index lock with the agent working there.
        code2, out2 = runner(
            ["git", "--no-optional-locks", "-C", str(lane_dir), "status", "--porcelain"]
        )
        dirty = len([ln for ln in out2.splitlines() if ln.strip()]) if code2 == 0 else -1
    except (TimeoutError, RuntimeError, ValueError):
        return LaneRecord(repo, lane_dir.name, seat, "", -1, age, "UNKNOWN")
    state = "OK" if branch and dirty >= 0 else "UNKNOWN"
    return LaneRecord(repo, lane_dir.name, seat, branch, dirty, age, state)


def blocker_for(pr: dict) -> str:
    """Name the merge blocker from ``gh`` fields alone.  Never guesses.

    Order matters:  the first proved blocker wins.  A PR whose mergeability
    GitHub has not computed reads ``unknown``, not ``ready``.
    """
    if (pr.get("reviewDecision") or "") == "CHANGES_REQUESTED":
        return "changes requested"
    checks = pr.get("statusCheckRollup") or []
    for check in checks:
        # CheckRun entries carry a conclusion;  StatusContext entries carry
        # a state instead.  Both can fail, and neither may read as ready.
        if (check.get("conclusion") or "") in (
            "FAILURE",
            "STARTUP_FAILURE",
            "TIMED_OUT",
            "CANCELLED",
        ):
            return "failing checks"
        if (check.get("state") or "") in ("FAILURE", "ERROR"):
            return "failing checks"
    for check in checks:
        if (check.get("state") or "") in ("PENDING", "EXPECTED"):
            return "checks pending"
        if (check.get("status") or "") in ("IN_PROGRESS", "QUEUED", "PENDING"):
            return "checks pending"
    mergeable = pr.get("mergeable") or "UNKNOWN"
    state_status = pr.get("mergeStateStatus") or ""
    if mergeable == "CONFLICTING" or state_status == "DIRTY":
        return "conflicting with main"
    if state_status == "BEHIND":
        return "behind main"
    if mergeable == "UNKNOWN" or state_status == "UNKNOWN":
        return "unknown"
    if not pr.get("autoMergeRequest"):
        return "auto-merge not armed"
    if state_status == "BLOCKED":
        return "blocked (see PR)"
    return "ready"


def collect_pull_requests(
    repos: Sequence[str],
    index: SeatIndex,
    runner: Runner,
    budget: RequestBudget,
) -> Section:
    """One ``gh pr list`` per repo, inside the GitHub request budget."""
    items: list = []
    failures: list = []
    for repo in repos:
        if not budget.spend_github():
            return Section(
                state="UNKNOWN",
                reason="GitHub request budget spent (%d)" % budget.github_max,
                items=tuple(items),
            )
        argv = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,headRefName,mergeable,mergeStateStatus,reviewDecision,autoMergeRequest,statusCheckRollup",
        ]
        # One bad repo must not hide the rest:  keep what was observed, name
        # the failure, and let the section verdict stay UNKNOWN (fail closed).
        try:
            code, out = runner(argv)
        except (TimeoutError, RuntimeError, ValueError) as exc:
            failures.append("gh failed for %s: %s" % (repo, exc))
            continue
        if code != 0:
            failures.append("gh exited %d for %s" % (code, repo))
            continue
        try:
            rows = json.loads(out)
        except ValueError:
            failures.append("gh output unparseable for %s" % repo)
            continue
        if not isinstance(rows, list):
            failures.append("gh output not a list for %s" % repo)
            continue
        if len(rows) >= 50:
            # A full page may be a truncated one.  Say so (UNKNOWN) instead
            # of silently dropping the PRs past the limit.
            failures.append("results may be truncated at 50 for %s" % repo)
        for row in rows:
            if not isinstance(row, dict):
                continue
            branch = row.get("headRefName") or ""
            items.append(
                PullRequestRecord(
                    repo=repo,
                    number=int(row.get("number") or 0),
                    title=row.get("title") or "",
                    branch=branch,
                    seat=index.seat_for_branch_name(branch),
                    blocker=blocker_for(row),
                    state="OK",
                )
            )
    if failures:
        return Section(
            state="UNKNOWN",
            reason="%d open PR(s) observed; %s" % (len(items), "; ".join(failures)),
            items=tuple(items),
        )
    return Section(state="OK", reason="%d open PR(s)" % len(items), items=tuple(items))


def collect_claims(
    runner: Runner,
    budget: RequestBudget,
    now: datetime,
    expiry_hours: int,
) -> Section:
    """One ``board list --json`` for every In Progress claim.

    Grouping is by the ``claimed:<SEAT>`` label.  Expiry compares the claim's
    last update age with ``expiry_hours`` (the CLAIM_EXPIRY ceiling the caller
    resolved from the fleet knobs);  an expired claim is flagged, never hidden.
    """
    if not budget.spend_linear():
        return Section(
            state="UNKNOWN", reason="Linear request budget spent (%d)" % budget.linear_max
        )
    try:
        code, out = runner(["board", "list", "--status", "in_progress", "--limit", "200", "--json"])
    except (TimeoutError, RuntimeError, ValueError) as exc:
        return Section(state="UNKNOWN", reason="board failed: %s" % exc)
    if code != 0:
        return Section(state="UNKNOWN", reason="board exited %d" % code)
    try:
        rows = json.loads(out)
    except ValueError:
        return Section(state="UNKNOWN", reason="board output unparseable")
    if not isinstance(rows, list):
        return Section(state="UNKNOWN", reason="board output not a list")
    items: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        seat = "UNCLAIMED"
        for label in row.get("labels") or []:
            if isinstance(label, str) and label.startswith("claimed:"):
                seat = label.split(":", 1)[1] or "UNCLAIMED"
        stamp = row.get("updated_at") or row.get("created_at") or ""
        # An unreadable stamp must not read as a fresh claim:  age -1 with
        # the expired flag set renders red (needs attention), never green.
        try:
            age = max(0, int((now - parse_iso(stamp)).total_seconds())) if stamp else -1
        except ValueError:
            age = -1
        items.append(
            ClaimRecord(
                identifier=row.get("identifier") or "",
                title=row.get("title") or "",
                seat=seat,
                age_seconds=age,
                expired=bool(age < 0 or (expiry_hours > 0 and age > expiry_hours * 3600)),
            )
        )
    if len(items) >= 200:
        return Section(
            state="UNKNOWN",
            reason="%d claim(s) observed; results may be truncated at 200" % len(items),
            items=tuple(items),
        )
    return Section(state="OK", reason="%d claim(s)" % len(items), items=tuple(items))
