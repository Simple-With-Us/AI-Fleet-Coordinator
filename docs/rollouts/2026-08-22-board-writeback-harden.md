# 2026-08-22 — Harden THE BOARD two-way sync

## Summary

AG shipped `mac-collab-writeback` so agents can write THE BOARD and have Mac
effort logs plus GitHub Issues follow.  The first run treated every
`mac-collab-sync` POST as a board write (`updated_at` always bumped), then
`gh issue close/reopen` on ~1700 issues per 10-minute cycle, aborted on TLS
timeouts (cursor did not advance), re-serialized live effort logs (Changelog
headings dropped into Planned), and committed `docs/EFFORT-LOG.md` on
`~/Code/<repo>` (ST onto `ag/sanitize-public-infra`; other repos rejected by
branch protection).

Grok stopped the job, reset those local write-back commits, restored Changelog
headings, and hardened the loop.

## What changed

- `write_back.py`: applied-status map + first-run bootstrap (no copies written);
  surgical bullet moves; REST `gh api` with current-state check; per-item
  try/except; no git on `~/Code`.
- `mac-collab-server.py`: no-op upsert does not bump `updated_at`; omitted
  `status` does not default to `open`; 15-min `writeback_at` grace on POST.
- `sync_board.py`: grace window also omits `status` on github-issue payloads.
- Ecosystem + `mac-process-watch` expected list include `mac-collab-writeback`
  (15 pm2 apps).
- Docs/skills: board is the write surface; copies are live Mac files + Issues.

## Verification

```bash
python3 /Users/jay/apps/mac-collab/test_write_back.py
python3 /Users/jay/apps/mac-collab/write_back.py --dry-run
python3 /Users/jay/apps/mac-collab/write_back.py
pm2 restart mac-collab
pm2 start /Users/jay/apps/pm2-ecosystem.config.cjs --only mac-collab-writeback
```

Do not `pm2 save` while `grok-leader` is stopped for a live TUI.

## Follow-ups

- `docs/EFFORT-LOG.md` git mirrors still land with the next app PR.
- Live ST/UM In Progress vs origin/main drift is mostly Cursor consolidating
  rows on the live file, not writeback drops.  Not restored from origin.
