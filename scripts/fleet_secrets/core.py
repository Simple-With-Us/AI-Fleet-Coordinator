"""Scoped Infisical service-token reads and allowlisted writes.

Only explicit process bindings are consumed: INFISICAL_TOKEN is read-only and
INFISICAL_WRITE_TOKEN is a separate writer.  No Keychain, Automation identity,
CLI login session or credential-file fallback runs.  Values stay in memory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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

# Retired Keychain identifiers retained for compatibility callers; this module never reads them.
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
KEYCHAIN_SERVICE = "fleet-secrets"
KEYCHAIN_CLIENT_ID = "infisical-client-id"
KEYCHAIN_CLIENT_SECRET = "infisical-client-secret"
ENV_TOKEN = "INFISICAL_TOKEN"
ENV_AI_FLEET_COORDINATOR_TOKEN = "INFISICAL_AI_FLEET_COORDINATOR_TOKEN"
ENV_WRITE_TOKEN = "INFISICAL_WRITE_TOKEN"
MAX_NAMED_READS = 8
ENV_READ_PATHS = "INFISICAL_READ_PATHS"
ENV_WRITE_PATHS = "INFISICAL_WRITE_PATHS"
ENV_CLIENT_ID = "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"
# Retired universal-auth variable names are stripped from helper environments.
ENV_AUTOMATION_ID = "INFISICAL_AUTOMATION_CLIENT_ID"
ENV_AUTOMATION_SECRET = "INFISICAL_AUTOMATION_CLIENT_SECRET"

MACHINE_MODES = ()

# Auth variables of this tool:  never handed on to the command `run` starts.
AUTH_ENV_VARS = frozenset(
    {
        ENV_TOKEN,
        ENV_AI_FLEET_COORDINATOR_TOKEN,
        ENV_WRITE_TOKEN,
        ENV_CLIENT_ID,
        ENV_CLIENT_SECRET,
        ENV_AUTOMATION_ID,
        ENV_AUTOMATION_SECRET,
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
        "INFISICAL_SHARED_CLIENT_ID",
        "INFISICAL_SHARED_CLIENT_SECRET",
    }
)

REF_RE = re.compile(r"\$\{[^}]+\}")
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
    if not PATH_RE.fullmatch(path or "") or any(part in (".", "..") for part in path.split("/")):
        raise UsageError("bad --path:  use an absolute Infisical folder path such as /")
    return path


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise UsageError("bad key name:  use letters, digits and underscores only")
    return name


def _base_env(env: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    # An explicit interactive admin session must not inherit an automation identity or
    # service token that could become an implicit fallback.
    for name in AUTH_ENV_VARS:
        out.pop(name, None)
    out["INFISICAL_DISABLE_UPDATE_CHECK"] = "true"
    return out


# --------------------------------------------------------------------------- authentication


def login_keychain() -> None:
    """Compatibility seam; implicit Keychain lookup is retired."""
    return


def keychain_presence(keychain: str | None) -> tuple[bool, bool]:
    return False, False


def automation_env_presence(env: Mapping[str, str]) -> tuple[bool, bool]:
    return False, False


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def auth_mode(env: Mapping[str, str]) -> str:
    return "token" if (env.get(ENV_TOKEN) or "").strip() else "missing"


def machine_token(*args, **kwargs) -> str:
    raise SecretsError("service-token-required: implicit machine authentication is retired")


def session_valid(cli: str, env: Mapping[str, str]) -> bool:
    return False


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
    allow_session: bool = False,
    writer: bool = False,
) -> dict[str, str]:
    """Read one declared folder using its read token unless writer is explicitly requested."""
    project = infisical_project_id(env, home_dir(env), project_name)
    if (
        token is None
        and not (env.get(project_token_name(project_name)) or "").strip()
        and allow_session
    ):
        return fetch_with_cli_session(project, path, env)
    return fetch_project(project, path, env, token=token, writer=writer)


def project_token_name(project_name: str) -> str:
    if project_name == "fleet-ops":
        return ENV_TOKEN
    if project_name == "ai-fleet-coordinator":
        return ENV_AI_FLEET_COORDINATOR_TOKEN
    raise UsageError("unknown Infisical project")


def project_name_for_id(project_id: str, env: Mapping[str, str]) -> str:
    for name in PROJECT_SOURCES:
        try:
            if infisical_project_id(env, home_dir(env), name) == project_id:
                return name
        except SecretsError:
            continue
    raise SecretsError("project-token-binding-missing: project id has no configured token binding")


def fetch_with_cli_session(project: str, path: str, env: Mapping[str, str]) -> dict[str, str]:
    """Explicit interactive read-only admin route; automation must pass a scoped token."""
    if not sys.stdin.isatty():
        raise SecretsError("service-token-required: noninteractive reads require a scoped token")
    cli = find_cli(env)
    if not cli:
        raise SecretsError("cli-missing")
    child = _base_env(env)
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
            stdin=None,
            capture_output=True,
            timeout=EXPORT_TIMEOUT,
            cwd="/",
            env=child,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise SecretsError("interactive-session-read-failed") from None
    if proc.returncode or len(proc.stdout) > MAX_OUTPUT_BYTES:
        raise SecretsError("interactive-session-read-failed")
    try:
        return _pairs(json.loads(proc.stdout.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        raise SecretsError("fetch-failed: bad-output") from None


def declared_paths(env: Mapping[str, str], path: str, *, writer: bool = False) -> tuple[str, ...]:
    name = ENV_WRITE_PATHS if writer else ENV_READ_PATHS
    raw = env.get(name)
    paths = (
        tuple(check_path(item.strip()) for item in raw.split(","))
        if raw is not None
        else (check_path(path),)
    )
    if not paths or len(paths) > 20 or len(paths) != len(set(paths)) or path not in paths:
        raise SecretsError("token-scope-mismatch: declared folders do not cover this operation")
    return paths


def validate_service_metadata(
    metadata: object, project: str, paths: Iterable[str], permissions: Iterable[str]
) -> None:
    """Validate the token's own grant, never a successful secret query."""
    if (
        not isinstance(metadata, dict)
        or metadata.get("workspace") != project
        or ("projectId" in metadata and metadata.get("projectId") != project)
    ):
        raise SecretsError("token-scope-mismatch")
    actual_permissions = metadata.get("permissions")
    scopes = metadata.get("scopes")
    if (
        not isinstance(actual_permissions, list)
        or any(not isinstance(item, str) for item in actual_permissions)
        or len(actual_permissions) != len(set(actual_permissions))
        or set(actual_permissions) != set(permissions)
        or not isinstance(scopes, list)
    ):
        raise SecretsError("token-scope-mismatch")
    actual_paths = []
    for scope in scopes:
        if (
            not isinstance(scope, dict)
            or scope.get("environment") != ENVIRONMENT
            or not isinstance(scope.get("secretPath"), str)
        ):
            raise SecretsError("token-scope-mismatch")
        actual_paths.append(scope["secretPath"])
    if len(actual_paths) != len(set(actual_paths)) or set(actual_paths) != set(paths):
        raise SecretsError("token-scope-mismatch")
    expiry = metadata.get("expiresAt")
    if expiry is not None:
        try:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):  # noqa: UP017 - Imported by Python 3.9 fleet knobs.
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SecretsError("token-expired-or-invalid") from None


def check_service_token(token: str) -> str:
    if (
        not isinstance(token, str)
        or not token.startswith("st.")
        or not 20 <= len(token) <= 8192
        or any(c.isspace() or c == "\0" for c in token)
    ):
        raise SecretsError("service-token-required: bind a scoped Infisical service token")
    return token


def verify_service_token(
    token: str,
    project: str,
    path: str,
    env: Mapping[str, str],
    *,
    writer: bool = False,
    deadline: float | None = None,
) -> tuple[str, ...]:
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    paths = declared_paths(env, path, writer=writer)
    check_service_token(token)
    status, metadata = _api_json(
        "GET", "/v2/service-token", token, None, env, max_bytes=128 * 1024, deadline=deadline
    )
    if status != 200:
        raise SecretsError("token-verification-failed")
    validate_service_metadata(metadata, project, paths, ("read", "write") if writer else ("read",))
    return paths


def secret_query(project: str, path: str) -> str:
    return urllib.parse.urlencode(
        {
            "workspaceId": project,
            "environment": ENVIRONMENT,
            "secretPath": path,
            "viewSecretValue": "true",
            "expandSecretReferences": "false",
            "recursive": "false",
            "include_imports": "false",
        }
    )


def validate_secret_record(
    record: object, project: str, path: str, *, single_scope: bool = True
) -> tuple[str, str]:
    if (
        not isinstance(record, dict)
        or record.get("type") != "shared"
        or record.get("secretValueHidden") is True
    ):
        raise SecretsError("unverified-source")
    for field, expected in (
        ("workspace", project),
        ("environment", ENVIRONMENT),
        ("secretPath", path),
    ):
        if record.get(field) != expected and not (single_scope and field not in record):
            raise SecretsError("unverified-source")
    key, value = record.get("secretKey"), record.get("secretValue")
    if not isinstance(key, str) or not NAME_RE.fullmatch(key) or not isinstance(value, str):
        raise SecretsError("fetch-failed: bad-output")
    return key, value


def fetch_project(
    project: str,
    path: str,
    env: Mapping[str, str],
    *,
    token: str | None = None,
    writer: bool = False,
    names: Iterable[str] | None = None,
    timeout: float = EXPORT_TIMEOUT,
) -> dict[str, str]:
    """Explicit project route shared with standalone recall and knobs consumers."""
    check_path(path)
    if token is None:
        project_name = project_name_for_id(project, env)
        token = resolve_token(env, writer=writer, project_name=project_name)
    deadline = time.monotonic() + timeout
    paths = verify_service_token(token, project, path, env, writer=writer, deadline=deadline)
    wanted = None if names is None else set(names)
    found: dict[str, str] = {}
    if wanted is None:
        endpoints = [(None, "/v3/secrets/raw?" + secret_query(project, path))]
    else:
        endpoints = [
            (
                check_name(name),
                "/v3/secrets/raw/"
                + urllib.parse.quote(name, safe="")
                + "?"
                + secret_query(project, path),
            )
            for name in sorted(wanted)
        ]

    # A whole folder may be large.  One named secret is small, and up to MAX_NAMED_READS run at once.
    max_bytes = MAX_OUTPUT_BYTES if wanted is None else MAX_VERIFIED_BYTES

    def read_one(item: tuple) -> tuple:
        name, endpoint = item
        status, payload = _api_json(
            "GET", endpoint, token, None, env, max_bytes=max_bytes, deadline=deadline
        )
        if name is not None and status == 404:
            return name, []
        if status != 200 or not isinstance(payload, dict):
            raise SecretsError("fetch-failed")
        records = payload.get("secrets") if name is None else [payload.get("secret")]
        if not isinstance(records, list):
            raise SecretsError("fetch-failed: bad-output")
        pairs = []
        for record in records:
            key, value = validate_secret_record(record, project, path, single_scope=len(paths) == 1)
            if name is not None and key != name:
                raise SecretsError("unverified-source")
            pairs.append((key, value))
        return name, pairs

    # Named reads share one deadline, and the knobs ask for every registered name at once, so they
    # run side by side:  one after another, 31 names took about 13 s against an 8 s budget.  Each
    # answer is checked as it arrives, and the first bad one cancels every read not yet started;
    # a read already in flight may still finish, but its answer is never used.
    answers: dict = {}
    with ThreadPoolExecutor(max_workers=MAX_NAMED_READS) as pool:
        futures = [pool.submit(read_one, item) for item in endpoints]
        try:
            for future in as_completed(futures):
                name, pairs = future.result()
                answers[name] = pairs
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    for expected_name, _endpoint in endpoints:
        for key, value in answers[expected_name]:
            if key in found:
                raise SecretsError("unverified-source")
            found[key] = value
    reject_unresolved(found)
    return found


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
    token = token if token is not None else resolve_token(env)
    verify_service_token(token, project, path, env)
    query = secret_query(project, path)
    status, answer = _api_json(
        "GET",
        "/v3/secrets/raw/{}?{}".format(urllib.parse.quote(name, safe=""), query),
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
        if val is not None and REF_RE.search(val):
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
    deadline: float | None = None,
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
    max_bytes = MAX_OUTPUT_BYTES if max_bytes is None else max_bytes
    limit = max_bytes + 1
    try:
        timeout = API_TIMEOUT if deadline is None else min(API_TIMEOUT, deadline - time.monotonic())
        if timeout <= 0:
            return 0, None
        with _opener().open(req, timeout=timeout) as resp:
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
    verify_service_token(token, project, path, env, writer=True)
    status, _ = _api_json(
        "GET",
        "/v3/secrets/raw?" + secret_query(project, path),
        token,
        None,
        env,
        max_bytes=MAX_OUTPUT_BYTES,
    )
    if status != 200:
        raise SecretsError("folder-missing-or-inaccessible: provisioning folders is separate")


def _is_existing_folder(answer: object) -> bool:
    """True when the API's refusal says the folder is already there."""
    if not isinstance(answer, dict):
        return False
    message = str(answer.get("message") or answer.get("error") or "")
    return "already exists" in message.lower()


def resolve_token(
    env: Mapping[str, str],
    cli: str | None = None,
    mode: str | None = None,
    *,
    writer: bool = False,
    project_name: str = DEFAULT_PROJECT,
) -> str:
    """Resolve one explicit binding, without cross-purpose or identity fallback."""
    if writer and project_name != DEFAULT_PROJECT:
        raise SecretsError("write-token-project-mismatch: writes are configured for Fleet-OPS only")
    name = ENV_WRITE_TOKEN if writer else project_token_name(project_name)
    token = (env.get(name) or "").strip()
    if not token:
        raise SecretsError(f"service-token-required: {name} is not set")
    return check_service_token(token)


def put_secret(
    name: str, path: str, value: str, env: Mapping[str, str], token: str | None = None
) -> str:
    """Create or update one Fleet-OPS prod secret at `path`, and return "created" or "updated".

    Only names on WRITE_ALLOWLIST are accepted.  A separate scoped writer token is required and its grant metadata is verified before the request.  The value never appears in an argument list, a message or a log."""
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    check_name(name)
    check_path(path)
    check_writable(name, path)
    if "\0" in value or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise SecretsError(f"value is empty-unsafe or larger than {MAX_VALUE_BYTES} bytes", 2)
    project = infisical_project_id(env, home_dir(env))
    token = token if token is not None else resolve_token(env, writer=True)
    verify_service_token(token, project, path, env, writer=True)
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
        if status == 404:
            status = _api("POST", "/v3/secrets/raw/" + quoted, token, body, env)
            if status in (200, 201):
                return "created"
        raise SecretsError(f"write-failed:  http-{status}")
    finally:
        body = {}
        token = ""
