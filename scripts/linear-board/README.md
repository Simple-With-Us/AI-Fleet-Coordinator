# board (Linear backend)

`scripts/linear-board/board` is the fleet's `board` command rebuilt on Linear, which replaces THE BOARD (owner 2026-10-10).  The design is `docs/protocols/linear-migration.md`, sections 2 (identity), 3 (workspace model), 4 (this CLI) and 6 (migration).

It is one stdlib-only Python 3 file that talks GraphQL with `urllib`.  Any seat can run it from an AI-Fleet-Coordinator checkout with `python3 scripts/linear-board/board`, including cloud seats, the Grok Bot VM and BotFleet containers.

Status: built and tested against a local mock of Linear only.  The Fleet Linear app and its client credentials do not exist yet, so every real call stops with `no Fleet credentials`.  Writes stay off until cutover (see Safety).

## Install

### Before cutover: as `board-linear`

Install it next to the old CLI and leave `~/.local/bin/board` alone.  Install from the repo only, so the live copy cannot drift from the tracked one.

```bash
ln -sf "$HOME/Code/AI-Fleet-Coordinator/scripts/linear-board/board" "$HOME/.local/bin/board-linear"
AGENT_SEAT=CLAUDE board-linear whoami
```

The symlink is resolved, so `board-ids.txt` is found next to the script.  `whoami` must print `viewer: Fleet (app user)` and workspace `simple-with-us`; it exits 6 otherwise.  Read verbs (`list`, `show`, `stats`) and `whoami` work in this mode.  `file`, `claim`, `status` and `comment` refuse with exit 7.

Installing it is a helper other agents are expected to run.  Add an on-demand row to `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` and refresh the Apple Note in the same change.

### At cutover only (do not do this before step 4)

Step 4 of the design swaps `~/.local/bin/board` for this CLI and turns writes on.  A wrapper keeps both in one place:

```bash
cat > "$HOME/.local/bin/board" <<'EOF'
#!/bin/sh
export LINEAR_BOARD_WRITES=1
exec python3 "$HOME/Code/AI-Fleet-Coordinator/scripts/linear-board/board" "$@"
EOF
chmod 755 "$HOME/.local/bin/board"
```

Cloud seats and BotFleet set `LINEAR_BOARD_WRITES=1` in their own environment at the same moment.

### Rollback

Set `BOARD_BACKEND=mac-collab`.  The CLI then runs the old board CLI with the same arguments, before any other check, so the result matches today's behavior exactly.  The old script stays where it is (`~/apps/mac-collab/board`, or the path in `BOARD_MAC_COLLAB_CLI`); nothing is copied.

## Frozen contract

Every verb and flag of `~/apps/mac-collab/board` still works and prints the same lines.  The skills (`board-ops`, `closeout`, `session-start`, `land-lane`, `unstick-pr`) need no change to keep working.

| Verb and flags | Linear operation | Output, and what changed |
|---|---|---|
| `list --app --status --severity --kind --search --mine <SEAT> --limit --json` | `issues` per priority bucket (Urgent, High, Medium, Low, none), newest first, following `endCursor` until `--limit` | `N shown of M matching`, then per row `<id8>  <sev> <status> <app>` + title + `@<seat> <env>`.  New: the identifier (`AFC-123`) ends the first line.  `M` is exact up to `LINEAR_BOARD_COUNT_CAP` (1,000), then `1000+`.  P3 and P4 share Linear's Low priority, so in a list cut by `--limit` they interleave by date. |
| `show <id> --json` | `issue(id:)` with labels, attachments, relations, then every comment page | Same sections (Description, Fix, Resolution, meta, comments).  New meta lines: identifier, url, team, state, labels, duplicate links, the import's provenance footer.  Times are Central, 12-hour. |
| `file --title --app --severity --desc --fix --by --where --env --kind --uid --url` | `issueCreate` in the routed team, Todo, with priority and labels; `attachmentCreate` for `--url` and the uid | `filed <uuid>  (<sev> <app>)  <identifier>`.  A repeat `--uid` prints the existing issue with `(already filed; unchanged)`.  Unlike the board, a repeat does not rewrite the existing issue. |
| `claim <id> --by --where --env --session` | check, then `issueUpdate` (In Progress, `addedLabelIds` `seat:<SEAT>`, `removedLabelIds` other seats and `stale-claim`), then a `CLAIMED` comment | `claimed <id8> by <SEAT> -> in_progress  <identifier>`.  New: refuses (exit 3) when another seat holds an In Progress claim, unless `--steal --reason "..."`. |
| `status <id> <value> --by --resolution --session` | `issueUpdate` to the mapped state, then a comment | `<id8> -> <status>  <identifier>`.  A `--resolution` becomes a `Resolution (<status>): ...` comment; without one the comment is `Status: <status>`, so the seat is on record. |
| `status <id> duplicate --of <id>` (new) | `issueRelationCreate` (type duplicate, `issueId` = this issue, `relatedIssueId` = the canonical one), then a read back | Linear moves the issue to Duplicate itself.  The CLI never writes the Duplicate state, which Linear refuses.  If Linear leaves the issue elsewhere, it warns and names the state. |
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
| claimed by, `--mine` | `seat:<SEAT>` label plus a `CLAIMED` comment.  The shared Fleet app cannot tell seats apart, so the label and the tag carry the seat. |
| `--by`, authorship | The first line of every issue and comment is the seat tag `[SEAT·session8]` (the session from `--session` or `CLAUDE_CODE_SESSION_ID`). |
| `--where`, `--env`, `--session` on comments | A `Where:` line and a `Sessions:` line at the end of the comment |
| `--uid` | The issue id is a UUID derived from the routed team, any fallback slug and the uid, so `socratic`, `socratic-trade` and `ST` give the same id.  Before creating, the CLI looks up that id, then an attachment with the uid URL (`https://board.jays.services/uid/<app>/<uid>`, or the uid itself when it is a URL), then an imported issue whose footer says ``External uid: `<uid>` ``. |

## Safety

- **Identity.**  One shared Linear app, `Fleet`, by OAuth client credentials (design section 2, phase 1).  The CLI never reads a personal or admin API key from any file or variable, so it cannot act as Jay.  It refuses to run without `AGENT_SEAT` and refuses a `--by` that differs from it.
- **Credentials.**  `~/.secrets/Linear/Fleet.env` holds `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` (the `LINEAR_FLEET_` forms also work).  A cloud seat may set the same two variables in its environment instead.  With neither, every verb exits 4 with `no Fleet credentials`.
- **Token cache.**  `~/.secrets/Linear/cache/Fleet.token`, mode 600, read and written under `flock`.  The CLI mints at most once per 24 hours per host.  On a 401 it re-mints only when the cache still holds the token that failed and the last mint is over a day old, or when the credentials changed (rotation).  `LINEAR_BOARD_REMINT=1` forces one mint.  It never revokes.  Tokens and the client secret are never printed.
- **Scope.**  `read,write,app:assignable,app:mentionable` is a constant in the script.  Asking for a different scope string revokes every token the app holds, so it changes only by a reviewed edit, after pilot gate 1.
- **Endpoints.**  Only `https://api.linear.app/graphql` and `https://api.linear.app/oauth/token`.  The URL overrides accept `http://127.0.0.1:<port>` only (the test mock).  Redirects are refused and proxy variables are ignored.
- **Writes.**  Off unless `LINEAR_BOARD_WRITES=1`, checked before any credential is read or request is sent.
- **Rate limits.**  One shared app means one request budget for the whole fleet.  Every connection asks for an explicit `first:` and follows `endCursor`.  Team, state and label ids are cached for a day.  Linear's rate limit (HTTP 400 with `RATELIMITED`, or 429) is retried with backoff to `X-RateLimit-Requests-Reset`, but never past `LINEAR_BOARD_MAX_WAIT`; past it the CLI exits 5 and says when the budget resets.
- **Idempotence.**  Issues, comments, relations and labels are created with client ids, so a retried request cannot create a second copy.  A failed create is judged by reading the id back, not by Linear's wording.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_SEAT` | required | The seat tag every write carries |
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

Exit codes:  0 ok, 1 error or not found, 2 usage (including no `AGENT_SEAT`, a `--by` mismatch, or a refused URL), 3 claim held by another seat, 4 no Fleet credentials, 5 rate limit outlasted the wait, 6 authentication or `whoami` check failed, 7 a write while writes are off.

## Tests

```bash
python3 scripts/linear-board/test_board.py
```

35 tests, about 40 seconds, stdlib only.  Each test starts a mock of Linear's GraphQL and OAuth endpoints on 127.0.0.1 and runs the CLI as a subprocess with `HOME`, the credentials, the token cache and every cache in a temporary directory, so no test reaches Linear or a real secret.  The mock serves two nodes a page, so every list has to follow the cursor.  It fails a test when a query names a connection without `first:`, when `issueUpdate` sends `labelIds`, or when anything writes the Duplicate state directly.

Covered:  every verb in text and JSON, all three id forms plus merged, ambiguous, unknown and newly listed ids, team routing and fallback labels, idempotent `file --uid` (by derived id, by attachment, and by an imported footer), claim refusal, `--steal` with and without `--reason`, status mapping and resolution comments, duplicate by relation, comment pagination, stats and its cache, rate-limit retry and give-up, token caching, the 24-hour mint guard, re-mint after a day or a rotation, missing credentials, writes off, `AGENT_SEAT` and `--by` checks, loopback-only URLs, and the `mac-collab` fallback.

## Not built yet

- The design's counts snapshot (section 5, a scheduled job writing per-team counts) does not exist, so `stats` counts live and caches for 10 minutes.
- Claims use the `seat:<SEAT>` label, not Linear's delegate field.  Delegation and `createAsUser` wait on pilot gates 2 and 4 and per-seat apps.
- `--session` is a `Sessions:` comment line, not the Usage-Monitor attachment the design sketches.
