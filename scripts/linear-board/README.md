# board (Linear backend)

`scripts/linear-board/board` is the fleet's `board` command rebuilt on Linear, which replaces THE BOARD (owner 2026-10-10).  The design is `docs/protocols/linear-migration.md`, sections 2 (identity), 3 (workspace model), 4 (this CLI) and 6 (migration).

It is one stdlib-only Python 3 file that talks GraphQL with `urllib`.  Any seat can run it from an AI-Fleet-Coordinator checkout with `python3 scripts/linear-board/board`, including cloud seats, the Grok Bot VM and BotFleet containers.

Status: built and tested against a local mock of Linear only.  The Fleet Linear app and its client credentials do not exist yet, so every real call stops with `no Fleet credentials`.  Writes stay off until cutover (see Safety), and cutover has gates (see Cutover gates).

## Install

### Before cutover: as `board-linear`

Install it next to the old CLI and leave `~/.local/bin/board` alone.  Install from the repo only, so the live copy cannot drift from the tracked one.  Link to the managed runtime checkout, never to the human integration tree under `~/Code`:  `/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime` is a detached worktree that `com.jay.agent-sync-runtime-sync` keeps on AFC origin/main every 120 seconds (the `agent-sync` CLI already runs from it).

```bash
ln -sf "/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime/scripts/linear-board/board" "$HOME/.local/bin/board-linear"
AGENT_SEAT=CLAUDE board-linear whoami
```

The symlink is resolved, so `board-ids.txt` is found next to the script.  `whoami` must print `viewer: Fleet (app user)` and workspace `simple-with-us`; it exits 6 otherwise.  Read verbs (`list`, `show`, `stats`) and `whoami` work in this mode, with or without `AGENT_SEAT`.  `file`, `claim`, `status` and `comment` refuse with exit 7.

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

1. **`AGENT_SEAT` in every runtime that writes.**  `file`, `claim`, `status` and `comment` exit 2 without it, because every write is signed with the seat and the CLI never guesses one.  Reads (`list`, `show`, `stats`, `whoami`) need no seat, so `board stats` and `board list ...` in a plain shell or `zsh -lic` work as they did.  Set it in each runtime's own environment (Claude Code settings `env`, Codex, Cursor, the cloud seats, BotFleet).  The listener's board filing now passes the seat it is waking (`scripts/agent_sync/daemon.py`, `claude_env(..., seat=)`), because its `env -i` drops the inherited one.  The `board` it runs is whatever resolves on its wake PATH, which after step 4 is the wrapper below, and that turns writes on.
2. **The claim relabel.**  Run `relabel_claims.py` once (see Claims).  Until it has run, `list` and `list --mine` show no claimant for imported In Progress and In Review issues.  `list` says so on stderr, and `claim` and `show` still read the claimant from the import footer.  Owner-approved step; it adds labels and removes none.
3. **Skill updates in the step 4 skills PR** (outside this directory):  `board-ops` and `closeout` list `duplicate` as a plain status value, which now needs `--of` (see the contract table); `session-start` and `pickup-seat` use `--mine`, which reads claim labels; and the identity text in all five skills should say writes need `AGENT_SEAT`.

## Frozen contract

Every verb and flag of `~/apps/mac-collab/board` is accepted and prints the same lines, with these deliberate breaks:

- `status <id> duplicate` needs `--of <id>` (or a `dup of <id>` in `--resolution`).  The old CLI took a bare `duplicate`; 88 board rows used it.  Linear only makes a Duplicate through a relation to a canonical issue.
- `claim` refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`.
- The four write verbs need `AGENT_SEAT`, and `--by` must match it.
- `stats` counts open items per kind, not all items.
- `status <id> in_progress` moves the state but records no claim:  use `claim`.  `status <id> open` releases the claim.

The skills (`board-ops`, `closeout`, `session-start`, `land-lane`, `unstick-pr`) keep working with those changes plus the gates above.

| Verb and flags | Linear operation | Output, and what changed |
|---|---|---|
| `list --app --status --severity --kind --search --mine <SEAT> --limit --json` | `issues` per priority bucket (Urgent, High, Medium, Low, none), newest first, following `endCursor` until `--limit`; `--mine` filters on the `claimed:<SEAT>` label | `N shown of M matching`, then per row `<id8>  <sev> <status> <app>` + title + `@<seat> <env>`.  The `@<seat>` is the claim label only, so a reporter's `seat:` label never shows.  New: the identifier (`AFC-123`) ends the first line, and `--json` rows carry `seats` (attribution).  `M` is exact up to `LINEAR_BOARD_COUNT_CAP` (1,000), then `1000+`.  P3 and P4 share Linear's Low priority, so in a list cut by `--limit` they interleave by date. |
| `show <id> --json` | `issue(id:)` with labels, attachments, relations, then every comment page | Same sections (Description, Fix, Resolution, meta, comments).  New meta lines: identifier, url, team, state, labels, duplicate links, the import's provenance footer.  Times are Central, 12-hour. |
| `file --title --app --severity --desc --fix --by --where --env --kind --uid --url` | `issueCreate` in the routed team, Todo, with priority and labels; `attachmentCreate` for `--url` and the uid | `filed <uuid>  (<sev> <app>)  <identifier>`.  A repeat `--uid` prints the existing issue with `(already filed; unchanged)`.  Unlike the board, a repeat does not rewrite the existing issue. |
| `claim <id> --by --where --env --session` | check, then `issueUpdate` (In Progress, `addedLabelIds` `claimed:<SEAT>` and `seat:<SEAT>`, `removedLabelIds` every other `claimed:` label and `stale-claim`), then a `CLAIMED` comment | `claimed <id8> by <SEAT> -> in_progress  <identifier>`.  New: refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`.  A `seat:` label is never removed. |
| `status <id> <value> --by --resolution --session` | `issueUpdate` to the mapped state (and, for `open` or `backlog`, removing the `claimed:` label), then a comment | `<id8> -> <status>  <identifier>`.  A `--resolution` becomes a `Resolution (<status>): ...` comment; without one the comment is `Status: <status>`, so the seat is on record. |
| `status <id> duplicate --of <id>` (**deliberate break:** `--of` is now required) | `issueRelationCreate` (type duplicate, `issueId` = this issue, `relatedIssueId` = the canonical one), then a read back | Linear moves the issue to Duplicate itself.  The CLI never writes the Duplicate state, which Linear refuses.  If Linear leaves the issue elsewhere, it warns and names the state.  Without `--of`, a `--resolution` that says `dup of <id>` or `duplicate of <id>` (an identifier like `AFC-12` or a full board id, exactly one) supplies it, and the CLI says so on stderr.  A bare 8-hex word is never taken:  it may be a commit.  Otherwise exit 2, naming `status wontfix --resolution "duplicate, original unknown"` as the way out. |
| `comment <id> --by --text --where --env --session` | `commentCreate` | `commented on <id8> as <SEAT>  <identifier>` |
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
| claimed by, `--mine` | `claimed:<SEAT>` label plus a `CLAIMED` comment (see Claims).  The shared Fleet app cannot tell seats apart, so the label and the tag carry the seat. |
| reported or touched by | `seat:<SEAT>` label, attribution only.  The import put it on reporters and claimants alike, and `claim` adds the claimant's.  It is never read as a claim and never removed. |
| `--by`, authorship | The first line of every issue and comment is the seat tag `[SEAT·session8]` (the session from `--session` or `CLAUDE_CODE_SESSION_ID`). |
| `--where`, `--env`, `--session` on comments | A `Where:` line and a `Sessions:` line at the end of the comment |
| `--uid` | The issue id is a UUID derived from the routed team, any fallback slug and the uid, so `socratic`, `socratic-trade` and `ST` give the same id.  Before creating, the CLI looks up that id, then an attachment with the uid URL (`https://board.jays.services/uid/<app>/<uid>`, or the uid itself when it is a URL), then an imported issue whose footer says ``External uid: `<uid>` ``. |

## Claims

A claim is a `claimed:<SEAT>` label on the issue, written by `claim` together with the move to In Progress and a `CLAIMED` comment.  There is one holder at a time:  `claim` adds the claimant's label and removes every other `claimed:` label.  `status open` or `backlog` releases the claim (Todo means unclaimed).  Moving to Done, Deployed or In Review keeps the label, which records who addressed it.

`seat:<SEAT>` labels are attribution.  The import (`build_plan.py` `seats()`) put them on every reporter and claimant, so an issue reported by CLAUDE and claimed by CODEX carries both.  In the import plan, 35 of the 159 In Progress issues carry a seat label that is not their claimant, and on Todo issues every seat label is attribution.  The CLI never treats a `seat:` label as a claim, never removes one, and `claim` adds the claimant's so the issue also shows who worked it.

Who holds an issue, in order:

1. The `claimed:` labels.  This is all `list` and `--mine` can see, because they fetch neither description nor comments.
2. For an In Progress issue with none (every imported claim, until the relabel), the provenance footer's `addressed by` line, which `claim` and `show` already have loaded.  It is normalized as the importer did (aliases such as `minimax` to `MM` and `designer` to `BF-DESIGNER`, `dsh`/`deepseek`/`harness` to `CLUTCH`), and a name that is not a seat holds nothing.  All 159 In Progress issues in the import plan carry exactly one such line.
3. Nobody.

`CLAIMED` comments are the audit trail, not a source of truth:  a comment can outlive a release, so the CLI does not infer a holder from one.

### Cutover relabel

`python3 scripts/linear-board/relabel_claims.py` adds `claimed:<SEAT>` to each In Progress and In Review issue that has none, from its footer.  It prints the plan and writes nothing unless run with `--apply` and `LINEAR_BOARD_WRITES=1`.  It only adds labels, skips issues that already have a marker (so it is idempotent and safe to resume, or to cap with `--limit N`), leaves closed issues alone, and uses the same Fleet credentials, rate-limit handling and loopback-only URL rule as the CLI.  The import plan has 169 such issues (159 In Progress, 10 In Review), so one run is about that many `issueUpdate` calls on the shared budget.  It is an owner-approved step:  do not run it before the owner says so.

## Safety

- **Identity.**  One shared Linear app, `Fleet`, by OAuth client credentials (design section 2, phase 1).  The CLI never reads a personal or admin API key from any file or variable, so it cannot act as Jay.  The four write verbs refuse to run without `AGENT_SEAT` and refuse a `--by` that differs from it; reads need no seat.
- **Credentials.**  `~/.secrets/Linear/Fleet.env` holds `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` (the `LINEAR_FLEET_` forms also work).  A cloud seat may set the same two variables in its environment instead.  With neither, every verb exits 4 with `no Fleet credentials`.
- **Token cache.**  `~/.secrets/Linear/cache/Fleet.token`, mode 600, read and written under `flock`.  The CLI mints at most once per 24 hours per host.  On a 401 it re-mints only when the cache still holds the token that failed and the last mint is over a day old, or when the credentials changed (rotation).  `LINEAR_BOARD_REMINT=1` forces one mint.  It never revokes.  Tokens and the client secret are never printed.
- **Scope.**  `read,write,app:assignable,app:mentionable` is a constant in the script.  Asking for a different scope string revokes every token the app holds, so it changes only by a reviewed edit, after pilot gate 1.
- **Endpoints.**  Only `https://api.linear.app/graphql` and `https://api.linear.app/oauth/token`.  The URL overrides accept `http://127.0.0.1:<port>` only (the test mock).  Redirects are refused and proxy variables are ignored.
- **Writes.**  Off unless `LINEAR_BOARD_WRITES=1`, checked before any credential is read or request is sent.
- **Rate limits.**  One shared app means one request budget for the whole fleet.  Every connection asks for an explicit `first:` and follows `endCursor`.  Team, state and label ids are cached for a day.  Linear's rate limit (HTTP 400 with `RATELIMITED`, or 429) is retried with backoff to `X-RateLimit-Requests-Reset`, but never past `LINEAR_BOARD_MAX_WAIT`; past it the CLI exits 5 and says when the budget resets.
- **Idempotence.**  Issues, relations and labels are created with client ids, so a retried request cannot create a second copy.  A failed create is judged by reading the id back, not by Linear's wording.  Comment ids are fixed too:  they come from the issue, seat, verb, a hash of the text and the session (the Central day when there is none), so rerunning a `claim`, `status` or `comment` that stopped part way posts the comment once, and a rerun inside one session or day never posts the same words twice.  The CLI says so on stderr when it skips one.  The same words from a new session, or on another day, post again.
- **Partial writes.**  A verb with several requests (`claim`, `status`, `file`) that gives up part way, on a rate limit or an unreachable Linear, names the steps that already landed instead of saying it was not applied.  `file` prints its `filed <id>` line as soon as the issue exists, before attaching the URL and uid, so the id is never lost.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_SEAT` | required for writes | The seat tag every write carries.  `list`, `show`, `stats` and `whoami` run without it |
| `LINEAR_BOARD_WRITES` | off | `1` enables `file`, `claim`, `status`, `comment` |
| `BOARD_BACKEND` | `linear` | `mac-collab` runs the old board CLI (rollback) |
| `BOARD_MAC_COLLAB_CLI` | `~/apps/mac-collab/board` | The old CLI that `BOARD_BACKEND=mac-collab` runs |
| `LINEAR_FLEET_ENV` | `~/.secrets/Linear/Fleet.env` | Fleet client credentials file (must end in `.env`) |
| `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | unset | Cloud seats only, used when the file has no credentials |
| `LINEAR_FLEET_TOKEN_CACHE` | `~/.secrets/Linear/cache/Fleet.token` | Token cache (mode 600, flock) |
| `LINEAR_FLEET_APP_NAME` | `Fleet` | The app name `whoami` expects |
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

Exit codes:  0 ok, 1 error or not found, 2 usage (including no `AGENT_SEAT` on a write, a `--by` mismatch, a `duplicate` with no target, or a refused URL), 3 claim held by another seat, 4 no Fleet credentials, 5 rate limit outlasted the wait, 6 authentication or `whoami` check failed, 7 a write while writes are off.

## Tests

```bash
python3 scripts/linear-board/test_board.py
```

57 tests, about a minute, stdlib only.  Each test starts a mock of Linear's GraphQL and OAuth endpoints on 127.0.0.1 and runs the CLI as a subprocess with `HOME`, the credentials, the token cache and every cache in a temporary directory, so no test reaches Linear or a real secret.  The mock serves two nodes a page, so every list has to follow the cursor.  It stores descriptions the way Linear hands them back (`[`, `]` and `~` escaped outside code spans, `-` bullets as `*`) and comments as sent, which is what the live workspace showed.  It fails a test when a query names a connection without `first:`, when `issueUpdate` sends `labelIds`, when `issueUpdate` removes a `seat:` label, or when anything writes the Duplicate state directly.  It can rate-limit a single operation, to stop a verb between two of its requests.

Covered:  an imported In Progress issue shaped like UM-1447 (reported by CLAUDE, claimed by CODEX, both `seat:` labelled):  CODEX may claim it, CLAUDE is refused, a steal keeps both attribution labels; footer forms and aliases; reporter-only labels on Todo issues; `--mine`; release on `status open`; the relabel script (dry run, additive, resumable, writes off); a rate limit between `claim`'s update and its comment, between `status`'s, and before `file`'s attachments, with the rerun posting one comment; fixed comment ids; `duplicate` with no target, with a target in `--resolution`, and the forms that are refused; reads without `AGENT_SEAT`; every verb in text and JSON, a description round trip through Linear's markdown escaping, all three id forms plus merged, ambiguous, unknown and newly listed ids, team routing and fallback labels, idempotent `file --uid` (by derived id, by attachment, and by an imported footer), claim refusal, `--steal` with and without `--reason`, status mapping and resolution comments, duplicate by relation, comment pagination, stats and its cache, rate-limit retry and give-up, token caching, the 24-hour mint guard, re-mint after a day or a rotation, missing credentials, writes off, `AGENT_SEAT` on writes and `--by` checks, loopback-only URLs, and the `mac-collab` fallback.

## Not built yet

- The design's counts snapshot (section 5, a scheduled job writing per-team counts) does not exist, so `stats` counts live and caches for 10 minutes.
- Claims use the `claimed:<SEAT>` label, not Linear's delegate field, and not the design's single-select `Claimed By` group (a flat label avoids reusing the names of the existing top-level `seat:` labels, which Linear may refuse as group children).  Delegation and `createAsUser` wait on pilot gates 2 and 4 and per-seat apps.
- Until the relabel runs, `list --mine` misses imported claims (it filters on the label); it never lists an issue a seat only reported.
- `--session` is a `Sessions:` comment line, not the Usage-Monitor attachment the design sketches.
