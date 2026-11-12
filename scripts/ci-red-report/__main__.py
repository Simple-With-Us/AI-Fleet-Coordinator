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
    from fleet_apps import (  # type: ignore
        FleetAppsError,
        fleet_apps_path,
        fleet_token_visibility_note,
        load_fleet_repo_names,
    )
    from github import GitHubClient  # type: ignore
    from scanner import DEFAULT_MAX_RUNS, DEFAULT_ORG, build_report, render_markdown  # type: ignore
else:
    from .fleet_apps import (
        FleetAppsError,
        fleet_apps_path,
        fleet_token_visibility_note,
        load_fleet_repo_names,
    )
    from .github import GitHubClient
    from .scanner import DEFAULT_MAX_RUNS, DEFAULT_ORG, build_report, render_markdown

ENV_FLEET_READ_TOKEN = "FLEET_READ_TOKEN"


def _token_from_env() -> tuple[str, bool]:
    """Return (token, using_fleet_read_token)."""
    fleet = os.environ.get(ENV_FLEET_READ_TOKEN, "").strip()
    if fleet:
        return fleet, True
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value, False
    return "", False


def _resolve_repos_and_note(
    *,
    explicit_repos: list[str] | None,
    using_fleet_token: bool,
    visibility_note: str,
    fleet_apps: Path | None,
    fleet_apps_explicit: bool = False,
) -> tuple[list[str] | None, str] | None:
    """Return (repos, note), or None after printing why the inputs are unusable."""
    if explicit_repos:
        return explicit_repos, visibility_note
    if fleet_apps_explicit and not using_fleet_token:
        print(
            "ci-red-report: --fleet-apps needs FLEET_READ_TOKEN; refusing to scan a different repo set",
            file=sys.stderr,
        )
        return None
    if using_fleet_token:
        try:
            repos = load_fleet_repo_names(path=fleet_apps)
        except FleetAppsError as exc:
            print(f"ci-red-report: {exc}", file=sys.stderr)
            return None
        note = visibility_note.strip() or fleet_token_visibility_note()
        return repos, note
    return None, visibility_note


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
    parser.add_argument(
        "--fleet-apps",
        type=Path,
        default=None,
        help="Path to fleet-apps.json (default: repo root fleet-apps.json)",
    )
    args = parser.parse_args(argv)

    token, using_fleet_token = _token_from_env()
    if not token:
        print(
            "ci-red-report: set FLEET_READ_TOKEN, GITHUB_TOKEN, or GH_TOKEN",
            file=sys.stderr,
        )
        return 2

    fleet_apps = args.fleet_apps
    if fleet_apps is None:
        fleet_apps = fleet_apps_path()

    resolved = _resolve_repos_and_note(
        explicit_repos=args.repos,
        using_fleet_token=using_fleet_token,
        visibility_note=args.visibility_note.strip(),
        fleet_apps=fleet_apps,
        fleet_apps_explicit=args.fleet_apps is not None,
    )
    if resolved is None:
        return 2
    repos, visibility = resolved

    client = GitHubClient(token)
    report = build_report(
        client,
        org=args.org,
        repos=repos,
        max_runs_per_workflow=max(1, args.max_runs_per_workflow),
        visibility_note=visibility,
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
