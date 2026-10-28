---
name: pickup-seat
description: >-
  Pick up a capped-out or abandoned peer seat's in-flight work (owner-directed only).  Inventory Linear, PRs, dirty worktrees, and Zulip; claim; adopt uncommitted work with authorship credit; disposition each item; hand back.  Use when the owner says a seat hit a usage cap, died mid-task, or "take over X's lanes."
---

# Pick up a seat (CURSOR)

> **This install is for `CURSOR`.**  Chat tag `[CURSOR·session8]`.  Notes `Cursor`.  Branches `cursor/`.  Lanes `~/apps/lanes/<Repo>/cursor-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `cursor-bot@simplewithus.zulipchat.com`, credential in Infisical Fleet-OPS `/zulip` (`CURSOR_ZULIP_EMAIL`, `CURSOR_ZULIP_KEY`, `CURSOR_ZULIP_SITE`).  Session tag `[CURSOR·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime fork (Cursor).**  Local Cursor IDE / Auto on this Mac is `[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, your chat tag is `[GB-<NAME>]` (GB-DIRECTOR, GB-FIXER, GB-DESIGNER, GB-COMPILER, GB-HOUSEKEEPER, GB-PUBLISHER, GB-DEPLOYER, GB-MONITOR, GB-PLUMBER, GB-ORACLE, GB-TRADER, GB-ENGINEER) — not `[GROK-BOT]`, not `[CURSOR]`, and not `[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.


Owner-directed only.  Do not initiate a raid on a live peer.

You are **CURSOR**.  Keep `cursor/` branches.  If you continue a peer's `claude/` or `grok/` branch, say so in the work topic and do not rebrand their prefix as yours unless you are opening a new follow-up branch.

## INVENTORY

```bash
simple-board list --status in_progress --limit 50
simple-board list --mine <THEIR_TAG> --status open,in_progress

# Pickup is often cross-app:  list every team, not just one app.  The effort logs are archived,
# so Linear is the whole inventory.  (--mine reads the claimed:<SEAT> label.)

gh pr list --state open --json number,title,author,mergeStateStatus,autoMergeRequest,url

git worktree list
# then git status in each dirty tree you might adopt

git for-each-ref --sort=-committerdate refs/remotes/origin --format='%(committerdate:short) %(refname:short) %(authorname)' | head -30

simple-chat inbox
simple-chat topics --limit 30
recall "<what they were working on>" --limit 5
```

Also read their last claim in the work topic (`simple-chat read --topic "<topic>"`) and any living Apple Note titled `[TEAM_KEY, Agent] …`.

## CLAIM

Post repo-first, naming exactly what you are taking:

```bash
simple-chat post --topic "<ID> <subject>" --to "<Peer Display Name>" $'repo:  <project>  |  CLAIMED\nclaim:  picking up <SEAT> cap — issue + PR #<n>\nclaimed:  <Day, Mon D, YYYY>\nKEEPOUT:  <files glob>'
```

`--to` adds the `@**Name**` that wakes the peer; the bracket label alone wakes nobody.

Put the same claim on the Linear issue.  `simple-board claim <id> ... --steal --reason "picking up <SEAT> cap"` takes the claim over (a plain `claim` exits 3 while the peer holds it), or use `simple-board comment` when you only mean to help.  The effort logs are archived, so there is no second board to update.

Never take over a stale claim silently.  Post your own `CLAIMED` in the same topic so the history shows the handoff.

## ADOPT uncommitted work

Never `reset --hard` / checkout over dirty files.

```bash
git add <files>
git commit -m "Uncommitted work from capped <SEAT> session.

Landed as continuation during cap handoff; authorship credit retained.

Co-Authored-By: <peer's existing trailer>"
```

Match the trailer already in history **per tool**, not a fabricated seat email.  Monet and Claude Code both use `Claude <noreply@anthropic.com>`.  Codex/Grok/Cursor trailers stay those tools' trailers.

Confirm no new commits or work-topic posts from that seat since the cap before you overwrite their narrative.

## DISPOSITION

| State | Action |
|-------|--------|
| Already merged | `git log origin/main`; Linear issue Deployed if prod verified, else Completed |
| Armed + green | babysit; `unstick-pr` if it stalls |
| Committed, not landed | `land-lane` |
| Uncommitted, finished | commit with credit; land or hold |
| Uncommitted, unfinished | complete only if owner-directed; else note and park |
| Claimed, not started | release (`simple-board status <id> open`); work topic + Linear |
| Genuinely blocked | `simple-board comment` with the reason; escalate P0 |

Do not kill `com.jay.claude-remote-control` while hunting "stuck Claude."  Monet/Renoir/Claude all look like `claude` in `ps`.

## HAND BACK

Answer disambiguation pings fast.  Cede lanes the returning seat re-claims, especially their authored deltas.  Report SHAs, PR numbers, issue ids, what is ready to merge.

## CLOSE OUT

`closeout` skill: the Linear resolution, `docs/rollouts/YYYY-MM-DD-pickup-<seat>-cap.md`, Apple Note `[TEAM_KEY, Agent] pickup <seat> cap`, and a `DONE` post in the work topic.  Correct premature claims with a new comment or status.  Never delete their comments.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` — handoff, seats, Mac local processes
- `/Users/jay/apps/EFFORT-LOG-PROTOCOL.md` — retired stub
- Skills: `session-start`, `board-ops`, `land-lane`, `unstick-pr`, `closeout`

## Substitute Agent Directed Closeout `[SUB→ORIGINAL]`

Once you finish taking over a peer agent's work (or reach a clean handoff point):
1. **Post in the work topic addressed to the original seat.**  A Zulip reply is a post to the same channel and topic, so address it with the envelope plus an @-mention — the bracket label alone wakes nobody.  `simple-chat post --topic "<ID> <subject>" --to "<Original Display Name>"` writes both:
   ```text
   [<YOUR_TAG>·session8→<ORIGINAL_TAG>] @**<Original Display Name>** repo:  <repo>  |  DONE
   task:  <Feature / PR #<num>>
   status:  Completed
   pr:  <repo>#<num>
   notes: <Summary of what was completed, any bugs fixed, or caveats for the original agent to review>
   ```
   `status:` is one of `Completed`, `Deployed`, `Blocked`.
2. **Update Apple Note:** Add a completion section to the original handoff note or publish the final closeout note referencing the adopted branch.
