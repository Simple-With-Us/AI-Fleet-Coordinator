# 2026-09-13 — Skip pointless Vercel production deploys

Board `0934111e`.  Branch `fx/vercel-skip-pointless`.

Owner: do not let agents burn Vercel Hobby with merges that have no site change, multiple times an hour.

Policy: production git deploys only when site files changed, at most once per hour.  Previews skipped.  Coolify ST/CT/UM unchanged.

App PRs: Personal-Site, DealDex, BotFleet, ContactLogo on `fx/vercel-skip-pointless`.
