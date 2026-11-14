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
)


def _real_parents(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Symlink in installation path")


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
        if previous.get("schema") != 1 or set(previous.get("files", {})) != set(FILES):
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
        manifest = {
            "schema": 1,
            "source_commit": source_commit,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        (stage / "manifest.json").chmod(0o600)
        for root, dirs, files in os.walk(stage):
            os.chmod(root, 0o700)
        if destination.exists():
            backup = destination.with_name(destination.name + ".backup-" + uuid.uuid4().hex)
            os.rename(destination, backup)
        try:
            if backup is not None:
                for root, dirs, files in os.walk(backup):
                    os.chmod(root, 0o700)
            os.rename(stage, destination)
        except BaseException:
            if backup is not None:
                os.rename(backup, destination)
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
