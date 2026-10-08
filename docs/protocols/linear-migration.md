# [AFC, Claude] Linear Replaces THE BOARD: Migration Design

Drafted Thu, Oct 8, 2026 12:50am.  Revised Thu, Oct 8, 2026 1:32am after a verified review (38 findings: most applied, several narrowed, a few rejected).
Read-only research; nothing in Linear, the board, or any repo was changed.
Legend: [DOC] Linear docs, [SCHEMA] Linear public GraphQL schema, [DB] read-only query of `findings.db`, [INF] inference, [UNV] unverified, settled by a pilot gate (section 6).
Sources: Linear docs and schema copies in `scratchpad/lin/`, plus `/Users/jay/apps/mac-collab/findings.db` (9,629 findings, 2,514 comments).

## 1. Can bots use their Zulip emails for Linear accounts?

**No.  API and CLI seats do not need a mailbox at all.  A seat that must use the Linear UI needs a real mailbox, not a Zulip address.**

- **Zulip bot addresses cannot be used for anything in Linear.**  Invites and every login path deliver a link or code to the address, or need Google, a passkey added after login, or SAML (Enterprise). [DOC login-methods, invite-members]  `claude-bot@simplewithus.zulipchat.com` only authenticates Zulip's API and receives nothing.
- **API and CLI seats get a Linear agent ("app user").**  Each seat has its own OAuth app; its name and icon come from the app settings, and nobody types an email anywhere. [DOC developers/agents]
  - Linear still stores an email value on the app user (`User.email` is non-null [SCHEMA]).  Linear assigns it; the pilot reads it with `viewer { email app }`.
  - Agents are not billable seats. [DOC agents-in-linear]  They cannot sign in, use admin functions or manage users.
- **A seat that needs the Linear UI cannot be an app user**, because app users cannot sign in.  INSTINCT is the one case today: browser-only, it signs in at the board's `/login` (16 board writes in 14 days [DB]).
  - Option A: a paid member account (about $16 a month on Business) on a deliverable alias Jay owns, for example `instinct@<jay's domain>` forwarded by Cloudflare Email Routing.
  - Option B: no Linear login; INSTINCT gets its own app, and its Zulip posts are filed by the listener as that app.  Open decision 7.
- **The Zulip bot email stays a Zulip-only identifier.**  The registry (section 2) ties seat tag, Zulip bot email and Linear app user id together.

## 2. Identity: one Linear agent app per seat

**Interim shared agent (owner 2026-10-08).**  To start sooner, phase 1 may use ONE shared `Fleet` Linear agent app for every seat instead of waiting for the full roster.  Each issue and comment it writes carries the seat tag on its first line (`[CLAUDE·6db9ee77]`) and, where the API allows, `createAsUser` set to the seat name for display.  This matches THE BOARD's current attribution, which is self-reported (`--by SEAT`).  It must never be Jay's account or a connector bound to Jay (that breaks owner verification), and it never replaces per-seat apps permanently:  each seat moves to its own app as Jay creates them, and the CLI switches per seat by config with no data migration.  Under this option the pilot needs two apps (`Fleet`, `Board Import`) plus `Fleet Reader`.

**Rules**
- One OAuth app per active identity.  Never a shared app, Jay's account, Jay's personal API key, the claude.ai Linear connector or Composio.
- Seats never use the Linear MCP's interactive OAuth: it authenticates whoever clicks, which would be Jay.
- Retired seats (MONET, RENOIR, DSH, HARNESS, KIMI) get no app.  Their history keeps its names in the description footer.
- **Enforced, not just stated.**  This Mac's Claude sessions already carry a claude.ai Linear connector (`save_issue`, `save_comment`, `delete_comment`) and Composio execute tools, and both act as Jay.  The step 4 PR adds `permissions.deny` rules for the connector's write tools and for Composio's Linear toolkit in `~/.claude/settings.json`, plus the equivalents for Codex, Cursor and Grok, or Jay disconnects Linear from the claude.ai connectors Code uses.  `board whoami` exits non-zero when the viewer is a human rather than an app, and the session-start skill runs it.  A weekly audit lists issues and comments authored by Jay's user that carry a seat footer.

**Apps Jay creates** (app name = the seat's Zulip bot full name, so @-mentions read the same in both places).  The roster comes from fleet-apps.json plus every identity with real board writes since Sep 24 (comments, filings or claims; the Oct 4 bulk resync excluded) [DB], after alias normalization (section 6).

| Wave | Apps | Credentials live in |
|---|---|---|
| Pilot (2) | `Claude`, `Board Import` | Mac; `Board Import` is temporary |
| After the gates: registered seats (11) | `Codex`, `AG`, `Cursor`, `Grok` (terminal Grok and Grok Build are one seat, GROK, owner 2026-10-08), `Grok Web` (GROK-WEB cloud seat), `Clutch`, `Fx`, `MM`, `Muse Assist`, `Muse Code`, `Grok Bot` | Mac file and/or that seat's cloud env |
| After the gates: Grok Bot personas (4) | `GB-Housekeeper`, `GB-Monitor`, `GB-Compiler`, `GB-Plumber` | Grok Bot VM |
| After the gates: BotFleet roles (11) | `BF-Fixer`, `BF-Director`, `BF-Compiler`, `BF-Designer`, `BF-Publisher`, `BF-Plumber`, `BF-Oracle`, `BF-Monitor`, `BF-Housekeeper`, `BF-Deployer`, `BF-Builder` | BotFleet containers, one role's values per process |
| Service (1) | `Fleet Reader` (`read` only: recall, admin panel, digests, wake poll, backups, rollback replay) | Infisical, then the job or Worker |
| Decision 7 | `Instinct` (option B only) | Mac |
| On first need | `BF-Producer` (no real writes in 14 days) | BotFleet |
| Not until Jay confirms | DOT (3 writes on Oct 7) and FINCH (1 old attribution); neither is in fleet-apps.json | — |

- That is 29 apps, plus 1 for decision 7.  The number of apps allowed per workspace is undocumented [UNV]; ask Linear support before the BF and GB waves.
- Cursor, FX, Grok Bot and Clutch have no real writes under those tags in 14 days; they get apps because they are registered seats, and Clutch inherits HARNESS's live claim.  BF-Oracle, BF-Monitor, BF-Housekeeper, BF-Deployer and BF-Builder have one or two writes each and could move to "on first need" if Jay prefers.
- Registry: a `linear` block per seat in `fleet-apps.json` with `appName`, `appUserId` and `zulipBotEmail`.  No secrets.  It adds GB-* and BF-* identities, which fleet-apps.json lacks today.  The CLI, listener, importer and recall read seat mapping from it.

**How Jay creates each app** (about 2 minutes, from a generated link):
1. Confirm the workspace switcher shows `simplewithus`, because the manifest link is workspace-agnostic. [DOC oauth-app-manifests]
2. Open `https://linear.app/settings/api/applications/new?` with `oauth.client_name`, `developer.name=Simple With Us`, `oauth.client_uri`, `oauth.redirect_uris=http://localhost:3000/oauth/callback` (unused; the docs use this form), `oauth.grant_types=authorization_code&oauth.grant_types=client_credentials` (`authorization_code` is always required), `display.iconUrl` (a hosted 256px or larger image, the same as the Zulip avatar) and `distribution=private`. [DOC]
3. Create the app inside `simplewithus`.  The OAuth doc suggests a separate management workspace so other admins cannot see app settings; Jay is the only admin, and a client-credentials token acts in the workspace that owns the app. [INF; pilot gate]
4. Never run the `actor=app` authorization-code install for these apps.  A client-credentials token is already an app actor token. [DOC]  If an app actor token is first generated through another grant type, the app cannot hold parallel tokens. [DOC oauth-2-0-authentication]
5. Leave webhooks off in phase 1.  Paste `client_id` and `client_secret` into Infisical (below).  Nothing else leaves the page.

**Scopes.**  One string, in one shared module that every caller of a seat app imports: `read,write,app:assignable,app:mentionable`.
- Requesting a token with a different scope string revokes every token that app holds. [DOC]  So the string is fixed in the pilot and never changed casually; dropping `app:mentionable` later would itself be a change.
- Whether `client_credentials` accepts the `app:*` scopes is [UNV]: the docs list them only for the `actor=app` install.  Pilot gate 1 settles it before any other app exists.
- `admin` is impossible with an app actor.  Teams, workflow states, webhooks and integrations stay Jay's job.  `Fleet Reader` uses `read` only.

**Credentials: client credentials stored, tokens minted.**  The long-lived secret per seat is `client_id` plus `client_secret`.  Tokens are minted from them (30 days, no refresh token). [DOC]

| Where | What |
|---|---|
| Infisical (canonical) | A **separate project, "Fleet Linear Seats"**, not "AI Fleet Coordinator": that project's shared automation identity has Admin and is held by hosted services and agent machines (INFISICAL.md), so any holder could mint as any seat.  Keys `LINEAR_<SEAT>_CLIENT_ID` and `LINEAR_<SEAT>_CLIENT_SECRET`, hyphen to underscore (`LINEAR_BF_COMPILER_CLIENT_SECRET`).  One machine identity per runtime, scoped to its seats' paths: the Mac sync helper, the Grok Bot VM, each BotFleet container. |
| Mac seats | `~/.secrets/linear/<seat>.env` (dir 700, file 600), only that seat's two values, written by an on-demand sync helper.  Register the helper in `MAC-LOCAL-PROCESSES.md` and refresh the Apple Note. |
| Cloud seats | `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` in that seat's environment only.  BotFleet passes only the active role's two values into each child process, never the whole container environment. |
| Token cache | `~/.secrets/linear/cache/<seat>.token` on Mac (mode 600; the secret guard already covers `.secrets/`, and the CLI reads the file in-process); Workers KV with a 24h TTL for Workers; a 600-mode file for any other one-shot runtime.  Read, mint and write under `flock` on the seat's cache file.  Mint at most once per 24h per host, re-mint on 401 only if the cached token is still the one that failed.  **Never revoke on mint**: at one token a day per host the 1,000-token cap is unreachable.  `whoami` prints a mint counter. |

- **Why the cache.**  Every CLI call is a new process, and the admin Worker, digests and bots restart often.  Minting per call would reach the 1,000 parallel-token cap within days, and behavior past it is undocumented.  This bends Linear's "mint per run" wording with a bounded cache.
- **Rotation.**  On suspected leak, and every 90 days; on retirement, delete the app.  Rotate in Linear, update Infisical, sync.  Rotation invalidates that app's client-credentials tokens immediately [DOC], so only that seat re-mints.  Revoke (`POST https://api.linear.app/oauth/revoke`) only on rotation or retirement.
- **Mac caveat.**  All Mac seat files sit under one Unix user, so per-seat identity on the Mac is enforced by policy, not isolation.  The CLI refuses to run without `AGENT_SEAT`, loads only that seat's file, and checks `--by` against the credential's seat; `--by` is never the source of identity.

## 3. Workspace model

- **Teams: one public team per app**, key = the fleet-apps.json acronym: ST, CT, UM, CTS, DD, AFC, PS, AR, CL, BF, HH, OPS, CK.
  - Issue ids carry the app (`AFC-123`), replacing `<APP>` and `board8` in Zulip topics and branch names.
  - 13 teams need Business; Basic caps at 5, Free at 2 teams and 250 issues. [DOC pricing]  Only humans are billable.
  - Teams must be public: client-credentials tokens see public teams only. [DOC]
- **Projects** are for cross-team deliverables (this migration, the Zulip cutover), not per app.
- **Unregistered board slugs:** `fleet-infra` → OPS; `harness`, `clutch`, `dsh-runtime` → CK; `usage`, `congress`, `socratic` → their apps; anything else → AFC with label `app:<raw-slug>`.  Moving an issue between teams later changes its identifier (`Issue.previousIdentifiers` [SCHEMA]), so nothing keys on identifiers (section 6).
- **Existing workspace state is [UNV].**  The `LIN-n` linkifier suggests a `LIN` team exists; keep it as the sandbox team.
- **Workflow states**, one template for every team.  Jay sets **Todo as the default state** for new issues, and `file` also passes the Todo `stateId` explicitly.

| Linear state (category) | From board status | Meaning |
|---|---|---|
| Todo (Unstarted) | open | Unclaimed |
| In Progress (Started) | in_progress | Claimed: delegate is set |
| In Review (Started) | addressed (16 rows) | PR open, set by GitHub automation |
| Done (Completed) | completed | Merged |
| Deployed (Completed) | deployed | Verified in production |
| Parked (Backlog) | none today; Zulip `PARKED` | Stopped on purpose |
| Won't Fix (Canceled) | wontfix | |
| Duplicate (Canceled) | duplicate | Plus a duplicate relation to the kept issue |

  Triage stays off in phase 1; whether app users count as team members is [UNV].
- **Priority:** P0 Urgent, P1 High, P2 Medium, P3 and P4 Low; the exact value is also a `Severity` label.
- **Workspace label groups:** `Severity` (P0 to P4), `Kind` (agent-report, review-finding, sentry; effort-row and github-issue for imports), `Env` (Mac, Cloud), plus `stale-claim` and `imported`.  `surface`, `category`, `repo` and `location` go to the description footer.
- **Claims.**  The actor is the seat's app user for creating, commenting and delegation.
  - **Claim = `delegate` set to the seat, plus a `CLAIMED` comment.**  The human `assignee` stays empty or Jay. [DOC agents-in-linear]  This depends on pilot gate 2.
  - **Fallback, kept behind a CLI flag:** a single-select `Claimed By` label group with `seat:<tag>`, plus the comment; wake polling filters by label instead of delegate.
- **Sessions.**  The board's `session_ids` is already dead (the server drops it).  `--session <id>` becomes an attachment whose URL is the Usage-Monitor cost link for that session. [INF]

## 4. The agent-facing CLI

- **Shape.**  Single-file, stdlib-only Python 3 (urllib plus GraphQL), tracked at AFC `scripts/linear-board/board`, runnable from any AFC checkout (`python3 scripts/linear-board/board`) so cloud seats, the Grok Bot VM and BotFleet containers can use it.
  - Steps 0 to 3: installed on the Mac as **`board-linear`** (`whoami` and read verbs only).  Step 4 swaps `~/.local/bin/board` to it and enables writes.
  - Install from the repo only; this ends the live-versus-tracked drift (77-line diff today).
- **Frozen contract.**  Every verb and flag of today's `~/apps/mac-collab/board` keeps working, tested against the examples in the skills:
  - `list --app --status --severity --kind --search --mine <SEAT> --limit --json`
  - `file --title --app --severity --desc --fix --by --where --env --kind --uid --url`
  - `claim <id> --by --where --env --session`; `status <id> <value> --by --resolution --session`
  - `comment <id> --by --text --where --env --session`; `show <id> --json`; `stats`
  - New: `whoami`, `status ... --of <id>` for duplicates, `--steal --reason` on `claim`.
  - `--app` accepts both board slugs (`socratic-trade`; default `fleet-infra` → OPS) and team keys.
- **Ids.**  Accepts `AFC-123`, a full board id, or an 8-or-more-character prefix.  Imported issues keep the board id as their Linear UUID (section 6), so a full id resolves with `issue(id:)` directly.  Prefixes resolve through `board-ids.txt` (imported full ids, generated at import, committed to AFC).

| Verb | Linear operation |
|---|---|
| `list` | `issues` filtered by team, state, priority or label, delegate for `--mine`; always an explicit `first:` |
| `show` | Issue plus comments and attachments |
| `file` | `issueCreate` in the app's team with the Todo `stateId`; `--uid` upserts (below) |
| `claim` | Refuse if another seat is delegate and the issue is In Progress, unless `--steal --reason`; else set delegate and In Progress, comment `CLAIMED` with the where text |
| `comment` | `commentCreate` as the app; `--where` and `--env` become a footer line |
| `status` | State change; the resolution becomes a `Resolution:` comment; `--of` adds a duplicate relation |
| `stats` | Reads the counts snapshot (section 5), not live pagination |
| `whoami` | `viewer { id name app }`; fails if not an app user or not this seat |

- **Upsert on `--uid`.**  Replaces the board's unique (app, external_uid).  Every uid, URL or not, maps to an attachment URL: the URL itself when it is one, otherwise `https://board.jays.services/uid/<app-slug>/<urlencoded uid>` (covers `effort-<sha>`, `issue-<owner>/<repo>-<n>`, `botfleet-audit:...`, `web-a11y/...`, `zulip:...`).  `file` checks `attachmentsForURL` first.  The new issue's id is a v4-shaped hash of (app, uid), so a crash between create and attach makes the retry fail as "already exists" rather than duplicate.
- **Lost on purpose.**  The 20-minute server-side claim lease becomes check-then-set, a small race at fleet volume.
- **Errors.**  Linear signals rate limiting as HTTP 400 with `RATELIMITED` in `errors[].extensions.code`, not 429. [DOC rate-limiting]  The CLI parses the body and backs off to `X-RateLimit-Requests-Reset`.
- **Output.**  The same text lines as today, plus `--json`.  `lane.py` and `request-mac-seat.sh` already accept `AFC-123`.

## 5. Integrations

- **GitHub** [DOC github]
  - The Linear GitHub app requests read and write on actions, code, issues, pull requests and workflows.  Jay installs it on **selected repositories only**: those whose teams use PR automation.
  - Branches become `<seat>/<lin-id>-<slug>` (`claude/afc-123-zulip-cutover`).  **`Fixes AFC-123` when the PR finishes the issue; `Refs AFC-123` only for partial work**, because a non-closing word stops the on-merge status even when the branch carries the id. [DOC]
  - Per-team automation: PR opened → In Review, merged → Done.  Deployed is set by the seat after the production check.  **Automations stay off until step 4**, so merges during dual-run cannot diverge from the board.
  - GitHub Issues sync: one-way GitHub → Linear for new external issues only, enabled at step 4.  Each Linear team syncs with a single repository. [DOC github-to-linear]
- **Zulip**
  - The Linear → Zulip integration posts to a dedicated `#linear` channel.
  - Work topics become `<LIN-ID> <subject>` in 58 characters or fewer.
  - Linkifier pattern `(?P<id>(?:ST|CT|UM|CTS|DD|AFC|PS|AR|CL|BF|HH|OPS|CK)-[0-9]+)`, URL `https://linear.app/simplewithus/issue/{id}`.  It does not collide with the GitHub `ST#n` linkifiers.
  - **Waking a delegated seat, phase 1:** the listener runs one `Fleet Reader` poll a minute, `issues(filter: {delegate: {id: {in: [registry appUserIds]}}, updatedAt: {gt: cursor}}, first: 50)`, routed by delegate id through the registry.  About 60 requests an hour, and no seat secret is loaded to poll.  Linear discourages polling [DOC], so this is phase 1 only.
  - **Phase 2 (decision 5):** one Cloudflare Worker receives an org-level Issue and Comment data-change webhook (Jay creates it) and replaces the poll.  Agent session events come only if pilot gate 9 shows they reach a client-credentials-only app; otherwise they need the `actor=app` install and a re-mint.
  - The listener (`docs/protocols/agent-sync-listener.md`) is a design, not yet built on main, so **there is no wake path at cutover** until it ships.  That is not a regression: the board never woke anyone.  Its `board file --desc --by --url --uid zulip:...` call must target the frozen CLI contract, and its daemon must set `AGENT_SEAT=<seat>` per subprocess so each filing loads one seat's credential.
- **Sentry** [DOC sentry]: one org, public teams.  P0 and P1 alert rules create issues; the Sentry issue resolves when the Linear issue completes.  Assignee sync matches by email, so it never maps to app users.  PagerDuty stays the pager.  Enable after cutover.
- **Recall:** a `linear` source in `scripts/fleet_rag/sources.py`, read through `Fleet Reader`, with doc ids keyed by issue UUID.  Imported issues share the board UUID, so `board/<id>` docs are skipped rather than duplicated.  Also update `build-rag-snapshot.mjs`, `test_sources.py` and the golden sets.
- **Effort-log mirror: retire it** (decision 4).
  - Freeze `~/apps/*-EFFORT-LOG.md` and each repo's `docs/EFFORT-LOG.md` with an "Archived; see Linear team X" header, and disable each app's `effort-issues-sync.yml`.
  - `board-check.sh` ships only in Socratic.Trade; the user-level hook runs it from the current checkout behind `[ -f ... ]`.  ST lanes branched before the change keep the old script and would refuse to push without touching an archived file.  So at step 4 the hook command in `~/.claude/settings.json` (and the Codex and Grok equivalents) skips when `~/.fleet/effort-log-retired` exists; the repo script change is cleanup.
- **Counts, admin panel and tiles**
  - `IssueConnection` has no `totalCount`, and `Team.issueCount` takes no filter. [SCHEMA]  A scheduled job (Worker cron) computes open, in-progress and P0/P1 counts per team every 10 minutes into KV; the admin card and `board stats` read that snapshot in one subrequest.  Drop the `MAC_COLLAB_TOKEN` Worker secret.
  - The admin panel's "The Board" health tile probes `/board`; repoint it to the saved Linear view or drop it.
  - `gb_monitor_pull.py` runs on the Mac, so it reads through `Fleet Reader`, not GB-Monitor's app (a `read`-only token request would revoke every GB-Monitor token).
  - THE BOARD tiles in AFC and Fleet-OPS `public/index.html` and the `board.jays.services` redirect point to a saved Linear view.
- **Backups.**  Linear's workspace CSV export is a manual admin action delivered by email (link expires in 12 hours) and has no comments. [DOC exporting-data]  So the safety net is a nightly `Fleet Reader` API dump (issues, comments, attachments, relations), paced per section 6, written to B2.  Built in step 2, restore rehearsed before step 4, registered always-on in `MAC-LOCAL-PROCESSES.md` with the Apple Note refreshed.  CSV is a manual monthly extra.

## 6. Migration of existing board items

- **The importer reads `findings.db` with `sqlite3 -readonly`**, never the API, because the board server restarted every 10 minutes tonight.  It runs as `Board Import`.
- **What to import** (decision 3):
  - Every `open`, `in_progress` and `addressed` row, whatever its age: 2,810 rows. [DB]  (`addressed` is In Review; 14 of its 16 rows predate Sep 24.)
  - Closed rows with real activity since Sep 24, judged by `created_at`, a comment or `claimed_at`, **not `updated_at`**: 1,125 rows. [DB]  Bulk resyncs rewrote `updated_at` on 2,741 rows at 2:25am Oct 4, and on 698 rows on Oct 7 (639 at 8:35am), so an `updated_at` window would pull 3,518.  Planned total: 3,935, not 6,300.
  - Stale claims (no `addressed_by`, or no comment or claim in 14 days) import as Todo with `stale-claim`.
  - Older rows stay in the read-only archive and recall.
- **GitHub duplicates.**  Repo transfers left two rows per issue (`jaywedgeworth22/...` and `Simple-With-Us/...`): 171 pairs are both live, and 276 pairs touch the import set. [DB]  Normalize the owner to `Simple-With-Us` and the repo name, import one issue per pair with both URLs as attachments, and report the pair count in the pilot.
- **Attribution normalization**, applied to `reported_by`, `addressed_by` and comment authors before resolving app users: strip `@` and brackets; MINIMAX → MM (33 live claims under MINIMAX); MUSE → MA; bare DIRECTOR, COMPILER, FIXER, DESIGNER, PLUMBER, PUBLISHER, PRODUCER, DEPLOYER, MONITOR, HOUSEKEEPER, ORACLE → `BF-<ROLE>`; DSH, DEEPSEEK, HARNESS → `Clutch` as delegate for live claims (the lineage: DSH retired Sep 19, HARNESS Oct 7), footer otherwise.  DSH's apparent 217 recent writes are the Oct 4 resync; its last comment was Sep 15, but it still holds 12 `addressed` rows, and HARNESS holds 1 in-progress claim from Sep 28.  Whatever stays unmapped is reported in the pilot.
- **Field map**

| Board | Linear |
|---|---|
| id | `IssueCreateInput.id` = the board id, hyphenated.  All 9,629 finding ids are already UUID v4. [DB, SCHEMA]  Also `board: <id8>` in the footer. |
| title, description, recommended_fix | Title; description with a `## Recommended fix` section and a footer: source, category, surface, repo, location, reported_by, addressed_by, and the close time for canceled rows |
| app, severity, status, source_kind, env | Team, priority plus `Severity` label, state, `Kind` label, `Env` label |
| created_at | `createdAt` (past only) [SCHEMA] |
| close time (Completed-category rows) | `completedAt`.  **Omitted for Won't Fix and Duplicate**: the schema rejects `completedAt` with an incompatible state, and there is no `canceledAt` input. |
| reported_by | `createAsUser` display name.  The schema limits `createAsUser` to `actor=app` mode; pilot gate 4 checks client-credentials tokens qualify, else the name goes in the footer. |
| addressed_by (live rows) | `delegateId` for the normalized seat's app user; otherwise footer only |
| comments | `commentCreate` with `id` = the board comment id (all 2,514 are UUID v4 [DB]), `createdAt`, `createAsUser` = author, and `location` and `env` as a footer line.  Resolution becomes the last comment. |
| source_url, external_uid | Attachments (the uid rule from section 4) |

- **Idempotence comes from the ids, not from attachments.**  A rerun of an issue or comment fails as "already exists", which the importer treats as success.  `issueBatchCreate` is atomic [SCHEMA], so one duplicate sinks its whole batch: the importer pre-checks each batch with aliased `issue(id:)` reads and falls back to single creates on any batch error.
- **Delta (dual-run and final).**  Two cursors, one on findings changes and one on `comments.created_at` with a small overlap, because adding a board comment never touches `findings.updated_at`.  The importer stores a hash of each row's mapped fields and writes only on change.  Before writing, it compares the issue's Linear `updatedAt` and last actor with its own last write; anything else changed it, so it reports rather than overwrites.
- **Order and limits**
  - Chronological creation, so Linear numbers follow time.
  - Budget: about 80 batch creates for 3,935 issues, up to about 2,500 comments, a few thousand attachments, and the pre-check reads, so roughly 6,000 to 9,000 requests.  At 5,000 an hour per app user that is a **multi-hour paced job**, not one hour.  Rate boosts scale with paid users, so a one-member workspace gets none. [DOC]  The importer reads `X-RateLimit-Requests-Remaining`, sleeps to the reset, and Jay asks Linear support for a temporary import limit, which the docs offer.
- **Noise guards:** Linear → Zulip webhook off; nothing assigned to Jay; GitHub PR automations off until step 4.

| Step | What happens | Who |
|---|---|---|
| 0. Pilot | `Claude` and `Board Import` apps on the sandbox team; run the gates below.  List teams, plan, member count.  **Gate:** Business or higher. | Jay creates 2 apps; CLAUDE runs it |
| 1. Shape | Teams, state template with Todo default, label groups, GitHub app on selected repos (automations off), Sentry, Zulip `#linear` webhook (off) | Jay (admin only) |
| 2. Identities and clients | Seat apps from generated links, then the "Fleet Linear Seats" Infisical project, the Mac sync helper and cloud envs.  Ship the CLI runnable from an AFC checkout; switch BotFleet and Grok Bot from board REST to it behind a flag.  Build the nightly API dump.  Each seat runs `board-linear whoami` in its own runtime. | Jay, then each seat |
| 3. Dual-run, 3 to 7 days | After step 2.  Full import, then the delta every 10 minutes, board → Linear only.  Seats keep using the old `board`; `board-linear` has no write verbs yet.  Restore rehearsal from the dump. | Importer (pm2, always-on during dual-run, registered, retired at cutover) |
| 4. Cutover | **Gate:** every writer seen in the last 14 days (including cloud CODEX and GROK, and every BF and GB identity) passes `whoami` from its runtime; for INSTINCT, option A is live or Jay has accepted report-in-Zulip only; and a dry delta fits the remaining hourly budget.  Then: one `[CLAUDE->FLEET]` post; board write routes return a machine-readable 410 naming the replacement command; final delta; swap `board` to the Linear CLI; merge the skills, protocol, hook and deny-rule PR; stop `mac-collab-sync` and `mac-collab-writeback`, remove them from `mac-process-watch.sh` and from `pm2-ecosystem.config.cjs`, then `pm2 delete` and `pm2 save`; set the effort-log marker and disable the effort-issues workflows; turn on PR automations, one-way GitHub sync, the Zulip webhook and the new linkifier; rotate or delete `Board Import`'s secret. | CLAUDE, Jay for admin toggles |
| 4a. Right after cutover | With sync_board stopped: backfill GitHub issues created since its last pass; post "Moved to AFC-123" on each deduped live GitHub issue (about 636) and close it as not planned, throttled under GitHub's content limit of roughly 500 writes an hour (about 3 hours); link still-open PRs whose bodies cite 8-to-32-character board ids, via `board-ids.txt` and `attachmentLinkGitHubPR`. | CLAUDE |
| 5. Hold, 14 days | Board read-only.  Litestream keeps running.  Recall reads the archived DB. | — |
| 6. Retire, day 30 | Move the `recall` symlink out of `~/apps/mac-collab`.  Either keep the server in files-only mode with one token limited to `/files` (cloud agents read AGENT-SYNC, MAC-LOCAL-PROCESSES and key names there, and `/files` requires a token), or move that hosting first and update the `fleet-sentry-monitor/monitor.py` `/health` probe in the same change.  Final DB snapshot to B2 and Drive, stop litestream, revoke the per-seat `MAC_COLLAB_TOKEN_<SEAT>` write tokens.  Update `MAC-LOCAL-PROCESSES.md`, the Note, the admin panel and DNS. | CLAUDE, Jay for revokes |

**Pilot gates (step 0)**

| # | Test | If it fails |
|---|---|---|
| 1 | Mint with the exact final scope string by `client_credentials` | Find the accepted string and fix the shared constant before any other app exists |
| 2 | `issueUpdate(delegateId: self)` works, `isAssignable` is true, and `Board Import` can set `Claude` as delegate on a team | Claim by the `Claimed By` label fallback |
| 3 | `assigneeId` accepts an app user | Informational |
| 4 | `createAsUser` under a client-credentials token | Author names go to the footer |
| 5 | Delegate and mention with webhooks off: does a session appear and get marked unresponsive? [DOC: sessions are created on mention or delegation] | `claim` and the importer set `externalUrls` (Zulip topic or PR link) through `agentSessionUpdate`, which the docs say prevents that |
| 6 | Create with a hyphenated board id twice; a batch with one duplicate; `completedAt` on a Canceled state | Confirms the idempotence and field-map rules |
| 7 | Create a public team after minting, then write to it from the app | Jay grants new teams to every app at creation |
| 8 | `viewer { id name email app }` | Records the app user's email value |
| 9 | Manifest webhook fields to a throwaway Worker, no install: do agent events arrive? | Phase 2 needs the `actor=app` install and a re-mint |
| 10 | Do aliased mutations in one request count as one request? `searchIssues` with an empty term? | Adjust the budget and the counts job |
| 11 | A branch-linked PR with `Fixes` reaches Done on merge | Fix the automation before step 4 |
| 12 | Duplicate-pair count and unmapped attribution count | Fix the maps before step 3 |

- **Rollback.**  Steps 0 to 3: stop the mirror.  Step 5: set `BOARD_BACKEND=mac-collab` in the CLI, lift the 410s, replay Linear changes since cutover into the board as `Fleet Reader` (issue UUID = board id; new issues filed as `external_uid=linear:<uuid>`), re-enable the pm2 jobs and watch entries.  Closed-as-moved GitHub issues use "not planned", which the replay ignores.  After day 30: restore from the nightly API dump.
- **Docs to rewrite in the step 4 PR:** `AGENT-SYNC.md` § THE BOARD and the triple claim; `EFFORT-LOG-PROTOCOL.md`; `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.fx/AGENTS.md`; the Zulip guide's topic, claim and closeout sections; the listener design; the canonical skills and `fleet_skill_identity.py`, then regenerate and reinstall the per-seat copies.

## 7. Risks and open owner decisions

**Risks**
- **Linear for Agents is a Developer Preview** ("may change before general availability" [DOC developers/agents]), and delegation, app users and the `app:*` scopes are the claim model's foundation.  Commit a schema snapshot and run a nightly introspection check for `delegateId`, `IssueFilter.delegate`, `createAsUser`, `User.app` and `isAssignable`.  The label fallback stays behind a CLI flag.
- **Identity unknowns [UNV]:** gates 1, 2, 4, 5 and 8, plus the app-count cap (Linear support).  If apps are capped, BF and GB apps come on first need; shared apps with `createAsUser` would break the one-identity rule and need Jay's explicit OK.
- **One scope-string slip revokes every token that seat holds.**  Guard: one shared constant, fixed in the pilot.
- **Shared Infisical identity.**  Mitigated by the separate project with per-runtime identities; the Mac's single Unix user remains a policy boundary.
- **Claim atomicity is lost** (server lock → check-then-set).  Acceptable at fleet volume.
- **Admin-only setup.**  Teams, states, webhooks and integrations need Jay; every Jay step is in section 6.
- **Board server instability.**  Mitigated by direct SQLite reads.  `session_ids` is already dropped today.
- **Vendor dependency.**  Mitigated by the nightly API dump and the recall mirror.

**Open owner decisions** (recommended defaults in bold)

1. **Plan and teams.**  **Business with one team per app** (13 teams; only Jay is billable).  Alternative: Basic with one `Fleet` team and an `App` label group.
2. **Bot roster.**  **The 29 apps in section 2:** every registered seat plus every BF role and GB persona with real board writes in 14 days.  BF-Producer on first need; DOT and FINCH only after Jay confirms them; retired seats none.
3. **History depth.**  **Live rows (2,810) plus closed rows created, commented or claimed since Sep 24 (1,125): 3,935 in all.**  Older rows stay in the archive and recall.
4. **Effort logs and GitHub-issue mirroring.**  **Retire the live and repo effort logs and the effort-issues workflows; move and close the live GitHub issues in step 4a; one-way sync for new ones.**
5. **Wake path.**  **Phase 1 polling through `Fleet Reader`; phase 2 one Worker for data-change webhooks**, and agent sessions only if gate 9 allows.
6. **Claim and closeout surfaces.**  **Linear plus Zulip** replaces the triple (board, effort board or issue, Slack).
7. **Seats that need the Linear UI (INSTINCT).**  **Option B: an `Instinct` app with Zulip relay, no member account.**  This default removes INSTINCT's write access at cutover until the listener ships; meanwhile it reports in Zulip only.  Option A, a paid member account on a deliverable alias Jay owns, keeps it writing from day one.
8. **Secrets home.**  **A separate "Fleet Linear Seats" Infisical project with one machine identity per runtime**, rather than a `/linear` folder in "AI Fleet Coordinator".
