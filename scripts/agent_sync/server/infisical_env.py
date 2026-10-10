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
    ZULIP_ALERT_<CODE>_ENDPOINT, ZULIP_ALERT_<CODE>_KEY  a Grok Bot routine webhook (URL and bare key)
    <CODE>_ROUTINE_URL, <CODE>_ROUTINE_KEY               the listener's default routine names

CODE is the seat with hyphens as underscores.  An Infisical value wins over a container variable of the same
name, so a rotation in Infisical takes effect on the next restart even if Coolify still holds an old copy.

On any failure (login refused, network, a bad response) it logs one line naming the stage and the HTTP status,
never a body or a value, and starts the listener with the container's own environment (the identity removed),
so a deployment that still carries Coolify copies keeps working.  No value is ever printed or logged.
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
from typing import Callable, Mapping

API_DEFAULT = "https://app.infisical.com/api"
IDENTITY = ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET")
REQUIRED = IDENTITY + ("INFISICAL_PROJECT_ID",)
TIMEOUT_SECONDS = 8.0
ATTEMPTS = 2                      # per request; worst case stays well inside the 60-second health start period
PARTITION_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "docs",
                                 "protocols", "agent-sync-partition.toml")
_SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")


class LoadError(Exception):
    """A failure that names only the stage and a status:  never a body, a URL query or a value."""


def log(message: str) -> None:
    print("agent-sync: %s" % message, file=sys.stderr, flush=True)


def server_codes(partition_file: str) -> set[str]:
    with open(partition_file, "rb") as fh:
        seats = tomllib.load(fh).get("seats") or {}
    return {str(seat).upper().replace("-", "_") for seat, where in seats.items()
            if where == "server" and _SEAT_RE.match(str(seat).upper())}


def allowed_names(codes: set[str]) -> set[str]:
    names: set[str] = set()
    for code in codes:
        names |= {"ZULIP_%s_EMAIL" % code, "ZULIP_%s_API_KEY" % code,
                  "ZULIP_ALERT_%s_ENDPOINT" % code, "ZULIP_ALERT_%s_KEY" % code,
                  "%s_ROUTINE_URL" % code, "%s_ROUTINE_KEY" % code}
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
    def redirect_request(self, *args, **kwargs):  # never re-send the identity or the token to another URL
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(stage: str, url: str, *, body: dict | None = None, token: str | None = None,
             opener: Callable = _OPENER.open, sleep: Callable[[float], None] = time.sleep) -> dict:
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
            exc.close()                 # the body is never read, let alone logged
            if exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        except (ValueError, UnicodeDecodeError):
            raise LoadError("%s: the response is not JSON" % stage) from None
        if attempt + 1 < ATTEMPTS:
            sleep(2.0)
    raise LoadError("%s: %s" % (stage, last))


def fetch(env: Mapping[str, str], **kw) -> dict[str, str]:
    """{name: value} for every secret in the folder.  Raises LoadError."""
    base = api_base(env)
    login = _request("login", base + "/v1/auth/universal-auth/login",
                     body={"clientId": env["INFISICAL_CLIENT_ID"].strip(),
                           "clientSecret": env["INFISICAL_CLIENT_SECRET"].strip()}, **kw)
    token = login.get("accessToken")
    if not isinstance(token, str) or not token:
        raise LoadError("login: no access token in the response")
    query = urllib.parse.urlencode({
        "workspaceId": env["INFISICAL_PROJECT_ID"].strip(),
        "environment": (env.get("INFISICAL_ENVIRONMENT") or "prod").strip(),
        "secretPath": (env.get("INFISICAL_SECRET_PATH") or "/zulip").strip(),
        "expandSecretReferences": "true",
    })
    got = _request("read", base + "/v3/secrets/raw?" + query, token=token, **kw)
    secrets = got.get("secrets")
    if not isinstance(secrets, list):
        raise LoadError("read: no secrets list in the response")
    out: dict[str, str] = {}
    for item in secrets:
        if isinstance(item, dict) and isinstance(item.get("secretKey"), str) and isinstance(item.get("secretValue"), str):
            out[item["secretKey"]] = item["secretValue"]
    return out


def build_env(env: Mapping[str, str], **kw) -> tuple[dict[str, str], str]:
    """(the environment to exec with, the one log line).  The identity is always removed."""
    out = {k: v for k, v in env.items() if k not in IDENTITY}
    missing = [name for name in REQUIRED if not (env.get(name) or "").strip()]
    if missing:
        return out, "Infisical not used (%s not set); starting with the container environment" % ", ".join(missing)
    partition = env.get("AGENT_SYNC_PARTITION") or PARTITION_DEFAULT
    try:
        allowed = allowed_names(server_codes(partition))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return out, ("Infisical load skipped (cannot read the seat partition: %s); starting with the container "
                     "environment" % type(exc).__name__)
    if not allowed:
        return out, "Infisical load skipped (the seat partition gives no seat to server); starting with the container environment"
    try:
        found = fetch(env, **kw)
    except LoadError as exc:
        return out, "Infisical load failed (%s); starting with the container environment" % exc
    loaded = sorted(name for name in found if name in allowed)
    replaced = sum(1 for name in loaded if name in env)
    for name in loaded:
        out[name] = found[name]
    where = "%s %s" % ((env.get("INFISICAL_ENVIRONMENT") or "prod").strip(),
                       (env.get("INFISICAL_SECRET_PATH") or "/zulip").strip())
    return out, ("loaded %d variables from Infisical (%s; %d replaced a container value, %d other names in the "
                 "folder ignored)" % (len(loaded), where, replaced, len(found) - len(loaded)))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        log("usage: infisical_env.py <program> [args ...]")
        return 2
    env, line = build_env(os.environ)
    log(line)
    os.execve(argv[1], argv[1:], env)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv))
