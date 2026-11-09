# 2026-08-22 — KIMI unclaim + claim dates

Owner: KIMI must have nothing In Progress or reserved for future work.
Agents must say the date they claimed a task so forgotten lanes are obvious.

## Board

- Closed leftover KIMI In Progress and COMPLETED-but-still-open effort-row mirrors.
- Left real PLANNED findings open and unclaimed, with a comment that KIMI is retired.
- Reopened `bd8d05b0` (P0 history-scrub verify) after a false-positive close — the title
  contains the word "completed" but the work is not done.
- Zero KIMI items remain `in_progress`.

## Protocol

- Slack claims include `claimed: Sat, Aug 22, 2026`.
- Board `--where` starts with that date, then worktree `@` branch.
- Effort-log row date is the claim date.  Refresh it on re-claim.
- KIMI retired notice strengthened: no assign, no In Progress, no Planned reserved
  to KIMI.

## DealDex (same session)

#134 squash-merged.  Production `https://dealdex.online` SSR has SCAN + Hide Proxies
and no REPACKS/Radar.
