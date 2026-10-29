"""Bounded startup-only seat policy input, with no provider or runtime writes."""

import os
import stat

from .policy import PolicyError

MAX_POLICY_BYTES = 4 * 1024 * 1024


def seats_json_from_env(environ=None):
    """Keep inline input compatible, or load one private regular UTF-8 file.

    The file replaces only the input representation. It grants no permissions and
    never merges or modifies seat objects. No hot reload or fallback is performed.
    """
    env = os.environ if environ is None else environ
    if "GIT_RELAY_SEATS_FILE" not in env:
        return env.get("GIT_RELAY_SEATS_JSON", "")
    if "GIT_RELAY_SEATS_JSON" in env:
        raise PolicyError("seat policy inline and file inputs are mutually exclusive")
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
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(MAX_POLICY_BYTES + 1)
            if not raw or len(raw) > MAX_POLICY_BYTES:
                raise PolicyError("seat policy file must be nonempty and bounded")
        return raw.decode("utf-8")
    except PolicyError:
        # Our own constant diagnostics contain no paths or policy contents.
        raise
    except (OSError, UnicodeError, ValueError):
        # No path, OS text or policy bytes in logs or startup diagnostics.
        raise PolicyError("seat policy file could not be safely read") from None
    finally:
        if fd is not None:
            os.close(fd)
