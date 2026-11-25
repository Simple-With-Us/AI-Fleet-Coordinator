"""Optional bounded host configuration. Secrets are never configuration values."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .operator_pr_origin import Held

#: The two credential values may arrive as loader-delivered environment values (the
#: sanctioned Infisical transport) instead of file paths;  a file name wins when both
#: are present.
NAMES = frozenset(
    (
        "RA_ENABLED",
        "RA_REPOS",
        "RA_SEAT_MAP_JSON",
        "RA_PLATFORM_MAP_JSON",
        "RA_STATE_DIR",
        "RA_SERVICE_TOKEN_FILE",
        "RA_WAKE_KEY_FILE",
        "RA_SERVICE_TOKEN",
        "RA_WAKE_KEY",
        "RA_SELFTEST_SEAT",
        "RA_INTERVAL_S",
        "RA_RELAY_URL",
        "RA_ROUTER_URL",
    )
)
#: The local narrowing file never carries a secret:  the two credential VALUES may arrive only
#: through the loader-delivered environment (the sanctioned Infisical transport), never through
#: config.json, which a hand on the host could otherwise read.  FILE paths stay allowed there.
#: The two credential VALUES:  loader-delivered environment only, never the local file.
SECRET_VALUE_NAMES = frozenset({"RA_SERVICE_TOKEN", "RA_WAKE_KEY"})


def configured(env):
    out = dict(env)
    path = Path(env.get("SIMPLE_TOOLS_STATE_DIR", "/data")) / "review-alerts/config.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return out
    except OSError:
        raise Held("review-config-unreadable") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o077
        ):
            raise Held("review-config-custody")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise Held("review-config-size")
    finally:
        os.close(fd)
    try:

        def pairs(items):
            value = {}
            for k, v in items:
                if k in value:
                    raise ValueError()
                value[k] = v
            return value

        values = json.loads(raw, object_pairs_hook=pairs)
        if (
            not isinstance(values, dict)
            or set(values) - NAMES
            or any(not isinstance(v, str) for v in values.values())
        ):
            raise ValueError()
        # A credential value never comes from the host-writable narrowing file.
        if set(values) & SECRET_VALUE_NAMES:
            raise Held("review-config-secret-name")
    except (ValueError, UnicodeError):
        raise Held("review-config-invalid") from None
    # Explicit runtime environment remains authoritative, including disabling.
    for k, v in values.items():
        out.setdefault(k, v)
    return out
