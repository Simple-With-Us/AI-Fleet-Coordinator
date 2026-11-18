"""Check-only and optional file-copy apply for Hetzner GitOps G1.

Check is the default.  --apply copies tracked unit, timer, and cron bytes
into --live-root.  It does not reload systemd, restart units, enable timers,
or delete forbidden files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from hetzner_gitops.diff import LiveLayout, PathEscape, diff_manifest, resolve_under
from hetzner_gitops.manifest import FileSpec, HetznerManifest, default_manifest_path, load_manifest

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_REFUSED = 2


def repo_root_from_manifest(manifest_path: Path) -> Path:
    return manifest_path.resolve().parents[2]


def _atomic_write(payload: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".hetzner-gitops-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, payload)
    except Exception:
        os.close(fd)
        os.unlink(temporary)
        raise
    os.close(fd)
    os.chmod(temporary, 0o644)
    os.replace(temporary, dest)
    os.chmod(dest, 0o644)


def apply_copies(manifest: HetznerManifest, layout: LiveLayout) -> list[str]:
    """Copy desired files.  Does not delete forbid matches or touch excluded units.

    A missing source copies nothing.  Check reports that same source as drift.
    """
    if not layout.live_root.is_dir():
        raise PathEscape(f"live root is not a directory: {layout.live_root}")
    missing: list[str] = []
    staged: list[tuple[FileSpec, Path, Path]] = []
    for spec in manifest.files:
        source = layout.repo_root / spec.source
        dest = resolve_under(layout.live_root, spec.installed)
        if not source.is_file():
            missing.append(f"skip missing source {spec.source}")
            continue
        staged.append((spec, source, dest))
    if missing:
        return missing
    actions: list[str] = []
    for spec, source, dest in staged:
        _atomic_write(source.read_bytes(), dest)
        actions.append(f"copied {spec.id}")
    return actions


def _payload(manifest: HetznerManifest, manifest_path: Path, mode: str) -> dict:
    return {
        "schemaVersion": 1,
        "tool": "hetzner-gitops-reconcile",
        "mode": mode,
        "manifest": str(manifest_path),
        "manifestIssue": manifest.issue,
        "desiredCount": len(manifest.files),
        "excludedCount": len(manifest.excluded_units),
        "gapCount": len(manifest.gaps),
        "gaps": [
            {"id": gap.id, "kind": gap.kind, "inventory": gap.inventory, "note": gap.note}
            for gap in manifest.gaps
        ],
    }


def run_check(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else repo_root_from_manifest(manifest_path)
    )
    live_root = Path(args.live_root).expanduser() if args.live_root else Path("/")
    layout = LiveLayout(live_root=live_root, repo_root=repo_root)
    result = diff_manifest(manifest, layout)
    payload = {**_payload(manifest, manifest_path, "check"), **result.to_json()}
    if args.json:
        print(json.dumps(payload, indent=2))
    elif result.ok:
        print(f"hetzner-gitops: ok (no drift); gaps={len(manifest.gaps)} (not drift)")
    else:
        print(
            f"hetzner-gitops: drift ({len(result.drifts)} item(s)); gaps={len(manifest.gaps)} (not drift)"
        )
        for item in result.drifts:
            print(f"  {item.kind.value}\t{item.target}\t{item.detail}")
    return EXIT_OK if result.ok else EXIT_DRIFT


def run_apply(args: argparse.Namespace) -> int:
    if not args.apply:
        print("refused: apply requires --apply", file=sys.stderr)
        return EXIT_REFUSED
    if not args.live_root:
        print("refused: apply requires --live-root", file=sys.stderr)
        return EXIT_REFUSED
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else repo_root_from_manifest(manifest_path)
    )
    live_root = Path(args.live_root).expanduser().resolve()
    layout = LiveLayout(live_root=live_root, repo_root=repo_root)
    try:
        actions = apply_copies(manifest, layout)
    except PathEscape as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if args.json:
        print(
            json.dumps({**_payload(manifest, manifest_path, "apply"), "actions": actions}, indent=2)
        )
    else:
        for line in actions:
            print(line)
    if any(line.startswith("skip missing source ") for line in actions):
        return EXIT_DRIFT
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare deploy/hetzner/manifest.toml to a live root (check-only by default)."
    )
    parser.add_argument(
        "--manifest",
        default=str(default_manifest_path()),
        help="Path to deploy/hetzner/manifest.toml",
    )
    parser.add_argument("--repo-root", help="AFC repo root (default: parent of deploy/hetzner/)")
    parser.add_argument(
        "--live-root",
        help="Directory that stands in for the host root. Check defaults to /. Apply requires this flag.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy tracked files into --live-root. Does not reload or restart anything.",
    )
    args = parser.parse_args(argv)
    if args.apply:
        return run_apply(args)
    return run_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
