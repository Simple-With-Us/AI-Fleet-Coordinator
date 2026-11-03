#!/usr/bin/env python3
"""Load the listener's variables from Infisical at container start, then exec the listener.

    infisical_env.py <program> [args ...]

entrypoint.sh runs this only when INFISICAL_TOKEN and INFISICAL_PROJECT_ID are
set.  The scoped primary service token is used directly as a bearer to read the
configured folder (INFISICAL_SECRET_PATH, default /zulip) and /operator.  Its exact
read-only prod grant must contain only those paths, with duplicates removed.  If
INFISICAL_LISTENER_READ_PROJECT_ID enables the secondary project read, it also requires
INFISICAL_LISTENER_READ_TOKEN, a different read-only token scoped only to prod /zulip
in that project.  The primary token is never reused for the secondary project.  Each
path's token metadata is verified before its raw secret values are requested.  The
loader copies the allowlisted names into
the environment, removes the identity from it, and execs <program> with that environment.  exec keeps the
listener as the container's PID 1, so SIGTERM still reaches the daemon and it deletes its queues on the way out.

The allowlist is built from the seat partition (every seat it gives to "server"), so nothing else in the folder
(BotFleet bots, Mac seats, webhook HEADER entries, generic names) ever reaches the container:

    ZULIP_<CODE>_EMAIL, ZULIP_<CODE>_API_KEY            a seat's Zulip pair
    ZULIP_OUTGOING_<CODE>_TOKEN                         an optional Zulip outgoing-webhook token
    ZULIP_ALERT_<CODE>_ENDPOINT, ZULIP_ALERT_<CODE>_KEY  a Grok Bot routine webhook (URL and bare key)
    <CODE>_ROUTINE_URL, <CODE>_ROUTINE_KEY               the listener's default routine names
    SENTRY_FLEET_DSN, AGENT_SYNC_SENTRY_DSN              the Sentry Crons heartbeat's DSN (heartbeat.py); not per seat
    OPERATOR_ZULIP_USER_ID, OPERATOR_ZULIP_EMAIL         the Operator service bot's own Zulip identity;  a
                                                         user id and an address, never a credential, and
                                                         needed so a seat can answer inside a DM the
                                                         Operator opened (dm_policy.py)
    OPERATOR_NOTES_{WORKER,SERVER,RUNNER}_KEY            the notes queue's three keys, from the folder /operator
                                                         (AFC-486), with the same login

CODE is the seat with hyphens as underscores.  An Infisical value wins over a container variable of the same
name, so a rotation in Infisical takes effect on the next restart even if Coolify still holds an old copy.
The Operator's two names live in the Operator's own folder (/zulip_operator), so a container that reads that
folder has to point INFISICAL_SECRET_PATH at it;  a container that reads /zulip has to carry the two names
there instead.  Either way the host provides them, and the runtime never guesses an id.

Infisical is the intended source of these variables.  Missing or invalid service-token configuration and any
primary fetch failure stop startup; the listener never falls back to container copies.  Errors name the stage and HTTP status,
never a body or a value.  A successful load logs one line with
the variable count and how many GB seats got both Zulip names from Infisical (names only).  No value is ever
printed or logged.
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
from datetime import datetime, timezone
from collections.abc import Callable, Mapping

API_DEFAULT = "https://app.infisical.com/api"
TOKEN = "INFISICAL_TOKEN"
LISTENER_READ_TOKEN = "INFISICAL_LISTENER_READ_TOKEN"
REQUIRED = (TOKEN, "INFISICAL_PROJECT_ID")
SERVICE_TOKEN_RE = re.compile(r"^st\.\S{17,2045}$")
PROJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
UNRESOLVED_REFERENCE_RE = re.compile(r"\$\{[^}]+\}")
MAX_METADATA_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 8.0
ATTEMPTS = 2  # per request; worst case stays well inside the 60-second health start period
PARTITION_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "docs",
    "protocols",
    "agent-sync-partition.toml",
)
SHARED_NAMES = frozenset(
    {
        "SENTRY_FLEET_DSN",
        "AGENT_SYNC_SENTRY_DSN",
        #  The Operator service bot's own Zulip identity.  A user id and an address, never a
        #  credential:  the runtime needs them to tell a DM the Operator opened from one a seat
        #  opened, and it refuses by name rather than guessing when a host does not provide them.
        "OPERATOR_ZULIP_USER_ID",
        "OPERATOR_ZULIP_EMAIL",
    }
)  # the heartbeat's DSN and the Operator's identity;  the first of the two DSN names wins
# The Operator's notes-queue keys (AFC-486, docs/protocols/operator-notes.md), read from their own folder.
OPERATOR_PATH = "/operator"
OPERATOR_NAMES = frozenset(
    {"OPERATOR_NOTES_WORKER_KEY", "OPERATOR_NOTES_SERVER_KEY", "OPERATOR_NOTES_RUNNER_KEY"}
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
    if not isinstance(path, str) or not path.startswith("/") or any(
        part in (".", "..") for part in path.split("/")
    ):
        raise LoadError("configuration: invalid Infisical folder")
    return path


def verify_service_token(
    env: Mapping[str, str],
    token: str,
    *,
    project_id: str,
    expected_paths: tuple[str, ...],
    **kw,
) -> None:
    """Check the token's own exact prod/read grant before requesting secret values."""
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise LoadError("service-token metadata: project id is invalid")
    paths = tuple(dict.fromkeys(_safe_folder(path) for path in expected_paths))
    if not paths:
        raise LoadError("service-token metadata: folder grant is invalid")
    base = api_base(env)
    metadata = _request(
        "service-token metadata",
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
        or metadata.get("projectId") != project_id
        or metadata.get("workspace") != project_id
    ):
        raise LoadError("service-token metadata: project identity did not match")
    scopes = metadata.get("scopes")
    if not isinstance(scopes, list):
        raise LoadError("service-token metadata: folder scope is invalid")
    actual_paths = []
    for scope in scopes:
        if (
            not isinstance(scope, dict)
            or set(scope) != {"environment", "secretPath"}
            or scope.get("environment") != "prod"
            or not isinstance(scope.get("secretPath"), str)
        ):
            raise LoadError("service-token metadata: folder scope is invalid")
        actual_paths.append(_safe_folder(scope["secretPath"]))
    if len(actual_paths) != len(set(actual_paths)) or set(actual_paths) != set(paths):
        raise LoadError("service-token metadata: folder scope did not match")
    permissions = metadata.get("permissions")
    if (
        not isinstance(permissions, list)
        or any(not isinstance(permission, str) for permission in permissions)
        or len(permissions) != len(set(permissions))
        or set(permissions) != {"read"}
    ):
        raise LoadError("service-token metadata: read-only permission did not match")
    expires_at = metadata.get("expiresAt")
    if not isinstance(expires_at, str):
        raise LoadError("service-token metadata: expiry is invalid")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        raise LoadError("service-token metadata: expiry is invalid") from None
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise LoadError("service-token metadata: expiry is invalid")


def fetch(
    env: Mapping[str, str],
    *,
    path: str | None = None,
    token: str | None = None,
    expected_paths: tuple[str, ...] | None = None,
    stage: str = "read",
    strict: bool = False,
    **kw,
) -> dict[str, str]:
    """{name: value} for every secret in one folder using a scoped service token."""
    base = api_base(env)
    if token is None:
        token = (env.get(TOKEN) or "").strip()
    if not isinstance(token, str) or not SERVICE_TOKEN_RE.fullmatch(token):
        raise LoadError("service-token: INFISICAL_TOKEN is missing or invalid")
    project_id = (env.get("INFISICAL_PROJECT_ID") or "").strip()
    environment = (env.get("INFISICAL_ENVIRONMENT") or "prod").strip()
    requested_path = _safe_folder(path or (env.get("INFISICAL_SECRET_PATH") or "/zulip").strip())
    if environment != "prod":
        raise LoadError("configuration: prod is the only supported Infisical environment")
    grant_paths = expected_paths or (requested_path,)
    if requested_path not in grant_paths:
        raise LoadError("service-token metadata: requested folder is outside the verified grant")
    verify_service_token(
        env, token, project_id=project_id, expected_paths=tuple(grant_paths), **kw
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
    got = _request(stage, base + "/v3/secrets/raw?" + query, token=token, max_bytes=MAX_RESPONSE_BYTES, **kw)
    secrets = got.get("secrets")
    if (
        not isinstance(secrets, list)
        or len(secrets) > 2000
        or ("totalCount" in got and got["totalCount"] != len(secrets))
    ):
        raise LoadError("read: no secrets list in the response")
    out: dict[str, str] = {}
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
            or item["secretKey"] in out
        ):
            raise LoadError("%s: invalid or unverified listing" % stage)
        if UNRESOLVED_REFERENCE_RE.search(item["secretValue"]):
            raise LoadError("%s: secret value contains an unresolved reference" % stage)
        out[item["secretKey"]] = item["secretValue"]
    return out


LISTENER_READ_PROJECT = "INFISICAL_LISTENER_READ_PROJECT_ID"
LISTENER_READ_CODES = frozenset(
    {
        "MA",
        "GB_DIRECTOR",
        "GB_FIXER",
        "GB_DESIGNER",
        "GB_COMPILER",
        "GB_HOUSEKEEPER",
        "GB_PUBLISHER",
        "GB_DEPLOYER",
        "GB_MONITOR",
        "GB_PLUMBER",
        "GB_ORACLE",
        "GB_TRADER",
        "GB_ENGINEER",
    }
)


def listener_read_keys(
    env: Mapping[str, str], codes: set[str], **kw
) -> tuple[dict[str, str], str]:
    """An opt-in second project read with its own exact-scope token binding."""
    project = (env.get(LISTENER_READ_PROJECT) or "").strip()
    if not project:
        return {}, ""
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", project):
        return {}, "; listener read keys not loaded (invalid project id)"
    token = (env.get(LISTENER_READ_TOKEN) or "").strip()
    if not SERVICE_TOKEN_RE.fullmatch(token):
        return {}, "; listener read keys not loaded (dedicated service token is missing or invalid)"
    names = {code + "_ROUTINE_KEY" for code in LISTENER_READ_CODES & codes}
    if not names:
        return {}, "; listener read keys: no eligible server seats"
    source = dict(env, INFISICAL_PROJECT_ID=project, INFISICAL_ENVIRONMENT="prod")
    try:
        found = fetch(
            source,
            path="/zulip",
            token=token,
            expected_paths=("/zulip",),
            stage="read listener keys",
            strict=True,
            **kw,
        )
        keys = {
            name: value
            for name, value in found.items()
            if name in names and len(value.strip()) >= 32 and "${" not in value
        }
        return keys, "; listener read keys from secondary native source: %d of %d" % (
            len(keys),
            len(names),
        )
    except LoadError as exc:
        return {}, "; listener read keys not loaded (%s)" % exc


def build_env(env: Mapping[str, str], **kw) -> tuple[dict[str, str], str]:
    """(the environment to exec with, the one log line).  The service token is always removed."""
    out = {k: v for k, v in env.items() if k not in {TOKEN, LISTENER_READ_TOKEN}}
    # Opt-in native secondary keys never fall back to primary-project or container copies.
    secondary_names = {code + "_ROUTINE_KEY" for code in LISTENER_READ_CODES}
    if (env.get(LISTENER_READ_PROJECT) or "").strip():
        for name in secondary_names:
            out.pop(name, None)
    missing = [name for name in REQUIRED if not (env.get(name) or "").strip()]
    if missing:
        raise LoadError("configuration: missing " + ", ".join(missing))
    if not SERVICE_TOKEN_RE.fullmatch((env.get(TOKEN) or "").strip()):
        raise LoadError("service-token: INFISICAL_TOKEN is invalid")
    project_id = (env.get("INFISICAL_PROJECT_ID") or "").strip()
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise LoadError("configuration: INFISICAL_PROJECT_ID is invalid")
    partition = env.get("AGENT_SYNC_PARTITION") or PARTITION_DEFAULT
    try:
        codes = server_codes(partition)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LoadError("partition: " + type(exc).__name__) from None
    allowed = allowed_names(codes)
    if not codes:
        raise LoadError("partition: no seat is assigned to server")
    primary_path = _safe_folder((env.get("INFISICAL_SECRET_PATH") or "/zulip").strip())
    primary_paths = tuple(dict.fromkeys((primary_path, OPERATOR_PATH)))
    try:
        token = (env.get(TOKEN) or "").strip()
        found = fetch(env, token=token, expected_paths=primary_paths, **kw)
    except LoadError as exc:
        raise exc
    # The Operator's notes-queue keys (AFC-486) live in their own folder.  A failed read of it never stops the
    # listener:  the queue then refuses every principal, and the line says so.
    try:
        operator = {
            name: value
            for name, value in fetch(
                env,
                path=OPERATOR_PATH,
                token=token,
                expected_paths=primary_paths,
                stage="read operator",
                **kw,
            ).items()
            if name in OPERATOR_NAMES
        }
        operator_note = "; notes-queue keys from %s: %d of %d" % (
            OPERATOR_PATH,
            len(operator),
            len(OPERATOR_NAMES),
        )
    except LoadError as exc:
        operator, operator_note = {}, "; notes-queue keys not loaded (%s)" % exc
    read_keys, read_note = listener_read_keys(env, codes, **kw)
    if (env.get(LISTENER_READ_PROJECT) or "").strip():
        found = {name: value for name, value in found.items() if name not in secondary_names}
    found = dict(found, **operator, **read_keys)
    allowed = allowed | set(operator)
    loaded = sorted(name for name in found if name in allowed)
    replaced = sum(1 for name in loaded if name in env)
    for name in loaded:
        out[name] = found[name]
    where = "%s %s" % (
        (env.get("INFISICAL_ENVIRONMENT") or "prod").strip(),
        (env.get("INFISICAL_SECRET_PATH") or "/zulip").strip(),
    )
    gb = sorted(code for code in codes if code.startswith("GB_"))
    complete = [c for c in gb if f"ZULIP_{c}_EMAIL" in loaded and f"ZULIP_{c}_API_KEY" in loaded]
    missing_gb = [c for c in gb if c not in complete]
    coverage = f"; GB seats with both Zulip names from Infisical: {len(complete)} of {len(gb)}"
    if missing_gb:
        coverage += " (not loaded: " + ", ".join(c.replace("_", "-") for c in missing_gb) + ")"
    # A seat is only wakeable when its alert endpoint and key are present too:  count them apart.
    # They may come from Coolify copies instead, so this reports, it does not refuse.
    armed = [
        c for c in gb if f"ZULIP_ALERT_{c}_ENDPOINT" in loaded and f"ZULIP_ALERT_{c}_KEY" in loaded
    ]
    coverage += f"; GB seats with alert endpoint and key from Infisical: {len(armed)} of {len(gb)}"
    no_alert = [c for c in gb if c not in armed]
    if no_alert:
        coverage += " (not loaded: " + ", ".join(c.replace("_", "-") for c in no_alert) + ")"
    ignored = len(found) - len(loaded)
    return out, (
        f"loaded {len(loaded)} variables from Infisical ({where}; {replaced} replaced a container value, "
        f"{ignored} other names in the folder ignored){coverage}{operator_note}{read_note}"
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
