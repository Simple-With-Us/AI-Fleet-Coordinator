#!/usr/bin/env python3
"""Build a finite code-only relay artifact from one immutable local Git commit.

This prepares public source; it installs nothing and reads no host configuration or store.
The monorepo layout keeps the relay's canonical reader at its fixed package-relative path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
MODULES = (
    "__init__",
    "app_tokens",
    "assertion",
    "audit",
    "deploy_entry_prep",
    "deploy_guard",
    "deploy_host",
    "deploy_journal",
    "deploy_manifest",
    "deploy_proof",
    "deploy_recovery",
    "deploy_runner",
    "deploy_supervisor",
    "deploy_transaction",
    "echo_review_read",
    "fleet_admin_read",
    "git_read",
    "github_client",
    "mcp_server",
    "outcomes",
    "patch_policy",
    "patch_secretscan",
    "policy",
    "policy_input",
    "provenance_patch",
    "provider_scan",
    "reviewed_fixtures",
    "reviewed_preservation",
    "same_path_reuse",
    "seat_add_policy",
    "secret_scan",
    "server",
    "shared_bearer",
    "typed_disarm",
    "typed_gateway",
    "workflow_verbs",
)
FILES = {"git-relay/bin/git-relay": "100755", "scripts/fleet_infisical.py": "100644"}
FILES.update({f"git-relay/git_relay/{name}.py": "100644" for name in MODULES})
MANIFEST = "relay-runtime.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30


class ArtifactError(Exception):
    """Fixed public-source preparation failure; never a host receipt."""


def _git(*arguments: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPOSITORY), *arguments],
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise ArtifactError("source-object-timeout") from None
    except (OSError, subprocess.CalledProcessError):
        raise ArtifactError("source-object-unavailable") from None


def _source_files(commit: str) -> tuple[dict, dict[str, bytes]]:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ArtifactError("full-source-commit-required")
    if _git("cat-file", "-t", commit).strip() != b"commit":
        raise ArtifactError("source-commit-required")
    rows, contents = {}, {}
    total = 0
    for path, mode in sorted(FILES.items()):
        entry = _git("ls-tree", "-z", commit, "--", path)
        fields = entry.removesuffix(b"\0").split(b"\t")
        if len(fields) != 2 or fields[1] != path.encode():
            raise ArtifactError("source-inventory-refused")
        header = fields[0].split(b" ")
        if len(header) != 3 or header[:2] != [mode.encode(), b"blob"]:
            raise ArtifactError("source-mode-refused")
        blob = header[2].decode("ascii")
        if not re.fullmatch(r"[0-9a-f]{40}", blob):
            raise ArtifactError("source-object-refused")
        size = int(_git("cat-file", "-s", blob))
        total += size
        if not 0 < size <= MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ArtifactError("source-size-refused")
        data = _git("cat-file", "blob", blob)
        if len(data) != size:
            raise ArtifactError("source-size-refused")
        digest = hashlib.sha1(b"blob " + str(size).encode() + b"\0" + data).hexdigest()
        if digest != blob:
            raise ArtifactError("source-object-refused")
        contents[path] = data
        rows[path] = dict(blob=blob, sha256=hashlib.sha256(data).hexdigest(), size=size, mode=mode)
    return dict(schemaVersion=1, sourceCommit=commit, files=rows), contents


def _new_destination(raw: str | Path) -> Path:
    text = str(raw)
    if not os.path.isabs(text) or any(part in (".", "..") for part in text.split(os.sep)):
        raise ArtifactError("absolute-new-destination-required")
    destination = Path(text)
    # Existing parents must be real directories; the builder never creates or follows a selector.
    try:
        for parent in reversed(destination.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ArtifactError("destination-parent-refused")
        if os.path.lexists(destination):
            raise ArtifactError("destination-already-exists")
    except OSError:
        raise ArtifactError("destination-parent-refused") from None
    return destination


def verify_artifact(destination: Path, manifest: dict) -> None:
    """Verify the complete finite payload, modes and content hashes before reporting success."""
    if (
        set(manifest) != {"schemaVersion", "sourceCommit", "files"}
        or manifest["schemaVersion"] != 1
        or not re.fullmatch(r"[0-9a-f]{40}", manifest["sourceCommit"])
        or set(manifest["files"]) != set(FILES)
    ):
        raise ArtifactError("artifact-inventory-refused")
    found = set()
    for path in destination.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactError("artifact-mode-refused")
        if stat.S_ISREG(info.st_mode):
            found.add(path.relative_to(destination).as_posix())
        elif not stat.S_ISDIR(info.st_mode):
            raise ArtifactError("artifact-mode-refused")
    if found != set(FILES) | {MANIFEST}:
        raise ArtifactError("artifact-inventory-refused")
    for path, row in manifest["files"].items():
        target = destination / path
        info = target.lstat()
        if (
            row["mode"] != FILES[path]
            or stat.S_IMODE(info.st_mode) != int(row["mode"], 8) & 0o777
            or info.st_size != row["size"]
            or not 0 < info.st_size <= MAX_FILE_BYTES
        ):
            raise ArtifactError("artifact-mode-refused")
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ArtifactError("artifact-content-refused")
    if json.loads((destination / MANIFEST).read_bytes()) != manifest:
        raise ArtifactError("artifact-manifest-refused")


def build_runtime(commit: str, raw_destination: str | Path) -> dict:
    manifest, contents = _source_files(commit)
    destination = _new_destination(raw_destination)
    created = None
    try:
        destination.mkdir(mode=0o700)
        info = destination.lstat()
        created = (info.st_dev, info.st_ino)
        for path, data in contents.items():
            target = destination / path
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
            target.chmod(int(FILES[path], 8) & 0o777)
        with (destination / MANIFEST).open("xb") as stream:
            stream.write((json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
        verify_artifact(destination, manifest)
        return manifest
    except BaseException:
        if created is not None:
            try:
                info = destination.lstat()
                if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == created:
                    shutil.rmtree(destination)
            except OSError:
                pass
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: build-runtime.py FULL_SOURCE_COMMIT ABSOLUTE_NEW_DESTINATION", file=sys.stderr
        )
        return 2
    try:
        manifest = build_runtime(argv[0], argv[1])
    except (ArtifactError, OSError, ValueError, KeyError, UnicodeError):
        print("relay-runtime: code artifact preparation refused", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"sourceCommit": manifest["sourceCommit"], "codeFiles": len(FILES), "verified": True}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
