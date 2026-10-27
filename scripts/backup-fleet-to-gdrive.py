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
  python3 scripts/backup-fleet-to-gdrive.py --dry-run   # report the plan, change nothing
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

# The dated folder this job writes into Drive.  Retention only ever touches
# folders that match this prefix exactly, so nothing else in Drive is at risk.
BACKUP_FOLDER_PREFIX = "Website & App Source Backups - "

# How many daily backups to keep.  Two weeks of daily source backups is
# plenty of history to restore from, and the job was writing one folder a day
# with no retention at all.
DEFAULT_KEEP = 14

# The operator-visible wording for a retention pass that could not run.  It
# has to read differently from the normal "removed N" summary, because a
# skipped retention that looks like a successful zero-count prune is how the
# dated backup set grows without anybody noticing.
RETENTION_SKIPPED = "Retention: skipped (Drive root could not be listed)"

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


def dated_backup_folders(gdrive: Path) -> list[tuple[datetime.date, Path]] | None:
    """The dated backup folders in Drive, oldest first.

    Only a folder whose name is the prefix plus a real ISO date counts, so
    nothing else in Drive can be mistaken for a backup set.

    Returns None when the Drive root could not be listed, and a list when it
    could.  The two must not look alike: an empty list means there was
    nothing to prune, while None means the prune never got to look, and a
    refusal that reads as "nothing to prune" is how the dated backup set
    grows without anybody noticing.

    The Drive is a File Provider mount, and it refuses some requests
    outright: listing the root of a large Drive is one of them, and it has
    refused from the launchd job with "Operation not permitted" while the
    same call succeeds from a shell.  Retention is housekeeping, and it runs
    *after* the repository zips are already in Drive, so a refusal there must
    not take the job down: the run would fail, the mirror would never run,
    and the daily backup would be reported as broken when the part the owner
    depends on had already succeeded.

    A refusal can also come for one entry instead of the whole listing, so
    every per-entry filesystem call is guarded the same way.  One unreadable
    folder in the Drive must not cost the owner the whole backup.
    """
    found: list[tuple[datetime.date, Path]] = []
    try:
        if not gdrive.is_dir():
            print(
                f"WARN: {gdrive} is not a readable directory, so it cannot be listed "
                "to prune old backups",
                file=sys.stderr,
            )
            return None
        entries = sorted(gdrive.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        print(
            f"WARN: could not list {gdrive} to prune old backups: {exc}",
            file=sys.stderr,
        )
        return None
    for entry in entries:
        # The stat that decides whether the entry is a folder is the call
        # the File Provider mount refuses.  Skip the entry and keep going:
        # the rest of Drive is still worth pruning.
        try:
            entry_is_dir = entry.is_dir()
        except OSError as exc:
            print(f"WARN: could not stat {entry}: {exc}", file=sys.stderr)
            continue
        if not entry_is_dir or not entry.name.startswith(BACKUP_FOLDER_PREFIX):
            continue
        stamp = entry.name[len(BACKUP_FOLDER_PREFIX) :]
        try:
            day = datetime.date.fromisoformat(stamp)
        except ValueError:
            continue
        found.append((day, entry))
    found.sort(key=lambda item: item[0])
    return found


def prune_old_backups(gdrive: Path, keep: int, dry_run: bool = False) -> list[Path] | None:
    """Drop the dated backup folders beyond the newest `keep`.

    The job wrote one folder a day with no retention at all, so the backup
    set grew without bound: 48 folders of roughly 1.5 GB each by 2026-10-20.
    Two weeks of daily source backups is plenty of history, and the oldest
    ones are the ones nobody will ever restore.

    A dry run reports what would go and touches nothing.

    Returns None when the retention could not be applied, and the list of
    removed folders when it was.  None is not the same thing as an empty
    list: an empty list is a prune that ran and found nothing to remove,
    while None is a prune that never ran, and the operator has to be able to
    tell those apart.
    """
    if keep < 0:
        return []
    folders = dated_backup_folders(gdrive)
    if folders is None:
        print(RETENTION_SKIPPED, file=sys.stderr)
        return None
    doomed = folders[: max(0, len(folders) - keep)]
    removed: list[Path] = []
    for _day, path in doomed:
        if dry_run:
            print(f"would remove {path}")
            removed.append(path)
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            print(f"WARN: could not remove {path}: {exc}", file=sys.stderr)
            continue
        print(f"removed {path}")
        removed.append(path)
    return removed


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
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        metavar="N",
        help=f"How many daily backup folders to keep (default {DEFAULT_KEEP}; 0 keeps none)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be backed up and removed, and change nothing",
    )
    parser.add_argument(
        "--prune-only",
        action="store_true",
        help="Apply retention without taking a new backup",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repos = planned_repos()
    if args.only:
        wanted = set(args.only)
        missing = sorted(
            wanted - set(repos) - {p.name for p in CODE_ROOT.iterdir() if p.is_dir()}
            if CODE_ROOT.is_dir()
            else wanted
        )
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

    if args.prune_only:
        removed = prune_old_backups(gdrive, args.keep, dry_run=args.dry_run)
        if removed is None:
            # The retention never ran, so the summary says so instead of
            # counting a prune that did not happen.  The exit status stays 0:
            # housekeeping must never take the daily job down.
            print(RETENTION_SKIPPED)
            return 0
        verb = "would remove" if args.dry_run else "removed"
        print(f"{verb} {len(removed)} backup folder(s), keeping the newest {args.keep}")
        return 0

    dest_dir = gdrive / f"Website & App Source Backups - {DATE_STR}"
    # A dry run writes nothing:  no dated folder, no zips, no notes, no
    # findings.db copy, and no removal.  It only reports the plan.
    if not args.dry_run:
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
        zip_out = dest_dir / f"{repo}-backup-{DATE_STR}.zip"
        if args.dry_run:
            print(f"would back up {repo} -> {zip_out.name}")
            backed_up.append(repo)
            continue
        print(f"Backing up {repo}...")
        count = zip_repo(repo_path, zip_out)
        size_mb = zip_out.stat().st_size / (1024 * 1024)
        print(f"  saved {zip_out.name} ({count} files, {size_mb:.2f} MB)")
        backed_up.append(repo)

    # mac-collab was retired on 2026-10-19 (AFC-363):  the live tree moved to
    # ~/apps/_retired/mac-collab-20261019/.  The old path no longer exists, so the
    # snapshot below was silently skipped until this was pointed at the archive.
    findings_db = Path.home() / "apps" / "_retired" / "mac-collab-20261019" / "findings.db"
    if findings_db.is_file():
        if args.dry_run:
            print(f"would save findings-{DATE_STR}.db (findings.db SQLite snapshot)")
        else:
            try:
                import sqlite3

                db_dest = dest_dir / f"findings-{DATE_STR}.db"
                # The archive is frozen and read-only;  open it read-only, like every
                # other reader of that file does, so it is never checkpointed or rewritten.
                src_conn = sqlite3.connect(f"file:{findings_db}?mode=ro", uri=True)
                dst_conn = sqlite3.connect(db_dest)
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
                print(f"  saved {db_dest.name} (findings.db SQLite snapshot)")
            except Exception as e:
                print(f"WARN: findings.db gdrive backup failed: {e}", file=sys.stderr)

    if args.dry_run:
        print(f"would write {dest_dir.name}/BACKUP-NOTES.md")
    else:
        write_notes(dest_dir, backed_up, skipped)
    if not backed_up:
        print(
            "No repositories would be backed up."
            if args.dry_run
            else "No repositories were backed up.",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print(f"Would back up {len(backed_up)} repositories to {dest_dir}")
    else:
        print(f"Backed up {len(backed_up)} repositories to {dest_dir}")

    removed = prune_old_backups(gdrive, args.keep, dry_run=args.dry_run)
    if removed is None:
        # The zips are already in Drive, so a retention that could not run is
        # a warning, not a failure: the summary says the retention was
        # skipped and the exit status stays 0.
        print(RETENTION_SKIPPED)
        return 0
    verb = "would remove" if args.dry_run else "removed"
    print(f"Retention: {verb} {len(removed)} old backup folder(s), keeping the newest {args.keep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
