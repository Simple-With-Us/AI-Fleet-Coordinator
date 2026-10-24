#!/usr/bin/env python3
"""Populate the scoped Fleet-OPS folders for the ST-MCP runtime (AFC-478 follow-through).

DO NOT RUN --apply UNTIL THE OWNER'S APPROVAL IS RELAYED.  Default is --dry-run:
prints the 72-entry mapping (names only, never values) and checks the read-tools
token's scope WITHOUT writing anything.

Copies 72 existing credentials (no new secrets, no deletions, no source edits):
  A   3 wake keys        AFC -> Fleet-OPS /wake
  B  18 FleetLink        AFC /fleetlink -> Fleet-OPS /fleetlink
  C  48 Grok-Bot fields  AFC /zulip + /linear -> Fleet-OPS /zulip + /linear
  D   3 recall           Fleet-OPS / -> Fleet-OPS /tools (2 renamed)
Copies are POINT-IN-TIME: they do NOT track source rotation.  After a source
rotation the Fleet-OPS copy must be reconciled by hand:  a re-run stops at the
first destination that holds a different value (see below) and never overwrites.

The automation login comes from the environment first, then `fleet-secrets get`
(INFISICAL_AUTOMATION_CLIENT_ID and INFISICAL_AUTOMATION_CLIENT_SECRET, the same
handoff as install_seat_key.py);  no credential file is read.  The values
themselves move through Infisical's API because the copy crosses projects and
paths and writes, which `fleet-secrets get` cannot do.

Values transit this process's memory only: never printed, logged, or persisted.
Step 0 fails safe: the read-tools token's own metadata (GET /api/v2/service-token,
verified against current Infisical docs; the old service-token-data path 404s)
must confirm every destination path is in scope, or the run aborts BEFORE any
write; an endpoint error OR an unrecognized schema aborts too (never guesses).
Existing destinations are read first: an equal value is skipped, a DIFFERENT
value stops the run for reconciliation - no blind overwrite.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

AFC = "9bf7417a-fbbb-42ca-870c-2b45207233f5"
FLEETOPS = "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5"
ENV = "prod"

GB_ROLES = [
    "COMPILER",
    "DEPLOYER",
    "DESIGNER",
    "DIRECTOR",
    "ENGINEER",
    "FIXER",
    "HOUSEKEEPER",
    "MONITOR",
    "ORACLE",
    "PLUMBER",
    "PUBLISHER",
    "TRADER",
]
FLEETLINK_SEATS = ["JET", "ECHO", "WISE", "CURSOR", "CLAUDE", "GROK_WEB"] + [
    f"GB_{r}" for r in GB_ROLES if r != "ENGINEER"
]

# (source_workspace, source_path, source_name, dest_path, dest_name)
MAPPING = (
    [
        (AFC, "/", "WAKE_HMAC_KEY_JET", "/wake", "WAKE_HMAC_KEY_JET"),
        (AFC, "/jet", "WA_BRIDGE_HMAC_KEY_JET", "/wake", "WA_BRIDGE_HMAC_KEY_JET"),
        (
            AFC,
            "/operator-jet-wake",
            "WA_OPERATOR_HMAC_KEY_JET",
            "/wake",
            "WA_OPERATOR_HMAC_KEY_JET",
        ),
    ]
    + [(AFC, "/fleetlink", "FLEETLINK_AGENT_SECRET", "/fleetlink", "FLEETLINK_AGENT_SECRET")]
    + [
        (AFC, "/fleetlink", f"FLEETLINK_TOKEN_{s}", "/fleetlink", f"FLEETLINK_TOKEN_{s}")
        for s in FLEETLINK_SEATS
    ]
    + [(AFC, "/zulip", f"ZULIP_GB_{r}_EMAIL", "/zulip", f"ZULIP_GB_{r}_EMAIL") for r in GB_ROLES]
    + [
        (AFC, "/zulip", f"ZULIP_GB_{r}_API_KEY", "/zulip", f"ZULIP_GB_{r}_API_KEY")
        for r in GB_ROLES
    ]
    + [
        (AFC, "/linear", f"LINEAR_GB_{r}_CLIENT_ID", "/linear", f"LINEAR_GB_{r}_CLIENT_ID")
        for r in GB_ROLES
    ]
    + [
        (AFC, "/linear", f"LINEAR_GB_{r}_CLIENT_SECRET", "/linear", f"LINEAR_GB_{r}_CLIENT_SECRET")
        for r in GB_ROLES
    ]
    + [
        (FLEETOPS, "/", "RECALL_API_TOKEN", "/tools", "RECALL_API_TOKEN"),
        (
            FLEETOPS,
            "/",
            "AGENTS_JAYS_SERVICES_ACCESS_SERVICE_TOKEN_CF_ACCESS_CLIENT_ID",
            "/tools",
            "RECALL_ACCESS_CLIENT_ID",
        ),
        (
            FLEETOPS,
            "/",
            "AGENTS_JAYS_SERVICES_ACCESS_SERVICE_TOKEN_CF_ACCESS_CLIENT_SECRET",
            "/tools",
            "RECALL_ACCESS_CLIENT_SECRET",
        ),
    ]
)
assert len(MAPPING) == 72 and len({(d, n) for _, _, _, d, n in MAPPING}) == 72

BASE = "https://app.infisical.com"
# An explicit, project-identifying User-Agent on every Infisical call:  the
# runtime default (Python-urllib/3.x) is what protected endpoints refuse.
USER_AGENT = "AI-Fleet-Coordinator populate-scoped-folders"


def req(url, token, data=None, method=None):
    r = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    return json.load(urllib.request.urlopen(r, timeout=35))


def handoff(name):
    """One credential:  the environment first, then `fleet-secrets get` (the
    install_seat_key.py handoff, OPS-818).  Only the name is ever reported."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    binary = shutil.which("fleet-secrets") or str(
        pathlib.Path.home() / ".local" / "bin" / "fleet-secrets"
    )
    try:
        proc = subprocess.run([binary, "get", name], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise SystemExit(
        f"{name} is not in the environment or fleet-secrets (name only; no value printed)"
    )


def login():
    body = {
        "clientId": handoff("INFISICAL_AUTOMATION_CLIENT_ID"),
        "clientSecret": handoff("INFISICAL_AUTOMATION_CLIENT_SECRET"),
    }
    data = json.load(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{BASE}/api/v1/auth/universal-auth/login",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            ),
            timeout=35,
        )
    )
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("Infisical login answer has no accessToken string")
    return token


def secret_value(data):
    """The secretValue of a /api/v3/secrets/raw/<name> answer, shape-checked
    before use:  a malformed third-party answer raises ValueError (names only),
    never a KeyError, and never sends a non-string value on to upsert."""
    secret = data.get("secret") if isinstance(data, dict) else None
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    if not isinstance(value, str):
        raise ValueError("Infisical secret answer has no secretValue string")
    return value


def read_secret(token, ws, path, name):
    q = urllib.parse.urlencode({"workspaceId": ws, "environment": ENV, "secretPath": path})
    return secret_value(req(f"{BASE}/api/v3/secrets/raw/{urllib.parse.quote(name)}?{q}", token))


def read_secret_opt(token, ws, path, name):
    """Read or None on 404.  Error BODIES are never surfaced: an HTTP error carries
    only its status code so a server message can never leak a value."""
    q = urllib.parse.urlencode({"workspaceId": ws, "environment": ENV, "secretPath": path})
    try:
        return secret_value(req(f"{BASE}/api/v3/secrets/raw/{urllib.parse.quote(name)}?{q}", token))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def upsert(token, ws, path, name, value):
    req(
        f"{BASE}/api/v3/secrets/raw/{urllib.parse.quote(name)}",
        token,
        data={
            "workspaceId": ws,
            "environment": ENV,
            "secretPath": path,
            "secretValue": value,
            "type": "shared",
        },
    )


def token_data(token):
    """Self-metadata for a SERVICE token.  Verified against current Infisical docs
    (https://infisical.com/docs/api-reference/endpoints/service-tokens/get, GET
    /api/v2/service-token; the old /api/v2/service-token-data path 404s).  The docs
    leave `scopes` untyped and mark the endpoint deprecated, so the parser below is
    STRICT: any shape other than a list of {environment, secretPath} strings aborts
    fail-safe.  One live response with the real read-tools token is still required
    at execution time; an unrecognized schema keeps this a blocker, never a pass."""
    return json.load(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{BASE}/api/v2/service-token",
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            ),
            timeout=35,
        )
    )


def parse_scopes(data):
    """Return {(environment, secretPath)} or None when the schema is not exactly as expected."""
    # The top level is checked too:  a non-object answer is the same fail-safe
    # None, never an AttributeError.
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if not isinstance(scopes, list):
        return None
    out = set()
    for s in scopes:
        if (
            not isinstance(s, dict)
            or not isinstance(s.get("environment"), str)
            or not isinstance(s.get("secretPath"), str)
        ):
            return None
        out.add((s["environment"], s["secretPath"]))
    return out


def check_read_tools_scope():
    """Fail-safe gate: the read-tools token reports its own scopes; abort on any doubt."""
    st = os.environ.get("READ_TOOLS_SERVICE_TOKEN", "").strip()
    if not st:
        print(
            "GATE: READ_TOOLS_SERVICE_TOKEN not in env - cannot verify scope. Aborting (fail safe)."
        )
        return False
    try:
        data = token_data(st)
    except urllib.error.HTTPError as e:
        print(f"GATE: token metadata endpoint returned HTTP {e.code}. Aborting (fail safe).")
        return False
    except Exception as e:  # noqa: BLE001 - fail-safe gate: ANY metadata failure aborts
        print(
            f"GATE: token metadata endpoint unsupported/error ({type(e).__name__}). Aborting (fail safe)."
        )
        return False
    granted = parse_scopes(data)
    if granted is None:
        print(
            "GATE: token metadata schema not the expected scopes list of {environment, secretPath}. "
            "Aborting (fail safe); confirm the live schema before any run."
        )
        return False
    needed = {(ENV, p) for _, _, _, p, _ in MAPPING}
    missing = sorted(needed - granted)
    if missing:
        print(
            f"GATE: read-tools token does NOT grant: {missing}. Aborting; broadening scope is the owner's call."
        )
        return False
    print(f"GATE: read-tools token covers all {len(needed)} destination paths.")
    return True


def main():
    dry = "--apply" not in sys.argv
    print(f"{'DRY RUN - no writes' if dry else 'APPLYING'}: {len(MAPPING)} copies")
    for sws, spath, sname, dpath, dname in MAPPING:
        src = "AFC" if sws == AFC else "Fleet-OPS"
        print(f"  {src} {spath or '/'}/{sname}  ->  Fleet-OPS {dpath}/{dname}")
    if dry:
        print("Dry run only. Step-0 scope gate:")
        check_read_tools_scope()
        print("Re-run with --apply only after the owner's approval is relayed.")
        return
    if not check_read_tools_scope():
        sys.exit(1)
    token = login()
    done = skipped = 0
    for sws, spath, sname, dpath, dname in MAPPING:
        try:
            value = read_secret(token, sws, spath, sname)  # in memory only
            existing = read_secret_opt(token, FLEETOPS, dpath, dname)
            if existing is not None and existing != value:
                value = existing = None
                print(
                    f"COLLISION: Fleet-OPS {dpath}/{dname} already exists with a DIFFERENT value."
                )
                print(
                    "Stopping for reconciliation - no overwrite is authorized by default. "
                    "Reconcile the destination by hand, then re-run."
                )
                sys.exit(2)
            if existing is not None:
                value = existing = None
                skipped += 1
                print(f"  already in sync: {dpath}/{dname}")  # names only, never values
                continue
            upsert(token, FLEETOPS, dpath, dname, value)
            value = existing = None
        except urllib.error.HTTPError as e:
            print(
                f"STOP at {done + skipped + 1}/72 ({dpath}/{dname}): HTTP {e.code}. "
                f"{done} copied, {skipped} already in sync. Re-run reconciles; equal values are skipped."
            )
            sys.exit(1)
        except Exception as e:  # noqa: BLE001 - any copy failure stops visibly, re-run reconciles
            print(
                f"STOP at {done + skipped + 1}/72 ({dpath}/{dname}): {type(e).__name__}. "
                f"{done} copied, {skipped} already in sync. Re-run reconciles; equal values are skipped."
            )
            sys.exit(1)
        done += 1
        print(f"  copied {done}/72: {dpath}/{dname}")  # names only, never values
    print(
        f"Done: {done} copies applied, {skipped} already in sync. Sources unmodified; nothing deleted."
    )


if __name__ == "__main__":
    main()
