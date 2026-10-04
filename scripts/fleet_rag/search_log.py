"""Search logging and retrieval telemetry for the fleet-agents RAG pipeline.

Tracks search queries, caller seats, latencies, and hit counts to make agent
retrieval behavior observable. Enables calculating search-to-contribute ratios
to identify retrieval neglect (agents that contribute lessons but rarely search
shared memory before diagnosing).

Never raises: telemetry must never break a search caller.
Never logs unscrubbed credentials: query strings pass through scrub before append.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .scrub import scrub

DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_KEEP_LINES = 5000
DAY_MS = 24 * 3600 * 1000


def default_state_dir() -> str:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, "apps", "fleet-rag", "state")


def log_path(state_dir: str | None = None) -> str:
    base = state_dir or default_state_dir()
    return os.path.join(base, "search-log.jsonl")


def _rotate_if_needed(path: str, max_bytes: int = DEFAULT_MAX_BYTES, keep_lines: int = DEFAULT_KEEP_LINES) -> None:
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
        lines: list[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > keep_lines:
            lines = lines[-keep_lines:]
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp_path, path)
    except Exception:
        pass


def log_search(
    query: str,
    seat: str | None = None,
    app: str | None = None,
    hits_count: int = 0,
    latency_ms: int = 0,
    mode: str = "unknown",
    state_dir: str | None = None,
    now_ms: int | None = None,
) -> None:
    """Append one search record to the search log. Silently swallows all errors."""
    try:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        resolved_seat = (
            seat
            or os.environ.get("AGENT_SEAT")
            or os.environ.get("AGENT_TAG")
            or "UNKNOWN"
        ).strip().upper()

        # Sanitize query with the fleet scrub
        scrubbed_query, _ = scrub(str(query or ""))
        # Truncate very long queries for logging sanity
        if len(scrubbed_query) > 500:
            scrubbed_query = scrubbed_query[:497] + "..."

        record = {
            "timestamp": now,
            "seat": resolved_seat,
            "app": (app or "fleet").lower(),
            "query": scrubbed_query,
            "hits": int(hits_count),
            "latency_ms": int(latency_ms),
            "mode": mode,
        }

        path = log_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def read_search_log(
    state_dir: str | None = None,
    days: int = 7,
    app: str | None = None,
    now_ms: int | None = None,
) -> list[dict]:
    """Read search events within the specified lookback window."""
    path = log_path(state_dir)
    if not os.path.exists(path):
        return []

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff = now - (days * DAY_MS)
    app_filter = app.lower() if app else None

    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = rec.get("timestamp")
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue
                if app_filter and rec.get("app") != app_filter:
                    continue
                records.append(rec)
    except Exception:
        return []
    return records


def search_summary(
    state_dir: str | None = None,
    days: int = 7,
    app: str | None = None,
    now_ms: int | None = None,
) -> dict:
    """Summarize search counts by seat and app over the given window."""
    records = read_search_log(state_dir=state_dir, days=days, app=app, now_ms=now_ms)
    by_seat: dict[str, int] = {}
    by_app: dict[str, int] = {}
    total_latency = 0

    for r in records:
        seat = r.get("seat") or "UNKNOWN"
        app_name = r.get("app") or "fleet"
        by_seat[seat] = by_seat.get(seat, 0) + 1
        by_app[app_name] = by_app.get(app_name, 0) + 1
        total_latency += int(r.get("latency_ms") or 0)

    avg_latency = round(total_latency / len(records), 1) if records else 0.0

    return {
        "days": days,
        "total_searches": len(records),
        "by_seat": dict(sorted(by_seat.items(), key=lambda x: -x[1])),
        "by_app": dict(sorted(by_app.items(), key=lambda x: -x[1])),
        "avg_latency_ms": avg_latency,
    }


def compute_ratios(
    contribution_seats: dict[str, int],
    search_seats: dict[str, int],
) -> list[dict]:
    """Compute search-to-contribute ratios for seats that contributed or searched.
    
    A healthy seat searches before deriving (ratio >= 3.0).
    A warning is raised when a seat contributed >= 2 lessons but has ratio < 2.0.
    """
    all_seats = sorted(set(contribution_seats.keys()) | set(search_seats.keys()))
    rows: list[dict] = []

    for seat in all_seats:
        c_count = contribution_seats.get(seat, 0)
        s_count = search_seats.get(seat, 0)
        ratio = round(s_count / c_count, 2) if c_count > 0 else None
        # Warning condition: at least 2 contributions but less than 2.0 searches per contribution
        is_warn = bool(c_count >= 2 and (ratio is None or ratio < 2.0))

        rows.append({
            "seat": seat,
            "searches": s_count,
            "contributions": c_count,
            "ratio": ratio,
            "warning": is_warn,
        })

    # Sort primarily by warning flag, then by total activity
    rows.sort(key=lambda x: (not x["warning"], -(x["searches"] + x["contributions"])))
    return rows
