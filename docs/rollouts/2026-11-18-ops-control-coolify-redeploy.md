# The ops-control Coolify reader, bound and verified on the server (2026-11-18)

**Why.**  `scripts/fleet-ops-control.py` read a Coolify token from an Infisical secret and folder that
do not exist: the Fleet-OPS prod project has no `/coolify` folder and no `COOLIFY_API_TOKEN`, so
`server redeploy` and `--check-token` failed closed with "the Coolify token is not available" before
any Coolify request;  the helper now names the variable it needs in that failure.  The server's audit log carries those failures for the Eavesdropper redeploys on
2026-11-18.

**What the deploy-only Coolify token is.**  Fleet-OPS prod holds three Coolify tokens at the project
root, and the deploy-only one has a folder of its own, `/coolify-deploy-only-token`, with exactly one
secret in it, `COOLIFY_DEPLOY_ONLY`.  It is the same token value as the root `COOLIFY_DEPLOY`.  It can
trigger a deploy and nothing else: a read of `/api/v1/version` answers 403 with "Missing required
permissions: read", while the deploy route answers for it.

**The grant.**  A read-only Infisical service token named `fleet-ops-control-coolify-deploy-ro`, on the
Fleet-OPS project, environment `prod`, scoped to `/coolify-deploy-only-token` alone, expiring
2027-02-16.  Its identifier is recorded in the Fleet Reference sheet, tab `Infisical Service
Tokens`, where it is registered by hand.  A named read through the helper's own reader returns
`COOLIFY_DEPLOY_ONLY` from that folder, and a read outside the folder answers 403, so the grant is
enforced at the folder.

**Where it is installed.**  On the server, at `/home/jay/.config/fleet-tools/ops-control.env`, mode 0600,
owned by the consumer's account, holding one line: `FLEET_OPS_CONTROL_INFISICAL_TOKEN=<value>`.  The
value is never on a command line, in an argv, in the answer or in the audit.  The path follows the
running account's home, so the forced command, which runs as `jay`, reads that file, while a root
shell reads `/root/.config/fleet-tools/ops-control.env`, finds nothing, and fails with "the Coolify
token is not available" even though the ssh path works.

**What was verified, and how.**

- `sudo -u jay env -i HOME=/home/jay PATH=/usr/bin:/bin /usr/bin/python3 -I
  /home/jay/.local/lib/fleet-resource-control/fleet-ops-control.py --check-token` on the server answered
  `"result": "done"` with the detail "the Coolify token is available and accepted".  The check reads the
  token the way a redeploy does and posts the deploy route the nil uuid, which names no resource, so
  nothing is deployed and Coolify's own not-found is what proves the token is accepted.
- The same script with `server redeploy l40rxd4rbj1pnogmbtetzmsf --plan` answered
  `"result": "planned"` with the command `[["coolify", "POST", "/api/v1/deploy?uuid=l40rxd4rbj1pnogmbtetzmsf"]]`.
  A plan sends nothing.
- The installed copy on the server is the helper this change ships, installed as `jay` by
  `scripts/hetzner/install-resource-control.sh` from the server's own checkout, and the installed file is
  byte for byte the branch's `scripts/fleet-ops-control.py`.    The command above runs as `jay` with `HOME=/home/jay`, because `env -i` clears the environment but
  leaves the effective UID alone, and the helper refuses a credential file that another account owns;  a
  root shell proves nothing about the ssh path, where the forced command runs as `jay`.

**What was not done.**  No redeploy was triggered: the Eavesdropper redeploy is the owner's call, and
the plan above is what it would run.

**The Coolify API's own version is 4.7.5**, not the 4.3.1 that `~/apps/COOLIFY.md` still records, and
the deploy route answers POST only since Coolify 4.x.  The fleet deploy guards have POSTed since
August 2026; the control helper had not.

**The code change is** [Simple-With-Us/AI-Fleet-Coordinator#2339](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/2339),
and the board item is OPS-1097.
