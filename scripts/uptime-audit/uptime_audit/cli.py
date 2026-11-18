"""Command line for the uptime coverage audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from uptime_audit.errors import AuditError
from uptime_audit.inventory import load_inventory
from uptime_audit.report import compare, render_json, render_markdown, summary_line
from uptime_audit.sentry import fetch_monitors, parse_monitor_page

DEFAULT_INVENTORY = Path(__file__).resolve().parent.parent / "expected-hosts.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uptime_audit",
        description="Compare the reviewed public host list with Sentry uptime monitors.",
    )
    parser.add_argument("--expected", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--monitors",
        type=Path,
        help="Read monitors from a JSON file instead of calling Sentry.",
    )
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--markdown", dest="markdown_path", type=Path)
    parser.add_argument(
        "--fail-on-uncovered",
        action="store_true",
        help="Exit 1 when a certain health URL has no active monitor.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SENTRY_UPTIME_API_BASE", "https://us.sentry.io"),
        help="Sentry API origin. Defaults to https://us.sentry.io.",
    )
    args = parser.parse_args(argv)
    try:
        inventory = load_inventory(args.expected)
        if args.monitors:
            monitors = parse_monitor_page(_read_fixture(args.monitors))
        else:
            token = os.environ.get("SENTRY_AUTH_TOKEN", "")
            monitors = fetch_monitors(inventory.org, token, base=args.base_url)
        report = compare(inventory, monitors)
        markdown = render_markdown(report)
        payload = render_json(report)
        if args.json_path:
            _write(args.json_path, payload)
        if args.markdown_path:
            _write(args.markdown_path, markdown)
        else:
            sys.stdout.write(markdown)
        print(summary_line(report), file=sys.stderr)
    except AuditError as exc:
        print(f"uptime-audit: {exc}", file=sys.stderr)
        return 2
    if args.fail_on_uncovered and report["uncovered"]:
        return 1
    return 0


def _read_fixture(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"could not read monitor fixture {path}") from exc


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
