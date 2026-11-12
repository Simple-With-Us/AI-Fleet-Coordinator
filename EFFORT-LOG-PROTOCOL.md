# Effort-Log Protocol (retired)

**Retired at the Linear cutover (owner 2026-10-10).**  The effort logs are archived, and Linear is the system of record for fleet work.  Nothing in this file binds any agent any more.  It stays as a stub so old links and old skill text resolve to the right answer.

Current rules live in `FLEET-PROTOCOL.md` § Linear and the Claim Lifecycle:

- Work is a Linear issue, one public team per app, ids like `BF-123`.  Agents use the `simple-board` CLI (same verbs and flags as before, Linear-backed).
- Claim with `simple-board claim`, close out with `simple-board status <id> completed|deployed`, and post in the work's Zulip topic `<ID> <subject>`.
- Branch `<seat>/<id>-<slug>`.  The PR body says `Fixes <ID>` when it finishes the issue and `Refs <ID>` for partial work.
- Writes need `AGENT_SEAT`; reads do not.

## What was retired

- The live effort logs, `/Users/jay/apps/<APP>-EFFORT-LOG.md`, and each repo's `docs/EFFORT-LOG.md`.  Each carries an "Archived" header that names the app's Linear team.  Do not edit them, and do not add a row for new work.
- The two-file system, the repo mirror row, and the rule to land the mirror in the app PR.
- THE BOARD server (`mac-collab`) as a write surface, and its pm2 jobs `mac-collab-sync` and `mac-collab-writeback`.  The server itself was retired on 2026-10-19 (AFC-363) and its tracked source removed from this repo;  what is left is a frozen, read-only archive (path below).
- The GitHub issues mirror:  `scripts/sync-effort-issues.py` and `.github/workflows/effort-issues-sync.yml` are disabled in every repo that had them (they may be deleted from a repo at its next convenient PR).
- The `scripts/claude-hooks/board-check.sh` push hook in Socratic.Trade.  Once `~/.fleet/effort-log-retired` exists, the user-level hook skips it everywhere.
- The triple claim and triple closeout.  Claims and closeouts are the Linear issue, the linked PR, and a Zulip post.

## The archived header

Every archived log starts with this block, right under its title:

```
> **ARCHIVED <date>.**  Linear is the system of record: team <KEY>, https://linear.app/simple-with-us/team/<KEY>.
> This file is read-only history.  Do not edit it or add rows.  See EFFORT-LOG-PROTOCOL.md.
```

Every item in the logs was imported into Linear (items that neither a board row nor a GitHub issue already carried became issues of their own, marked `imported`), so nothing is lost by not reading them.

## Reading history

- Linear:  `simple-board list --app <app> --search "<words>"` or the app's team in the browser.
- The old board's database is archived at `~/apps/_retired/mac-collab-20261019/findings.db` (read-only and frozen; the litestream replica was retired with the server), and the old board id of an imported row is still a valid `simple-board show` id.
- Git history of `docs/EFFORT-LOG.md` and the archived files under `~/apps/` are the original text.

## Registry

`fleet-apps.json` still records each app's archived log file name as `liveBoard`.  It is a record only:  nothing creates, parses or checks those files, and `scripts/check-fleet-registry.py` no longer reads this document.

## Bootstrapping a new app

A new app gets a Linear team (Jay creates it, key = the app acronym) instead of an effort log.  Run `scripts/onboard-new-app.sh` (it creates no effort log and no mirror), add the standard coordination stanza from `TEMPLATE-AGENTS.md` to the app's `AGENTS.md`, and run `python3 scripts/check-fleet-registry.py` from an AI-Fleet-Coordinator worktree.  Full procedure:  `docs/ONBOARDING-NEW-APP.md`.

## Apple Notes close-out (all agents, all apps)

The Apple Notes rules are in `FLEET-PROTOCOL.md` § Apple Notes and the `apple-notes` skill.  The title shape is `[TEAM_KEY, Agent] Topic`, with a second row `Sun, Aug 9, 3:52pm · PR #18`, and the helper is `~/apps/apple-notes-coding.sh`.
