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
rotation, re-run this script by hand; it overwrites destination values in place.

Values transit this process's memory only: never printed, logged, or persisted.
Step 0 fails safe: the read-tools token's own metadata must confirm every
destination path is in scope, or the run aborts BEFORE any write; if the
metadata endpoint is unsupported or errors, it aborts too (never guesses).
"""
import json, os, pathlib, sys, urllib.parse, urllib.request

AFC = "9bf7417a-fbbb-42ca-870c-2b45207233f5"
FLEETOPS = "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5"
ENV = "prod"

GB_ROLES = ["COMPILER", "DEPLOYER", "DESIGNER", "DIRECTOR", "ENGINEER", "FIXER",
            "HOUSEKEEPER", "MONITOR", "ORACLE", "PLUMBER", "PUBLISHER", "TRADER"]
FLEETLINK_SEATS = ["JET", "ECHO", "WISE", "CURSOR", "CLAUDE", "GROK_WEB"] + [f"GB_{r}" for r in GB_ROLES if r != "ENGINEER"]

# (source_workspace, source_path, source_name, dest_path, dest_name)
MAPPING = (
    [(AFC, "/", "WAKE_HMAC_KEY_JET", "/wake", "WAKE_HMAC_KEY_JET"),
     (AFC, "/jet", "WA_BRIDGE_HMAC_KEY_JET", "/wake", "WA_BRIDGE_HMAC_KEY_JET"),
     (AFC, "/operator-jet-wake", "WA_OPERATOR_HMAC_KEY_JET", "/wake", "WA_OPERATOR_HMAC_KEY_JET")]
    + [(AFC, "/fleetlink", "FLEETLINK_AGENT_SECRET", "/fleetlink", "FLEETLINK_AGENT_SECRET")]
    + [(AFC, "/fleetlink", f"FLEETLINK_TOKEN_{s}", "/fleetlink", f"FLEETLINK_TOKEN_{s}") for s in FLEETLINK_SEATS]
    + [(AFC, "/zulip", f"ZULIP_GB_{r}_EMAIL", "/zulip", f"ZULIP_GB_{r}_EMAIL") for r in GB_ROLES]
    + [(AFC, "/zulip", f"ZULIP_GB_{r}_API_KEY", "/zulip", f"ZULIP_GB_{r}_API_KEY") for r in GB_ROLES]
    + [(AFC, "/linear", f"LINEAR_GB_{r}_CLIENT_ID", "/linear", f"LINEAR_GB_{r}_CLIENT_ID") for r in GB_ROLES]
    + [(AFC, "/linear", f"LINEAR_GB_{r}_CLIENT_SECRET", "/linear", f"LINEAR_GB_{r}_CLIENT_SECRET") for r in GB_ROLES]
    + [(FLEETOPS, "/", "RECALL_API_TOKEN", "/tools", "RECALL_API_TOKEN"),
     (FLEETOPS, "/", "AGENTS_JAYS_SERVICES_ACCESS_SERVICE_TOKEN_CF_ACCESS_CLIENT_ID", "/tools", "RECALL_ACCESS_CLIENT_ID"),
     (FLEETOPS, "/", "AGENTS_JAYS_SERVICES_ACCESS_SERVICE_TOKEN_CF_ACCESS_CLIENT_SECRET", "/tools", "RECALL_ACCESS_CLIENT_SECRET")]
)
assert len(MAPPING) == 72 and len({(d, n) for _, _, _, d, n in MAPPING}) == 72

BASE = "https://app.infisical.com"

def req(url, token, data=None, method=None):
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method=method)
    return json.load(urllib.request.urlopen(r, timeout=35))

def login():
    d = pathlib.Path(os.environ["INFISICAL_AUTOMATION_DIR"])
    body = {"clientId": (d / "username").read_text().strip(),
            "clientSecret": (d / "password").read_text().strip()}
    return json.load(urllib.request.urlopen(urllib.request.Request(
        f"{BASE}/api/v1/auth/universal-auth/login", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=35))["accessToken"]

def read_secret(token, ws, path, name):
    q = urllib.parse.urlencode({"workspaceId": ws, "environment": ENV, "secretPath": path})
    return req(f"{BASE}/api/v3/secrets/raw/{urllib.parse.quote(name)}?{q}", token)["secret"]["secretValue"]

def upsert(token, ws, path, name, value):
    req(f"{BASE}/api/v3/secrets/raw/{urllib.parse.quote(name)}", token,
        data={"workspaceId": ws, "environment": ENV, "secretPath": path, "secretValue": value, "type": "shared"})

def check_read_tools_scope():
    """Fail-safe gate: the read-tools token reports its own scopes; abort on any doubt."""
    st = os.environ.get("READ_TOOLS_SERVICE_TOKEN", "").strip()
    if not st:
        print("GATE: READ_TOOLS_SERVICE_TOKEN not in env - cannot verify scope. Aborting (fail safe).")
        return False
    try:
        data = json.load(urllib.request.urlopen(urllib.request.Request(
            f"{BASE}/api/v2/service-token-data", headers={"Authorization": f"Bearer {st}"}), timeout=35))
    except Exception as e:
        print(f"GATE: token metadata endpoint unsupported/error ({type(e).__name__}). Aborting (fail safe).")
        return False
    scopes = data.get("scopes") or []
    granted = {(s.get("environment"), s.get("secretPath")) for s in scopes}
    needed = {(ENV, p) for _, _, _, p, _ in MAPPING}
    missing = sorted(needed - granted)
    if missing:
        print(f"GATE: read-tools token does NOT grant: {missing}. Aborting; broadening scope is the owner's call.")
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
    done = 0
    for sws, spath, sname, dpath, dname in MAPPING:
        value = read_secret(token, sws, spath, sname)  # in memory only
        upsert(token, FLEETOPS, dpath, dname, value)
        value = None
        done += 1
        print(f"  copied {done}/72: {dpath}/{dname}")  # names only, never values
    print("Done: 72 copies applied. Sources unmodified; nothing deleted.")

if __name__ == "__main__":
    main()
