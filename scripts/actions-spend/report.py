#!/usr/bin/env python3
"""Write the Actions spend report for one repository.

Read-only. The token is read from the environment or from `gh auth token`
and is never printed. Job summaries and the artifact files are the output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import github_api
import spend

HERE = Path(__file__).resolve().parent
DEFAULT_WORKFLOWS = HERE.parents[1] / ".github" / "workflows"


def resolve_token():
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(key)
        if not token:
            continue
        client = github_api.Client(token)
        try:
            client.get("rate_limit")
        except github_api.ApiError as exc:
            if exc.status != 401:
                raise
            continue
        return token
    env = os.environ.copy()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    proc = subprocess.run(["gh", "auth", "token"], capture_output=True, env=env)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(f"GitHub auth failed (status {proc.returncode}). No token is printed.")
    return proc.stdout.decode().strip()


def fetch_slice(client, repo, start, end):
    created = spend.format_time(start) + ".." + spend.format_time(end)
    runs = []
    capped = True
    for page in range(1, 13):
        data = client.get(
            f"repos/{repo}/actions/runs",
            {"per_page": "100", "page": str(page), "created": created},
        )
        batch = data.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < 100:
            capped = False
            break
    return runs, capped


def _profile_has_macos(profile):
    for job in (profile or {}).get("jobs") or []:
        if "macos" in job.get("operating_systems", ()):
            return True
    return False


def _order_lookups(runs, profiles=None):
    """Finish macOS workflows first, then the largest remaining workflows.

    A rate-limit stop then leaves the costliest workflows fully measured.
    Round-robin would leave every workflow a partial lower bound.
    """
    profiles = profiles or {}
    grouped = defaultdict(list)
    for run in runs:
        grouped[run.get("path") or ""].append(run)

    def rank(path):
        macos = _profile_has_macos(profiles.get(path))
        return (1 if macos else 0, len(grouped[path]))

    order = []
    for path in sorted(grouped, key=rank, reverse=True):
        group = sorted(grouped[path], key=lambda item: item.get("created_at") or "")
        order.extend(group)
    return order


def _measure_one(client, repo, run, use_timing):
    timing = None
    if use_timing:
        try:
            timing = client.get(f"repos/{repo}/actions/runs/{run['id']}/timing")
        except github_api.ApiError as exc:
            if exc.status != 404:
                raise
        parsed = spend.minutes_from_timing(timing)
        if parsed is not None:
            parsed["source"] = "timing"
            return parsed
    try:
        jobs = []
        for page in range(1, 11):
            data = client.get(
                f"repos/{repo}/actions/runs/{run['id']}/jobs",
                {"per_page": "100", "page": str(page)},
            )
            batch = data.get("jobs") or []
            jobs.extend(batch)
            if len(batch) < 100:
                break
    except github_api.ApiError as exc:
        if exc.status == 404:
            result = {"minutes": spend.empty_minutes(), "jobs": [], "source": "jobs"}
            return result
        raise
    result = spend.minutes_from_jobs(jobs)
    result["source"] = "jobs"
    return result


def _load_cache(path):
    found = {}
    if path is None or not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        found[row["id"]] = row["result"]
    return found


def _save_cache(path, cache):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for run_id, result in cache.items():
            handle.write(json.dumps({"id": run_id, "result": result}) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def measure(client, repo, runs, cache_path, workers, profiles=None):
    cache = _load_cache(cache_path)
    lookups = [run for run in runs if spend.needs_job_lookup(run) and run["id"] not in cache]
    probe = lookups[:8]
    use_timing = False
    positives = 0
    partial = client.stop
    for run in probe:
        try:
            result = _measure_one(client, repo, run, use_timing=True)
        except github_api.ApiError:
            partial = True
            break
        if result.get("source") == "timing":
            positives += 1
        cache[run["id"]] = result
    use_timing = positives > 0
    method = "timing" if use_timing else "jobs"
    if not probe:
        method = "none"
    print(f"minute source={method} probed={len(probe)}", file=sys.stderr)
    pending = [run for run in lookups if run["id"] not in cache]
    order = _order_lookups(pending, profiles)
    counts = defaultdict(int)
    seen = []
    for run in order:
        path = run.get("path") or ""
        if path not in counts:
            seen.append(path)
        counts[path] += 1
    preview = ", ".join(f"{counts[path]} {path.rsplit('/', 1)[-1]}" for path in seen[:8])
    print(f"lookup order {preview}", file=sys.stderr)
    lock = threading.Lock()
    done = 0

    def work(run):
        nonlocal done
        if client.stop:
            return
        try:
            result = _measure_one(client, repo, run, use_timing)
        except github_api.RateLimited:
            return
        except github_api.ApiError as exc:
            print(f"measure failed status={exc.status} path={run.get('path')}", file=sys.stderr)
            return
        with lock:
            cache[run["id"]] = result
            done += 1
            if done % 25 == 0:
                _save_cache(cache_path, cache)
            if done % 100 == 0:
                print(f"measured {done}/{len(order)}", file=sys.stderr)

    if order:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(work, order))
    _save_cache(cache_path, cache)
    if client.stop or any(spend.needs_job_lookup(run) and run["id"] not in cache for run in runs):
        partial = True
    return cache, method, partial


def build(runs, measured, profiles, meta):
    usable = {}
    for run_id, result in measured.items():
        if result is None:
            continue
        usable[run_id] = result
    return spend.assemble(runs, usable, profiles, meta)


def write_outputs(report, out_dir, token):
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = spend.redact(spend.render_markdown(report), token)
    document = spend.redact(json.dumps(report, indent=2) + "\n", token)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    (out_dir / "report.json").write_text(document, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(markdown)
    return markdown


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Read-only GitHub Actions spend report")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--workflows-dir", type=Path, default=DEFAULT_WORKFLOWS)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--slice-hours", type=float, default=2)
    parser.add_argument("--runs-file", type=Path, default=None)
    parser.add_argument("--reuse-runs", action="store_true")
    parser.add_argument("--max-long-waits", type=int, default=1)
    parser.add_argument("--max-wait-minutes", type=float, default=45)
    return parser.parse_args(argv)


def _write_runs(path, runs, truncated, start, end):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "start": spend.format_time(start),
        "end": spend.format_time(end),
        "truncated": truncated,
        "runs": runs,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_runs(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["runs"], bool(payload.get("truncated")), payload["start"], payload["end"]


def main(argv=None):
    args = parse_args(argv)
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=args.days)
    token = resolve_token()

    def refresh_token():
        try:
            fresh = resolve_token()
        except (SystemExit, OSError):
            return None
        print("refreshed GitHub auth after HTTP 401", file=sys.stderr)
        return fresh

    client = github_api.Client(
        token,
        max_wait_s=max(1.0, args.max_wait_minutes) * 60,
        max_long_waits=max(0, args.max_long_waits),
        refresh=refresh_token,
    )
    profiles = spend.load_profiles(args.workflows_dir)

    def fetch(slice_start, slice_end):
        return fetch_slice(client, args.repo, slice_start, slice_end)

    reused = False
    if args.reuse_runs and args.runs_file and args.runs_file.exists():
        runs, truncated, start_text, end_text = _read_runs(args.runs_file)
        start = spend.parse_time(start_text)
        end = spend.parse_time(end_text)
        reused = True
        print(
            f"reusing {len(runs)} listed runs from {start_text} to {end_text}",
            file=sys.stderr,
        )
    else:
        print(
            f"listing runs from {spend.format_time(start)} to {spend.format_time(end)}",
            file=sys.stderr,
        )

        def progress(index, total, count, truncated_now):
            print(
                f"listed slices {index}/{total} runs={count} truncated={str(truncated_now).lower()}",
                file=sys.stderr,
            )

        runs, truncated = spend.collect_runs(
            fetch, start, end, slice_hours=args.slice_hours, progress=progress
        )
        if args.runs_file:
            _write_runs(args.runs_file, runs, truncated, start, end)
    lookups = sum(1 for run in runs if spend.needs_job_lookup(run))
    print(
        f"listed {len(runs)} runs, {lookups} need a job lookup, reused={str(reused).lower()}",
        file=sys.stderr,
    )
    cache_path = args.cache
    if cache_path is None:
        cache_path = Path(os.environ.get("TMPDIR", "/tmp")) / "actions-spend-cache.jsonl"
    measured, method, measure_partial = measure(
        client, args.repo, runs, cache_path, args.workers, profiles
    )
    meta = {
        "repo": args.repo,
        "start": spend.format_time(start),
        "end": spend.format_time(end),
        "truncated": truncated,
        "method": method,
    }
    report = build(runs, measured, profiles, meta)
    if measure_partial:
        report["partial"] = True
    markdown = write_outputs(report, args.out_dir, token)
    print(f"wrote {args.out_dir / 'report.md'}", file=sys.stderr)
    print(
        f"runs={report['totals']['runs']} weighted={report['totals']['weighted']} "
        f"partial={str(report['partial']).lower()} api_calls={client.calls}",
        file=sys.stderr,
    )
    for item in report["workflows"][:5]:
        print(
            f"{item['weighted']}\t{item['linux_min']}\t{item['macos_min']}\t{item['windows_min']}\t{item['name']}",
            file=sys.stderr,
        )
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except github_api.ApiError as exc:
        print(f"GitHub API error status={exc.status}", file=sys.stderr)
        raise SystemExit(1) from None
