---
name: board-ops
description: Use Linear through the `simple-board` CLI as the first place to look and write.  File, claim, comment, and resolve fleet items (issue ids like BF-123).  Trigger whenever starting work, hunting open P0/P1s, reviewing a peer fix, closing a lane, or when the owner mentions the board, Linear, findings, or an issue id.
---

# Board CLI on Linear (MM)

> **This install is for `MM`.**  Chat tag `[MM·session8]`.  Notes `MiniMax`.  Branches `minimax/`.  Lanes `~/apps/lanes/<Repo>/minimax-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot for seat `MM`, credential in Infisical Fleet-OPS `/zulip` (`MM_ZULIP_EMAIL`, `MM_ZULIP_KEY`, `MM_ZULIP_SITE`).  Session tag `[MM·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime (MiniMax).**  MiniMax Code has no global rules file.  The fleet pointer lives in `~/.minimax/memory/user.md` (user memory, injected into every session's system prompt); per-repo `AGENTS.md` is project memory.  Skills here are loaded on demand from `<available_skills>`, so read the one that matches before acting — nothing in this directory is auto-applied.  `config.yaml` ships `permissionMode: bypassPermissions`, so nothing prompts: hold the destructive-op pause yourself.


Linear is the system of record for fleet work (owner 2026-10-10), and `simple-board` is how agents use it.  Workspace Simple With Us, `https://linear.app/simple-with-us`, one public team per app: ST, CT, UM, CTS, DD, AFC, PS, AR, CL, BF, HH, OPS, C, FL, CC, SWU.  Every issue has an identifier such as `BF-123`.  The old board's rows, the GitHub issues and the effort-log items were all imported, so the history is there too.

Humans use Linear in the browser.  Agents use the `simple-board` CLI, which is the same command with the same flags as before; it now reads and writes Linear and signs each write with your seat.  The old board archive was retired on 2026-10-19, and the old board host now redirects to Linear (https://linear.app/simple-with-us).  The effort logs are archived.  Nothing mirrors to them any more.

## Invoke it literally

```bash
simple-board stats
simple-board list --status open,in_progress --severity P0,P1
simple-board list --app congress-trade --mine <YOUR_TAG>
simple-board show <id>
```

`<id>` is a Linear identifier (`BF-123`), a full old board id, or the first 8 or more hex characters of an old board id.  Every output line ends with the identifier, so use it from then on.

The CLI reads the Fleet app's credentials from Infisical Fleet-OPS (`fleet-secrets`) itself.  The secret must never appear on a command line, in `ps`, or in a transcript.  Do not use the claude.ai Linear connector or Composio to write to Linear: they act as Jay, not as a seat.

Some agent CLIs only allowlist a stable command prefix.  `simple-board stats` allowlists.  `B=…/simple-board; $B stats`, `$(…)`, pipes, and `&&` chains do not.

**Your seat is proven by your own bearer, never chosen.**  `simple-board` reads the seat bearer at the `SIMPLE_BOARD_BEARER_LOCATOR` your launcher bound to this run (a `FOLDER:KEY_NAME` location, never a value) and the fleet verifier names the seat.  There is no seat picker: `AGENT_SEAT`, `AGENT_LAUNCH_SEAT` and any other variable are ignored.  With no readable bearer the four writes (`file`, `claim`, `status`, `comment`) exit 6, and the reads (`list`, `show`, `stats`) run without an identity.  `--by` is only a check and must equal the verified seat (a mismatch exits 2).  A write needs the bearer-verified seat and nothing else.  `simple-board whoami` prints the verified seat (`bearer-verified`) and whether writes are on.  The `board` name on PATH is retired with its wrapper; use `simple-board`.  `--env` is only `Mac` or `cloud`.  `--by` for this seat is your verified seat (`MM` in an ordinary MiniMax session).

**Pass your session id.**  Every `simple-board` write (`file`, `claim`, `comment`, `status`), every `notes_create` / `notes_update` call (and `simple-notes`), and `apple-notes-coding.sh` needs your session id, so a headless resume-wake can find your session for the topic, and they refuse without one.  Pass it as `--session <id>` (the `session` argument on the MCP tools), else export `AGENT_SESSION`, else the tool reads your platform's own session variable, which your global rules file names.  Where a platform sets none that is known to be set (FX, Clutch, Antigravity, Muse Code, Grok Web), pass `--session <id>` or export `AGENT_SESSION`, using the id your app shows for the current chat.  An MCP server may not inherit the platform variable, so pass `session` explicitly on `notes_create` / `notes_update`; cloud seats have no shell env and always pass `session`.  Exempt, because they have one thread with the owner: every Grok bot, MA, JET, WISE and ECHO.  Notes get a small `Session: <id>` line under the timestamp.  A chat tag such as `session8` is not a session id and is refused.

## File / claim / talk / finish

```bash
simple-board file --title "Scout drops Senate rows on 502" --app congress-trade \
  --severity P1 --by <YOUR_TAG> --env Mac --where "~/apps/lanes/Congress.Trade/<seat>-fix @ <branch>" \
  --desc "path:line + repro"

simple-board claim <id> --by <YOUR_TAG> --env Mac --where "~/apps/lanes/Congress.Trade/<seat>-fix @ <branch>"

simple-board comment <id> --by <YOUR_TAG> --text "Verified on main; the shared helper is right."

simple-board status <id> completed --resolution "Landed in #2894."
```

`--app` takes an app team key (`CT`) or the old slug (`congress-trade`, `socratic-trade`, `usage-monitor`, `congress-trading-shared`, `dealdex`, `personal-site`, `fleet-infra`, which files to OPS).  If unsure, `simple-board stats` prints the live app list.

Status values: `open`, `in_progress`, `addressed` (In Review), `completed` (Done), `deployed`, `wontfix`, `duplicate`, plus `parked`.  Two rules differ from the old board:

- `status <id> duplicate` needs `--of <canonical id>` (or `dup of <id>` in `--resolution`), because Linear only makes a Duplicate through a link to the issue it duplicates.
- `claim` is check-then-set.  It exits 3 when another seat holds the issue In Progress, unless you pass `--steal --reason "..."`.  A claim is the `claimed:<SEAT>` label plus a `CLAIMED` comment; `status <id> open` releases it.  There is no 20-minute lease and no server lock, so a second claim in the same second can still collide.

The full verb table, id forms and exit codes are in `scripts/linear-board/README.md` in AI-Fleet-Coordinator.

## What you owe Linear

1. **Before substantial work:** list the app.  Claim the existing issue or file then claim.  Name the branch `<seat>/<id>-<slug>` (`minimax/afc-123-zulip-cutover`).
2. **While working:** keep `--by <YOUR_TAG>`, `--env Mac`, and `--where "lane path @ branch"` accurate.
3. **In the PR:** put `Fixes <ID>` in the body when the PR finishes the issue, and `Refs <ID>` for partial work.  A `Refs` PR never closes it, even with the id in the branch name.  Once Linear's GitHub automation is on (Jay enables it right after the cutover), a merge moves a `Fixes` issue to Done by itself; `simple-board status <id> completed` does the same by hand, works before then, and is always safe to run.
4. **When done:** `completed`, or `deployed` only after the production check, with a resolution that names the PR and what changed.  Do not leave `in_progress` after you stopped.
5. **On a peer's item:** comment with evidence.  Reviewing fixes here is expected.

## Item kinds

`agent-report`, `review-finding`, `effort-row`, `github-issue` and `sentry` are labels now (`Kind`), not separate systems.  Nothing is mirrored in either direction: there is no writeback, no retired `mac-collab-sync`, and no effort log to land.  Imported effort-log items are ordinary issues, marked `imported`.

## Do not

- Write to Linear any way except `simple-board` (no connector, no personal API key, no raw GraphQL with a borrowed token).
- Use Linear as the only closeout.  The work topic in Zulip still gets its `DONE` post.
- File a duplicate because you did not `simple-board list --search` first.
- Edit an archived `*-EFFORT-LOG.md` or `docs/EFFORT-LOG.md`.  They carry an Archived header and nobody reads them.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` § Linear and the Claim Lifecycle
- `scripts/linear-board/README.md` and `docs/protocols/linear-migration.md` in AI-Fleet-Coordinator
- `simple-board --help` / `simple-board file --help`

Clutch's app acronym is `C` (owner 2026-10-23, AFC-333), the same as its live Linear team key.  `C-n` is a Clutch Linear issue and `C#n` a Clutch PR.  `CLUTCH`, legacy `CK`, `clutch`, `harness`, and `dsh-runtime` all route to that same team, and prior `CK-n` and `CLUTCH-n` identifiers remain resolvable.  `CC` is CodeCaps, never Clutch.
