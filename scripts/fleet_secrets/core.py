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
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
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
WRITE_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("/linear", r"^CACHE_[A-Z0-9_]+$"),
    # The CLI logins a seat shares with the hosts it runs on:  one folder per seat under
    # /cli-logins,  and only the names a CLI actually reads out of its environment.  The
    # value is the seat's own CLI login,  so the write is the seat's own credential going
    # where the seat already uses it,  and it is never a general-purpose write:  the path
    # names the seat and the name is one of four.
    (
        r"/cli-logins/[A-Z][A-Z0-9-]{0,15}",
        r"^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|COPILOT_GITHUB_TOKEN)$",
    ),
    # The same class the other way round:  one folder,  the seat in the name.  A consumer
    # whose own table is static  (BotFleet's secret map names one secret per bot)  reads
    # this shape,  and the reader renames the value to the environment variable the CLI
    # expects.
    (
        r"/cli-logins",
        r"^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|COPILOT_GITHUB_TOKEN)_[A-Z][A-Z0-9-]{0,15}$",
    ),
)
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
                f"refusing Infisical environment {slug!r} from {var}:  prod is the only environment "
                "(dev and staging are retired);  unset it or set it to prod;  see INFISICAL.md"
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
            f"the {_LABELS[project]} Infisical project id is not set:  set {env_name} or put it in ~/{project_file} "
            "(it lives in the private fleet-ops inventory)"
        )
    if not _UUID_RE.match(value):
        raise SecretsError(
            f"the {_LABELS[project]} Infisical project id ({env_name} or ~/{project_file}) is not a UUID"
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
    # fullmatch:  `$` also matches before a final newline, so re.match would pass "/x\n".
    if not PATH_RE.fullmatch(path or "") or ".." in path.split("/"):
        raise UsageError("bad --path:  use an absolute Infisical folder path such as /")
    return path


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise UsageError("bad key name:  use letters, digits and underscores only")
    return name


def _base_env(env: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    # Automation credentials are resolved by machine_token from the caller's mapping,
    # then translated to the CLI's universal-auth names only for the login subprocess.
    out.pop(ENV_AUTOMATION_ID, None)
    out.pop(ENV_AUTOMATION_SECRET, None)
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
            f"keychain-incomplete:  {label} needs both accounts {KEYCHAIN_CLIENT_ID} and {KEYCHAIN_CLIENT_SECRET} under service {KEYCHAIN_SERVICE}, or neither"
        )
    return False


def is_linux() -> bool:
    return sys.platform.startswith("linux")


LINUX_ROOT_PRIVATE_BOOTSTRAP_DIR_PARTS = ("root", ".config", "fleet-secrets")
LINUX_ROOT_PRIVATE_BOOTSTRAP_BASENAME = "bootstrap.json"
LINUX_ROOT_PRIVATE_BOOTSTRAP_MAX_BYTES = 16384
LINUX_ROOT_PRIVATE_BOOTSTRAP_ERROR = "root-private bootstrap is invalid or unsafe"


def _linux_root_private_bootstrap_pairs(pairs: list[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate bootstrap key")
        result[name] = value
    return result


def load_linux_root_private_bootstrap() -> dict[str, str]:
    """Approved on-disk fallback for Linux UID 0 only (INFISICAL.md).

    Reads the fixed existing file under /root/.config/fleet-secrets/ using descriptor-scoped
    opens that refuse symlinks.  This is the only fleet-secrets code path that reads a credential
    file;  every other source is process environment or Infisical.
    """
    if not is_linux() or os.geteuid() != 0:
        return {}
    fd: int | None = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for name in LINUX_ROOT_PRIVATE_BOOTSTRAP_DIR_PARTS:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("unsafe bootstrap directory")
            if name == "fleet-secrets" and stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("unsafe bootstrap parent")
        child = os.open(
            LINUX_ROOT_PRIVATE_BOOTSTRAP_BASENAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=fd,
        )
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("unsafe bootstrap file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            text = stream.read(LINUX_ROOT_PRIVATE_BOOTSTRAP_MAX_BYTES + 1)
        if len(text) > LINUX_ROOT_PRIVATE_BOOTSTRAP_MAX_BYTES:
            raise ValueError("oversized bootstrap")
        data = json.loads(text, object_pairs_hook=_linux_root_private_bootstrap_pairs)
        expected = {ENV_AUTOMATION_ID, ENV_AUTOMATION_SECRET}
        if (
            not isinstance(data, dict)
            or set(data) != expected
            or any(not isinstance(v, str) or not v.strip() for v in data.values())
        ):
            raise ValueError("invalid bootstrap schema")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError) as exc:
        raise SecretsError(LINUX_ROOT_PRIVATE_BOOTSTRAP_ERROR) from exc
    finally:
        if fd is not None:
            os.close(fd)


def automation_env_presence(env: Mapping[str, str]) -> tuple[bool, bool]:
    """(INFISICAL_AUTOMATION_CLIENT_ID set, INFISICAL_AUTOMATION_CLIENT_SECRET set).  Booleans only."""
    return (
        bool((env.get(ENV_AUTOMATION_ID) or "").strip()),
        bool((env.get(ENV_AUTOMATION_SECRET) or "").strip()),
    )


def auth_mode(env: Mapping[str, str]) -> str:
    """ "token", "automation-env" (Linux only), "keychain", "system-keychain" or "session", in that order:
    an identity handed to the process in its environment always beats a keychain one.  Raises on a
    half-filled keychain or a half-set automation pair rather than downgrade."""
    if (env.get(ENV_TOKEN) or "").strip():
        return "token"
    if is_linux():
        have_id, have_secret = automation_env_presence(env)
        if have_id and have_secret:
            return "automation-env"
        if have_id or have_secret:
            raise SecretsError(
                f"automation-env-incomplete:  set both {ENV_AUTOMATION_ID} and {ENV_AUTOMATION_SECRET}, or neither"
            )
    if _complete_or_none("the login Keychain", login_keychain()):
        return "keychain"
    if _complete_or_none("the System keychain", SYSTEM_KEYCHAIN):
        return "system-keychain"
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
        raise SecretsError(f"login-failed:  exit-{proc.returncode}")
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
    path: str,
    env: Mapping[str, str],
    project_name: str = DEFAULT_PROJECT,
    token: str | None = None,
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
        #  The token the caller already resolved is used as it is,  so a read that
        #  follows a write does not log in a second time.
        child[ENV_TOKEN] = token if token is not None else machine_token(cli, env, mode)
    if mode != "token":
        for var in AUTH_ENV_VARS - {ENV_TOKEN}:
            child.pop(var, None)
    argv = [
        cli,
        "export",
        f"--env={ENVIRONMENT}",
        f"--projectId={project}",
        f"--path={path}",
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
        raise SecretsError(f"fetch-failed:  exit-{proc.returncode}")
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


# One raw-secret row is a few KB; 16 MiB of "secret" is an attack or a bug.
MAX_VERIFIED_BYTES = 1024 * 1024


def fetch_verified(
    name: str,
    path: str,
    env: Mapping[str, str],
    token: str | None = None,
) -> str:
    """One folder-PROVEN secret, through the REST raw-secret endpoint.

    `fetch_all` cannot prove provenance:  the CLI export row's secretPath
    comes back empty, and a token scoped to one folder silently clamps a
    --path it cannot read to its own.  Here the ANSWER's own fields must
    show the row came from exactly (Fleet-OPS, prod, `path`):  workspace,
    environment, secretPath, secretKey, and type "shared" (a personal
    override is exactly the shadow-precedence hazard).  Any missing or
    mismatched field, a hidden or non-string value, an unresolved
    reference, an oversized answer or a redirect fails closed.  The value
    never appears in a message or a log;  errors name the key, the folder,
    or the HTTP status only.  Exit 3 when the key is absent at that path.
    """
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    check_name(name)
    check_path(path)
    project = infisical_project_id(env, home_dir(env))
    if token is None:
        # The CLI is only needed to mint a token,  so a caller that already
        # has one does not need it on this host.
        cli = find_cli(env)
        if not cli:
            raise SecretsError("cli-missing:  the infisical CLI is not installed", 4)
        mode = auth_mode(env)
        if mode == "session":
            raise SecretsError(
                "read-needs-machine-identity:  get-verified needs a machine identity "
                "(keychain or INFISICAL_AUTOMATION_*) or INFISICAL_TOKEN",
                4,
            )
        token = resolve_token(env, cli, mode)
    query = urllib.parse.urlencode(
        {"workspaceId": project, "environment": ENVIRONMENT, "secretPath": path}
    )
    status, answer = _api_json(
        "GET",
        "/v3/secrets/raw/{}?{}".format(urllib.request.quote(name, safe=""), query),
        token,
        None,
        env,
        max_bytes=MAX_VERIFIED_BYTES,
    )
    if status in (400, 404):
        raise SecretsError(f"key {name} is not in Infisical at {path}", 3)
    if status != 200:
        detail = "unreachable" if status == 0 else f"status-{status}"
        raise SecretsError(f"fetch-failed:  {detail} for {name} at {path}", 4)
    if not isinstance(answer, dict):
        raise SecretsError(f"fetch-failed:  bad-output for {name} at {path}", 4)
    secret = answer.get("secret")
    if not isinstance(secret, dict):
        raise SecretsError(f"unverified-source:  no secret row for {name} at {path}", 4)
    checks = (
        ("secretKey", name),
        ("secretPath", path),
        ("workspace", project),
        ("environment", ENVIRONMENT),
        ("type", "shared"),
    )
    for field, want in checks:
        if secret.get(field) != want:
            raise SecretsError(f"unverified-source:  {name} at {path} ({field})", 4)
    if "secretValueHidden" not in secret or secret["secretValueHidden"]:
        raise SecretsError(f"unverified-source:  {name} at {path} (secretValueHidden)", 4)
    value = secret.get("secretValue")
    if not isinstance(value, str) or not value:
        raise SecretsError(f"unverified-source:  {name} at {path} (secretValue)", 4)
    reject_unresolved({name: value}, [name])
    return value


def reject_unresolved(secrets: Mapping[str, str], names: Iterable[str] | None = None) -> None:
    """Fail closed when a value is still an unresolved reference.  Names the key, never the value."""
    for key in sorted(names if names is not None else secrets):
        val = secrets.get(key)
        if val is not None and REF_RE.match(val):
            raise SecretsError(
                f"unresolved-reference:  {key} came back as an unexpanded Infisical reference", 5
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect.

    The bearer token travels in a header,  so a redirect to another origin would hand
    the credential to whoever the response named.  The pinned API does not redirect,
    so a 3xx is a failure and not a hop.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    """The opener every call uses:  the defaults,  and no redirect following."""
    return urllib.request.build_opener(_NoRedirect())


def _api_json(
    method: str,
    path: str,
    token: str,
    body: dict | None,
    env: Mapping[str, str],
    max_bytes: int | None = None,
) -> tuple[int, object]:
    """One Infisical REST call.  The status and the parsed body,  never a message.

    The body comes back for the calls that have to read it,  like the folder create,
    whose 400 says whether the folder is already there or the request was wrong.  The
    token travels in a header, never in an argument list.

    `max_bytes`, when set, bounds the socket read itself (not a post-read length
    check):  an oversized answer fails closed as (0, None) without the body being
    buffered whole.  Existing callers pass nothing and keep the unbounded read.
    """
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
    limit = None if max_bytes is None else max_bytes + 1
    try:
        with _opener().open(req, timeout=API_TIMEOUT) as resp:
            raw = resp.read(limit)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(limit)
        status = exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None
    finally:
        data = None
    if max_bytes is not None and len(raw) > max_bytes:
        return 0, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


def _api(method: str, path: str, token: str, body: dict | None, env: Mapping[str, str]) -> int:
    """One Infisical REST call.  Returns the HTTP status;  the response body is never read into a message.
    The token travels in a header, never in an argument list."""
    return _api_json(method, path, token, body, env)[0]


def check_writable(name: str, path: str) -> None:
    """Raise SecretsError (exit 2) unless (path, name) is on the built-in write allowlist.

    The folder is matched whole,  so a pattern like ``/cli-logins/[A-Z][A-Z0-9-]{0,15}``
    names one seat's folder and not a prefix of it.
    """
    for folder, pattern in WRITE_ALLOWLIST:
        if re.fullmatch(folder, path) and re.match(pattern, name):
            return
    raise SecretsError(
        "set refused:  {} at {} is not on the built-in write allowlist ({});  widening it needs a code change".format(
            name, path, ", ".join(f"{f} {p}" for f, p in WRITE_ALLOWLIST)
        ),
        2,
    )


def ensure_folder(path: str, env: Mapping[str, str], token: str) -> None:
    """Make sure the folder path exists,  creating the levels that do not.

    ``put_secret`` refuses to write into a folder that is not there,  which is the right
    rule for a secret but not for a path the fleet owns the shape of:  a seat's own
    folder is not a secret,  and every seat should not need a human to create one
    before its first sync.  A folder that is already there is not an error,  and the
    400 that says so is read rather than assumed,  so a rejected request is not
    mistaken for a folder that is there.

    The request carries the folder name and the parent path,  never a value,  and the
    token is the one the caller already resolved.
    """
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    project = infisical_project_id(env, home_dir(env))
    parts = [part for part in path.split("/") if part]
    parent = "/"
    for part in parts:
        body = {
            "workspaceId": project,
            "environment": ENVIRONMENT,
            "name": part,
            "path": parent,
        }
        status, answer = _api_json("POST", "/v1/folders", token, body, env)
        if status in (200, 201):
            parent = parent.rstrip("/") + "/" + part
            continue
        if status in (400, 409) and _is_existing_folder(answer):
            parent = parent.rstrip("/") + "/" + part
            continue
        raise SecretsError(f"folder-failed:  http-{status} for {part}", 4)


def _is_existing_folder(answer: object) -> bool:
    """True when the API's refusal says the folder is already there."""
    if not isinstance(answer, dict):
        return False
    message = str(answer.get("message") or answer.get("error") or "")
    return "already exists" in message.lower()


def resolve_token(env: Mapping[str, str], cli: str | None = None, mode: str | None = None) -> str:
    """The token this host authenticates with,  resolved once.

    A plain token in the environment is the token,  and the machine modes mint one
    through the CLI's own login.  Asking for the token once and passing it on is what
    keeps a sync from logging in three times.
    """
    if mode is None:
        mode = auth_mode(env)
    if mode == "token":
        token = (env.get(ENV_TOKEN) or "").strip()
        if not token:
            raise SecretsError("token-missing:  INFISICAL_TOKEN is not set", 4)
        return token
    if cli is None:
        cli = find_cli(env)
    if not cli:
        raise SecretsError("cli-missing:  the infisical CLI is not installed", 4)
    return machine_token(cli, env, mode)


def put_secret(
    name: str, path: str, value: str, env: Mapping[str, str], token: str | None = None
) -> str:
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
        raise SecretsError(f"value is empty-unsafe or larger than {MAX_VALUE_BYTES} bytes", 2)
    project = infisical_project_id(env, home_dir(env))
    if token is None:
        # The CLI is only needed to mint a token,  so a caller that already has one
        # does not need it on this host.
        cli = find_cli(env)
        if not cli:
            raise SecretsError("cli-missing:  the infisical CLI is not installed", 4)
        mode = auth_mode(env)
        if mode == "session":
            raise SecretsError(
                "write-needs-machine-identity:  set needs a machine identity (keychain or "
                "INFISICAL_AUTOMATION_*) or INFISICAL_TOKEN",
                4,
            )
        token = resolve_token(env, cli, mode)
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
            "/v3/secrets/raw?workspaceId={}&environment={}&secretPath={}&recursive=false&include_imports=false".format(
                project, ENVIRONMENT, urllib.request.quote(path, safe="/")
            ),
            token,
            None,
            env,
        )
        if probe == 404:
            raise SecretsError(
                f"folder-missing:  {path} does not exist in Fleet-OPS;  nothing was written", 4
            )
        if probe != 200:
            raise SecretsError(f"write-failed:  folder-check http-{probe}", 4)
        status = _api("PATCH", "/v3/secrets/raw/" + quoted, token, body, env)
        if status in (200, 201):
            return "updated"
        if status in (404, 400):
            status = _api("POST", "/v3/secrets/raw/" + quoted, token, body, env)
            if status in (200, 201):
                return "created"
        raise SecretsError(f"write-failed:  http-{status}")
    finally:
        body = {}
        token = ""
