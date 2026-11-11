# 2026-08-22 — Mac disk prune, Kimi salvage, janitor coverage

## Why

Owner: free more disk, prune real-old worktrees from any app, heavily prune Kimi (retired), extract 1–2 week half-done Kimi work, and fix Mac + Hetzner process health.  Disk janitor only walked ST/CT/CTS, so UM/DealDex/Autorotate/FLEET trees piled up.  The Mac sat at 90% / 42G free with swap ~34G used.

## What landed

- Live `~/.claude-disk-janitor/janitor.sh` now walks all fleet Code repos, protects unsuffixed standing lanes + runtimes, reaps clean kimi-named / nested `.claude/worktrees` / `/tmp` scratch, and uses `STALE_DAYS=2` when free < 50G.
- Tracked copy: `scripts/disk-janitor.sh`.  After merge, copy it to the live path if they drift.
- One-shot prune this morning: 201 → ~157 worktrees (39 merged/idle + tmp/nested + 6 leftover CT nested trees whose only dirt was generated vendor bins).  Kimi named checkouts are gone; branches with unique work remain.
- Salvage: `~/apps/KIMI-SALVAGE-2026-08-22/` (ST #3044 patch + rollout; Autorotate handoff).  Closed duplicate coordinator #87.  ST #3044 stays open (CONFLICTING).
- Process: SIGTERM'd 242 duplicate leaked `toolbox-sdk` / `data-agent-kit` MCP children of the Grok TUI (kept one of each argv).  Swap used 34G → ~27G.
- Hetzner `fleet-hetzner-nbg1`: 51% / 72G free, unused Docker already 0B, ST/UM health ok, CT health `degraded` (80 outbox DLQ — existing finding, not this PR).

## Verification

```bash
bash -n ~/.claude-disk-janitor/janitor.sh
df -h /System/Volumes/Data
git -C ~/Code/Socratic.Trade worktree list | wc -l
sysctl vm.swapusage
curl -fsS -m 8 https://socratictrade.com/api/health | head -c 80
curl -fsS -m 8 https://usage.jays.services/api/health | head -c 80
```

Ran: janitor syntax OK.  Data volume 83% / 69G free after this pass (from 90% / 42G).  Local `/health` 200 on :8791 :8792 :8787 :8899.  Host 51%.

## Follow-ups

- Land ST #3044 in a dedicated Socratic.Trade lane (Robinhood peer intraday + CI pin-check).  Do not force-merge from disk cleanup.
- CT 80 failed outbox items still degrade `/api/health`.  Board `a22263e1`.
- Swap remains high (~27G) while Antigravity + Xcode swift-frontend + this Grok TUI run.  Memory, not disk, is what takes the 16GB box down.
- AG also claimed a same-morning disk/Kimi row.  Overlapped; this seat did salvage + prune + janitor.  Leave AG's row for them to close.
