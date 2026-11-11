# 2026-09-18 — mac-collab-litestream inventory row

## Context & Objective

A Socratic.Trade health-check (Fri, Sep 18, 2026) found `pm2 mac-collab-litestream`
online and already in `mac-process-watch.sh` `expect_pm2`, but the owner-facing
master list skipped it.  The table jumped from `mac-collab-writeback` to
`vision-worker`.  Tracked `scripts/pm2-ecosystem.config.cjs` on `origin/main`
still listed retired scout/senate jobs and omitted live `seat-mcp` +
`mac-collab-litestream`.

This is documentation + tracked-ecosystem sync.  No process restart.  No
Coolify.  No extra-ship of ST product code.

Board `adc3f0d3`.  Branch `grok/litestream-inventory`.  Worktree
`~/apps/fleet-grok-litestream-inv`.

## Changes Made

- Live `~/apps/MAC-LOCAL-PROCESSES.md` and tracked `docs/MAC-LOCAL-PROCESSES.md`:
  always-on row for `pm2 mac-collab-litestream` (B2 replica of THE BOARD
  `findings.db`), helper rows for `start_litestream.sh` + `litestream.yml`,
  `seat-mcp` corrected from "not pm2" to the live ecosystem job, pm2 count
  14 → 15.
- Tracked `scripts/pm2-ecosystem.config.cjs` copied from the live
  `~/apps/pm2-ecosystem.config.cjs` (retired scout/senate/residential already
  gone live; adds `seat-mcp` and `mac-collab-litestream`; Datadog agent env
  already on the live file).
- Apple Note `⭐️ Background Jobs Master List` refreshed in the same change.

## Verification

```bash
pm2 jlist | python3 -c 'import json,sys
for p in json.load(sys.stdin):
  n=p.get("name","")
  if "collab" in n or n=="seat-mcp":
    print(n, p.get("pm2_env",{}).get("status"))'
grep -n 'mac-collab-litestream' ~/apps/MAC-LOCAL-PROCESSES.md
```

Verified 2026-09-18: `mac-collab`, `mac-collab-sync`, `mac-collab-writeback`,
and `mac-collab-litestream` all `online`.  Job was already replicating; this
lane only closed the inventory leak.

## Follow-ups (not this PR)

- ST `docs/EFFORT-LOG.md` git mirror lags the live board (writeback does not
  git-commit `~/Code` trees).  Land that in an ST PR, do not teach writeback
  to push git.
- Issue #3385 (post-#3383 sqliteYieldRetry remainder) stays with
  `grok/rth-event-loop-stall` / Fixer durable lane; CLAUDE owns related
  stall P1s `4ca246e5` / `081c8ecf`.
- ST R2 retain=4 vs 10 GiB free cap (`afb7fe08`) and restart-loop alerting
  (`a9676caf`) still need an owner call.
