"""Bind the shared letter client's own scoped Infisical bootstrap token.

Only the bootstrap token lives in the host's protected consumer store.  The registry
address and signing key come from the registry-only Infisical folder through the shared reader.
No seat credentials, retired registry helper, CLI login or signing-key cache is read.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import MutableMapping

TOKEN_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN"
STORE_DIRECTORY = ".config/platform-letter"
STORE_NAME = "infisical-registry.token"
MAX_TOKEN_BYTES = 8192


def bind(environ: MutableMapping[str, str] | None = None) -> str:
    """Bind a private host token when no explicit binding exists; return a safe reason."""
    env = os.environ if environ is None else environ
    if TOKEN_ENV in env:
        return (
            "explicit-binding"  # A bad explicit binding is refused by the reader, never replaced.
        )
    # Presence selects the registry-only route even if custody fails.  Never fall back
    # to an ambient token for a different consumer or to a cached signing key.
    env[TOKEN_ENV] = ""
    home = env.get("HOME") or os.path.expanduser("~")
    folder = os.path.join(home, STORE_DIRECTORY)
    directory_fd = token_fd = None
    try:
        directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return "bootstrap-custody-refused"
        token_fd = os.open(
            STORE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
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
        env[TOKEN_ENV] = token
        return "bootstrap-bound"
    except FileNotFoundError:
        return "bootstrap-missing"
    except (OSError, UnicodeError):
        return "bootstrap-unreadable"
    finally:
        if token_fd is not None:
            os.close(token_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def report(reason: str) -> None:
    """Report a refused binding by name and reason, never its value; keep chats usable."""
    if reason not in {"bootstrap-bound", "explicit-binding"}:
        sys.stderr.write(
            "platform-letter: %s unavailable (%s); registry access disabled\n" % (TOKEN_ENV, reason)
        )
