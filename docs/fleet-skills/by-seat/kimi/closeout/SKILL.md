---
name: closeout
description: Finish a Kimi work unit — the Linear issue, the PR link, Zulip, Apple Notes, PR merge state, and Mac-process inventory.  Use when a lane is merged, deployed, parked, or handed off.  Never silently walk away from In Progress.
---

# Closeout (KIMI)

> **Retired seat.**  Owner directive 2026-08-21: do not assign or accept new Kimi work.  Do not start a Kimi session.  Do not take work.  This catalog copy is inactive — do not install to `~/.kimi`.


Start-of-work is a claim on the Linear issue and a post in its Zulip topic.  End-of-work is the same two surfaces, the PR that links the issue, plus Notes when the owner might ask "what happened?"

## 1. Truth check

- Merged to `main`?  Do **not** use commit ancestry after a squash-merge (`git merge-base --is-ancestor` lies because squash rewrites SHAs and `delete_branch_on_merge` removes the remote).  Use PR state, then a three-dot diff:

```bash
gh pr list --head "$BRANCH" --state all --json number,state,mergedAt,url
git fetch origin
git diff origin/main...HEAD    # three-dot: remaining unique work vs merge-base
# helper: scripts/branch-landed.sh [repo] [branch]
```

A two-dot `git diff origin/main HEAD` on a stale lane is actively misleading.  Ancestry is still correct for "does this exact SHA exist on live/main" (deploy-verify).
- Production verified? (`deploy-verify`) Completed ≠ Deployed.
- Uncommitted files?  Commit or explicitly report why not (failing tests, secrets, owner hold).

## 2. Linear

```bash
simple-board status <id> completed --resolution "Landed in #<PR>.  <one line>."
# or: deployed — only after health verify
```

The PR body carried `Fixes <ID>` when the PR finished the issue.  Once Linear's GitHub automation is on, that moves the issue to Done on merge; before then, run the command above.  It is the same move done by hand and is safe to repeat.  A PR that carried only `Refs <ID>` never closes the issue, so run the command yourself, or leave the issue open on purpose.  Writes need your bearer-verified seat (`simple-board whoami`), and `--by` must equal it.

- **Completed** = merged to main.
- **Deployed** = released and verified (say how).
- Correct a wrong status or resolution with a new `simple-board comment` or `simple-board status`, never by editing another seat's comment.

Every imported GitHub issue has a Linear twin; work and close the Linear issue, not the GitHub one.  A new GitHub issue filed after the cutover is copied into Linear one way, by Linear's GitHub sync, as an issue with its own id; close that Linear issue, not the GitHub one.  Cross-app work gets an issue on each affected team, linked in the descriptions.

There is no effort log to update and nothing for writeback to copy.  The effort logs are archived.

## 3. The PR

The PR body names the issue (`Fixes <ID>` or `Refs <ID>`), and the branch is `<seat>/<id>-<slug>`.  Say in the closeout post which of the two you used.

Opening, editing, replying to, resolving and merging a PR all have a credential-safe path: `git-relay`, with the MCP wrapper **GR-MCP** (`git-relay mcp`), which exposes `open_pr`, `edit_pr`, `reply_thread`, `comment_pr`, `merge_pr`, `workflow_dispatch` and `resolve_thread`; `mint_token` is deliberately not exposed over MCP, because it returns a raw credential.  A seat reaches the tools with its own bearer token: ST-MCP shares the same bearer-token arrangement, so a seat may find these tools on its ST-MCP connection, while other seats run GR-MCP directly as its own stdio server — the tool names are the same either way.  **The relay is not deployed yet** (verified Oct 23 2026): there is no daemon, no launchd job and no seat pointed at it, so the interim `gh` and shell path is still the one that works, until the owner decides the cutover.

## 4. Zulip

Linear first, then the work topic — the same one you claimed in.

```bash
simple-chat post --topic "<ID> <subject>" $'repo:  <project>  |  DONE\npr: #<n>\nissue: <ID>\ngates: <what ran>\nwork: <what landed>'
```

Then resolve the topic when the issue reaches Deployed or Parked: `simple-chat resolve --topic "<topic>"`.  Resolving renames the topic, so post your last words first.  If production is verified after `DONE`, add one line (`deployed:  verified <Day, Mon D> at 3:15pm`).

Not a fleet wake (`@**all**`) for a normal closeout.

## 5. Apple Notes

Substantial work: living Completion note, `--update` in place.  Title `[TEAM_KEY, Agent] Topic`.  See `apple-notes`.  Cloud sessions: skip Notes, say so, leave the handoff in the PR.

## 6. Mac local processes

If this unit created, loaded, bootout, or retired a LaunchAgent, cron, login item, pm2 KeepAlive job, **or a helper other agents run**: add/update a row on `/Users/jay/apps/MAC-LOCAL-PROCESSES.md` **and** `--update "⭐️ Background Jobs Master List"` in the same change.  Say always-on vs on-demand.  Retire in place; never delete historical rows.

Do not SIGKILL `com.jay.claude-remote-control`.

## 7. Retire the lane

Once the PR is merged (or closed on purpose) and nothing is left in the lane, remove it from OUTSIDE the lane, so your own shell does not count as a process using it:

```bash
cd ~ && ~/apps/lane retire <slug>    # or the lane's path; --repo <app> if the slug is in two repos
~/apps/lane prune                    # a table of all your lanes; add --apply to remove the removable ones
```

`lane retire` refuses (exit 64, with the reason) a lane that is dirty, has unpushed commits no merged or closed PR holds, has an open PR, or is in use; fix that or leave the lane.  It uses `git worktree remove` without `--force` and `git branch -d`, never `rm -rf` or `-D`.  A parked lane stays.  Rules: `docs/protocols/lane-map.md` (Retiring A Lane).

## 8. Default-off features

If you shipped a flag that is off, reserve a Planned enablement row (ST: also `docs/FEATURE-ENABLEMENT-BACKLOG.md`).

## Parked, not done

If you stop without merge: the issue stays accurate (`simple-board status <id> open` to release it, or `parked`, plus a comment "parked because …"), the work topic says `BLOCKED`/`PARKED` with a `reason:` line, worktree is not dirty with uncommitted finished code.  In Progress after you left is how three agents redo the same slice.

## Fleet recall (every closeout)

If you learned a reusable lesson (gotcha, measured number, owner preference, runbook step), contribute it **now**.  Search first so you corroborate rather than duplicate.

```bash
recall contribute "<one paragraph>" --category lesson --app <slug>
```

or MCP `recall_contribute`.  40–4000 chars, one idea, category `lesson | preference | infrastructure | decision | runbook`.  Optional `url` (Linear issue or PR) is provenance, not a gate.  Owner 2026-09-02: this is the highest-yield write path.  Do not paste transcripts or secrets.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` — Linear and the claim lifecycle; Apple Notes; Mac local processes; always-commit
- `/Users/jay/apps/EFFORT-LOG-PROTOCOL.md` — retired stub: the effort logs are archived
- Skills: `board-ops`, `apple-notes`, `deploy-verify`, `land-lane`

## Living Handoff Morphing to Closeout

Throughout execution, maintain a brief big-picture outline of task state.  When the task is complete, this outline naturally becomes your **Closeout Report** by:
1. Marking all milestones as completed with commit/PR references.
2. Replacing in-flight WIP notes with live production deployment verification (`/api/health` 200, build SHA).
3. If closing out work adopted from a peer, posting in that work topic with the envelope `[<SUB_TAG>·session8→<ORIGINAL_TAG>]` and an `@**<Original Seat Display Name>**` mention, so the original seat is actually woken.
