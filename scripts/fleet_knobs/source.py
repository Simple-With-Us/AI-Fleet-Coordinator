"""Fetch knobs from Infisical through the installed ``infisical`` CLI.

Prod is the only environment (owner 2026-10-10):  every call passes ``--env=prod`` itself,
because the CLI's own default is dev, and a stray ``INFISICAL_ENVIRONMENT`` other than prod
is refused before anything runs.  The knobs live in the "AI Fleet Coordinator" project at
the root path.

One ``export`` call returns the whole path.  This module keeps only the names it was asked
for and drops the rest at once, so no other secret outlives the call.  The CLI's output is
never printed or logged, and neither is its stderr:  a failure is reduced to a short reason
token (``cli-missing``, ``timeout``, ``exit-1``, ``bad-output``).

Why the CLI and not HTTP:  a stable copy run with ``python3 -I`` then needs no sibling
package, and the Mac's LaunchAgents already rely on the CLI's login (see
scripts/infisical-secrets-sync.sh).  ``fleet_rag/infisical_settings.py`` is the reference for
the contract (prod only, load once, keep the last-known-good, names only in logs).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable

PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator
ENVIRONMENT = "prod"
CLI_NAME = "infisical"
# A LaunchAgent's PATH is minimal, so look in the usual install places after PATH.
FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
TIMEOUT_SECONDS = 20.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
ENV_NAMES = ("INFISICAL_ENVIRONMENT", "INFISICAL_ENV")


class FetchFailed(Exception):
    """The fetch did not produce a usable answer.  `reason` is a short token, never output."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def environment_refusal() -> str | None:
    """Why Infisical may not be read (a non-prod slug in the environment), or None."""
    for var in ENV_NAMES:
        slug = (os.environ.get(var) or "").strip().lower()
        if slug and slug != ENVIRONMENT:
            return (
                f"refusing Infisical environment {slug!r} from {var}:  prod is the only "
                "environment (dev and staging are retired);  unset it or set it to prod;  "
                "see INFISICAL.md"
            )
    return None


def find_cli() -> str | None:
    found = shutil.which(CLI_NAME)
    if found:
        return found
    for directory in FALLBACK_DIRS:
        candidate = os.path.join(os.path.expanduser(directory), CLI_NAME)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def argv_for(cli: str) -> list:
    return [
        cli,
        "export",
        f"--env={ENVIRONMENT}",
        f"--projectId={PROJECT_ID}",
        "--format=json",
        "--path=/",
        "--silent",
        "--expand=false",
        "--include-imports=false",
    ]


def fetch(names: Iterable[str]) -> dict[str, str]:
    """The raw string value of each of `names` that Infisical holds (absent names are left out).

    Raises FetchFailed with a reason token.  Never logs or returns anything but the
    requested names."""
    wanted = set(names)
    cli = find_cli()
    if not cli:
        raise FetchFailed("cli-missing")
    return _pick(_run(argv_for(cli)), wanted)


def argv_named(cli: str, names: Iterable[str], project_id: str, path: str) -> list:
    return [
        cli,
        "secrets",
        "get",
        *sorted(set(names)),
        f"--env={ENVIRONMENT}",
        f"--projectId={project_id}",
        f"--path={path}",
        "--output=json",
        "--silent",
        "--expand=false",
        "--include-imports=false",
    ]


def fetch_named(names: Iterable[str], *, project_id: str, path: str) -> dict[str, str]:
    """Only the named secrets of one folder of another project (``infisical secrets get``).

    Unlike ``fetch`` the CLI itself is asked for just these names, so no other secret in the
    folder reaches this process.  Absent names are left out.  Same contract otherwise:  prod only,
    the CLI's output and stderr never printed, a failure reduced to a reason token."""
    wanted = set(names)
    cli = find_cli()
    if not cli:
        raise FetchFailed("cli-missing")
    return _pick(_run(argv_named(cli, wanted, project_id, path)), wanted)


def _run(argv: list):
    """Run the CLI and parse its JSON.  Raises FetchFailed with a reason token."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            cwd="/",
            env=dict(os.environ),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise FetchFailed("timeout") from None
    except OSError:
        raise FetchFailed("cli-missing") from None
    if proc.returncode != 0:
        raise FetchFailed(f"exit-{proc.returncode}")
    out = proc.stdout
    if len(out) > MAX_OUTPUT_BYTES:
        raise FetchFailed("bad-output")
    try:
        return json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FetchFailed("bad-output") from None
    finally:
        out = b""


def _pick(payload, wanted) -> dict[str, str]:
    """Accept the shapes ``export --format=json`` has used:  {KEY: value}, a list of
    {key, value} or {secretKey, secretValue} objects, or an API-style {"secrets": [...]}."""
    if isinstance(payload, dict) and isinstance(payload.get("secrets"), list):
        payload = payload["secrets"]
    found: dict[str, str] = {}
    if isinstance(payload, dict):
        pairs = payload.items()
    elif isinstance(payload, list):
        pairs = []
        for item in payload:
            if not isinstance(item, dict):
                raise FetchFailed("bad-output")
            key = item.get("key", item.get("secretKey"))
            val = item.get("value", item.get("secretValue"))
            pairs.append((key, val))
    else:
        raise FetchFailed("bad-output")
    for key, val in pairs:
        if key in wanted and isinstance(val, (str, int, float, bool)):
            text = str(val)
            if isinstance(val, bool):
                text = "true" if val else "false"
            found[key] = text
    return found
