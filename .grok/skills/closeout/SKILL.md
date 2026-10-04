---
name: closeout
description: Finish a Grok work unit — THE BOARD, effort log, GitHub issue, Slack, Apple Notes, PR merge state, and Mac-process inventory. Use when a lane is merged, deployed, parked, or handed off. Never silently walk away from In Progress.
---

# Closeout (GROK)

> **This install is for `GROK`.** Slack `[GROK]`.  Notes `Grok`.  Branches `grok/`.  Worktrees `~/apps/<app>-grok`.  Do not inherit another seat's tag from a shared template.

> **Runtime fork (Grok).** Mac Grok TUI / CLI is `[GROK]`.  If this session is **Grok Build**, pin `AGENT_SEAT=GROK-BUILD`, tag `[GROK-BUILD]`, branches `grok-build/`, worktrees `~/apps/<app>-grok-build`.  Grok Bot (Cursor cloud) uses `[GB-<NAME>]` role tags, not this pack and not `[GROK-BOT]`.  Never `[MONET]`.


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
- Never delete another row.  Correct in place with `(Grok): …` and the date.

Board and GitHub issues must match.  Prefer landing the mirror so `effort-issues-sync` closes the issue.  If you executed a numbered issue, comment/close it so it is not abandoned.

Cross-app work gets a row on each affected board.

## 4. Slack

```bash
AGENT_TAG=GROK /Users/jay/apps/agent-sync-websocket.py --post "[GROK] sync-N
repo: <project>
state: DONE
pr: #<n>
board: <id>
work: <what landed>"
```

Not `FLEET` for a normal closeout.

## 5. Apple Notes

Substantial work: living Completion note, `--update` in place.  Title `[APP, Grok] short topic`.  See `apple-notes`.  Cloud sessions: skip Notes, say so, leave the handoff in the PR.

## 6. Mac local processes

If this unit created, loaded, bootout, or retired a LaunchAgent, cron, login item, pm2 KeepAlive job, **or a helper other agents run**: add/update a row on `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` **and** `--update "⭐️ Background Jobs Master List"` in the same change.  Say always-on vs on-demand.  Retire in place; never delete historical rows.

Do not SIGKILL `com.jay.claude-remote-control`.

## 7. Default-off features

If you shipped a flag that is off, reserve a Planned enablement row (ST: also `docs/FEATURE-ENABLEMENT-BACKLOG.md`).

## Parked, not done

If you stop without merge: board stays accurate (`open` or a comment "parked because …"), Slack says BLOCKED/parked, worktree is not dirty with uncommitted finished code.  In Progress after you left is how three agents redo the same slice.

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
3. If closing out work adopted from a peer, posting a direct `[<SUB_TAG>-><ORIGINAL_TAG>]` Slack notification to `#agent-sync`.
