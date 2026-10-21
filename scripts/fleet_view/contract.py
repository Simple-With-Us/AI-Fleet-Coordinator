"""The pinned JSON envelope for ``fleet-view status`` (AFC-394).

One flat shape, mirroring ``fleet_config.contract``:  ``schemaVersion`` is
pinned at 1, the three sections ride as named objects with a state each, and
the request counts ride at the top level so a test can assert the budget
without parsing prose.  The envelope never carries a secret value:  it holds
names, branches, states and timestamps only.

Verdict rule:  ``PASS`` only when every section observed its source.  Any
``UNKNOWN`` section makes the run ``UNKNOWN``, never ``PASS`` — a view that
could not look must not render as a fleet that is fine.
"""

from __future__ import annotations

from . import SCHEMA_VERSION
from .types import FleetReport

__all__ = ["build_envelope", "overall_verdict"]


def overall_verdict(report: FleetReport) -> str:
    """``PASS`` when all three sections are OK, else ``UNKNOWN``."""
    sections = (report.lanes, report.pull_requests, report.claims)
    if all(section.state == "OK" for section in sections):
        return "PASS"
    return "UNKNOWN"


def _lane_dict(item) -> dict:
    return {
        "repo": item.repo,
        "lane": item.lane,
        "seat": item.seat,
        "branch": item.branch,
        "dirtyFiles": item.dirty_files,
        "ageSeconds": item.age_seconds,
        "state": item.state,
    }


def _pr_dict(item) -> dict:
    return {
        "repo": item.repo,
        "number": item.number,
        "title": item.title,
        "branch": item.branch,
        "seat": item.seat,
        "blocker": item.blocker,
        "state": item.state,
    }


def _claim_dict(item) -> dict:
    return {
        "identifier": item.identifier,
        "title": item.title,
        "seat": item.seat,
        "ageSeconds": item.age_seconds,
        "expired": item.expired,
    }


def _section(section, render) -> dict:
    return {
        "state": section.state,
        "reason": section.reason,
        "items": [render(item) for item in section.items],
    }


def build_envelope(report: FleetReport) -> dict:
    """Render a ``FleetReport`` into the pinned envelope dict."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": "fleet-view",
        "command": "status",
        "generatedAt": report.generated_at,
        "summary": {
            "lanes": len(report.lanes.items),
            "pullRequests": len(report.pull_requests.items),
            "claims": len(report.claims.items),
            "overallVerdict": overall_verdict(report),
        },
        "requestCounts": {
            "github": report.budget.github_used,
            "linear": report.budget.linear_used,
            "githubMax": report.budget.github_max,
            "linearMax": report.budget.linear_max,
        },
        "sections": {
            "lanes": _section(report.lanes, _lane_dict),
            "pullRequests": _section(report.pull_requests, _pr_dict),
            "claims": _section(report.claims, _claim_dict),
        },
        "notes": list(report.notes),
    }
