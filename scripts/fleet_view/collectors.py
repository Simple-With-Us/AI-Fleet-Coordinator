"""The three collectors:  lanes, pull requests and Linear claims.

Each collector takes a ``runner`` so a test can drive it with fake output and
no network, no real home and no real ``gh`` or ``simple-board``.  The runner is the
only place a child process starts, it carries a timeout, and it refuses any
executable outside the allowlist before a process exists.

Failure rule (fail closed, mirroring fleet_config section 4):  a source that
errors, times out or returns unparseable output yields an ``UNKNOWN``
section with a failing reason, never an empty ``OK`` one.

Lanes are read on the machine that runs the collector, and every lane row names
that machine.  Pull requests and claims come from GitHub and Linear, so they are
the same whichever machine reads them.
"""

from __future__ import annotations

import json
import pathlib
import platform
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .types import (
    ClaimRecord,
    LaneRecord,
    PullRequestRecord,
    RequestBudget,
    Section,
)

__all__ = [
    "ALLOWED_EXECUTABLES",
    "MAX_PARALLEL",
    "SEVERITY",
    "Runner",
    "SeatIndex",
    "blocker_for",
    "collect_claims",
    "collect_lanes",
    "collect_pull_requests",
    "host_name",
    "make_runner",
    "parse_iso",
    "plural",
    "seat_for_branch",
    "seat_for_lane",
    "severity_for",
    "this_host",
]

# The only executables a collector may start.  A command outside this list is
# refused before any process exists.
ALLOWED_EXECUTABLES = frozenset({"git", "gh", "simple-board", "board"})

# How many git or gh calls run at once.  Bounded, so a large lanes folder never
# starts hundreds of processes, and more than one, so one slow repo or lane does
# not hold every other read behind it.
MAX_PARALLEL = 8

# A page of pull requests.  A full page may be a truncated one.
PR_PAGE = 50

# runner(argv) -> (exit_code, stdout).  It raises TimeoutError on a timeout.
Runner = Callable[[list], tuple]

# What each blocker asks of people.  "action" needs someone to do something,
# "waiting" is a normal step on the way to a merge, "ready" can merge, and
# "unknown" was not proved either way.  Only "action" is a problem.
SEVERITY = {
    "ready": "ready",
    "draft": "waiting",
    "changes requested": "action",
    "failing checks": "action",
    "checks need a rerun": "action",
    "checks pending": "waiting",
    "conflicting with main": "action",
    "behind main": "waiting",
    "unknown": "unknown",
    "waiting for review": "waiting",
    "blocked (see PR)": "waiting",
    "auto-merge not armed": "waiting",
}

_FAILED_CONCLUSIONS = ("FAILURE", "STARTUP_FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
_FAILED_STATES = ("FAILURE", "ERROR")
_PENDING_STATES = ("PENDING", "EXPECTED")
_PENDING_STATUSES = ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED")

_GH_FIELDS = ",".join(
    [
        "number",
        "title",
        "url",
        "isDraft",
        "headRefName",
        "mergeable",
        "mergeStateStatus",
        "reviewDecision",
        "autoMergeRequest",
        "statusCheckRollup",
    ]
)


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


def this_host() -> str:
    """``mac`` on macOS and ``server`` anywhere else.  Computed, never written in."""
    return "mac" if platform.system() == "Darwin" else "server"


def host_name(host: str) -> str:
    """The machine inside a sentence:  ``the Mac`` or ``the server``."""
    return "the Mac" if host == "mac" else "the server"


def plural(count: int, singular: str, many: str = "") -> str:
    """``1 lane``, ``2 lanes``:  the count and the word that agrees with it."""
    word = singular if count == 1 else (many or singular + "s")
    return "%d %s" % (count, word)


def parse_iso(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp (``Z`` or offset) into an aware datetime."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(value) -> str:
    """A string field, or "" when the source sent anything else."""
    return value if isinstance(value, str) else ""


def _parallel_map(func, items: list) -> list:
    """``func`` over ``items`` on a bounded pool, with the results in input order."""
    if len(items) <= 1:
        return [func(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(items))) as pool:
        return list(pool.map(func, items))


class SeatIndex:
    """Registry seat lookup by lane suffix and by branch prefix.

    Built once from ``fleet-apps.json`` rows (tag, worktreeSuffix,
    branchPrefixes, retired, aliasOf).  Retired seats never match, so a retired
    lane reads ``UNKNOWN`` instead of resurrecting a retired seat.  A prefix that
    two live seats share names both (``ECHO or WISE``), because the branch alone
    cannot tell them apart.
    """

    def __init__(self, seats: Sequence[dict]) -> None:
        self._by_suffix: list = []  # (suffix, tag), longest suffix first
        prefixes: dict = {}  # prefix -> [tag, ...] in registry order
        self.aliases: dict = {}  # retired tag -> the live tag it now means
        for row in seats:
            tag = row.get("tag", "")
            if row.get("retired"):
                if tag and row.get("aliasOf"):
                    self.aliases[tag] = row.get("aliasOf")
                continue
            suffix = row.get("worktreeSuffix", "")
            if tag and suffix:
                self._by_suffix.append((suffix, tag))
            for prefix in row.get("branchPrefixes", []) or []:
                if tag and prefix and tag not in prefixes.setdefault(prefix, []):
                    prefixes[prefix].append(tag)
        self._by_suffix.sort(key=lambda pair: -len(pair[0]))
        self._by_prefix = sorted(prefixes.items(), key=lambda pair: -len(pair[0]))

    def seat_for_lane_name(self, lane_name: str) -> str:
        """The seat that owns a lane directory name, or ``UNKNOWN``."""
        for suffix, tag in self._by_suffix:
            if lane_name == suffix or lane_name.startswith(suffix + "-"):
                return tag
        return "UNKNOWN"

    def seat_for_branch_name(self, branch: str) -> str:
        """The seat that owns a branch name, or ``UNKNOWN``."""
        for prefix, tags in self._by_prefix:
            if branch.startswith(prefix):
                return " or ".join(tags)
        return "UNKNOWN"

    def seat_for_lane(self, lane_name: str, branch: str = "") -> str:
        """The lane's seat:  its folder name first, then the branch it is on.

        A review checkout (``review-pr-<n>``) belongs to no seat and says so.  A lane
        whose folder uses a short seat name (``ag-``, ``mm-``) still finds its seat
        from its branch.
        """
        seat = self.seat_for_lane_name(lane_name)
        if seat != "UNKNOWN":
            return seat
        if lane_name.startswith("review-pr-"):
            return "REVIEW"
        if branch and branch != "HEAD":
            return self.seat_for_branch_name(branch)
        return "UNKNOWN"

    def canonical(self, tag: str) -> str:
        """A retired alias as the live seat it now means;  any other tag as it is."""
        return self.aliases.get(tag, tag)


def seat_for_lane(index: SeatIndex, lane_name: str) -> str:
    """Module-level spelling of the lane lookup, for tests and the CLI."""
    return index.seat_for_lane_name(lane_name)


def seat_for_branch(index: SeatIndex, branch: str) -> str:
    """Module-level spelling of the branch lookup, for tests and the CLI."""
    return index.seat_for_branch_name(branch)


def _dirs(path: pathlib.Path) -> list:
    return sorted(p for p in path.iterdir() if p.is_dir())


def _is_checkout(path: pathlib.Path) -> bool:
    """Whether a folder is a git checkout.  One that cannot be checked is kept, so git decides."""
    try:
        return (path / ".git").exists()
    except OSError:
        return True


def collect_lanes(
    lanes_root: pathlib.Path,
    index: SeatIndex,
    runner: Runner,
    now: datetime,
    host: str = "",
) -> Section:
    """Scan ``<lanes_root>/<Repo>/<lane>`` and ``_codex/<slug>/<Repo>`` and ask git about each.

    Read only.  A lane whose git calls fail keeps its record with state
    ``UNKNOWN``, and the reason counts it;  a lanes root that cannot be listed
    fails the whole section.  A tree that cannot be listed is named and fails the
    section closed, so an unreadable tree never reads as an empty one.  Hidden
    folders (``.git``, ``.codex``) hold tool state and are not lanes;  a folder
    with no git checkout in it is skipped and counted;  any other ``_`` tree is
    named in the reason as not scanned.
    """
    try:
        top = _dirs(lanes_root)
    except OSError as exc:
        return Section(state="UNKNOWN", reason="lanes root unreadable: %s" % exc)
    candidates: list = []  # (repo, lane name, folder, layout)
    failures: list = []
    not_scanned: list = []
    for repo_dir in top:
        name = repo_dir.name
        if name.startswith("."):
            continue
        if name == "_codex":
            # Codex desktop files its lanes as _codex/<slug>/<Repo>.
            try:
                for slug_dir in _dirs(repo_dir):
                    for checkout in _dirs(slug_dir):
                        lane = (checkout.name, slug_dir.name, checkout, "codex-desktop")
                        candidates.append(lane)
            except OSError as exc:
                failures.append("lanes unreadable for %s: %s" % (name, exc))
            continue
        if name.startswith("_"):
            not_scanned.append(name)
            continue
        try:
            lane_dirs = _dirs(repo_dir)
        except OSError as exc:
            failures.append("lanes unreadable for %s: %s" % (name, exc))
            continue
        for lane_dir in lane_dirs:
            candidates.append((name, lane_dir.name, lane_dir, "seat"))
    checkouts = [c for c in candidates if _is_checkout(c[2])]
    skipped = len(candidates) - len(checkouts)

    def read(candidate):
        repo, lane_name, folder, layout = candidate
        return _lane_record(repo, lane_name, folder, layout, index, runner, now, host)

    items = _parallel_map(read, checkouts)
    unreadable = len([item for item in items if item.state != "OK"])
    parts = [plural(len(items), "lane")]
    if unreadable:
        parts.append("%d could not be read" % unreadable)
    if skipped:
        parts.append(plural(skipped, "folder") + " with no git checkout skipped")
    if not_scanned:
        parts.append("not scanned: %s" % ", ".join(not_scanned))
    parts.extend(failures)
    return Section(
        state="UNKNOWN" if failures else "OK",
        reason="; ".join(parts),
        items=tuple(items),
    )


def _parse_status(out: str) -> tuple:
    """``git status --porcelain=v2 --branch`` as (branch, commit, changed entries)."""
    branch = ""
    commit = ""
    changed = 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head ") :].strip()
        elif line.startswith("# branch.oid "):
            commit = line[len("# branch.oid ") :].strip()
        elif line.strip() and not line.startswith("#"):
            changed += 1
    return branch, commit, changed


def _lane_record(repo, lane_name, lane_dir, layout, index, runner, now, host) -> LaneRecord:
    path = str(lane_dir)
    try:
        age = max(0, int(now.timestamp() - lane_dir.stat().st_mtime))
    except OSError:
        age = 0
    try:
        # --no-optional-locks:  a read-only scan of hundreds of live lanes
        # must never contend for an index lock with the agent working there.
        code, out = runner(
            ["git", "--no-optional-locks", "-C", path, "status", "--porcelain=v2", "--branch"]
        )
        code2, out2 = runner(["git", "-C", path, "log", "-1", "--format=%ct"])
    except (TimeoutError, RuntimeError, ValueError):
        seat = index.seat_for_lane(lane_name)
        return LaneRecord(
            repo, lane_name, seat, "", -1, age, "UNKNOWN", host=host, path=path, layout=layout
        )
    branch, commit, dirty = _parse_status(out) if code == 0 else ("", "", -1)
    detached = branch == "(detached)"
    if detached:
        branch = "HEAD"
    head = commit[:7] if commit and commit != "(initial)" else ""
    last_commit = -1
    if code2 == 0:
        try:
            last_commit = max(0, int(now.timestamp()) - int(out2.strip()))
        except ValueError:
            last_commit = -1
    seat = index.seat_for_lane(lane_name, branch)
    state = "OK" if branch and dirty >= 0 else "UNKNOWN"
    return LaneRecord(
        repo,
        lane_name,
        seat,
        branch,
        dirty,
        age,
        state,
        host=host,
        detached=detached,
        head=head,
        last_commit_seconds=last_commit,
        path=path,
        layout=layout,
    )


def blocker_for(pr: dict) -> str:
    """Name the merge blocker from ``gh`` fields alone.  Never guesses.

    Order matters:  the first proved blocker wins.  A draft is a draft first,
    because its author has said it is not ready.  A PR whose mergeability
    GitHub has not computed reads ``unknown``, not ``ready``.  A rule that
    blocks the merge (a required review, say) is named before auto-merge,
    because arming auto-merge would not move it.
    """
    state_status = pr.get("mergeStateStatus") or ""
    if pr.get("isDraft") or state_status == "DRAFT":
        return "draft"
    if (pr.get("reviewDecision") or "") == "CHANGES_REQUESTED":
        return "changes requested"
    checks = pr.get("statusCheckRollup") or []
    for check in checks:
        # CheckRun entries carry a conclusion;  StatusContext entries carry
        # a state instead.  Both can fail, and neither may read as ready.
        if (check.get("conclusion") or "") in _FAILED_CONCLUSIONS:
            return "failing checks"
        if (check.get("state") or "") in _FAILED_STATES:
            return "failing checks"
    for check in checks:
        if (check.get("conclusion") or "") == "STALE":
            return "checks need a rerun"
    for check in checks:
        if (check.get("state") or "") in _PENDING_STATES:
            return "checks pending"
        if (check.get("status") or "") in _PENDING_STATUSES:
            return "checks pending"
    mergeable = pr.get("mergeable") or "UNKNOWN"
    if mergeable == "CONFLICTING" or state_status == "DIRTY":
        return "conflicting with main"
    if state_status == "BEHIND":
        return "behind main"
    if mergeable == "UNKNOWN" or state_status == "UNKNOWN":
        return "unknown"
    if (pr.get("reviewDecision") or "") == "REVIEW_REQUIRED":
        return "waiting for review"
    if state_status == "BLOCKED":
        return "blocked (see PR)"
    if not pr.get("autoMergeRequest"):
        return "auto-merge not armed"
    return "ready"


def severity_for(blocker: str) -> str:
    """What a blocker asks of people.  A blocker this table does not know is unknown."""
    return SEVERITY.get(blocker, "unknown")


def _grouped(failures: list) -> str:
    """``(phrase, repo)`` pairs as one clause per phrase:  one cause reads once."""
    order: list = []
    names: dict = {}
    for phrase, repo in failures:
        if phrase not in names:
            order.append(phrase)
            names[phrase] = []
        names[phrase].append(repo)
    clauses = []
    for phrase in order:
        repos = names[phrase]
        shown = ", ".join(repos[:3])
        if len(repos) > 3:
            shown += " and %d more" % (len(repos) - 3)
        clauses.append("%s for %s (%s)" % (phrase, plural(len(repos), "repo"), shown))
    return "; ".join(clauses)


def _read_repo(repo: str, runner: Runner, host: str) -> tuple:
    """One ``gh pr list``.  Returns (rows, problem), with problem "" on success."""
    argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(PR_PAGE),
        "--json",
        _GH_FIELDS,
    ]
    try:
        code, out = runner(argv)
    except TimeoutError:
        return [], "gh timed out"
    except RuntimeError:
        return [], "gh failed to start"
    except ValueError:
        return [], "gh was refused"
    if code == 4:
        # gh's own exit code for "authentication required".
        return [], "gh is not signed in on %s" % host_name(host)
    if code != 0:
        return [], "gh exited %d" % code
    try:
        rows = json.loads(out)
    except ValueError:
        return [], "gh output unparseable"
    if not isinstance(rows, list):
        return [], "gh output not a list"
    return rows, ""


def _number(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def collect_pull_requests(
    repos: Sequence[str],
    index: SeatIndex,
    runner: Runner,
    budget: RequestBudget,
    host: str = "",
) -> Section:
    """One ``gh pr list`` per repo, inside the GitHub request budget.

    The repos run a few at a time.  One bad repo does not hide the rest:  what
    was observed is kept, the repo is named in ``unread``, and the section stays
    ``UNKNOWN`` (fail closed).
    """
    allowed: list = []
    unread: list = []
    for repo in repos:
        if budget.spend_github():
            allowed.append(repo)
        else:
            unread.append(repo)
    over_budget = list(unread)
    results = _parallel_map(lambda repo: _read_repo(repo, runner, host), allowed)
    items: list = []
    failures: list = []
    for repo, (rows, problem) in zip(allowed, results):
        if problem:
            failures.append((problem, repo))
            unread.append(repo)
            continue
        if len(rows) >= PR_PAGE:
            # A full page may be a truncated one.  Say so (UNKNOWN) instead
            # of silently dropping the PRs past the limit.
            failures.append(("results may be truncated at %d" % PR_PAGE, repo))
            unread.append(repo)
        for row in rows:
            if not isinstance(row, dict):
                continue
            branch = _text(row.get("headRefName"))
            blocker = blocker_for(row)
            items.append(
                PullRequestRecord(
                    repo=repo,
                    number=_number(row.get("number")),
                    title=_text(row.get("title")),
                    branch=branch,
                    seat=index.seat_for_branch_name(branch),
                    blocker=blocker,
                    state="OK",
                    severity=severity_for(blocker),
                    draft=bool(row.get("isDraft")),
                    url=_text(row.get("url")),
                )
            )
    parts = [plural(len(items), "open PR")]
    if over_budget:
        missed = plural(len(over_budget), "repo was", "repos were")
        parts.append(
            "GitHub request budget spent (%d), so %s not read" % (budget.github_max, missed)
        )
    if failures:
        parts.append(_grouped(failures))
    return Section(
        state="UNKNOWN" if unread else "OK",
        reason="; ".join(parts),
        items=tuple(items),
        unread=tuple(unread),
    )


def collect_claims(
    runner: Runner,
    budget: RequestBudget,
    now: datetime,
    expiry_hours: int,
    index: SeatIndex = None,
) -> Section:
    """One ``simple-board list --json`` for every In Progress claim.

    Grouping is by the ``claimed:<SEAT>`` label, with a retired alias read as
    the seat it now means.  Expiry compares the claim's last update age with
    ``expiry_hours`` (from ``--expiry-hours`` or ``CLAIM_EXPIRY_HOURS``, else 72);
    an expired claim is flagged, never hidden.
    """
    if not budget.spend_linear():
        return Section(
            state="UNKNOWN", reason="Linear request budget spent (%d)" % budget.linear_max
        )
    try:
        code, out = runner(
            ["simple-board", "list", "--status", "in_progress", "--limit", "200", "--json"]
        )
    except TimeoutError:
        return Section(state="UNKNOWN", reason="simple-board timed out")
    except RuntimeError:
        return Section(state="UNKNOWN", reason="simple-board failed to start")
    except ValueError:
        return Section(state="UNKNOWN", reason="simple-board was refused")
    if code != 0:
        return Section(state="UNKNOWN", reason="simple-board exited %d" % code)
    try:
        rows = json.loads(out)
    except ValueError:
        return Section(state="UNKNOWN", reason="simple-board output unparseable")
    if isinstance(rows, dict) and "structured" in rows:
        return Section(
            state="UNKNOWN",
            reason="simple-board answered through the hosted route, which this view cannot read",
        )
    if not isinstance(rows, list):
        return Section(state="UNKNOWN", reason="simple-board output not a list")
    items: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        seat = "UNCLAIMED"
        for label in row.get("labels") or []:
            if isinstance(label, str) and label.startswith("claimed:"):
                seat = label.split(":", 1)[1] or "UNCLAIMED"
        if index is not None:
            seat = index.canonical(seat)
        stamp = _text(row.get("updated_at")) or _text(row.get("created_at"))
        # An unreadable stamp must not read as a fresh claim:  age -1 with
        # the expired flag set renders red (needs attention), never green.
        try:
            age = max(0, int((now - parse_iso(stamp)).total_seconds())) if stamp else -1
        except ValueError:
            age = -1
        items.append(
            ClaimRecord(
                identifier=_text(row.get("identifier")),
                title=_text(row.get("title")),
                seat=seat,
                age_seconds=age,
                expired=bool(age < 0 or (expiry_hours > 0 and age > expiry_hours * 3600)),
                url=_text(row.get("url")),
            )
        )
    if len(items) >= 200:
        return Section(
            state="UNKNOWN",
            reason="%s observed; results may be truncated at 200" % plural(len(items), "claim"),
            items=tuple(items),
        )
    return Section(state="OK", reason=plural(len(items), "claim"), items=tuple(items))
