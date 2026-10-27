"""The fixed vocabulary for ``fleet_view``:  records, sections and budgets.

Every record is a plain dataclass with no behaviour beyond construction.  The
envelope builder in ``contract.py`` is the only place that renders them, so
the CLI, the tests and the Swift app all read one shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ClaimRecord",
    "FleetReport",
    "LaneRecord",
    "PullRequestRecord",
    "RequestBudget",
    "Section",
    "SectionState",
]

# A section is either OK (it observed its source) or UNKNOWN (it could not).
# There is no third healthy-looking state:  UNKNOWN never renders as healthy
# in any consumer, exactly as in fleet_config.
SectionState = str  # "OK" | "UNKNOWN"


@dataclass(frozen=True)
class LaneRecord:
    """One lane checkout under ``~/apps/lanes/<Repo>/<seat>-<slug>``."""

    repo: str
    lane: str
    seat: str  # registry tag, or "UNKNOWN" when no suffix matches
    branch: str  # "" when git could not answer
    dirty_files: int  # -1 when git could not answer
    age_seconds: int
    state: str  # "OK" | "UNKNOWN"


@dataclass(frozen=True)
class PullRequestRecord:
    """One open pull request, with the merge blocker named."""

    repo: str
    number: int
    title: str
    branch: str
    seat: str  # registry tag from the branch prefix, or "UNKNOWN"
    blocker: str  # "ready" or one named blocker; "unknown" when unproved
    state: str  # "OK" | "UNKNOWN"


@dataclass(frozen=True)
class ClaimRecord:
    """One In Progress Linear claim, grouped by its ``claimed:`` seat."""

    identifier: str
    title: str
    seat: str  # the claimed:<SEAT> label, or "UNCLAIMED"
    age_seconds: int
    expired: bool


@dataclass
class Section:
    """One collector section:  a state, its items and a human reason."""

    state: str = "UNKNOWN"
    reason: str = "not run"
    items: tuple = ()


@dataclass
class RequestBudget:
    """The per-cycle request budget (AFC-394:  <60 GitHub, <30 Linear).

    Every ``gh`` call consumes one GitHub request and every ``simple-board`` call
    consumes one Linear request.  A collector that would exceed its budget
    stops and reports ``UNKNOWN`` instead of spending past the cap, and the
    counts ride in the envelope so a test can assert them.
    """

    github_max: int = 60
    linear_max: int = 30
    github_used: int = 0
    linear_used: int = 0

    def spend_github(self) -> bool:
        """Spend one GitHub request.  False when the budget is spent."""
        if self.github_used >= self.github_max:
            return False
        self.github_used += 1
        return True

    def spend_linear(self) -> bool:
        """Spend one Linear request.  False when the budget is spent."""
        if self.linear_used >= self.linear_max:
            return False
        self.linear_used += 1
        return True


@dataclass
class FleetReport:
    """The whole run:  three sections, the budget and the clock."""

    generated_at: str  # ISO 8601 UTC, Z suffix
    lanes: Section = field(default_factory=Section)
    pull_requests: Section = field(default_factory=Section)
    claims: Section = field(default_factory=Section)
    budget: RequestBudget = field(default_factory=RequestBudget)
    notes: tuple = ()
