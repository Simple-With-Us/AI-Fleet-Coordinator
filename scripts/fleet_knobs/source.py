"""Fetch knobs from Infisical through the installed ``infisical`` CLI.

Prod is the only environment (owner 2026-10-10):  every call passes ``--env=prod`` itself,
because the CLI's own default is dev, and a stray ``INFISICAL_ENVIRONMENT`` other than prod
is refused before anything runs.  The knobs live in the "AI Fleet Coordinator" project at
the root path.

One ``export`` call returns the whole path.  This module keeps only the names it was asked
for and drops the rest at once, so no other secret outlives the call.  The CLI's output is
never printed or logged, and neither is its stderr:  a failure is reduced to a short reason
token (``cli-missing``, ``timeout``, ``exit-1``, ``bad-output``, ``unresolved-reference``).

Why the CLI and not HTTP:  a stable copy run with ``python3 -I`` then needs no sibling
package.  ``fleet_rag/infisical_settings.py`` is the reference for the contract (prod only,
load once, keep the last-known-good, names only in logs).

Auth, first match wins, the same order as ``fleet-secrets`` (scripts/fleet_secrets/core.py):
``INFISICAL_TOKEN`` already in the environment;  else the universal-auth machine identity in
the login Keychain, then in the System keychain, logged in through fleet-secrets' own helper;
else the CLI's own login session.  The machine identity matters for pm2:  pm2 runs as the
system LaunchDaemon ``com.pm2.daemon``, in the launchd System session, where the login
Keychain (and so the CLI's session) is unreachable, and a session-only fetch there exits 1 or
hangs.  The helper is loaded opportunistically (``FLEET_SECRETS_HOME``, then a sibling
``fleet_secrets`` package, then ``~/.local/share/fleet-secrets``);  when none is found this
module falls back to the CLI session, so it stays stdlib-only and standalone.  The access
token goes only into the child's environment:  it is never logged, never in an argument
list and never written to disk.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from types import ModuleType

PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator
ENVIRONMENT = "prod"
CLI_NAME = "infisical"
# A LaunchAgent's PATH is minimal, so look in the usual install places after PATH.
FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
# Short on purpose:  a hung CLI (a locked keychain under launchd) must not stall a job or an MCP
# start for long, and lkg.py's negative cache spares sibling processes the wait (fail fast).
TIMEOUT_SECONDS = 8.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
ENV_NAMES = ("INFISICAL_ENVIRONMENT", "INFISICAL_ENV")
# ``infisical secrets get NAME`` exits 0 and answers a name it does not hold with this value
# (verified against the real CLI, OPS-818), so it means "absent", never a secret's value.
NOT_FOUND_PLACEHOLDERS = frozenset({"*not found*"})
# A value that is still a whole bare Infisical reference such as ${prod.some-folder.NAME}: the
# Fleet-OPS root is stored that way (OPS-818), and every read passes --expand=true so the server
# resolves it.  One that survives is an unresolved reference, never a credential.
UNRESOLVED_REFERENCE = re.compile(r"^\$\{[^}]+\}$")


ENV_TOKEN = "INFISICAL_TOKEN"
ENV_HELPER_HOME = "FLEET_SECRETS_HOME"
# FLEET_KNOBS_AUTH=session skips the machine identity and uses only the CLI's own session (the
# old behaviour).  For hermetic tests that run a fake CLI in a subprocess on a Mac whose System
# keychain holds the real identity;  anything else in it is ignored.
ENV_AUTH = "FLEET_KNOBS_AUTH"
HELPER_HOME = "~/.local/share/fleet-secrets"
HELPER_MODULE = "_fleet_knobs_fleet_secrets_core"


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
        "--expand=true",
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
        "--expand=true",
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


def _helper_candidates() -> list:
    """Where fleet-secrets' ``core.py`` may be, in order.  Paths only;  nothing is imported."""
    out = []
    home = (os.environ.get(ENV_HELPER_HOME) or "").strip()
    if home:
        out.append(os.path.join(os.path.expanduser(home), "fleet_secrets", "core.py"))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out.append(os.path.join(here, "fleet_secrets", "core.py"))
    out.append(os.path.join(os.path.expanduser(HELPER_HOME), "fleet_secrets", "core.py"))
    return out


def _load_helper() -> ModuleType | None:
    """fleet-secrets' core module, loaded from a file under a private name, or None."""
    cached = _HELPER_CACHE.get("core")
    if cached is not None:
        return cached
    for candidate in _helper_candidates():
        if not os.path.isfile(candidate):
            continue
        try:
            spec = importlib.util.spec_from_file_location(HELPER_MODULE, candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:                             # noqa: BLE001 - optional helper
            continue
        if all(hasattr(module, n) for n in ("auth_mode", "machine_token", "find_cli")):
            _HELPER_CACHE["core"] = module
            return module
    return None


_HELPER_CACHE: dict = {}
# Tests replace this with a callable that returns a fake helper, or None for "no helper".
HELPER_LOADER: Callable[[], ModuleType | None] = _load_helper


def child_env() -> dict[str, str]:
    """The environment for one CLI call, with a machine-identity token when one is available.

    ``INFISICAL_TOKEN`` already set wins.  Otherwise fleet-secrets' helper picks the mode;  a
    keychain machine identity is logged in and its token put in the returned dict only.  With
    no helper or no identity (mode ``session``) the CLI's own login session is used, as before.
    Raises FetchFailed (``keychain-incomplete``, ``login-failed``) when an identity exists but
    cannot be used, rather than quietly trying a session that cannot work under launchd."""
    env = dict(os.environ)
    env["INFISICAL_DISABLE_UPDATE_CHECK"] = "true"
    if (env.get(ENV_TOKEN) or "").strip():
        return env
    env.pop(ENV_TOKEN, None)
    if (env.get(ENV_AUTH) or "").strip().lower() == "session":
        return env
    try:
        helper = HELPER_LOADER()
    except Exception:                                 # noqa: BLE001 - optional helper
        helper = None
    if helper is None:
        return env
    try:
        mode = helper.auth_mode(env)
    except Exception:                                 # noqa: BLE001 - half-filled keychain
        raise FetchFailed("keychain-incomplete") from None
    if mode not in ("keychain", "system-keychain"):
        return env
    cli = find_cli()
    if not cli:
        raise FetchFailed("cli-missing")
    try:
        token = helper.machine_token(cli, env, mode)
    except Exception:                                 # noqa: BLE001 - never carry its text
        raise FetchFailed("login-failed") from None
    if not isinstance(token, str) or not token.strip():
        raise FetchFailed("login-failed")
    env[ENV_TOKEN] = token
    return env


def _run(argv: list):
    """Run the CLI and parse its JSON.  Raises FetchFailed with a reason token."""
    env = child_env()
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            cwd="/",
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise FetchFailed("timeout") from None
    except OSError:
        raise FetchFailed("cli-missing") from None
    finally:
        env = {}
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
            if isinstance(val, str) and val.strip().lower() in NOT_FOUND_PLACEHOLDERS:
                continue
            text = str(val)
            if isinstance(val, bool):
                text = "true" if val else "false"
            if UNRESOLVED_REFERENCE.match(text.strip()):
                # A bare ${prod.folder.NAME} that expansion left alone (target missing, or the
                # identity cannot read the referenced folder).  Fail closed on a reason token;
                # the key and the reference text are never carried (OPS-818).
                raise FetchFailed("unresolved-reference")
            found[key] = text
    return found
