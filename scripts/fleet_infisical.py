"""Scoped Infisical service-token reads and allowlisted writes.

Only explicit process bindings are consumed: INFISICAL_TOKEN is read-only and
INFISICAL_WRITE_TOKEN is a separate writer.  No Keychain, Automation identity,
CLI login session or credential-file fallback runs.  Values stay in memory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import namedtuple
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
# Infisical's public wire marker from backend/src/services/secret/secret-fns.ts.
# https://github.com/Infisical/infisical/blob/main/backend/src/services/secret/secret-fns.ts
HIDDEN_VALUE_MASK = "<hidden-by-infisical>"

ENV_PROJECT = "INFISICAL_FLEET_OPS_PROJECT_ID"
PROJECT_FILE = ".simple-tools/fleet-ops-project-id"
ENV_NAMES = ("INFISICAL_ENVIRONMENT", "INFISICAL_ENV")
# Read-only second project:  the FleetLink seat tokens (FLEETLINK_TOKEN_<SEAT>) live in AI Fleet Coordinator
# prod /fleetlink, not in Fleet-OPS.  `names --project ai-fleet-coordinator` lists them (names only).
DEFAULT_PROJECT = "fleet-ops"
PROJECT_SOURCES = {
    "fleet-ops": (ENV_PROJECT, PROJECT_FILE),
    "ai-fleet-coordinator": (
        "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID",
        ".simple-tools/ai-fleet-coordinator-project-id",
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

    def __init__(
        self, message: str, code: int = 4, *, failure_kind=None, provider_status=None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        # Stable policy reason; exit status remains backwards compatible.
        self.reason = message if message == "token-expired-or-invalid" else None
        self.failure_kind = failure_kind if failure_kind in ("unavailable", "refused") else None
        self.provider_status = (
            provider_status
            if type(provider_status) is int and 0 <= provider_status <= 599
            else None
        )


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
    ~/.simple-tools/fleet-ops-project-id.  Fails closed, naming both, and never echoes a malformed value.

    Same sources and error text as ``scripts/simple_tools/zulip.py:infisical_project_id`` (PR #634);
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
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise UsageError("bad key name:  use letters, digits and underscores only")
    return name


def _base_env(env: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    # The optional version probe never inherits an identity or service token.
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
    """Read one declared folder with a scoped token; allow_session is a retired compatibility flag."""
    project = infisical_project_id(env, home_dir(env), project_name)
    return fetch_project(project, path, env, token=token, writer=writer)


def project_token_name(project_name: str) -> str:
    if project_name == "fleet-ops":
        return ENV_TOKEN
    if project_name == "ai-fleet-coordinator":
        return ENV_AI_FLEET_COORDINATOR_TOKEN
    raise UsageError("unknown Infisical project")


def project_name_for_id(project_id: str, env: Mapping[str, str]) -> str:
    unavailable = []
    for name in PROJECT_SOURCES:
        try:
            if infisical_project_id(env, home_dir(env), name) == project_id:
                return name
        except SecretsError:
            env_name, project_file = PROJECT_SOURCES[name]
            unavailable.append(f"{env_name} or ~/{project_file}")
            continue
    detail = "; unavailable or malformed sources: " + ", ".join(unavailable) if unavailable else ""
    raise SecretsError(
        "project-token-binding-missing: project id has no configured token binding" + detail
    )


def fetch_with_cli_session(project: str, path: str, env: Mapping[str, str]) -> dict[str, str]:
    """Retired compatibility entry; a TTY never authorizes a tokenless consumer read."""
    raise SecretsError("service-token-required: consumer reads require their own scoped token")


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
    try:
        if not isinstance(expiry, str):
            raise ValueError()
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
        raise SecretsError(refusal, 2, failure_kind="refused")
    try:
        paths = declared_paths(env, path, writer=writer)
        check_service_token(token)
    except SecretsError as exc:
        exc.failure_kind = "refused"
        raise
    status, metadata = _api_json(
        "GET", "/v2/service-token", token, None, env, max_bytes=128 * 1024, deadline=deadline
    )
    if status != 200 or not isinstance(metadata, dict):
        unavailable = status in (0, 200, 408, 425, 429) or 500 <= status <= 599
        raise SecretsError(
            "token-verification-failed",
            failure_kind="unavailable" if unavailable else "refused",
            provider_status=status,
        )
    try:
        validate_service_metadata(
            metadata, project, paths, ("read", "write") if writer else ("read",)
        )
    except SecretsError as exc:
        exc.failure_kind = "refused"
        raise
    return paths


def secret_query(project: str, path: str, *, values: bool = True) -> str:
    return urllib.parse.urlencode(
        {
            "workspaceId": project,
            "environment": ENVIRONMENT,
            "secretPath": path,
            "viewSecretValue": "true" if values else "false",
            "expandSecretReferences": "false",
            "recursive": "false",
            "include_imports": "false",
        }
    )


def validate_secret_name_record(
    record: object, project: str, path: str, *, single_scope: bool = True
) -> str:
    """The shared provenance and name checks for value reads and names-only listing."""
    if not isinstance(record, dict) or record.get("type") != "shared":
        raise SecretsError("unverified-source")
    if "secretValueHidden" in record and type(record["secretValueHidden"]) is not bool:
        raise SecretsError("unverified-source")
    for field, expected in (
        ("workspace", project),
        ("environment", ENVIRONMENT),
        ("secretPath", path),
    ):
        if record.get(field) != expected and not (single_scope and field not in record):
            raise SecretsError("unverified-source")
    key = record.get("secretKey")
    if not isinstance(key, str) or not NAME_RE.fullmatch(key):
        raise SecretsError("fetch-failed: bad-output")
    return key


def validate_secret_record(
    record: object, project: str, path: str, *, single_scope: bool = True
) -> tuple[str, str]:
    key = validate_secret_name_record(record, project, path, single_scope=single_scope)
    if record.get("secretValueHidden") is True:
        raise SecretsError("unverified-source")
    value = record.get("secretValue")
    if not isinstance(value, str):
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
    # a read already in flight may still finish, but its answer is never used.  A failure cancels
    # only the reads after it in the sorted list, so every earlier read still runs, and the refusal
    # always names the earliest failing endpoint.  An interrupt is never held back.
    answers: dict = {}
    failures: dict = {}
    with ThreadPoolExecutor(max_workers=MAX_NAMED_READS) as pool:
        futures = [pool.submit(read_one, item) for item in endpoints]
        try:
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                try:
                    name, pairs = future.result()
                except Exception as exc:  # noqa: BLE001 - re-raised below, earliest first
                    index = futures.index(future)
                    failures[index] = exc
                    for later in futures[index + 1 :]:
                        later.cancel()
                    continue
                answers[name] = pairs
        except BaseException:
            # An interrupt stops every read that has not started before it propagates.
            for future in futures:
                future.cancel()
            raise
    if failures:
        raise failures[min(failures)]
    for expected_name, _endpoint in endpoints:
        for key, value in answers[expected_name]:
            if key in found:
                raise SecretsError("unverified-source")
            found[key] = value
    reject_unresolved(found)
    return found


def _bounded_read_command() -> list[str]:
    """The same reviewed client in an isolated, disposable read-only process."""
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--bounded-read-worker"]


def _read_worker_pipes(child, request: bytes, limit: int, deadline: float) -> bytes:
    """Bound both pipe directions without an unbounded communicate buffer or helper thread."""
    output = bytearray()
    position = 0
    with selectors.DefaultSelector() as selected:
        for stream, event, name in (
            (child.stdin, selectors.EVENT_WRITE, "input"),
            (child.stdout, selectors.EVENT_READ, "output"),
        ):
            os.set_blocking(stream.fileno(), False)
            selected.register(stream, event, name)
        while selected.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("bounded read", 0)
            ready = selected.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired("bounded read", 0)
            for key, _event in ready:
                if key.data == "input":
                    try:
                        position += os.write(key.fd, request[position : position + 65536])
                    except BrokenPipeError:
                        position = len(request)
                    if position == len(request):
                        selected.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, min(65536, limit + 1 - len(output)))
                    output.extend(chunk)
                    if len(output) > limit:
                        raise SecretsError("bounded-read-output-refused")
                    if not chunk:
                        selected.unregister(key.fileobj)
                        key.fileobj.close()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("bounded read", 0)
    child.wait(timeout=remaining)
    return bytes(output)


def _bounded_read_request(request: dict, deadline: float) -> dict:
    """Bound provider/DNS/executor waits and reap only the child this call created."""
    encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 128 * 1024:
        raise UsageError("bounded-read-invalid")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SecretsError("bounded-read-timeout", failure_kind="unavailable")
    # Leave time to kill/reap a stuck read before the caller's deadline.
    cleanup_reserve = min(0.1, remaining / 5)
    child = None
    try:
        child = subprocess.Popen(
            _bounded_read_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
            shell=False,
            close_fds=True,
        )
        wait = deadline - time.monotonic() - cleanup_reserve
        if wait <= 0:
            raise subprocess.TimeoutExpired("bounded read", 0)
        output = _read_worker_pipes(
            child, encoded, request["max_output_bytes"], deadline - cleanup_reserve
        )
        if len(output) > request["max_output_bytes"] or time.monotonic() >= deadline:
            raise SecretsError("bounded-read-failed", failure_kind="unavailable")
        try:
            reply = _decode_json(output)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise SecretsError("bounded-read-failed", failure_kind="unavailable") from None
        if (
            not isinstance(reply, dict)
            or type(reply.get("ok")) is not bool
            or type(reply.get("schema")) is not int
            or reply["schema"] != 1
        ):
            raise SecretsError("bounded-read-failed", failure_kind="unavailable")
        if not reply["ok"] or child.returncode != 0:
            reason = (
                "token-expired-or-invalid"
                if reply.get("reason") == "token-expired-or-invalid"
                else "bounded-read-failed"
            )
            raise SecretsError(reason, failure_kind=reply.get("failure_kind"))
        if set(reply) != {"schema", "ok", "values"} or not isinstance(reply["values"], dict):
            raise SecretsError("bounded-read-failed", failure_kind="unavailable")
        if time.monotonic() >= deadline:
            raise SecretsError("bounded-read-timeout", failure_kind="unavailable")
        return reply["values"]
    except subprocess.TimeoutExpired:
        raise SecretsError("bounded-read-timeout", failure_kind="unavailable") from None
    except OSError:
        raise SecretsError("bounded-read-unavailable", failure_kind="unavailable") from None
    finally:
        primary_error = sys.exc_info()[0]
        encoded = b""
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                # Never wait indefinitely or hide an OS cleanup failure.
                if primary_error not in (KeyboardInterrupt, SystemExit):
                    raise SecretsError(
                        "bounded-read-cleanup-failed", failure_kind="unavailable"
                    ) from None
            finally:
                for stream in (child.stdin, child.stdout):
                    if stream is not None:
                        stream.close()


def bounded_fetch_project(
    project: str,
    path: str,
    env: Mapping[str, str],
    *,
    token: str,
    names: Iterable[str],
    timeout: float,
    writer: bool = False,
    max_output_bytes: int | None = None,
) -> dict[str, str]:
    """A small named read batch with a parent-enforced operation deadline.

    The disposable child runs fetch_project, including the same exact grant and
    record verifier.  Only this caller's explicit binding enters its private stdin;
    no credential is put in argv, inherited environment, files or diagnostics.
    Process launch/OS scheduling must be healthy; provider, DNS, body and worker
    cleanup waits do not extend the caller's remaining budget.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise UsageError("bounded-read-invalid")
    if max_output_bytes is None:
        max_output_bytes = MAX_VERIFIED_BYTES
    if (
        type(writer) is not bool
        or type(max_output_bytes) is not int
        or not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES
    ):
        raise UsageError("bounded-read-invalid")
    check_path(path)
    check_service_token(token)
    if not isinstance(project, str) or not project or len(project) > 256:
        raise UsageError("bounded-read-invalid")
    wanted = []
    for name in names:
        wanted.append(check_name(name))
        if len(wanted) > 1024:
            raise UsageError("bounded-read-invalid")
    wanted = sorted(set(wanted))
    if not wanted:
        raise UsageError("bounded-read-invalid")
    selected = {key: env[key] for key in (ENV_API, *ENV_NAMES, ENV_READ_PATHS) if key in env}
    if any(not isinstance(value, str) for value in selected.values()):
        raise UsageError("bounded-read-invalid")
    request = dict(
        schema=1,
        operation="named-fetch",
        project=project,
        path=path,
        env=selected,
        token=token,
        names=wanted,
        timeout=timeout,
        writer=writer,
        max_output_bytes=max_output_bytes,
    )
    values = _bounded_read_request(request, time.monotonic() + timeout)
    if any(key not in wanted or not isinstance(value, str) for key, value in values.items()):
        raise SecretsError("bounded-read-failed", failure_kind="unavailable")
    reject_unresolved(values)
    return values


def _bounded_read_worker() -> int:
    """Private stdin/stdout protocol; it performs no provider mutation."""
    reply = {
        "schema": 1,
        "ok": False,
        "reason": "bounded-read-failed",
        "failure_kind": "unavailable",
    }
    limit = MAX_VERIFIED_BYTES
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise UsageError("bounded-read-invalid")
        request = _decode_json(raw)
        fields = {
            "schema",
            "operation",
            "project",
            "path",
            "env",
            "token",
            "names",
            "timeout",
            "writer",
            "max_output_bytes",
        }
        if (
            not isinstance(request, dict)
            or set(request) != fields
            or type(request["schema"]) is not int
            or request["schema"] != 1
        ):
            raise UsageError("bounded-read-invalid")
        if request["operation"] != "named-fetch":
            raise UsageError("bounded-read-invalid")
        if (
            not isinstance(request["names"], list)
            or not 1 <= len(request["names"]) <= 1024
            or type(request["writer"]) is not bool
            or type(request["timeout"]) not in (int, float)
            or not math.isfinite(request["timeout"])
            or request["timeout"] <= 0
        ):
            raise UsageError("bounded-read-invalid")
        if (
            not isinstance(request["env"], dict)
            or set(request["env"]) - {ENV_API, *ENV_NAMES, ENV_READ_PATHS}
            or any(not isinstance(value, str) for value in request["env"].values())
        ):
            raise UsageError("bounded-read-invalid")
        check_path(request["path"])
        check_service_token(request["token"])
        for name in request["names"]:
            check_name(name)
        limit = request["max_output_bytes"]
        if type(limit) is not int or not 1024 <= limit <= MAX_OUTPUT_BYTES:
            raise UsageError("bounded-read-invalid")
        values = fetch_project(
            request["project"],
            request["path"],
            request["env"],
            token=request["token"],
            names=request["names"],
            writer=request["writer"],
            timeout=request["timeout"],
        )
        reply = {"schema": 1, "ok": True, "values": values}
    except SecretsError as exc:
        reply["reason"] = (
            "token-expired-or-invalid"
            if exc.reason == "token-expired-or-invalid"
            else "bounded-read-failed"
        )
        reply["failure_kind"] = exc.failure_kind
    except Exception:
        pass
    output = json.dumps(reply, ensure_ascii=False).encode("utf-8")
    if len(output) > limit:
        reply = {
            "schema": 1,
            "ok": False,
            "reason": "bounded-read-failed",
            "failure_kind": "unavailable",
        }
        output = json.dumps(reply).encode("utf-8")
    sys.stdout.buffer.write(output)
    return 0 if reply["ok"] else 4


def list_project_names(
    project: str,
    path: str,
    env: Mapping[str, str],
    *,
    token: str | None = None,
    timeout: float = EXPORT_TIMEOUT,
) -> tuple[str, ...]:
    """List one folder's names without requesting or retaining secret values.

    The same transport and grant verifier serve every consumer.  A listing requires
    exactly this prod folder and read-only permission; ambient folder declarations
    cannot widen the grant accepted here.
    """
    check_path(path)
    scoped_env = dict(env)
    scoped_env[ENV_READ_PATHS] = path
    if token is None:
        project_name = project_name_for_id(project, scoped_env)
        token = resolve_token(scoped_env, project_name=project_name)
    deadline = time.monotonic() + timeout
    verify_service_token(token, project, path, scoped_env, deadline=deadline)
    status, payload = _api_json(
        "GET",
        "/v3/secrets/raw?" + secret_query(project, path, values=False),
        token,
        None,
        scoped_env,
        max_bytes=MAX_OUTPUT_BYTES,
        deadline=deadline,
    )
    if status != 200 or not isinstance(payload, dict):
        raise SecretsError("fetch-failed")
    records = payload.get("secrets")
    if not isinstance(records, list):
        raise SecretsError("fetch-failed: bad-output")
    found: set[str] = set()
    for record in records:
        key = validate_secret_name_record(record, project, path)
        value = record.get("secretValue")
        hidden_mask = record.get("secretValueHidden") is True and value == HIDDEN_VALUE_MASK
        if key in found or (value is not None and value != "" and not hidden_mask):
            raise SecretsError("unverified-source")
        found.add(key)
    return tuple(sorted(found))


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
        if not isinstance(key, str) or not NAME_RE.fullmatch(key):
            raise SecretsError("fetch-failed: bad-output")
        if key in found:
            raise SecretsError("fetch-failed: duplicate-output-key")
        if isinstance(val, bool):
            val = "true" if val else "false"
        elif isinstance(val, (int, float)):
            if isinstance(val, float) and not math.isfinite(val):
                raise SecretsError("fetch-failed: bad-output")
            val = str(val)
        elif not isinstance(val, str):
            raise SecretsError("fetch-failed: bad-output")
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

    This compatibility entry requires explicit row provenance as well as the
    scoped metadata shared with fetch_all.  The retired CLI-export route never
    runs.  Here the ANSWER's own fields must
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


MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_json_constant(value):
    raise ValueError("nonfinite JSON number")


def _decode_json(raw: bytes):
    """Bound external JSON before any consumer observes its graph."""
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_invalid_json_constant,
    )
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON bounds exceeded")
        if isinstance(item, dict):
            if nodes + len(pending) + 2 * len(item) > MAX_JSON_NODES:
                raise ValueError("JSON bounds exceeded")
            nodes += len(item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if nodes + len(pending) + len(item) > MAX_JSON_NODES:
                raise ValueError("JSON bounds exceeded")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("nonfinite JSON number")
    return value


def _api_json(
    method: str,
    path: str,
    token: str,
    body: dict | None,
    env: Mapping[str, str],
    max_bytes: int | None = None,
    deadline: float | None = None,
    on_status=None,
    on_unsent=None,
) -> tuple[int, object]:
    """One Infisical REST call.  The status and the parsed body,  never a message.

    The body comes back for the calls that have to read it,  like the folder create,
    whose 400 says whether the folder is already there or the request was wrong.  The
    token travels in a header, never in an argument list.

    `max_bytes`, when set, bounds the socket read itself (not a post-read length
    check):  an oversized answer fails closed as (0, None) without the body being
    buffered whole.  Calls without a narrower cap use MAX_OUTPUT_BYTES.
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
    response_seen = False
    try:
        timeout = API_TIMEOUT if deadline is None else min(API_TIMEOUT, deadline - time.monotonic())
        if timeout <= 0:
            return 0, None
        with _opener().open(req, timeout=timeout) as resp:
            response_seen = True
            status = resp.status
            if on_status is not None:
                on_status(status)
            raw = resp.read(limit)
    except urllib.error.HTTPError as exc:
        response_seen = True
        status = exc.code
        if on_status is not None:
            on_status(status)
        raw = exc.read(limit)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if (
            not response_seen
            and on_unsent is not None
            and isinstance(getattr(exc, "reason", exc), ConnectionRefusedError)
        ):
            on_unsent()
        return 0, None
    finally:
        data = None
    if max_bytes is not None and len(raw) > max_bytes:
        return 0, None
    try:
        return status, _decode_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
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
        if re.fullmatch(folder, path) and re.fullmatch(pattern, name):
            return
    raise SecretsError(
        "set refused:  {} at {} is not on the built-in write allowlist ({});  widening it needs a code change".format(
            name, path, ", ".join(f"{f} {p}" for f, p in WRITE_ALLOWLIST)
        ),
        2,
    )


def ensure_folder(path: str, env: Mapping[str, str], token: str) -> None:
    """Verify an existing folder without requesting values; provisioning is separate.

    This compatibility helper never creates a folder or writes a secret.  The
    caller supplies its own exact scoped writer, verified before the bounded GET.
    """
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    project = infisical_project_id(env, home_dir(env))
    verify_service_token(token, project, path, env, writer=True)
    status, _ = _api_json(
        "GET",
        "/v3/secrets/raw?" + secret_query(project, path, values=False),
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


WRITE_INTENT_TTL_MS = 24 * 60 * 60 * 1000
WRITE_CLOCK_SKEW_MS = 5 * 60 * 1000
WRITE_CAPACITY = 1000
_WRITE_FIELDS = frozenset(
    {
        "schema",
        "request_id",
        "created_at_ms",
        "purpose",
        "fingerprint",
        "provider",
        "project",
        "path",
        "name",
        "digest",
        "phase",
        "result",
        "ack_status",
        "provider_status",
        "retryable",
        "failure_kind",
    }
)
_WRITE_PHASES = frozenset(
    {"prepared", "sending", "acknowledged", "verified", "refused", "cancelled"}
)


class WriteIntent(namedtuple("_WriteIntent", "state_dir purpose request_id created_at_ms")):
    """Caller-owned durable location and stable identity; no credential or value."""

    __slots__ = ()

    def __new__(cls, state_dir: Path, purpose: str, request_id: str, created_at_ms: int):
        try:
            directory = Path(state_dir)
            valid_id = isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id
        except (ValueError, TypeError, AttributeError):
            raise SecretsError("write-intent-invalid", 2) from None
        if (
            not directory.is_absolute()
            or ".." in directory.parts
            or not isinstance(purpose, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,79}", purpose) is None
            or not valid_id
            or type(created_at_ms) is not int
            or not 0 < created_at_ms <= (1 << 63) - 1
        ):
            raise SecretsError("write-intent-invalid", 2)
        return super().__new__(cls, directory, purpose, request_id, created_at_ms)


class WriteReceipt(
    namedtuple(
        "_WriteReceipt", "request_id created_at_ms result replayed outcome", defaults=(False, "ok")
    )
):
    """Immutable nonsecret acknowledgment of one known completed intent."""

    __slots__ = ()


class WriteError(SecretsError):
    """A fixed outcome plus the caller's nonsecret recovery identity."""

    def __init__(
        self,
        outcome: str,
        intent: WriteIntent,
        *,
        reason=None,
        required_binding=None,
        name=None,
        retryable=False,
        failure_kind=None,
        provider_status=None,
    ):
        super().__init__(
            "write-" + outcome, failure_kind=failure_kind, provider_status=provider_status
        )
        self.outcome = outcome
        self.request_id = intent.request_id
        self.created_at_ms = intent.created_at_ms
        self.purpose = intent.purpose
        self.name = name
        self.request_may_have_been_sent = outcome in (
            "outcome_unknown",
            "accepted_unverified",
            "readback_conflict",
            "prior_outcome_unknown",
            "state_unavailable",
            "state-invalid",
        )
        self.retryable = retryable is True and not self.request_may_have_been_sent
        self.reason = reason if reason == "token-expired-or-invalid" else None
        self.required_binding = required_binding if required_binding == ENV_WRITE_TOKEN else None
        if self.reason:
            self.message += ": " + self.reason
        if self.required_binding:
            self.message += ": " + self.required_binding + " is not set"
        self.args = (self.message,)


def new_write_intent(state_dir: Path, purpose: str) -> WriteIntent:
    """Allocate once per explicit caller attempt; put_secret durably records it before dispatch."""
    return WriteIntent(state_dir, purpose, str(uuid.uuid4()), time.time_ns() // 1_000_000)


def _write_ledger(directory: Path):
    # Read-only imports and calls never load a writer's state dependency or create files.
    expected = Path(__file__).resolve().parent / "simple_tools" / "operator_github_state.py"
    module_name = "_fleet_infisical_write_ledger"
    try:
        loaded = sys.modules.get(module_name)
        if loaded is not None:
            if Path(loaded.__file__).resolve() != expected:
                raise ValueError()
        else:
            spec = importlib.util.spec_from_file_location(module_name, expected)
            if spec is None or spec.loader is None:
                raise ValueError()
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            sys.modules[module_name] = loaded
        return loaded.NonceLedger(directory, retain_unsettled=True)
    except Exception:
        raise SecretsError("write-state-unavailable") from None


def _write_provider(env):
    try:
        provider = (env.get(ENV_API) or API_BASE).rstrip("/")
        parsed = urllib.parse.urlsplit(provider)
        if (
            len(provider) > 2048
            or parsed.scheme not in ("https", "http")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or "\\" in provider
            or "//" in parsed.path
            or any(part in (".", "..") for part in parsed.path.split("/"))
        ):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
            host = "[" + address.compressed + "]" if address.version == 6 else address.compressed
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host) or re.fullmatch(r"[0-9.]+", host):
                raise ValueError() from None
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += ":" + str(port)
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise SecretsError("write-provider-invalid", 2) from None


def _write_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write_namespace(provider, project, path):
    # Purpose and name do not divide this barrier; fresh intents cannot pass an uncertain write.
    return "infisical-write:" + _write_digest(_write_json([provider, project, ENVIRONMENT, path]))


def _write_record(intent, provider, project, path, name, digest):
    binding = [
        "put",
        provider,
        project,
        ENVIRONMENT,
        path,
        name,
        digest,
        intent.purpose,
        intent.created_at_ms,
    ]
    return dict(
        schema=1,
        request_id=intent.request_id,
        created_at_ms=intent.created_at_ms,
        purpose=intent.purpose,
        fingerprint=_write_digest(_write_json(binding)),
        provider=provider,
        project=project,
        path=path,
        name=name,
        digest=digest,
        phase="prepared",
        result="",
        ack_status=0,
        provider_status=0,
        retryable=False,
        failure_kind="",
    )


def _write_cached(raw, intent, provider, project, path, name, outcome):
    try:
        record = _decode_json(raw.encode("utf-8"))
        if not isinstance(record, dict) or set(record) != _WRITE_FIELDS:
            raise ValueError()
        if (
            type(record["schema"]) is not int
            or record["schema"] != 1
            or record["request_id"] != intent.request_id
            or record["created_at_ms"] != intent.created_at_ms
            or record["purpose"] != intent.purpose
            or record["provider"] != provider
            or record["project"] != project
            or record["path"] != path
            or record["name"] != name
            or record["phase"] not in _WRITE_PHASES
            or record["result"] not in ("", "created", "updated")
            or type(record["ack_status"]) is not int
            or record["ack_status"] not in (0, 200, 201)
            or type(record["provider_status"]) is not int
            or not 0 <= record["provider_status"] <= 599
            or type(record["retryable"]) is not bool
            or record["failure_kind"] not in ("", "unavailable", "refused")
            or not isinstance(record["digest"], str)
            or re.fullmatch(r"[a-f0-9]{64}", record["digest"]) is None
        ):
            raise ValueError()
        expected = _write_record(intent, provider, project, path, name, record["digest"])
        if record["fingerprint"] != expected["fingerprint"]:
            raise ValueError()
        acknowledged = record["phase"] in ("acknowledged", "verified")
        if acknowledged != (record["ack_status"] in (200, 201)) or acknowledged != bool(
            record["result"]
        ):
            raise ValueError()
        if acknowledged and (
            record["provider_status"] != record["ack_status"]
            or record["retryable"]
            or record["failure_kind"]
        ):
            raise ValueError()
        if record["phase"] == "refused":
            if record["failure_kind"] not in ("unavailable", "refused") or record["retryable"] != (
                record["failure_kind"] == "unavailable"
            ):
                raise ValueError()
        elif record["retryable"] or record["failure_kind"]:
            raise ValueError()
        allowed = {"verified": {"ok"}, "refused": {"refused"}, "cancelled": {"refused"}}
        if outcome not in allowed.get(record["phase"], {"pending", "outcome_unknown"}):
            raise ValueError()
        return record
    except Exception:
        raise WriteError("state-invalid", intent) from None


def _write_checkpoint(ledger, namespace, intent, record, outcome="pending"):
    ledger.settle(namespace, intent.request_id, outcome, _write_json(record))


def _write_refusal(intent, record):
    return WriteError(
        "refused",
        intent,
        retryable=record["retryable"],
        failure_kind=record["failure_kind"],
        provider_status=record["provider_status"],
    )


def _write_validate_history(ledger, directory):
    def expected_expiry(namespace, nonce, outcome, raw):
        record = _decode_json(raw.encode("utf-8"))
        intent = WriteIntent(
            directory, record["purpose"], record["request_id"], record["created_at_ms"]
        )
        check_name(record["name"])
        check_path(record["path"])
        check_writable(record["name"], record["path"])
        if (
            not isinstance(record["project"], str)
            or not _UUID_RE.fullmatch(record["project"])
            or str(uuid.UUID(record["project"])) != record["project"]
            or _write_provider({ENV_API: record["provider"]}) != record["provider"]
            or namespace != _write_namespace(record["provider"], record["project"], record["path"])
            or nonce != intent.request_id
        ):
            raise ValueError()
        _write_cached(
            raw,
            intent,
            record["provider"],
            record["project"],
            record["path"],
            record["name"],
            outcome,
        )
        return (intent.created_at_ms + WRITE_INTENT_TTL_MS) / 1000

    ledger.validate_retained_rows(expected_expiry)


def _write_lookup(ledger, namespace, intent):
    _write_validate_history(ledger, intent.state_dir)
    existing = ledger.lookup_identity(intent.request_id)
    if existing is None:
        return None
    if existing[0] != namespace:
        raise WriteError("intent_conflict", intent)
    return existing[1:]


def _write_blocker(ledger, namespace, intent, provider, project, path):
    held = ledger.lookup_unsettled(namespace)
    if held is None:
        return
    try:
        nonce, outcome, raw = held
        record = _decode_json(raw.encode("utf-8"))
        original = WriteIntent(
            intent.state_dir, record["purpose"], record["request_id"], record["created_at_ms"]
        )
        name = check_name(record["name"])
        check_writable(name, path)
        _write_cached(raw, original, provider, project, path, name, outcome)
        if nonce != original.request_id:
            raise ValueError()
    except Exception:
        raise WriteError("state-invalid", intent) from None
    # The blocked new attempt never replaces the original durable recovery identity.
    raise WriteError("prior_outcome_unknown", original, name=name)


def _write_readback(name, path, project, token, env, deadline):
    status, payload = _api_json(
        "GET",
        "/v3/secrets/raw/" + urllib.parse.quote(name, safe="") + "?" + secret_query(project, path),
        token,
        None,
        env,
        max_bytes=MAX_VERIFIED_BYTES,
        deadline=deadline,
    )
    if status != 200 or not isinstance(payload, dict):
        raise SecretsError("fetch-failed")
    key, value = validate_secret_record(payload.get("secret"), project, path)
    if key != name:
        raise SecretsError("unverified-source")
    reject_unresolved({key: value})
    return _write_digest(value)


def _write_finish_readback(ledger, namespace, intent, record, token, env, deadline):
    try:
        digest = _write_readback(
            record["name"], record["path"], record["project"], token, env, deadline
        )
    except Exception:
        _write_checkpoint(ledger, namespace, intent, record, "outcome_unknown")
        raise WriteError(
            "accepted_unverified", intent, provider_status=record["provider_status"]
        ) from None
    if digest != record["digest"]:
        _write_checkpoint(ledger, namespace, intent, record, "outcome_unknown")
        raise WriteError("readback_conflict", intent, provider_status=record["provider_status"])
    record["phase"] = "verified"
    _write_checkpoint(ledger, namespace, intent, record, "ok")
    return WriteReceipt(intent.request_id, intent.created_at_ms, record["result"])


def _write_context(name, path, env, intent, timeout):
    if not isinstance(intent, WriteIntent):
        raise SecretsError("write-intent-required", 2)
    WriteIntent(*intent)  # Validate tuple reconstruction helpers at the public boundary too.
    refusal = environment_refusal(env)
    if refusal:
        raise SecretsError(refusal, 2)
    check_name(name)
    check_path(path)
    check_writable(name, path)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise SecretsError("write-timeout-invalid", 2)
    project = str(uuid.UUID(infisical_project_id(env, home_dir(env))))
    provider = _write_provider(env)
    return project, provider, _write_namespace(provider, project, path), time.monotonic() + timeout


def put_secret(
    name: str,
    path: str,
    value: str,
    env: Mapping[str, str],
    token: str | None = None,
    *,
    intent: WriteIntent | None = None,
    timeout: float = API_TIMEOUT,
) -> WriteReceipt:
    """One allowlisted intent, serialized with all writers using its caller-owned state.

    A duplicate never writes; a fresh UUID, changed purpose or no-op cannot bypass an
    unsettled provider/project/folder operation in the same store.  Unknown attempts
    stay held across expiry and restart.  This is not cross-host or provider CAS, and
    another state location or direct external writer is outside this guarantee.
    """
    env = dict(env)
    env[ENV_WRITE_PATHS] = path
    project, provider, namespace, deadline = _write_context(name, path, env, intent, timeout)
    env[ENV_API] = provider
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise SecretsError("write-value-invalid", 2)
    record = _write_record(intent, provider, project, path, name, _write_digest(value))
    ledger = _write_ledger(intent.state_dir)
    try:
        with ledger.serialized(timeout=min(5.0, max(0, deadline - time.monotonic()))):
            old = _write_lookup(ledger, namespace, intent)
            if old is not None:
                saved = _write_cached(old[1], intent, provider, project, path, name, old[0])
                if saved["fingerprint"] != record["fingerprint"]:
                    raise WriteError("intent_conflict", intent)
                if old[0] == "ok" and saved["phase"] == "verified":
                    return WriteReceipt(
                        intent.request_id, intent.created_at_ms, saved["result"], replayed=True
                    )
                if old[0] == "refused":
                    raise _write_refusal(intent, saved)
                raise WriteError("outcome_unknown", intent)
            now_ms = time.time_ns() // 1_000_000
            if (
                not now_ms - WRITE_INTENT_TTL_MS
                <= intent.created_at_ms
                <= now_ms + WRITE_CLOCK_SKEW_MS
            ):
                raise WriteError("intent_expired", intent)
            _write_blocker(ledger, namespace, intent, provider, project, path)
            claim = ledger.begin(
                namespace,
                intent.request_id,
                (intent.created_at_ms + WRITE_INTENT_TTL_MS) / 1000,
                now_ms / 1000,
                WRITE_CAPACITY,
                response=_write_json(record),
            )
            if claim != "fresh":
                raise WriteError("state_unavailable", intent)
            return _write_dispatch(ledger, namespace, intent, record, value, token, env, deadline)
    except SecretsError:
        raise
    except Exception:
        raise WriteError("state_unavailable", intent) from None


def _write_dispatch(ledger, namespace, intent, record, value, token, env, deadline):
    # Definite preflight refusals have never entered a provider mutation.
    try:
        token = token if token is not None else resolve_token(env, writer=True)
        verify_service_token(
            token, record["project"], record["path"], env, writer=True, deadline=deadline
        )
        probe, _ = _api_json(
            "GET",
            "/v3/secrets/raw?" + secret_query(record["project"], record["path"], values=False),
            token,
            None,
            env,
            max_bytes=MAX_VERIFIED_BYTES,
            deadline=deadline,
        )
        if probe != 200 or time.monotonic() >= deadline:
            unavailable = (
                probe in (0, 408, 425, 429) or 500 <= probe <= 599 or time.monotonic() >= deadline
            )
            raise SecretsError(
                "folder-missing-or-inaccessible",
                failure_kind="unavailable" if unavailable else "refused",
                provider_status=probe,
            )
    except SecretsError as exc:
        kind = exc.failure_kind or "refused"
        record.update(
            phase="refused",
            failure_kind=kind,
            retryable=kind == "unavailable",
            provider_status=exc.provider_status or 0,
        )
        _write_checkpoint(ledger, namespace, intent, record, "refused")
        missing = ENV_WRITE_TOKEN if token is None and not env.get(ENV_WRITE_TOKEN) else None
        raise WriteError(
            "refused",
            intent,
            reason=exc.reason,
            required_binding=missing,
            retryable=record["retryable"],
            failure_kind=kind,
            provider_status=record["provider_status"],
        ) from None
    endpoint = "/v3/secrets/raw/" + urllib.parse.quote(record["name"], safe="")
    body = dict(
        workspaceId=record["project"],
        environment=ENVIRONMENT,
        secretPath=record["path"],
        secretValue=value,
        type="shared",
    )
    try:
        for method, result in (("PATCH", "updated"), ("POST", "created")):
            if time.monotonic() >= deadline:
                record.update(phase="refused", failure_kind="unavailable", retryable=True)
                _write_checkpoint(ledger, namespace, intent, record, "refused")
                raise _write_refusal(intent, record)
            record["phase"] = "sending"
            _write_checkpoint(ledger, namespace, intent, record)

            def acknowledge(status):
                if status in (200, 201):
                    record.update(
                        phase="acknowledged",
                        result=result,
                        ack_status=status,
                        provider_status=status,
                    )
                    _write_checkpoint(ledger, namespace, intent, record)

            def unsent():
                record.update(phase="refused", failure_kind="unavailable", retryable=True)
                _write_checkpoint(ledger, namespace, intent, record, "refused")

            try:
                status, _ = _api_json(
                    method,
                    endpoint,
                    token,
                    body,
                    env,
                    max_bytes=MAX_VERIFIED_BYTES,
                    deadline=deadline,
                    on_status=acknowledge,
                    on_unsent=unsent,
                )
            except Exception:
                if record["phase"] == "refused":
                    _write_checkpoint(ledger, namespace, intent, record, "refused")
                    raise _write_refusal(intent, record) from None
                _write_checkpoint(ledger, namespace, intent, record, "outcome_unknown")
                raise WriteError(
                    "accepted_unverified"
                    if record["phase"] == "acknowledged"
                    else "outcome_unknown",
                    intent,
                    provider_status=record["provider_status"],
                ) from None
            if record["phase"] == "acknowledged":
                return _write_finish_readback(
                    ledger, namespace, intent, record, token, env, deadline
                )
            if record["phase"] == "refused":
                raise _write_refusal(intent, record)
            if status == 404 and method == "PATCH":
                record["phase"] = "prepared"
                _write_checkpoint(ledger, namespace, intent, record)
                continue
            if status in (400, 401, 403, 404, 405, 409, 422, 429):
                record.update(
                    phase="refused",
                    failure_kind="unavailable" if status == 429 else "refused",
                    retryable=status == 429,
                    provider_status=status,
                )
                _write_checkpoint(ledger, namespace, intent, record, "refused")
                raise _write_refusal(intent, record)
            record["provider_status"] = status if type(status) is int and 0 <= status <= 599 else 0
            _write_checkpoint(ledger, namespace, intent, record, "outcome_unknown")
            raise WriteError("outcome_unknown", intent, provider_status=record["provider_status"])
    finally:
        body.clear()


def reconcile_secret(
    name: str,
    path: str,
    env: Mapping[str, str],
    *,
    intent: WriteIntent,
    token: str | None = None,
    timeout: float = API_TIMEOUT,
) -> WriteReceipt:
    """Read-only recovery; equality alone never settles an attempt without a recorded causal ACK."""
    env = dict(env)
    env[ENV_WRITE_PATHS] = path
    project, provider, namespace, deadline = _write_context(name, path, env, intent, timeout)
    env[ENV_API] = provider
    ledger = _write_ledger(intent.state_dir)
    try:
        with ledger.serialized(timeout=min(5.0, max(0, deadline - time.monotonic()))):
            old = _write_lookup(ledger, namespace, intent)
            if old is None:
                _write_blocker(ledger, namespace, intent, provider, project, path)
                # A retained cancellation fences an original arriving after this recovery.
                now_ms = time.time_ns() // 1_000_000
                if (
                    not now_ms - WRITE_INTENT_TTL_MS
                    <= intent.created_at_ms
                    <= now_ms + WRITE_CLOCK_SKEW_MS
                ):
                    raise WriteError("intent_expired", intent)
                cancelled = _write_record(intent, provider, project, path, name, "0" * 64)
                cancelled["phase"] = "cancelled"
                claim = ledger.begin(
                    namespace,
                    intent.request_id,
                    (intent.created_at_ms + WRITE_INTENT_TTL_MS) / 1000,
                    now_ms / 1000,
                    WRITE_CAPACITY,
                    response=_write_json(cancelled),
                )
                if claim != "fresh":
                    raise WriteError("state_unavailable", intent)
                _write_checkpoint(ledger, namespace, intent, cancelled, "refused")
                raise WriteError("cancelled", intent)
            record = _write_cached(old[1], intent, provider, project, path, name, old[0])
            if old[0] == "ok" and record["phase"] == "verified":
                return WriteReceipt(
                    intent.request_id, intent.created_at_ms, record["result"], replayed=True
                )
            if old[0] == "refused":
                if record["phase"] == "cancelled":
                    raise WriteError("cancelled", intent)
                raise _write_refusal(intent, record)
            if record["phase"] == "prepared":
                # Sending is fsynced BEFORE dispatch, under this same lock.
                record["phase"] = "cancelled"
                _write_checkpoint(ledger, namespace, intent, record, "refused")
                raise WriteError("cancelled", intent)
            if record["phase"] != "acknowledged":
                raise WriteError("outcome_unknown", intent)
            token = token if token is not None else resolve_token(env, writer=True)
            verify_service_token(token, project, path, env, writer=True, deadline=deadline)
            return _write_finish_readback(ledger, namespace, intent, record, token, env, deadline)
    except SecretsError:
        raise
    except Exception:
        raise WriteError("state_unavailable", intent) from None


if __name__ == "__main__":
    raise SystemExit(_bounded_read_worker() if sys.argv[1:] == ["--bounded-read-worker"] else 2)
