"""Compare the inventory with monitors and render the report."""

from __future__ import annotations

import json
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from uptime_audit.inventory import Inventory
from uptime_audit.sentry import Monitor

# Prose the report writes for a person. The source file stays ASCII.
SENTENCE_GAP = "\u00a0 "


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower().rstrip(".")


def compare(inventory: Inventory, monitors: list[Monitor]) -> dict:
    by_host: dict[str, list] = defaultdict(list)
    for entry in inventory.entries:
        if entry.host:
            by_host[entry.host].append(entry)
    excluded = {item.host: item.reason for item in inventory.excluded}
    covered: set[str] = set()
    unexpected = []
    excluded_hits = []
    disabled = []
    mismatches = []
    unspecified = []

    for monitor in monitors:
        record = _monitor(monitor)
        if not monitor.covering:
            disabled.append(record)
        host = host_of(monitor.url)
        if host in excluded:
            excluded_hits.append({**record, "reason": excluded[host]})
            continue
        entries = by_host.get(host, [])
        if not entries:
            unexpected.append(record)
            continue
        target = normalize_url(monitor.url)
        matched = [
            entry
            for entry in entries
            if entry.health_url and normalize_url(entry.health_url) == target
        ]
        if matched:
            if monitor.covering:
                covered.update(entry.id for entry in matched)
            continue
        known = [entry.health_url for entry in entries if entry.health_url]
        if known:
            mismatches.append({**record, "expected_urls": known})
        else:
            unspecified.append(record)

    uncovered = []
    uncovered_uncertain = []
    unresolved = []
    ok = []
    for entry in inventory.entries:
        row = _entry(entry)
        if not entry.health_url:
            unresolved.append(row)
        elif entry.id in covered:
            ok.append(row)
        elif entry.certainty == "certain":
            uncovered.append(row)
        else:
            uncovered_uncertain.append(row)

    return {
        "org": inventory.org,
        "expected_count": len(inventory.entries),
        "monitor_count": len(monitors),
        "uncovered": uncovered,
        "uncovered_uncertain": uncovered_uncertain,
        "unexpected_hosts": unexpected,
        "excluded_hosts": excluded_hits,
        "disabled_or_muted": disabled,
        "health_url_mismatches": mismatches,
        "health_path_unspecified": unspecified,
        "unresolved": unresolved,
        "covered": ok,
    }


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_markdown(report: dict) -> str:
    gap = SENTENCE_GAP
    lines = [
        "# Uptime Coverage Audit",
        "",
        (
            f"Compared {report['expected_count']} reviewed entries with "
            f"{report['monitor_count']} Sentry uptime monitors in `{report['org']}`."
            f"{gap}This report is read-only."
        ),
        "",
        "## Uncovered Hosts",
        "",
        "Certain expected health URLs with no active monitor on that URL.",
        "",
    ]
    lines.extend(_entries(report["uncovered"]))
    lines.extend(
        [
            "",
            "## Uncovered Uncertain",
            "",
            "These rows are not established firmly enough to fail the uncovered-host check.",
            "",
        ]
    )
    lines.extend(_entries(report["uncovered_uncertain"]))
    lines.extend(
        [
            "",
            "## Monitors On Other Hosts",
            "",
            "Not in the reviewed list and not excluded. These may be retired.",
            "",
        ]
    )
    lines.extend(_monitors(report["unexpected_hosts"]))
    lines.extend(["", "## Monitors On Excluded Hosts", ""])
    if report["excluded_hosts"]:
        for row in report["excluded_hosts"]:
            lines.append(f"- `{row['url']}` ({row['name'] or row['id']}): {row['reason']}")
    else:
        lines.append("None.")
    lines.extend(["", "## Disabled Or Muted", ""])
    lines.extend(_monitors(report["disabled_or_muted"]))
    lines.extend(["", "## Health URL Mismatches", ""])
    if report["health_url_mismatches"]:
        for row in report["health_url_mismatches"]:
            expected = ", ".join(f"`{url}`" for url in row["expected_urls"])
            lines.append(f"- `{row['url']}` does not match {expected}.")
    else:
        lines.append("None.")
    lines.extend(["", "## Health Path Not Established", ""])
    lines.extend(_monitors(report["health_path_unspecified"]))
    lines.extend(["", "## Unresolved Entries", ""])
    lines.extend(_entries(report["unresolved"]))
    lines.extend(["", "## Covered", ""])
    lines.extend(_entries(report["covered"]))
    lines.append("")
    return "\n".join(lines)


def summary_line(report: dict) -> str:
    return (
        f"uncovered={len(report['uncovered'])} "
        f"uncertain={len(report['uncovered_uncertain'])} "
        f"unexpected={len(report['unexpected_hosts'])} "
        f"excluded={len(report['excluded_hosts'])} "
        f"disabled={len(report['disabled_or_muted'])} "
        f"mismatches={len(report['health_url_mismatches'])}"
    )


def _entry(entry) -> dict:
    return {
        "id": entry.id,
        "owner": entry.owner,
        "host": entry.host,
        "health_url": entry.health_url,
        "certainty": entry.certainty,
        "source": entry.source,
        "note": entry.note,
    }


def _monitor(monitor: Monitor) -> dict:
    return {
        "id": monitor.id,
        "name": monitor.name,
        "url": monitor.url,
        "host": host_of(monitor.url),
        "status": monitor.status,
        "project": monitor.project,
        "covering": monitor.covering,
    }


def _entries(rows: list[dict]) -> list[str]:
    if not rows:
        return ["None."]
    return [
        f"- **{row['owner']}** `{row['health_url'] or row['host'] or row['id']}` ({row['certainty']})"
        for row in rows
    ]


def _monitors(rows: list[dict]) -> list[str]:
    if not rows:
        return ["None."]
    return [
        f"- `{row['url']}` ({row['name'] or row['id']}, status {row['status'] or 'unknown'})"
        for row in rows
    ]
