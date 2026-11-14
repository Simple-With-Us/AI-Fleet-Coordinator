#!/usr/bin/env python3
"""Install the identical source-only Code mirror dependency closure on either host."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

FILES = (
    "code-main-keeper.sh",
    "fleet_lanes/__init__.py",
    "fleet_lanes/code_main_sync.py",
    "fleet_lanes/code_mirror_credentials.py",
    "fleet_lanes/placement.py",
    "fleet_lanes/placement-exceptions.json",
    "fleet_knobs/__init__.py",
    "fleet_knobs/knobs.py",
    "fleet_knobs/lkg.py",
    "fleet_knobs/registry.py",
    "fleet_knobs/registry.json",
    "fleet_knobs/source.py",
    "fleet_knobs/values.py",
    "fleet_secrets/__init__.py",
    "fleet_secrets/core.py",
    "collect-repo-documents.py",
    "export-repo-git-source.py",
    "remote_git_source.py",
    "repo_catalog.py",
    "fleet_docs/__init__.py",
    "fleet_docs/__main__.py",
    "fleet_docs/pipeline.py",
    "fleet_docs/snapshot.py",
)
# The first shared runtime shipped this exact closure before Docs was added.
LEGACY_FILES = FILES[:15]


def _real_parents(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Symlink in installation path")


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _matches_payload(root, expected):
    """Do not remove a published candidate if an unrelated writer changed it."""
    if root.is_symlink() or not root.is_dir():
        return False
    observed = set()
    allowed_dirs = {parent.as_posix() for name in expected for parent in Path(name).parents}
    for folder, dirs, files in os.walk(root, followlinks=False):
        metadata = Path(folder).lstat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            return False
        for name in dirs:
            path = Path(folder) / name
            if path.is_symlink() or path.relative_to(root).as_posix() not in allowed_dirs:
                return False
        for name in files:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if relative not in expected:
                return False
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                mode = 0o700 if relative == "scripts/code-main-keeper.sh" else 0o600
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size != len(expected[relative]) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode:
                    return False
                if stream.read(len(expected[relative]) + 1) != expected[relative]:
                    return False
            observed.add(relative)
    return observed == set(expected)


def install(source, destination, source_commit):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Source commit must be a full Git commit identifier")
    _real_parents(destination)
    payload = {}
    for name in FILES:
        path = source / "scripts" / name
        _real_parents(path)
        if not path.is_file():
            raise ValueError("Required runtime dependency missing: " + name)
        payload[name] = path.read_bytes()
    if destination.exists():
        allowed = {"manifest.json"} | {"scripts/" + name for name in FILES}
        allowed_dirs = {
            "scripts",
            "scripts/fleet_lanes",
            "scripts/fleet_knobs",
            "scripts/fleet_secrets",
            "scripts/fleet_docs",
        }
        for root, dirs, files in os.walk(destination, followlinks=False):
            for name in dirs + files:
                path = Path(root) / name
                mode = path.lstat().st_mode
                relative = path.relative_to(destination).as_posix()
                if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise ValueError("Unsafe existing bundle entry")
                if mode & 0o077:
                    raise ValueError("Existing bundle entry is not private")
                if relative not in (allowed_dirs if stat.S_ISDIR(mode) else allowed):
                    raise ValueError("Unknown existing bundle entry: " + relative)
        if not (destination / "manifest.json").is_file():
            raise ValueError("Existing bundle has no source manifest")
        previous = json.loads((destination / "manifest.json").read_text())
        if previous.get("schema") != 1 or set(previous.get("files", {})) not in (
            set(FILES),
            set(LEGACY_FILES),
        ):
            raise ValueError("Existing bundle manifest is not recognized")
        for name, digest in previous["files"].items():
            target = destination / "scripts" / name
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("Existing bundle differs from its source manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".code-mirrors-stage-", dir=destination.parent))
    backup = None
    try:
        for name, data in payload.items():
            target = stage / "scripts" / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            target.chmod(0o700 if name == "code-main-keeper.sh" else 0o600)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
        manifest = {
            "schema": 1,
            "source_commit": source_commit,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
        }
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        (stage / "manifest.json").write_bytes(manifest_bytes)
        (stage / "manifest.json").chmod(0o600)
        with (stage / "manifest.json").open("rb") as stream:
            os.fsync(stream.fileno())
        for root, dirs, files in os.walk(stage):
            os.chmod(root, 0o700)
        for root, dirs, files in os.walk(stage, topdown=False):
            _fsync_directory(root)
        _fsync_directory(destination.parent)
        published = False
        try:
            if destination.exists():
                backup = destination.with_name(destination.name + ".backup-" + uuid.uuid4().hex)
                os.rename(destination, backup)
                _fsync_directory(destination.parent)
            if backup is not None:
                for root, dirs, files in os.walk(backup):
                    os.chmod(root, 0o700)
            os.rename(stage, destination)
            published = True
            _fsync_directory(destination.parent)
        except BaseException:
            if published:
                expected = {"scripts/" + name: data for name, data in payload.items()}
                expected["manifest.json"] = manifest_bytes
                if not _matches_payload(destination, expected):
                    raise ValueError("Published candidate changed; active and backup preserved")
                os.rename(destination, stage)
            if backup is not None and backup.exists():
                os.rename(backup, destination)
            _fsync_directory(destination.parent)
            raise
        return {
            "destination": str(destination),
            "backup": str(backup) if backup else None,
            **manifest,
        }
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--destination", type=Path, default=Path.home() / ".local/share/code-mirrors"
    )
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            install(args.source_root, args.destination, args.source_commit),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
