#!/usr/bin/env python3
"""Archive fleet repositories into per-repo zips, resilient to a failed target.

Extracted from .github/workflows/backup-repos.yml so the loop is unit-testable
(AFC-682).  Previously one repo that failed to clone made the step exit before
the upload ran, discarding every archive that did succeed:  a partial failure
silently erased the rest of that day's offsite.

Behavior contract:
  * every repo is attempted;  one failure never skips the others;
  * archives that succeeded are kept on disk for the upload step;
  * BACKUP-SUMMARY.txt records the succeeded/failed lists and counts, and the
    artifact name gains a -PARTIAL suffix (via BACKUP_ARTIFACT_SUFFIX in
    $GITHUB_ENV) when any target failed;
  * the process still exits non-zero on any failure:  partial is never green;
  * the archive count guard stays:  zips on disk must equal the repo-list
    count minus the recorded failures.

The clone command defaults to `gh repo clone` (GH_TOKEN supplies auth, kept
out of argv).  Tests override it with BACKUP_REPOS_CLONE_CMD, a shell-like
template containing the literal placeholders {repo} and {dest}.
"""

import argparse
import fnmatch
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

# Exclusions mirror the former inline `zip -x` list:  dependency/build noise
# and anything shaped like a secret never enters an offsite archive.
EXCLUDE_PATTERNS = [
    "*/node_modules/*",
    "*/.git/*",
    "*/dist/*",
    "*/build/*",
    ".env*",
    "*/.env*",
    "*.key",
    "*/*.key",
    "*.pem",
    "*/*.pem",
    "*.p12",
    "*/*.p12",
    "*.pfx",
    "*/*.pfx",
    "id_*",
    "*/id_*",
    "*credential*",
    "*/*credential*",
    "*service-account*",
    "*/*service-account*",
]

DEFAULT_CLONE_CMD = "gh repo clone Simple-With-Us/{repo} {dest}"
SUMMARY_NAME = "BACKUP-SUMMARY.txt"


def is_excluded(relpath):
    """True when relpath (posix, relative to the clone root) is excluded.

    A leading `*/` also matches the clone root itself, so top-level
    node_modules/, .git/, dist/ and build/ are excluded, not only nested
    ones:  the archive must never carry VCS internals or dependency trees.
    """
    for pat in EXCLUDE_PATTERNS:
        if fnmatch.fnmatchcase(relpath, pat):
            return True
        if pat.startswith("*/") and fnmatch.fnmatchcase(relpath, pat[2:]):
            return True
    return False


def clone_cmd(repo, dest):
    template = os.environ.get("BACKUP_REPOS_CLONE_CMD", DEFAULT_CLONE_CMD)
    return [part.format(repo=repo, dest=str(dest)) for part in shlex.split(template)]


def zip_clone(clone_root, zip_path):
    """Zip clone_root into zip_path, applying EXCLUDE_PATTERNS."""
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(clone_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(clone_root).as_posix()
            if is_excluded(rel):
                continue
            zf.write(path, rel)
            count += 1
    return count


def append_env_file(path, text):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def write_summary(outdir, date_str, succeeded, failed, zip_count):
    status = "COMPLETE" if not failed else "PARTIAL"
    lines = [
        f"Fleet repo backup {date_str}: {status}",
        f"generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"archives: {zip_count}",
        f"succeeded ({len(succeeded)}): {', '.join(succeeded) or '-'}",
        f"failed ({len(failed)}): {', '.join(failed) or '-'}",
    ]
    if failed:
        lines.append(
            "WARNING: this artifact set is incomplete; "
            "the failed repositories have no archive in this run."
        )
    (outdir / SUMMARY_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return status


def run_backup(repo_list_path, outdir, date_str):
    repos = [line.strip() for line in Path(repo_list_path).read_text().splitlines() if line.strip()]
    outdir.mkdir(parents=True, exist_ok=True)
    succeeded, failed = [], []
    for repo in repos:
        print("=" * 40)
        print(f"Cloning Simple-With-Us/{repo}")
        clone_dest = outdir / repo
        proc = subprocess.run(clone_cmd(repo, clone_dest))
        if proc.returncode != 0:
            print(f"::error::could not clone Simple-With-Us/{repo}")
            failed.append(repo)
            continue
        zip_path = outdir / f"{repo}-backup-{date_str}.zip"
        zip_clone(clone_dest, zip_path)
        shutil.rmtree(clone_dest)
        print(f"Archive created: {zip_path.name}")
        succeeded.append(repo)

    zips = sorted(outdir.glob("*.zip"))
    status = write_summary(outdir, date_str, succeeded, failed, len(zips))
    suffix = "-PARTIAL" if failed else ""
    append_env_file(os.environ.get("GITHUB_ENV"), f"BACKUP_ARTIFACT_SUFFIX={suffix}\n")
    append_env_file(
        os.environ.get("GITHUB_STEP_SUMMARY"),
        f"## Fleet repo backup: {status}\n\n"
        f"- archives: {len(zips)}\n"
        f"- failed: {', '.join(failed) or 'none'}\n",
    )

    # The count guard stays:  a silent drop must read as an error, not as a
    # complete offsite.
    expected = len(repos)
    if len(zips) != expected - len(failed):
        print(
            f"::error::expected {expected - len(failed)} archives "
            f"({expected} repos, {len(failed)} failed), found {len(zips)}"
        )
        return 1
    if failed:
        print(f"::error::{len(failed)} repositories failed to clone: {', '.join(failed)}")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-list", required=True, help="file with one repo name per line")
    parser.add_argument("--outdir", required=True, help="output directory")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD stamp")
    args = parser.parse_args(argv)
    return run_backup(Path(args.repo_list), Path(args.outdir), args.date)


if __name__ == "__main__":
    sys.exit(main())
