#!/usr/bin/env python3
"""Load the listener's variables from Infisical at container start, then exec the listener.

    infisical_env.py <program> [args ...]

entrypoint.sh runs this only when INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET and INFISICAL_PROJECT_ID are
all set (a Universal Auth machine identity with read-only access to the folder).  It logs in, reads one folder
(INFISICAL_ENVIRONMENT, default prod; INFISICAL_SECRET_PATH, default /zulip), copies the allowlisted names into
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

Infisical (fleet-secrets' source project) is the intended source of these variables;  Coolify copies are only a
fallback.  On any failure (login refused, network, a bad response) it logs one ERROR line naming the stage and the
HTTP status, never a body or a value, and starts the listener with the container's own environment (the identity
removed), so a deployment that still carries Coolify copies keeps working.  A successful load logs one line with
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
from collections.abc import Callable, Mapping

API_DEFAULT = "https://app.infisical.com/api"
IDENTITY = ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET")
REQUIRED = IDENTITY + ("INFISICAL_PROJECT_ID",)
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
                payload = json.loads(resp.read().decode("utf-8"))
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


def login(env: Mapping[str, str], **kw) -> str:
    """An access token for the machine identity.  Raises LoadError."""
    got = _request(
        "login",
        api_base(env) + "/v1/auth/universal-auth/login",
        body={
            "clientId": env["INFISICAL_CLIENT_ID"].strip(),
            "clientSecret": env["INFISICAL_CLIENT_SECRET"].strip(),
        },
        **kw,
    )
    token = got.get("accessToken")
    if not isinstance(token, str) or not token:
        raise LoadError("login: no access token in the response")
    return token


def fetch(
    env: Mapping[str, str],
    *,
    path: str | None = None,
    token: str | None = None,
    stage: str = "read",
    strict: bool = False,
    **kw,
) -> dict[str, str]:
    """{name: value} for every secret in one folder (default INFISICAL_SECRET_PATH, else /zulip).  Logs in first
    unless `token` is given.  Raises LoadError."""
    base = api_base(env)
    if token is None:
        token = login(env, **kw)
    query = urllib.parse.urlencode(
        {
            "workspaceId": env["INFISICAL_PROJECT_ID"].strip(),
            "environment": (env.get("INFISICAL_ENVIRONMENT") or "prod").strip(),
            "secretPath": path or (env.get("INFISICAL_SECRET_PATH") or "/zulip").strip(),
            "expandSecretReferences": "true",
        }
    )
    got = _request(stage, base + "/v3/secrets/raw?" + query, token=token, **kw)
    secrets = got.get("secrets")
    if not isinstance(secrets, list):
        raise LoadError("read: no secrets list in the response")
    if strict and (len(secrets) > 2000 or
            ("totalCount" in got and got["totalCount"] != len(secrets))):
        raise LoadError("read listener keys: incomplete listing")
    out: dict[str, str] = {}
    for item in secrets:
        if strict and (not isinstance(item, dict) or not isinstance(item.get("secretKey"), str)
                or not isinstance(item.get("secretValue"), str) or item["secretKey"] in out):
            raise LoadError("read listener keys: invalid or duplicate listing")
        if (
            isinstance(item, dict)
            and isinstance(item.get("secretKey"), str)
            and isinstance(item.get("secretValue"), str)
        ):
            out[item["secretKey"]] = item["secretValue"]
    return out


LISTENER_READ_PROJECT = "INFISICAL_LISTENER_READ_PROJECT_ID"
LISTENER_READ_CODES = frozenset({"MA", "GB_DIRECTOR", "GB_FIXER", "GB_DESIGNER", "GB_COMPILER",
    "GB_HOUSEKEEPER", "GB_PUBLISHER", "GB_DEPLOYER", "GB_MONITOR", "GB_PLUMBER",
    "GB_ORACLE", "GB_TRADER", "GB_ENGINEER"})


def listener_read_keys(env: Mapping[str, str], codes: set[str], token: str, **kw) -> tuple[dict[str, str], str]:
    """An opt-in second native read, same identity, only the 13 server-seat HMAC read keys."""
    project = (env.get(LISTENER_READ_PROJECT) or "").strip()
    if not project:
        return {}, ""
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", project):
        return {}, "; listener read keys not loaded (invalid project id)"
    names = {code + "_ROUTINE_KEY" for code in LISTENER_READ_CODES & codes}
    source = dict(env, INFISICAL_PROJECT_ID=project, INFISICAL_ENVIRONMENT="prod")
    try:
        found = fetch(source, path="/zulip", token=token, stage="read listener keys", strict=True, **kw)
        keys = {name: value for name, value in found.items() if name in names
                and len(value.strip()) >= 32 and "${" not in value}
        return keys, "; listener read keys from secondary native source: %d of %d" % (len(keys), len(names))
    except LoadError as exc:
        return {}, "; listener read keys not loaded (%s)" % exc


def build_env(env: Mapping[str, str], **kw) -> tuple[dict[str, str], str]:
    """(the environment to exec with, the one log line).  The identity is always removed."""
    out = {k: v for k, v in env.items() if k not in IDENTITY}
    # Opt-in native secondary keys never fall back to primary-project or container copies.
    secondary_names = {code + "_ROUTINE_KEY" for code in LISTENER_READ_CODES}
    if (env.get(LISTENER_READ_PROJECT) or "").strip():
        for name in secondary_names:
            out.pop(name, None)
    missing = [name for name in REQUIRED if not (env.get(name) or "").strip()]
    if missing:
        return (
            out,
            "Infisical not used (%s not set); starting with the container environment"
            % ", ".join(missing),
        )
    partition = env.get("AGENT_SYNC_PARTITION") or PARTITION_DEFAULT
    try:
        codes = server_codes(partition)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return out, (
            "Infisical load skipped (cannot read the seat partition: %s); starting with the container "
            "environment" % type(exc).__name__
        )
    allowed = allowed_names(codes)
    if not codes:
        return (
            out,
            "Infisical load skipped (the seat partition gives no seat to server); starting with the container environment",
        )
    try:
        token = login(env, **kw)
        found = fetch(env, token=token, **kw)
    except LoadError as exc:
        return out, "Infisical load failed (%s); starting with the container environment" % exc
    # The Operator's notes-queue keys (AFC-486) live in their own folder.  A failed read of it never stops the
    # listener:  the queue then refuses every principal, and the line says so.
    try:
        operator = {
            name: value
            for name, value in fetch(
                env, path=OPERATOR_PATH, token=token, stage="read operator", **kw
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
    read_keys, read_note = listener_read_keys(env, codes, token, **kw)
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
    env, line = build_env(os.environ)
    # A failed or skipped load means the container runs on Coolify copies:  make that stand out in the log.
    log(("ERROR: " if line.startswith(("Infisical load", "Infisical not used")) else "") + line)
    os.execve(argv[1], argv[1:], env)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv))
