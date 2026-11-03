#!/usr/bin/env python3
"""Fetch named secrets with the dedicated Fleet Recall scoped service-token binding.

Built for cloud sandboxes (Claude Code Cloud, Codespaces, Cursor cloud, any throwaway
clone), where there is no `fleet-secrets` but the platform can inject a scoped token.
The same binding is used on the Mac and cloud environments, and each request validates
the service-token grant before reading.

Token env name: ``FLEET_RECALL_INFISICAL_TOKEN``.

Usage:
    fleet-infisical-fetch.py --env-file PATH KEY [KEY ...]   # write a 0600 env file
    fleet-infisical-fetch.py --identity                      # report identity, no network

Values are NEVER printed, logged, or passed on a command line -- only key NAMES and
booleans reach stdout.  The env file is created under umask 077.

Exit codes: 0 ok  ·  2 usage  ·  3 scoped token missing  ·  4 fetch failed.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fleet_rag.core import (
    FleetRagError,
    http_json,
    infisical_api,
    infisical_env,
    infisical_project,
)
from fleet_secrets.core import SecretsError, check_service_token

TOKEN_ENV = "FLEET_RECALL_INFISICAL_TOKEN"


def env_identity() -> tuple[str | None, None, None]:
    """Report only whether the recall-specific service-token binding exists."""
    return (TOKEN_ENV, None, None) if (os.environ.get(TOKEN_ENV) or "").strip() else (None, None, None)


def fetch(token: str, wanted: list[str]) -> dict[str, str]:
    from fleet_rag.core import infisical_api, infisical_env, infisical_project
    from fleet_secrets import core as secrets
    env = dict(os.environ)
    env[secrets.ENV_API] = infisical_api()
    try:
        infisical_env()
        return secrets.fetch_project(infisical_project(), "/", env, token=token, names=wanted)
    except secrets.SecretsError as exc:
        raise FleetRagError("scoped Infisical read failed: " + exc.message) from None


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Parse an existing KEY=value env file.  Returns the map; values stay in memory only."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if not k:
            continue
        try:
            parts = shlex.split(v)
        except ValueError:
            parts = [v]
        out[k] = parts[0] if parts else ""
    return out


def write_env_file(path: pathlib.Path, values: dict[str, str]) -> None:
    """Write KEY='value' lines to a 0600 file via temp-file + rename.  Never echoes a value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# Fleet recall credentials -- written by scripts/cloud-setup.sh from Infisical.\n"
        "# Load with:  set -a; . " + str(path) + "; set +a\n"
    )
    for k in sorted(values):
        body += f"{k}={shlex.quote(values[k])}\n"
    old = os.umask(0o077)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".fleet-recall.", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old)


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "--identity":
        prefix, _, _ = env_identity()
        print(f"service-token-binding: {'true' if prefix else 'false'}" + (f" ({prefix})" if prefix else ""))
        return 0 if prefix else 3
    if len(argv) < 3 or argv[0] != "--env-file":
        print("usage: fleet-infisical-fetch.py --env-file PATH KEY [KEY ...]", file=sys.stderr)
        print("       fleet-infisical-fetch.py --identity", file=sys.stderr)
        return 2

    path = pathlib.Path(argv[1]).expanduser()
    wanted = argv[2:]

    prefix, _, _ = env_identity()
    if not prefix:
        print("no-identity")
        return 3
    print(f"service-token-binding: true ({prefix})")

    try:
        token = (os.environ.get(TOKEN_ENV) or "").strip()
        token = check_service_token(token)
        got = fetch(token, wanted)
    except (FleetRagError, SecretsError, KeyError, TypeError, ValueError) as e:
        print(f"fetch-failed: {type(e).__name__}", file=sys.stderr)
        return 4

    merged = read_env_file(path)
    missing = [k for k in wanted if k not in got]
    changed = [k for k in wanted if k in got and merged.get(k) != got[k]]
    merged.update(got)

    if changed:
        write_env_file(path, merged)
        for k in changed:
            print(f"wrote: {k}")
    else:
        print("unchanged")
    for k in missing:
        print(f"missing: {k}")
    return 0 if not missing else 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
