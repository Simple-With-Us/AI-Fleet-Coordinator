"""Fetch Fleet-OPS secrets from Infisical through the installed ``infisical`` CLI.

Contract (same family as ``fleet_knobs/source.py`` and ``fleet_rag/infisical_settings.py``):

* Prod is the only environment.  Every call passes ``--env=prod`` itself, and a stray
  ``INFISICAL_ENVIRONMENT`` or ``INFISICAL_ENV`` other than prod is refused before anything runs.
* One fetch per process.  Values live in this process's memory and are never written to disk,
  a log, or stderr.  The CLI's stdout and stderr are captured, never echoed:  a failure is
  reduced to a short reason token (``cli-missing``, ``timeout``, ``exit-1``, ``bad-output``).
* Fail closed.  A value that is still an unresolved Infisical reference (``${...}``) stops the
  command and names the key, never the value.
* Auth, first match wins:  ``INFISICAL_TOKEN`` already in the environment ("token"), a
  universal-auth machine identity whose id and secret are in the login Keychain ("keychain"),
  the same pair in the System keychain ("system-keychain", for pm2 and other boot-time jobs
  that run in the launchd System session, where the login Keychain is unreachable), or the
  Infisical CLI's own login session ("session").  On Linux (the Hetzner server, which has no keychain) the
  server's universal-auth identity in ``INFISICAL_AUTOMATION_CLIENT_ID`` and ``_CLIENT_SECRET`` comes
  before "session" ("automation-env").  A machine-identity secret travels to the CLI
  by environment variable and is never put in an argument list.  The access token it returns
  goes to the export call by ``INFISICAL_TOKEN`` for the same reason.

Stdlib only, so a stable copy run with ``python3 -I`` needs nothing beside this package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path

ENVIRONMENT = "prod"
CLI_NAME = "infisical"
# A LaunchAgent's PATH is minimal, so look in the usual install places after PATH.
FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
SECURITY_BIN = "/usr/bin/security"
EXPORT_TIMEOUT = 30.0
API_BASE = "https://app.infisical.com/api"
ENV_API = "FLEET_SECRETS_API"
API_TIMEOUT = 30.0
MAX_VALUE_BYTES = 256 * 1024
# What `set` may write:  (folder, name pattern) pairs, and nothing else.  This list is code on purpose:
# widening it takes a reviewed change, never a flag, an environment variable or a config file.  A general
# write command would let any agent on this Mac overwrite any Fleet-OPS secret with the shared automation
# identity.  CACHE_<APP> in /linear is the minted Linear token that `board` stores there (AFC-359).
WRITE_ALLOWLIST: tuple[tuple[str, str], ...] = (("/linear", r"^CACHE_[A-Z0-9_]+$"),)
LOGIN_TIMEOUT = 30.0
KEYCHAIN_TIMEOUT = 10.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

ENV_PROJECT = "INFISICAL_FLEET_OPS_PROJECT_ID"
PROJECT_FILE = ".agent-sync/fleet-ops-project-id"
ENV_NAMES = ("INFISICAL_ENVIRONMENT", "INFISICAL_ENV")
# Read-only second project:  the FleetLink seat tokens (FLEETLINK_TOKEN_<SEAT>) live in AI Fleet Coordinator
# prod /fleetlink, not in Fleet-OPS.  `names --project ai-fleet-coordinator` lists them (names only).
DEFAULT_PROJECT = "fleet-ops"
PROJECT_SOURCES = {
    "fleet-ops": (ENV_PROJECT, PROJECT_FILE),
    "ai-fleet-coordinator": (
        "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID",
        ".agent-sync/ai-fleet-coordinator-project-id",
    ),
}

SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
KEYCHAIN_SERVICE = "fleet-secrets"
KEYCHAIN_CLIENT_ID = "infisical-client-id"
KEYCHAIN_CLIENT_SECRET = "infisical-client-secret"
ENV_TOKEN = "INFISICAL_TOKEN"
ENV_CLIENT_ID = "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"
# The server's own universal-auth identity (Linux only).  The machine's profile exports the pair.
ENV_AUTOMATION_ID = "INFISICAL_AUTOMATION_CLIENT_ID"
ENV_AUTOMATION_SECRET = "INFISICAL_AUTOMATION_CLIENT_SECRET"

MACHINE_MODES = ("keychain", "system-keychain", "automation-env")

# Auth variables of this tool:  never handed on to the command `run` starts.
AUTH_ENV_VARS = frozenset(
    {ENV_TOKEN, ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_AUTOMATION_ID, ENV_AUTOMATION_SECRET}
)

REF_RE = re.compile(r"^\$\{[^}]+\}$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# `run` never injects these, so a stray key in Infisical cannot redirect the loader, the
# interpreter search path or the shell of the command it starts.  `get` still returns them.
NOT_INJECTED_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "PWD",
        "OLDPWD",
        "IFS",
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
    }
)
NOT_INJECTED_PREFIXES = ("LD_", "DYLD_")


class SecretsError(Exception):
    """A failure with a short message that never holds a secret value.  `code` is the exit code."""

    def __init__(self, message: str, code: int = 4) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class UsageError(SecretsError):
    """The command line is wrong.  The CLI prints the usage text after the message."""

    def __init__(self, message: str) -> None:
        super().__init__(message, 2)


# --------------------------------------------------------------------------- configuration


def environment_refusal(env: Mapping[str, str]) -> str | None:
    """Why Infisical may not be read (a non-prod slug in the environment), or None."""
    for var in ENV_NAMES:
        slug = (env.get(var) or "").strip().lower()
        if slug and slug != ENVIRONMENT:
            return (
                "refusing Infisical environment %r from %s:  prod is the only environment "
                "(dev and staging are retired);  unset it or set it to prod;  see INFISICAL.md"
                % (slug, var)
            )
    return None


_LABELS = {"fleet-ops": "Fleet-OPS", "ai-fleet-coordinator": "AI Fleet Coordinator"}


def infisical_project_id(env: Mapping[str, str], home: Path, project: str = DEFAULT_PROJECT) -> str:
    """The Fleet-OPS Infisical project id, from env INFISICAL_FLEET_OPS_PROJECT_ID or the first line of
    ~/.agent-sync/fleet-ops-project-id.  Fails closed, naming both, and never echoes a malformed value.

    Same sources and error text as ``scripts/agent_sync/zulip.py:infisical_project_id`` (PR #634);
    this copy exists so a stable install needs no sibling package.  The id lives in the private
    fleet-ops inventory and is never committed to this public repo."""
    env_name, project_file = PROJECT_SOURCES[project]
    value = (env.get(env_name) or "").strip()
    if not value:
        try:
            value = (
                (home / project_file).read_text(encoding="utf-8").strip().splitlines()[0].strip()
            )
        except (OSError, UnicodeDecodeError, IndexError):
            value = ""
    if not value:
        raise SecretsError(
            "the %s Infisical project id is not set:  set %s or put it in ~/%s "
            "(it lives in the private fleet-ops inventory)" % (_LABELS[project], env_name, project_file)
        )
    if not _UUID_RE.match(value):
        raise SecretsError(
            "the %s Infisical project id (%s or ~/%s) is not a UUID" % (_LABELS[project], env_name, project_file)
        )
    return value


def home_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~"))


def find_cli(env: Mapping[str, str]) -> str | None:
    found = shutil.which(CLI_NAME, path=env.get("PATH") or os.defpath)
    if found:
        return found
    for directory in FALLBACK_DIRS:
        candidate = os.path.join(os.path.expanduser(directory), CLI_NAME)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def check_path(path: str) -> str:
    if not PATH_RE.match(path or "") or ".." in path.split("/"):
        raise UsageError("bad --path:  use an absolute Infisical folder path such as /")
    return path


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise UsageError("bad key name:  use letters, digits and underscores only")
    return name


def _base_env(env: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    out["INFISICAL_DISABLE_UPDATE_CHECK"] = "true"
    return out


# --------------------------------------------------------------------------- authentication


def login_keychain() -> str | None:
    """Path of the user's login keychain file, or None when there is none.

    Always searched by explicit path:  the default search list also holds the System keychain, so a
    path-less lookup would find System items and report `keychain` instead of `system-keychain`, or mix
    one item from each.  In the launchd System session the file exists but is locked, so the lookup simply
    fails and the System keychain is used."""
    base = os.path.expanduser("~/Library/Keychains")
    for name in ("login.keychain-db", "login.keychain"):
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def keychain_item(account: str, keychain: str | None) -> str | None:
    """The generic-password value for service fleet-secrets in ONE keychain file, or None when absent
    (`keychain` None means there is no such keychain).  Captured, never echoed."""
    if not keychain or not os.path.exists(SECURITY_BIN):
        return None
    argv = [
        SECURITY_BIN,
        "find-generic-password",
        "-s",
        KEYCHAIN_SERVICE,
        "-a",
        account,
        "-w",
        keychain,
    ]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = proc.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return value or None


def keychain_presence(keychain: str | None) -> tuple[bool, bool]:
    """(client id present, client secret present) in one keychain.  Booleans only."""
    return (
        keychain_item(KEYCHAIN_CLIENT_ID, keychain) is not None,
        keychain_item(KEYCHAIN_CLIENT_SECRET, keychain) is not None,
    )


def _complete_or_none(label: str, keychain: str | None) -> bool:
    have_id, have_secret = keychain_presence(keychain)
    if have_id and have_secret:
        return True
    if have_id or have_secret:
        raise SecretsError(
            "keychain-incomplete:  %s needs both accounts %s and %s under service %s, or neither"
            % (label, KEYCHAIN_CLIENT_ID, KEYCHAIN_CLIENT_SECRET, KEYCHAIN_SERVICE)
        )
    return False


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def automation_env_presence(env: Mapping[str, str]) -> tuple[bool, bool]:
    """(INFISICAL_AUTOMATION_CLIENT_ID set, INFISICAL_AUTOMATION_CLIENT_SECRET set).  Booleans only."""
    return (
        bool((env.get(ENV_AUTOMATION_ID) or "").strip()),
        bool((env.get(ENV_AUTOMATION_SECRET) or "").strip()),
    )


def auth_mode(env: Mapping[str, str]) -> str:
    """ "token", "keychain", "system-keychain", "automation-env" (Linux only) or "session".  Raises on a
    half-filled keychain or a half-set automation pair rather than downgrade."""
    if (env.get(ENV_TOKEN) or "").strip():
        return "token"
    if _complete_or_none("the login Keychain", login_keychain()):
        return "keychain"
    if _complete_or_none("the System keychain", SYSTEM_KEYCHAIN):
        return "system-keychain"
    if is_linux():
        have_id, have_secret = automation_env_presence(env)
        if have_id and have_secret:
            return "automation-env"
        if have_id or have_secret:
            raise SecretsError(
                "automation-env-incomplete:  set both %s and %s, or neither"
                % (ENV_AUTOMATION_ID, ENV_AUTOMATION_SECRET)
            )
    return "session"


def machine_token(cli: str, env: Mapping[str, str], mode: str = "keychain") -> str:
    """Log in with the mode's machine identity and return the access token (in memory only)."""
    if mode == "automation-env":
        client_id = (env.get(ENV_AUTOMATION_ID) or "").strip()
        client_secret = (env.get(ENV_AUTOMATION_SECRET) or "").strip()
        if not client_id or not client_secret:
            raise SecretsError("automation-env-incomplete")
    else:
        keychain = SYSTEM_KEYCHAIN if mode == "system-keychain" else login_keychain()
        client_id = keychain_item(KEYCHAIN_CLIENT_ID, keychain)
        client_secret = keychain_item(KEYCHAIN_CLIENT_SECRET, keychain)
        if not client_id or not client_secret:
            raise SecretsError("keychain-incomplete")
    child = _base_env(env)
    child[ENV_CLIENT_ID] = client_id
    child[ENV_CLIENT_SECRET] = client_secret
    child.pop(ENV_TOKEN, None)
    for var in (ENV_AUTOMATION_ID, ENV_AUTOMATION_SECRET):
        child.pop(var, None)
    argv = [cli, "login", "--method=universal-auth", "--plain", "--silent"]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=LOGIN_TIMEOUT,
            cwd="/",
            env=child,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SecretsError("login-timeout") from None
    except OSError:
        raise SecretsError("cli-missing") from None
    finally:
        client_id = client_secret = ""
        child = {}
    if proc.returncode != 0:
        raise SecretsError("login-failed:  exit-%d" % proc.returncode)
    try:
        token = proc.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise SecretsError("login-failed:  bad-output") from None
    if not token or len(token.splitlines()) != 1 or any(c.isspace() for c in token):
        raise SecretsError("login-failed:  bad-output")
    return token


def session_valid(cli: str, env: Mapping[str, str]) -> bool:
    """True when the CLI's own login session is signed in.  Only the exit code is used."""
    child = _base_env(env)
    for var in AUTH_ENV_VARS:
        child.pop(var, None)
    try:
        proc = subprocess.run(
            [cli, "login", "status", "--silent"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=LOGIN_TIMEOUT,
            cwd="/",
            env=child,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def cli_version(cli: str, env: Mapping[str, str]) -> str | None:
    try:
        proc = subprocess.run(
            [cli, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT,
            cwd="/",
            env=_base_env(env),
            check=False,
        )
        text = proc.stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", text)
    return m.group(0) if m else None


# --------------------------------------------------------------------------- fetch


def fetch_all(
    path: str, env: Mapping[str, str], project_name: str = DEFAULT_PROJECT
) -> dict[str, str]:
    """Every secret at `path` in Fleet-OPS prod, references expanded.  Raises SecretsError.

    No value is validated here beyond its type;  callers apply `reject_unresolved` to the
    ones they use."""
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    check_path(path)
    project = infisical_project_id(env, home_dir(env), project_name)
    cli = find_cli(env)
    if not cli:
        raise SecretsError("cli-missing:  the infisical CLI is not installed")
    mode = auth_mode(env)
    child = _base_env(env)
    if mode in MACHINE_MODES:
        child[ENV_TOKEN] = machine_token(cli, env, mode)
    if mode != "token":
        for var in AUTH_ENV_VARS - {ENV_TOKEN}:
            child.pop(var, None)
    argv = [
        cli,
        "export",
        "--env=%s" % ENVIRONMENT,
        "--projectId=%s" % project,
        "--path=%s" % path,
        "--format=json",
        "--silent",
    ]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=EXPORT_TIMEOUT,
            cwd="/",
            env=child,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SecretsError("timeout") from None
    except OSError:
        raise SecretsError("cli-missing") from None
    finally:
        child = {}
    if proc.returncode != 0:
        raise SecretsError("fetch-failed:  exit-%d" % proc.returncode)
    out = proc.stdout
    if len(out) > MAX_OUTPUT_BYTES:
        raise SecretsError("fetch-failed:  bad-output")
    try:
        payload = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SecretsError("fetch-failed:  bad-output") from None
    finally:
        out = b""
    return _pairs(payload)


def _pairs(payload) -> dict[str, str]:
    """Accept the shapes ``export --format=json`` has used:  {KEY: value}, a list of
    {key, value} or {secretKey, secretValue} objects, or an API-style {"secrets": [...]}."""
    if isinstance(payload, dict) and isinstance(payload.get("secrets"), list):
        payload = payload["secrets"]
    if isinstance(payload, dict):
        pairs = list(payload.items())
    elif isinstance(payload, list):
        pairs = []
        for item in payload:
            if not isinstance(item, dict):
                raise SecretsError("fetch-failed:  bad-output")
            pairs.append(
                (item.get("key", item.get("secretKey")), item.get("value", item.get("secretValue")))
            )
    else:
        raise SecretsError("fetch-failed:  bad-output")
    found: dict[str, str] = {}
    for key, val in pairs:
        if not isinstance(key, str) or not NAME_RE.match(key):
            continue  # not usable as an environment variable name
        if isinstance(val, bool):
            val = "true" if val else "false"
        if isinstance(val, (int, float)):
            val = str(val)
        if isinstance(val, str):
            found[key] = val
    return found


def reject_unresolved(secrets: Mapping[str, str], names: Iterable[str] | None = None) -> None:
    """Fail closed when a value is still an unresolved reference.  Names the key, never the value."""
    for key in sorted(names if names is not None else secrets):
        val = secrets.get(key)
        if val is not None and REF_RE.match(val):
            raise SecretsError(
                "unresolved-reference:  %s came back as an unexpanded Infisical reference" % key, 5
            )


def injectable(name: str) -> bool:
    """False for a name `run` must never put in a child's environment:  loader and interpreter
    variables, and every INFISICAL_* name (the root path mirrors the old bootstrap layer, which held
    INFISICAL_TOKEN and client id and secret keys, and those must not reach the command)."""
    return (
        name not in NOT_INJECTED_EXACT
        and name not in AUTH_ENV_VARS
        and not name.startswith(NOT_INJECTED_PREFIXES)
        and not name.upper().startswith("INFISICAL_")
    )


def _api(method: str, path: str, token: str, body: dict | None, env: Mapping[str, str]) -> int:
    """One Infisical REST call.  Returns the HTTP status;  the response body is never read into a message.
    The token travels in a header, never in an argument list."""
    base = (env.get(ENV_API) or API_BASE).rstrip("/")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "fleet-secrets",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0
    finally:
        data = None


def check_writable(name: str, path: str) -> None:
    """Raise SecretsError (exit 2) unless (path, name) is on the built-in write allowlist."""
    for folder, pattern in WRITE_ALLOWLIST:
        if path == folder and re.match(pattern, name):
            return
    raise SecretsError(
        "set refused:  %s at %s is not on the built-in write allowlist (%s);  widening it needs a code change"
        % (name, path, ", ".join("%s %s" % (f, p) for f, p in WRITE_ALLOWLIST)),
        2,
    )


def put_secret(name: str, path: str, value: str, env: Mapping[str, str]) -> str:
    """Create or update one Fleet-OPS prod secret at `path`, and return "created" or "updated".

    Only names on WRITE_ALLOWLIST are accepted.  Needs a machine identity (token, keychain, system-keychain or automation-env):  the CLI's own login session
    cannot hand its token to a REST call, so mode `session` is refused.  The value never appears in an argument list, a message or a log."""
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    check_name(name)
    check_path(path)
    check_writable(name, path)
    if "\0" in value or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise SecretsError("value is empty-unsafe or larger than %d bytes" % MAX_VALUE_BYTES, 2)
    project = infisical_project_id(env, home_dir(env))
    cli = find_cli(env)
    if not cli:
        raise SecretsError("cli-missing:  the infisical CLI is not installed")
    mode = auth_mode(env)
    if mode == "session":
        raise SecretsError(
            "write-needs-machine-identity:  set needs a machine identity (keychain or INFISICAL_AUTOMATION_*) or INFISICAL_TOKEN",
            4,
        )
    token = env[ENV_TOKEN].strip() if mode == "token" else machine_token(cli, env, mode)
    body = {
        "workspaceId": project,
        "environment": ENVIRONMENT,
        "secretPath": path,
        "secretValue": value,
        "type": "shared",
    }
    try:
        quoted = urllib.request.quote(name, safe="")
        # `set` never creates folders:  the allowlist names a folder that already exists.  A missing folder
        # (404 on a listing of it) is an error and nothing is written.
        probe = _api(
            "GET",
            "/v3/secrets/raw?workspaceId=%s&environment=%s&secretPath=%s&recursive=false&include_imports=false"
            % (project, ENVIRONMENT, urllib.request.quote(path, safe="/")),
            token,
            None,
            env,
        )
        if probe == 404:
            raise SecretsError(
                "folder-missing:  %s does not exist in Fleet-OPS;  nothing was written" % path, 4
            )
        if probe != 200:
            raise SecretsError("write-failed:  folder-check http-%d" % probe, 4)
        status = _api("PATCH", "/v3/secrets/raw/" + quoted, token, body, env)
        if status in (200, 201):
            return "updated"
        if status in (404, 400):
            status = _api("POST", "/v3/secrets/raw/" + quoted, token, body, env)
            if status in (200, 201):
                return "created"
        raise SecretsError("write-failed:  http-%d" % status)
    finally:
        body = {}
        token = ""
