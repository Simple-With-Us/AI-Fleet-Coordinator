---
name: pickup-seat
description: >-
  Pick up a capped-out or abandoned peer seat's in-flight work (owner-directed only). Inventory THE BOARD, effort logs, PRs, dirty worktrees, and Zulip; claim; adopt uncommitted work with authorship credit; disposition each item; hand back. Use when the owner says a seat hit a usage cap, died mid-task, or "take over X's lanes."
---

# Pick up a seat (MM)

> **This install is for `MM`.**  Chat tag `[MM·session8]`.  Notes `MiniMax`.  Branches `minimax/`.  Lanes `~/apps/lanes/<Repo>/minimax-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `mm-bot@simplewithus.zulipchat.com`, credential file `~/.secrets/Zulip/MM-zuliprc` (mode 600).  Session tag `[MM·session8]`, and the `agent-sync` CLI writes it for you.

> **Runtime (MiniMax).** MiniMax Code has no global rules file.  The fleet pointer lives in `~/.minimax/memory/user.md` (user memory, injected into every session's system prompt); per-repo `AGENTS.md` is project memory.  Skills here are loaded on demand from `<available_skills>`, so read the one that matches before acting — nothing in this directory is auto-applied.  `config.yaml` ships `permissionMode: bypassPermissions`, so nothing prompts: hold the destructive-op pause yourself.


Owner-directed only.  Do not initiate a raid on a live peer.

You are **MM**.  Keep `minimax/` branches.  If you continue a peer's `claude/` or `grok/` branch, say so in the work topic and do not rebrand their prefix as yours unless you are opening a new follow-up branch.

## INVENTORY

```bash
board list --status in_progress --limit 50
board list --mine <THEIR_TAG> --status open,in_progress

# Live boards (skip none — pickup is often cross-app)
rg -n "In Progress" /Users/jay/apps/*EFFORT-LOG.md

gh pr list --state open --json number,title,author,mergeStateStatus,autoMergeRequest,url

git worktree list
# then git status in each dirty tree you might adopt

git for-each-ref --sort=-committerdate refs/remotes/origin --format='%(committerdate:short) %(refname:short) %(authorname)' | head -30

agent-sync inbox
agent-sync topics --limit 30
recall "<what they were working on>" --limit 5
```

Also read their last claim in the work topic (`agent-sync read --topic "<topic>"`) and any living Apple Note titled `[APP, <Seat>] …`.

## CLAIM

Post repo-first, naming exactly what you are taking:

```bash
agent-sync post --topic "<APP> <board8> <subject>" --to "<Peer Display Name>" $'repo:  <project>  |  CLAIMED\nclaim:  picking up <SEAT> cap — effort + PR #<n>\nclaimed:  <Day, Mon D, YYYY>\nKEEPOUT:  <files glob>'
```

`--to` adds the `@**Name**` that wakes the peer; the bracket label alone wakes nobody.

Put the same claim on THE BOARD (`board claim` or `board comment`) **and** on the live effort board + `docs/EFFORT-LOG.md`.  Live-only rows have been lost before.

Never take over a stale claim silently.  Post your own `CLAIMED` in the same topic so the history shows the handoff.

## ADOPT uncommitted work

Never `reset --hard` / checkout over dirty files.

```bash
git add <files>
git commit -m "Uncommitted work from capped <SEAT> session.

Landed as continuation during cap handoff; authorship credit retained.

Co-Authored-By: <peer's existing trailer>"
```

Match the trailer already in history **per tool**, not a fabricated seat email.  Monet and Claude Code both use `Claude <noreply@anthropic.com>`.  Codex/Grok/Cursor trailers stay those tools' trailers.

Confirm no new commits or work-topic posts from that seat since the cap before you overwrite their narrative.

## DISPOSITION

| State | Action |
|-------|--------|
| Already merged | `git log origin/main`; board row Deployed if prod verified, else Completed |
| Armed + green | babysit; `unstick-pr` if it stalls |
| Committed, not landed | `land-lane` |
| Uncommitted, finished | commit with credit; land or hold |
| Uncommitted, unfinished | complete only if owner-directed; else note and park |
| Claimed, not started | release; work topic + board |
| Genuinely blocked | board comment with reason; escalate P0 |

Do not kill `com.jay.claude-remote-control` while hunting "stuck Claude."  Monet/Renoir/Claude all look like `claude` in `ps`.

## HAND BACK

Answer disambiguation pings fast.  Cede lanes the returning seat re-claims, especially their authored deltas.  Report SHAs, PR numbers, board ids, what is ready to merge.

## CLOSE OUT

`closeout` skill: both effort boards, THE BOARD resolution, `docs/rollouts/YYYY-MM-DD-pickup-<seat>-cap.md`, Apple Note `[APP, MiniMax] pickup <seat> cap`, and a `DONE` post in the work topic.  Correct premature claims in place.  Never delete their row.

## Canon

- `/Users/jay/apps/AGENT-SYNC.md` — handoff, seats, Mac local processes
- `/Users/jay/apps/EFFORT-LOG-PROTOCOL.md`
- Skills: `session-start`, `board-ops`, `land-lane`, `unstick-pr`, `closeout`

## Substitute Agent Directed Closeout `[SUB→ORIGINAL]`

Once you finish taking over a peer agent's work (or reach a clean handoff point):
1. **Post in the work topic addressed to the original seat.**  A Zulip reply is a post to the same channel and topic, so address it with the envelope plus an @-mention — the bracket label alone wakes nobody.  `agent-sync post --topic "<APP> <board8> <subject>" --to "<Original Display Name>"` writes both:
   ```text
   [<YOUR_TAG>·session8→<ORIGINAL_TAG>] @**<Original Display Name>** repo:  <repo>  |  DONE
   task:  <Feature / PR #<num>>
   status:  Completed
   pr:  <repo>#<num>
   notes: <Summary of what was completed, any bugs fixed, or caveats for the original agent to review>
   ```
   `status:` is one of `Completed`, `Deployed`, `Blocked`.
2. **Update Apple Note:** Add a completion section to the original handoff note or publish the final closeout note referencing the adopted branch.
