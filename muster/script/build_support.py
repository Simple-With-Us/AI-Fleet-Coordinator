"""Filesystem operations for the local Muster builder.  Never launches an app."""

import os
import shutil
import signal
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
    """Flush one file or directory entry;  a rename alone is not crash-durable."""
    flags = os.O_RDONLY
    if path.is_dir():
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root):
    """Flush a copied bundle deepest-first, then its root, before publication."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
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
        for signum in (signal.SIGTERM, signal.SIGINT):
            saved_handlers[signum] = signal.signal(signum, interrupted)
        if os.path.lexists(installed):
            os.replace(installed, previous)
            fsync_path(installed.parent)
        try:
            os.replace(prepared, installed)
            fsync_path(installed.parent)
            verify(installed)
            committed = True
        except BaseException:
            if previous.exists():
                if os.path.lexists(installed):
                    shutil.rmtree(installed)
                os.replace(previous, installed)
                fsync_path(installed.parent)
            raise
    finally:
        try:
            # The signal may arrive between the first rename and the inner try.
            if previous.exists() and not os.path.lexists(installed):
                os.replace(previous, installed)
                fsync_path(installed.parent)
        finally:
            for signum, handler in saved_handlers.items():
                signal.signal(signum, handler)
            # A failed rollback must preserve the old bundle for recovery.
            if committed or not previous.exists():
                shutil.rmtree(temporary)
