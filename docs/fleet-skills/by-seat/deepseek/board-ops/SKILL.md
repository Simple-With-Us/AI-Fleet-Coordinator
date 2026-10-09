---
name: board-ops
description: Use THE BOARD (mac.jays.services/board + the board CLI) as the first place to look and write. File, claim, comment, and resolve fleet items. Trigger whenever starting work, hunting open P0/P1s, reviewing a peer fix, closing a lane, or when the owner mentions the board, mac-collab, or findings.
---

# THE BOARD (DSH)

> **Retired seat.** DSH (DeepSeek Harness) is retired (2026-09-19): use `CLUTCH` for `Simple-With-Us/Clutch`.  Do not take work as DSH and do not install this pack to `~/.deepseek/skills`.  This catalog copy is inactive.


Primary coordination surface (owner 2026-08-19).  One searchable board over review findings, every app's effort-board rows, and every repo's GitHub issues, synced about every 10 minutes.

Humans: `https://mac.jays.services/board` (HTTP Basic Auth, any username, password = `$MAC_COLLAB_TOKEN`).  Short link `https://board.jays.services` 302s there.  Agents: the `board` CLI.

## Invoke it literally

```bash
board stats
board list --status open,in_progress --severity P0,P1
board list --app congress-trade --mine DSH
board show <id>
```

`~/apps/mac-collab/board`, also `~/.local/bin/board`.  It reads `MAC_COLLAB_TOKEN` from `~/.secrets/mac-collab.env`.  The token must never appear on a command line, in `ps`, or in a transcript.

Some agent CLIs only allowlist a stable command prefix.  `board stats` allowlists.  `B=…/board; $B stats`, `$(…)`, pipes, and `&&` chains do not.

`--env` is only `Mac` or `cloud`.  `--by` for this seat is `DSH`.

## File / claim / talk / finish

```bash
board file --title "Scout drops Senate rows on 502" --app congress-trade \
  --severity P1 --by DSH --env Mac --where "~/apps/lanes/congress/<seat>-fix @ <branch>" \
  --desc "path:line + repro"

board claim <id> --by DSH --env Mac --where "~/apps/lanes/congress/<seat>-fix @ <branch>"

board comment <id> --by DSH --text "Verified on main; the shared helper is right."

board status <id> completed --resolution "Landed in #2894."
```

`--app` values you will actually use: `socratic-trade` / `Socratic.Trade`, `congress-trade`, `usage-monitor`, `congress-trading-shared`, `dealdex`, `personal-site`, `fleet-infra`.  If unsure, `board stats` prints the live app list.

Status values: `open`, `in_progress`, `completed`, `deployed`, `addressed`, `wontfix`, `duplicate`.

## What you owe the board

1. **Before substantial work:** list the app.  Claim the existing item or file then claim.
2. **While working:** keep `--by DSH`, `--env Mac`, and `--where "lane path @ branch"` accurate.
3. **When done:** `completed` or `deployed` with a resolution that names the PR and what changed.  Do not leave `in_progress` after you stopped.
4. **On a peer's item:** comment with evidence.  Reviewing fixes here is expected.

## Item kinds

- `agent-report` (default) and `review-finding` — your status persists.
- `effort-row` and `github-issue` — mirrored both ways.  `mac-collab-writeback` (pm2, 10 min) moves the live effort-log bullet and closes/reopens the GitHub issue to match a status you set on the board.  A 15-minute grace window stops `mac-collab-sync` from overwriting that status.  Still land `docs/EFFORT-LOG.md` in the app PR when you are in that repo; writeback does not push git.

The board is the **write surface**.  Live `~/apps/*-EFFORT-LOG.md` and GitHub Issues are copies for seats that cannot reach the board.

## Do not

- Paste `MAC_COLLAB_TOKEN` into curl "to be safe."  Use `board`.
- Use THE BOARD as the only closeout.  Zulip + effort board + issues still move.
- File a duplicate because you did not `board list --search` first.

## Canon

- `/Users/jay/apps/AGENT-SYNC.md` § THE BOARD
- `/Users/jay/apps/EFFORT-LOG-PROTOCOL.md`
- `board --help` / `board file --help`
