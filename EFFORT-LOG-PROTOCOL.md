# Effort-Log Protocol (canonical, all apps, all agents)

Machine-level companion to `/Users/jay/apps/AGENT-SYNC.md`.  Every AI agent on every platform
(CLAUDE, MONET, CODEX, AG, CURSOR, GROK, GROK-BUILD, GROK-BOT, future tools) uses the SAME
effort-log system in EVERY app, current and future.

**Look first at THE BOARD** (`https://mac.jays.services/board`, short link `https://board.jays.services`).  Per-app effort boards
remain the durable Mac copies (`~/apps/*-EFFORT-LOG.md`) plus git mirrors and GitHub
Issues.  THE BOARD is the write surface; `mac-collab-writeback` copies board writes
to the live files and Issues.  `#agent-sync` is the realtime layer on top — never a
substitute for either surface.

`GROK-BOT` is a fleet-wide identity (Cursor cloud), not a per-app coding seat.
Do not add app-specific Grok Bot lanes.

**Related owner policy (session continuity):** prior owner messages stay in scope — new
messages add work unless the owner explicitly cancels or redirects. Full rule:
`AGENT-SYNC.md` “Prior messages stay in scope”.

## The two-file system (per app)

1. **Live board** — `/Users/jay/apps/<APP>-EFFORT-LOG.md`. Branch-neutral, machine-level:
   visible to every agent regardless of worktree/branch state. Update FIRST.
2. **Repo mirror** — `docs/EFFORT-LOG.md` inside the app's repo. Tracked in git so history,
   PRs, and remote/cloud sessions see it. Mirror the relevant state BEFORE every commit/push.
   Sessions without Mac filesystem access update the mirror and say so in #agent-sync; the
   next Mac-side agent reconciles the live board (note "mirrored by <TAG>, board pending").

Cloud / no-Mac agents can **read** the live boards at `https://mac.jays.services` (pm2 `mac-collab`, Jay's Tunnel). `GET /health` is public. `GET /files` and `GET /files/<name>` need `Authorization: Bearer $MAC_COLLAB_TOKEN` (Mac: `~/.secrets/mac-collab.env`; never print; never commit). Allowlist only — not a general Mac share. Writes still happen on the Mac or via the repo mirror + Slack, per the rest of this protocol.

### Board registry

| App | Live board | Repo mirror |
|-----|-----------|-------------|
| Socratic-Trade | `/Users/jay/apps/TRADING-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| congress-trading-shared | `/Users/jay/apps/CONGRESS-SHARED-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| API-usage-monitor | `/Users/jay/apps/API-USAGE-MONITOR-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| Congress.Trade | `/Users/jay/apps/CONGRESS-TRADE-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| DealDex | `/Users/jay/apps/DEALDEX-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| Personal-Site | `/Users/jay/apps/PERSONAL-SITE-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| Autorotate | `/Users/jay/apps/AUTOROTATE-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| ContactLogo | `/Users/jay/apps/CONTACTLOGO-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| BotFleet | `/Users/jay/apps/BOTFLEET-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| Clutch | `/Users/jay/apps/CLUTCH-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| HogHunter | `/Users/jay/apps/HOGHUNTER-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| fleet-ops | `/Users/jay/apps/FLEET-OPS-EFFORT-LOG.md` | `docs/EFFORT-LOG.md` |
| fleet-infra (machine-side) | `/Users/jay/apps/FLEET-INFRA-EFFORT-LOG.md` | (none — not a repo; no issues mirror) |

## States (universal)

- **Planned / Reserved** — agreed or reserved, not started. Add the row BEFORE substantial
  work so parallel agents see the reservation. Include blockers ("needs owner decision").
- **In Progress** — actively being built. Carry owner tag + **claim date** + branch/worktree + one-line status.  The date on the row is when the seat claimed it; refresh it if you re-claim.  Owner 2026-08-22: a claim without a date is unfinished, because nobody can tell if it was forgotten.
- **Completed** — merged to the app's main branch (integration/beta only, if the app has that
  distinction).
- **Deployed** — released to the app's production target and verified. Only move a row here
  when the deploy actually happened and was verified (say how).

## Rules (identical in every app)

1. **Claim at start of ANY work; complete at end (binding — 2026-08-05).**  Before substantial
   work: put/move the row to **In Progress** on the live board **and** repo mirror, with your
   tag + **claim date** + branch/worktree + one-line status, and ensure the matching **GitHub issue(s)** show
   claimed/in-progress.  Slack claims include `claimed: Sat, Aug 22, 2026`.  Board `--where`
   starts with that same date.  When finished: move to **Completed** (merged) or **Deployed** (prod
   verified) and close/complete the matching issue state.  Do not start silent; do not leave
   rows or issues open after you are done.  Also claim/closeout on `#agent-sync` (see
   AGENT-SYNC Message Structure).

   **Shortcut (2026-08-22 — bidirectional sync live):** `board claim <id>` / `board status <id>
   completed` are now sufficient.  `mac-collab-writeback` (pm2, 10-min cycle) propagates every
   *board write* to the matching live `~/apps/*-EFFORT-LOG.md` and closes/reopens the GitHub
   Issue.  It does **not** push `docs/EFFORT-LOG.md` to `main` (branch protection; do not
   commit in `~/Code/<repo>`).  Land the git mirror in the app PR as usual.  You no longer
   need to hand-edit the live effort-log file or `gh issue close` after updating the board.
   The board is the **write surface**.  (The old manual-mirror path still works as a
   fallback; `mac-collab-sync` picks it up on the next 10-min pass.)

2. Reserve BEFORE work when the effort is first identified (Planned); move to In Progress
   before substantial edits; update at every boundary: start, handoff, commit, PR, merge, deploy.
3. NEVER delete another agent's row. Correct in place and note the correction with your tag
   and date.
4. Every commit that changes work-state also updates the repo mirror; live board first when
   you can reach it. **Auto-commit finished units** (including docs/board mirror edits) per
   AGENT-SYNC "Always commit + land finished work" — do not leave board+code only local.
5. **Board and GitHub issues must match and stay accurate.** Prefer landing `docs/EFFORT-LOG.md`
   so `effort-issues-sync` reconciles labels/open-closed state. If you are executing a
   numbered issue, also claim/comment it at start and close or mark done at end when that
   does not fight the mirror. Never leave the board In Progress while the issue is closed
   (or the reverse) without correcting both.
6. When landing a **default-off / dormant** feature, also reserve a Planned enablement row
   (and update ST `docs/FEATURE-ENABLEMENT-BACKLOG.md` when the flag lives in Socratic.Trade)
   so shipped-but-off switches are not forgotten.
7. Cross-app efforts get a row on EACH affected app's board, cross-referencing the other.
8. A row is not a lock on files — keepouts/filesets are negotiated in #agent-sync; the board
   records the claim.
9. Owner directives supersede board state; a stale board is corrected, not obeyed.

## Issues mirror (standard)

Every app in the Board registry gets a **GitHub Issues mirror** of its `docs/EFFORT-LOG.md`.
This is the owner-visibility layer for effort state. **Boards remain the coordination source
of truth**; the workflow reconciles mirrored issues from the committed mirror. Agents **must
still keep issues accurate**: land board state promptly at claim and complete so the mirror
updates, and claim/close numbered issues you execute so nothing looks abandoned.

- **Why the committed mirror, not the live board:** the sync runs in GitHub Actions, which has
  no access to the operator's Mac filesystem. It reads each repo's `docs/EFFORT-LOG.md` at HEAD
  — i.e. state as of the last landing, not every live-board edit. That is the right cadence for
  owner notifications (issue-assignment pushes mobile alerts) and is called out explicitly in
  the sync script's own docstring so nobody mistakes it for real-time.
- **Two files, kept identical across every app:** `scripts/sync-effort-issues.py` (python3 stdlib
  only — no third-party deps, no GraphQL, just `urllib` against the plain REST API using the
  Actions-provided `GITHUB_TOKEN`) and `.github/workflows/effort-issues-sync.yml` (additive;
  triggers on push to `main` touching `docs/EFFORT-LOG.md`, a daily off-minute cron for drift,
  and `workflow_dispatch`). Copy both **verbatim** into a new app — the script reads its own
  repo context from the `GITHUB_REPOSITORY` env var Actions sets automatically, so no
  repo-specific edits are needed or wanted.
- **Parsing tolerates heading/format drift** across apps (e.g. "Planned / Reserved Before
  Implementation" vs "Planned / Reserved", with or without emoji) by keyword-classifying each
  `##` section rather than requiring an exact string match. Confirmed working against all three
  bootstrapped apps' real boards before rollout (Socratic.Trade's 58-item board,
  congress-trading-shared's 1-item board, API-usage-monitor's 2-item board).
- **Item identity** is a SHA1 hash of the item's normalized first line, embedded in the mirrored
  issue body as `<!-- effort-key: ... -->`. This makes the sync idempotent and lets a row's
  state transition (Planned → In Progress → Completed) update the same issue in place, as long
  as the row's first line isn't reworded. Reconciliation: Planned/In Progress → issue open
  (`effort-board` + `state:planned`/`state:in-progress`, assigned to the owner for mobile
  notifications); Completed/Deployed → issue closed (`state:completed`/`state:deployed`). Never
  deletes issues; never touches hand-made issues without the marker; creates any missing labels
  on first run. After all current rows reconcile, an open marker issue whose key vanished from a
  **non-empty** board is closed as `state:orphaned`; already-closed Completed/Deployed history is
  preserved, and a returning key is reopened/restored normally. Orphan retirement requires the
  board to retain at least half of all previously mirrored keys; lower-coverage parses are treated
  as truncation/parser drift and skip retirement rather than mass-closing the mirror.
- Current reference implementation: `congress-trading-shared` — its stdlib unit suite covers
  placeholder parsing, reversible orphan retirement, label preservation, deterministic ordering,
  the empty-board safeguard, and partial rate-limit handling. Propagate script changes verbatim to
  every registered repo through normal owned PRs; never overwrite another app's dirty worktree.

## Bootstrapping a new app (future apps — do this in your FIRST commit there)

Full procedure: `/Users/jay/Code/AI-Fleet-Coordinator/docs/ONBOARDING-NEW-APP.md` +
`scripts/onboard-new-app.sh`
(https://github.com/jaywedgeworth22/AI-Fleet-Coordinator/blob/main/docs/ONBOARDING-NEW-APP.md).
New seats: `/Users/jay/Code/AI-Fleet-Coordinator/docs/ONBOARDING-NEW-AGENT.md`
(https://github.com/jaywedgeworth22/AI-Fleet-Coordinator/blob/main/docs/ONBOARDING-NEW-AGENT.md).
Minimum first-commit set:

1. Create `/Users/jay/apps/<APP>-EFFORT-LOG.md` (if you have Mac filesystem access) from the
   template below, and `docs/EFFORT-LOG.md` in the repo with the same content.
2. Add the app to the Board registry table above **and** `fleet-apps.json`.
3. Add the standard coordination stanza to the app's `AGENTS.md` (see AGENT-SYNC.md's
   onboarding section) — it covers both the channel and this protocol.
4. Copy `scripts/sync-effort-issues.py` and `.github/workflows/effort-issues-sync.yml` from any
   already-bootstrapped app (verbatim, no edits) — see "Issues mirror (standard)" above.
5. Run `python3 scripts/check-fleet-registry.py` from an AI-Fleet-Coordinator worktree.

### Template

```
# <APP> Effort Log — cross-agent board
Protocol: /Users/jay/apps/EFFORT-LOG-PROTOCOL.md (canonical). Live board: this file
(mirror: docs/EFFORT-LOG.md in the repo). As of <date>.

## Deployed
- (none)

## Completed
- (none)

## In Progress
- (none)

## Planned / Reserved
- (none)

## Changelog of this log
- <date> — bootstrapped by <TAG>.
```

## Apple Notes close-out (all agents, all apps — 2026-08-09; shortcuts 2026-08-10)

**Title:** `[APP, Agent] short topic` — app acronym(s) + agent **first**.
Examples: `[UM, Grok] TestFlight first ship` · `[ST, CT, Monet] R2 peer digests`.
Acronyms: `UM` `ST` `CT` `CTS` `FLEET`. Multi-app: list each (`[ST, CT, Grok] …`).
Agent display Title Case (`Grok`/`Monet`/`Claude`/`Codex`/`AG`/…), not ALL-CAPS Slack tags.

**Second body row:** local stamp + optional PR numbers `Sun, Aug 9, 3:52pm · PR #18` (create **or** last update — refresh on every change; pass `--pr "18"` to helper). Helper auto-injects/refreshes it.

**Always** write/update living Completion notes for substantial work; update in place.
Folder **Coding**, pin when able (via macOS System Settings App Shortcut `⌘⌥P` for `Pin Note` / `Unpin Note` or headless macOS Shortcuts app automation `Pin Coding Note`).
Helper: `~/apps/apple-notes-coding.sh` (or this repo's `scripts/apple-notes-coding.sh`; supports `--update`, `--pr "18"`, `--notify`, `--needs-owner`, `--summary`). Canonical: `AGENT-SYNC.md` § Apple Notes (live board: `~/apps/AGENT-SYNC.md`).

### Apple Notes title shape (all apps)
`[APP, Agent] topic` + second row `Sun, Aug 9, 3:52pm · PR #18`.
