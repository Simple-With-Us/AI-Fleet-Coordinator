"""Command line for the cron monitor audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cron_monitor_audit.classify import build_catalog, classify_monitor, explainable
from cron_monitor_audit.errors import AuditError, SentryForbidden
from cron_monitor_audit.report import build_report, render_json, render_markdown, summary_line
from cron_monitor_audit.sentry import fetch_monitors, parse_monitor_page
from cron_monitor_audit.sources import (
    CodeHit,
    CodeSearch,
    RepoSource,
    fetch_repos,
    fleet_identities,
    load_apps,
    scan_checkout,
    search_code,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_APPS = REPO_ROOT / "fleet-apps.json"
LOCAL_REPO = "Simple-With-Us/AI-Fleet-Coordinator"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cron_monitor_audit",
        description="List Sentry cron monitors and match them to check-in senders. Read-only.",
    )
    parser.add_argument("--apps", type=Path, default=DEFAULT_APPS)
    parser.add_argument("--checkout", type=Path, default=REPO_ROOT)
    parser.add_argument("--monitors", type=Path, help="Read monitors from JSON instead of Sentry.")
    parser.add_argument(
        "--sources", type=Path, help="Read repo sources from a directory instead of GitHub."
    )
    parser.add_argument(
        "--code-search", type=Path, help="Read code-search hits from JSON instead of GitHub."
    )
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--markdown", dest="markdown_path", type=Path)
    parser.add_argument("--org", default="simple-with-us")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SENTRY_API_BASE", "https://us.sentry.io"),
    )
    parser.add_argument(
        "--github-base-url",
        default=os.environ.get("GITHUB_API_BASE", "https://api.github.com"),
    )
    args = parser.parse_args(argv)
    try:
        apps = load_apps(args.apps)
        if args.monitors:
            monitors = parse_monitor_page(_read_json(args.monitors))
        else:
            monitors = fetch_monitors(args.org, _sentry_token(), base=args.base_url)
        if args.sources:
            repos = _load_sources(args.sources)
            token = ""
        else:
            local = scan_checkout(args.checkout, LOCAL_REPO)
            repos, token = fetch_repos(
                apps,
                os.environ.get("GITHUB_TOKEN", ""),
                base=args.github_base_url,
                skip={LOCAL_REPO},
            )
            repos.append(local)
        identities = fleet_identities(apps)
        if args.code_search:
            search = _load_search(args.code_search)
        else:
            catalog = build_catalog(repos, identities)
            pending = [
                monitor.slug for monitor in monitors if not explainable(monitor.slug, catalog)
            ]
            search = search_code(pending, apps[0].owner, token, base=args.github_base_url)
        catalog = build_catalog(repos, identities, search)
        findings = [classify_monitor(monitor, catalog) for monitor in monitors]
        report = build_report(args.org, monitors, findings, catalog)
        markdown = render_markdown(report)
        payload = render_json(report)
        if args.json_path:
            _write(args.json_path, payload)
        if args.markdown_path:
            _write(args.markdown_path, markdown)
        else:
            sys.stdout.write(markdown)
        print(summary_line(report), file=sys.stderr)
    except SentryForbidden as exc:
        print(f"cron-monitor-audit: {exc}", file=sys.stderr)
        return 3
    except AuditError as exc:
        print(f"cron-monitor-audit: {exc}", file=sys.stderr)
        return 2
    return 0


def _sentry_token() -> str:
    full = os.environ.get("SENTRY_AUTH_TOKEN_FULLSCOPE", "").strip()
    plain = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
    return full or plain


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"could not read {path}") from exc


def _load_sources(path: Path) -> list[RepoSource]:
    payload = _read_json(path / "repos.json")
    if not isinstance(payload, list):
        raise AuditError("sources repos.json was not a list")
    repos = []
    for row in payload:
        if not isinstance(row, dict):
            raise AuditError("sources repos.json has a row that is not an object")
        files = []
        for item in row.get("files") or []:
            if not isinstance(item, dict):
                raise AuditError("sources file entry was not an object")
            rel = item.get("path")
            if not isinstance(rel, str):
                raise AuditError("sources file path is invalid")
            files.append((rel, (path / rel).read_text(encoding="utf-8")))
        names = row.get("workflow_names") or []
        repos.append(
            RepoSource(
                full_name=str(row.get("full_name") or ""),
                readable=bool(row.get("readable", True)),
                names_known=bool(row.get("names_known", True)),
                workflow_names=tuple(str(name) for name in names),
                files=tuple(files),
                reason=str(row.get("reason") or ""),
            )
        )
    return repos


def _load_search(path: Path) -> CodeSearch:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise AuditError("code search fixture was not an object")
    hits = {}
    for slug, rows in (payload.get("hits") or {}).items():
        parsed = []
        for row in rows:
            parsed.append(CodeHit(str(row.get("path") or ""), str(row.get("fragment") or "")))
        hits[str(slug)] = tuple(parsed)
    return CodeSearch(
        str(payload.get("state") or "unavailable"),
        str(payload.get("detail") or ""),
        hits,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
