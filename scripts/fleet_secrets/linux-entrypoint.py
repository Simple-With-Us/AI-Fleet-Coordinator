#!/usr/bin/env -S python3 -I
"""On-demand Linux bootstrap for fleet-secrets.  No credentials enter argv or output."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def load_bootstrap(path):
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pinfo = os.fstat(parent)
        if pinfo.st_uid != os.geteuid() or stat.S_IMODE(pinfo.st_mode) != 0o700:
            raise ValueError("bootstrap directory must be an owned directory with mode 0700")
    finally:
        os.close(parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("bootstrap must be an owned regular file with mode 0600")
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("bootstrap must be a JSON object")
    names = ("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET")
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in names):
        raise ValueError("bootstrap requires both machine credentials")
    return [data[k].strip() for k in names]


def authenticated_env(*, refresh: bool = False):
    """refresh=True mints a fresh token even when one is inherited, so a long-lived
    wrapper never hands the CLI a token that expired after it was injected."""
    env = dict(os.environ)
    if refresh or not (env.get("INFISICAL_TOKEN") or "").strip():
        client_id, client_secret = load_bootstrap(
            Path.home() / ".config/fleet-secrets/bootstrap.json"
        )
        child = dict(env)
        child["INFISICAL_DISABLE_UPDATE_CHECK"] = "true"
        child["INFISICAL_UNIVERSAL_AUTH_CLIENT_ID"] = client_id
        child["INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"] = client_secret
        try:
            result = subprocess.run(
                ["infisical", "login", "--method=universal-auth", "--plain", "--silent"],
                env=child,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        finally:
            client_id = client_secret = ""
            child = {}
        token = result.stdout.strip()
        if result.returncode or not token or any(c.isspace() for c in token):
            raise ValueError("machine authentication failed")
        env["INFISICAL_TOKEN"] = token
    return env


def main():
    try:
        env = authenticated_env(refresh=True)
        target = Path.home() / ".local/share/fleet-secrets/fleet-secrets"
        os.execve(str(target), [str(target), *sys.argv[1:]], env)
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "fleet-secrets: Linux machine bootstrap failed; check provisioning and permissions",
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    sys.exit(main())
