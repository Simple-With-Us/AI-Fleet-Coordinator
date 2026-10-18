# Linear Fleet Guide

simple-with-us Linear — how every agent seat files, claims, and closes its work.

- **Workspace:**  [Simple With Us](https://linear.app/simple-with-us), slug `simple-with-us`.  One team per app, ids like `AFC-123`.
- **What this is:**  how a seat uses Linear as the fleet's system of record.  It stands alone:  a seat that has never seen AGENT-SYNC.md can follow it end to end.  The chat it is discussed in is covered by the [Zulip Fleet Guide](zulip-fleet-guide.md).
- **Status:**  canonical for Linear conduct.  Linear replaced THE BOARD (owner 2026-10-10).  The old board is a read-only archive for about 30 days; the effort logs, `mac-collab-sync`, `mac-collab-writeback` and `effort-issues-sync` are retired.
- **Design:**  `docs/protocols/linear-migration.md` (identity, workspace model, the CLI, the cutover, the migration).  This file is the operating manual; that one is the design record.
- **Source:**  `ai-fleet-coordinator` repo, `docs/protocols/linear-fleet-guide.md`.  Published to https://fleetlink.online/linear/linear-fleet-guide.md.  Edit the source, then republish; never edit only the published copy.

## Contents

- [Linear and Zulip in your own identity](#linear-and-zulip-in-your-own-identity)
- [Before You Start](#before-you-start)
- [Tools, in order of preference](#tools-in-order-of-preference)
- [Never Write Through Jay's Connector](#never-write-through-jays-connector)
- [Every Write Shows Your Seat](#every-write-shows-your-seat)
- [The Claim Lifecycle](#the-claim-lifecycle)
- [Status Vocabulary](#status-vocabulary)
- [Labels](#labels)
- [Ids](#ids)
- [Teams](#teams)
- [Credentials](#credentials)
- [When a Tool Fails](#when-a-tool-fails)
- [Changelog](#changelog)

## Linear and Zulip in your own identity

This is the rule the fleet runs on.  It exists because attribution is the whole point:  a record of who did what is only worth anything if nobody can write under someone else's name.

1. **Search before you start.**  Use `linear_list`, or your app token's search.  Look for related or duplicate issues.
2. **Check the issue is real and still open in practice.**  Read it, then look at recent PRs, fleet recall and Sentry to confirm it has not been superseded or already fixed.
3. **Act on what you found.**  If it is stale, superseded or already resolved, comment with your evidence and close it, or mark it a duplicate.  If it is real, claim it under your own identity before you start.  If a related issue already exists, comment on that one instead of opening parallel work.
4. **Use the Agent-Sync `linear_*` tools first** for `linear_whoami`, `linear_list`, `linear_show`, `linear_file`, `linear_claim`, `linear_comment` and `linear_status`.
5. **For anything Agent-Sync cannot do yet, use your own seat's Linear app token** through Linear's API.  That covers search, editing issues, assignees, labels, priority, moving issues between projects, creating or editing projects, cycles, sub-issues, blocking links, attachments, and unclaiming.
6. **Never write to Linear through Jay's Linear connector.**  It always acts as Jay, and it may not be connected at all times, so do not depend on it.  Reads only, when it is there and neither Agent-Sync nor your app token can do the read.
7. **Every write must show your seat's name.**  Filing, comments, claims, status changes, labels, edits, projects, links and attachments, all of them.
8. **If Agent-Sync is rate-limited or failing, switch to your app token, and go back to Agent-Sync once it works again.**

For Zulip, use the Agent-Sync Zulip tools, which post as your own bot:  `zulip_topics` lists threads, `zulip_read_topic` reads one, `zulip_inbox` shows what is new for you, `zulip_post` starts a topic, `zulip_reply` answers in a thread, `zulip_react` adds an emoji, and `zulip_dm_list`, `zulip_dm_read` and `zulip_dm_send` handle direct messages.  Writes name the bot they act as in `identity`, as in `docs/protocols/per-call-identity.md`.  The full tool list, the connection choice and the error codes are in the [Agent-Sync Tools Guide](https://fleetlink.online/agent-sync-tools-guide/) (or call the `guide` tool).  Read the [Zulip Fleet Guide](https://fleetlink.online/zulip/zulip-fleet-guide.md) before posting:  it covers naming, message format, claims, approvals and catch-up after downtime.

## Before You Start

The cost of a duplicate issue is higher than the cost of a search.  In order:

1. `linear_list --search "<terms>"` for the obvious words, then try the app name, the repo name, and a distinctive error string.
2. `linear_show <id>` on anything plausible, and read the comments, not just the title.  A fixed problem usually has a closeout comment.
3. Recent PRs in the repo (`gh pr list --state merged`), because a PR is where the fix actually lands.
4. Fleet recall for a prior ruling or a known cause.
5. Sentry for the error's history.

Only then decide: **claim it**, **close it with evidence**, **mark it duplicate**, or **file a new one**.

When you close or duplicate something, the evidence goes in the comment.  "Already fixed in PR #N, merged Tue, Oct 13" is a closeout; "seems stale" is not.  Duplicate needs the canonical issue named:  `board status <id> duplicate --of <canonical>`, because Linear only makes a Duplicate through a relation.

## Tools, in order of preference

Use the highest one that does the job.  Dropping a level is allowed; silently using a worse one to act as the wrong identity is not.

| # | Tool | Use it for | Identity it writes as |
| --- | --- | --- | --- |
| 1 | Agent-Sync `linear_*` MCP tools | whoami, list, show, file, claim, comment, status | Your seat, stamped by the server |
| 2 | Your seat's Linear app token (`~/.secrets/Linear/<SEAT>.env`) through the API | search, edits, assignees, labels, priority, projects, cycles, sub-issues, blocking links, attachments, unclaiming | Your seat's own app |
| 3 | Jay's Linear connector | **reads only**, and only when 1 and 2 cannot do the read | Jay, so reads only |

On the Mac the `board` CLI wraps level 2 and is the same contract, with the seat stamped by `--by`/`AGENT_SEAT`:

```bash
AGENT_SEAT=MM board whoami
AGENT_SEAT=MM board list --search "linkifier" --limit 10
AGENT_SEAT=MM board file --title "..." --app AFC --severity P1 --desc-file /tmp/desc.md --by MM
AGENT_SEAT=MM board claim AFC-313 --by MM --where "Mac" --env Mac
AGENT_SEAT=MM board status AFC-313 completed --resolution "..."
```

`board` takes descriptions and fixes from files, and the files must be mode 600 or 400.  That is deliberate:  it keeps free text out of `argv` and out of shell history.

Read `board --help` and `board <verb> --help` before assuming a flag.  Two breaks from the pre-Linear CLI catch people:

- `status <id> duplicate` requires `--of <id>`.
- The five write verbs (`file`, `claim`, `status`, `comment`, `label`) refuse without `AGENT_SEAT`, and `--by` must match it.

Set `AGENT_SEAT` in the same command as the call:  shell state does not persist between commands here.  Never put `AGENT_SEAT` in a global setting like a coding-agent `env` block —  that would override a launcher's seat for every session and cause `--by` mismatches.

## Never Write Through Jay's Connector

Jay's Linear connector always acts as Jay.  It may also be disconnected at any moment.

- **Reads:** acceptable, and only when Agent-Sync and your own app token cannot do the read.
- **Writes:** never.  Not a comment, not a label, not a status, not an attachment, not a project edit.

This is the Linear half of the standing rule that no agent posts, DMs, reacts, or writes through Jay's account anywhere.  The reason is identical:  a record that says Jay did it, when a bot did it, is worse than no record, because it is confidently wrong.

One exception exists and it is not a connector:  GROK-WEB (Grok on the web and iOS) has no shell, so it uses its own connector acting under Jay's login.  Every Linear comment and issue body it writes starts with `[GROK-WEB·<session>]` on its own first line, and it claims with the `claimed:GROK-WEB` label and a `CLAIMED` comment.  No other seat may do this.

## Every Write Shows Your Seat

Linear's API lets a write hide its author, which is exactly why the fleet forbids it.  On every write:

- The **seat tag line** goes first, `[SEAT·session8]`, on its own line, in an issue body or a comment.  The `agent-sync` tooling writes it for you when you pass `session`.
- The **`claimed:<SEAT>` label** marks an issue you hold, so `board list --mine MM` finds it without a scan of every comment.
- The **`--by` flag** or the MCP tool's seat context names the author.  `--by` that does not match `AGENT_SEAT` is refused.

Two spaces between sentences, in every body and comment, without exception ([FLEET-UI-COPY.md](/Users/jay/apps/FLEET-UI-COPY.md)).

A seat never edits or deletes a peer's issue or comment.  Correct a record by adding a new comment that says what changed and when, for example `2026-10-15 (MM): corrected the branch name from minimax/fleet-guides to minimax/fleet-guides-linear-zulip`.

## The Claim Lifecycle

Claim before you write code, not after.  The claim is what stops two seats from solving the same problem in parallel.

1. **File or find** the issue.
2. **Claim it:**  `board claim <id> --by <SEAT>` or `linear_claim`.  This sets In Progress, adds `claimed:<SEAT>`, and posts a `CLAIMED` comment.  An issue another seat holds is refused; stealing needs `--steal --reason "..."`.
3. **Branch:**  `<seat-prefix>/<id>-<slug>`, in a lane (never the human's integration tree).  For MM that is `minimax/<id>-<slug>` under `~/apps/lanes/<Repo>/`.
4. **Post the claim in Zulip** in the work topic, named `<ID> <subject>`.  The Linear claim and the chat post go together; the PR links the issue.
5. **Work, keeping the issue accurate.**  If the scope changes, comment on the issue.  A stale issue is worse than no issue.
6. **Land it:**  the PR says `Fixes <ID>` when it finishes the work, or `Refs <ID>` when it is partial.
7. **Close out:**  `board status <id> completed --resolution "..."`, then a `DONE` post in the work topic, then resolve the topic.

**Completed** means merged.  **Deployed** means verified in production.  They are different states, and conflating them is how a fleet convinces itself something shipped that did not.

Release the claim when you stop:  `board status <id> open` removes `claimed:<SEAT>`.  Park work rather than abandoning it, so the next seat can see it exists.

## Status Vocabulary

`board status <id> <value>`:

| Board value | Linear state | Use it for |
| --- | --- | --- |
| `open` | Todo | Released, unclaimed, or ready to pick up |
| `in_progress` | In Progress | Claimed via `board claim` (use `claim`, not `status`, to record ownership) |
| `addressed` | In Review | Work done, awaiting merge or review |
| `completed` | Done | Merged |
| `deployed` | Deployed | Verified running in production |
| `wontfix` | Won't Fix | Decided against, with the reason in `--resolution` |
| `duplicate` | Duplicate (by relation) | Another issue owns it; `--of <id>` is required |
| `canceled` | Canceled | Abandoned by an owner decision |
| `parked` | Backlog | Deliberately deferred, still real |

`backlog` is accepted as a status value too.  Closing a status removes `needs-owner` and `ask-capture` automatically.

## Labels

Four flat workspace labels carry meaning across the fleet:  `needs-owner`, `ask-capture`, `fleet-report`, `stale-claim`.  Two others are per-seat:  `claimed:<SEAT>` (the claim) and `seat:<SEAT>` (attribution, never removed).

```bash
AGENT_SEAT=MM board label add AFC-313 needs-owner --by MM
AGENT_SEAT=MM board list --label stale-claim
```

Adding or removing a flat label posts no comment, so it is safe to automate.  Always set `AGENT_SEAT` on the command.

## Ids

Three forms are accepted everywhere an id is expected, by `board`, by `linear_show`, and by the MCP tools:

- **`AFC-123`**, the Linear identifier.  Use this;  it names the app and survives every migration.
- **32 hex characters**, with or without hyphens.  An imported issue reuses its old board id as its Linear UUID, so pre-cutover references still resolve.
- **8 or more hex characters**, resolved through `board-ids.txt` plus the ids this host has already listed.  Ambiguous prefixes are refused with the candidates.

**A bare 8-hex word is never taken as a duplicate target**:  it may be a commit.

## Teams

One team per app.  `--app` takes a team key (`AFC`, `FL`) or a board slug (`socratic-trade`); `fleet-infra` routes to OPS; an unknown slug goes to AFC with an `app:<slug>` label.  Teams live:  AFC, AR, BF, CC, C, CL, CT, CTS, DD, FL, HH, OPS, PS, ST, SWU, UM.

`fleet-apps.json` is the source of truth for the acronym-to-repo mapping.  Do not infer a repo from an acronym.

## Credentials

Each seat has its own Linear app, named for the seat bot's display name.  Its credentials live in Infisical (AI Fleet Coordinator, `prod`, `/linear`) as `LINEAR_<SEAT>_CLIENT_ID` and `LINEAR_<SEAT>_CLIENT_SECRET`, hyphens as underscores, and the shared app's as `LINEAR_FLEET_CLIENT_*` (owner 2026-10-18, AFC-348).  `~/.secrets/Linear/<SEAT>.env`, mode 600, is only a fallback for a host without the Infisical identity.  Never a personal or admin API key, from any file or variable.

Lookup order:  the `LINEAR_CLIENT_ID`/`LINEAR_CLIENT_SECRET` environment pair (a cloud runtime), then the seat's pair from Infisical, then `<SEAT>.env` beside `Fleet.env`, then `LINEAR_<SEAT>_CLIENT_*` in the environment, then the shared app's pair from Infisical, then `Fleet.env` for a seat that has no app yet.  Infisical is read through the `INFISICAL_AUTOMATION` machine identity:  from the environment (`INFISICAL_AUTOMATION_CLIENT_ID` / `_SECRET`), else an `INFISICAL_TOKEN` in the environment, else `fleet-secrets get`, which on the Mac reads it from Fleet-OPS through its Keychain login.  Without any of them, or when Infisical is unreachable, the CLI says so once and uses the local files.  Over SSH the Mac's Keychain is locked, so there it falls back too unless the identity is exported.

Each app has one minted token, shared through Infisical `/linear` as `LINEAR_<SEAT>_CACHE_TOKEN` (`LINEAR_FLEET_CACHE_TOKEN` for the shared app), so the Mac, the Hetzner server and any other host use the same token instead of minting their own against Linear's one-mint-a-day limit.  The local copy stays in `~/.secrets/Linear/cache/<SEAT>.token`, mode 600, each under its own lock:  a normal run reads only that, Infisical is asked only when the local token is missing, refused, near expiry or due for its day-28 re-mint, and a new mint is written to both.  `board whoami` prints both places.  The hosted Agent-Sync Worker still keeps its own token per seat in that seat's Durable Object.

`board whoami` prints the identity line, the viewer, the workspace, and the credentials path.  Check it before your first write:

```bash
AGENT_SEAT=MM board whoami
```

- The viewer must be your seat's own app (`MiniMax (MM) (app user)` for MM) or Fleet with a `fallback for <SEAT>` notice.
- Exit 6 naming swapped credentials means `<SEAT>.env` holds another app's key.  Fix the file; writes stay refused until you do.
- Exit 3 means the seat did not resolve.  Do not guess a seat.

## When a Tool Fails

- **Agent-Sync rate-limited or erroring:**  use your seat's app token for the work, then return to Agent-Sync when it recovers.  Say in the work topic which tool you used, so the next reader knows the record came by a second path.
- **Writes refused for a missing seat:**  set `AGENT_SEAT` in the same command.  Never put it in a global setting.
- **Claim refused:**  another seat holds it.  Check whether the work is genuinely theirs, then coordinate in the work topic.  `--steal` is a last resort and needs a reason.
- **A record looks wrong:**  do not edit a peer's text.  Comment with the evidence and the date ([Every Write Shows Your Seat](#every-write-shows-your-seat)).

## Changelog

| Version | When | Who | What |
| --- | --- | --- | --- |
| v1 | 2026-10-15 | MiniMax (MM) | First standalone guide, written for Jay alongside the Zulip guide v4 update.  Carries the fleet's Linear-and-Zulip identity rule as its opening section, the search-before-you-start check, the three-level tool order with Jay's connector reads-only, the seat-tag and label requirements on every write, the claim lifecycle, the status vocabulary, ids, teams, credentials, and the failure playbook.  Facts read live Thu, Oct 15, 2026 from `board whoami`, `board --help`, and `scripts/linear-board/README.md`. |

CLUTCH is the canonical app name. Its verified live Linear team key is `C`. `C`, `CLUTCH`, legacy `CK`, `clutch`, `harness`, and `dsh-runtime` all route to that same live team. Prior `CK-n` and `CLUTCH-n` identifiers remain resolvable after the rename.
