# 2026-11-08: Deploying with the scoped per-Worker token (versions upload + versions deploy)

Record of the AFC #1524 recall search-timeout deploy and the route the scoped per-Worker deploy token requires.

## What was deployed

- Artifact commit `130dfa82c244833e6614c5ffd22920fb45f63d71` (branch `echo/recall-45s-on-deployed-baseline`), parent `99d118ac2ac64de83e08ae30dafd31d23b4edb17` (the version then serving), tree `e7062dab6698e1ae3192591bd9a03e797d801d2e`. Single-file change: `scripts/agent-sync-mcp/src/recall.js` blob `096d14b0642ea77c25aa7832fb2a99499b9ed964` (+9/-4): `RECALL_SEARCH_TIMEOUT_MS` becomes 45_000; base `RECALL_TIMEOUT_MS` stays 10_000 (writes/stats path).
- Independently reviewed; baseline suite 1225 tests, 0 failures; new timeout regression passes.

## Credential route used

- `CLOUDFLARE_ECHO_ST_MCP_DEPLOY` read from Infisical Fleet-OPS `prod` `/cloudflare` with the scoped reader `st-mcp-deploy-read-scoped` (read-only; registered on the Fleet Reference sheet, "Infisical Service Tokens" tab, row 114).
- The reader was loaded as `INFISICAL_TOKEN` from a mode-600 file on the deploy host; the deploy token was read into `CLOUDFLARE_API_TOKEN` in-process. No secret was printed, logged, or passed in argv.

## What failed (verbatim)

Command (from `scripts/agent-sync-mcp/` at the artifact checkout, wrangler 4.154.0):

    npx wrangler deploy

Result: the script uploaded ("Uploaded simple-tools-mcp (3.23 sec)") but promotion did not happen; the trigger step failed:

    ✘ [ERROR] Trigger configuration for "simple-tools-mcp" was only partially updated:

      Custom domains:
        - A request to the Cloudflare API (/accounts/3a9368057468d0909cafaa85df12d1b7/workers/scripts/simple-tools-mcp/domains/records) failed.
          - The zone "agents.simplewithus.com" does not exist on your account. [code: 10084]

      Successful trigger changes were not rolled back.

    No targets deployed for simple-tools-mcp (0.86 sec)

Cause: the scoped per-Worker token is bound to account `3a9368057468d0909cafaa85df12d1b7` and cannot see the `simplewithus.com` zone, so the custom-domain attach call fails. No production impact: the pre-existing custom domain attachment was untouched (`GET /health` stayed 200) and no new version was promoted by the failed run.

## What worked

    npx wrangler versions upload --message "echo: #1524 recall 45s/10s on deployed baseline 99d118ac (artifact 130dfa82, reviewed)"
    # -> Worker Version ID: 7c3fbbec-9c1c-4a80-86d9-8f00d4cb272f
    npx wrangler versions deploy 7c3fbbec-9c1c-4a80-86d9-8f00d4cb272f@100 --yes
    # -> SUCCESS  Deployed simple-tools-mcp version 7c3fbbec-9c1c-4a80-86d9-8f00d4cb272f at 100% (0.75 sec)

`versions deploy` is a traffic-only promotion; it does not touch triggers or custom domains, which is why it completes with the scoped token while `wrangler deploy` cannot.

## Verification after promotion

- `npx wrangler deployments list`, latest entry: Created 2026-11-08T08:20:54.975Z, Version(s): (100%) 7c3fbbec-9c1c-4a80-86d9-8f00d4cb272f (Created 2026-11-08T08:20:51.968Z), Message: echo: #1524 recall 45s/10s on deployed baseline 99d118ac (artifact 130dfa82, reviewed).
- `GET https://agents.simplewithus.com/health` -> 200 (custom domain serving the new version).
- `POST https://agents.simplewithus.com/mcp` without auth -> 401 (auth boundary intact).

## Rule going forward

With the scoped per-Worker deploy token, deploy = `wrangler versions upload` + `wrangler versions deploy <id>@100`. Do not retry plain `wrangler deploy` expecting a different result: it will always fail at the custom-domain step with this token. Do not widen the token or fall back to the retired Global-key route without the owner.
