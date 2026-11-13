"""Bind a caller-declared consumer's private Infisical bootstrap token.

Only the bootstrap service token lives in the protected consumer store.  Callers
declare their own absolute directory, filename and process binding; this module
does not discover stores, create files or borrow another consumer's credential.
"""

from __future__ import annotations

import os
import re
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import MutableMapping

MAX_TOKEN_BYTES = 8192
KEYCHAIN_SERVICE = "infisical-service-token"
ACCOUNT_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}\Z")


class BootstrapError(Exception):
    """A fixed reason only; never captured output or declaration content."""


def read_private_declaration(path: str, *, max_bytes: int = 16 * 1024) -> bytes:
    """Read one explicit protected file through its checked descriptors."""
    if (
        not isinstance(path, str)
        or not os.path.isabs(path)
        or any(part in {"", ".", ".."} for part in path.split("/")[1:])
    ):
        raise BootstrapError("consumer-binding-config-refused")
    descriptors = []
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(parent)
        parts = path.split("/")[1:]
        for component in parts[:-1]:
            parent = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            descriptors.append(parent)
        directory = os.fstat(parent)
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise BootstrapError("consumer-binding-custody-refused")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 1 <= before.st_size <= max_bytes
        ):
            raise BootstrapError("consumer-binding-custody-refused")
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(4096, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (
            len(raw) != before.st_size
            or len(raw) > max_bytes
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise BootstrapError("consumer-binding-changed")
        return bytes(raw)
    except FileNotFoundError:
        raise BootstrapError("consumer-binding-missing") from None
    except (OSError, ValueError):
        raise BootstrapError("consumer-binding-unreadable") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def bind_keychain_token(
    environ: MutableMapping[str, str],
    *,
    token_env: str,
    service: str,
    account: str,
    deadline: float,
) -> str:
    """Bind only the caller's declared item; never discover another account or store."""
    if token_env in environ:
        return "bootstrap-binding-conflict"
    environ[token_env] = ""
    if (
        service != KEYCHAIN_SERVICE
        or not isinstance(account, str)
        or not ACCOUNT_RE.fullmatch(account)
    ):
        return "bootstrap-config-refused"
    if sys.platform != "darwin":
        return "bootstrap-platform-unavailable"
    process = None
    try:
        remaining = min(5.0, deadline - time.monotonic())
        if remaining <= 0:
            return "bootstrap-timeout"
        end = time.monotonic() + remaining
        process = subprocess.Popen(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env={"HOME": environ.get("HOME", ""), "PATH": "/usr/bin:/bin"},
            close_fds=True,
        )
        raw = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return "bootstrap-timeout"
                chunk = os.read(process.stdout.fileno(), min(4096, MAX_TOKEN_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_TOKEN_BYTES:
                    return "bootstrap-shape-refused"
        status = process.wait(timeout=max(0.001, end - time.monotonic()))
        if status:
            return (
                "bootstrap-missing"
                if status == 44
                else "bootstrap-keychain-locked"
                if status == 36
                else "bootstrap-unreadable"
            )
        token = raw.decode("ascii").strip()
        if (
            not 20 <= len(token) <= MAX_TOKEN_BYTES
            or not token.startswith("st.")
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            return "bootstrap-shape-refused"
        environ[token_env] = token
        return "bootstrap-bound"
    except subprocess.TimeoutExpired:
        return "bootstrap-timeout"
    except (OSError, ValueError, UnicodeError):
        return "bootstrap-unreadable"
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.stdout is not None:
                process.stdout.close()


def bind_private_token(
    environ: MutableMapping[str, str],
    *,
    token_env: str,
    store_directory: str | os.PathLike[str],
    store_name: str,
) -> str:
    """Preserve explicit bindings; otherwise bind only a private regular token file.

    Return a safe reason token and leave an empty binding on failure.  The empty
    marker selects this consumer's route without an ambient credential fallback.
    """
    if token_env in environ:
        return "explicit-binding"
    environ[token_env] = ""
    directory = os.fspath(store_directory)
    if (
        not os.path.isabs(directory)
        or not store_name
        or store_name in {".", ".."}
        or os.path.basename(store_name) != store_name
    ):
        return "bootstrap-config-refused"
    directory_fd = token_fd = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return "bootstrap-custody-refused"
        token_fd = os.open(
            store_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        info = os.fstat(token_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 20 <= info.st_size <= MAX_TOKEN_BYTES
        ):
            return "bootstrap-custody-refused"
        raw = os.read(token_fd, MAX_TOKEN_BYTES + 1)
        token = raw.decode("ascii").strip()
        if (
            len(raw) > MAX_TOKEN_BYTES
            or len(token) < 20
            or not token.startswith("st.")
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            return "bootstrap-shape-refused"
        environ[token_env] = token
        return "bootstrap-bound"
    except FileNotFoundError:
        return "bootstrap-missing"
    except (OSError, UnicodeError, ValueError):
        return "bootstrap-unreadable"
    finally:
        if token_fd is not None:
            os.close(token_fd)
        if directory_fd is not None:
            os.close(directory_fd)
