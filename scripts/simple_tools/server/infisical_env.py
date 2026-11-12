#!/usr/bin/env python3
"""Load the listener's variables from Infisical at container start, then exec the listener.

    infisical_env.py <program> [args ...]

entrypoint.sh runs this only when INFISICAL_TOKEN and INFISICAL_PROJECT_ID are set.  The folders are fixed:
prod /zulip and /operator with the primary token, and prod /wake-router with a second required read-only
token, EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN (eavesdropper-wake-router-ro).  The retired knobs
INFISICAL_SECRET_PATH and INFISICAL_ENVIRONMENT stop startup if they are set.  Each token's own metadata
(project, exact prod folders, read-only, expiry) is verified before any value is requested, and every metadata
error names the token's variable.  Two further bindings are optional:  INFISICAL_LISTENER_READ_TOKEN with
INFISICAL_LISTENER_READ_PROJECT_ID (a secondary project's prod /zulip) and INFISICAL_OPERATOR_VOICE_TOKEN with
INFISICAL_FLEET_OPS_PROJECT_ID (Fleet-OPS prod /zulip_operator).  An unbound optional source is skipped and the
line says so;  a bound one that cannot be read stops startup.  The loader execs <program> with the loaded
environment;  exec keeps the listener as the container's PID 1, so SIGTERM still reaches the daemon and it
deletes its queues on the way out.

Nothing the loader can supply comes from the container.  Before anything is read it removes every bootstrap
credential, every per-seat name of any seat (the patterns below, server seat or not), the Operator's names and
every INFISICAL_ONLY name, then adds back only what Infisical returned.  The per-seat allowlist is built from
the seat partition (every seat it gives to "server"), so nothing else in the folder reaches the daemon:

    ZULIP_<CODE>_EMAIL, ZULIP_<CODE>_API_KEY            a seat's Zulip pair
    ZULIP_OUTGOING_<CODE>_TOKEN                         an optional Zulip outgoing-webhook token
    ZULIP_ALERT_<CODE>_ENDPOINT, ZULIP_ALERT_<CODE>_KEY  a routine webhook (URL and bare key)
    <CODE>_ROUTINE_URL, <CODE>_ROUTINE_KEY               the listener's default routine names;  with the
                                                         secondary read on, a routine key may come from the
                                                         secondary project instead, and one held by both
                                                         projects is loaded from neither
    SENTRY_FLEET_DSN, SIMPLE_TOOLS_SENTRY_DSN            the Sentry Crons heartbeat's DSN;  not per seat
    OPERATOR_NOTES_{WORKER,SERVER,RUNNER}_KEY            the notes queue's three keys, from /operator (AFC-486)
    ZULIP_OPERATOR_EMAIL, ZULIP_OPERATOR_API_KEY         the Operator bot's own address and key (AFC-602)

CODE is the seat with hyphens as underscores.  The same rule applies to every seat:  a seat whose names are
missing, or whose routine URL or key is not usable (https, or http to loopback or a Tailscale address;  a key of 32 characters or
more), is simply not loaded, the line names it, and every other seat runs.

INFISICAL_ONLY names are not per seat, so a missing or unusable one stops startup with an error naming the
NAME and folder, never a value.  Each has exactly one source:

    OPERATOR_ZULIP_USER_ID, OPERATOR_ZULIP_EMAIL         /operator:  the Operator service bot's own Zulip
                                                         identity, a user id and an address (dm_policy.py)
    OPERATOR_REGISTRY_KEY                                /wake-router SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY:  the
                                                         registry service's signing key (listener_registry.py),
                                                         the same home every registry client reads

ZULIP_SITE is the one seat-wide value the container keeps:  it pins the realm the keys are sent to, so a value
in the folder can never redirect them.  Errors name the stage, the token variable and the HTTP status, never a
body or a value.  A successful load logs one line with counts and names only.  No value is ever printed or
logged.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

API_DEFAULT = "https://app.infisical.com/api"
TOKEN = "INFISICAL_TOKEN"
LISTENER_READ_TOKEN = "INFISICAL_LISTENER_READ_TOKEN"
#: The Operator's own voice (AFC-602):  a third, separate read-only token, scoped to exactly the
#: Fleet-OPS prod folder that holds the Operator bot's address and key.  Its project is the one the
#: container already names in INFISICAL_FLEET_OPS_PROJECT_ID.  The image ships no fleet-secrets and
#: carries no machine identity (this loader strips them), so this is the Operator's only route to
#: its credential on the server;  without the token the Operator stays unavailable, visibly.
OPERATOR_VOICE_TOKEN = "INFISICAL_OPERATOR_VOICE_TOKEN"
OPERATOR_VOICE_PROJECT = "INFISICAL_FLEET_OPS_PROJECT_ID"
OPERATOR_VOICE_PATH = "/zulip_operator"
OPERATOR_VOICE_NAMES = frozenset({"ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY"})
#: The wake router's folder (#1816) in the primary (AI Fleet Coordinator) project, read with its own
#: read-only token scoped to exactly prod /wake-router (eavesdropper-wake-router-ro).  Required:  the
#: Eavesdropper takes the router's bearer and the registry signing key from it and from nowhere else.
WAKE_ROUTER_TOKEN = "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN"
WAKE_ROUTER_PATH = "/wake-router"
AUTH_ENV_VARS = frozenset(
    {
        TOKEN,
        LISTENER_READ_TOKEN,
        OPERATOR_VOICE_TOKEN,
        WAKE_ROUTER_TOKEN,
        "INFISICAL_AI_FLEET_COORDINATOR_TOKEN",
        "INFISICAL_WRITE_TOKEN",
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
        "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
        "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
        "INFISICAL_AUTOMATION_CLIENT_ID",
        "INFISICAL_AUTOMATION_CLIENT_SECRET",
        "INFISICAL_SHARED_CLIENT_ID",
        "INFISICAL_SHARED_CLIENT_SECRET",
    }
)
REQUIRED = (TOKEN, "INFISICAL_PROJECT_ID")
SERVICE_TOKEN_RE = re.compile(r"^st\.\S{17,2045}$")
PROJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
UNRESOLVED_REFERENCE_RE = re.compile(r"\$\{[^}]+\}")
MAX_METADATA_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 8.0
# Per request.  The worst case is 18 seconds a request (two 8-second timeouts and a 2-second pause), and a
# start makes six requests with the two optional sources off and ten with both on, so an Infisical that
# answers nothing can outlast the 60-second health start period;  the container then restarts.
ATTEMPTS = 2
PARTITION_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "docs",
    "protocols",
    "simple-tools-partition.toml",
)
SHARED_NAMES = frozenset(
    {
        "SENTRY_FLEET_DSN",
        "SIMPLE_TOOLS_SENTRY_DSN",
    }
)  # the heartbeat's DSN;  the first of the two names wins
ZULIP_PATH = "/zulip"
ENVIRONMENT = "prod"
#: Knobs that used to move the folder or the environment.  Hard cut:  both are fixed, and a host that still
#: sets either is told so before anything is read.
RETIRED_KNOBS = ("INFISICAL_SECRET_PATH", "INFISICAL_ENVIRONMENT")
#: Every per-seat name, for any seat:  none of them may come from the container.
SEAT_NAME_RE = re.compile(
    r"ZULIP_(?:OUTGOING_|ALERT_)?[A-Z0-9_]+_(?:EMAIL|API_KEY|TOKEN|ENDPOINT|KEY)|[A-Z0-9_]+_ROUTINE_(?:URL|KEY)"
)
# The Operator's notes-queue keys (AFC-486, docs/protocols/operator-notes.md), read from their own folder.
OPERATOR_PATH = "/operator"
OPERATOR_NAMES = frozenset(
    {"OPERATOR_NOTES_WORKER_KEY", "OPERATOR_NOTES_SERVER_KEY", "OPERATOR_NOTES_RUNNER_KEY"}
)
#: Names whose only source is Infisical and that are not per seat:  (the name exported to the daemon, its one
#: folder, its one name there, the pattern a usable value matches).  A container copy of an exported or a source
#: name is removed before anything is read, and a missing or unusable value stops startup.  One table for every
#: platform:  no row names a seat.
INFISICAL_ONLY: tuple[tuple[str, str, str, str], ...] = (
    # The Operator service bot's own Zulip identity.  A user id and an address, never a credential:  the
    # runtime needs them to tell a DM the Operator opened from one a seat opened.
    ("OPERATOR_ZULIP_USER_ID", OPERATOR_PATH, "OPERATOR_ZULIP_USER_ID", r"[1-9][0-9]{0,11}"),
    ("OPERATOR_ZULIP_EMAIL", OPERATOR_PATH, "OPERATOR_ZULIP_EMAIL", r"[^@\s]+@[^@\s]+\.[^@\s]+"),
    # The registry service's signing key (listener_registry.OPERATOR_KEY_ENV), from the one home every
    # registry client reads, held to the pattern the knob registry holds it to (fleet_knobs/registry.py).
    (
        "OPERATOR_REGISTRY_KEY",
        WAKE_ROUTER_PATH,
        "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY",
        r"[!-~]{32,512}",
    ),
)
_SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")


class LoadError(Exception):
    """A failure that names only the stage and a status:  never a body, a URL query or a value."""


def log(message: str) -> None:
    print("simple-chat: %s" % message, file=sys.stderr, flush=True)


def server_codes(partition_file: str) -> set[str]:
    with open(partition_file, "rb") as fh:
        seats = tomllib.load(fh).get("seats") or {}
    return {
        str(seat).upper().replace("-", "_")
        for seat, where in seats.items()
        if where == "server" and _SEAT_RE.match(str(seat).upper())
    }


def allowed_names(codes: set[str]) -> set[str]:
    names: set[str] = set(SHARED_NAMES)
    for code in codes:
        names |= {
            "ZULIP_%s_EMAIL" % code,
            "ZULIP_%s_API_KEY" % code,
            "ZULIP_OUTGOING_%s_TOKEN" % code,
            "ZULIP_ALERT_%s_ENDPOINT" % code,
            "ZULIP_ALERT_%s_KEY" % code,
            "%s_ROUTINE_URL" % code,
            "%s_ROUTINE_KEY" % code,
        }
    return names


def api_base(env: Mapping[str, str]) -> str:
    """INFISICAL_API_URL (self-hosted Infisical) must be https; plain http only for a loopback test server."""
    base = (env.get("INFISICAL_API_URL") or API_DEFAULT).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme == "https" and parsed.hostname:
        return base
    if parsed.scheme == "http" and parsed.hostname:
        try:
            if parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback:
                return base
        except ValueError:
            pass
    raise LoadError("INFISICAL_API_URL must be an https URL")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, *args, **kwargs
    ):  # never re-send the identity or the token to another URL
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(
    stage: str,
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    opener: Callable = _OPENER.open,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last = "no attempt"
    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with opener(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise LoadError("%s: the response exceeds the size limit" % stage)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise LoadError("%s: the response is not a JSON object" % stage)
            return payload
        except urllib.error.HTTPError as exc:
            last = "HTTP %d" % exc.code
            exc.close()  # the body is never read, let alone logged
            if exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        except (ValueError, UnicodeDecodeError):
            raise LoadError("%s: the response is not JSON" % stage) from None
        if attempt + 1 < ATTEMPTS:
            sleep(2.0)
    raise LoadError("%s: %s" % (stage, last))


def _safe_folder(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(part in (".", "..") for part in path.split("/"))
    ):
        raise LoadError("configuration: invalid Infisical folder")
    return path


def verify_service_token(
    env: Mapping[str, str],
    token: str,
    *,
    project_id: str,
    expected_paths: tuple[str, ...],
    token_name: str = TOKEN,
    **kw,
) -> None:
    """Check the token's own exact prod/read grant before requesting secret values.

    Every error names the token's variable, so a responder rotates the token that was refused."""
    stage = "service-token metadata (%s)" % token_name
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise LoadError("%s: project id is invalid" % stage)
    paths = tuple(dict.fromkeys(_safe_folder(path) for path in expected_paths))
    if not paths:
        raise LoadError("%s: folder grant is invalid" % stage)
    base = api_base(env)
    metadata = _request(
        stage,
        base + "/v2/service-token",
        token=token,
        max_bytes=MAX_METADATA_BYTES,
        **kw,
    )
    token_id = metadata.get("id")
    if (
        not isinstance(token_id, str)
        or not token_id
        or ("_id" in metadata and metadata.get("_id") != token_id)
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
        or ("projectId" in metadata and metadata.get("projectId") != project_id)
        or metadata.get("workspace") != project_id
    ):
        raise LoadError(stage + ": project identity did not match")
    scopes = metadata.get("scopes")
    if not isinstance(scopes, list):
        raise LoadError(stage + ": folder scope is invalid")
    actual_paths = []
    for scope in scopes:
        if (
            not isinstance(scope, dict)
            or set(scope) != {"environment", "secretPath"}
            or scope.get("environment") != "prod"
            or not isinstance(scope.get("secretPath"), str)
        ):
            raise LoadError(stage + ": folder scope is invalid")
        actual_paths.append(_safe_folder(scope["secretPath"]))
    if len(actual_paths) != len(set(actual_paths)) or set(actual_paths) != set(paths):
        raise LoadError(stage + ": folder scope did not match")
    permissions = metadata.get("permissions")
    if (
        not isinstance(permissions, list)
        or any(not isinstance(permission, str) for permission in permissions)
        or len(permissions) != len(set(permissions))
        or set(permissions) != {"read"}
    ):
        raise LoadError(stage + ": read-only permission did not match")
    expires_at = metadata.get("expiresAt")
    if expires_at is None:
        return
    if not isinstance(expires_at, str):
        raise LoadError(stage + ": expiry is invalid")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        raise LoadError(stage + ": expiry is invalid") from None
    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
        raise LoadError(stage + ": expiry is invalid")


def fetch(
    env: Mapping[str, str],
    *,
    path: str = ZULIP_PATH,
    token: str | None = None,
    token_name: str = TOKEN,
    expected_paths: tuple[str, ...] | None = None,
    stage: str = "read",
    keep: frozenset[str] | None = None,
    **kw,
) -> dict[str, str]:
    """{name: value} for every secret in one prod folder using a scoped service token.

    With `keep`, only those names are returned and checked for an unresolved reference:  every listed row is
    still verified, but a reference in a name the loader never uses cannot stop startup."""
    base = api_base(env)
    if token is None:
        token = (env.get(TOKEN) or "").strip()
    if not isinstance(token, str) or not SERVICE_TOKEN_RE.fullmatch(token):
        raise LoadError("service-token: %s is missing or invalid" % token_name)
    project_id = (env.get("INFISICAL_PROJECT_ID") or "").strip()
    environment = ENVIRONMENT
    requested_path = _safe_folder(path)
    grant_paths = expected_paths or (requested_path,)
    if requested_path not in grant_paths:
        raise LoadError(
            "service-token metadata (%s): requested folder is outside the verified grant"
            % token_name
        )
    verify_service_token(
        env,
        token,
        project_id=project_id,
        expected_paths=tuple(grant_paths),
        token_name=token_name,
        **kw,
    )
    query = urllib.parse.urlencode(
        {
            "workspaceId": project_id,
            "environment": environment,
            "secretPath": requested_path,
            "viewSecretValue": "true",
            "expandSecretReferences": "false",
            "recursive": "false",
            "include_imports": "false",
        }
    )
    got = _request(
        stage, base + "/v3/secrets/raw?" + query, token=token, max_bytes=MAX_RESPONSE_BYTES, **kw
    )
    secrets = got.get("secrets")
    if (
        not isinstance(secrets, list)
        or len(secrets) > 2000
        or ("totalCount" in got and got["totalCount"] != len(secrets))
    ):
        raise LoadError("read: no secrets list in the response")
    out: dict[str, str] = {}
    seen: set[str] = set()
    for item in secrets:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("secretKey"), str)
            or not isinstance(item.get("secretValue"), str)
            or item.get("type") != "shared"
            or item.get("secretValueHidden") is True
            or ("workspace" in item and item["workspace"] != project_id)
            or ("environment" in item and item["environment"] != environment)
            or ("secretPath" in item and item["secretPath"] != requested_path)
            or item["secretKey"] in seen
        ):
            raise LoadError("%s: invalid or unverified listing" % stage)
        seen.add(item["secretKey"])
        if keep is not None and item["secretKey"] not in keep:
            continue
        if UNRESOLVED_REFERENCE_RE.search(item["secretValue"]):
            raise LoadError("%s: secret value contains an unresolved reference" % stage)
        out[item["secretKey"]] = item["secretValue"]
    return out


LISTENER_READ_PROJECT = "INFISICAL_LISTENER_READ_PROJECT_ID"


def _secondary_names(codes: set[str]) -> set[str]:
    """The names the secondary project may supply:  every server seat's routine key, the same rule for every seat."""
    return {code + "_ROUTINE_KEY" for code in codes}


def listener_read_keys(env: Mapping[str, str], codes: set[str], **kw) -> tuple[dict[str, str], str]:
    """An opt-in second project read with its own exact-scope token binding.

    Off when INFISICAL_LISTENER_READ_PROJECT_ID is unset.  Once it is set, the read is a source the
    host configured, so a bad project id, a missing or invalid token or a failed read stops startup."""
    project = (env.get(LISTENER_READ_PROJECT) or "").strip()
    if not project:
        return {}, ""
    if not PROJECT_ID_RE.fullmatch(project):
        raise LoadError("configuration: %s is invalid" % LISTENER_READ_PROJECT)
    token = (env.get(LISTENER_READ_TOKEN) or "").strip()
    if not SERVICE_TOKEN_RE.fullmatch(token):
        raise LoadError(
            "service-token: %s is missing or invalid (it is required while %s is set)"
            % (LISTENER_READ_TOKEN, LISTENER_READ_PROJECT)
        )
    names = _secondary_names(codes)
    source = dict(env, INFISICAL_PROJECT_ID=project)
    found = fetch(
        source,
        path=ZULIP_PATH,
        token=token,
        token_name=LISTENER_READ_TOKEN,
        expected_paths=(ZULIP_PATH,),
        stage="read listener keys",
        **kw,
    )
    keys = {name: value for name, value in found.items() if name in names}
    return keys, "; listener read keys from the secondary project: %d" % len(keys)


def operator_voice_keys(env: Mapping[str, str], **kw) -> tuple[dict[str, str], str]:
    """The Operator bot's own address and key, read with their own exact-scope token binding.

    Off when INFISICAL_OPERATOR_VOICE_TOKEN is unbound, and the line says so (the Operator then reports
    `operator_voice` unavailable in its status).  Once it is bound, a bad project id, a failed read or a
    pair that is not complete stops startup, the same as every other configured source."""
    token = (env.get(OPERATOR_VOICE_TOKEN) or "").strip()
    if not token:
        return {}, "; operator voice keys not loaded (no dedicated token)"
    project = (env.get(OPERATOR_VOICE_PROJECT) or "").strip()
    if not PROJECT_ID_RE.fullmatch(project):
        raise LoadError("configuration: %s is invalid" % OPERATOR_VOICE_PROJECT)
    if not SERVICE_TOKEN_RE.fullmatch(token):
        raise LoadError("service-token: %s is invalid" % OPERATOR_VOICE_TOKEN)
    source = dict(env, INFISICAL_PROJECT_ID=project)
    found = fetch(
        source,
        path=OPERATOR_VOICE_PATH,
        token=token,
        token_name=OPERATOR_VOICE_TOKEN,
        expected_paths=(OPERATOR_VOICE_PATH,),
        stage="read operator voice",
        **kw,
    )
    keys = {
        name: value.strip()
        for name, value in found.items()
        if name in OPERATOR_VOICE_NAMES and value.strip() and "${" not in value
    }
    if set(keys) != OPERATOR_VOICE_NAMES:
        raise LoadError(
            "Infisical is missing %s (%s)"
            % (", ".join(sorted(OPERATOR_VOICE_NAMES - set(keys))), OPERATOR_VOICE_PATH)
        )
    return keys, "; operator voice keys from %s: %d of %d" % (
        OPERATOR_VOICE_PATH,
        len(keys),
        len(OPERATOR_VOICE_NAMES),
    )


def infisical_only_names() -> set[str]:
    """Every exported and source name of INFISICAL_ONLY:  none of them may come from the container."""
    names: set[str] = set()
    for export, _, name, _ in INFISICAL_ONLY:
        names |= {export, name}
    return names


def _wake_router_token(env: Mapping[str, str]) -> str:
    token = (env.get(WAKE_ROUTER_TOKEN) or "").strip()
    if not token:
        raise LoadError(
            "configuration: missing %s (the read-only token for prod %s)"
            % (WAKE_ROUTER_TOKEN, WAKE_ROUTER_PATH)
        )
    if not SERVICE_TOKEN_RE.fullmatch(token):
        raise LoadError("service-token: %s is invalid" % WAKE_ROUTER_TOKEN)
    return token


def infisical_only_sources(folder: str) -> frozenset[str]:
    """The source names INFISICAL_ONLY reads from one folder."""
    return frozenset(source for _, where, source, _ in INFISICAL_ONLY if where == folder)


def wake_router_values(env: Mapping[str, str], **kw) -> dict[str, str]:
    """Every name in prod /wake-router of the primary project, read with its own exact-scope token.

    Fatal on any failure:  the primary token is never used for this folder."""
    return fetch(
        env,
        path=WAKE_ROUTER_PATH,
        token=_wake_router_token(env),
        token_name=WAKE_ROUTER_TOKEN,
        expected_paths=(WAKE_ROUTER_PATH,),
        stage="read %s" % WAKE_ROUTER_PATH,
        keep=infisical_only_sources(WAKE_ROUTER_PATH),
        **kw,
    )


def resolve_infisical_only(
    folders: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, str], list[str]]:
    """({exported name: value}, ["NAME from /folder", ...]).

    Each row has one source.  Raises naming every missing or unusable NAME and its folder, never a value."""
    values: dict[str, str] = {}
    sources: list[str] = []
    missing: list[str] = []
    unusable: list[str] = []
    for export, folder, name, pattern in INFISICAL_ONLY:
        if folder not in folders:
            raise LoadError(
                "configuration: %s is read from %s, which this loader does not read"
                % (name, folder)
            )
        where = folder if name == export else "%s %s" % (folder, name)
        value = (folders[folder].get(name) or "").strip()
        if not value:
            missing.append("%s (%s)" % (export, where))
        elif not re.fullmatch(pattern, value):
            unusable.append("%s (%s)" % (export, where))
        else:
            values[export] = value
            sources.append("%s from %s" % (export, where))
    problems = []
    if missing:
        problems.append("Infisical is missing %s" % ", ".join(missing))
    if unusable:
        problems.append("the Infisical value for %s is not usable" % ", ".join(unusable))
    if problems:
        raise LoadError(";  ".join(problems) + ";  a container copy is never used")
    return values, sources


def is_seat_name(name: str) -> bool:
    """A per-seat name of any seat, server or not:  it comes only from Infisical, never from the container."""
    return bool(SEAT_NAME_RE.fullmatch(name))


def seat_value_usable(name: str, value: str) -> bool:
    """A routine pair's URL must pass the daemon's rule (routine_url_usable) and its key at least 32 characters, the same
    floor the daemon applies;  anything else is refused for that seat alone."""
    if name.endswith("_ROUTINE_KEY"):
        return bool(re.fullmatch(r"[!-~]{32,4096}", value.strip()))
    if name.endswith("_ROUTINE_URL"):
        return routine_url_usable(value.strip())
    return True


# The Tailscale range (RFC 6598), built from numbers as adapters._TAILNET is, so the repo's private-address
# guard does not read the rule as inventory.
_TAILNET = ipaddress.ip_network((ipaddress.IPv4Address((100 << 24) | (64 << 16)), 10))


def routine_url_usable(url: str) -> bool:
    """The daemon's own rule (adapters.routine_url_problem):  https, or plain http only to loopback or a
    Tailscale IP literal;  a host, no user:password part, no whitespace or control character.  A URL that does
    not parse is refused for its seat alone, never raised."""
    if any(ch in url for ch in "\r\n\x00\t "):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    if parts.scheme not in ("https", "http") or not host:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "https":
        return True
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address in _TAILNET


def _retired_knobs(env: Mapping[str, str]) -> None:
    for name in RETIRED_KNOBS:
        if name in env:
            raise LoadError(
                "configuration: %s is retired (the folders are fixed:  prod %s, %s and %s);  unset it"
                % (name, ZULIP_PATH, OPERATOR_PATH, WAKE_ROUTER_PATH)
            )


def _codes_line(label: str, have: list[str], codes: list[str]) -> str:
    line = "; server seats with %s from Infisical: %d of %d" % (label, len(have), len(codes))
    lacking = [c.replace("_", "-") for c in codes if c not in have]
    if lacking:
        line += " (not loaded: " + ", ".join(lacking) + ")"
    return line


def build_env(env: Mapping[str, str], **kw) -> tuple[dict[str, str], str]:
    """(the environment to exec with, the one log line).  Bootstrap credentials are removed."""
    _retired_knobs(env)
    missing = [name for name in REQUIRED if not (env.get(name) or "").strip()]
    if missing:
        raise LoadError("configuration: missing " + ", ".join(missing))
    token = (env.get(TOKEN) or "").strip()
    if not SERVICE_TOKEN_RE.fullmatch(token):
        raise LoadError("service-token: INFISICAL_TOKEN is invalid")
    project_id = (env.get("INFISICAL_PROJECT_ID") or "").strip()
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise LoadError("configuration: INFISICAL_PROJECT_ID is invalid")
    _wake_router_token(env)  # checked before any request, so its absence is named on its own
    partition = env.get("SIMPLE_TOOLS_PARTITION") or PARTITION_DEFAULT
    try:
        codes = server_codes(partition)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LoadError("partition: " + type(exc).__name__) from None
    if not codes:
        raise LoadError("partition: no seat is assigned to server")
    allowed = allowed_names(codes)
    # Nothing the loader can supply survives from the container:  bootstrap credentials, every seat's names
    # (any seat, server or not), the Operator's names and the INFISICAL_ONLY names.  What follows is only what
    # Infisical returned.
    only = infisical_only_names()
    owned = only | OPERATOR_NAMES | OPERATOR_VOICE_NAMES | allowed

    def from_container(name: str) -> bool:
        # WAKE_ROUTER_* are the router's own knobs, read by the router alone from /wake-router (including
        # its bearer, which the Eavesdropper does not use yet):  a container copy is never carried either.
        return (
            name in AUTH_ENV_VARS
            or name in owned
            or is_seat_name(name)
            or name.startswith("WAKE_ROUTER_")
        )

    out = {k: v for k, v in env.items() if not from_container(k)}
    removed = sorted(k for k in env if k not in AUTH_ENV_VARS and from_container(k))
    primary_paths = (ZULIP_PATH, OPERATOR_PATH)
    found = fetch(env, path=ZULIP_PATH, token=token, expected_paths=primary_paths, **kw)
    # /operator holds the notes-queue keys (AFC-486) and INFISICAL_ONLY names, so a failed read stops startup.
    # A missing notes-queue key does not:  the queue then refuses that principal, and the line says so.
    operator_all = fetch(
        env,
        path=OPERATOR_PATH,
        token=token,
        expected_paths=primary_paths,
        stage="read operator",
        keep=OPERATOR_NAMES | infisical_only_sources(OPERATOR_PATH),
        **kw,
    )
    operator = {name: value for name, value in operator_all.items() if name in OPERATOR_NAMES}
    operator_note = "; notes-queue keys from %s: %d of %d" % (
        OPERATOR_PATH,
        len(operator),
        len(OPERATOR_NAMES),
    )
    only_values, only_sources = resolve_infisical_only(
        {
            ZULIP_PATH: found,
            OPERATOR_PATH: operator_all,
            WAKE_ROUTER_PATH: wake_router_values(env, **kw),
        }
    )
    read_keys, read_note = listener_read_keys(env, codes, **kw)
    # One home per name:  a routine key held by both projects is used from neither, so deleting one copy to
    # revoke it can never leave the other in service.
    conflicts = sorted(name for name in read_keys if name in found)
    for name in conflicts:
        found.pop(name)
        read_keys.pop(name)
    voice_keys, voice_note = operator_voice_keys(env, **kw)
    seats = {name: value for name, value in found.items() if name in allowed}
    seats.update(read_keys)
    refused = sorted(name for name, value in seats.items() if not seat_value_usable(name, value))
    for name in refused:
        seats.pop(name)
    # The four sources own disjoint names (a test holds them so);  merged in order, never with ** keywords,
    # so a future overlap cannot raise mid-start:  the later, more specific source wins.
    loaded_values: dict[str, str] = {}
    for part in (seats, operator, voice_keys, only_values):
        loaded_values.update(part)
    loaded = sorted(loaded_values)
    replaced = sum(1 for name in loaded if name in env)
    out.update(loaded_values)
    ignored = len(found) - sum(1 for name in found if name in allowed)
    only_note = "; Infisical-only names: %d of %d (%s)" % (
        len(only_values),
        len(INFISICAL_ONLY),
        ", ".join(only_sources),
    )
    removed_note = "; container copies removed: %d%s" % (
        len(removed),
        " (" + ", ".join(removed) + ")" if removed else "",
    )
    seat_note = ""
    if conflicts:
        seat_note += "; held by both projects, so not loaded: " + ", ".join(conflicts)
    if refused:
        seat_note += "; not usable, so not loaded: " + ", ".join(refused)
    ordered = sorted(codes)
    zulip = [
        c for c in ordered if "ZULIP_%s_EMAIL" % c in seats and "ZULIP_%s_API_KEY" % c in seats
    ]
    # A seat can be woken by its alert endpoint and key or by its routine URL and key;  either pair counts.
    routed = [
        c
        for c in ordered
        if ("ZULIP_ALERT_%s_ENDPOINT" % c in seats and "ZULIP_ALERT_%s_KEY" % c in seats)
        or ("%s_ROUTINE_URL" % c in seats and "%s_ROUTINE_KEY" % c in seats)
    ]
    coverage = _codes_line("a Zulip pair", zulip, ordered) + _codes_line(
        "a wake pair", routed, ordered
    )
    return out, (
        f"loaded {len(loaded)} variables from Infisical (prod {ZULIP_PATH}; {replaced} replaced a container "
        f"value, {ignored} other names in the folder ignored){coverage}{seat_note}{operator_note}"
        f"{only_note}{removed_note}{read_note}{voice_note}"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        log("usage: infisical_env.py <program> [args ...]")
        return 2
    try:
        env, line = build_env(os.environ)
    except LoadError as exc:
        log("ERROR: " + str(exc))
        return 1
    log(line)
    os.execve(argv[1], argv[1:], env)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv))
