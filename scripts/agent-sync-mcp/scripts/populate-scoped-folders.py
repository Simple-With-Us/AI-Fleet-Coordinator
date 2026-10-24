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
handoff as install_seat_key.py);  no credential file is read.

Every value READ, source and destination alike, goes through
`fleet-secrets get NAME --path PATH --project ai-fleet-coordinator|fleet-ops`
(kody 4365621961).  The child gets that login's access token as INFISICAL_TOKEN
(one identity for reads and writes, no login per read) and the project ids
pinned to AFC and FLEETOPS below, so each read lands in exactly the workspace
and folder MAPPING names.  fleet-secrets expands Infisical references (Fleet-OPS
root holds its keys as references, INFISICAL.md) and refuses an unresolved one
(exit 5), so a literal ${...} is never copied;  exit 3 means absent.  The values
are deliberately NOT taken from the environment first:  the copy's source of
truth is the named project and path, and a same-named variable in this shell
(a seat's FLEETLINK_TOKEN_<SEAT>, say) would copy a different value.  Only the
write uses Infisical's API, because `fleet-secrets set` writes nothing but its
allowlisted names (core.WRITE_ALLOWLIST:  CACHE_* under /linear and the CLI
logins under /cli-logins).

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

# Pinned on purpose (kody 4365623903, 4366921599):  every copy must read from and
# write to exactly these two projects, and an id taken from the environment (the
# variables fleet-secrets and other tools share) could point all 72 copies at
# another project.  Both are project identifiers, not credentials, and both are
# already committed on main (install_seat_key.py, .github/workflows/worker-deploy.yml,
# scripts/fleet_knobs/source.py).
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
# Exit status when fleet-secrets itself is not installed (the shell's "command
# not found"), apart from 1 for every other stop (kody 4366921905).
MISSING_EXECUTABLE = 127
# Exit status when a fleet-secrets run times out, the status timeout(1) uses
# (kody 4367021960).
TIMED_OUT = 124

# Explicit limits on every Infisical answer (kody 4367021839), the caps
# src/infisical.js INFISICAL_LIMITS puts on one secret's answer:  the body is
# read up to a byte cap (a longer one is refused), then the parsed tree is
# walked without recursion against a depth and a node cap.  Every answer this
# script reads (the login, the token's scope metadata, an upsert's reply) is
# one small object, far inside these caps.
MAX_RESPONSE_BYTES = 256 * 1024
MAX_RESPONSE_DEPTH = 32
MAX_RESPONSE_NODES = 50_000


class ResponseLimitError(ValueError):
    """An Infisical answer over a cap, or not JSON.  Carries no body text."""


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Every 3xx is an error (kody 4366921692):  urllib's default handler
    re-sends the request headers, the bearer token among them, to wherever
    the Location points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(RefuseRedirects)


def urlopen(request, timeout):
    """Every Infisical call this script makes goes through here."""
    return _OPENER.open(request, timeout=timeout)


def _within_shape(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_RESPONSE_NODES or depth > MAX_RESPONSE_DEPTH:
            return False
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return True


def read_json(request, timeout):
    """One Infisical answer as JSON, within the byte, depth and node caps.
    Raises ResponseLimitError (no body text) for anything else."""
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ResponseLimitError("Infisical answer is over the byte cap")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ResponseLimitError("Infisical answer is not JSON within the caps") from None
    if not _within_shape(value):
        raise ResponseLimitError("Infisical answer is past the depth or node cap")
    return value


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
    return read_json(r, timeout=35)


def fleet_secrets_binary():
    return shutil.which("fleet-secrets") or str(
        pathlib.Path.home() / ".local" / "bin" / "fleet-secrets"
    )


def handoff(name):
    """One credential:  the environment first, then `fleet-secrets get` (the
    install_seat_key.py handoff, OPS-818).  Only the name is ever reported."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    binary = fleet_secrets_binary()
    try:
        proc = subprocess.run(
            [binary, "get", name], check=False, capture_output=True, text=True, timeout=60
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        print(
            f"{name} is not in the environment, and fleet-secrets timed out reading it.  "
            "Name only;  no value printed.",
            file=sys.stderr,
        )
        raise SystemExit(TIMED_OUT) from None
    except FileNotFoundError:
        print(
            f"{name} is not in the environment, and fleet-secrets is not installed.  "
            "Name only;  no value printed.",
            file=sys.stderr,
        )
        raise SystemExit(MISSING_EXECUTABLE) from None
    except (OSError, subprocess.SubprocessError):
        pass
    raise SystemExit(
        f"{name} is not in the environment or fleet-secrets.  Name only;  no value printed."
    )


def login():
    body = {
        "clientId": handoff("INFISICAL_AUTOMATION_CLIENT_ID"),
        "clientSecret": handoff("INFISICAL_AUTOMATION_CLIENT_SECRET"),
    }
    data = read_json(
        urllib.request.Request(
            f"{BASE}/api/v1/auth/universal-auth/login",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        ),
        timeout=35,
    )
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("Infisical login answer has no accessToken string")
    return token


# fleet-secrets' read-only project slugs, and the variables it resolves each
# slug's id from first (scripts/fleet_secrets/core.py PROJECT_SOURCES).  The
# child gets the ids below, so no ~/.agent-sync file can point a read elsewhere.
FLEET_SECRETS_PROJECT = {AFC: "ai-fleet-coordinator", FLEETOPS: "fleet-ops"}
FLEET_SECRETS_PROJECT_ID_ENV = {
    AFC: "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID",
    FLEETOPS: "INFISICAL_FLEET_OPS_PROJECT_ID",
}
# Login material this script holds that the child never needs:  it reads with
# the access token alone.
CHILD_DROPPED_ENV = (
    "INFISICAL_AUTOMATION_CLIENT_ID",
    "INFISICAL_AUTOMATION_CLIENT_SECRET",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
    "READ_TOOLS_SERVICE_TOKEN",
)
FLEET_SECRETS_ABSENT = 3  # `fleet-secrets get`:  the key is not in Infisical at that path


class ReadError(Exception):
    """`fleet-secrets get` failed.  Carries its exit code (or a reason word)
    only:  its output is never kept, so no value or server text can leak."""

    def __init__(self, code):
        super().__init__(f"fleet-secrets get failed ({code})")
        self.code = code


def fleet_secrets_get(token, ws, path, name):
    """One value through `fleet-secrets get NAME --path PATH --project P`, or
    None when it exits 3 (absent).  The value comes back byte for byte (stdout
    as bytes, decoded as UTF-8, never stripped).  Any other exit raises
    ReadError with the code only;  stderr is discarded unread."""
    env = {k: v for k, v in os.environ.items() if k not in CHILD_DROPPED_ENV}
    env["INFISICAL_TOKEN"] = token
    env[FLEET_SECRETS_PROJECT_ID_ENV[ws]] = ws
    argv = [
        fleet_secrets_binary(),
        "get",
        name,
        "--path",
        path,
        "--project",
        FLEET_SECRETS_PROJECT[ws],
    ]
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ReadError("timeout") from None
    except FileNotFoundError:
        raise ReadError(MISSING_EXECUTABLE) from None
    except OSError:
        raise ReadError("unavailable") from None
    finally:
        env = {}
    if proc.returncode == FLEET_SECRETS_ABSENT:
        return None
    if proc.returncode != 0:
        raise ReadError(proc.returncode)
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError("bad-output") from None


def read_secret(token, ws, path, name):
    """A source value.  An absent source stops the run (exit 3 -> ReadError)."""
    value = fleet_secrets_get(token, ws, path, name)
    if value is None:
        raise ReadError(FLEET_SECRETS_ABSENT)
    return value


def read_secret_opt(token, ws, path, name):
    """A destination value, or None when it does not exist yet."""
    return fleet_secrets_get(token, ws, path, name)


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
    return read_json(
        urllib.request.Request(
            f"{BASE}/api/v2/service-token",
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        ),
        timeout=35,
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
            "GATE: READ_TOOLS_SERVICE_TOKEN not in env - cannot verify scope.  Aborting (fail safe)."
        )
        return False
    try:
        data = token_data(st)
    except urllib.error.HTTPError as e:
        print(f"GATE: token metadata endpoint returned HTTP {e.code}.  Aborting (fail safe).")
        return False
    except Exception as e:  # noqa: BLE001 - fail-safe gate: ANY metadata failure aborts
        print(
            f"GATE: token metadata endpoint unsupported/error ({type(e).__name__}).  Aborting (fail safe)."
        )
        return False
    granted = parse_scopes(data)
    if granted is None:
        print(
            "GATE: token metadata schema not the expected scopes list of {environment, secretPath}.  "
            "Aborting (fail safe);  confirm the live schema before any run."
        )
        return False
    needed = {(ENV, p) for _, _, _, p, _ in MAPPING}
    missing = sorted(needed - granted)
    if missing:
        print(
            f"GATE: read-tools token does NOT grant: {missing}.  Aborting;  broadening scope is the owner's call."
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
        print("Dry run only.  Step-0 scope gate:")
        if not check_read_tools_scope():
            # Fail closed in a dry run too (kody 4366921815):  a gate that cannot
            # pass is a non-zero exit, never a clean-looking rehearsal.
            sys.exit(1)
        print("Re-run with --apply only after the owner's approval is relayed.")
        return
    if not check_read_tools_scope():
        sys.exit(1)
    token = login()
    done = skipped = 0
    for sws, spath, sname, dpath, dname in MAPPING:
        reading = f"source {'AFC' if sws == AFC else 'Fleet-OPS'} {spath.rstrip('/')}/{sname}"
        try:
            value = read_secret(token, sws, spath, sname)  # in memory only
            reading = f"destination Fleet-OPS {dpath}/{dname}"
            existing = read_secret_opt(token, FLEETOPS, dpath, dname)
            if existing is not None and existing != value:
                value = existing = None
                print(
                    f"COLLISION: Fleet-OPS {dpath}/{dname} already exists with a DIFFERENT value."
                )
                print(
                    "Stopping for reconciliation - no overwrite is authorized by default.  "
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
                f"STOP at {done + skipped + 1}/72 ({dpath}/{dname}): HTTP {e.code}.  "
                f"{done} copied, {skipped} already in sync.  Re-run reconciles;  equal values are skipped."
            )
            sys.exit(1)
        except ReadError as e:
            value = existing = None
            missing_tool = e.code == MISSING_EXECUTABLE
            timed_out = e.code == "timeout"
            if missing_tool:
                what, status = "is not installed", MISSING_EXECUTABLE
            elif timed_out:
                what, status = "timed out", TIMED_OUT
            else:
                what, status = f"exit {e.code}", 1
            print(
                f"STOP at {done + skipped + 1}/72 ({dpath}/{dname}): fleet-secrets get "
                f"{what}, reading the {reading}.  "
                f"{done} copied, {skipped} already in sync.  Re-run reconciles;  equal values are skipped."
            )
            sys.exit(status)
        except Exception as e:  # noqa: BLE001 - any copy failure stops visibly, re-run reconciles
            print(
                f"STOP at {done + skipped + 1}/72 ({dpath}/{dname}): {type(e).__name__}.  "
                f"{done} copied, {skipped} already in sync.  Re-run reconciles;  equal values are skipped."
            )
            sys.exit(1)
        done += 1
        print(f"  copied {done}/72: {dpath}/{dname}")  # names only, never values
    print(
        f"Done: {done} copies applied, {skipped} already in sync.  Sources unmodified;  nothing deleted."
    )


if __name__ == "__main__":
    main()
