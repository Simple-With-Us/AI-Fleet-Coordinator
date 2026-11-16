#!/usr/bin/env python3
"""Read-only GitHub Actions spend math.

Minutes prefer the run timing API. When that payload is all zeros, minutes
come from job start and end timestamps and the runner labels. Nothing here
performs a workflow run or a write.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

WEIGHTS = {"linux": 1, "windows": 2, "macos": 10}
MINUTE_KEYS = ("linux", "macos", "windows", "self_hosted", "other")
GAP = "\u00a0 "
MAC_SIGNAL = re.compile(
    r"\b(xcodebuild|xcrun|xcodegen|simctl|plistbuddy|codesign|notarize|swift|sparkle|brew|macos|mac|bsd)\b",
    re.IGNORECASE,
)
_TIMING_OS = {"UBUNTU": "linux", "MACOS": "macos", "WINDOWS": "windows"}
_DOW = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def prose(sentences):
    """Join sentences with the house gap. Each item is one sentence."""
    clean = [sentence.strip() for sentence in sentences if sentence and sentence.strip()]
    return GAP.join(clean)


def empty_minutes():
    return {key: 0 for key in MINUTE_KEYS}


def parse_time(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_time(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rounded_ms(duration_ms):
    if duration_ms <= 0:
        return 0
    return math.ceil(duration_ms / 60000)


def job_minutes(started, completed):
    start = parse_time(started)
    end = parse_time(completed)
    if start is None or end is None:
        return 0
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return 0
    return math.ceil(seconds / 60)


def weighted(minutes):
    return (
        minutes.get("linux", 0) * WEIGHTS["linux"]
        + minutes.get("windows", 0) * WEIGHTS["windows"]
        + minutes.get("macos", 0) * WEIGHTS["macos"]
    )


def _cron_atom(text, names):
    key = text.strip().lower()
    if names and key in names:
        return names[key]
    return int(key)


def _field_values(field, low, high, names=None):
    values = set()
    for part in field.split(","):
        piece = part.strip()
        step = 1
        if "/" in piece:
            piece, step_text = piece.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError(step_text)
        if piece in ("*", "?"):
            start, end = low, high
        elif "-" in piece:
            left, right = piece.split("-", 1)
            start = _cron_atom(left, names)
            end = _cron_atom(right, names)
        else:
            start = end = _cron_atom(piece, names)
        if start > end or step <= 0:
            raise ValueError(field)
        values.update(range(start, end + 1, step))
    return {value for value in values if low <= value <= high}


def cron_hits_per_week(expr, origin=None):
    """How many times a five-field UTC cron fires in seven days.

    GitHub follows the standard rule that a restricted day-of-month and a
    restricted day-of-week match when either one matches. Returns None when
    the expression is not five fields or a field does not parse.
    """
    fields = expr.split()
    if len(fields) != 5:
        return None
    try:
        minutes = _field_values(fields[0], 0, 59)
        hours = _field_values(fields[1], 0, 23)
        doms = _field_values(fields[2], 1, 31)
        months = _field_values(fields[3], 1, 12, _MONTHS)
        dows = _field_values(fields[4], 0, 7, _DOW)
    except (ValueError, TypeError):
        return None
    if 7 in dows:
        dows.add(0)
        dows.discard(7)
    dom_star = fields[2] in ("*", "?")
    dow_star = fields[4] in ("*", "?")
    start = origin or datetime(2026, 11, 9, tzinfo=UTC)
    hits = 0
    for offset in range(7 * 24 * 60):
        moment = start + timedelta(minutes=offset)
        if moment.minute not in minutes or moment.hour not in hours:
            continue
        if moment.month not in months:
            continue
        dom_ok = moment.day in doms
        cron_dow = (moment.weekday() + 1) % 7
        dow_ok = cron_dow in dows
        if dom_star and dow_star:
            day_ok = True
        elif dom_star:
            day_ok = dow_ok
        elif dow_star:
            day_ok = dom_ok
        else:
            day_ok = dom_ok or dow_ok
        if day_ok:
            hits += 1
    return hits


def os_from_labels(labels):
    text = " ".join(str(label) for label in labels).lower()
    if "self-hosted" in text:
        return "self_hosted"
    if "macos" in text or re.search(r"\bmac-", text):
        return "macos"
    if "windows" in text:
        return "windows"
    if "ubuntu" in text or re.search(r"\blinux\b", text):
        return "linux"
    if "${{" in text:
        return "dynamic"
    return "other"


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def minutes_from_timing(timing):
    """Return minute rows when the timing API reports a positive duration.

    An all-zero billable payload is treated as unusable so the caller can
    fall back to job timestamps. GitHub has returned that shape for runs
    whose jobs had already finished.
    """
    if not isinstance(timing, dict):
        return None
    billable = timing.get("billable")
    if not isinstance(billable, dict) or not billable:
        return None
    totals = empty_minutes()
    any_positive = False
    for key, bucket in billable.items():
        # A malformed bucket makes the whole payload unusable, so the caller
        # measures this run from job timestamps instead.  Skipping only the bad
        # bucket would silently understate that OS's minutes.
        if not isinstance(bucket, dict):
            return None
        os_name = _TIMING_OS.get(str(key).upper(), "other")
        job_runs = bucket.get("job_runs")
        if job_runs is not None and not isinstance(job_runs, list):
            return None
        durations = []
        for job in job_runs or []:
            duration = job.get("duration_ms") if isinstance(job, dict) else None
            if not _non_negative_int(duration):
                return None
            durations.append(duration)
        positive = [duration for duration in durations if duration > 0]
        if positive:
            any_positive = True
            minutes = sum(rounded_ms(duration) for duration in positive)
        else:
            total_ms = bucket.get("total_ms", 0)
            if total_ms is None:
                total_ms = 0
            if not _non_negative_int(total_ms):
                return None
            if total_ms > 0:
                any_positive = True
            minutes = rounded_ms(total_ms)
        totals[os_name] = totals.get(os_name, 0) + minutes
    if not any_positive:
        return None
    return {"minutes": totals, "jobs": []}


def minutes_from_jobs(jobs):
    totals = empty_minutes()
    rows = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        os_name = os_from_labels(job.get("labels") or [])
        if os_name not in totals:
            os_name = "other"
        minutes = job_minutes(job.get("started_at"), job.get("completed_at"))
        totals[os_name] += minutes
        name = str(job.get("name") or "")
        rows.append({"name": name, "os": os_name, "minutes": minutes})
    return {"minutes": totals, "jobs": rows}


def choose_minutes(timing, jobs):
    parsed = minutes_from_timing(timing)
    if parsed is not None:
        return parsed, "timing"
    if jobs is None:
        return {"minutes": empty_minutes(), "jobs": []}, "none"
    return minutes_from_jobs(jobs), "jobs"


def wall_seconds(run):
    start = parse_time(run.get("run_started_at") or run.get("created_at"))
    end = parse_time(run.get("updated_at"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def needs_job_lookup(run):
    """True when a completed run may have billed and the list payload cannot say."""
    if run.get("status") != "completed":
        return False
    conclusion = run.get("conclusion")
    if conclusion in ("skipped", "neutral"):
        return False
    if conclusion == "cancelled":
        wall = wall_seconds(run)
        if wall is None:
            return True
        return wall >= 60
    return True


def slim_run(run):
    return {
        "id": run.get("id"),
        "name": run.get("name") or "",
        "path": run.get("path") or "",
        "event": run.get("event") or "",
        "status": run.get("status") or "",
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at") or "",
        "updated_at": run.get("updated_at") or "",
        "run_started_at": run.get("run_started_at") or "",
        "head_sha": run.get("head_sha") or "",
    }


def iter_slices(start, end, hours):
    if not hours > 0:
        raise ValueError("slice hours must be greater than 0")
    cursor = start
    step = timedelta(hours=hours)
    while cursor < end:
        nxt = min(end, cursor + step)
        yield cursor, nxt
        cursor = nxt


def list_range(fetch, start, end, depth=0):
    """Fetch one window, splitting only when the page cap is hit."""
    batch, capped = fetch(start, end)
    if not capped or depth >= 8 or (end - start) <= timedelta(minutes=15):
        return batch, bool(capped)
    mid = start + (end - start) / 2
    left, left_capped = list_range(fetch, start, mid, depth + 1)
    right, right_capped = list_range(fetch, mid, end, depth + 1)
    return left + right, left_capped or right_capped


def collect_runs(fetch, start, end, slice_hours=2, progress=None):
    found = {}
    truncated = False
    slices = list(iter_slices(start, end, slice_hours))
    for index, (slice_start, slice_end) in enumerate(slices, 1):
        batch, capped = list_range(fetch, slice_start, slice_end)
        if capped:
            truncated = True
        for run in batch:
            created = parse_time(run.get("created_at"))
            if created is None or created < start or created >= end:
                continue
            run_id = run.get("id")
            if run_id is None:
                continue
            found[run_id] = slim_run(run)
        if progress is not None and (index % 6 == 0 or index == len(slices)):
            progress(index, len(slices), len(found), truncated)
    runs = sorted(found.values(), key=lambda item: item["created_at"])
    return runs, truncated


def _trigger_block(doc):
    if not isinstance(doc, dict):
        return {}
    raw = doc["on"] if "on" in doc else doc.get(True)
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def _push_main_only(push):
    if not isinstance(push, dict):
        return False
    branches = push.get("branches")
    if isinstance(branches, str):
        branches = [branches]
    if not isinstance(branches, list) or not branches:
        return False
    return {str(branch) for branch in branches} <= {"main", "master"}


def _crons(schedule):
    crons = []
    items = schedule if isinstance(schedule, list) else [schedule]
    for item in items:
        if isinstance(item, dict) and item.get("cron"):
            crons.append(str(item["cron"]).strip())
    return crons


def _intentional_false(raw_text):
    lines = raw_text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"cancel-in-progress:\s*false\b", line):
            continue
        window = "\n".join(lines[max(0, index - 3) : index + 1])
        if re.search(r"\bnever\b", window, re.IGNORECASE):
            return True
    return False


def _cancel_mode(doc, raw_text):
    concurrency = doc.get("concurrency") if isinstance(doc, dict) else None
    if concurrency is None or isinstance(concurrency, str):
        return "missing"
    if not isinstance(concurrency, dict) or "cancel-in-progress" not in concurrency:
        return "missing"
    value = concurrency.get("cancel-in-progress")
    # `== 1` / `== 0` so a YAML 1 / 0 reads like true / false (True == 1).
    if value == 1 and not isinstance(value, str):
        return "true"
    if value == 0 and not isinstance(value, str):
        if _intentional_false(raw_text):
            return "intentional-false"
        return "false"
    if isinstance(value, str) and "${{" in value:
        return "conditional"
    return "false"


def _labels(runs_on):
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [str(item) for item in runs_on]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels") or []
        if isinstance(labels, str):
            return [labels]
        return [str(item) for item in labels]
    return []


def _matrix_labels(job):
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    found = []
    if not isinstance(matrix, dict):
        return found
    for key in ("os", "runner", "runs-on"):
        values = matrix.get(key)
        if isinstance(values, list):
            found.extend(str(item) for item in values)
    include = matrix.get("include")
    if isinstance(include, list):
        for row in include:
            if not isinstance(row, dict):
                continue
            for key in ("os", "runner", "runs-on"):
                if key in row:
                    found.append(str(row[key]))
    return found


def _step_text(job):
    parts = [str(job.get("name") or "")]
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        parts.append(str(step.get("name") or ""))
        parts.append(str(step.get("run") or ""))
        if step.get("uses"):
            parts.append(str(step["uses"]))
    return "\n".join(parts)


def _job_spec(key, job):
    base = os_from_labels(_labels(job.get("runs-on")))
    if base == "dynamic":
        systems = []
        for label in _matrix_labels(job):
            system = os_from_labels([label])
            if system not in ("dynamic", "other"):
                systems.append(system)
        if not systems:
            systems = ["dynamic"]
    else:
        systems = [base]
    display = str(job.get("name") or key)
    names = tuple(dict.fromkeys([display, str(key)]))
    return {
        "names": names,
        "operating_systems": tuple(dict.fromkeys(systems)),
        "mac_signal": bool(MAC_SIGNAL.search(_step_text(job))),
    }


def _local_callee(uses, docs):
    """Resolve only same-checkout reusable workflows (`./.github/workflows/x.yml`).

    `owner/repo/.github/workflows/x.yml@ref` may be another repository or a
    pinned revision whose jobs differ from the local file of the same name, so
    it is treated as dynamic rather than borrowing unrelated runner data.
    """
    match = re.fullmatch(r"\./\.github/workflows/([^@\s/]+\.ya?ml)", str(uses).strip())
    if not match:
        return None
    return docs.get(match.group(1))


def _job_specs(doc, docs, depth=0):
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    specs = []
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if uses and depth < 2:
            callee = _local_callee(uses, docs)
            if isinstance(callee, dict):
                specs.extend(_job_specs(callee, docs, depth + 1))
                continue
        specs.append(_job_spec(key, job))
    return specs


def _paths_limited(triggers):
    relevant = []
    for key in ("push", "pull_request", "pull_request_target"):
        if key in triggers:
            relevant.append(triggers[key])
    if not relevant:
        return True
    for value in relevant:
        if not isinstance(value, dict):
            return False
        paths = value.get("paths")
        if not isinstance(paths, list) or not paths:
            return False
    return True


def _profile(filename, doc, raw_text, docs):
    triggers = _trigger_block(doc)
    crons = _crons(triggers.get("schedule"))
    per_week = 0
    for cron in crons:
        hits = cron_hits_per_week(cron)
        if hits:
            per_week += hits
    push = triggers.get("push") if "push" in triggers else None
    return {
        "path": ".github/workflows/" + filename,
        "name": str(doc.get("name") or filename) if isinstance(doc, dict) else filename,
        "crons": crons,
        "cron_per_week": per_week,
        "has_push": "push" in triggers,
        "has_pull_request": "pull_request" in triggers or "pull_request_target" in triggers,
        "push_main_only": _push_main_only(push) if "push" in triggers else False,
        "paths_limited": _paths_limited(triggers),
        "cancel": _cancel_mode(doc if isinstance(doc, dict) else {}, raw_text),
        "jobs": _job_specs(doc if isinstance(doc, dict) else {}, docs),
    }


def load_profiles(directory):
    root = Path(directory)
    files = sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml"))
    docs = {}
    raws = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        raws[path.name] = text
        loaded = yaml.safe_load(text)
        docs[path.name] = loaded if isinstance(loaded, dict) else {}
    profiles = {}
    for name, doc in docs.items():
        profile = _profile(name, doc, raws[name], docs)
        profiles[profile["path"]] = profile
    return profiles


def _add_minutes(dst, src):
    for key in MINUTE_KEYS:
        dst[key] += src.get(key, 0)


def _event_bucket():
    return {"runs": 0, "weighted": 0, "minutes": empty_minutes()}


def assemble(runs, measured, profiles, meta):
    """Fold runs and measurements into workflow totals and proposed cuts.

    `measured` maps a run id to a minutes result. A completed run that needs a
    lookup and is absent from the map is unmeasured, and the totals are then
    a lower bound.
    """
    grouped = {}
    unmeasured = 0
    open_runs = 0
    timing_runs = 0
    job_runs = 0
    for run in runs:
        path = run.get("path") or "(unknown)"
        stats = grouped.get(path)
        if stats is None:
            profile = profiles.get(path) or {}
            stats = {
                "path": path,
                "name": run.get("name") or profile.get("name") or path,
                "runs": 0,
                "open_runs": 0,
                "minutes": empty_minutes(),
                "by_event": {},
                "by_job": {},
                "rows": [],
                "measured": 0,
                "unmeasured": 0,
            }
            grouped[path] = stats
        stats["runs"] += 1
        if run.get("status") != "completed":
            stats["open_runs"] += 1
            open_runs += 1
            continue
        if needs_job_lookup(run):
            result = measured.get(run.get("id"))
            if result is None:
                stats["unmeasured"] += 1
                unmeasured += 1
                continue
            source = result.get("source")
            if source == "timing":
                timing_runs += 1
            elif source == "jobs":
                job_runs += 1
            minutes = result.get("minutes") or empty_minutes()
        else:
            minutes = empty_minutes()
            result = {"jobs": []}
        stats["measured"] += 1
        _add_minutes(stats["minutes"], minutes)
        event = run.get("event") or ""
        bucket = stats["by_event"].get(event)
        if bucket is None:
            bucket = _event_bucket()
            stats["by_event"][event] = bucket
        bucket["runs"] += 1
        _add_minutes(bucket["minutes"], minutes)
        bucket["weighted"] += weighted(minutes)
        for job in result.get("jobs") or []:
            name = job.get("name") or ""
            if not name:
                continue
            slot = stats["by_job"].setdefault(name, empty_minutes())
            os_name = job.get("os") or "other"
            if os_name not in slot:
                os_name = "other"
            slot[os_name] += int(job.get("minutes") or 0)
        start = parse_time(run.get("run_started_at") or run.get("created_at"))
        end = parse_time(run.get("updated_at"))
        if start is not None and end is not None:
            stats["rows"].append(
                {
                    "sha": run.get("head_sha") or "",
                    "event": event,
                    "start": start,
                    "end": end,
                    "weighted": weighted(minutes),
                    "minutes": dict(minutes),
                }
            )

    workflows = []
    for stats in grouped.values():
        minutes = stats["minutes"]
        workflows.append(
            {
                "path": stats["path"],
                "name": stats["name"],
                "runs": stats["runs"],
                "open_runs": stats["open_runs"],
                "linux_min": minutes["linux"],
                "macos_min": minutes["macos"],
                "windows_min": minutes["windows"],
                "self_hosted_min": minutes["self_hosted"],
                "other_min": minutes["other"],
                "weighted": weighted(minutes),
                "measured": stats["measured"],
                "unmeasured": stats["unmeasured"],
            }
        )
    workflows.sort(key=lambda item: (-item["weighted"], -item["macos_min"], item["path"]))
    totals = {
        "runs": sum(item["runs"] for item in workflows),
        "open_runs": open_runs,
        "linux_min": sum(item["linux_min"] for item in workflows),
        "macos_min": sum(item["macos_min"] for item in workflows),
        "windows_min": sum(item["windows_min"] for item in workflows),
        "self_hosted_min": sum(item["self_hosted_min"] for item in workflows),
        "other_min": sum(item["other_min"] for item in workflows),
        "weighted": sum(item["weighted"] for item in workflows),
        "unmeasured_runs": unmeasured,
        "timing_runs": timing_runs,
        "job_runs": job_runs,
    }
    cuts = propose_cuts(grouped, profiles)
    notes = _notes(meta, totals)
    return {
        "repo": meta.get("repo", ""),
        "start": meta.get("start", ""),
        "end": meta.get("end", ""),
        "partial": bool(meta.get("truncated") or unmeasured),
        "truncated_list": bool(meta.get("truncated")),
        "method": meta.get("method", ""),
        "totals": totals,
        "workflows": workflows,
        "cuts": cuts,
        "notes": notes,
    }


def _notes(meta, totals):
    notes = []
    if meta.get("method") == "jobs":
        notes.append(
            "The run timing API returned 0 ms for the runs that were checked, so these minutes are the job start and end timestamps rounded up per job."
        )
    elif meta.get("method") == "timing":
        notes.append("Minutes come from the run timing API, rounded up per job.")
    else:
        notes.append(
            "Minutes come from the run timing API when it reports time, and otherwise from job timestamps."
        )
    notes.append(
        "Weighted minutes price Linux at 1, Windows at 2, and macOS at 10, the standard GitHub-hosted runner multipliers."
    )
    if totals["unmeasured_runs"]:
        notes.append(
            f"{totals['unmeasured_runs']} completed runs were not measured, so these totals are a lower bound."
        )
    if meta.get("truncated"):
        notes.append(
            "The runs list hit the API page cap inside a slice, so some runs in that slice are missing."
        )
    return notes


def _overlap_savings(rows):
    by_sha = defaultdict(list)
    for row in rows:
        if row["sha"]:
            by_sha[row["sha"]].append(row)
    saved = 0.0
    for group in by_sha.values():
        if len(group) < 2:
            continue
        for earlier in group:
            duration = (earlier["end"] - earlier["start"]).total_seconds()
            if duration <= 0 or earlier["weighted"] <= 0:
                continue
            best = 0.0
            for later in group:
                if later["start"] <= earlier["start"] or later["start"] >= earlier["end"]:
                    continue
                tail = (earlier["end"] - later["start"]).total_seconds()
                fraction = min(1.0, max(0.0, tail / duration))
                best = max(best, earlier["weighted"] * fraction)
            saved += best
    return saved


def _portable_macos_minutes(profile, by_job, workflow_macos):
    portable = []
    required = []
    for job in profile.get("jobs") or []:
        if "macos" not in job["operating_systems"]:
            continue
        if job["mac_signal"]:
            required.append(job)
        else:
            portable.append(job)
    matched = set()
    portable_minutes = 0
    portable_names = []
    for job in portable:
        for name in job["names"]:
            slot = by_job.get(name)
            if slot is None or name in matched:
                continue
            portable_minutes += slot.get("macos", 0)
            matched.add(name)
            portable_names.append(name)
    if portable_minutes == 0 and portable and not required and workflow_macos:
        portable_minutes = workflow_macos
        portable_names = [job["names"][0] for job in portable]
    required_minutes = 0
    required_names = []
    for job in required:
        hit = False
        for name in job["names"]:
            slot = by_job.get(name)
            if slot is None or name in matched:
                continue
            required_minutes += slot.get("macos", 0)
            matched.add(name)
            required_names.append(name)
            hit = True
        if not hit:
            required_names.append(job["names"][0])
    if required_minutes == 0 and required and not portable and workflow_macos:
        required_minutes = workflow_macos
    if not by_job and portable and required and workflow_macos:
        # Timing-API runs carry no per-job names, so split the workflow's macOS
        # total by macOS job count rather than dropping both proposals.
        share = workflow_macos / (len(portable) + len(required))
        portable_minutes = share * len(portable)
        portable_names = [job["names"][0] for job in portable]
        required_minutes = share * len(required)
        required_names = [job["names"][0] for job in required]
    return portable_minutes, portable_names, required_minutes, required_names


def propose_cuts(grouped, profiles):
    cuts = []
    for path, stats in grouped.items():
        profile = profiles.get(path) or {}
        minutes = stats["minutes"]
        schedule_weighted = stats["by_event"].get("schedule", {}).get("weighted", 0)
        per_week = profile.get("cron_per_week") or 0
        if per_week > 7 and schedule_weighted > 0:
            savings = schedule_weighted * (per_week - 7) / per_week
            crons = ", ".join(profile.get("crons") or [])
            cuts.append(
                {
                    "kind": "schedule",
                    "confidence": "measured",
                    "path": path,
                    "savings_weighted": savings,
                    "detail": (
                        f"{path} cron {crons} fires {per_week} times a week and its schedule runs used "
                        f"{schedule_weighted} weighted minutes, so a once-a-day cron would drop about "
                        f"{round(savings)} of those weighted minutes."
                    ),
                }
            )
        duplicate = _duplicate_savings(stats["rows"])
        if duplicate > 0:
            if (
                profile.get("has_push")
                and profile.get("has_pull_request")
                and not profile.get("push_main_only")
            ):
                fix = "Restrict push to main, or drop one of the two triggers."
            else:
                fix = "Drop the overlapping trigger or put both events in one concurrency group."
            cuts.append(
                {
                    "kind": "duplicate",
                    "confidence": "measured",
                    "path": path,
                    "savings_weighted": duplicate,
                    "detail": prose(
                        [
                            (
                                f"{path} ran a push and a pull request on the same commit, and the cheaper side "
                                f"sums to about {round(duplicate)} weighted minutes."
                            ),
                            fix,
                        ]
                    ),
                }
            )
        portable_minutes, portable_names, required_minutes, required_names = (
            _portable_macos_minutes(profile, stats["by_job"], minutes["macos"])
        )
        if portable_minutes > 0:
            savings = portable_minutes * 9
            names = ", ".join(portable_names)
            cuts.append(
                {
                    "kind": "macos-linux",
                    "confidence": "measured",
                    "path": path,
                    "savings_weighted": savings,
                    "detail": (
                        f"Job {names} in {path} runs on macOS and its steps do not mention macOS-only tools, "
                        f"so moving it to ubuntu-latest would drop the 10× premium, about {round(savings)} weighted minutes."
                    ),
                }
            )
        if required_minutes > 0 and not profile.get("paths_limited", True):
            savings = required_minutes * WEIGHTS["macos"]
            names = ", ".join(dict.fromkeys(required_names))
            cuts.append(
                {
                    "kind": "macos-paths",
                    "confidence": "upper-bound",
                    "path": path,
                    "savings_weighted": savings,
                    "detail": (
                        f"Jobs {names} in {path} stay on macOS because their steps call macOS-only tools, and "
                        f"the upper bound of a paths filter that skipped every current run is {round(savings)} weighted minutes."
                    ),
                }
            )
        cancel = profile.get("cancel")
        if cancel in ("missing", "false"):
            overlap = _overlap_savings(stats["rows"])
            if overlap >= 1:
                state = (
                    "has no concurrency cancel-in-progress"
                    if cancel == "missing"
                    else "sets cancel-in-progress to false"
                )
                cuts.append(
                    {
                        "kind": "cancel",
                        "confidence": "measured",
                        "path": path,
                        "savings_weighted": overlap,
                        "detail": (
                            f"{path} {state}, and overlapping runs on the same commit spent about "
                            f"{round(overlap)} weighted minutes after a newer run had already started."
                        ),
                    }
                )
    cuts = [cut for cut in cuts if cut["savings_weighted"] >= 1]
    cuts.sort(key=lambda cut: (-cut["savings_weighted"], cut["path"], cut["kind"]))
    for index, cut in enumerate(cuts, 1):
        cut["rank"] = index
        cut["savings_weighted"] = round(cut["savings_weighted"])
    return cuts


def _duplicate_savings(rows):
    by_sha = defaultdict(lambda: {"push": 0, "pr": 0})
    for row in rows:
        if not row.get("sha"):
            continue
        if row["event"] == "push":
            by_sha[row["sha"]]["push"] += row["weighted"]
        elif row["event"] in ("pull_request", "pull_request_target"):
            by_sha[row["sha"]]["pr"] += row["weighted"]
    saved = 0
    for sides in by_sha.values():
        if sides["push"] and sides["pr"]:
            saved += min(sides["push"], sides["pr"])
    return saved


def _table(workflows, include_self_hosted, include_other):
    headers = ["Rank", "Workflow", "Runs", "Linux min", "macOS min", "Windows min"]
    if include_self_hosted:
        headers.append("Self-hosted min")
    if include_other:
        headers.append("Other min")
    headers.append("Weighted min")
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for rank, item in enumerate(workflows, 1):
        name = (item["name"] or item["path"]).replace("|", "/")
        cells = [
            str(rank),
            f"{name} `{item['path']}`",
            str(item["runs"]),
            str(item["linux_min"]),
            str(item["macos_min"]),
            str(item["windows_min"]),
        ]
        if include_self_hosted:
            cells.append(str(item["self_hosted_min"]))
        if include_other:
            cells.append(str(item["other_min"]))
        cells.append(str(item["weighted"]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_markdown(report, limit=10):
    totals = report["totals"]
    workflows = report["workflows"]
    include_self_hosted = totals["self_hosted_min"] > 0
    include_other = totals["other_min"] > 0
    lines = [
        "# Actions Spend Report",
        "",
        prose(
            [
                f"This report covers {report['repo']} from {report['start']} to {report['end']}.",
                f"There were {totals['runs']} runs and {totals['weighted']} weighted minutes.",
            ]
        ),
        "",
        prose(report["notes"]),
        "",
        "## Top 10 Workflows",
        "",
        _table(workflows[:limit], include_self_hosted, include_other),
        "",
        "## Proposed Cuts",
        "",
    ]
    if not report["cuts"]:
        lines.append("No proposed cut cleared 1 weighted minute in this window.")
    else:
        for cut in report["cuts"][:15]:
            lead = (
                f"{cut['rank']}. **{cut['kind']}** ({cut['confidence']}) — "
                f"about {cut['savings_weighted']} weighted minutes."
            )
            lines.append(prose([lead, cut["detail"]]))
    lines.extend(
        ["", "## Every Workflow", "", _table(workflows, include_self_hosted, include_other), ""]
    )
    return "\n".join(lines) + "\n"


def redact(text, token):
    if token and text and token in text:
        return text.replace(token, "[redacted]")
    return text
