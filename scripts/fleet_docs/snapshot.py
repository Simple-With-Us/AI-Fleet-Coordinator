"""Private generated-library snapshots with explicit ownership and atomic promotion."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

MAX_FILES = 100_000
MAX_BYTES = 2 * 1024**3
MAX_FILE_BYTES = 64 * 1024**2
MAX_HEADER = 32 * 1024**2
HEX = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """A fixed reason token; document and provider contents never enter diagnostics."""


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def collector():
    name = "_fleet_docs_collector"
    if name not in sys.modules:
        source = Path(__file__).resolve().parents[1] / "collect-repo-documents.py"
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def relative(value):
    require(
        isinstance(value, str) and value and "\0" not in value and "\\" not in value,
        "unsafe-snapshot-path",
    )
    path = PurePosixPath(value)
    require(
        path.parts
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts),
        "unsafe-snapshot-path",
    )
    return path


@contextmanager
def anchored(path, *, directory=False):
    """Open every component relative to a pinned directory descriptor without following links."""
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(path.anchor, flags)
    leaf = None
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        if len(path.parts) == 1:
            leaf = os.dup(current)
        else:
            leaf = os.open(
                path.name,
                flags if directory else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=current,
            )
        yield leaf
    except OSError:
        raise Refused("unsafe-library-path") from None
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(current)


def create_file(root, name):
    parts = relative(name).parts
    with anchored(root, directory=True) as root_fd:
        current = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
                info = os.fstat(current)
                require(
                    info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                    "unsafe-stage-directory",
                )
            return os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=current,
            )
        finally:
            os.close(current)


def fsync_directory(path):
    with anchored(path, directory=True) as fd:
        collector()._fsync_directory(path, fd=fd)


def private(path, directory=False):
    with anchored(path, directory=directory) as fd:
        info = os.fstat(fd)
    require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
        "unsafe-library-entry",
    )
    return info


def read_file(path):
    with anchored(path) as fd, os.fdopen(os.dup(fd), "rb") as source:
        before = os.fstat(source.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.getuid()
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_size <= MAX_FILE_BYTES,
            "unsafe-library-entry",
        )
        data = source.read(MAX_FILE_BYTES + 1)
        after = os.fstat(source.fileno())
    require(
        len(data) == before.st_size
        and (before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_ino, after.st_size, after.st_mtime_ns),
        "library-changed-during-read",
    )
    return data


def inventory(root):
    private(root, directory=True)
    require(not (root / ".collector-transaction").exists(), "collector-transaction-pending")
    files, total = {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            private(Path(directory) / name, directory=True)
        for name in names:
            path = Path(directory) / name
            data = read_file(path)
            key = path.relative_to(root).as_posix()
            relative(key)
            files[key] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            total += len(data)
            require(len(files) <= MAX_FILES and total <= MAX_BYTES, "snapshot-size-bound")
    return {
        "schema": 1,
        "files": files,
        "sha256": hashlib.sha256(canonical(files)).hexdigest(),
        "bytes": total,
    }


def validate_generated(root):
    """Recognize every existing file before allowing any replacement or deletion."""
    current = inventory(root)
    c = collector()
    expected = set()
    repositories = []
    if ".collector.lock" in current["files"]:
        require(read_file(root / ".collector.lock") == b"", "collector-lock-has-content")
        expected.add(".collector.lock")
    for repo_root in sorted(root.iterdir()):
        if not repo_root.is_dir():
            continue
        manifest = c.parse_existing_manifest(read_file(repo_root / "manifest.json"), repo_root.name)
        require(bool(manifest), "unknown-library-directory")
        owner, name = manifest["owner_repo"].split("/", 1)
        info = c.RepoInfo(Path("."), owner, name, manifest["source_ref"], manifest["source_commit"])
        repositories.append((info, manifest))
        expected.add(repo_root.name + "/manifest.json")
        for key, record in manifest.get("files", {}).items():
            relative(key)
            require(
                key.startswith("documents/")
                and isinstance(record, dict)
                and HEX.fullmatch(record.get("sha256", "")),
                "unrecognized-document-record",
            )
            require(
                current["files"].get(repo_root.name + "/" + key, {}).get("sha256") == record["sha256"],
                "generated-document-edited",
            )
            expected.add(repo_root.name + "/" + key)
        catalog = None
        if manifest.get("collection_policy_id"):
            data = read_file(repo_root / "catalog.json")
            require(
                hashlib.sha256(data).hexdigest() == manifest.get("catalog_sha256"),
                "generated-catalog-edited",
            )
            catalog = json.loads(data)
            require(
                catalog.get("source_commit") == manifest["source_commit"]
                and catalog.get("owner_repo") == manifest["owner_repo"],
                "catalog-provenance-mismatch",
            )
            expected.add(repo_root.name + "/catalog.json")
        readme = (
            c._repo_readme(info, manifest, catalog)
            if catalog is not None
            else c._legacy_repo_readme(info, manifest)
        )
        require(read_file(repo_root / "README.md") == readme, "generated-readme-edited")
        expected.add(repo_root.name + "/README.md")
        reports = repo_root / "reports"
        require(reports.is_dir(), "version-history-missing")
        for version in reports.iterdir():
            require(version.is_dir(), "unknown-version-entry")
            past = c.parse_existing_manifest(read_file(version / "manifest.json"), repo_root.name)
            require(
                past and c._manifest_version_key(past) == version.name,
                "version-provenance-mismatch",
            )
            prefix = repo_root.name + "/reports/" + version.name + "/"
            expected.add(prefix + "manifest.json")
            past_owner, past_name = past["owner_repo"].split("/", 1)
            past_info = c.RepoInfo(
                Path("."), past_owner, past_name, past["source_ref"], past["source_commit"]
            )
            readme = (
                c._version_readme(past_info, past)
                if past.get("collection_policy_id")
                else c._legacy_version_readme(past_info, past)
            )
            require(read_file(version / "README.md") == readme, "version-readme-edited")
            expected.add(prefix + "README.md")
            if past.get("collection_policy_id"):
                data = read_file(version / "catalog.json")
                require(
                    hashlib.sha256(data).hexdigest() == past.get("catalog_sha256"),
                    "version-catalog-edited",
                )
                expected.add(prefix + "catalog.json")
        version = reports / c._manifest_version_key(manifest)
        require(
            read_file(version / "manifest.json") == read_file(repo_root / "manifest.json"),
            "current-version-mismatch",
        )
    require(repositories, "empty-or-unrecognized-library")
    require(
        read_file(root / "README.md") == c._root_readme(repositories),
        "generated-root-readme-edited",
    )
    expected.add("README.md")
    require(expected == set(current["files"]), "unknown-or-missing-library-files")
    return current


@contextmanager
def receiver_lock(root):
    require(
        root.name == "Generated" and root.parent.is_dir() and not root.parent.is_symlink(),
        "unsafe-generated-root",
    )
    parent = root.parent.lstat()
    require(parent.st_uid == os.getuid() and not parent.st_mode & 0o022, "unsafe-generated-parent")
    path = root.parent / ".generated-docs-sync.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "unsafe-receiver-lock",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("receiver-already-running") from None
        yield
    finally:
        os.close(fd)


def validate_manifest(value):
    require(
        isinstance(value, dict)
        and value.get("schema") == 1
        and isinstance(value.get("files"), dict),
        "invalid-snapshot-manifest",
    )
    require(0 < len(value["files"]) <= MAX_FILES, "snapshot-size-bound")
    total = 0
    for name, record in value["files"].items():
        relative(name)
        require(
            isinstance(record, dict)
            and set(record) == {"sha256", "size"}
            and isinstance(record["sha256"], str)
            and HEX.fullmatch(record["sha256"])
            and type(record["size"]) is int
            and 0 <= record["size"] <= MAX_FILE_BYTES,
            "invalid-snapshot-record",
        )
        total += record["size"]
    require(
        total <= MAX_BYTES
        and value.get("bytes") == total
        and value.get("sha256") == hashlib.sha256(canonical(value["files"])).hexdigest(),
        "snapshot-digest-mismatch",
    )
    return value


def transaction_path(root):
    return root.parent / ".generated-docs-transaction.json"


def atomic_write(parent, name, data, *, directory_fd=None):
    """Publish complete private metadata only after its file and parent are durable."""
    require(relative(name).name == name, "unsafe-metadata-name")
    temporary = ".generated-docs-stage-write-" + uuid.uuid4().hex
    def write(fd):
        created = False
        try:
            output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            created = True
            with os.fdopen(output_fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    if directory_fd is not None:
        write(directory_fd)
    else:
        with anchored(parent, directory=True) as fd:
            write(fd)


def save_transaction(root, stage, backup, before, after):
    document = {
        "schema": 1,
        "stage": stage.name,
        "backup": backup.name,
        "before": before,
        "after": after,
    }
    require(not transaction_path(root).exists() and not transaction_path(root).is_symlink(), "transaction-already-exists")
    atomic_write(root.parent, transaction_path(root).name, canonical(document))


def recover(root, *, rename=os.rename):
    """Recover only the exact recorded generated-tree transaction, preserving edited output."""
    path = transaction_path(root)
    if not path.exists() and not path.is_symlink():
        return
    document = json.loads(read_file(path))
    require(
        document.get("schema") == 1
        and isinstance(document.get("after"), str)
        and HEX.fullmatch(document["after"])
        and (
            document.get("before") is None
            or isinstance(document["before"], str)
            and HEX.fullmatch(document["before"])
        ),
        "unrecognized-receiver-transaction",
    )
    for key, prefix in (("stage", ".generated-docs-stage-"), ("backup", ".generated-docs-backup-")):
        name = document.get(key)
        require(
            isinstance(name, str)
            and name.startswith(prefix)
            and re.fullmatch(r"[A-Za-z0-9._-]+", name),
            "unsafe-transaction-path",
        )
    stage, backup = root.parent / document["stage"], root.parent / document["backup"]
    if not root.exists() and backup.exists():
        require(
            validate_generated(backup)["sha256"] == document["before"],
            "backup-edited-preserve-transaction",
        )
        rename(backup, root)
        fsync_directory(root.parent)
    actual = validate_generated(root)["sha256"] if root.exists() else None
    require(actual in (document["before"], document["after"]), "output-edited-preserve-transaction")
    if backup.exists():
        require(
            actual == document["after"]
            and validate_generated(backup)["sha256"] == document["before"],
            "ambiguous-backup-preserve-transaction",
        )
        shutil.rmtree(backup)
        fsync_directory(root.parent)
    if stage.exists():
        require(
            validate_generated(stage)["sha256"] == document["after"],
            "stage-edited-preserve-transaction",
        )
        shutil.rmtree(stage)
        fsync_directory(root.parent)
    path.unlink()
    fsync_directory(root.parent)


def write_snapshot(root, manifest, output, base_sha256):
    output.write(canonical({"manifest": manifest, "base_sha256": base_sha256}))
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for name, record in sorted(manifest["files"].items()):
            data = read_file(root / name)
            require(
                len(data) == record["size"]
                and hashlib.sha256(data).hexdigest() == record["sha256"],
                "source-snapshot-changed",
            )
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))


def receive(root, source, *, rename=os.rename):
    """The caller holds receiver_lock through validation, staging and promotion."""
    recover(root, rename=rename)
    header = source.readline(MAX_HEADER + 1)
    require(len(header) <= MAX_HEADER and header.endswith(b"\n"), "snapshot-header-bound")
    envelope = json.loads(header)
    desired = validate_manifest(envelope.get("manifest"))
    before = validate_generated(root) if root.exists() else None
    require(
        envelope.get("base_sha256") == (before["sha256"] if before else None), "destination-changed"
    )
    if before:
        require(
            all(
                desired["files"].get(name) == record
                for name, record in before["files"].items()
                if len(Path(name).parts) >= 4 and Path(name).parts[1] == "reports"
            ),
            "immutable-history-missing-or-changed",
        )
    stage = Path(tempfile.mkdtemp(prefix=".generated-docs-stage-", dir=root.parent))
    backup = root.with_name(".generated-docs-backup-" + uuid.uuid4().hex)
    moved = False
    try:
        seen = set()
        with tarfile.open(fileobj=source, mode="r|") as archive:
            for member in archive:
                relative(member.name)
                require(
                    member.isfile()
                    and not member.issparse()
                    and member.name not in seen
                    and member.name in desired["files"],
                    "unsafe-archive-member",
                )
                record = desired["files"][member.name]
                require(member.size == record["size"], "archive-size-mismatch")
                fd = create_file(stage, member.name)
                digest = hashlib.sha256()
                with archive.extractfile(member) as content, os.fdopen(fd, "wb") as output:
                    while True:
                        block = content.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                require(digest.hexdigest() == record["sha256"], "archive-content-mismatch")
                seen.add(member.name)
        require(seen == set(desired["files"]), "archive-files-missing")
        for directory, _, _ in os.walk(stage, topdown=False):
            fsync_directory(Path(directory))
        fsync_directory(root.parent)
        require(validate_generated(stage)["sha256"] == desired["sha256"], "staged-library-invalid")
        require(
            (validate_generated(root)["sha256"] if root.exists() else None)
            == (before["sha256"] if before else None),
            "destination-changed-during-transfer",
        )
        save_transaction(
            root, stage, backup, before["sha256"] if before else None, desired["sha256"]
        )
        if root.exists():
            rename(root, backup)
            fsync_directory(root.parent)
            moved = True
        try:
            rename(stage, root)
            fsync_directory(root.parent)
        except BaseException:
            if moved:
                rename(backup, root)
                fsync_directory(root.parent)
            raise
        result = inventory(root)
        require(result["sha256"] == desired["sha256"], "promoted-snapshot-mismatch")
        if moved:
            require(
                validate_generated(backup)["sha256"] == before["sha256"],
                "backup-changed-preserve-it",
            )
            shutil.rmtree(backup)
            fsync_directory(root.parent)
        transaction_path(root).unlink()
        fsync_directory(root.parent)
        return {
            "state": "synced",
            "sha256": result["sha256"],
            "files": len(result["files"]),
            "bytes": result["bytes"],
        }
    finally:
        if stage.exists() and not transaction_path(root).exists():
            shutil.rmtree(stage)
            fsync_directory(root.parent)
