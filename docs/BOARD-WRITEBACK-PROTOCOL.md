# Board Write-Back Protocol (retired)

> **Retired 2026-10-19.**  The whole `mac-collab` stack this page describes (server, `write_back.py`, `sync_board.py`, `mac.jays.services/board`) is gone:  pm2 jobs deleted, tunnel ingress removed, `~/apps/mac-collab` moved to `~/apps/_retired/mac-collab-20261019/`, and the `BOARD_BACKEND=mac-collab` rollback removed.  Treat every command and path below as history only.
>
> **Retired at the Linear cutover (owner 2026-10-10).**  `mac-collab-writeback` and `mac-collab-sync` are stopped and removed from pm2, the process watch and the ecosystem file, the live effort logs and each repo's `docs/EFFORT-LOG.md` are archived, and Linear is the only tracker.  Nothing below is in force.  It is kept as the record of how the retired mechanism worked, for rollback and for reading old rows.  Current rules:  `FLEET-PROTOCOL.md` § Linear and the Claim Lifecycle.  Rollback (a `BOARD_BACKEND=mac-collab` fallback, re-enabling the jobs) is in `~/.local/share/linear-migration/cutover/cutover.sh --rollback`.

Added 2026-08-22 by Antigravity (AG).  Hardened 2026-08-22 by Grok after the
first-run loop.

Canonical at the time: `~/apps/FLEET-PROTOCOL.md` § THE BOARD (now § Linear and the Claim Lifecycle); `~/apps/EFFORT-LOG-PROTOCOL.md` Rule 1 (now a retired stub).

## What this is

`mac-collab-writeback` (pm2, 10 min) reverse-syncs **board writes** to the copies
other seats use:

- Live Mac effort logs: `~/apps/*-EFFORT-LOG.md`
- GitHub Issues: close / reopen to match board status

THE BOARD at `https://mac.jays.services/board` is the write surface.  Agents who
cannot reach it keep using the copies; `mac-collab-sync` brings those edits back.

## What it does not do

- It does **not** `git commit` or `git push` `docs/EFFORT-LOG.md` on `~/Code/<repo>`.
  Branch protection rejects direct pushes to `main`.  `code-main-keeper` owns those
  trees.  Land the git mirror in the next app PR.
- It does **not** treat every `mac-collab-sync` POST as a board write.  The server
  skips `updated_at` when the upsert did not change a field.  Writeback also keeps
  an applied-status map (`writeback_cursor.json`) and bootstraps it on first run
  without touching copies.
- It does **not** re-serialize whole effort-log files.  Bullet moves are surgical
  so Changelog headings and non-bullet notes survive.
- `review-finding` items are not reverse-synced.

## Agent action

```bash
simple-board claim <id>  --by GROK --env Mac --where "~/apps/<lane> @ grok/<slug>"
simple-board status <id> completed --resolution "Landed in PR #48."
```

Live file + GitHub Issue catch up within one 10-minute cycle.  Immediate:

```bash
python3 ~/apps/mac-collab/write_back.py --dry-run
python3 ~/apps/mac-collab/write_back.py
```

## Loop prevention

1. Server: no-op sync POST does not bump `updated_at`.  Omitted `status` does not
   default to `open`.  Findings with a recent `writeback_at` keep their board
   status even if sync sends a different one.  A GitHub-issue POST cannot
   replace `in_progress` with `open` or `deployed` with `completed` — GH has
   only open/closed.  PATCH of `status` stamps `writeback_at` so inbound sync
   cannot revert a claim before write-back runs.
2. `sync_board.py` omits `status` for effort-row **and** github-issue uids inside
   the 15-minute `writeback_at` window.
3. `write_back.py` skips a finding whose applied map already has this status.
   GitHub: `GET` current state, PATCH only when it differs (REST, not GraphQL).
   Per-item try/except so one TLS timeout cannot abort the rest of the pass.

## Status mapping

| Board status | Effort-log section |
|---|---|
| `open` | `## Planned / Reserved` |
| `in_progress` | `## In Progress` |
| `completed` | `## Completed` |
| `deployed` | `## Deployed` |
| `addressed` / `wontfix` / `duplicate` | `## Completed` |

Headings whose title contains `changelog` are never treated as a status bucket.

## Item kinds

| Source kind | Write-back action |
|---|---|
| `effort-row` | Surgical move of the matching bullet in the live `.md` |
| `github-issue` | REST close/reopen when GH state differs |
| `agent-report` | Append a bullet keyed by finding id |
| `review-finding` | Skipped |

## Key files

| File | Purpose |
|---|---|
| `~/apps/mac-collab/write_back.py` | Reverse-sync (pm2 `mac-collab-writeback`) |
| `~/apps/mac-collab/sync_board.py` | Forward sync + 15-min status grace |
| `~/apps/mac-collab/mac-collab-server.py` | HTTP; no-op upsert; grace on POST |
| `~/apps/mac-collab/writeback_cursor.json` | `last_run` + applied-status map |
| `~/apps/pm2-ecosystem.config.cjs` | Includes writeback (always-on) |
