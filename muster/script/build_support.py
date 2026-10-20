"""Filesystem operations for the local Muster builder.  Never launches an app."""

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path


def acquire_lock(path):
    """Only an existing lock means another build holds it."""
    try:
        Path(path).mkdir()
    except FileExistsError:
        raise RuntimeError(
            "Another Muster build holds the lock; inspect it before removal."
        ) from None


def fsync_path(path):
    flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if path.is_dir() else 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root):
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_symlink():
            fsync_path(path)
    fsync_path(root)


def install_bundle(staged, installed, verify):
    """Prepare on the target filesystem, then replace with rollback on failure."""
    staged, installed = Path(staged), Path(installed)
    temporary = Path(tempfile.mkdtemp(prefix=".muster-install-", dir=installed.parent))
    prepared, previous = temporary / "new.app", temporary / "previous.app"
    saved_handlers = {}
    committed = False

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    try:
        shutil.copytree(staged, prepared, symlinks=True)
        verify(prepared)
        fsync_tree(prepared)
        fsync_path(temporary)
        for signum in (signal.SIGTERM, signal.SIGINT):
            saved_handlers[signum] = signal.signal(signum, interrupted)
        if os.path.lexists(installed):
            os.replace(installed, previous)
            fsync_path(temporary)
            fsync_path(installed.parent)
        try:
            os.replace(prepared, installed)
            fsync_path(temporary)
            fsync_path(installed.parent)
            verify(installed)
            committed = True
        except BaseException:
            # A first-time install has nothing to restore, so drop the unverified bundle.
            if os.path.lexists(installed):
                shutil.rmtree(installed)
                fsync_path(installed.parent)
            if previous.exists():
                os.replace(previous, installed)
                fsync_path(temporary)
                fsync_path(installed.parent)
            raise
    finally:
        try:
            # The signal may arrive between the first rename and the inner try.
            if previous.exists() and not os.path.lexists(installed):
                os.replace(previous, installed)
                fsync_path(temporary)
                fsync_path(installed.parent)
        finally:
            for signum, handler in saved_handlers.items():
                signal.signal(signum, handler)
            # A failed rollback must preserve the old bundle for recovery.
            if committed or not previous.exists():
                shutil.rmtree(temporary)


def installed_pids(binary):
    """Find only the installed executable, without reading any process arguments."""
    binary = Path(binary).resolve()
    found = subprocess.run(
        ["/usr/bin/pgrep", "-x", binary.name],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if found.returncode == 1:
        return []
    if found.returncode:
        raise RuntimeError("Cannot discover active Muster processes.")
    pids = found.stdout.split()
    if not pids or any(not pid.isdecimal() or int(pid) <= 0 for pid in pids):
        raise RuntimeError("Invalid Muster process discovery result.")
    matches = []
    for pid in dict.fromkeys(pids):
        try:
            inspected = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", pid, "-d", "txt", "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            paths = [line[1:] for line in inspected.stdout.splitlines() if line.startswith("n")]
            if inspected.returncode or not paths:
                raise RuntimeError("Cannot inspect an active Muster executable.")
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            raise RuntimeError("Cannot inspect an active Muster executable.") from None
        if any(Path(path).resolve() == binary for path in paths):
            matches.append(pid)
    return matches
