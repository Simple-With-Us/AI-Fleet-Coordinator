---
name: closeout
description: Finish a Renoir work unit — THE BOARD, effort log, GitHub issue, Zulip, Apple Notes, PR merge state, and Mac-process inventory. Use when a lane is merged, deployed, parked, or handed off. Never silently walk away from In Progress.
---

# Closeout (RENOIR)

> **Retired seat.**  Owner directive 2026-10-07: the seat never opened, and the Renoir Claude account and app are no longer used.  `CLAUDE` is the only Claude seat.  Do not install to `~/.renoir/skills`.  Do not take fleet work as RENOIR.  This catalog copy is inactive.


Start-of-work is a triple claim.  End-of-work is the same three surfaces plus Notes when the owner might ask "what happened?"

## 1. Truth check

- Merged to `main`?  Do **not** use commit ancestry after a squash-merge (`git merge-base --is-ancestor` lies because squash rewrites SHAs and `delete_branch_on_merge` removes the remote).  Use PR state, then a three-dot diff:

```bash
gh pr list --head "$BRANCH" --state all --json number,state,mergedAt,url
git fetch origin
git diff origin/main...HEAD    # three-dot: remaining unique work vs merge-base
# helper: scripts/branch-landed.sh [repo] [branch]
```

A two-dot `git diff origin/main HEAD` on a stale lane is actively misleading.  Ancestry is still correct for "does this exact SHA exist on live/main" (deploy-verify).
- Production verified?  (`deploy-verify`)  Completed ≠ Deployed.
- Uncommitted files?  Commit or explicitly report why not (failing tests, secrets, owner hold).

## 2. THE BOARD

```bash
board status <id> completed --resolution "Landed in #<PR>.  <one line>."
# or: deployed — only after health verify
```

If the item is an `effort-row` / `github-issue`, writeback copies that status to the live effort log and GitHub within ~10 min.  Still land `docs/EFFORT-LOG.md` in the app PR.  A comment is useful context; the 15-min grace window keeps sync from clobbering the status you just set.

## 3. Effort board + issues

Live board first, then `docs/EFFORT-LOG.md` in the landing commit.

- **Completed** = merged to main.
- **Deployed** = released and verified (say how).
- Never delete another row.  Correct in place with `(Renoir): …` and the date.

Board and GitHub issues must match.  Prefer landing the mirror so `effort-issues-sync` closes the issue.  If you executed a numbered issue, comment/close it so it is not abandoned.

Cross-app work gets a row on each affected board.

## 4. Zulip

Board first, then the work topic — the same one you claimed in.

```bash
agent-sync post --topic "<APP> <board8> <subject>" $'repo:  <project>  |  DONE\npr: #<n>\nboard: <id>\ngates: <what ran>\nwork: <what landed>'
```

Then resolve the topic when the board item reaches Deployed or Parked:  `agent-sync resolve --topic "<topic>"`.  Resolving renames the topic, so post your last words first.  If production is verified after `DONE`, add one line (`deployed:  verified <Day, Mon D> at 3:15pm`).

Not `@*fleet*` for a normal closeout.  The Slack-era helpers (`slack-sync.sh`, `agent-sync-websocket.py`, `agent-sync-poll.py`) are retired; `agent-sync` replaces them.

## 5. Apple Notes

Substantial work: living Completion note, `--update` in place.  Title `[APP, Renoir] short topic`.  See `apple-notes`.  Cloud sessions: skip Notes, say so, leave the handoff in the PR.

## 6. Mac local processes

If this unit created, loaded, bootout, or retired a LaunchAgent, cron, login item, pm2 KeepAlive job, **or a helper other agents run**: add/update a row on `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` **and** `--update "⭐️ Background Jobs Master List"` in the same change.  Say always-on vs on-demand.  Retire in place; never delete historical rows.

Do not SIGKILL `com.jay.claude-remote-control`.

## 7. Default-off features

If you shipped a flag that is off, reserve a Planned enablement row (ST: also `docs/FEATURE-ENABLEMENT-BACKLOG.md`).

## Parked, not done

If you stop without merge: board stays accurate (`open` or a comment "parked because …"), the work topic says `BLOCKED`/`PARKED` with a `reason:` line, worktree is not dirty with uncommitted finished code.  In Progress after you left is how three agents redo the same slice.

## Fleet recall (every closeout)

If you learned a reusable lesson (gotcha, measured number, owner preference, runbook step), contribute it **now**.  Search first so you corroborate rather than duplicate.

```bash
recall contribute "<one paragraph>" --category lesson --app <slug>
```

or MCP `recall_contribute`.  40–4000 chars, one idea, category `lesson | preference | infrastructure | decision | runbook`.  Optional `url` (board/PR) is provenance, not a gate.  Owner 2026-09-02: this is the highest-yield write path.  Do not paste transcripts or secrets.

## Canon

- `/Users/jay/apps/AGENT-SYNC.md` — triple closeout; Apple Notes; Mac local processes; always-commit
- `/Users/jay/apps/EFFORT-LOG-PROTOCOL.md`
- Skills: `board-ops`, `apple-notes`, `deploy-verify`, `land-lane`

## Living Handoff Morphing to Closeout

Throughout execution, maintain a brief big-picture outline of task state. When the task is complete, this outline naturally becomes your **Closeout Report** by:
1. Marking all milestones as completed with commit/PR references.
2. Replacing in-flight WIP notes with live production deployment verification (`/api/health` 200, build SHA).
3. If closing out work adopted from a peer, posting in that work topic with the envelope `[<SUB_TAG>·session8→<ORIGINAL_TAG>]` and an `@**<Original Seat Display Name>**` mention, so the original seat is actually woken.
