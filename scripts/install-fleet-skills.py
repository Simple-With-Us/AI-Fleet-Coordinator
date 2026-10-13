#!/usr/bin/env python3
"""Install fleet skills to every agent platform, specialized per seat.

Canonical source: docs/fleet-skills (Monet / Claude.app pack).
Home dirs, repo-tracked copies, and docs/fleet-skills/by-seat/<seat>/ get
rewritten identity.  Skills in NEVER_INSTALL (ios-ship) are omitted, not
copied as a Monet-voiced leftover.  Retired seats (Monet, Renoir, DSH, Kimi)
have write_home=False: catalog/by-seat only, never a tool home (no
~/Desktop/fleet-skills, ~/.deepseek, ~/.kimi, ~/.renoir).  Clutch renders to
~/.clutch/dsh/skills, the DSH engine's own skill root (it outranks the shared
~/.agents/skills, which no installer here writes).
The shared ~/.claude/skills home renders as CLAUDE, the only Claude seat.

Usage:
    python3 scripts/install-fleet-skills.py              # repo trees AND tool homes
    python3 scripts/install-fleet-skills.py --repo-only  # repo trees only

--repo-only re-renders everything tracked or catalogued inside this checkout
(skills/, docs/fleet-skills zips, .claude/.cursor/.grok skills, by-seat) and
writes nothing under the home directory: no tool-home skill dirs and no fx
scan-tree description folding.  Use it from a lane after editing the canonical
pack, so a session started in this repo loads the same text the pack says.
"""

from __future__ import annotations

import argparse
import os
import shutil
import zipfile

from fleet_skill_identity import (
    NEVER_INSTALL,
    catalog_seats,
    catalog_skill_names,
    platform_installs,
    repo_platform_copies,
    rewrite_skill_tree,
    skill_allowed_for_seat,
    specialize_from_monet,
    specialize_universal,
    tool_home_exists,
)

FX_SCAN_ROOTS = (
    "~/.fx/skills",
    "~/.config/opencode/skills",
    "~/.codex/skills",
    "~/.claude/skills",
    "~/.agents/skills",
    "~/.claw/skills",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_SKILLS = os.path.join(REPO_ROOT, "docs", "fleet-skills")
ROOT_SKILLS = os.path.join(REPO_ROOT, "skills")
BY_SEAT = os.path.join(DOCS_SKILLS, "by-seat")


def _write_skill(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def _zip_matches(zip_path: str, arcname: str, md_path: str) -> bool:
    """True when zip_path already holds exactly arcname with md_path's bytes."""
    if not os.path.isfile(zip_path):
        return False
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if zf.namelist() != [arcname]:
                return False
            with open(md_path, "rb") as f:
                return zf.read(arcname) == f.read()
    except (OSError, zipfile.BadZipFile):
        return False


def _zip_skill(zip_path: str, skill_name: str, md_path: str) -> bool:
    """Write the pack zip.  Skip an identical one: a rewrite only changes the
    stored mtime, which churns tracked binaries for no content change."""
    arcname = os.path.join(skill_name, "SKILL.md")
    if _zip_matches(zip_path, arcname, md_path):
        return False
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(md_path, arcname=arcname)
    return True


def _purge_retired(dest_base: str) -> None:
    if not dest_base or not os.path.isdir(dest_base):
        return
    for name in NEVER_INSTALL:
        path = os.path.join(dest_base, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        zip_path = os.path.join(dest_base, f"{name}.zip")
        if os.path.isfile(zip_path):
            os.remove(zip_path)


def _install_skill_set(
    dest_base: str, sources: dict[str, str], seat, *, zip_each: bool = False
) -> list[str]:
    written: list[str] = []
    os.makedirs(dest_base, exist_ok=True)
    _purge_retired(dest_base)
    for name, monet_src in sources.items():
        if not skill_allowed_for_seat(name, seat):
            leftover = os.path.join(dest_base, name)
            if os.path.isdir(leftover):
                shutil.rmtree(leftover)
            leftover_zip = os.path.join(dest_base, f"{name}.zip")
            if os.path.isfile(leftover_zip):
                os.remove(leftover_zip)
            continue
        rendered = specialize_from_monet(monet_src, seat, skill_name=name)
        md_path = os.path.join(dest_base, name, "SKILL.md")
        _write_skill(md_path, rendered)
        written.append(name)
        if zip_each:
            _zip_skill(os.path.join(dest_base, f"{name}.zip"), name, md_path)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo-only",
        action="store_true",
        help="re-render only trees inside this checkout; never write the home directory",
    )
    args = parser.parse_args(argv)

    skill_names = catalog_skill_names(DOCS_SKILLS)
    print(f"Found {len(skill_names)} installable fleet skills in {DOCS_SKILLS}:")
    for name in skill_names:
        print(f"  - {name}")
    skipped = sorted(NEVER_INSTALL)
    if skipped:
        print("Omitted from every seat (never install):")
        for name in skipped:
            print(f"  - {name}")

    os.makedirs(ROOT_SKILLS, exist_ok=True)
    os.makedirs(BY_SEAT, exist_ok=True)
    _purge_retired(DOCS_SKILLS)
    _purge_retired(ROOT_SKILLS)

    sources: dict[str, str] = {}
    for name in skill_names:
        src_md = os.path.join(DOCS_SKILLS, name, "SKILL.md")
        if not os.path.isfile(src_md):
            continue
        with open(src_md, encoding="utf-8") as f:
            sources[name] = f.read()
        root_dest = os.path.join(ROOT_SKILLS, name)
        os.makedirs(root_dest, exist_ok=True)
        universal = specialize_universal(sources[name], skill_name=name)
        _write_skill(os.path.join(root_dest, "SKILL.md"), universal)
        _zip_skill(os.path.join(DOCS_SKILLS, f"{name}.zip"), name, src_md)

    if args.repo_only:
        print("\nHome-dir installs: skipped (--repo-only)")
    else:
        print("\nHome-dir installs:")
        for dest, seat in platform_installs():
            if not tool_home_exists(dest):
                print(f"  {seat.tag:12}  skip (no tool home)  {dest}")
                continue
            written = _install_skill_set(dest, sources, seat)
            print(f"  {seat.tag:12}  {dest}  ({len(written)} skills)")

    print("\nRepo-tracked platform copies:")
    for dest, seat in repo_platform_copies(REPO_ROOT):
        written = _install_skill_set(dest, sources, seat)
        print(f"  {seat.tag:12}  {dest}  ({len(written)} skills)")

    print("\nUpload packs: docs/fleet-skills/by-seat/<seat>/")
    for seat in catalog_seats():
        key = seat.seat_key or seat.tag.lower()
        seat_root = os.path.join(BY_SEAT, key)
        written = _install_skill_set(seat_root, sources, seat, zip_each=True)
        print(f"  {seat.tag:12}  {seat.seat_key}  ({len(written)} skills)")

    if args.repo_only:
        return

    print("\nFolded quoted descriptions in fx-scanned trees:")
    for raw in FX_SCAN_ROOTS:
        path = os.path.expanduser(raw)
        n = rewrite_skill_tree(path)
        print(f"  {path}: {n} file(s)")


if __name__ == "__main__":
    main()
