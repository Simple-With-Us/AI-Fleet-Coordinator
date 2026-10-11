# board (Linear backend)

`scripts/linear-board/board` is the fleet's `board` command rebuilt on Linear, which replaces THE BOARD (owner 2026-10-10).  The design is `docs/protocols/linear-migration.md`, sections 2 (identity), 3 (workspace model), 4 (this CLI) and 6 (migration).

It is one stdlib-only Python 3 file that talks GraphQL with `urllib`.  Any seat can run it from an AI-Fleet-Coordinator checkout with `python3 scripts/linear-board/board`, including cloud seats, the Grok Bot VM and BotFleet containers.

Status: built and tested against a local mock of Linear, and checked live with read verbs only.  The shared Fleet app exists, and so does the first seat app (Claude); the other seat apps are being created (owner 2026-10-10), and a seat without one uses Fleet (see Identity and credentials).  Writes stay off until cutover (see Safety), and cutover has gates (see Cutover gates).

## Install

### Before cutover: as `board-linear`

Install it next to the old CLI and leave `~/.local/bin/board` alone.  Install from the repo only, so the live copy cannot drift from the tracked one.  Link to the managed runtime checkout, never to the human integration tree under `~/Code`:  `/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime` is a detached worktree that `com.jay.agent-sync-runtime-sync` keeps on AFC origin/main every 120 seconds (the `agent-sync` CLI already runs from it).

```bash
ln -sf "/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime/scripts/linear-board/board" "$HOME/.local/bin/board-linear"
AGENT_SEAT=CLAUDE board-linear whoami
```

The symlink is resolved, so `board-ids.txt` is found next to the script.  `whoami` must print the seat's own app as the viewer (`viewer: Claude (app user)` for CLAUDE), or `viewer: Fleet (app user)` with one `notice:` line for a seat that has no app yet, and workspace `simple-with-us`; it exits 6 otherwise.  Read verbs (`list`, `show`, `stats`) and `whoami` work in this mode, with or without `AGENT_SEAT`.  `file`, `claim`, `status`, `comment` and `label` refuse with exit 7.

Installing it is a helper other agents are expected to run.  Add an on-demand row to `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` and refresh the Apple Note in the same change.

### At cutover only (do not do this before step 4)

Step 4 of the design swaps `~/.local/bin/board` for this CLI and turns writes on.  The whole cutover, including this install, is the runbook `~/.local/share/linear-migration/cutover/cutover.sh` (dry by default).  The wrapper it writes keeps both settings in one place and uses absolute paths, because the listener runs `env -i` with no `PATH`:

```bash
cat > "$HOME/.local/bin/board" <<'EOF'
#!/bin/sh
export LINEAR_BOARD_WRITES=1
CHECKOUT="${BOARD_CHECKOUT:-/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime}"
for py in /opt/homebrew/bin/python3 /usr/bin/python3; do
  [ -x "$py" ] && exec "$py" "$CHECKOUT/scripts/linear-board/board" "$@"
done
echo "board: no python3 found" >&2
exit 1
EOF
chmod 755 "$HOME/.local/bin/board"
```

The old CLI stays as `~/.local/bin/board-legacy` (a symlink to `~/apps/mac-collab/board`).  Cloud seats and BotFleet set `LINEAR_BOARD_WRITES=1` in their own environment at the same moment.

### Rollback

Set `BOARD_BACKEND=mac-collab`.  The CLI then runs the old board CLI with the same arguments, before any other check, so the result matches today's behavior exactly.  The old script stays where it is (`~/apps/mac-collab/board`, or the path in `BOARD_MAC_COLLAB_CLI`); nothing is copied.

## Cutover gates

Do these before step 4 swaps `~/.local/bin/board`.  The CLI does not make them for you.

1. **`AGENT_SEAT` in every runtime that writes.**  `file`, `claim`, `status`, `comment` and `label` exit 2 without it, because every write is signed with the seat and the CLI never guesses one.  Reads (`list`, `show`, `stats`, `whoami`) need no seat, so `board stats` and `board list ...` in a plain shell or `zsh -lic` work as they did.  Interactive seats set it in the same command (`AGENT_SEAT=<SEAT> board claim ...`), because shell state may not persist between commands.  Daemons and cloud runtimes (BotFleet roles, Grok Bot personas, the cloud seats) set it in their own environment.  Never put it in Claude Code settings `env` or any other global setting:  that would override a launcher's seat for every session and cause `--by` mismatches.  The listener's board filing now passes the seat it is waking (`scripts/agent_sync/daemon.py`, `claude_env(..., seat=)`), because its `env -i` drops the inherited one.  The `board` it runs is whatever resolves on its wake PATH, which after step 4 is the wrapper below, and that turns writes on.
2. **The claim relabel.**  Run `relabel_claims.py` once (see Claims).  Until it has run, `list` and `list --mine` show no claimant for imported In Progress and In Review issues.  `list` says so on stderr, and `claim` and `show` still read the claimant from the import footer.  Owner-approved step; it adds labels and removes none.
3. **Skill updates in the step 4 skills PR** (outside this directory):  `board-ops` and `closeout` list `duplicate` as a plain status value, which now needs `--of` (see the contract table); `session-start` and `pickup-seat` use `--mine`, which reads claim labels; and the identity text in all five skills should say writes need `AGENT_SEAT`.
4. **Seat apps.**  Before a seat writes on Linear, `AGENT_SEAT=<SEAT> board whoami` exits 0 and its `identity:` line names that seat's own app, or Fleet with `fallback for <SEAT>` and a `notice:` line.  Either is fine for cutover:  a seat on Fleet still signs every write with its tag line and `claimed:` label.  An exit 6 that says the credentials look swapped means `<SEAT>.env` holds another app's credentials; that seat's writes refuse (exit 4) until the file is fixed or moved aside.

## Identity and credentials

Each seat has its own Linear app, named for the seat bot's display name (owner 2026-10-10):  CLAUDE is Claude, CODEX Codex, CURSOR Cursor, AG AG, GROK Grok, GROK-WEB Grok Web, CLUTCH Clutch, MM MM, FX Fx, MC Muse Code, MA Muse Assist, JET Jet, OPENCODE OpenCode, ECHO Echo (Instinct) and INSTINCT Wise (Instinct) (`SEAT_APP_NAMES` in the script;  WISE is accepted as another spelling of INSTINCT, `SEAT_TAG_ALIASES`).  Renaming an app in Linear never locks its seat out:  the swapped-credentials check also accepts the aliases in `SEAT_APP_ALIASES` (Antigravity, MiniMax, FX.sh, Grok (Build), Grok (Web/iOS), Muse Code (MC), OC and the like), the seat tag itself, and any capitalisation.  A new alias goes in that table, and a test fails if two seats would share one.  A seat whose app does not exist yet, and every read without `AGENT_SEAT`, uses the shared Fleet app.

Lookup order, the first that applies:

1. `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` in the environment, a cloud runtime's pair.  They may hold the seat's own app or Fleet, and they still win:  this pair now comes before every file, where it used to come after `Fleet.env`.  No Mac runtime sets it.  Its token is cached in `Fleet.token`, as before.
2. With `AGENT_SEAT` naming a seat in the table:  `<SEAT>.env` beside `Fleet.env` (`~/.secrets/Linear/CLAUDE.env`; the seat is upper case with hyphens kept, so `GROK-WEB.env`) holding `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`.  Then `LINEAR_<SEAT>_CLIENT_ID` and `LINEAR_<SEAT>_CLIENT_SECRET` in the environment, with hyphens as underscores (`LINEAR_GROK_WEB_CLIENT_ID`).  A `<SEAT>.env` without both keys stops with exit 4 rather than falling back silently.  Any spelling of GROK-BUILD is the seat GROK, so it reads `GROK.env`.
3. `Fleet.env`, the shared Fleet app (the `LINEAR_FLEET_` key forms also work).
4. Otherwise exit 4, `no Fleet credentials`.

A personal or admin API key is never read, from any file or variable.

**One token cache per app.**  A seat app's token lives in `~/.secrets/Linear/cache/<SEAT>.token` and Fleet's in `Fleet.token`, each mode 600 under its own `flock`.  The mint rules in Safety (24 hours, re-mint on a 401 only while the cache still holds the failed token, a new mint when the credential fingerprint changes) apply per file, so switching seats on one Mac never re-mints, never shares a token and never waits on another seat's lock.  Each seat app also has its own rate-limit budget.

**whoami** prints an `identity:` line (the seat's own app, or `Fleet (the shared app; fallback for <SEAT>)`), the viewer name and app flag, the credentials file path or variable names (never a value) and a `token cache:` path.  A seat on Fleet gets one `notice:` line saying it has no app yet; its writes still work.  The contract table's `whoami` row describes the Fleet case:  with a seat app, the viewer must be that app.

**Swapped credentials.**  Before the first write with a seat app's token, the CLI asks Linear who the viewer is, once per token, and keeps the answer in that token's cache entry.  If the viewer is not that seat's app (`CLAUDE.env` holding Codex's credentials, say), `file`, `claim`, `status`, `comment` and `label` refuse with exit 4 and name the file to fix, before any write request.  Reads still work, and `whoami` exits 6 and says the credentials look swapped.  The environment pair gets the same check and accepts the seat's app or Fleet.  Writes through `Fleet.env` are not checked this way, as before.

Everything else is unchanged:  claims keep their `claimed:<SEAT>` label and `CLAIMED` comment, every issue and comment keeps its seat tag line, the CLI sends no delegate and no `createAsUser`, the scope string is the same constant, and writes still need `LINEAR_BOARD_WRITES=1` and `AGENT_SEAT`.  `relabel_claims.py` never uses a seat app, whatever `AGENT_SEAT` says:  it acts as the Fleet app, or as the cloud pair when `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` are set.

## Frozen contract

Every verb and flag of `~/apps/mac-collab/board` is accepted and prints the same lines, with these deliberate breaks:

- `status <id> duplicate` needs `--of <id>` (or a `dup of <id>` in `--resolution`).  The old CLI took a bare `duplicate`; 88 board rows used it.  Linear only makes a Duplicate through a relation to a canonical issue.
- `claim` refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`.
- The five write verbs (`file`, `claim`, `status`, `comment`, `label`) need `AGENT_SEAT`, and `--by` must match it.
- `stats` counts open items per kind, not all items.
- `status <id> in_progress` moves the state but records no claim:  use `claim`.  `status <id> open` releases the claim.

The skills (`board-ops`, `closeout`, `session-start`, `land-lane`, `unstick-pr`) keep working with those changes plus the gates above.

**Deliberate additions** (open-loops unit B1;  nothing here changes an existing flag or output line):  `file --desc-file`, `--fix-file` and `--label`;  `comment --text-file` (and `--text` is no longer required when it is given);  the `label add|remove` verb;  `list --label`;  and `status` removing `needs-owner` and `ask-capture` on a closing status.  They exist so callers (the needs-owner, ask-capture and claim-expiry tools) never put free text in `argv`.  See Input files and Flat labels.  The old board CLI has none of them, so `BOARD_BACKEND=mac-collab` refuses them with exit 2.  One side effect of the new spellings:  `--desc` and `--fix` still match exactly, but the shorter prefixes argparse used to accept (`--de`, `--fi`) are now ambiguous.

| Verb and flags | Linear operation | Output, and what changed |
|---|---|---|
| `list --app --status --severity --kind --search --mine <SEAT> --label <NAME> --limit --json` | `issues` per priority bucket (Urgent, High, Medium, Low, none), newest first, following `endCursor` until `--limit`; `--mine` filters on the `claimed:<SEAT>` label, `--label` (repeatable, all must match) on a flat label | `N shown of M matching`, then per row `<id8>  <sev> <status> <app>` + title + `@<seat> <env>`.  The `@<seat>` is the claim label only, so a reporter's `seat:` label never shows.  New: the identifier (`AFC-123`) ends the first line, and `--json` rows carry `seats` (attribution) and `labels` (every label, `Kind/agent-report` style for grouped ones).  `M` is exact up to `LINEAR_BOARD_COUNT_CAP` (1,000), then `1000+`.  P3 and P4 share Linear's Low priority, so in a list cut by `--limit` they interleave by date. |
| `show <id> --json` | `issue(id:)` with labels, attachments, relations, then every comment page | Same sections (Description, Fix, Resolution, meta, comments).  New meta lines: identifier, url, team, state, labels, duplicate links, the import's provenance footer.  Times are Central, 12-hour.  In `--json` each comment also carries `author_id` and `author_app`, the Linear account that wrote it (null when Linear does not say):  `author` is the seat a tag line claims and can be typed by anyone, so a consumer that has to know who wrote a comment (the needs-owner notifier, for the owner's reply) reads these two. |
| `file --title --app --severity --desc\|--desc-file --fix\|--fix-file --label <NAME> --by --where --env --kind --uid --url` | `issueCreate` in the routed team, Todo, with priority and labels (plus each `--label`); `attachmentCreate` for `--url` and the uid | `filed <uuid>  (<sev> <app>)  <identifier>`.  A repeat `--uid` prints the existing issue with `(already filed; unchanged)`.  Unlike the board, a repeat does not rewrite the existing issue. |
| `claim <id> --by --where --env --session` | check, then `issueUpdate` (In Progress, `addedLabelIds` `claimed:<SEAT>` and `seat:<SEAT>`, `removedLabelIds` every other `claimed:` label and `stale-claim`), then a `CLAIMED` comment | `claimed <id8> by <SEAT> -> in_progress  <identifier>`.  New: refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`.  A `seat:` label is never removed. |
| `status <id> <value> --by --resolution --session` | `issueUpdate` to the mapped state (and, for `open` or `backlog`, removing the `claimed:` label;  for `completed`, `deployed`, `wontfix`, `duplicate` and `canceled`, removing `needs-owner` and `ask-capture` when the issue has them), then a comment | `<id8> -> <status>  <identifier>`.  A `--resolution` becomes a `Resolution (<status>): ...` comment; without one the comment is `Status: <status>`, so the seat is on record. |
| `status <id> duplicate --of <id>` (**deliberate break:** `--of` is now required) | `issueRelationCreate` (type duplicate, `issueId` = this issue, `relatedIssueId` = the canonical one), then a read back | Linear moves the issue to Duplicate itself.  The CLI never writes the Duplicate state, which Linear refuses.  If Linear leaves the issue elsewhere, it warns and names the state.  Without `--of`, a `--resolution` that says `dup of <id>` or `duplicate of <id>` (an identifier like `AFC-12` or a full board id, exactly one) supplies it, and the CLI says so on stderr.  A bare 8-hex word is never taken:  it may be a commit.  Otherwise exit 2, naming `status wontfix --resolution "duplicate, original unknown"` as the way out.  If the issue carries `needs-owner` or `ask-capture`, a second `issueUpdate` (`removedLabelIds` only) removes them after the relation. |
| `comment <id> --by --text\|--text-file --where --env --session` | `commentCreate` | `commented on <id8> as <SEAT>  <identifier>` |
| `label add\|remove <id> <NAME> --by` (new) | `issueUpdate` with `addedLabelIds` or `removedLabelIds` only;  `add` finds or creates the flat workspace label | `labeled <id8> +<NAME>  <identifier>` or `unlabeled <id8> -<NAME>  <identifier>`;  `<id8> already has <NAME>  (unchanged)  <identifier>` or `<id8> does not have <NAME>  (unchanged)  <identifier>` when there is nothing to do.  No comment is posted. |
| `stats` | `teams { issueCount }` plus bounded id-only counts | `total / open / P0/P1 open / done`, kind lines, `apps:`.  Kind lines now count open items only, under a new `open items by kind:` line.  Results are cached for 10 minutes per host. |
| `whoami` (new) | `viewer`, `organization` | Seat, backend, writes on or off, viewer, workspace, credentials source, token mint time and count.  Exits 6 unless the viewer is the Fleet app user in `simple-with-us`. |

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
- Everything is validated before a credential is read or a request is sent, after the writes-off (exit 7) and `AGENT_SEAT` (exit 2) gates:  a gated write never reads its file.  An empty `--text-file` exits 2 like an empty `--text`.
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

`seat:<SEAT>` labels are attribution.  The import (`build_plan.py` `seats()`) put them on every reporter and claimant, so an issue reported by CLAUDE and claimed by CODEX carries both.  In the import plan, 35 of the 159 In Progress issues carry a seat label that is not their claimant, and on Todo issues every seat label is attribution.  The CLI never treats a `seat:` label as a claim, never removes one, and `claim` adds the claimant's so the issue also shows who worked it.

Who holds an issue, in order:

1. The `claimed:` labels.  This is all `list` and `--mine` can see, because they fetch neither description nor comments.
2. For an In Progress issue with none (every imported claim, until the relabel), the provenance footer's `addressed by` line, which `claim` and `show` already have loaded.  It is normalized as the importer did (aliases such as `minimax` to `MM` and `designer` to `BF-DESIGNER`, `dsh`/`deepseek`/`harness` to `CLUTCH`), and a name that is not a seat holds nothing.  All 159 In Progress issues in the import plan carry exactly one such line.
3. Nobody.

`CLAIMED` comments are the audit trail, not a source of truth:  a comment can outlive a release, so the CLI does not infer a holder from one.

### Cutover relabel

`python3 scripts/linear-board/relabel_claims.py` adds `claimed:<SEAT>` to each In Progress and In Review issue that has none, from its footer.  It prints the plan and writes nothing unless run with `--apply` and `LINEAR_BOARD_WRITES=1`.  It only adds labels, skips issues that already have a marker (so it is idempotent and safe to resume, or to cap with `--limit N`), leaves closed issues alone, and uses the Fleet app's credentials (or the cloud pair when it is set, but never a seat app's, whatever `AGENT_SEAT` says), the CLI's rate-limit handling and its loopback-only URL rule.  The import plan has 169 such issues (159 In Progress, 10 In Review), so one run is about that many `issueUpdate` calls on the Fleet app's budget.  It is an owner-approved step:  do not run it before the owner says so.

## Safety

- **Identity.**  One Linear app per seat, with the shared `Fleet` app as the fallback, by OAuth client credentials (design section 2; see Identity and credentials).  The CLI never reads a personal or admin API key from any file or variable, so it cannot act as Jay.  The five write verbs refuse to run without `AGENT_SEAT` and refuse a `--by` that differs from it; reads need no seat.
- **Credentials.**  `~/.secrets/Linear/<SEAT>.env` for a seat's own app and `~/.secrets/Linear/Fleet.env` for Fleet, each holding `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`.  A cloud seat may set `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`, or `LINEAR_<SEAT>_CLIENT_ID` and `LINEAR_<SEAT>_CLIENT_SECRET`, in its environment instead.  The order is in Identity and credentials.  With none, every verb exits 4 with `no Fleet credentials`.
- **Token cache.**  One file per app in `~/.secrets/Linear/cache/` (`<SEAT>.token` or `Fleet.token`), mode 600, each read and written under its own `flock`.  The CLI mints at most once per 24 hours per host and file.  On a 401 it re-mints only when the cache still holds the token that failed and the last mint is over a day old, or when the credentials changed (rotation).  `LINEAR_BOARD_REMINT=1` forces one mint.  It never revokes.  Tokens and the client secret are never printed.
- **Scope.**  `read,write,app:assignable,app:mentionable` is a constant in the script.  Asking for a different scope string revokes every token the app holds, so it changes only by a reviewed edit, after pilot gate 1.
- **Endpoints.**  Only `https://api.linear.app/graphql` and `https://api.linear.app/oauth/token`.  The URL overrides accept `http://127.0.0.1:<port>` only (the test mock).  Redirects are refused and proxy variables are ignored.
- **Writes.**  Off unless `LINEAR_BOARD_WRITES=1`, checked before any credential is read or request is sent.  `label` is a write like the other four.
- **Input files.**  `--desc-file`, `--fix-file` and `--text-file` refuse anything but a private regular file (see Input files), so a seat never puts free text in `argv`.
- **Rate limits.**  Seats on the shared Fleet app share one request budget; each seat app has its own.  Every connection asks for an explicit `first:` and follows `endCursor`.  Team, state and label ids are cached for a day.  Linear's rate limit (HTTP 400 with `RATELIMITED`, or 429) is retried with backoff to `X-RateLimit-Requests-Reset`, but never past `LINEAR_BOARD_MAX_WAIT`; past it the CLI exits 5 and says when the budget resets.
- **Idempotence.**  Issues, relations and labels are created with client ids, so a retried request cannot create a second copy.  A failed create is judged by reading the id back, not by Linear's wording.  Comment ids are fixed too:  they come from the issue, seat, verb, a hash of the text and the session (the Central day when there is none), so rerunning a `claim`, `status` or `comment` that stopped part way posts the comment once, and a rerun inside one session or day never posts the same words twice.  The CLI says so on stderr when it skips one.  The same words from a new session, or on another day, post again.
- **Partial writes.**  A verb with several requests (`claim`, `status`, `file`) that gives up part way, on a rate limit or an unreachable Linear, names the steps that already landed instead of saying it was not applied.  `file` prints its `filed <id>` line as soon as the issue exists, before attaching the URL and uid, so the id is never lost.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_SEAT` | required for writes | The seat tag every write carries.  `list`, `show`, `stats` and `whoami` run without it |
| `LINEAR_BOARD_WRITES` | off | `1` enables `file`, `claim`, `status`, `comment`, `label` |
| `BOARD_BACKEND` | `linear` | `mac-collab` runs the old board CLI (rollback) |
| `BOARD_MAC_COLLAB_CLI` | `~/apps/mac-collab/board` | The old CLI that `BOARD_BACKEND=mac-collab` runs |
| `LINEAR_FLEET_ENV` | `~/.secrets/Linear/Fleet.env` | Fleet client credentials file (must end in `.env`).  Seat files, `<SEAT>.env`, sit in the same directory |
| `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | unset | Cloud runtimes:  the seat's own app or Fleet.  When both are set they come first |
| `LINEAR_<SEAT>_CLIENT_ID`, `LINEAR_<SEAT>_CLIENT_SECRET` | unset | Cloud runtimes:  the seat's own app, for the seat in `AGENT_SEAT` only, with hyphens as underscores (`LINEAR_GROK_WEB_CLIENT_ID`) |
| `LINEAR_FLEET_TOKEN_CACHE` | `~/.secrets/Linear/cache/Fleet.token` | Fleet token cache (mode 600, flock).  Seat caches, `<SEAT>.token`, sit in the same directory |
| `LINEAR_FLEET_APP_NAME` | `Fleet` | The app name `whoami` expects for Fleet |
| `LINEAR_BOARD_REMINT` | off | `1` allows one mint inside the 24-hour window |
| `LINEAR_BOARD_IDS` | `board-ids.txt` beside the script | Board id index |
| `LINEAR_BOARD_CACHE_DIR` | `~/.cache/linear-board` | Non-secret caches:  workspace ids, seen ids, stats |
| `LINEAR_BOARD_TIMEOUT` | 60 | Seconds per HTTP request |
| `LINEAR_BOARD_MAX_WAIT` | 90 | Total seconds of retries and rate-limit waits per request |
| `LINEAR_BOARD_BACKOFF_BASE` | 2 | First retry delay in seconds |
| `LINEAR_BOARD_COUNT_CAP` | 1000 | Most issues `list` counts for `M matching` |
| `LINEAR_BOARD_STATS_TTL` | 600 | Seconds `stats` reuses its counts |
| `LINEAR_BOARD_DUP_WAIT` | 3 | Seconds to wait before re-reading a duplicate's state |
| `LINEAR_BOARD_API_URL`, `LINEAR_BOARD_TOKEN_URL` | Linear | Tests only:  `http://127.0.0.1:<port>/...` |
| `CLAUDE_CODE_SESSION_ID` | set by Claude Code | The default `--session` |

Exit codes:  0 ok, 1 error or not found, 2 usage (including no `AGENT_SEAT` on a write, a `--by` mismatch, a `duplicate` with no target, a refused URL, an unsafe, oversized or non-UTF-8 input file, or a label outside the flat list), 3 claim held by another seat, 4 no Fleet credentials, a `<SEAT>.env` without both keys, or a write whose seat credentials belong to another app, 5 rate limit outlasted the wait, 6 authentication or `whoami` check failed, 7 a write while writes are off.

## Tests

```bash
python3 scripts/linear-board/test_board.py
```

105 tests, about three minutes, stdlib only.  Each test starts a mock of Linear's GraphQL and OAuth endpoints on 127.0.0.1 and runs the CLI as a subprocess with `HOME`, the credentials, the token cache and every cache in a temporary directory, so no test reaches Linear or a real secret.  The mock serves two nodes a page, so every list has to follow the cursor.  It stores descriptions the way Linear hands them back (`[`, `]` and `~` escaped outside code spans, `-` bullets as `*`) and comments as sent, which is what the live workspace showed.  It fails a test when a query names a connection without `first:`, when `issueUpdate` sends `labelIds`, when `issueUpdate` removes a `seat:` label, or when anything writes the Duplicate state directly.  It can rate-limit a single operation, to stop a verb between two of its requests.  It knows several apps by client id, answers the viewer query with the app a token was minted for, records which app made each request, and fails a test when a write sends `createAsUser` or a delegate.

Covered:  an imported In Progress issue shaped like UM-1447 (reported by CLAUDE, claimed by CODEX, both `seat:` labelled):  CODEX may claim it, CLAUDE is refused, a steal keeps both attribution labels; footer forms and aliases; reporter-only labels on Todo issues; `--mine`; release on `status open`; the relabel script (dry run, additive, resumable, writes off); a rate limit between `claim`'s update and its comment, between `status`'s, and before `file`'s attachments, with the rerun posting one comment; fixed comment ids; `duplicate` with no target, with a target in `--resolution`, and the forms that are refused; reads without `AGENT_SEAT`; every verb in text and JSON, a description round trip through Linear's markdown escaping, all three id forms plus merged, ambiguous, unknown and newly listed ids, team routing and fallback labels, idempotent `file --uid` (by derived id, by attachment, and by an imported footer), claim refusal, `--steal` with and without `--reason`, status mapping and resolution comments, duplicate by relation, comment pagination, stats and its cache, rate-limit retry and give-up, token caching, the 24-hour mint guard, re-mint after a day or a rotation, missing credentials, writes off, `AGENT_SEAT` on writes and `--by` checks, loopback-only URLs, and the `mac-collab` fallback.  Input files and flat labels:  description, fix and comment round trips from files and from standard input (non-ASCII included, 0400 accepted, exactly 64 KB accepted and one byte more refused), a file readable by others (0644, 0640, 0660, 0604, 0666, 0700), unreadable, a symlink, a directory, a FIFO, missing, oversized, not UTF-8 and owned by someone else all refused with no request, standard input used twice, a flag and its file form together, `--text-file` with no `--text` and with no `--by`, a gated write that never reads its file, a label created once as a flat workspace label and then reused, a team-scoped decoy ignored, a label made in the UI since the cache was written, a cache from before flat labels, four labels repeated and in any case, a repeat `--uid` that changes nothing, `claimed:*`, `seat:*`, Kind, Severity, Env and unlisted names refused on every entry point, `--kind fleet-report` refused, `label add` and `remove` (no label creation on remove, no comment, nothing else removed), `list --label` filters and `labels` in `--json`, the closing statuses removing `needs-owner` and `ask-capture` in one update (and by a label-only update for `duplicate`), the other statuses leaving them, and the `mac-collab` refusal of the Linear-only flags.  Seat apps:  two seats with their own credentials, tokens and cache files (no re-mint and no shared token when switching), a lock per cache file, the Fleet fallback and its notice, reads without `AGENT_SEAT` on Fleet, a seat outside the table, `GROK-WEB` in file and variable names, the GROK-BUILD alias, a seat's own variables, the cloud pair holding Fleet, the seat app or another seat's app, swapped credentials (exit 4 for every write before any write request, reads work, `whoami` exits 6), an incomplete seat file, re-mint and rotation rules per cache file, the relabel script staying on Fleet, and no token, client id or secret in any output.

## Not built yet

- The design's counts snapshot (section 5, a scheduled job writing per-team counts) does not exist, so `stats` counts live and caches for 10 minutes.
- Claims use the `claimed:<SEAT>` label, not Linear's delegate field, and not the design's single-select `Claimed By` group (a flat label avoids reusing the names of the existing top-level `seat:` labels, which Linear may refuse as group children).  Delegation and `createAsUser` wait on pilot gates 2 and 4; seat apps sign requests, but the seat is still carried by the tag line and labels.
- Until the relabel runs, `list --mine` misses imported claims (it filters on the label); it never lists an issue a seat only reported.
- `--session` is a `Sessions:` comment line, not the Usage-Monitor attachment the design sketches.
