# 2026-09-13 — Merge to main is production for Vercel sites

Board `ef71d6c1`.  Branch `fx/merge-equals-live-docs`.

Owner: jays.services and the other Vercel sites must go live on merge, same as Coolify ST/CT/UM.

`vercel-ignore-hourly.sh` in Personal-Site, DealDex, BotFleet `apps/site`, and ContactLogo `web` now only skips preview auto-deploys.  Production always builds.  The 1/hour cap is gone.

Skills: `deploy-verify` and `land-lane` no longer say Personal-Site is not merge=live.

App PRs: `fx/merge-equals-live` on Personal-Site, DealDex, BotFleet, ContactLogo.
