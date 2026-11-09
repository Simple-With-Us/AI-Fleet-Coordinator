#!/usr/bin/env python3
"""Mirror fleet agent skills and rules into Google Drive.

Google Drive for desktop only syncs inside the CloudStorage mount.  Dotfolders
such as ~/.Gemini, ~/.cursor, ~/.claude, and ~/.grok cannot be added to native
sync directly.  This job mirrors the fleet-relevant subtrees into My Drive so
the desktop app backs them up automatically.

Layout under My Drive/fleet-agent-config/:
  gemini/skills/        <- ~/.Gemini/skills (or ~/.gemini/skills)
  cursor/skills/        <- ~/.cursor/skills
  cursor/skills-cursor/ <- ~/.cursor/skills-cursor
  cursor/rules/         <- ~/.cursor/rules
  claude/skills/        <- ~/.claude/skills
  grok/skills/          <- ~/.grok/skills

Also refreshes My Drive/fleet-skills/ from AI-Fleet-Coordinator docs/fleet-skills
(canonical git catalog + .zip packages for Claude app upload).

Usage:
  python3 scripts/sync-fleet-agent-config-to-gdrive.py
  python3 scripts/sync-fleet-agent-config-to-gdrive.py --list
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import sys
import time
from pathlib import Path

GDRIVE_CANDIDATES = [
    Path("/Users/jay/Google Drive/My Drive"),
    Path("/Users/jay/Library/CloudStorage/GoogleDrive-jaywedgeworth22@gmail.com/My Drive"),
]

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent

FLEET_COORDINATOR_CANDIDATES = [
    SCRIPT_DIR.parent,
    Path("/Users/jay/Code/AI-Fleet-Coordinator"),
    HOME / "Code" / "AI-Fleet-Coordinator",
]


def fleet_coordinator_root() -> Path | None:
    for root in FLEET_COORDINATOR_CANDIDATES:
        if (root / "docs" / "fleet-skills").is_dir():
            return root
    return None


def docs_fleet_skills_path() -> Path | None:
    root = fleet_coordinator_root()
    if root is None:
        return None
    return root / "docs" / "fleet-skills"


AGENT_CONFIG_ROOT = "fleet-agent-config"
FLEET_SKILLS_ROOT = "fleet-skills"

# First existing home-relative path wins for each Drive destination.
MIRROR_SPECS: list[tuple[str, list[str]]] = [
    ("gemini/skills", [".Gemini/skills", ".gemini/skills"]),
    ("cursor/skills", [".cursor/skills"]),
    ("cursor/skills-cursor", [".cursor/skills-cursor"]),
    ("cursor/rules", [".cursor/rules"]),
    ("claude/skills", [".claude/skills"]),
    ("grok/skills", [".grok/skills"]),
]


def find_gdrive() -> Path:
    for cand in GDRIVE_CANDIDATES:
        if cand.is_dir():
            return cand
    print("Google Drive directory not found on this machine.", file=sys.stderr)
    sys.exit(1)


def resolve_home_source(rel_paths: list[str]) -> Path | None:
    for rel in rel_paths:
        path = HOME / rel
        if path.is_dir():
            return path
    return None


def copy_one_file(src: Path, dest: Path, retries: int = 5) -> None:
    """Write via a sibling temp file, then replace.  Retry File Provider deadlocks.

    A Google Drive file that has never been opened is a dataless placeholder,
    and overwriting one in place is refused with EPERM: the File Provider
    owns the inode and will not let a rename or a plain write land on it.
    Creating a new file in the same folder works, so the fallback removes the
    placeholder and writes the file again as a new one.  EPERM is not
    retried: it is a permission state, not a transient lock, and retrying it
    only turns one refused write into five.
    """
    tmp = dest.with_name(f".{dest.name}.tmp-mirror")
    last: OSError | None = None
    for attempt in range(retries):
        try:
            shutil.copy2(src, tmp)
            try:
                os.replace(tmp, dest)
                return
            except OSError as replace_err:
                if replace_err.errno != errno.EPERM:
                    raise
                try:
                    shutil.copy2(src, dest)
                except OSError as direct_err:
                    if direct_err.errno != errno.EPERM:
                        raise
                    # The destination is a placeholder.  Take it away and
                    # write the file again as a new one.
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dest)
                    return
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return
        except OSError as exc:
            last = exc
            try:
                tmp.unlink()
            except OSError:
                pass
            if exc.errno in (errno.EDEADLK, errno.EAGAIN, errno.EBUSY) and attempt < retries - 1:
                time.sleep(0.25 * (attempt + 1))
                continue
            raise
    if last is not None:
        raise last


def copy_mirror(src: Path, dest: Path) -> None:
    """Mirror src -> dest without rsync.

    Google Drive for desktop is a File Provider.  `rsync -a --delete` against
    that mount fails under launchd with `open: Operation not permitted` even
    though the same job can write zip files into a sibling folder via Python.
    Copy in-process so the backup job does not inherit rsync's open() mode.
    """
    dest.mkdir(parents=True, exist_ok=True)
    wanted: set[Path] = set()
    errors: list[str] = []

    for sfile in src.rglob("*"):
        if not sfile.is_file() or sfile.name == ".DS_Store":
            continue
        rel = sfile.relative_to(src)
        dfile = dest / rel
        wanted.add(rel)
        try:
            dfile.parent.mkdir(parents=True, exist_ok=True)
            # File Provider mtimes are not trustworthy; size match means
            # the dest already has this object and an in-place overwrite
            # can raise EDEADLK while Drive is syncing it.
            if dfile.is_file() and dfile.stat().st_size == sfile.stat().st_size:
                continue
            copy_one_file(sfile, dfile)
        except OSError as exc:
            errors.append(f"{sfile} -> {dfile}: {exc}")

    for dfile in dest.rglob("*"):
        if not dfile.is_file():
            continue
        if dfile.name == ".DS_Store" or dfile.name.endswith(".tmp-mirror"):
            try:
                dfile.unlink()
            except OSError:
                pass
            continue
        rel = dfile.relative_to(dest)
        if rel not in wanted:
            try:
                dfile.unlink()
            except OSError as exc:
                errors.append(f"unlink {dfile}: {exc}")

    if errors:
        for line in errors:
            print(f"mirror error: {line}", file=sys.stderr)
        raise RuntimeError(f"{len(errors)} file(s) failed to mirror into {dest}")


def rsync_mirror(src: Path, dest: Path) -> None:
    # Kept as the call-site name so older wrappers keep working.
    copy_mirror(src, dest)


def mirror_agent_config(gdrive: Path) -> tuple[list[str], list[str]]:
    """Mirror the agent dotfolders into Drive.

    Returns (mirrored, failures) instead of raising.  The mirror is the
    optional half of the backup job: the repository zips are the part the
    owner depends on, and a File Provider that refuses small writes was
    killing the whole run after the zips were already safely in Drive.  A
    failed subtree is reported loudly and the rest of the job continues.
    """
    root = gdrive / AGENT_CONFIG_ROOT
    root.mkdir(parents=True, exist_ok=True)
    mirrored: list[str] = []

    failures: list[str] = []
    for dest_rel, home_candidates in MIRROR_SPECS:
        src = resolve_home_source(home_candidates)
        if src is None:
            print(f"skip {dest_rel} (no source under {home_candidates})")
            continue
        dest = root / dest_rel
        print(f"mirror {src} -> {dest}")
        try:
            rsync_mirror(src, dest)
            mirrored.append(dest_rel)
        except RuntimeError as exc:
            print(f"mirror failed {dest_rel}: {exc}", file=sys.stderr)
            failures.append(dest_rel)

    readme = root / "README.md"
    body = "\n".join(
        [
            "# Fleet agent config mirror",
            "",
            "Mirrored from Mac home dotfolders by",
            "`scripts/sync-fleet-agent-config-to-gdrive.py`",
            "(launchd `com.jay.fleet-gdrive-backup`, daily 06:00).",
            "",
            "Google Drive desktop cannot sync ~/.Gemini / ~/.cursor /",
            "~/.claude / ~/.grok directly.  This folder is the backup copy.",
            "",
            "## Seats mirrored",
            "- `gemini/skills` — Antigravity / Gemini",
            "- `cursor/skills`, `cursor/skills-cursor`, `cursor/rules` — Cursor",
            "- `claude/skills` — Claude Code",
            "- `grok/skills` — Grok",
            "",
            "Canonical fleet skill source in git:",
            "`AI-Fleet-Coordinator/docs/fleet-skills/`.",
            "Upload packs for Claude.app also land in sibling `fleet-skills/`.",
            "",
        ]
    )
    staging = Path("/tmp/fleet-agent-config-README.md")
    staging.write_text(body)
    if not (readme.is_file() and readme.stat().st_size == staging.stat().st_size):
        try:
            copy_one_file(staging, readme)
        except OSError as exc:
            print(f"mirror error: {readme}: {exc}", file=sys.stderr)
    return mirrored, failures


def mirror_fleet_skills_catalog(gdrive: Path) -> bool:
    """Refresh the canonical skill catalog in Drive.

    Returns False when the catalog could not be mirrored, so the caller can
    report it instead of losing the whole run to it.
    """
    docs_fleet_skills = docs_fleet_skills_path()
    if docs_fleet_skills is None:
        print(f"skip {FLEET_SKILLS_ROOT} (AI-Fleet-Coordinator docs/fleet-skills not found)")
        return False
    dest = gdrive / FLEET_SKILLS_ROOT
    print(f"mirror {docs_fleet_skills} -> {dest}")
    try:
        rsync_mirror(docs_fleet_skills, dest)
    except RuntimeError as exc:
        print(f"mirror failed {FLEET_SKILLS_ROOT}: {exc}", file=sys.stderr)
        return False
    return True


def list_planned(gdrive: Path) -> None:
    print(f"Google Drive: {gdrive}")
    print(f"Agent config root: {gdrive / AGENT_CONFIG_ROOT}")
    print(f"Fleet skills catalog: {gdrive / FLEET_SKILLS_ROOT}")
    for dest_rel, home_candidates in MIRROR_SPECS:
        src = resolve_home_source(home_candidates)
        mark = str(src) if src else "missing"
        print(f"  {dest_rel}\t{mark}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print planned mirrors and exit")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any subtree fails to mirror (the default is to report and carry on)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gdrive = find_gdrive()
    if args.list:
        list_planned(gdrive)
        return 0

    mirrored, failures = mirror_agent_config(gdrive)
    if not mirror_fleet_skills_catalog(gdrive):
        failures.append(FLEET_SKILLS_ROOT)

    if not mirrored and docs_fleet_skills_path() is None:
        print("Nothing mirrored.", file=sys.stderr)
        return 1

    print(
        f"Mirrored {len(mirrored)} agent subtrees to {gdrive / AGENT_CONFIG_ROOT}; "
        f"refreshed {gdrive / FLEET_SKILLS_ROOT}"
    )

    if failures:
        # A File Provider that refuses small writes is not a reason to fail
        # the whole job.  The repository zips are the part the owner depends
        # on, and they were already safely in Drive before this ran.  The
        # failure is reported loudly, and the job carries on.
        print(
            "WARNING: these subtrees did not mirror: " + ", ".join(failures),
            file=sys.stderr,
        )
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
