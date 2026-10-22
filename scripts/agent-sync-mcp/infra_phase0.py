#!/usr/bin/env python3
"""Cloudflare setup and checks for the agent-sync MCP Worker, Phase 0.

Every step is a dry run unless --apply is given.  The Cloudflare Global key is
read inside this process from the environment or `fleet-secrets get` and never printed, logged or
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
import re
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ACCOUNT_ID = "3a9368057468d0909cafaa85df12d1b7"  # Usage.Jays.Services (decision D1)
# Hard cutover (owner ruling Oct 22, 2026):  the served host moved from
# agent-sync.jays.services to agents.simplewithus.com, which lives in the
# simplewithus.com zone (same Cloudflare account as jays.services).  There is
# no fallback host and no alias:  this script provisions the one true host.
ZONE_NAME = "simplewithus.com"
HOST = "agents.simplewithus.com"
# The retired host, kept as its own constants for one reason only:  cleanup and
# verification.  The hard cutover stopped SERVING it - the Worker answers 404
# for that Host and no alias exists - but a public hostname aimed at the Mac's
# local dev port with no Access in front of it is a standing risk, not a
# preference.  Dropping its tunnel ingress rule and its *.cfargotunnel.com CNAME
# is how the retired name stops resolving to the Mac at all.  Nothing here may
# serve, alias or redirect the retired host.
RETIRED_ZONE_NAME = "jays.services"
RETIRED_HOST = "agent-sync.jays.services"
# Cleanup and post-deploy checks cover both names;  provisioning covers only HOST.
RETIRED_HOSTS = (HOST, RETIRED_HOST)
ISSUER = f"https://{HOST}"
RESOURCE = f"{ISSUER}/mcp"
TEAM_DOMAIN = "silent-frost-37e0.cloudflareaccess.com"
TUNNEL_NAME = "Jay's Tunnel"
MIRROR_APP_UUID = "1e1a5fc4-0f7d-44ac-bb0d-5fa6d5d73ddf"  # agents.jays.services
ACCESS_APP_NAME = "agents.simplewithus.com consent and admin"
ACCESS_PATHS = [f"{HOST}/authorize", f"{HOST}/admin"]
# Spec 3.3:  mail@jays.services only.  The identity provider is One-time PIN, so every
# address on this list is an admin credential for the server;  keep it to one.
OWNER_EMAILS = ["mail@jays.services"]
KV_TITLE = "agent-sync-mcp-OAUTH_KV"
WORKER_NAME = "agent-sync-mcp"
HERE = pathlib.Path(__file__).resolve().parent
WRANGLER = HERE / "wrangler.jsonc"
BACKUP_DIR = pathlib.Path.home() / ".agent-sync-mcp-phase0"

API = "https://api.cloudflare.com/client/v4"


# ---------------------------------------------------------------- credentials


def _load(name: str) -> str:
    """One credential:  the environment first, then `fleet-secrets get` (OPS-818).  The flat
    handoff file is retired (AFC-359).  Only the name is ever reported."""
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
        f"error: {name} is not in the environment or fleet-secrets (name only; no value printed)"
    )


class Cloudflare:
    def __init__(self) -> None:
        self._email = _load("CLOUDFLARE_JAY_ACCOUNT_EMAIL")
        self._key = _load("CLOUDFLARE_JAY_API_KEY")

    def wrangler_env(self) -> dict:
        """The environment a wrangler child needs to authenticate.

        Wrangler reads CLOUDFLARE_EMAIL / CLOUDFLARE_API_KEY / CLOUDFLARE_ACCOUNT_ID,
        while this script loads the first two under the fleet's own names and keeps
        them inside this object.  Nothing here is printed or logged; the mapping is
        the same one install_seat_key.py already uses.
        """
        env = dict(os.environ)
        env.update(
            {
                "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
                "CLOUDFLARE_EMAIL": self._email,
                "CLOUDFLARE_API_KEY": self._key,
                "WRANGLER_SEND_METRICS": "false",
            }
        )
        return env

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


def zone_id(cf: Cloudflare, zone: str = ZONE_NAME) -> str:
    res = cf.ok("GET", "/zones", name=zone)
    for z in res.get("result") or []:
        if z["account"]["id"] == ACCOUNT_ID:
            return z["id"]
    raise SystemExit(f"error: zone {zone} not on account {ACCOUNT_ID}")


def dns_records(cf: Cloudflare, zid: str, host: str = HOST) -> list[dict]:
    return cf.ok("GET", f"/zones/{zid}/dns_records", name=host).get("result") or []


def tunnel_ingress_from_config(config: dict, tid: str) -> list[dict]:
    """The tunnel's ingress rules from one already-fetched configuration.

    Shape-checked:  Cloudflare's response is a third party, and every caller
    here reads `rule["hostname"]` and `rule["service"]`.  A malformed answer
    should be a precise refusal rather than an incidental AttributeError that
    looks like a bug in this script - especially in `check`, where a crash
    would be read as "could not verify" rather than "the answer was not
    understood".
    """
    if not isinstance(config, dict):
        raise SystemExit(f"error: tunnel {tid} configuration is not an object")
    ingress = config.get("ingress")
    if ingress is None:
        return []
    if not isinstance(ingress, list):
        raise SystemExit(f"error: tunnel {tid} ingress is not a list")
    for rule in ingress:
        if not isinstance(rule, dict):
            raise SystemExit(f"error: tunnel {tid} ingress entry is not an object: {rule!r}")
        hostname = rule.get("hostname")
        service = rule.get("service")
        if hostname is not None and not isinstance(hostname, str):
            raise SystemExit(f"error: tunnel {tid} ingress hostname is not a string: {hostname!r}")
        if not isinstance(service, str):
            raise SystemExit(f"error: tunnel {tid} ingress service is not a string: {service!r}")
    return ingress


def tunnel_ingress(cf: Cloudflare, tid: str) -> list[dict]:
    """The tunnel's ingress rules, shape-checked, in one fetch."""
    return tunnel_ingress_from_config(tunnel_config(cf, tid), tid)


def tunnel_hosts(cf: Cloudflare, hosts=RETIRED_HOSTS) -> dict[str, list[str]]:
    """Map each hostname to the tunnel services an ingress rule points it at."""
    t = tunnel(cf)
    if not t:
        return {}
    found = {}
    for rule in tunnel_ingress(cf, t["id"]):
        hostname = rule.get("hostname")
        if hostname in hosts:
            found.setdefault(hostname, []).append(rule.get("service"))
    return found


def tunnel(cf: Cloudflare) -> dict | None:
    for t in (
        cf.ok("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel", is_deleted="false").get("result") or []
    ):
        if t.get("name") == TUNNEL_NAME:
            return t
    return None


def tunnel_config(cf: Cloudflare, tid: str) -> dict:
    return (
        cf.ok("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tid}/configurations").get("result") or {}
    ).get("config") or {}


def access_app(cf: Cloudflare) -> dict | None:
    for app in cf.ok("GET", f"/accounts/{ACCOUNT_ID}/access/apps").get("result") or []:
        uris = [d.get("uri") for d in app.get("destinations") or [] if isinstance(d, dict)] + [
            app.get("domain")
        ]
        if app.get("name") == ACCESS_APP_NAME or any(u and u.startswith(HOST) for u in uris):
            return app
    return None


def kv_namespace(cf: Cloudflare) -> dict | None:
    for ns in (
        cf.ok("GET", f"/accounts/{ACCOUNT_ID}/storage/kv/namespaces", per_page=100).get("result")
        or []
    ):
        if ns.get("title") == KV_TITLE:
            return ns
    return None


def custom_domains(cf: Cloudflare) -> list[dict]:
    return (
        cf.ok("GET", f"/accounts/{ACCOUNT_ID}/workers/domains", hostname=HOST).get("result") or []
    )


# ---------------------------------------------------------------- steps


def local_listener(port: str) -> str:
    """Process name, pid and working directory of whatever listens on 127.0.0.1:<port>."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "lsof unavailable"
    pids = re.findall(r"^p(\d+)$", out, re.MULTILINE)
    names = re.findall(r"^c(.+)$", out, re.MULTILINE)
    if not pids:
        return "nothing listening"
    try:
        cwd_out = subprocess.run(
            ["lsof", "-a", "-p", pids[0], "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        cwd = next(
            (line[1:] for line in cwd_out.splitlines() if line.startswith("n")), "unknown cwd"
        )
    except (OSError, subprocess.SubprocessError):
        cwd = "unknown cwd"
    return f"{names[0] if names else 'process'} pid {pids[0]} in {cwd}"


def cmd_status(cf: Cloudflare, _apply: bool) -> None:
    zid = zone_id(cf)
    say(f"zone {ZONE_NAME}: {zid}")
    for r in dns_records(cf, zid):
        say(
            f"dns: {r['type']} {r['name']} -> {r['content']} proxied={r.get('proxied')} id={r['id']}"
        )
    t = tunnel(cf)
    if t:
        rules = [r for r in tunnel_ingress(cf, t["id"]) if r.get("hostname") == HOST]
        say(
            f"tunnel {TUNNEL_NAME} ({t['id']}, {t.get('status')}): {len(rules)} ingress rule(s) for {HOST}"
        )
        for r in rules:
            say(f"  ingress: {r.get('hostname')} -> {r.get('service')}")
            port = re.search(r"localhost:(\d+)$", str(r.get("service") or ""))
            if port:
                say(
                    f"  local listener on port {port.group(1)} (read-only lsof):  {local_listener(port.group(1))}"
                )
    for d in custom_domains(cf):
        say(
            f"worker custom domain: {d.get('hostname')} -> {d.get('service')} ({d.get('environment')})"
        )
    app = access_app(cf)
    say(
        f"access app: {app['name']} aud={app.get('aud')} session={app.get('session_duration')}"
        if app
        else "access app: none"
    )
    ns = kv_namespace(cf)
    say(f"kv: {ns['title']} id={ns['id']}" if ns else "kv: none")


def cmd_access(cf: Cloudflare, apply: bool) -> None:
    existing = access_app(cf)
    if existing:
        say(f"exists: {existing['name']} ({existing['id']}) aud={existing.get('aud')}")
        say(f"destinations: {json.dumps(existing.get('destinations'))}")
        return
    mirror = (
        cf.ok("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{MIRROR_APP_UUID}").get("result") or {}
    )
    body = {
        "name": ACCESS_APP_NAME,
        "type": "self_hosted",
        "domain": ACCESS_PATHS[0],
        "destinations": [{"type": "public", "uri": uri} for uri in ACCESS_PATHS],
        # Spec 3.3 said 15 minutes.  The owner ruled "30 days for all" on Fri, Oct 9, 2026, so the
        # consent and admin paths now use a 30-day session (720h), matching the Access organization.
        "session_duration": "720h",
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
    pols = (
        cf.call("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{res.get('id')}/policies").get("result")
        or []
    )
    if not any(p.get("decision") == "allow" for p in pols):
        say(
            "WARNING: the app has no allow policy.  Nobody can pass it, which fails closed.  Add 'Jay emails only' in the dashboard."
        )


def cmd_kv(cf: Cloudflare, apply: bool) -> None:
    ns = kv_namespace(cf)
    if ns:
        say(f"exists: {ns['title']} id={ns['id']}")
        return
    if plan(apply, f"create KV namespace {KV_TITLE}"):
        res = (
            cf.ok("POST", f"/accounts/{ACCOUNT_ID}/storage/kv/namespaces", {"title": KV_TITLE}).get(
                "result"
            )
            or {}
        )
        say(f"created {KV_TITLE} id={res.get('id')}")


# Wrangler prints its own failure text, and third-party output can carry a
# Cloudflare response body, request data or authorization material, so none of
# it is relayed:  only these two scalar fields are kept, and only in the shapes
# wrangler documents.  Replacing known secrets is a denylist and cannot cover
# what a response body happens to contain;  an allow-list can.
_WRANGLER_CODE = re.compile(r"\bcode:\s*(\d{1,12})\b")
# Cloudflare request ids are fixed 32-hex-character strings;  matching exactly
# that shape keeps a longer or shorter lookalike (an API key, a token fragment)
# from being captured as a request id.
_WRANGLER_REQUEST_ID = re.compile(r"\brequest id:\s*([0-9a-f]{32})\b", re.IGNORECASE)


def _wrangler_diagnostic(stdout: str, stderr: str, secret: str, api_key: str = "") -> str:
    """The allow-listed facts from wrangler's output, or nothing."""
    output = f"{stderr}\n{stdout}"
    found = []
    code = _WRANGLER_CODE.search(output)
    if code:
        found.append(f"cf code {code.group(1)}")
    request_id = _WRANGLER_REQUEST_ID.search(output)
    if request_id:
        found.append(f"request id {request_id.group(1)}")
    # A captured value would already be one of the two documented fields, but
    # the check costs nothing and keeps the rule true by construction:  if
    # wrangler ever labels the secret or the API key as a request id, the whole
    # detail is withheld rather than relayed.
    detail = ", ".join(found)
    for sensitive in (secret, api_key):
        if sensitive and sensitive in detail:
            return ""
    return detail


def _terminate_group(pid: int, sig: int) -> None:
    """Signal the child's whole process group.

    The child is started in its own session, so a signal aimed at this process
    never reaches it and a signal aimed at the group does:  `wrangler` is an
    npx shim that spawns node underneath, so signalling the group ends the real
    work rather than an orphan still holding the secret it was fed.  The caller
    reaps the child;  a group that is already gone is not an error.
    """
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def put_worker_secret(cf: Cloudflare, name: str, value: str) -> None:
    """Set an encrypted Worker secret.

    The value is fed on stdin, never argv.  The child gets the credentials
    wrangler actually reads plus cwd=HERE, because a bare inherited environment
    runs `wrangler secret put` unauthenticated and fails with no usable reason.
    A failing run reports only its exit code and the allow-listed fields of
    wrangler's output:  a secret put that dies on a missing Worker or a wrong
    account is otherwise just "exit 1", which is not enough to act on.
    """
    if shutil.which("wrangler"):
        argv = ["wrangler"]
    elif shutil.which("npx"):
        # --no-install keeps the pinned devDependency (4.139.0, DEPLOY-PHASE0.md
        # step 1) instead of whatever a global install happens to be.
        argv = ["npx", "--no-install", "wrangler"]
    else:
        # 127, the shell's "command not found", so automation can tell a missing
        # executable from a failed run.  A string argument would exit 1.
        sys.stderr.write(f"error: wrangler is not on PATH; cannot provision {name}\n")
        raise SystemExit(127)
    try:
        # Popen, not run:  `run` cannot end the child when this process is
        # interrupted, and an interrupted provisioning run must not leave
        # wrangler writing a secret with no owner.  start_new_session puts it in
        # its own process group, so the group is what has to be signalled.
        proc = subprocess.Popen(
            argv + ["secret", "put", name, "--config", str(WRANGLER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=HERE,
            env=cf.wrangler_env(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"error: {argv[0]} is not on PATH; cannot provision {name}\n")
        raise SystemExit(127) from exc
    except OSError as exc:
        raise SystemExit(f"error: could not run wrangler to set {name}: {exc}") from exc

    def _on_sigterm(signum, frame):
        # A SIGTERM to this runner must take the child with it.  Converting the
        # signal into SystemExit runs the same cleanup path as any other
        # interruption, then exits 128 + SIGTERM the way a killed process does.
        _terminate_group(proc.pid, signal.SIGTERM)
        raise SystemExit(143)

    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # Not the main thread:  signal handlers cannot be installed here, so
        # SIGTERM keeps its default disposition.  KeyboardInterrupt and any
        # SystemExit raised by the caller still clean the group up below.
        previous = None
    try:
        try:
            stdout, stderr = proc.communicate(value + "\n", timeout=300)
        except subprocess.TimeoutExpired as exc:
            _terminate_group(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise SystemExit(124) from exc
        except BaseException:
            # KeyboardInterrupt, a SystemExit from _on_sigterm, or anything else
            # unwinding this thread:  the child never outlives the run.
            _terminate_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_group(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    if proc.returncode != 0:
        api_key = cf.wrangler_env().get("CLOUDFLARE_API_KEY", "") or ""
        detail = _wrangler_diagnostic(stdout or "", stderr or "", value, api_key)
        raise SystemExit(
            f"error: wrangler secret put {name} failed (exit {proc.returncode})"
            + (f": {detail}" if detail else "")
        )


def cmd_write_config(cf: Cloudflare, apply: bool) -> None:
    text = WRANGLER.read_text(encoding="utf-8")
    ns = kv_namespace(cf)
    app = access_app(cf)
    if not ns or not app:
        raise SystemExit("error: run 'kv --apply' and 'access --apply' first")
    # The KV namespace id is not a credential, so it stays in the committed file.
    # Replace whatever is there - the original placeholder or the id written by
    # an earlier run - so a re-created namespace is picked up instead of being
    # silently ignored as "already configured".
    new, count = re.subn(
        r'("binding"\s*:\s*"OAUTH_KV"\s*,\s*"id"\s*:\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"{ns["id"]}"',
        text,
    )
    if count == 0:
        raise SystemExit("error: no OAUTH_KV binding id found in wrangler.jsonc; not touching it")
    if count > 1:
        raise SystemExit("error: OAUTH_KV binding is ambiguous in wrangler.jsonc; not touching it")
    if new != text:
        say(f"OAUTH_KV id -> {ns['id']}")
        if plan(apply, "write wrangler.jsonc"):
            WRANGLER.write_text(new, encoding="utf-8")
    else:
        say("wrangler.jsonc already has the live OAUTH_KV id")

    # The Access aud is a private Cloudflare resource identifier, so it is not
    # committed.  Provision it as an encrypted Worker secret, fed on stdin so
    # the value never appears in argv or in this script's output.  The Worker's
    # /authorize and /admin fail closed naming ACCESS_AUD until it is set.
    say(f"ACCESS_AUD -> provisioned as a Worker secret from the {ACCESS_APP_NAME!r} app")
    if plan(apply, "wrangler secret put ACCESS_AUD (deploys a new Worker version)"):
        put_worker_secret(cf, "ACCESS_AUD", app["aud"])
        say("ACCESS_AUD secret is set; redeploy is not required for it to take effect")


def _backup(name: str, payload: dict) -> pathlib.Path:
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    path = BACKUP_DIR / name
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def cmd_dns(cf: Cloudflare, apply: bool) -> None:
    for zone, host in ((ZONE_NAME, HOST), (RETIRED_ZONE_NAME, RETIRED_HOST)):
        zid = zone_id(cf, zone)
        recs = dns_records(cf, zid, host)
        if not recs:
            say(f"no DNS record for {host} in zone {zone}; nothing to delete")
            continue
        for r in recs:
            if r["type"] != "CNAME" or not r["content"].endswith(".cfargotunnel.com"):
                raise SystemExit(
                    f"error: unexpected record {r['type']} -> {r['content']}; not touching it"
                )
        for r in recs:
            say(
                f"record: {r['type']} {r['name']} -> {r['content']} proxied={r.get('proxied')} id={r['id']}"
            )
            if plan(
                apply,
                f"back up and delete DNS record {r['id']} (a Worker custom domain cannot bind while it exists)",
            ):
                path = _backup(f"dns-{r['id']}.json", r)
                say(f"backup: {path}")
                cf.ok("DELETE", f"/zones/{zid}/dns_records/{r['id']}")
                say("deleted")
                say(
                    "The backup is a record of what existed, NOT a rollback:  recreating this CNAME would publish"
                )
                say(
                    "the Mac's local port again with no Access.  To undo a bad deploy, roll back the Worker version."
                )
                say(
                    "Next:  deploy (step 5), then 'check', then 'tunnel --apply' (step 6) to drop the dead ingress rules."
                )


def cmd_tunnel(cf: Cloudflare, apply: bool) -> None:
    t = tunnel(cf)
    if not t:
        raise SystemExit(f"error: tunnel {TUNNEL_NAME} not found")
    # One read:  the PUT below writes {**config, "ingress": keep}, so the
    # validated rules must come from the same object the backup keeps, not a
    # second fetch that a concurrent change could slip between.
    config = tunnel_config(cf, t["id"])
    ingress = tunnel_ingress_from_config(config, t["id"])
    # Drop a rule for the served host OR the retired one:  either keeps a public
    # hostname aimed at the Mac alive.
    keep = [r for r in ingress if r.get("hostname") not in RETIRED_HOSTS]
    removed = [r for r in ingress if r.get("hostname") in RETIRED_HOSTS]
    if not removed:
        say(f"no ingress rule for {HOST} or {RETIRED_HOST}; nothing to do")
        return
    if not keep or keep[-1].get("hostname"):
        raise SystemExit(
            "error: unexpected ingress shape (want only hostname rules removed and a catch-all last); not touching it"
        )
    say(f"tunnel {TUNNEL_NAME} ({t['id']}): {len(ingress)} rules -> {len(keep)}")
    for rule in removed:
        say(f"remove: {rule.get('hostname')} -> {rule.get('service')}")
    say(
        "Do this right after the custom domain is verified:  a dead rule that points at a local dev port"
    )
    say("comes back to life the moment anyone re-adds a CNAME for that host.")
    if plan(
        apply,
        "PUT the tunnel configuration without those rules (whole-config write on a live tunnel)",
    ):
        path = _backup(f"tunnel-{t['id']}-config.json", config)
        say(f"backup: {path}")
        cf.ok(
            "PUT",
            f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{t['id']}/configurations",
            {"config": {**config, "ingress": keep}},
        )
        say("updated")


# ---------------------------------------------------------------- post-deploy checks


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"User-Agent": "agent-sync-phase0-check", **(headers or {})},
    )
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
    expect(
        "issuer is the hostname, no trailing slash",
        asm.get("issuer") == ISSUER,
        str(asm.get("issuer")),
    )
    expect("authorization_endpoint", asm.get("authorization_endpoint") == f"{ISSUER}/authorize")
    expect("token_endpoint", asm.get("token_endpoint") == f"{ISSUER}/oauth/token")
    expect("PKCE is S256 only", asm.get("code_challenge_methods_supported") == ["S256"])
    expect(
        "iss on authorization responses",
        asm.get("authorization_response_iss_parameter_supported") is True,
    )
    expect("CIMD advertised", asm.get("client_id_metadata_document_supported") is True)
    expect(
        "DCR is off (no registration_endpoint)",
        "registration_endpoint" not in asm or asm.get("registration_endpoint") is None,
    )

    status, _, body = _http("GET", f"{ISSUER}/.well-known/oauth-protected-resource/mcp")
    prm = json.loads(body) if status == 200 else {}
    expect("protected resource metadata is 200", status == 200, str(status))
    expect("resource is /mcp", prm.get("resource") == RESOURCE)
    servers = prm.get("authorization_servers") or []
    expect(
        "authorization_servers[0] equals issuer byte for byte",
        bool(servers) and servers[0] == asm.get("issuer"),
        f"{servers[:1]} vs {asm.get('issuer')}",
    )

    init = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "phase0-check", "version": "0"},
            },
        }
    ).encode()
    status, headers, _ = _http(
        "POST",
        RESOURCE,
        init,
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    challenge = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), "")
    expect("unauthenticated initialize is 401", status == 401, str(status))
    expect(
        "401 names the resource metadata",
        f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in challenge,
        challenge[:160],
    )
    status, _, _ = _http("GET", RESOURCE)
    expect("GET /mcp without a token is 401", status == 401, str(status))

    for path in ("/authorize", "/admin"):
        status, headers, _ = _http("GET", f"{ISSUER}{path}")
        location = next((v for k, v in headers.items() if k.lower() == "location"), "")
        expect(
            f"{path} redirects to Access",
            status in (302, 303) and TEAM_DOMAIN in location,
            f"{status} {location[:80]}",
        )

    # Access covers subpaths of /admin too:  a POST to /admin/action must stop at
    # Access, never reach the Worker (whose own 403 says "Sign-In Required").
    status, headers, body = _http(
        "POST",
        f"{ISSUER}/admin/action",
        b"action=arm&seat=JET",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    location = next((v for k, v in headers.items() if k.lower() == "location"), "")
    expect(
        "/admin/action is stopped by Access",
        (status in (302, 303) and TEAM_DOMAIN in location)
        or (status in (401, 403) and b"Sign-In Required" not in body),
        f"{status} {location[:80]}",
    )

    status, _, body = _http("GET", f"{ISSUER}/health")
    expect(
        "/health is the static 200 the fleet admin panel probes",
        status == 200 and body.strip() == b'{"ok":true}',
        str(status),
    )

    for path in ("/", "/oauth/register", "/post", "/internal/wake/NOPE"):
        status, _, _ = _http("GET", f"{ISSUER}{path}")
        expect(f"{path} is 404", status == 404, str(status))

    # The listener's wake (spec 3.12) is outside Access:  an unsigned POST is the
    # Worker's own 401 once WAKE_HMAC_KEY_JET is set (404 before), never Access.
    status, headers, _ = _http(
        "POST", f"{ISSUER}/internal/wake/JET", b"{}", {"Content-Type": "application/json"}
    )
    location = next((v for k, v in headers.items() if k.lower() == "location"), "")
    expect(
        "/internal/wake/JET refuses an unsigned POST itself (401), outside Access",
        status == 401 and TEAM_DOMAIN not in location,
        str(status),
    )

    if cf is not None:
        sub = (cf.call("GET", f"/accounts/{ACCOUNT_ID}/workers/subdomain").get("result") or {}).get(
            "subdomain"
        )
        if sub:
            status, _, body = _http(
                "GET",
                f"https://{WORKER_NAME}.{sub}.workers.dev/.well-known/oauth-authorization-server",
            )
            expect(
                "workers.dev does not serve this Worker",
                status != 200 or b'"issuer"' not in body,
                str(status),
            )
        domains = custom_domains(cf)
        expect(
            "custom domain bound to the Worker",
            any(d.get("service") == WORKER_NAME for d in domains),
            json.dumps([d.get("service") for d in domains]),
        )
        t = tunnel(cf)
        stale = tunnel_hosts(cf) if t else {}
        for host in RETIRED_HOSTS:
            expect(
                f"no stale tunnel ingress rule for {host} (run 'tunnel --apply')",
                host not in stale,
                json.dumps(stale.get(host, [])),
            )
        for zone, host in ((ZONE_NAME, HOST), (RETIRED_ZONE_NAME, RETIRED_HOST)):
            expect(
                f"no leftover tunnel DNS record for {host} in zone {zone}",
                not [
                    r
                    for r in dns_records(cf, zone_id(cf, zone), host)
                    if r["type"] == "CNAME" and r["content"].endswith(".cfargotunnel.com")
                ],
            )

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
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--apply", action="store_true", help="make the change (default is a dry run)")
    ap.add_argument(
        "--no-key",
        action="store_true",
        help="check only:  skip the steps that need the Cloudflare key",
    )
    args = ap.parse_args(argv)
    cf = None if (args.command == "check" and args.no_key) else Cloudflare()
    COMMANDS[args.command](cf, args.apply)


if __name__ == "__main__":
    main(sys.argv[1:])
