"""Atomic module-only update helpers for install-operator-sheets-drain.sh --code-only.

This module reads and writes public code files only.  It never opens consumer
configuration or credentials; the installer verifies those files by metadata.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any

MODULES = (
    "operator_sheets_export.mjs",
    "operator_sheets_google.mjs",
    "operator_sheets_drain.mjs",
    "thread_registry_export.mjs",
    "registry_export_reader.mjs",
    "fleet_infisical.py",
)
CORE_MODULES = MODULES[:3]
OPTIONAL_MODULES = MODULES[3:]


class InstallError(Exception):
    """A safe refusal that names paths or fields, never file contents."""


class RecoveryError(InstallError):
    """The previous module set could not be proven coherent after rollback."""


class InstallInterrupted(InstallError):
    """A handled termination signal interrupted the installer."""


def _check_path(path: Path, *, uid: int, gid: int, mode: int, directory: bool) -> os.stat_result:
    for ancestor in reversed((path, *path.parents)):
        try:
            ancestor_info = ancestor.lstat()
        except OSError:
            raise InstallError(f"required existing path is unavailable: {ancestor}") from None
        if stat.S_ISLNK(ancestor_info.st_mode):
            raise InstallError(f"refusing symlink path component: {ancestor}")
        if ancestor != path:
            if not stat.S_ISDIR(ancestor_info.st_mode):
                raise InstallError(f"refusing non-plain path: {ancestor}")
            root_owned = (ancestor_info.st_uid, ancestor_info.st_gid) == (0, 0)
            expected_owned = (ancestor_info.st_uid, ancestor_info.st_gid) == (uid, gid)
            if not (root_owned or expected_owned):
                raise InstallError(f"refusing unexpected owner or mode: {ancestor}")
            writable = stat.S_IMODE(ancestor_info.st_mode) & 0o022
            trusted_sticky_root = root_owned and bool(ancestor_info.st_mode & stat.S_ISVTX)
            if writable and not trusted_sticky_root:
                raise InstallError(f"refusing unexpected owner or mode: {ancestor}")
    try:
        info = path.lstat()
    except OSError:
        raise InstallError(f"required existing path is unavailable: {path}") from None
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type:
        raise InstallError(f"refusing non-plain path: {path}")
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
        raise InstallError(f"refusing unexpected owner or mode: {path}")
    return info


def validate_existing_install(
    *, lib_dir: Path, etc_dir: Path, unit_dir: Path, uid: int = 0, gid: int = 0
) -> None:
    """Require the existing core deployment and private consumer files, without reading them."""
    _check_path(lib_dir, uid=uid, gid=gid, mode=0o755, directory=True)
    _check_path(etc_dir, uid=uid, gid=gid, mode=0o700, directory=True)
    _check_path(unit_dir, uid=uid, gid=gid, mode=0o755, directory=True)
    for name in ("sa.json", "drain.env"):
        _check_path(etc_dir / name, uid=uid, gid=gid, mode=0o600, directory=False)
    for name in ("operator-sheets-drain.service", "operator-sheets-drain.timer"):
        _check_path(unit_dir / name, uid=uid, gid=gid, mode=0o644, directory=False)
    for name in MODULES:
        path = lib_dir / name
        if name in OPTIONAL_MODULES and not path.exists() and not path.is_symlink():
            continue  # Upgrade older three- or four-file installations to the complete closure.
        _check_path(path, uid=uid, gid=gid, mode=0o644, directory=False)
    unexpected = sorted(entry.name for entry in lib_dir.iterdir() if entry.name not in MODULES)
    if unexpected:
        raise InstallError(f"refusing unexpected or partial-install entries under {lib_dir}")


def _write_file(path: Path, data: bytes, *, uid: int, gid: int, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup_root(path: Path, *, uid: int, gid: int) -> None:
    if path.exists() or path.is_symlink():
        _check_path(path, uid=uid, gid=gid, mode=0o700, directory=True)
        return
    path.mkdir(mode=0o700)
    os.chown(path, uid, gid)
    os.chmod(path, 0o700)
    _fsync_dir(path.parent)


def validate_backup_destination(path: Path, *, uid: int, gid: int) -> None:
    _check_path(path.parent, uid=uid, gid=gid, mode=0o755, directory=True)
    if path.exists() or path.is_symlink():
        _check_path(path, uid=uid, gid=gid, mode=0o700, directory=True)


def install_modules(
    *,
    source_dir: Path,
    lib_dir: Path,
    etc_dir: Path,
    unit_dir: Path,
    backup_root: Path,
    node: str,
    uid: int = 0,
    gid: int = 0,
) -> Path:
    """Back up the old code closure, atomically replace six files, and roll back on failure."""
    validate_existing_install(
        lib_dir=lib_dir,
        etc_dir=etc_dir,
        unit_dir=unit_dir,
        uid=uid,
        gid=gid,
    )
    validate_backup_destination(backup_root, uid=uid, gid=gid)

    source_bytes = _validated_new_snapshot(source_dir, node=node, uid=uid, gid=gid)
    _verify_prior_snapshot(source_dir, lib_dir, uid=uid, gid=gid)

    original: dict[str, tuple[bytes, int, int, int] | None] = {}
    for name in MODULES:
        path = lib_dir / name
        if not path.exists() and not path.is_symlink():
            original[name] = None
            continue
        info = _check_path(path, uid=uid, gid=gid, mode=0o644, directory=False)
        original[name] = (path.read_bytes(), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))

    _backup_root(backup_root, uid=uid, gid=gid)
    backup_dir = backup_root / (time.strftime("bundle-%Y%m%d-%H%M%S-") + secrets.token_hex(4))
    backup_dir.mkdir(mode=0o700)
    os.chown(backup_dir, uid, gid)
    os.chmod(backup_dir, 0o700)
    manifest: dict[str, Any] = {"modules": {}}
    for name, prior in original.items():
        if prior is None:
            manifest["modules"][name] = {"present": False}
            continue
        data, old_uid, old_gid, old_mode = prior
        _write_file(backup_dir / name, data, uid=uid, gid=gid, mode=0o600)
        manifest["modules"][name] = {
            "present": True,
            "sha256": hashlib.sha256(data).hexdigest(),
            "uid": old_uid,
            "gid": old_gid,
            "mode": old_mode,
        }
    _write_file(
        backup_dir / "manifest.json",
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        uid=uid,
        gid=gid,
        mode=0o600,
    )
    _fsync_dir(backup_dir)
    _fsync_dir(backup_root)

    transaction = lib_dir / (".code-only-" + secrets.token_hex(8))
    restore_dir = transaction / "restore"
    replaced: list[str] = []
    try:
        transaction.mkdir(mode=0o700)
        os.chown(transaction, uid, gid)
        os.chmod(transaction, 0o700)
        restore_dir.mkdir(mode=0o700)
        os.chown(restore_dir, uid, gid)
        os.chmod(restore_dir, 0o700)
        for name, data in source_bytes.items():
            _write_file(transaction / name, data, uid=uid, gid=gid, mode=0o644)
        for name, prior in original.items():
            if prior is not None:
                data, old_uid, old_gid, old_mode = prior
                _write_file(restore_dir / name, data, uid=old_uid, gid=old_gid, mode=old_mode)
        _fsync_dir(transaction)
        _fsync_dir(restore_dir)

        for name in MODULES:
            os.replace(transaction / name, lib_dir / name)
            replaced.append(name)
        _fsync_dir(lib_dir)
        _verify_new(lib_dir, source_bytes, uid=uid, gid=gid)
    except BaseException:
        previous_handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, signal.SIG_IGN)
        rollback_error = False
        try:
            for name in reversed(replaced):
                old = original[name]
                destination = lib_dir / name
                try:
                    if old is None:
                        destination.unlink()
                    else:
                        os.replace(restore_dir / name, destination)
                except FileNotFoundError:
                    if old is not None:
                        rollback_error = True
                except OSError:
                    rollback_error = True
            try:
                _fsync_dir(lib_dir)
                _verify_prior(lib_dir, original, uid=uid, gid=gid)
            except (InstallError, OSError):
                rollback_error = True
            if rollback_error:
                raise RecoveryError(
                    f"module replacement failed and prior-bundle restoration is unverified; leave the timer stopped (backup: {backup_dir})"
                ) from None
            raise InstallError(
                f"module replacement failed; the prior bundle was restored (backup: {backup_dir})"
            ) from None
        finally:
            for signum, previous_handler in previous_handlers.items():
                signal.signal(signum, previous_handler)
    finally:
        shutil.rmtree(transaction, ignore_errors=True)

    return backup_dir


def _verify_prior(
    lib_dir: Path, original: dict[str, tuple[bytes, int, int, int] | None], *, uid: int, gid: int
) -> None:
    for name, prior in original.items():
        path = lib_dir / name
        if prior is None:
            if path.exists() or path.is_symlink():
                raise InstallError(f"prior absent module was not restored: {name}")
            continue
        data, old_uid, old_gid, old_mode = prior
        info = _check_path(path, uid=old_uid, gid=old_gid, mode=old_mode, directory=False)
        if info.st_uid != uid or info.st_gid != gid or path.read_bytes() != data:
            raise InstallError(f"prior module verification failed for {name}")


def _verify_new(lib_dir: Path, source_bytes: dict[str, bytes], *, uid: int, gid: int) -> None:
    for name, expected in source_bytes.items():
        path = lib_dir / name
        _check_path(path, uid=uid, gid=gid, mode=0o644, directory=False)
        if path.read_bytes() != expected:
            raise InstallError(f"installed module verification failed for {name}")


def validate_source_bundle(*, source_dir: Path, node: str, uid: int, gid: int) -> dict[str, bytes]:
    """Read and syntax-check every JavaScript/Python file before replacing installed code."""
    import subprocess

    source_bytes: dict[str, bytes] = {}
    for name in MODULES:
        source = source_dir / name
        _check_path(
            source,
            uid=uid,
            gid=gid,
            mode=0o600,
            directory=False,
        )
        data = source.read_bytes()
        if source.suffix == ".py":
            try:
                ast.parse(data.decode("utf-8"), filename=name)
            except (SyntaxError, UnicodeDecodeError):
                raise InstallError(f"source syntax check failed for {name}") from None
        else:
            try:
                checked = subprocess.run(
                    [node, "--check", "--", str(source)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                raise InstallError(f"source syntax check timed out for {name}") from None
            if checked.returncode:
                raise InstallError(f"source syntax check failed for {name}")
        source_bytes[name] = data
    return source_bytes


def stage_source_bundle(
    *, source_dir: Path, staging_root: Path, lib_dir: Path, node: str, uid: int, gid: int
) -> Path:
    """Snapshot and syntax-check the exact source bytes before the service wait."""
    _backup_root(staging_root, uid=uid, gid=gid)
    source_bytes: dict[str, bytes] = {}
    for name in MODULES:
        source = source_dir / name
        if name == "fleet_infisical.py" and not source.exists() and not source.is_symlink():
            # The repository keeps this canonical worker at scripts/fleet_infisical.py,
            # beside simple_tools/, while the immutable stage itself is flat.
            source = source_dir.parent / name
        _check_path(
            source,
            uid=uid,
            gid=gid,
            mode=0o600,
            directory=False,
        )
        source_bytes[name] = source.read_bytes()
    stage_dir = staging_root / ("staged-" + secrets.token_hex(12))
    stage_dir.mkdir(mode=0o700)
    os.chown(stage_dir, uid, gid)
    os.chmod(stage_dir, 0o700)
    try:
        for name, data in source_bytes.items():
            _write_file(stage_dir / name, data, uid=uid, gid=gid, mode=0o600)
        staged_bytes = validate_source_bundle(source_dir=stage_dir, node=node, uid=uid, gid=gid)
        if staged_bytes != source_bytes:
            raise InstallError("staged module bytes differ from the reviewed source snapshot")
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in staged_bytes.items()}
        _write_file(
            stage_dir / "manifest.json",
            (json.dumps(hashes, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            uid=uid,
            gid=gid,
            mode=0o600,
        )
        prior_dir = stage_dir / "prior"
        prior_dir.mkdir(mode=0o700)
        os.chown(prior_dir, uid, gid)
        os.chmod(prior_dir, 0o700)
        prior_manifest: dict[str, Any] = {"modules": {}}
        for name in MODULES:
            path = lib_dir / name
            if not path.exists() and not path.is_symlink():
                prior_manifest["modules"][name] = {"present": False}
                continue
            info = _check_path(path, uid=uid, gid=gid, mode=0o644, directory=False)
            data = path.read_bytes()
            _write_file(prior_dir / name, data, uid=uid, gid=gid, mode=0o600)
            prior_manifest["modules"][name] = {
                "present": True,
                "sha256": hashlib.sha256(data).hexdigest(),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": stat.S_IMODE(info.st_mode),
            }
        _write_file(
            stage_dir / "prior-manifest.json",
            (json.dumps(prior_manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            uid=uid,
            gid=gid,
            mode=0o600,
        )
        _fsync_dir(prior_dir)
        _fsync_dir(stage_dir)
        _fsync_dir(staging_root)
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return stage_dir


def _verify_prior_snapshot(source_dir: Path, lib_dir: Path, *, uid: int, gid: int) -> None:
    _check_path(source_dir, uid=uid, gid=gid, mode=0o700, directory=True)
    prior_dir = source_dir / "prior"
    _check_path(prior_dir, uid=uid, gid=gid, mode=0o700, directory=True)
    prior_manifest_path = source_dir / "prior-manifest.json"
    _check_path(prior_manifest_path, uid=uid, gid=gid, mode=0o600, directory=False)
    try:
        modules = json.loads(prior_manifest_path.read_text())["modules"]
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError("the prior module snapshot is missing or invalid") from None
    if not isinstance(modules, dict):
        raise InstallError("the prior module snapshot is incomplete")
    for name in MODULES:
        record = modules.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("present"), bool):
            raise InstallError("the prior module snapshot is incomplete")
        path = lib_dir / name
        if not record["present"]:
            if path.exists() or path.is_symlink():
                raise InstallError(f"installed module set changed after staging: {name}")
            continue
        try:
            snapshot_path = prior_dir / name
            _check_path(snapshot_path, uid=uid, gid=gid, mode=0o600, directory=False)
            expected = snapshot_path.read_bytes()
        except OSError:
            raise InstallError("the prior module snapshot is incomplete") from None
        if hashlib.sha256(expected).hexdigest() != record.get("sha256"):
            raise InstallError("the prior module snapshot hash does not match")
        info = _check_path(
            path,
            uid=record.get("uid", -1),
            gid=record.get("gid", -1),
            mode=record.get("mode", -1),
            directory=False,
        )
        if info.st_uid != uid or info.st_gid != gid or path.read_bytes() != expected:
            raise InstallError(f"installed module set changed after staging: {name}")


def verify_installed_state(
    *, source_dir: Path, lib_dir: Path, uid: int, gid: int, node: str
) -> str:
    """Return prior/new only when the complete module set matches a saved state."""
    _check_path(lib_dir, uid=uid, gid=gid, mode=0o755, directory=True)
    unexpected = sorted(entry.name for entry in lib_dir.iterdir() if entry.name not in MODULES)
    if unexpected:
        raise InstallError("the installed module directory contains a partial-install entry")
    new_bytes = _validated_new_snapshot(source_dir, node=node, uid=uid, gid=gid)
    new_ok = True
    for name, expected in new_bytes.items():
        path = lib_dir / name
        try:
            _check_path(path, uid=uid, gid=gid, mode=0o644, directory=False)
            if path.read_bytes() != expected:
                new_ok = False
        except InstallError:
            new_ok = False
    if new_ok:
        return "new"
    try:
        _verify_prior_snapshot(source_dir, lib_dir, uid=uid, gid=gid)
    except InstallError:
        raise InstallError("installed module set is mixed or unverifiable") from None
    return "prior"


def _validated_new_snapshot(source_dir: Path, *, node: str, uid: int, gid: int) -> dict[str, bytes]:
    _check_path(source_dir, uid=uid, gid=gid, mode=0o700, directory=True)
    manifest_path = source_dir / "manifest.json"
    _check_path(manifest_path, uid=uid, gid=gid, mode=0o600, directory=False)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        raise InstallError("the new module snapshot manifest is missing or invalid") from None
    source_bytes = validate_source_bundle(source_dir=source_dir, node=node, uid=uid, gid=gid)
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
    if manifest != expected:
        raise InstallError("the new module snapshot hash does not match")
    return source_bytes


def main(argv: list[str] | None = None) -> int:
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(
        description="Install the reviewed Operator Sheets code bundle only"
    )
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--lib-dir", type=Path, required=True)
    parser.add_argument("--etc-dir", type=Path, required=True)
    parser.add_argument("--unit-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--verify-state", action="store_true")
    parser.add_argument("--uid", type=int, default=0)
    parser.add_argument("--gid", type=int, default=0)
    args = parser.parse_args(argv)

    def interrupt(signum: int, _frame: object) -> None:
        raise InstallInterrupted(f"interrupted by signal {signum}")

    previous_handlers = {}
    try:
        previous_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, interrupt)
        previous_handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, interrupt)
        try:
            if args.verify_state:
                state = verify_installed_state(
                    source_dir=args.source_dir,
                    lib_dir=args.lib_dir,
                    uid=args.uid,
                    gid=args.gid,
                    node=args.node,
                )
                print(f"operator-sheets-code-only: coherent={state}")
                return 0
            validate_existing_install(
                lib_dir=args.lib_dir,
                etc_dir=args.etc_dir,
                unit_dir=args.unit_dir,
                uid=args.uid,
                gid=args.gid,
            )
            validate_backup_destination(args.backup_root, uid=args.uid, gid=args.gid)
            if args.stage_only:
                staged = stage_source_bundle(
                    source_dir=args.source_dir,
                    staging_root=args.backup_root,
                    lib_dir=args.lib_dir,
                    node=args.node,
                    uid=args.uid,
                    gid=args.gid,
                )
                print(staged)
                return 0
            validate_source_bundle(
                source_dir=args.source_dir, node=args.node, uid=args.uid, gid=args.gid
            )
            if args.validate_only:
                print("operator-sheets-code-only: existing install and source bundle validated")
                return 0
            backup = install_modules(
                source_dir=args.source_dir,
                lib_dir=args.lib_dir,
                etc_dir=args.etc_dir,
                unit_dir=args.unit_dir,
                backup_root=args.backup_root,
                node=args.node,
                uid=args.uid,
                gid=args.gid,
            )
        except (InstallError, OSError, subprocess.SubprocessError) as exc:
            print(f"operator-sheets-code-only: {exc}", file=sys.stderr)
            return 2 if isinstance(exc, RecoveryError) else 1
        print(f"operator-sheets-code-only: installed six-file code bundle; backup={backup}")
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
