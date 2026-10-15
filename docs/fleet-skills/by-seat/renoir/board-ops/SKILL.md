---
name: board-ops
description: Use Linear through the `board` CLI as the first place to look and write.  File, claim, comment, and resolve fleet items (issue ids like BF-123).  Trigger whenever starting work, hunting open P0/P1s, reviewing a peer fix, closing a lane, or when the owner mentions the board, Linear, findings, or an issue id.
---

# Board CLI on Linear (RENOIR)

> **Retired seat.**  Owner directive 2026-10-07: the seat never opened, and the Renoir Claude account and app are no longer used.  `CLAUDE` is the only Claude seat.  Do not install to `~/.renoir/skills`.  Do not take fleet work as RENOIR.  This catalog copy is inactive.


Linear is the system of record for fleet work (owner 2026-10-10), and `board` is how agents use it.  Workspace Simple With Us, `https://linear.app/simple-with-us`, one public team per app:  ST, CT, UM, CTS, DD, AFC, PS, AR, CL, BF, HH, OPS, CK, FL, CC, SWU.  Every issue has an identifier such as `BF-123`.  The old board's rows, the GitHub issues and the effort-log items were all imported, so the history is there too.

Humans use Linear in the browser.  Agents use the `board` CLI, which is the same command with the same flags as before; it now reads and writes Linear and signs each write with your seat.  The old `mac.jays.services/board` is a read-only archive until about day 30 after the cutover.  The effort logs are archived.  Nothing mirrors to them any more.

## Invoke it literally

```bash
board stats
board list --status open,in_progress --severity P0,P1
board list --app congress-trade --mine "$AGENT_SEAT"
board show <id>
```

`<id>` is a Linear identifier (`BF-123`), a full old board id, or the first 8 or more hex characters of an old board id.  Every output line ends with the identifier, so use it from then on.

The CLI reads the Fleet app's credentials from `~/.secrets/Linear/Fleet.env` itself.  The secret must never appear on a command line, in `ps`, or in a transcript.  Do not use the claude.ai Linear connector or Composio to write to Linear: they act as Jay, not as a seat.

Claude Code only offers "Always Allow" when the command has a stable prefix.  `board stats` allowlists.  `B=…/board; $B stats`, `$(…)`, pipes, and `&&` chains do not.

**Writes need `AGENT_SEAT`.**  `file`, `claim`, `status` and `comment` exit 2 without it, and `--by` must equal it.  Reads (`list`, `show`, `stats`, `whoami`) need no seat.  Your shell may not keep exports between commands, so set it in the same command, from the seat you verified in `session-start`.  `--env` is only `Mac` or `cloud`.  `--by` for this seat is your verified seat (`RENOIR` in an ordinary Renoir session).

## File / claim / talk / finish

```bash
board file --title "Scout drops Senate rows on 502" --app congress-trade \
  --severity P1 --by "$AGENT_SEAT" --env Mac --where "~/apps/lanes/Congress.Trade/<seat>-fix @ <branch>" \
  --desc "path:line + repro"

board claim <id> --by "$AGENT_SEAT" --env Mac --where "~/apps/lanes/Congress.Trade/<seat>-fix @ <branch>"

board comment <id> --by "$AGENT_SEAT" --text "Verified on main; the shared helper is right."

board status <id> completed --resolution "Landed in #2894."
```

`--app` takes an app team key (`CT`) or the old slug (`congress-trade`, `socratic-trade`, `usage-monitor`, `congress-trading-shared`, `dealdex`, `personal-site`, `fleet-infra`, which files to OPS).  If unsure, `board stats` prints the live app list.

Status values: `open`, `in_progress`, `addressed` (In Review), `completed` (Done), `deployed`, `wontfix`, `duplicate`, plus `parked`.  Two rules differ from the old board:

- `status <id> duplicate` needs `--of <canonical id>` (or `dup of <id>` in `--resolution`), because Linear only makes a Duplicate through a link to the issue it duplicates.
- `claim` is check-then-set.  It exits 3 when another seat holds the issue In Progress, unless you pass `--steal --reason "..."`.  A claim is the `claimed:<SEAT>` label plus a `CLAIMED` comment; `status <id> open` releases it.  There is no 20-minute lease and no server lock, so a second claim in the same second can still collide.

The full verb table, id forms and exit codes are in `scripts/linear-board/README.md` in AI-Fleet-Coordinator.

## What you owe Linear

1. **Before substantial work:** list the app.  Claim the existing issue or file then claim.  Name the branch `<seat>/<id>-<slug>` (`renoir/afc-123-zulip-cutover`).
2. **While working:** keep `--by "$AGENT_SEAT"`, `--env Mac`, and `--where "lane path @ branch"` accurate.
3. **In the PR:** put `Fixes <ID>` in the body when the PR finishes the issue, and `Refs <ID>` for partial work.  A `Refs` PR never closes it, even with the id in the branch name.  Once Linear's GitHub automation is on (Jay enables it right after the cutover), a merge moves a `Fixes` issue to Done by itself;  `board status <id> completed` does the same by hand, works before then, and is always safe to run.
4. **When done:** `completed`, or `deployed` only after the production check, with a resolution that names the PR and what changed.  Do not leave `in_progress` after you stopped.
5. **On a peer's item:** comment with evidence.  Reviewing fixes here is expected.

## Item kinds

`agent-report`, `review-finding`, `effort-row`, `github-issue` and `sentry` are labels now (`Kind`), not separate systems.  Nothing is mirrored in either direction: there is no writeback, no `mac-collab-sync`, and no effort log to land.  Imported effort-log items are ordinary issues, marked `imported`.

## Do not

- Write to Linear any way except `board` (no connector, no personal API key, no raw GraphQL with a borrowed token).
- Use Linear as the only closeout.  The work topic in Zulip still gets its `DONE` post.
- File a duplicate because you did not `board list --search` first.
- Edit an archived `*-EFFORT-LOG.md` or `docs/EFFORT-LOG.md`.  They carry an Archived header and nobody reads them.

## Canon

- `/Users/jay/apps/AGENT-SYNC.md` § Linear and the Claim Lifecycle
- `scripts/linear-board/README.md` and `docs/protocols/linear-migration.md` in AI-Fleet-Coordinator
- `board --help` / `board file --help`
