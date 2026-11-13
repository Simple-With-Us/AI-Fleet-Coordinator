#!/usr/bin/env python3
"""CLI entry for the CI red streak report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Allow `python3 scripts/ci-red-report/__main__.py` from repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from github import GitHubClient  # type: ignore
    from scanner import DEFAULT_MAX_RUNS, DEFAULT_ORG, build_report, render_markdown  # type: ignore
else:
    from .github import GitHubClient
    from .scanner import DEFAULT_MAX_RUNS, DEFAULT_ORG, build_report, render_markdown


def _token_from_env() -> str:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report default-branch CI failure streaks across an org."
    )
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"GitHub org (default: {DEFAULT_ORG})")
    parser.add_argument(
        "--repo", action="append", dest="repos", metavar="NAME", help="Limit to repo(s)"
    )
    parser.add_argument("--json-out", metavar="PATH", help="Write JSON report to PATH")
    parser.add_argument("--markdown-out", metavar="PATH", help="Write markdown summary to PATH")
    parser.add_argument(
        "--fail-if-streak-ge",
        type=int,
        metavar="N",
        default=None,
        help="Exit 1 when any streak length is >= N (default: always exit 0)",
    )
    parser.add_argument(
        "--max-runs-per-workflow",
        type=int,
        default=DEFAULT_MAX_RUNS,
        help=f"Max completed runs to inspect per workflow (default: {DEFAULT_MAX_RUNS})",
    )
    parser.add_argument(
        "--visibility-note",
        default="",
        help="Optional note embedded in JSON (e.g. token scope limits)",
    )
    args = parser.parse_args(argv)

    token = _token_from_env()
    if not token:
        print("ci-red-report: set GITHUB_TOKEN or GH_TOKEN", file=sys.stderr)
        return 2

    client = GitHubClient(token)
    report = build_report(
        client,
        org=args.org,
        repos=args.repos,
        max_runs_per_workflow=max(1, args.max_runs_per_workflow),
        visibility_note=args.visibility_note.strip(),
    )
    md = render_markdown(report)
    payload = json.dumps(report, indent=2) + "\n"

    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)

    if args.markdown_out:
        Path(args.markdown_out).write_text(md, encoding="utf-8")
    else:
        sys.stderr.write(md)

    if args.fail_if_streak_ge is not None:
        threshold = args.fail_if_streak_ge
        reds = report.get("red_workflows")
        if isinstance(reds, list):
            for row in reds:
                if isinstance(row, dict) and int(row.get("streak_length") or 0) >= threshold:
                    return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
