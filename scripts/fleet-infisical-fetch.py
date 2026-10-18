#!/usr/bin/env python3
"""Fetch named secrets from Infisical using a machine identity found in the ENVIRONMENT.

Built for cloud sandboxes (Claude Code Cloud, Codespaces, Cursor cloud, any throwaway
clone), where there is no `fleet-secrets` but the platform can inject environment
variables.  On the Mac, scripts/fleet_rag/core.py reads the same universal-auth identity
out of the environment or `fleet-secrets` instead; this helper reuses core.py's login endpoint,
project, environment and HTTP client so both paths behave identically.

Identity env names use the fleet's existing prefix convention (see core.py `_identity`):

    INFISICAL_SHARED_CLIENT_ID     / INFISICAL_SHARED_CLIENT_SECRET
    INFISICAL_AUTOMATION_CLIENT_ID / INFISICAL_AUTOMATION_CLIENT_SECRET

Usage:
    fleet-infisical-fetch.py --env-file PATH KEY [KEY ...]   # write a 0600 env file
    fleet-infisical-fetch.py --identity                      # report identity, no network

Values are NEVER printed, logged, or passed on a command line -- only key NAMES and
booleans reach stdout.  The env file is created under umask 077.

Exit codes: 0 ok  ·  2 usage  ·  3 no identity in the environment  ·  4 fetch failed.
"""
from __future__ import annotations

import os
import pathlib
import shlex
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fleet_rag.core import (  # noqa: E402
    FleetRagError,
    http_json,
    infisical_api,
    infisical_env,
    infisical_project,
)

# Same prefixes, same order, as core.infisical_login().
PREFIXES = ("INFISICAL_SHARED", "INFISICAL_AUTOMATION")


def env_identity() -> tuple[str | None, str | None, str | None]:
    """Return (prefix, client_id, client_secret) from the environment, or (None, None, None)."""
    for prefix in PREFIXES:
        cid = (os.environ.get(f"{prefix}_CLIENT_ID") or "").strip()
        csec = (os.environ.get(f"{prefix}_CLIENT_SECRET") or "").strip()
        if cid and csec:
            return prefix, cid, csec
    return None, None, None


def login(cid: str, csec: str) -> str:
    return http_json(f"{infisical_api()}/v1/auth/universal-auth/login",
                     {"clientId": cid, "clientSecret": csec}, retries=1)["accessToken"]


def fetch(token: str, wanted: list[str]) -> dict[str, str]:
    got = http_json(
        f"{infisical_api()}/v3/secrets/raw?workspaceId={infisical_project()}"
        f"&environment={infisical_env()}&secretPath=%2F",
        headers={"Authorization": f"Bearer {token}"}, retries=1)
    out: dict[str, str] = {}
    for s in got.get("secrets", []):
        if s.get("secretKey") in wanted and s.get("secretValue"):
            out[s["secretKey"]] = s["secretValue"]
    return out


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
    body = ("# Fleet recall credentials -- written by scripts/cloud-setup.sh from Infisical.\n"
            "# Load with:  set -a; . " + str(path) + "; set +a\n")
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
        print(f"identity: {'true' if prefix else 'false'}"
              + (f" ({prefix})" if prefix else ""))
        return 0 if prefix else 3
    if len(argv) < 3 or argv[0] != "--env-file":
        print("usage: fleet-infisical-fetch.py --env-file PATH KEY [KEY ...]", file=sys.stderr)
        print("       fleet-infisical-fetch.py --identity", file=sys.stderr)
        return 2

    path = pathlib.Path(argv[1]).expanduser()
    wanted = argv[2:]

    prefix, cid, csec = env_identity()
    if not prefix:
        print("no-identity")
        return 3
    print(f"identity: true ({prefix})")

    try:
        token = login(cid, csec)  # type: ignore[arg-type]
        got = fetch(token, wanted)
    except (FleetRagError, KeyError, TypeError) as e:
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
