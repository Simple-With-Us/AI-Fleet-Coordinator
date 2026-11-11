"""Bounded startup-only seat policy input, with no provider or runtime writes."""

import os
import stat
import struct

from .policy import PolicyError

MAX_POLICY_BYTES = 4 * 1024 * 1024
SEAT_CREDENTIAL_NAME = "seat-policy"
SEAT_CREDENTIAL_DIRECTORY = "/run/credentials/git-relay.service"


def _read_policy_fd(fd):
    # Own a duplicate so all callers can close their original descriptor once.
    with os.fdopen(os.dup(fd), "rb") as stream:
        raw = stream.read(MAX_POLICY_BYTES + 1)
        if not raw or len(raw) > MAX_POLICY_BYTES:
            raise PolicyError("seat policy file must be nonempty and bounded")
    return raw.decode("utf-8")


def _systemd_acl(fd, permissions):
    """Accept only root and this service UID, not arbitrary named principals."""
    raw = os.getxattr(fd, "system.posix_acl_access")
    if len(raw) < 4 or struct.unpack("<I", raw[:4])[0] != 2 or (len(raw) - 4) % 8:
        raise PolicyError("seat policy credential ACL invalid")
    entries = [struct.unpack("<HHI", raw[i : i + 8]) for i in range(4, len(raw), 8)]
    none = 0xFFFFFFFF
    expected = {
        (1, permissions, none),
        (2, permissions, os.geteuid()),
        (4, 0, none),
        (16, permissions, none),
        (32, 0, none),
    }
    if len(entries) != len(expected) or set(entries) != expected:
        raise PolicyError("seat policy credential ACL invalid")


def _credential_policy(env):
    """Read only the dedicated systemd copy, never a caller-selected file."""
    if (
        env.get("GIT_RELAY_SEATS_CREDENTIAL") != SEAT_CREDENTIAL_NAME
        or env.get("CREDENTIALS_DIRECTORY") != SEAT_CREDENTIAL_DIRECTORY
    ):
        raise PolicyError("seat policy credential binding invalid")
    fds = []
    try:
        # No symlink traversal in any path component. The service name and final
        # credential name are fixed, not arbitrary paths supplied by a caller.
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        fds.append(parent)
        for component in ("run", "credentials", "git-relay.service"):
            parent = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            fds.append(parent)
            info = os.fstat(parent)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise PolicyError("seat policy credential directory invalid")
        info = os.fstat(parent)
        if stat.S_IMODE(info.st_mode) != 0o550 or info.st_gid != 0:
            raise PolicyError("seat policy credential directory invalid")
        _systemd_acl(parent, 5)
        fd = os.open(
            SEAT_CREDENTIAL_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
        fds.append(fd)
        info = os.fstat(fd)
        # 0440 is the systemd ACL mask, not group authorization we add ourselves.
        # systemd supplies a named-user read ACL for this DynamicUser at startup.
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o440
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_POLICY_BYTES
        ):
            raise PolicyError("seat policy credential must be private, owned and bounded")
        _systemd_acl(fd, 4)
        return _read_policy_fd(fd)
    except PolicyError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PolicyError("seat policy credential could not be safely read") from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def seats_json_from_env(environ=None):
    """Choose exactly one input. No fallback, merge or hot reload."""
    env = os.environ if environ is None else environ
    selectors = ("GIT_RELAY_SEATS_JSON", "GIT_RELAY_SEATS_FILE", "GIT_RELAY_SEATS_CREDENTIAL")
    if sum(key in env for key in selectors) > 1:
        raise PolicyError("seat policy inputs are mutually exclusive")
    if "GIT_RELAY_SEATS_CREDENTIAL" in env:
        return _credential_policy(env)
    if "GIT_RELAY_SEATS_FILE" not in env:
        return env.get("GIT_RELAY_SEATS_JSON", "")
    path = env["GIT_RELAY_SEATS_FILE"]
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise PolicyError("seat policy file requires an absolute path")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
            or not 0 < info.st_size <= MAX_POLICY_BYTES
        ):
            raise PolicyError("seat policy file must be private, owned and bounded")
        return _read_policy_fd(fd)
    except PolicyError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PolicyError("seat policy file could not be safely read") from None
    finally:
        if fd is not None:
            os.close(fd)
