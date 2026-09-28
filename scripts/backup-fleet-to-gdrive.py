#!/usr/bin/env python3
"""Backup fleet git repositories to Google Drive.

Canonical Drive backup for every app under ~/Code.  Replaces the Personal-Site
GitHub Action that only uploaded 90-day artifacts and never wrote Drive.

Repo set:
  1. Every row in fleet-apps.json (new onboarded apps are included automatically)
  2. Any other git checkout directly under ~/Code, minus SKIP_NAMES

Creates/updates dated folder "Website & App Source Backups - YYYY-MM-DD"
under the Google Drive "My Drive" mount.

Usage:
  python3 scripts/backup-fleet-to-gdrive.py
  python3 scripts/backup-fleet-to-gdrive.py --list
  python3 scripts/backup-fleet-to-gdrive.py --only Personal-Site ContactLogo
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path

DATE_STR = datetime.date.today().strftime("%Y-%m-%d")

GDRIVE_CANDIDATES = [
    Path("/Users/jay/Google Drive/My Drive"),
    Path("/Users/jay/Library/CloudStorage/GoogleDrive-jaywedgeworth22@gmail.com/My Drive"),
]

CODE_ROOT = Path("/Users/jay/Code")

# Same denylist as code-main-keeper.sh.  Pionex is a leftover checkout, not a fleet app.
SKIP_NAMES = {
    "copilot-worktrees",
    "data",
    "Icons - Logos",
    "Pionex",
}

EXCLUDE_DIRS = {
    "node_modules",
    ".next",
    ".turbo",
    "dist",
    "build",
    ".dart_tool",
    "Pods",
    ".git",
    ".cache",
    ".venv",
    "venv",
    ".gradle",
    ".idea",
}

EXCLUDE_EXTS = {".sock", ".log", ".tmp", ".swp", ".pyc", ".env", ".pem", ".p12"}
EXCLUDE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "global-api-keys",
}

DOC_FILES = [
    "README.md",
    "AGENTS.md",
    "STATUS.md",
    "PLAN.md",
    "PROJECT.md",
    "package.json",
    "Cargo.toml",
    "pubspec.yaml",
]


def fleet_apps_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "fleet-apps.json",
        CODE_ROOT / "AI-Fleet-Coordinator" / "fleet-apps.json",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    print("fleet-apps.json not found (repo copy or ~/Code/AI-Fleet-Coordinator).", file=sys.stderr)
    sys.exit(1)


def load_fleet_repos() -> list[str]:
    data = json.loads(fleet_apps_path().read_text())
    repos = [app["repo"] for app in data.get("apps", []) if app.get("repo")]
    return repos


def discover_code_git_dirs() -> list[str]:
    extra: list[str] = []
    if not CODE_ROOT.is_dir():
        return extra
    for entry in sorted(CODE_ROOT.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in SKIP_NAMES:
            continue
        git = entry / ".git"
        if git.is_dir() or git.is_file():
            extra.append(entry.name)
    return extra


def planned_repos() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in load_fleet_repos() + discover_code_git_dirs():
        if name in seen or name in SKIP_NAMES:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def find_gdrive() -> Path:
    for cand in GDRIVE_CANDIDATES:
        if cand.is_dir():
            return cand
    print("Google Drive directory not found on this machine.", file=sys.stderr)
    sys.exit(1)


def should_skip_file(name: str) -> bool:
    if name == ".DS_Store" or name in EXCLUDE_NAMES:
        return True
    ext = os.path.splitext(name)[1].lower()
    return ext in EXCLUDE_EXTS


def zip_repo(repo_path: Path, zip_out: Path) -> int:
    file_count = 0
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for file in files:
                if should_skip_file(file):
                    continue
                full_path = Path(root) / file
                try:
                    st = os.lstat(full_path)
                    if stat.S_ISSOCK(st.st_mode) or stat.S_ISFIFO(st.st_mode):
                        continue
                    if full_path.is_symlink() and not full_path.exists():
                        continue
                    rel_path = full_path.relative_to(repo_path)
                    zf.write(full_path, arcname=str(Path(repo_path.name) / rel_path))
                    file_count += 1
                except OSError:
                    continue
    return file_count


def copy_docs(repo_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for doc in DOC_FILES:
        src = repo_path / doc
        if src.is_file():
            try:
                shutil.copy2(src, dest_dir / doc)
            except OSError:
                continue


def write_notes(dest_dir: Path, backed_up: list[str], skipped: list[str]) -> None:
    lines = [
        f"# Source Backups — {DATE_STR}",
        "",
        "Comprehensive backup of AI fleet repositories from `/Users/jay/Code`.",
        "",
        "Canonical job: `scripts/backup-fleet-to-gdrive.py` (Mac launchd",
        "`com.jay.fleet-gdrive-backup`).  Repo list = `fleet-apps.json` plus any",
        "other git checkout directly under `~/Code` (minus the code-main-keeper skip list).",
        "",
        "## Included repositories",
    ]
    for name in backed_up:
        lines.append(f"- **{name}**")
    if skipped:
        lines.extend(["", "## Skipped (missing locally)"])
        for name in skipped:
            lines.append(f"- {name}")
    lines.extend(
        [
            "",
            "## Backup structure",
            f"- Zip archives (`*-backup-{DATE_STR}.zip`): source trees without node_modules/build artifacts.",
            "- Source docs folders (`* (source docs)`): README / AGENTS / STATUS for browsing in Drive.",
            "",
            "Generated by: `scripts/backup-fleet-to-gdrive.py`",
            "",
        ]
    )
    (dest_dir / "BACKUP-NOTES.md").write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print the repo list and exit")
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="REPO",
        help="Backup only these repo folder names",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repos = planned_repos()
    if args.only:
        wanted = set(args.only)
        missing = sorted(wanted - set(repos) - {p.name for p in CODE_ROOT.iterdir() if p.is_dir()} if CODE_ROOT.is_dir() else wanted)
        repos = [r for r in repos if r in wanted]
        extra = [name for name in args.only if name not in repos]
        repos.extend(extra)
        if missing:
            print(f"note: --only names not in the auto list: {', '.join(missing)}")

    if args.list:
        print(f"fleet-apps.json: {fleet_apps_path()}")
        for name in repos:
            path = CODE_ROOT / name
            mark = "ok" if path.is_dir() else "missing"
            print(f"  {name}\t{mark}")
        return 0

    gdrive = find_gdrive()
    dest_dir = gdrive / f"Website & App Source Backups - {DATE_STR}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Target Google Drive folder: {dest_dir}")

    backed_up: list[str] = []
    skipped: list[str] = []
    for repo in repos:
        repo_path = CODE_ROOT / repo
        if not repo_path.is_dir():
            print(f"skip {repo} (not found at {repo_path})")
            skipped.append(repo)
            continue
        print(f"Backing up {repo}...")
        zip_out = dest_dir / f"{repo}-backup-{DATE_STR}.zip"
        count = zip_repo(repo_path, zip_out)
        size_mb = zip_out.stat().st_size / (1024 * 1024)
        print(f"  saved {zip_out.name} ({count} files, {size_mb:.2f} MB)")
        backed_up.append(repo)

    findings_db = Path.home() / "apps" / "mac-collab" / "findings.db"
    if findings_db.is_file():
        try:
            import sqlite3
            db_dest = dest_dir / f"findings-{DATE_STR}.db"
            src_conn = sqlite3.connect(findings_db)
            dst_conn = sqlite3.connect(db_dest)
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            print(f"  saved {db_dest.name} (findings.db SQLite snapshot)")
        except Exception as e:
            print(f"WARN: findings.db gdrive backup failed: {e}", file=sys.stderr)

    write_notes(dest_dir, backed_up, skipped)
    if not backed_up:
        print("No repositories were backed up.", file=sys.stderr)
        return 1
    print(f"Backed up {len(backed_up)} repositories to {dest_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
