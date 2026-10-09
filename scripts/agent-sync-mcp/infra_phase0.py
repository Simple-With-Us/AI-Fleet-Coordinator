#!/usr/bin/env python3
"""Cloudflare setup and checks for the agent-sync MCP Worker, Phase 0.

Every step is a dry run unless --apply is given.  The Cloudflare Global key is
read inside this process from the handoff file and never printed, logged or
put on a command line.  Run from scripts/agent-sync-mcp/ (see DEPLOY-PHASE0.md).

    python3 -I infra_phase0.py status            # read-only inventory
    python3 -I infra_phase0.py access [--apply]  # path-scoped Access app
    python3 -I infra_phase0.py kv [--apply]      # OAUTH_KV namespace
    python3 -I infra_phase0.py write-config [--apply]  # fill wrangler.jsonc ids
    python3 -I infra_phase0.py dns [--apply]     # back up, then delete the tunnel CNAME
    python3 -I infra_phase0.py tunnel [--apply]  # optional: drop the stale ingress rule
    python3 -I infra_phase0.py check             # read-only post-deploy checks

Standard library only.  Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ACCOUNT_ID = "3a9368057468d0909cafaa85df12d1b7"  # Usage.Jays.Services (decision D1)
ZONE_NAME = "jays.services"
HOST = "agent-sync.jays.services"
ISSUER = f"https://{HOST}"
RESOURCE = f"{ISSUER}/mcp"
TEAM_DOMAIN = "silent-frost-37e0.cloudflareaccess.com"
TUNNEL_NAME = "Jay's Tunnel"
MIRROR_APP_UUID = "1e1a5fc4-0f7d-44ac-bb0d-5fa6d5d73ddf"  # agents.jays.services
ACCESS_APP_NAME = "agent-sync.jays.services consent and admin"
ACCESS_PATHS = [f"{HOST}/authorize", f"{HOST}/admin"]
OWNER_EMAILS = ["mail@jays.services", "jaywedgeworth22@gmail.com"]
KV_TITLE = "agent-sync-mcp-OAUTH_KV"
WORKER_NAME = "agent-sync-mcp"
HANDOFF = pathlib.Path.home() / ".secrets" / "global-api-keys"
HERE = pathlib.Path(__file__).resolve().parent
WRANGLER = HERE / "wrangler.jsonc"
BACKUP_DIR = pathlib.Path.home() / ".agent-sync-mcp-phase0"

API = "https://api.cloudflare.com/client/v4"


# ---------------------------------------------------------------- credentials


def _load(name: str) -> str:
    try:
        with HANDOFF.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(name + "="):
                    value = line.split("=", 1)[1].strip().strip('"')
                    if value:
                        return value
    except OSError:
        pass
    raise SystemExit(f"error: {name} is not in the handoff file (name only; no value printed)")


class Cloudflare:
    def __init__(self) -> None:
        self._email = _load("CLOUDFLARE_JAY_ACCOUNT_EMAIL")
        self._key = _load("CLOUDFLARE_JAY_API_KEY")

    def call(self, method: str, path: str, body: dict | None = None, **params) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Auth-Email", self._email)
        req.add_header("X-Auth-Key", self._key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read() or b"{}")
            except ValueError:
                payload = {}
            return {"success": False, "status": err.code, "errors": payload.get("errors")}

    def ok(self, method: str, path: str, body: dict | None = None, **params) -> dict:
        res = self.call(method, path, body, **params)
        if not res.get("success"):
            raise SystemExit(f"error: {method} {path} failed: {json.dumps(res.get('errors'))}")
        return res


def say(text: str = "") -> None:
    print(text, flush=True)


def plan(apply: bool, what: str) -> bool:
    say(("APPLY  " if apply else "DRY RUN  ") + what)
    return apply


# ---------------------------------------------------------------- lookups


def zone_id(cf: Cloudflare) -> str:
    res = cf.ok("GET", "/zones", name=ZONE_NAME)
    for z in res.get("result") or []:
        if z["account"]["id"] == ACCOUNT_ID:
            return z["id"]
    raise SystemExit(f"error: zone {ZONE_NAME} not on account {ACCOUNT_ID}")


def dns_records(cf: Cloudflare, zid: str) -> list[dict]:
    return cf.ok("GET", f"/zones/{zid}/dns_records", name=HOST).get("result") or []


def tunnel(cf: Cloudflare) -> dict | None:
    for t in cf.ok("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel", is_deleted="false").get("result") or []:
        if t.get("name") == TUNNEL_NAME:
            return t
    return None


def tunnel_config(cf: Cloudflare, tid: str) -> dict:
    return (cf.ok("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tid}/configurations").get("result") or {}).get("config") or {}


def access_app(cf: Cloudflare) -> dict | None:
    for app in cf.ok("GET", f"/accounts/{ACCOUNT_ID}/access/apps").get("result") or []:
        uris = [d.get("uri") for d in app.get("destinations") or [] if isinstance(d, dict)] + [app.get("domain")]
        if app.get("name") == ACCESS_APP_NAME or any(u and u.startswith(HOST) for u in uris):
            return app
    return None


def kv_namespace(cf: Cloudflare) -> dict | None:
    for ns in cf.ok("GET", f"/accounts/{ACCOUNT_ID}/storage/kv/namespaces", per_page=100).get("result") or []:
        if ns.get("title") == KV_TITLE:
            return ns
    return None


def custom_domains(cf: Cloudflare) -> list[dict]:
    return cf.ok("GET", f"/accounts/{ACCOUNT_ID}/workers/domains", hostname=HOST).get("result") or []


# ---------------------------------------------------------------- steps


def cmd_status(cf: Cloudflare, _apply: bool) -> None:
    zid = zone_id(cf)
    say(f"zone {ZONE_NAME}: {zid}")
    for r in dns_records(cf, zid):
        say(f"dns: {r['type']} {r['name']} -> {r['content']} proxied={r.get('proxied')} id={r['id']}")
    t = tunnel(cf)
    if t:
        rules = [r for r in tunnel_config(cf, t["id"]).get("ingress") or [] if r.get("hostname") == HOST]
        say(f"tunnel {TUNNEL_NAME} ({t['id']}, {t.get('status')}): {len(rules)} ingress rule(s) for {HOST}")
        for r in rules:
            say(f"  ingress: {r.get('hostname')} -> {r.get('service')}")
    for d in custom_domains(cf):
        say(f"worker custom domain: {d.get('hostname')} -> {d.get('service')} ({d.get('environment')})")
    app = access_app(cf)
    say(f"access app: {app['name']} aud={app.get('aud')} session={app.get('session_duration')}" if app else "access app: none")
    ns = kv_namespace(cf)
    say(f"kv: {ns['title']} id={ns['id']}" if ns else "kv: none")


def cmd_access(cf: Cloudflare, apply: bool) -> None:
    existing = access_app(cf)
    if existing:
        say(f"exists: {existing['name']} ({existing['id']}) aud={existing.get('aud')}")
        say(f"destinations: {json.dumps(existing.get('destinations'))}")
        return
    mirror = cf.ok("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{MIRROR_APP_UUID}").get("result") or {}
    body = {
        "name": ACCESS_APP_NAME,
        "type": "self_hosted",
        "domain": ACCESS_PATHS[0],
        "destinations": [{"type": "public", "uri": uri} for uri in ACCESS_PATHS],
        # Spec 3.3:  a 15-minute session for the consent and admin paths.
        "session_duration": "15m",
        "allowed_idps": mirror.get("allowed_idps") or [],
        "auto_redirect_to_identity": bool(mirror.get("auto_redirect_to_identity")),
        "app_launcher_visible": False,
        "policies": [
            {
                "name": "Jay emails only",
                "decision": "allow",
                "include": [{"email": {"email": e}} for e in OWNER_EMAILS],
                "precedence": 1,
            }
        ],
    }
    say(json.dumps(body, indent=1))
    if not plan(apply, f"create Access app '{ACCESS_APP_NAME}' for {', '.join(ACCESS_PATHS)}"):
        return
    res = cf.ok("POST", f"/accounts/{ACCOUNT_ID}/access/apps", body).get("result") or {}
    say(f"created {res.get('id')} aud={res.get('aud')}")
    pols = cf.call("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{res.get('id')}/policies").get("result") or []
    if not any(p.get("decision") == "allow" for p in pols):
        say("WARNING: the app has no allow policy.  Nobody can pass it, which fails closed.  Add 'Jay emails only' in the dashboard.")


def cmd_kv(cf: Cloudflare, apply: bool) -> None:
    ns = kv_namespace(cf)
    if ns:
        say(f"exists: {ns['title']} id={ns['id']}")
        return
    if plan(apply, f"create KV namespace {KV_TITLE}"):
        res = cf.ok("POST", f"/accounts/{ACCOUNT_ID}/storage/kv/namespaces", {"title": KV_TITLE}).get("result") or {}
        say(f"created {KV_TITLE} id={res.get('id')}")


def cmd_write_config(cf: Cloudflare, apply: bool) -> None:
    text = WRANGLER.read_text(encoding="utf-8")
    ns = kv_namespace(cf)
    app = access_app(cf)
    if not ns or not app:
        raise SystemExit("error: run 'kv --apply' and 'access --apply' first")
    new = text.replace("REPLACE_WITH_OAUTH_KV_ID", ns["id"]).replace("REPLACE_WITH_ACCESS_AUD", app["aud"])
    if new == text:
        say("wrangler.jsonc already has both ids")
        return
    say(f"OAUTH_KV id -> {ns['id']}")
    say(f"ACCESS_AUD -> {app['aud']}")
    if plan(apply, "write wrangler.jsonc"):
        WRANGLER.write_text(new, encoding="utf-8")


def _backup(name: str, payload: dict) -> pathlib.Path:
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    path = BACKUP_DIR / name
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def cmd_dns(cf: Cloudflare, apply: bool) -> None:
    zid = zone_id(cf)
    recs = dns_records(cf, zid)
    if not recs:
        say(f"no DNS record for {HOST}; nothing to delete")
        return
    for r in recs:
        if r["type"] != "CNAME" or not r["content"].endswith(".cfargotunnel.com"):
            raise SystemExit(f"error: unexpected record {r['type']} -> {r['content']}; not touching it")
    for r in recs:
        say(f"record: {r['type']} {r['name']} -> {r['content']} proxied={r.get('proxied')} id={r['id']}")
        if plan(apply, f"back up and delete DNS record {r['id']} (a Worker custom domain cannot bind while it exists)"):
            path = _backup(f"dns-{r['id']}.json", r)
            say(f"backup: {path}")
            cf.ok("DELETE", f"/zones/{zid}/dns_records/{r['id']}")
            say("deleted")


def cmd_tunnel(cf: Cloudflare, apply: bool) -> None:
    t = tunnel(cf)
    if not t:
        raise SystemExit(f"error: tunnel {TUNNEL_NAME} not found")
    config = tunnel_config(cf, t["id"])
    ingress = config.get("ingress") or []
    keep = [r for r in ingress if r.get("hostname") != HOST]
    removed = [r for r in ingress if r.get("hostname") == HOST]
    if not removed:
        say("no ingress rule for this host; nothing to do")
        return
    if len(removed) != 1 or not keep or keep[-1].get("hostname"):
        raise SystemExit("error: unexpected ingress shape (want exactly one rule removed and a catch-all last); not touching it")
    say(f"tunnel {TUNNEL_NAME} ({t['id']}): {len(ingress)} rules -> {len(keep)}")
    say(f"remove: {removed[0].get('hostname')} -> {removed[0].get('service')}")
    if plan(apply, "PUT the tunnel configuration without that rule (whole-config write on a live tunnel)"):
        path = _backup(f"tunnel-{t['id']}-config.json", config)
        say(f"backup: {path}")
        cf.ok("PUT", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{t['id']}/configurations", {"config": {**config, "ingress": keep}})
        say("updated")


# ---------------------------------------------------------------- post-deploy checks


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "agent-sync-phase0-check", **(headers or {})})
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()
    except urllib.error.URLError as err:
        return 0, {}, str(err.reason).encode()


def cmd_check(cf: Cloudflare | None, _apply: bool) -> None:
    failures = 0

    def expect(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        say(f"{'PASS' if cond else 'FAIL'}  {name}{('  (' + detail + ')') if detail else ''}")
        failures += 0 if cond else 1

    status, _, body = _http("GET", f"{ISSUER}/.well-known/oauth-authorization-server")
    asm = json.loads(body) if status == 200 else {}
    expect("authorization server metadata is 200", status == 200, str(status))
    expect("issuer is the hostname, no trailing slash", asm.get("issuer") == ISSUER, str(asm.get("issuer")))
    expect("authorization_endpoint", asm.get("authorization_endpoint") == f"{ISSUER}/authorize")
    expect("token_endpoint", asm.get("token_endpoint") == f"{ISSUER}/oauth/token")
    expect("PKCE is S256 only", asm.get("code_challenge_methods_supported") == ["S256"])
    expect("iss on authorization responses", asm.get("authorization_response_iss_parameter_supported") is True)
    expect("CIMD advertised", asm.get("client_id_metadata_document_supported") is True)
    expect("DCR is off (no registration_endpoint)", "registration_endpoint" not in asm or asm.get("registration_endpoint") is None)

    status, _, body = _http("GET", f"{ISSUER}/.well-known/oauth-protected-resource/mcp")
    prm = json.loads(body) if status == 200 else {}
    expect("protected resource metadata is 200", status == 200, str(status))
    expect("resource is /mcp", prm.get("resource") == RESOURCE)
    servers = prm.get("authorization_servers") or []
    expect("authorization_servers[0] equals issuer byte for byte", bool(servers) and servers[0] == asm.get("issuer"), f"{servers[:1]} vs {asm.get('issuer')}")

    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "phase0-check", "version": "0"}}}).encode()
    status, headers, _ = _http("POST", RESOURCE, init, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    challenge = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), "")
    expect("unauthenticated initialize is 401", status == 401, str(status))
    expect("401 names the resource metadata", f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in challenge, challenge[:160])
    status, _, _ = _http("GET", RESOURCE)
    expect("GET /mcp without a token is 401", status == 401, str(status))

    for path in ("/authorize", "/admin"):
        status, headers, _ = _http("GET", f"{ISSUER}{path}")
        location = next((v for k, v in headers.items() if k.lower() == "location"), "")
        expect(f"{path} redirects to Access", status in (302, 303) and TEAM_DOMAIN in location, f"{status} {location[:80]}")

    for path in ("/", "/oauth/register", "/post"):
        status, _, _ = _http("GET", f"{ISSUER}{path}")
        expect(f"{path} is 404", status == 404, str(status))

    if cf is not None:
        sub = (cf.call("GET", f"/accounts/{ACCOUNT_ID}/workers/subdomain").get("result") or {}).get("subdomain")
        if sub:
            status, _, body = _http("GET", f"https://{WORKER_NAME}.{sub}.workers.dev/.well-known/oauth-authorization-server")
            expect("workers.dev does not serve this Worker", status != 200 or b'"issuer"' not in body, str(status))
        domains = custom_domains(cf)
        expect("custom domain bound to the Worker", any(d.get("service") == WORKER_NAME for d in domains), json.dumps([d.get("service") for d in domains]))

    say(f"{failures} failure(s)")
    if failures:
        raise SystemExit(1)


COMMANDS = {
    "status": cmd_status,
    "access": cmd_access,
    "kv": cmd_kv,
    "write-config": cmd_write_config,
    "dns": cmd_dns,
    "tunnel": cmd_tunnel,
    "check": cmd_check,
}


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--apply", action="store_true", help="make the change (default is a dry run)")
    ap.add_argument("--no-key", action="store_true", help="check only:  skip the steps that need the Cloudflare key")
    args = ap.parse_args(argv)
    cf = None if (args.command == "check" and args.no_key) else Cloudflare()
    COMMANDS[args.command](cf, args.apply)


if __name__ == "__main__":
    main(sys.argv[1:])
