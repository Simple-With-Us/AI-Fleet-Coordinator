"""Render the cron monitor audit."""

from __future__ import annotations

import json

from cron_monitor_audit.classify import Catalog, Finding, category_heading
from cron_monitor_audit.sentry import Monitor

# Prose the report writes for a person. The source file stays ASCII.
SENTENCE_GAP = "\u00a0 "

_ORDER = ("orphan", "matched_failing", "uncertain", "matched_healthy")


def build_report(org: str, monitors: list[Monitor], findings: list[Finding], catalog: Catalog) -> dict:
    rows = [_row(monitor, finding) for monitor, finding in zip(monitors, findings, strict=True)]
    rows.sort(key=lambda row: (row["slug"], row["project"]))
    grouped = {category: [row for row in rows if row["category"] == category] for category in _ORDER}
    return {
        "org": org,
        "monitor_count": len(rows),
        "orphans": grouped["orphan"],
        "matched_failing": grouped["matched_failing"],
        "uncertain": grouped["uncertain"],
        "matched_healthy": grouped["matched_healthy"],
        "code_search": catalog.code_search.state,
        "code_search_detail": catalog.code_search.detail,
        "repos_unread": [{"repo": repo, "reason": reason} for repo, reason in catalog.unreadable],
        "read_only": True,
    }


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_markdown(report: dict) -> str:
    gap = SENTENCE_GAP
    lines = [
        "# Cron Monitor Audit",
        "",
        (
            f"Listed {report['monitor_count']} Sentry cron monitors in `{report['org']}`."
            f"{gap}This report is read-only and does not disable monitors."
        ),
        "",
        (
            f"Code search is {report['code_search']}"
            + (f" ({report['code_search_detail']})." if report["code_search_detail"] else ".")
        ),
        "",
    ]
    if report["repos_unread"]:
        lines.append("Repos that could not be read:")
        lines.append("")
        for repo in report["repos_unread"]:
            lines.append(f"- `{repo['repo']}`: {repo['reason']}")
        lines.append("")
    sections = (
        ("orphans", "orphan", "No workflow or script sends a check-in. Active rows are a proposed disable."),
        (
            "matched_failing",
            "matched_failing",
            "A sender exists and the latest check-in is a real failure.",
        ),
        (
            "uncertain",
            "uncertain",
            "The scan could not decide, or a sender exists while the monitor is disabled.",
        ),
        ("matched_healthy", "matched_healthy", "A sender exists and the latest check-in is ok."),
    )
    for key, category, intro in sections:
        lines.extend([f"## {category_heading(category)}", "", intro, ""])
        lines.extend(_rows(report[key]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def summary_line(report: dict) -> str:
    return (
        f"orphans={len(report['orphans'])} "
        f"failing={len(report['matched_failing'])} "
        f"uncertain={len(report['uncertain'])} "
        f"healthy={len(report['matched_healthy'])}"
    )


def _row(monitor: Monitor, finding: Finding) -> dict:
    return {
        "slug": monitor.slug,
        "name": monitor.name,
        "project": monitor.project,
        "status": monitor.status,
        "check_status": monitor.check_status,
        "last_check_in": monitor.last_check_in,
        "muted": monitor.muted,
        "category": finding.category,
        "reason": finding.reason,
        "sender": finding.sender,
    }


def _rows(rows: list[dict]) -> list[str]:
    if not rows:
        return ["None."]
    rendered = []
    for row in rows:
        last = row["last_check_in"] or "none"
        muted = ", muted" if row["muted"] else ""
        sender = f", sender `{row['sender']}`" if row["sender"] else ""
        rendered.append(
            f"- `{row['slug']}` ({row['project'] or 'unknown project'}, "
            f"status {row['status'] or 'unknown'}, check-in {row['check_status'] or 'none'} "
            f"at {last}{muted}{sender}): {row['reason']}"
        )
    return rendered
