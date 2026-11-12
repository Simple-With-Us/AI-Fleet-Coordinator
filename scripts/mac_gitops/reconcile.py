"""Check-only and optional file-copy apply for Mac GitOps G7."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from mac_gitops.diff import MacLayout, diff_manifest, parse_pm2_jlist
from mac_gitops.manifest import default_manifest_path, load_manifest
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_REFUSED = 2
EXIT_USAGE = 64


def repo_root_from_manifest(manifest_path: Path) -> Path:
    return manifest_path.resolve().parents[2]


def read_pm2_names() -> frozenset[str] | None:
    if shutil.which("pm2") is None:
        return None
    try:
        proc = subprocess.run(
            ["pm2", "jlist"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return parse_pm2_jlist(proc.stdout)


def build_layout(
    repo_root: Path,
    home: Path | None,
    *,
    include_pm2: bool,
) -> MacLayout:
    home_path = (home or Path.home()).expanduser().resolve()
    pm2_names = read_pm2_names() if include_pm2 else None
    return MacLayout(
        home=home_path,
        launch_agents_dir=home_path / "Library" / "LaunchAgents",
        repo_root=repo_root,
        pm2_app_names=pm2_names,
        pm2_ecosystem_installed=home_path / "apps" / "pm2-ecosystem.config.cjs",
    )


def apply_file_copy(manifest_path: Path, layout: MacLayout) -> list[str]:
    """Copy tracked plists and helpers into the owner install paths. Never launchctl load."""
    manifest = load_manifest(manifest_path)
    actions: list[str] = []
    for spec in manifest.launch_agents:
        template = layout.repo_root / spec.plist_template
        dest = layout.launch_agents_dir / f"{spec.label}.plist"
        if not template.is_file():
            actions.append(f"skip missing template {spec.plist_template}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, dest)
        actions.append(f"copied plist {spec.label}")
        for helper in spec.helpers:
            src = layout.repo_root / helper.repo
            dst = layout.home / helper.installed
            if not src.is_file():
                actions.append(f"skip missing helper {helper.repo}")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            actions.append(f"copied helper {helper.installed}")
    pm2 = manifest.pm2
    src_eco = layout.repo_root / pm2.ecosystem_template
    dst_eco = layout.home / pm2.ecosystem_installed
    if src_eco.is_file():
        dst_eco.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_eco, dst_eco)
        actions.append("copied pm2 ecosystem")
    return actions


def run_check(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    repo_root = repo_root_from_manifest(manifest_path) if args.repo_root is None else Path(args.repo_root)
    layout = build_layout(
        repo_root,
        Path(args.home) if args.home else None,
        include_pm2=not args.skip_pm2,
    )
    result = diff_manifest(manifest, layout, report_excluded=not args.skip_excluded_report)
    payload = {
        "schemaVersion": 1,
        "tool": "mac-gitops-reconcile",
        "mode": "check",
        "manifest": str(manifest_path),
        "manifestIssue": manifest.issue,
        "excludedLaunchAgentCount": len(manifest.excluded_launch_agents),
        "desiredLaunchAgentCount": len(manifest.launch_agents),
        "pm2AppCount": len(manifest.pm2.app_names),
        **result.to_json(),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        if result.ok:
            print("mac-gitops: ok (no drift)")
        else:
            print(f"mac-gitops: drift ({len(result.drifts)} item(s))")
            for item in result.drifts:
                print(f"  {item.kind.value}\t{item.target}\t{item.detail}")
    return EXIT_OK if result.ok else EXIT_DRIFT


def run_apply(args: argparse.Namespace) -> int:
    if not args.apply:
        print("refused: apply requires --apply", file=sys.stderr)
        return EXIT_REFUSED
    if platform.system() != "Darwin":
        print("refused: apply runs only on Darwin", file=sys.stderr)
        return EXIT_REFUSED
    manifest_path = Path(args.manifest).expanduser().resolve()
    repo_root = repo_root_from_manifest(manifest_path) if args.repo_root is None else Path(args.repo_root)
    layout = build_layout(repo_root, Path(args.home) if args.home else None, include_pm2=False)
    actions = apply_file_copy(manifest_path, layout)
    if args.json:
        print(json.dumps({"mode": "apply", "actions": actions}, indent=2))
    else:
        for line in actions:
            print(line)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare deploy/mac/manifest.toml to the live Mac (check-only by default)."
    )
    parser.add_argument(
        "--manifest",
        default=str(default_manifest_path()),
        help="Path to deploy/mac/manifest.toml",
    )
    parser.add_argument("--repo-root", help="AFC repo root (default: parent of deploy/mac/)")
    parser.add_argument("--home", help="Owner home for Library/LaunchAgents (default: $HOME)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy tracked plists/helpers/ecosystem into install paths (no launchctl load)",
    )
    parser.add_argument("--skip-pm2", action="store_true", help="Do not query pm2 jlist")
    parser.add_argument(
        "--skip-excluded-report",
        action="store_true",
        help="Do not report EXCLUDED_PRESENT plists",
    )
    args = parser.parse_args(argv)
    if args.apply:
        return run_apply(args)
    return run_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
