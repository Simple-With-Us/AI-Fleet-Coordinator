# board (Linear backend)

`scripts/linear-board/board` is the fleet's `board` command rebuilt on Linear, which replaces THE BOARD (owner 2026-10-10).  The design is `docs/protocols/linear-migration.md`, sections 2 (identity), 3 (workspace model), 4 (this CLI) and 6 (migration).

It is one stdlib-only Python 3 file that talks GraphQL with `urllib`.  Any seat can run it from an AI-Fleet-Coordinator checkout with `python3 scripts/linear-board/board`, including cloud seats, the Grok Bot VM and BotFleet containers.

Status: built and tested against a local mock of Linear, and checked live with read verbs only.  The shared Fleet app exists, and so does the first seat app (Claude); the other seat apps are being created (owner 2026-10-10), and a seat without one uses Fleet (see Identity and credentials).  A write needs the seat's own bearer (see Safety), and the cutover is done;  its gates are recorded below.

## Install

### Before cutover: as `board-linear`

Install it next to the old CLI and leave `~/.local/bin/board` alone.  Install from the repo only, so the live copy cannot drift from the tracked one.  Link to the managed runtime checkout, never to the human integration tree under `~/Code`:  `/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime` is a detached worktree that `com.jay.agent-sync-runtime-sync` keeps on AFC origin/main every 120 seconds (the `simple-chat` CLI already runs from it).

```bash
ln -sf "/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime/scripts/linear-board/board" "$HOME/.local/bin/board-linear"
SIMPLE_BOARD_BEARER_LOCATOR=<FOLDER:KEY_NAME> board-linear whoami
```

The symlink is resolved, so `board-ids.txt` is found next to the script.  `whoami` must print the seat's own app as the viewer (`viewer: Claude (app user)` for CLAUDE), or `viewer: Fleet (app user)` with one `notice:` line for a seat that has no app yet, and workspace `simple-with-us`; it exits 6 otherwise.  Read verbs (`list`, `show`, `stats`) and `whoami` work in this mode, with or without a bearer-verified seat.  `file`, `claim`, `status`, `comment` and `label` refuse with exit 6, because no seat is verified without a bearer.

Installing it is a helper other agents are expected to run.  Add an on-demand row to `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` and refresh the Apple Note in the same change.

### After cutover: `simple-board`, and no wrapper

The cutover swapped `~/.local/bin/board` for this CLI, and the wrapper is retired with it.  `simple-board` is the entry point on `PATH`, and a write is gated by the seat's own bearer, so no environment variable turns writes on or off:

```bash
ln -sf "/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime/scripts/simple-board" "$HOME/.local/bin/simple-board"
SIMPLE_BOARD_BEARER_LOCATOR=<FOLDER:KEY_NAME> simple-board whoami
```

The old CLI was kept as `~/.local/bin/board-legacy` (a symlink to `~/apps/mac-collab/board`) until it was removed on 2026-10-19 (AFC-363);  that path is now the frozen archive `~/apps/_retired/mac-collab-20261019/board`.  The wrapper that carried `LINEAR_BOARD_WRITES=1` was removed on 2026-10-28, because the bearer is the only write gate.

### Rollback

**The old board server is retired (2026-10-19, AFC-363).**  Its pm2 jobs are deleted, `board-legacy` is removed, the retired ingress answers 404, and `~/apps/mac-collab` was moved to `~/apps/_retired/mac-collab-20261019/`.  Linear is the only tracker, and there is no live board to go back to.

What survives is the old CLI *script*, in the frozen archive.  Set `BOARD_BACKEND=mac-collab` and the CLI runs it with the same arguments, before any other check, so a read matches the old behavior exactly.  It reads the archived `findings.db` and talks to no server, and because that database is frozen and read-only the backend serves reads only:  `file`, `claim`, `status`, `comment` and `label` are refused before the archived CLI is run, rather than failing inside a read-only database with a raw sqlite error.  The archived CLI still needs its own `MAC_COLLAB_TOKEN`, which it takes from the environment or from Infisical Fleet-OPS `/mac-collab` through `fleet-secrets`.  That folder and its tokens were deleted on the day of the retirement, so with no value in the environment the archived CLI cannot authenticate at all, and the rollback backend is dead in practice and not only read-only.  The script stays where it is (`~/apps/_retired/mac-collab-20261019/board`, or the path in `BOARD_MAC_COLLAB_CLI`); nothing is copied.

## Cutover gates

Do these before step 4 swaps `~/.local/bin/board`.  The CLI does not make them for you.

1. **A bearer-verified seat in every runtime that writes.**  `file`, `claim`, `status`, `comment` and `label` exit 6 without one, because every write is signed with the seat and the CLI never guesses one.  The seat is never chosen:  the launcher binds `SIMPLE_BOARD_BEARER_LOCATOR` (a `FOLDER:KEY_NAME` location of the seat's own admin-minted bearer, never a value) and the fleet verifier names the seat.  `AGENT_SEAT` and `AGENT_LAUNCH_SEAT` are ignored.  Reads (`list`, `show`, `stats`, `whoami`) need no seat, so `board stats` and `board list ...` in a plain shell or `zsh -lic` work as they did.  The listener's board filing passes the waking seat's `bearer_locator` from `listener.toml` (`scripts/agent_sync/daemon.py`) and strips both seat variables, because its `env -i` drops the inherited environment; a seat with no locator skips the filing.  The `board` it runs is whatever resolves on its wake PATH, which after step 4 is the wrapper below, and that turns writes on.
2. **The claim relabel.**  Run `relabel_claims.py` once (see Claims).  Until it has run, `list` and `list --mine` show no claimant for imported In Progress and In Review issues.  `list` says so on stderr, and `claim` and `show` still read the claimant from the import footer.  Owner-approved step; it adds labels and removes none.
3. **Skill updates in the step 4 skills PR** (outside this directory):  `board-ops` and `closeout` list `duplicate` as a plain status value, which now needs `--of` (see the contract table); `session-start` and `pickup-seat` use `--mine`, which reads claim labels; and the identity text in all five skills should say writes need a bearer-verified seat.
4. **Seat apps.**  Before a seat writes on Linear, `simple-board whoami` (run with that seat's bearer locator bound) exits 0 and prints `seat: <SEAT> (bearer-verified)`, and and its `identity:` line names that seat's own app, or Fleet with `fallback for <SEAT>` and a `notice:` line.  Either is fine for cutover:  a seat on Fleet still signs every write with its tag line and `claimed:` label.  An exit 6 that says the credentials look swapped means `<SEAT>.env` holds another app's credentials; that seat's writes refuse (exit 4) until the file is fixed or moved aside.

## Identity and credentials

Each seat has its own Linear app, named for the seat bot's display name (owner 2026-10-10):  CLAUDE is Claude, CODEX Codex, CURSOR Cursor, AG AG, GROK Grok, GROK-WEB Grok Web, CLUTCH Clutch, MM MM, FX Fx, MC Muse Code, MA Muse Assist, JET Jet, OPENCODE OpenCode, ECHO Echo and WISE Wise.  Each BotFleet seat in the roster is named for its tag:  BF-Builder, BF-Compiler, BF-Deployer, BF-Designer, BF-Director, BF-Fixer, BF-Housekeeper, BF-Monitor, BF-Oracle, BF-Plumber, BF-Producer and BF-Publisher (`SEAT_APP_NAMES` in the script).  WISE accepts Instinct, Wise-Instinct and Echo-Instinct; MA accepts Muse-Assist, Rob and Rob-Muse.  Renaming an app in Linear never locks its seat out:  the swapped-credentials check also accepts the aliases in `SEAT_APP_ALIASES` (Antigravity, MiniMax, FX.sh, Grok (Build), Grok (Web/iOS), Muse Code (MC), OC and the like), the seat tag itself, and any capitalisation.  A new alias goes in that table, and a test fails if two seats would share one.  A seat whose app does not exist yet, and every read without a verified seat, uses the shared Fleet app.

Lookup order, the first that applies:

1. `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` in the environment, a cloud runtime's pair.  They may hold the seat's own app or Fleet, and they still win.  No Mac runtime sets it.  Its token is cached in `Fleet.token`, as before.
2. With the bearer-verified seat naming a seat in the table:  `<SEAT>_LINEAR_CLIENT_ID` and `<SEAT>_LINEAR_CLIENT_SECRET` in Infisical Fleet-OPS `/linear`, read with `fleet-secrets get` (the seat is upper case with hyphens as underscores, so `GROK_WEB_LINEAR_CLIENT_ID`).  Then `LINEAR_<SEAT>_CLIENT_ID` and `LINEAR_<SEAT>_CLIENT_SECRET` in the AI Fleet Coordinator `/linear` folder, then the same pair in the environment.  Any spelling of GROK-BUILD is the seat GROK.  Nothing is read from `~/.secrets` (AFC-359).
3. `FLEET_LINEAR_CLIENT_ID` and `FLEET_LINEAR_CLIENT_SECRET` in Fleet-OPS `/linear`, the shared Fleet app (then the `LINEAR_FLEET_` pair in the AI Fleet Coordinator `/linear` folder).
4. Otherwise exit 4, `no Fleet credentials`.

A personal or admin API key is never read, from any file or variable.

**One stored token per app, in Infisical.**  A seat app's minted token is the JSON secret `CACHE_<SEAT>` in Fleet-OPS `/linear` and Fleet's is `CACHE_FLEET` (fields `access_token`, `token_type`, `scope`, `minted_at`, `expires_at`, `cred_fp`, `viewer`).  Every run reads it, together with the client pair, one named key at a time with `fleet-secrets get NAME` (never a dump of the store's environment);  it is used while its `cred_fp` matches the current client pair and it has more than an hour left.  Otherwise the CLI re-reads it once, mints, and writes it back with `fleet-secrets set` (the value on standard input).  No token is ever written to disk, no lock is needed (last writer wins, and any fresh token works), and a failed write only warns.  The mint rules in Safety (24 hours, re-mint on a 401 only while the stored token is the one that failed, a new mint when the credential fingerprint changes) apply per app.  Each seat app also has its own rate-limit budget.

**whoami** prints an `identity:` line (the seat's own app, or `Fleet (the shared app; fallback for <SEAT>)`), the viewer name and app flag, where the credentials came from, as an Infisical folder and key names or variable names (never a value) and a `token cache:` path.  A seat on Fleet gets one `notice:` line saying it has no app yet; its writes still work.  The contract table's `whoami` row describes the Fleet case:  with a seat app, the viewer must be that app.

**Swapped credentials.**  Before the first write with a seat app's token, the CLI asks Linear who the viewer is, once per token, and keeps the answer in that token's cache entry.  If the viewer is not that seat's app (`CLAUDE.env` holding Codex's credentials, say), `file`, `claim`, `status`, `comment` and `label` refuse with exit 4 and name the file to fix, before any write request.  Reads still work, and `whoami` exits 6 and says the credentials look swapped.  The environment pair gets the same check and accepts the seat's app or Fleet.  Writes through `Fleet.env` are not checked this way, as before.

Everything else is unchanged:  claims keep their `claimed:<SEAT>` label and `CLAIMED` comment, every issue and comment keeps its seat tag line, the CLI sends no delegate and no `createAsUser`, the scope string is the same constant, and a write still needs a bearer-verified seat.  `relabel_claims.py` never uses a seat app, whatever seat is verified:  it acts as the Fleet app, or as the cloud pair when `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` are set.

## Frozen contract

Every verb and flag of the old board CLI (`~/apps/_retired/mac-collab-20261019/board`, frozen 2026-10-19) is accepted and prints the same lines, with these deliberate breaks:

- `status <id> duplicate` needs `--of <id>` (or a `dup of <id>` in `--resolution`).  The old CLI took a bare `duplicate`; 88 board rows used it.  Linear only makes a Duplicate through a relation to a canonical issue.
- `claim` refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`.
- The five write verbs (`file`, `claim`, `status`, `comment`, `label`) need a bearer-verified seat, and `--by` must match it.
- `stats` counts open items per kind, not all items.
- `status <id> in_progress` moves the state but records no claim:  use `claim`.  `status <id> open` releases the claim.

The skills (`board-ops`, `closeout`, `session-start`, `land-lane`, `unstick-pr`) keep working with those changes plus the gates above.

**Deliberate additions** (open-loops unit B1;  nothing here changes an existing flag or output line):  `file --desc-file`, `--fix-file` and `--label`;  `comment --text-file` (and `--text` is no longer required when it is given);  the `label add|remove` verb;  `list --label`;  and `status` removing `needs-owner` and `ask-capture` on a closing status.  They exist so callers (the needs-owner, ask-capture and claim-expiry tools) never put free text in `argv`.  See Input files and Flat labels.  The old board CLI has none of them, so `BOARD_BACKEND=mac-collab` refuses them with exit 2.  One side effect of the new spellings:  `--desc` and `--fix` still match exactly, but the shorter prefixes argparse used to accept (`--de`, `--fi`) are now ambiguous.

**Agent-Sync parity additions** (AFC-329):  `list --team KEY` and `list --env mac|cloud`, `claim --comment TEXT` (appended to the `CLAIMED` comment), and the verbs `commands`, `run`, `api` and `mcp` (see Agent-Sync parity).  `BOARD_BACKEND=mac-collab` refuses them with exit 2 too.

| Verb and flags | Linear operation | Output, and what changed |
|---|---|---|
| `list --app --status --severity --kind --search --mine <SEAT> --label <NAME> --limit --json` | `issues` per priority bucket (Urgent, High, Medium, Low, none), newest first, following `endCursor` until `--limit`; `--mine` filters on the `claimed:<SEAT>` label, `--label` (repeatable, all must match) on a flat label | `N shown of M matching`, then per row `<id8>  <sev> <status> <app>` + title + `@<seat> <env>`.  The `@<seat>` is the claim label only, so a reporter's `seat:` label never shows.  New: the identifier (`AFC-123`) ends the first line, and `--json` rows carry `seats` (attribution) and `labels` (every label, `Kind/agent-report` style for grouped ones).  `M` is exact up to `LINEAR_BOARD_COUNT_CAP` (1,000), then `1000+`.  P3 and P4 share Linear's Low priority, so in a list cut by `--limit` they interleave by date. |
| `show <id> --json` | `issue(id:)` with labels, attachments, relations, then every comment page | Same sections (Description, Fix, Resolution, meta, comments).  New meta lines: identifier, url, team, state, labels, duplicate links, the import's provenance footer.  Times are Central, 12-hour.  In `--json` each comment also carries `author_id` and `author_app`, the Linear account that wrote it (null when Linear does not say):  `author` is the seat a tag line claims and can be typed by anyone, so a consumer that has to know who wrote a comment (the needs-owner notifier, for the owner's reply) reads these two. |
| `file --title --app --severity --desc\|--desc-file --fix\|--fix-file --label <NAME> --by --where --env --kind --uid --url` | `issueCreate` in the routed team, Todo, with priority and labels (plus each `--label`); `attachmentCreate` for `--url` and the uid | `filed <uuid>  (<sev> <app>)  <identifier>`.  A repeat `--uid` prints the existing issue with `(already filed; unchanged)`.  Unlike the board, a repeat does not rewrite the existing issue. |
| `claim <id> --by --where --env --session` | check, then `issueUpdate` (In Progress, `addedLabelIds` `claimed:<SEAT>` and `seat:<SEAT>`, `removedLabelIds` every other `claimed:` label and `stale-claim`), then a `CLAIMED` comment | `claimed <id8> by <SEAT> -> in_progress  (sessions: <id>)  <identifier>`.  New: refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`.  A `seat:` label is never removed.  With `LINEAR_BOARD_OWNER=1` the same update also sets `assigneeId` to the seat's own Linear user, and says so on the `claimed` line;  a steal by a seat with no Linear user clears it instead, so no stale owner lingers.  A refused assignee write retries the same update without the owner field, so the claim still lands.  Without the flag the update and the output line are unchanged. |
| `status <id> <value> --by --resolution --session` | `issueUpdate` to the mapped state (and, for `open` or `backlog`, removing the `claimed:` label;  for `completed`, `deployed`, `wontfix`, `duplicate` and `canceled`, removing `needs-owner` and `ask-capture` when the issue has them), then a comment | `<id8> -> <status>  (sessions: <id>)  <identifier>`.  A `--resolution` becomes a `Resolution (<status>): ...` comment; without one the comment is `Status: <status>`, so the seat is on record.  With `LINEAR_BOARD_OWNER=1` a release (`open`/`backlog`) of a claimed issue, or a close (including `duplicate`) of a claimed issue, also clears `assigneeId`;  an unclaimed issue keeps its assignee.  Without the flag the update is unchanged. |
| `status <id> duplicate --of <id>` (**deliberate break:** `--of` is now required) | `issueRelationCreate` (type duplicate, `issueId` = this issue, `relatedIssueId` = the canonical one), then a read back | Linear moves the issue to Duplicate itself.  The CLI never writes the Duplicate state, which Linear refuses.  If Linear leaves the issue elsewhere, it warns and names the state.  Without `--of`, a `--resolution` that says `dup of <id>` or `duplicate of <id>` (an identifier like `AFC-12` or a full board id, exactly one) supplies it, and the CLI says so on stderr.  A bare 8-hex word is never taken:  it may be a commit.  Otherwise exit 2, naming `status wontfix --resolution "duplicate, original unknown"` as the way out.  If the issue carries `needs-owner` or `ask-capture`, a second `issueUpdate` (`removedLabelIds` only) removes them after the relation. |
| `comment <id> --by --text\|--text-file --where --env --session` | `commentCreate` | `commented on <id8> as <SEAT>  (sessions: <id>)  <identifier>` |
| `label add\|remove <id> <NAME> --by` (new) | `issueUpdate` with `addedLabelIds` or `removedLabelIds` only;  `add` finds or creates the flat workspace label | `labeled <id8> +<NAME>  <identifier>` or `unlabeled <id8> -<NAME>  <identifier>`;  `<id8> already has <NAME>  (unchanged)  <identifier>` or `<id8> does not have <NAME>  (unchanged)  <identifier>` when there is nothing to do.  No comment is posted. |
| `stats` | `teams { issueCount }` plus bounded id-only counts | `total / open / P0/P1 open / done`, kind lines, `apps:`.  Kind lines now count open items only, under a new `open items by kind:` line.  Results are cached for 10 minutes per host. |
| `whoami` (new) | `viewer`, `organization` | Seat, backend, writes on or off, viewer, workspace, credentials source, token mint and expiry time.  Exits 6 unless the viewer is the Fleet app user in `simple-with-us`. |

### Ids

- A full board id (32 hex, with or without hyphens).  Imported issues reuse the board id as their Linear UUID.
- A prefix of 8 or more hex characters, resolved through `board-ids.txt` and the ids this host has already listed or shown.  An ambiguous prefix is refused with the candidates.
- A Linear identifier such as `AFC-123`.

`board-ids.txt` holds only ids:  each imported board id, plus the Linear UUID after a tab for the 617 board rows the import merged into a GitHub-keyed issue with a different UUID.  Regenerate it with `python3 scripts/linear-board/make_board_ids.py <board-to-linear.tsv>`.

Issues created after the import have no board id.  Their prefix resolves only after this host has listed or shown them (`~/.cache/linear-board/seen-ids.tsv`); otherwise use the identifier, which every line prints.

### Mapping

| Board | Linear |
|---|---|
| `--app` | Team by the import's routing:  team keys (`ST`) and board slugs (`socratic-trade`) both work, `fleet-infra` (the default) goes to OPS, and an unknown slug goes to AFC with an `app:<slug>` label. |
| status | open = Todo (a list filter on open also matches Backlog), in_progress = In Progress, addressed = In Review, completed = Done, deployed = Deployed, wontfix = Won't Fix, duplicate = Duplicate by relation.  Also accepted:  parked, backlog, canceled. |
| severity | Priority (P0 Urgent, P1 High, P2 Medium, P3 and P4 Low) plus the `Severity` label.  Priority is the source, since the Linear UI edits it; the label only tells P3 from P4. |
| `--kind` | `Kind` label:  agent-report, review-finding, effort-row, github-issue, sentry |
| `--env` | `Env` label:  Mac or Cloud |
| `--label`, `label add\|remove`, `list --label` | A flat workspace label from a fixed list:  `needs-owner`, `ask-capture`, `fleet-report`, `stale-claim` (see Flat labels) |
| claimed by, `--mine` | `claimed:<SEAT>` label plus a `CLAIMED` comment (see Claims).  The shared Fleet app cannot tell seats apart, so the label and the tag carry the seat, with a seat app too. |
| reported or touched by | `seat:<SEAT>` label, attribution only.  The import put it on reporters and claimants alike, and `claim` adds the claimant's.  It is never read as a claim and never removed. |
| `--by`, authorship | The first line of every issue and comment is the seat tag `[SEAT·session8]` (the session from `--session` or `CLAUDE_CODE_SESSION_ID`). |
| `--where`, `--env`, `--session` on comments | A `Where:` line and a `Sessions:` line at the end of the comment |
| `--uid` | The issue id is a UUID derived from the routed team, any fallback slug and the uid, so `socratic`, `socratic-trade` and `ST` give the same id.  Before creating, the CLI looks up that id, then an attachment with the uid URL (`https://board.jays.services/uid/<app>/<uid>`, or the uid itself when it is a URL), then an imported issue whose footer says ``External uid: `<uid>` ``. |

## Input files

Free text stays out of `argv` (and so out of `ps` and shell history).  `file --desc-file PATH` and `--fix-file PATH`, and `comment --text-file PATH`, read the text from a file instead of `--desc`, `--fix` and `--text`, and each pair is mutually exclusive (argparse exits 2 on both).  `comment` takes exactly one of `--text` and `--text-file`;  `--by` stays required on `comment` and `claim`.

- `PATH` must be a regular file owned by the caller with mode exactly `0600` or `0400`, at most 64 KB (65,536 bytes) of strict UTF-8.  A symbolic link, a directory, a FIFO, a file anyone else can read or write (0640, 0644, 0700 and the rest), a missing or unreadable one, an oversized one or one that is not UTF-8 exits 2 and the text is never echoed.  The checks run on the open descriptor, so a swap between the check and the read cannot get a different file past them.
- `-` reads standard input, same cap and encoding, with no file checks.  Only one of `--desc-file` and `--fix-file` can be `-`.
- Everything is validated before a credential is read or a request is sent, after the bearer-verified seat (exit 6) gate:  a gated write never reads its file.  An empty `--text-file` exits 2 like an empty `--text`.
- These flags, `--label` and the `label` verb are Linear only:  with `BOARD_BACKEND=mac-collab` the CLI exits 2 saying so, instead of passing them to the old CLI.  Every other argument still passes through untouched.

## Flat labels

Four workspace labels carry the fleet's open loops:  `needs-owner` (a decision waiting on the owner), `ask-capture` (an owner ask captured for follow-up), `fleet-report` (a report the fleet files about itself) and `stale-claim` (a claim released for going quiet).  They are flat:  no team and no parent group.  Three entry points take them and nothing else:

- `file --label NAME` (repeatable), applied with the issue.  A repeat `file --uid` returns the existing issue and changes nothing, labels included.
- `label add <id> NAME` and `label remove <id> NAME`.  `add` on an issue that has the label, and `remove` on one that lacks it, change nothing (exit 0).  `remove` never creates a label.
- `list --label NAME` (repeatable, all must match), and `--json` rows carry `labels`.

The names are matched ignoring case and surrounding spaces (`Needs-Owner` is `needs-owner`, never a second label).  Anything else exits 2 before a request, with the reason:  `claimed:*` (a claim, written by `claim`), `seat:*` (attribution), a member of the single-select `Kind`, `Severity` or `Env` groups (use `--kind`, `--severity`, `--env`), or just not one of the four.  `fleet-report` is a label, never a Kind:  `KINDS` is unchanged and `--kind fleet-report` exits 2 and points at `--label fleet-report`.

**Look-up and creation.**  A label is looked up at workspace level only (a team-scoped label of the same name can be put on that team's issues alone, so it never counts).  When Linear has none, the CLI creates it once as a flat workspace label (no `teamId`, no `parentId`) under a fixed id, then caches the id with the day-long workspace cache, so every later run, and a racing one, finds it.  A cache written before this feature has no flat-label table, so it is read again once.

**Closing removes two.**  `status` to `completed`, `deployed`, `wontfix`, `duplicate` or `canceled` (the CLI's status words, not Linear state names) removes `needs-owner` and `ask-capture` from the issue, in the same `issueUpdate` as the state change (for `duplicate`, in a label-only update after the relation, because Linear moves the issue itself).  `fleet-report` and `stale-claim` stay.  Other statuses, including `addressed`, leave all four alone.

## Claims

A claim is a `claimed:<SEAT>` label on the issue, written by `claim` together with the move to In Progress and a `CLAIMED` comment.  There is one holder at a time:  `claim` adds the claimant's label and removes every other `claimed:` label.  `status open` or `backlog` releases the claim (Todo means unclaimed).  Moving to Done, Deployed or In Review keeps the label, which records who addressed it.

Ownership mirrors the claim onto the seat's own Linear `assigneeId` only when `LINEAR_BOARD_OWNER=1`:  `claim` sets it in the same update, and `status open`/`backlog` and every closing status clear it on a claimed issue;  an unclaimed issue keeps its assignee.  Off by default, because a Linear identity field is what pilot gates 2 and 4 hold back.  The users listing is fetched only with the flag on, and a refused listing degrades to a label-only claim;  a seat resolves only to an unambiguous active user (an exact name, else one case-insensitive match);  one malformed record turns ownership off for the whole workspace until the listing is clean (label-only claims, as with a refused listing);  and a refused assignee write retries the claim without the owner field rather than losing it.

`seat:<SEAT>` labels are attribution.  The import (`build_plan.py` `seats()`) put them on every reporter and claimant, so an issue reported by CLAUDE and claimed by CODEX carries both.  In the import plan, 35 of the 159 In Progress issues carry a seat label that is not their claimant, and on Todo issues every seat label is attribution.  The CLI never treats a `seat:` label as a claim, never removes one, and `claim` adds the claimant's so the issue also shows who worked it.

Who holds an issue, in order:

1. The `claimed:` labels.  This is all `list` and `--mine` can see, because they fetch neither description nor comments.
2. For an In Progress issue with none (every imported claim, until the relabel), the provenance footer's `addressed by` line, which `claim` and `show` already have loaded.  It is normalized as the importer did (aliases such as `minimax` to `MM` and `designer` to `BF-DESIGNER`, `dsh`/`deepseek`/`harness` to `CLUTCH`), and a name that is not a seat holds nothing.  All 159 In Progress issues in the import plan carry exactly one such line.
3. Nobody.

`CLAIMED` comments are the audit trail, not a source of truth:  a comment can outlive a release, so the CLI does not infer a holder from one.

### Cutover relabel

`python3 scripts/linear-board/relabel_claims.py` adds `claimed:<SEAT>` to each In Progress and In Review issue that has none, from its footer.  It prints the plan and writes nothing unless run with `--apply`, which needs a bearer-verified seat, as every write does.  It only adds labels, skips issues that already have a marker (so it is idempotent and safe to resume, or to cap with `--limit N`), leaves closed issues alone, and uses the Fleet app's credentials (or the cloud pair when it is set, but never a seat app's, whatever seat is verified), the CLI's rate-limit handling and its loopback-only URL rule.  The import plan has 169 such issues (159 In Progress, 10 In Review), so one run is about that many `issueUpdate` calls on the Fleet app's budget.  It is an owner-approved step:  do not run it before the owner says so.

## Agent-Sync parity

The hosted ST-MCP server (`scripts/agent-sync-mcp`) gives cloud seats Linear as 175 named commands, the general `linear_schema`, `linear_query` and `linear_mutation` tools, and 11 tools proxied from Linear's official MCP server.  `board` serves the same tools from a shell, with the same guards, so a seat on the Mac or a VM has everything a hosted seat has:

| Verb | Hosted tool | What it does |
|---|---|---|
| `commands [--search WORD] [--kind read\|write] [--json]`, `commands show NAME` | the named tools' list | Lists the named commands, or one command's arguments, types, defaults and default selection. |
| `run NAME [--arg KEY=VALUE ...] [--args-json JSON\|@FILE\|-] [--selection FIELDS] [--max-chars N] [--idempotency-key KEY] [--session ID] [--json]` | `linear_<name>` | Builds the server's own `query\|mutation AgentSyncNamed(...)` document from the supplied arguments only, after the server's argument schema check and defaults.  `--arg` reads a value by the argument's declared type (an `id` stays a string;  objects are JSON).  The name may drop the `linear_` prefix. |
| `api schema [TYPE] [--field TEXT] [--offset N] [--limit N] [--json]` | `linear_schema` | A type's fields with their arguments and types, its input fields or its enum values.  `TYPE` defaults to `Query`;  try `Mutation`. |
| `api query DOCUMENT [--vars JSON] [--max-chars N] [--json]` | `linear_query` | One read-only operation. |
| `api mutation DOCUMENT --vars JSON --idempotency-key KEY [--session ID] [--json]` | `linear_mutation` | One mutation root field. |
| `mcp list`, `mcp show TOOL`, `mcp call TOOL [--args-json JSON] [--idempotency-key KEY] [--max-chars N] [--json]` | the `linear_*` official tools (`get_diff`, `list_diffs`, `save_diff_comment`, `submit_diff_review`, `merge_diff`, `extract_images`, `search_documentation` and the rest) | Calls `https://mcp.linear.app/mcp` with the same app token over Streamable HTTP:  `initialize`, `notifications/initialized`, then `tools/call`, carrying `Mcp-Session-Id` and reading a JSON or SSE reply.  Arguments are checked against the tool's served schema first. |

`DOCUMENT`, `--vars` and `--args-json` take the text itself, `@PATH` (a regular file of at most 64 KB of UTF-8), or `-` for standard input.  Results print as JSON;  `--json` wraps a `run` or `api` result in an envelope that names the command, and returns the raw MCP result (images included) for `mcp call`.  Fields whose names look secret (`token`, `secret`, `apiKey` and the like) print as `[redacted secret]`, as on the server, and a result over `--max-chars` (24,000 by default, 1,000 to 64,000) is refused:  paginate or select fewer fields.

**Guards** (ported from `linear-graphql.js`, `linear-api.js`, `linear-named.js` and `linear-public-inputs.js`, message for message).  Exactly one operation of the expected kind;  only operation and fragment definitions;  no fragment cycles or unknown spreads;  at most depth 16, 500 expanded fields, 8,000 tokens, 24,000 characters of document and of variables;  a mutation has one root field, never a protected root (anything naming `apiKey`, `oauth`, `application`, `integration`, `sso`, `scim`, `organization`, `user`, `teamMembership`, `authentication`, `webhook`, `workspace`, `role`, `permission`, `session` or `accessToken`) and never a batch or bulk root.  Named command inputs may use only the public fields of Linear's schema (`catalog/public-inputs.json`).  A `--selection` override may hold only response fields.  A mutation may not name an author (`createAsUser`, `creatorId` and the like), may not add, remove or rename a `claimed:`, `seat:` or `stale-claim` label (`board claim` owns them), and may not replace the labels of an issue that holds a claim.  Every `body` and `description` a mutation's `input` writes, and every `body` or `description` a write MCP tool sends, starts with the seat tag line (`[CLAUDE·6db9ee77]`), as the hosted server's are.  All of this runs before any credential is read, except the label lookups, which need Linear.

**Writes.**  A write command, `api mutation` and a write `mcp call` are writes:  they need a bearer-verified seat (exit 6 without one), pass the swapped-credentials check, and need `--idempotency-key` (1 to 100 of letters, digits and `_.:-`).  The key's record sits beside the identity's token cache (`<SEAT>.linear-api.json` or `Fleet.linear-api.json`, mode 600, under `flock`), keyed by the seat and the key, for 24 hours.  A rerun with the same key and the same request prints the first result and sends nothing;  the same key for a different request exits 2.  The write is sent once and never retried:  a timeout, a dropped connection, a 5xx or a GraphQL error marks the outcome unknown, the CLI says it may have reached Linear, and that key is refused for 24 hours rather than replayed.  A refusal that certainly applied nothing (a rate limit, a 401 or 403, a parse or validation error) frees the key.

### Keeping board and Agent-Sync in step

The catalogs are never typed twice.  `catalog/named-commands.json`, `catalog/official-mcp-tools.json` and `catalog/public-inputs.json` are exported from the server's JavaScript, together with `testdata/agent-sync-fixtures.json` (the document the server builds for every named command, and its verdicts on a set of guard, public-input and argument cases).  To change a Linear command or tool:

1. Change the JavaScript in `scripts/agent-sync-mcp/src` (`linear-read-catalog.js`, `linear-write-catalog.js`, `linear-named-specs.js`, `linear-mcp-specs.js` or `linear-public-inputs.js`).
2. Regenerate:  `cd scripts/agent-sync-mcp && node scripts/export-linear-catalog.mjs` (`--check` only reports stale files).
3. Commit the JSON with the JavaScript, in the same PR.
4. If a guard changed (`linear-graphql.js`, `linear-api.js`, `linear-named.js`), change its Python port in `board` the same way, and add a case to the exporter's fixture lists.

CI enforces it twice.  `scripts/agent-sync-mcp/test/linear-catalog-parity.test.mjs` fails when the committed JSON differs from a fresh export, so a one-sided JavaScript change cannot merge.  `scripts/linear-board/test_board_parity.py` fails when `board commands` does not list exactly the exported names, or when a Python port builds a different document or gives a different verdict or message than the server's fixtures record.  The fleet-cli release ships the three catalog files next to `board` (`scripts/fleet-cli-release.py`, `LINEAR_CATALOG`);  the fixtures stay in the repository.

## Safety

- **Identity.**  One Linear app per seat, with the shared `Fleet` app as the fallback, by OAuth client credentials (design section 2; see Identity and credentials).  The CLI never reads a personal or admin API key from any file or variable, so it cannot act as Jay.  The five write verbs refuse to run without a bearer-verified seat and refuse a `--by` that differs from it; reads need no seat.
- **Credentials.**  `<SEAT>_LINEAR_CLIENT_ID` and `<SEAT>_LINEAR_CLIENT_SECRET` for a seat's own app, and `FLEET_LINEAR_CLIENT_ID` and `FLEET_LINEAR_CLIENT_SECRET` for Fleet, in Infisical Fleet-OPS `/linear`, read through `fleet-secrets get` (needs the machine identity;  `fleet-secrets doctor`).  A cloud seat may set `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`, or `LINEAR_<SEAT>_CLIENT_ID` and `LINEAR_<SEAT>_CLIENT_SECRET`, in its environment instead.  The order is in Identity and credentials.  With none, every verb exits 4 with `no Fleet credentials`.
- **Token store.**  One secret per app in Fleet-OPS `/linear` (`CACHE_<SEAT>` or `CACHE_FLEET`), nothing on disk.  The CLI mints at most once per 24 hours per app.  On a 401 it re-reads the secret and re-mints only when it still holds the token that failed and the last mint is over a day old, or when the credentials changed (rotation).  `LINEAR_BOARD_REMINT=1` forces one mint.  It never revokes.  Tokens and the client secret are never printed.  A host with no `fleet-secrets` that has an `INFISICAL_AUTOMATION` identity uses the old `LINEAR_<SEAT>_CACHE_TOKEN` secret in the AI Fleet Coordinator `/linear` folder instead, and one with neither mints a token per run and keeps it nowhere.  The idempotency receipts (hashes, not secrets) are `<app>.linear-api.json` in `LINEAR_BOARD_CACHE_DIR`.
- **Scope.**  `read,write,app:assignable,app:mentionable` is a constant in the script.  Asking for a different scope string revokes every token the app holds, so it changes only by a reviewed edit, after pilot gate 1.
- **Endpoints.**  Only `https://api.linear.app/graphql` and `https://api.linear.app/oauth/token`.  The URL overrides accept `http://127.0.0.1:<port>` only (the test mock).  Redirects are refused and proxy variables are ignored.
- **Writes.**  Gated by the bearer-verified seat alone, checked before any credential is read or request is sent.  `label` is a write like the other four.
- **Input files.**  `--desc-file`, `--fix-file` and `--text-file` refuse anything but a private regular file (see Input files), so a seat never puts free text in `argv`.
- **Rate limits.**  Seats on the shared Fleet app share one request budget; each seat app has its own.  Every connection asks for an explicit `first:` and follows `endCursor`.  Team, state and label ids are cached for a day.  Linear's rate limit (HTTP 400 with `RATELIMITED`, or 429) is retried with backoff to `X-RateLimit-Requests-Reset`, but never past `LINEAR_BOARD_MAX_WAIT`; past it the CLI exits 5 and says when the budget resets.
- **Idempotence.**  Issues, relations and labels are created with client ids, so a retried request cannot create a second copy.  A failed create is judged by reading the id back, not by Linear's wording.  Comment ids are fixed too:  they come from the issue, seat, verb, a hash of the text and the session (the Central day when there is none), so rerunning a `claim`, `status` or `comment` that stopped part way posts the comment once, and a rerun inside one session or day never posts the same words twice.  The CLI says so on stderr when it skips one.  The same words from a new session, or on another day, post again.
- **Partial writes.**  A verb with several requests (`claim`, `status`, `file`) that gives up part way, on a rate limit or an unreachable Linear, names the steps that already landed instead of saying it was not applied.  `file` prints its `filed <id>` line as soon as the issue exists, before attaching the URL and uid, so the id is never lost.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `SIMPLE_BOARD_BEARER_LOCATOR` | required for writes | A `FOLDER:KEY_NAME` location of the seat's own bearer;  the verifier names the seat every write carries.  `AGENT_SEAT` is ignored.  `list`, `show`, `stats` and `whoami` run without it |
| `BOARD_BACKEND` | `linear` | `mac-collab` runs the archived old board CLI (rollback);  the server it used is retired |
| `BOARD_MAC_COLLAB_CLI` | `~/apps/_retired/mac-collab-20261019/board` | The archived old CLI that `BOARD_BACKEND=mac-collab` runs |
| `LINEAR_BOARD_FLEET_SECRETS` | `fleet-secrets` on `PATH`, else `~/.local/bin/fleet-secrets` | The `fleet-secrets` binary used for the Fleet-OPS reads (tests point it at a stub) |
| `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | unset | Cloud runtimes:  the seat's own app or Fleet.  When both are set they come first |
| `LINEAR_<SEAT>_CLIENT_ID`, `LINEAR_<SEAT>_CLIENT_SECRET` | unset | Cloud runtimes:  the seat's own app, for the bearer-verified seat only, with hyphens as underscores (`LINEAR_GROK_WEB_CLIENT_ID`) |
| `LINEAR_FLEET_APP_NAME` | `Fleet` | The app name `whoami` expects for Fleet |
| `LINEAR_BOARD_REMINT` | off | `1` allows one mint inside the 24-hour window |
| `LINEAR_BOARD_OWNER` | off | `1` mirrors ownership onto the seat's Linear `assigneeId` on `claim`, and clears it on release and on close.  Pilot-gated (2 and 4);  off by default, so the seat still rides in the label and the tag line |
| `LINEAR_BOARD_IDS` | `board-ids.txt` beside the script | Board id index |
| `LINEAR_BOARD_CACHE_DIR` | `~/.cache/linear-board` | Non-secret caches:  workspace ids, seen ids, stats |
| `LINEAR_BOARD_TIMEOUT` | 60 | Seconds per HTTP request |
| `LINEAR_BOARD_MAX_WAIT` | 90 | Total seconds of retries and rate-limit waits per request |
| `LINEAR_BOARD_BACKOFF_BASE` | 2 | First retry delay in seconds |
| `LINEAR_BOARD_COUNT_CAP` | 1000 | Most issues `list` counts for `M matching` |
| `LINEAR_BOARD_STATS_TTL` | 600 | Seconds `stats` reuses its counts |
| `LINEAR_BOARD_DUP_WAIT` | 3 | Seconds to wait before re-reading a duplicate's state |
| `LINEAR_BOARD_API_URL`, `LINEAR_BOARD_TOKEN_URL`, `LINEAR_BOARD_MCP_URL` | Linear | Tests only:  `http://127.0.0.1:<port>/...` |
| `AGENT_SESSION` | unset | The session id when `--session` is not given, for a platform with no variable of its own (FX, Clutch, Antigravity, Muse Code, Grok Web) |
| `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `CURSOR_CONVERSATION_ID` (then `CURSOR_AGENT_CHAT_ID`), `GROK_SESSION_ID`, `OPENCODE_SESSION_ID`, `__MAVIS_RUNTIME_SESSION_ID` | set by the platform | The default `--session`, read for the matching seat only (the bearer-verified seat);  a seat with no entry tries all of them in this order |

Exit codes:  0 ok, 1 error or not found, 2 usage (including a `--by` mismatch, a `duplicate` with no target, a refused URL, an unsafe, oversized or non-UTF-8 input file, or a label outside the flat list), 3 claim held by another seat, 4 no Fleet credentials, a `<SEAT>.env` without both keys, or a write whose seat credentials belong to another app, 5 rate limit outlasted the wait, 6 no bearer-verified seat on a write, or an authentication or `whoami` check failed.  7 is retired:  it was the writes-off switch, and the bearer is the only write gate now.

## Tests

```bash
python3 scripts/linear-board/test_board.py
python3 scripts/linear-board/test_board_parity.py      # Agent-Sync parity, about a minute
cd scripts/agent-sync-mcp && node --test test/linear-catalog-parity.test.mjs
```

105 tests, about three minutes, stdlib only.  Each test starts a mock of Linear's GraphQL and OAuth endpoints on 127.0.0.1 and runs the CLI as a subprocess with `HOME`, the credentials, the token cache and every cache in a temporary directory, so no test reaches Linear or a real secret.  The mock serves two nodes a page, so every list has to follow the cursor.  It stores descriptions the way Linear hands them back (`[`, `]` and `~` escaped outside code spans, `-` bullets as `*`) and comments as sent, which is what the live workspace showed.  It fails a test when a query names a connection without `first:`, when `issueUpdate` sends `labelIds`, when `issueUpdate` removes a `seat:` label, or when anything writes the Duplicate state directly.  It can rate-limit a single operation, to stop a verb between two of its requests.  It knows several apps by client id, answers the viewer query with the app a token was minted for, records which app made each request, and fails a test when a write sends `createAsUser` or a delegate.

Covered:  an imported In Progress issue shaped like UM-1447 (reported by CLAUDE, claimed by CODEX, both `seat:` labelled):  CODEX may claim it, CLAUDE is refused, a steal keeps both attribution labels; footer forms and aliases; reporter-only labels on Todo issues; `--mine`; release on `status open`; the relabel script (dry run, additive, resumable, a write needs a bearer-verified seat); a rate limit between `claim`'s update and its comment, between `status`'s, and before `file`'s attachments, with the rerun posting one comment; fixed comment ids; `duplicate` with no target, with a target in `--resolution`, and the forms that are refused; reads without a seat; every verb in text and JSON, a description round trip through Linear's markdown escaping, all three id forms plus merged, ambiguous, unknown and newly listed ids, team routing and fallback labels, idempotent `file --uid` (by derived id, by attachment, and by an imported footer), claim refusal, `--steal` with and without `--reason`, status mapping and resolution comments, duplicate by relation, comment pagination, stats and its cache, rate-limit retry and give-up, token caching, the 24-hour mint guard, re-mint after a day or a rotation, missing credentials, writes off, the bearer-verified seat on writes and `--by` checks, loopback-only URLs, and the `mac-collab` fallback.  Input files and flat labels:  description, fix and comment round trips from files and from standard input (non-ASCII included, 0400 accepted, exactly 64 KB accepted and one byte more refused), a file readable by others (0644, 0640, 0660, 0604, 0666, 0700), unreadable, a symlink, a directory, a FIFO, missing, oversized, not UTF-8 and owned by someone else all refused with no request, standard input used twice, a flag and its file form together, `--text-file` with no `--text` and with no `--by`, a gated write that never reads its file, a label created once as a flat workspace label and then reused, a team-scoped decoy ignored, a label made in the UI since the cache was written, a cache from before flat labels, four labels repeated and in any case, a repeat `--uid` that changes nothing, `claimed:*`, `seat:*`, Kind, Severity, Env and unlisted names refused on every entry point, `--kind fleet-report` refused, `label add` and `remove` (no label creation on remove, no comment, nothing else removed), `list --label` filters and `labels` in `--json`, the closing statuses removing `needs-owner` and `ask-capture` in one update (and by a label-only update for `duplicate`), the other statuses leaving them, and the `mac-collab` refusal of the Linear-only flags.  Seat apps:  two seats with their own credentials, tokens and cache files (no re-mint and no shared token when switching), a lock per cache file, the Fleet fallback and its notice, reads without `AGENT_SEAT` on Fleet, a seat outside the table, `GROK-WEB` in file and variable names, the GROK-BUILD alias, a seat's own variables, the cloud pair holding Fleet, the seat app or another seat's app, swapped credentials (exit 4 for every write before any write request, reads work, `whoami` exits 6), an incomplete seat file, re-mint and rotation rules per cache file, the relabel script staying on Fleet, and no token, client id or secret in any output.

`test_board_parity.py` (34 tests) replays `testdata/agent-sync-fixtures.json` against the Python ports (every named command's document, the GraphQL guards, the public-input allowlist and the argument schema check), checks `commands --json` and `mcp list --json` against the catalog, and runs `run`, `api` and `mcp` against the same mock extended with the parity operations and a fake official MCP server that answers in JSON or SSE:  reads, defaults and typed `--arg` values, refusals before any request, the write gate and seat check, seat tags in variables and in inline strings, a retried key that sends nothing, a conflicting key, an unknown outcome never replayed, a rate-limited write that frees its key, protected claim labels, the session header and protocol version on `tools/call`, and loopback-only MCP URLs.  It also covers `list --team`, `list --env` and `claim --comment`.

## Not built yet

- The design's counts snapshot (section 5, a scheduled job writing per-team counts) does not exist, so `stats` counts live and caches for 10 minutes.
- Claims use the `claimed:<SEAT>` label, not Linear's delegate field, and not the design's single-select `Claimed By` group (a flat label avoids reusing the names of the existing top-level `seat:` labels, which Linear may refuse as group children).  Delegation and `createAsUser` wait on pilot gates 2 and 4; seat apps sign requests, but the seat is still carried by the tag line and labels.
- `LINEAR_BOARD_OWNER=1` is the same gate, held back rather than dropped:  it sets the seat's Linear `assigneeId` on `claim` and clears it on release and on close, so the board's assignee column agrees with the claim.  It is off by default, and like `delegateId` it is a Linear identity field the pilot has not opened yet.  A seat with no active Linear user of its own still claims and simply owns nothing:  the label is the record either way.
- Until the relabel runs, `list --mine` misses imported claims (it filters on the label); it never lists an issue a seat only reported.
- `--session` is a `Sessions:` comment line, not the Usage-Monitor attachment the design sketches.
- Session ids are required (AFC-361):  `file`, `claim`, `status` and `comment` exit 2 before they write when a seat that is not exempt has no id (`--session`, then `AGENT_SESSION`, then the seat's platform variable above).  `file` also records it as a `Sessions:` line in the footer.  Grok bots (`GROK-BOT`, `GB-*`), `MA`, `JET`, `WISE` and `ECHO` have one thread with the owner and need none;  the bearer-verified seat decides that and no flag can.  `fleet-apps.json` `sessionIds` is the data, and `scripts/test_session_ids_parity.py` keeps this CLI's copy equal.
