# Production deploys

> Moved from `FLEET-PROTOCOL.md` on 2026-09-01 (Plan B slice 2, doc diet).  Still binding for every agent on every platform.  Canonical pointer stays in FLEET-PROTOCOL.md; this file is the full text and is ingested into the fleet-agents corpus (`recall`).

> **⚠️ SUPERSEDED for <YOUR_PROJECT_NAME> (owner-directed 2026-07-10): AUTO-DEPLOY IS ON.** Merging to
> `main` now auto-deploys `socratic-trade-prod` via Coolify's GitHub-App webhook
> (`is_auto_deploy_enabled=true` + GitHub's webhook IP ranges whitelisted on the `jays.services` CF
> zone, which had been 403'ing them). **Merge == live** — do NOT announce or manually trigger
> <YOUR_PROJECT_NAME> deploys. Rollback: set `is_auto_deploy_enabled=false` on the app. Details:
> <YOUR_PROJECT_NAME> `docs/rollouts/2026-07-10-auto-deploy-on.md`. The ANNOUNCE-THEN-DEPLOY protocol below
> now applies ONLY to apps still on manual deploy (per the per-app list further down).

#### ANNOUNCE-THEN-DEPLOY (apps NOT on auto-deploy — owner ruling 2026-07-09)

**Owner ruled 2026-07-09** (in-session to MONET, after the fleet flagged the 07-06 directive
conflicting with the post-Coolify repo docs): agents MAY deploy merged, green `main` without a
per-release owner ask, **but only announce-then-deploy**:

1. **Announce first** on `#Work-In-Progress`: a claim line naming the app, the exact `main` commit,
   what it ships, and "deploying in N minutes unless objection". The claim line makes ONE agent
   the deployer — this is what prevents the 2026-07-09 double-trigger (two lanes deployed the
   same commit 2s apart; Coolify's cancel API is broken, so both built).
2. **Wait a short no-objection window** (~10 minutes) before triggering.
3. **Avoid market hours** unless the change is a fix — deploys restart the scheduler
   mid-session otherwise.
4. Then deploy, **health-verify, and update the Linear issues** (the deployer owns the close-out).

**Back the announce with a claim (added 2026-09-16, reworked for Linear at the cutover).** The
Zulip announce gives peers visibility, but it is not itself a lock — a poll-cadence gap is
exactly what let two lanes deploy the same commit 2 seconds apart on 2026-07-09 (Coolify's
cancel API is broken, so both builds ran). The old board server rejected a second claim of an
`in_progress` item with a `409` inside a 20-minute lease. That lock is gone with the server.
Linear's `simple-board claim` is check-then-set:  it exits `3` when another seat holds the issue In
Progress, but two claims in the same second can both pass the check, and the later write
replaces the earlier `claimed:` label. So the claim is a strong signal, not an atomic lock.
Use it together with the announce and the no-objection window, and re-read the holder before
you deploy:

```bash
simple-board file --title "DEPLOY LOCK: <app>" --app <app> --severity P2 --kind agent-report --by "$AGENT_SEAT" --env Mac
simple-board claim <id> --by "$AGENT_SEAT" --env Mac --where "deploying <commit>"   # exit 3 = someone already holds it, stop
sleep 10 && simple-board show <id>        # the holder must still be you;  if it names another seat, stand down
# ...announce, wait the window, deploy, health-verify...
simple-board status <id> completed --resolution "deployed <commit>"
```

Exit `3`, or a holder that is not you after the re-read, means another seat claimed that app's
deploy lock — stand down, do not also trigger, and go find them in `#Work-In-Progress` instead of
assuming your Zulip post won. Prefer reusing the app's open `DEPLOY LOCK` issue over filing a
new one each time (`simple-board list --app <app> --search "DEPLOY LOCK"`), so the history stays in
one place. An atomic lock (a GitHub environment or a Coolify-side lock) is an open item for the
owner.  This does not require GitHub Environments or a new `Kind` label.

This replaces the unconditional "deploy immediately on merge" reading of the 2026-07-06
directive; the older "never deploy without an explicit owner ask" lines in repo docs are equally
superseded. Batching several merged PRs into one announced release is preferred over
deploy-per-merge (the 4 GB box serializes builds).

This does **NOT** relax the merge gate: work still lands via the normal **PR → required checks
green → review threads resolved** flow (branch protection with `enforce_admins`, unchanged). The
change is only that the `main → production` release step no longer needs a human "go".

**Responsible-deploy contract** (so "always deploy" is not "deploy blind"):
- Deploy only a **merged, green `main`** — never a red or mid-flight branch.
- Use each app's **sanctioned deploy path** (live 2026-08-07+ Hetzner NBG1 Coolify; dashboard https://host.jays.services). Do **not** host or redeploy on Render. Oracle UUID `m1os7ijf31bg3fanil152e4b` is retired.
  - Socratic.Trade → Coolify UUID `d83b1aykr03uwr32yhgzaiay`. Auto-deploy from `main` is ON — merge == live; do NOT also click Deploy. Browser-like User-Agent on the Coolify API (Cloudflare 1010-blocks default tool UAs).
  - Congress.Trade → Coolify dockercompose UUID `c11c5hdhuczureb6w2pg20p0` (auto-deploy on `app/**` / `services/**`). The old Cloudflare Worker `deploy.yml` is leftover — not the production path.
  - Usage-Monitor → Coolify UUID `yagelvqux9e8l1kztif7bf2o` (GitHub webhook on `main` → usage.jays.services). `render.yaml` is rollback-only and must stay unused.
  - congress-trading-shared → cut a tagged release (it is a consumed library; "prod" = the published tag).
- **Verify health after.** <YOUR_OTHER_PROJECT_NAME> `/api/health` returns HTTP 403 to non-browser UAs
  (Cloudflare managed challenge) — the deploy workflow's own health step therefore reports a
  **FALSE failure** even though the Worker deployed fine; verify with a browser UA, not the
  workflow's red X.
- On a **real** deploy failure, roll back (or restart the prior good version) and raise it in
  `#Work-In-Progress` — do not leave production broken.
- Never run destructive one-offs (prod DB wipes, unbounded backfills/queue drains) under cover of
  "deploy" — this directive is about releasing merged code, not arbitrary prod mutations.

