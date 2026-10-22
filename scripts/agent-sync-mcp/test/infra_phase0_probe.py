#!/usr/bin/env python3
"""Exercise infra_phase0.py's tunnel/DNS cleanup against a fake Cloudflare API.

Driven by test/infra-phase0.test.mjs.  Stdlib only, no network:  the point is
to prove that a rule or CNAME belonging to the RETIRED host is still found and
removed after the hard cutover repointed HOST at agents.simplewithus.com.
Prints one JSON object; the node test asserts on it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("infra_phase0", HERE.parent / "infra_phase0.py")
infra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(infra)

TUNNEL_ID = "tunnel-1"
ZONES = {infra.ZONE_NAME: "zone-new", infra.RETIRED_ZONE_NAME: "zone-old"}
# Both hosts still carry a tunnel CNAME:  the retired one is the whole point.
RECORDS = {
    (infra.ZONE_NAME, infra.HOST): [
        {"id": "rec-new", "type": "CNAME", "name": infra.HOST, "content": "x.cfargotunnel.com"}
    ],
    (infra.RETIRED_ZONE_NAME, infra.RETIRED_HOST): [
        {
            "id": "rec-old",
            "type": "CNAME",
            "name": infra.RETIRED_HOST,
            "content": "y.cfargotunnel.com",
        }
    ],
}


class FakeCloudflare:
    def __init__(self):
        self.deleted = []
        self.puts = []
        self.queried = []

    def ok(self, method, path, body=None, **params):
        if path == "/zones":
            zone = params["name"]
            return {
                "success": True,
                "result": [{"id": ZONES[zone], "account": {"id": infra.ACCOUNT_ID}}],
            }
        if path == f"/accounts/{infra.ACCOUNT_ID}/cfd_tunnel":
            return {"success": True, "result": [{"id": TUNNEL_ID, "name": infra.TUNNEL_NAME}]}
        if path.endswith("/configurations"):
            if method == "PUT":
                self.puts.append(body["config"]["ingress"])
                return {"success": True, "result": {}}
            return {
                "success": True,
                "result": {
                    "config": {
                        "ingress": [
                            # The stale rule Kody flagged:  a public hostname
                            # aimed at the Mac's local dev port.
                            {"hostname": infra.RETIRED_HOST, "service": "http://localhost:8787"},
                            {"hostname": infra.HOST, "service": "http://localhost:8787"},
                            {"service": "http_status:404"},
                        ]
                    }
                },
            }
        if path.startswith("/zones/") and path.endswith("/dns_records"):
            zid = path.split("/")[2]
            zone = next(name for name, ident in ZONES.items() if ident == zid)
            host = params["name"]
            self.queried.append((zone, host))
            return {"success": True, "result": RECORDS.get((zone, host), [])}
        if path.startswith("/zones/") and "/dns_records/" in path:
            self.deleted.append(path.rsplit("/", 1)[1])
            return {"success": True, "result": {}}
        if path == f"/accounts/{infra.ACCOUNT_ID}/access/apps":
            return {
                "success": True,
                "result": [{"name": infra.ACCESS_APP_NAME, "aud": "a" * 64, "destinations": []}],
            }
        if path == f"/accounts/{infra.ACCOUNT_ID}/storage/kv/namespaces":
            return {"success": True, "result": [{"id": "kv-live", "title": infra.KV_TITLE}]}
        raise AssertionError(f"unexpected API call: {method} {path} {params}")


def main() -> None:
    cf = FakeCloudflare()
    # Keep the real backup directory out of the way.
    infra.BACKUP_DIR = pathlib.Path(tempfile.mkdtemp())

    out = io.StringIO()
    lines = []

    def fake_say(text=""):
        lines.append(text)

    def fake_plan(apply, what):
        lines.append(("APPLY  " if apply else "DRY RUN  ") + what)
        return apply

    infra.say = fake_say
    infra.plan = fake_plan

    infra.cmd_tunnel(cf, True)
    ingress_after = cf.puts[-1] if cf.puts else None

    infra.cmd_dns(cf, True)

    print(
        json.dumps(
            {
                "hosts_after_tunnel": [r.get("hostname") for r in (ingress_after or [])],
                "deleted_record_ids": cf.deleted,
                "queried": sorted(f"{zone}:{host}" for zone, host in cf.queried),
                "output": "\n".join(lines),
            }
        )
    )


if __name__ == "__main__":
    main()
