"""The Mac lane doctor report, summarized.  Read-only.

`lane ls --json` (the doctor) is slow, so a report under 900 seconds old is reused, judged by the report's
own `generated_at` and never by a file time.  Otherwise the doctor runs with a 20-minute cap.  The newest
good report is kept in `~/.cache/fleet-facts/lanes.json` (directory 0700, file 0600, written atomically) so
the next gatherer on this Mac can reuse it.

If the doctor could not see everything (`gh` not "ok", `lsof` not "ok", a failed or timed-out run, a report
that is not the expected shape) the source is failed and carries NO lane counts:  a count taken from a
partial look would read as "nothing wrong".

The data is the counts for every checkout plus a record for each one that needs a person's eye:  unpushed
or dirty work, never pushed, a git operation in progress, a dropped ball, or a safety class of UNKNOWN.
Free text (the doctor's reasons, a keep marker's reason) is cleaned.  A branch stays exact when plain.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import safety
from .activity import parse_time
from .runner import Runner, absent, failed, home_path, ok

REPORT_MAX_AGE_S = 900
DOCTOR_TIMEOUT_S = 20 * 60
CACHE_NAME = "lanes.json"


def lane_command() -> str | None:
    """`$HOME/apps/lane`, the installed lane tool (not the PATH:  another `lane` is not this one)."""
    path = home_path("apps", "lane")
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def cache_path() -> Path:
    return home_path(".cache", "fleet-facts", CACHE_NAME)


def _read_cache(now: datetime) -> dict | None:
    try:
        report = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if _fresh(report, now) and _problem(report) is None else None   # a partial look is never reused


def _fresh(report: Any, now: datetime) -> bool:
    """Under REPORT_MAX_AGE_S old by the report's own `generated_at` (a minute of clock skew is allowed)."""
    if not isinstance(report, dict):
        return False
    when = parse_time(report.get("generated_at"))
    if when is None:
        return False
    return -60 <= (now - when).total_seconds() < REPORT_MAX_AGE_S


def _write_cache(report: dict) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".lanes-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass  # the cache is a convenience;  a read-only home must not fail the gatherer


def _problem(report: Any) -> str | None:
    """Why this report cannot be trusted for counts, or None."""
    if not isinstance(report, dict) or not isinstance(report.get("checkouts"), list) or not isinstance(report.get("summary"), dict):
        return "the doctor report has an unexpected shape"
    if report.get("gh") != "ok":
        return "the doctor could not read GitHub (gh: %s)" % safety.clean(report.get("gh"), 20)
    if report.get("lsof") != "ok":
        return "the doctor could not run lsof (%s)" % safety.clean(report.get("lsof"), 20)
    return None


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _needs_eye(c: dict) -> bool:
    return bool((_num(c.get("unpushed")) or 0) > 0 or (_num(c.get("dirty_tracked")) or 0) > 0
                or (_num(c.get("dirty_untracked")) or 0) > 0 or c.get("never_pushed") is True
                or c.get("git_op_in_progress") or c.get("dropped_ball") not in (False, None)
                or c.get("safety") == "UNKNOWN")


def _record(c: dict) -> dict:
    ref, exact = safety.safe_ref(c.get("branch"))
    path, path_exact = safety.safe_path(c.get("path"))
    keep = c.get("keep") if isinstance(c.get("keep"), dict) else {}
    return {
        "path": path,
        "path_exact": path_exact,
        "repo": safety.clean(c.get("owner_repo"), 100) or None,
        "branch": ref,
        "branch_exact": exact,
        "location_class": safety.clean(c.get("location_class"), 30) or None,
        "layout_status": safety.clean(c.get("layout_status"), 30) or None,
        "safety": safety.clean(c.get("safety"), 20) or None,
        "reasons": [safety.clean(r, 200) for r in (c.get("safety_reasons") or [])][:8],
        "dropped_ball": c.get("dropped_ball") if c.get("dropped_ball") in (True, False, None) else None,
        "dirty_tracked": _num(c.get("dirty_tracked")),
        "dirty_untracked": _num(c.get("dirty_untracked")),
        "unpushed": _num(c.get("unpushed")),
        "never_pushed": c.get("never_pushed") if isinstance(c.get("never_pushed"), bool) else None,
        "never_pushed_age_hours": _num(c.get("never_pushed_age_hours")),
        "dirty_oldest_age_hours": _num(c.get("dirty_oldest_age_hours")),
        "last_change_at": c.get("last_change_at") if isinstance(c.get("last_change_at"), str) and parse_time(c.get("last_change_at")) else None,
        "last_change_basis": safety.clean(c.get("last_change_basis"), 30) or None,
        "git_op_in_progress": [safety.clean(x, 30) for x in (c.get("git_op_in_progress") or [])][:8],
        "pr_state": safety.clean(c.get("pr_state"), 20) or None,
        "pr_number": c.get("pr_number") if isinstance(c.get("pr_number"), int) and not isinstance(c.get("pr_number"), bool) else None,
        "pr_is_auto_draft": c.get("pr_is_auto_draft") if isinstance(c.get("pr_is_auto_draft"), bool) else None,
        "auto_draft_seat_commit": c.get("auto_draft_seat_commit") if isinstance(c.get("auto_draft_seat_commit"), bool) else None,
        "janitor_keep": c.get("janitor_keep") is True,
        "keep_reason": safety.clean(keep.get("reason"), 120) or None,
        "lane_age_days": _num(c.get("lane_age_days")),
        "idle_days": _num(c.get("idle_days")),
    }


def summarize(report: dict) -> dict:
    """The data block from a trusted doctor report.  Pure."""
    checkouts = [c for c in report["checkouts"] if isinstance(c, dict)]
    summary = report["summary"]
    attention = [_record(c) for c in checkouts if _needs_eye(c)]
    attention.sort(key=lambda r: (r["path"] or ""))
    return {
        "generated_at": report.get("generated_at"),
        "total": len(checkouts),
        "by_safety": {safety.clean(k, 20): v for k, v in (summary.get("by_safety") or {}).items() if isinstance(v, int)},
        "by_location_class": {safety.clean(k, 30): v for k, v in (summary.get("by_location_class") or {}).items() if isinstance(v, int)},
        "cleaner_candidates": len(report.get("cleaner_candidates") or []),
        "attention_count": len(attention),
        "attention": attention,
    }


def gather(run: Runner, *, now: datetime | None = None, no_mac: bool = False) -> dict:
    """The `lanes` source."""
    if no_mac:
        return absent("--no-mac")
    current = now or datetime.now(timezone.utc)
    report = _read_cache(current)
    reused = report is not None
    if report is None:
        lane = lane_command()
        if lane is None:
            return absent("the lane tool is not installed on this host")
        result = run([lane, "ls", "--json"], timeout=DOCTOR_TIMEOUT_S)
        if result.reason:
            return failed("lane ls %s" % result.reason)
        if result.rc != 0:
            return failed("lane ls exited %s" % result.rc)
        try:
            report = json.loads(result.out)
        except ValueError:
            return failed("lane ls did not print JSON")
        problem = _problem(report)
        if problem:
            return failed(problem)
        _write_cache(report)
    problem = _problem(report)
    if problem:
        return failed(problem)
    data = summarize(report)
    data["reused_report"] = reused
    return ok(data)
