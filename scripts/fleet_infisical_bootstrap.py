"""Bind a caller-declared consumer's private Infisical bootstrap token.

Only the bootstrap service token lives in the protected consumer store.  Callers
declare their own absolute directory, filename and process binding; this module
does not discover stores, create files or borrow another consumer's credential.
"""

from __future__ import annotations

import os
import stat
from collections.abc import MutableMapping

MAX_TOKEN_BYTES = 8192


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
