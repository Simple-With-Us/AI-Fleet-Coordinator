---
name: pr-sweep
description: Sweep the open PRs across the fleet repos and land them — inventory, plan, one worker per PR on any platform (Claude, Codex, Cursor, Grok, Antigravity, MiniMax, Muse, FX, OpenCode), status computed in code, repair rounds, a deterministic audit, deploy checks and lane retire.  Use when the owner says sweep, land or merge the open PRs, clear the PR backlog, or asks which PRs are stuck.
---

# PR sweep (CURSOR)

> **Runtime fork (Cursor).**  Local Cursor IDE / Auto on this Mac is `[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, your chat tag is `[GB-<NAME>]` (GB-DIRECTOR, GB-FIXER, GB-DESIGNER, GB-COMPILER, GB-HOUSEKEEPER, GB-PUBLISHER, GB-DEPLOYER, GB-MONITOR, GB-PLUMBER, GB-ORACLE, GB-TRADER, GB-ENGINEER) — not `[GROK-BOT]`, not `[CURSOR]`, and not `[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.


`pr-sweep` (`scripts/pr-sweep` in AI-Fleet-Coordinator, docs `docs/PR-SWEEP.md`) is the fleet's PR sweep for every platform.  The worker rules have ONE home, `scripts/pr_sweep/rules.md`, and `pr-sweep prompt` prints them filled in for a PR and your seat.  Never retype them into a chat or a script; print them.

## 0. Before you start

- Run it from a fresh checkout of `origin/main`, never from `~/Code`: `d=$(~/apps/lane new AI-Fleet-Coordinator pr-sweep-run) && cd "$d"`, then `PS="$PWD/scripts/pr-sweep"`.
- Every PR checkout, the sweep's and each worker's, is a lane made by `lane new`.  On the server that puts it in the lanes root the environment names in `FLEET_LANES_ROOT`, never a clone in `~/.local/state`, a scratchpad or `/tmp`.
- Coordination follows `/Users/jay/apps/FLEET-PROTOCOL.md`: claim the sweep in Linear (`simple-board claim`) and post in its Zulip work topic before you start, and close out there.  The workers never post; the seat that runs the sweep does.
- Your seat comes from `AGENT_SEAT` (see `session-start`).  Every worker the sweep launches inherits it.  `pr-sweep` refuses to run without it.
- The owner rulings the rules carry: merge by default; a hold only for the owner-needed reasons in rule 10; the do-not-automerge and needs-human-merge labels and the CODEOWNERS gate stop only auto-merge; stale Kody reviews may be dismissed under rule 7 (never a human review); a real reply on every thread before it is resolved; no Codex fix requests; merged PRs get a deploy check.  Read them in full with `$PS prompt --common` and `$PS prompt <owner/repo#n> --offline`.

## 1. Look first (read-only)

```bash
$PS inventory --fleet-only      # every open PR of Simple-With-Us and jaywedgeworth22 (JSON)
$PS plan                        # skip drafts, deferred and fork PRs; stacks kept together; chains per repo
$PS needs-work                  # which PRs need a worker now and why; `fresh` = someone touched it in the last 15 min
$PS status Simple-With-Us/BotFleet#1219   # current facts for named PRs
```

A missing status is exit 69 and is never read as "needs work".

## 2. Run the sweep

| Platform | Command |
|---|---|
| Claude Code | Workflow tool with `scripts/pr_sweep/adapters/claude/pr-sweep.workflow.js` (`args.cli` = `$PS`, `args.started` = `date -u +%FT%TZ`) |
| Codex | `$PS run --platform codex --dry-run`, then `--apply --auto-approve` |
| Cursor | `$PS run --platform cursor --apply --auto-approve` |
| Grok (terminal) | `$PS run --platform grok --apply --auto-approve` |
| Antigravity | `$PS run --platform antigravity --apply` |
| MiniMax Code | `$PS run --platform minimax --apply` |
| Muse Code | `$PS run --platform muse --apply` |
| FX | `$PS run --platform fx --apply` |
| OpenCode | `$PS run --platform opencode --apply` |
| Clutch, Grok web, Jet | `scripts/pr_sweep/adapters/generic/chat-prompt.md` (paste `$PS prompt <pr>`) |

Always read the `--dry-run` first: it shows each decision and the exact command, and launches nothing.  `--auto-approve` adds the CLI's own auto-approve flags from `platforms.json`; deciding to use it is yours, per run.  One PR: `$PS run --platform codex --apply Simple-With-Us/BotFleet#1219`.  Limit the load with `--max-parallel 2` and `--repos A,B`.

## 3. After the sweep

```bash
$PS audit <owner/repo#n> ... --since <start time>   # force pushes, dismissals, threads closed with no reply, merges with open requests, conflict markers
$PS lanes                                           # your lanes (lane prune); add --apply to remove the merged ones
```

`run --apply` already re-reads every PR from GitHub, retires the worker lane of each merged PR (`lane retire`), audits what it touched and launches one deploy check per repo that merged (`deploy-verify`).  Report what GitHub says now, not what the workers said.

## How it relates to other tools

- `unstick-pr` and `codex-triage`: the by-hand version of one worker's job (diagnose a stuck PR, triage its threads).  Use them for a single PR; a sweep worker applies the same practice under `rules.md`.
- `land-lane`: landing your OWN branch.  A sweep lands other seats' PRs.
- `merge-shepherd` (`com.jay.merge-shepherd`, every 30 minutes, see `MAC-LOCAL-PROCESSES.md`): runs `gh pr update-branch` so bot merges still retrigger verify.  The sweep neither replaces nor calls it.
- `scripts/branch-landed.sh`: the squash-safe "did this branch land?" check; `lane retire` uses the same PR-state evidence.
- `scripts/kody-deferred-sweep.mjs`: ranks deferred Kody issues, not PRs.
- `fleet_facts.github_prs`: the read-only census behind the digest and open loops.  The sweep has its own, because it needs base, head and stack data.
- The GitHub tools: `git-relay` with the MCP wrapper **GR-MCP** (`git-relay mcp`) is the credential-safe path for the PR writes a sweep makes — `open_pr`, `edit_pr`, `reply_thread`, `comment_pr`, `merge_pr`, `workflow_dispatch` and `resolve_thread` — and `mint_token` is deliberately not exposed over MCP, because it returns a raw credential.  A seat reaches the tools with its own bearer token: ST-MCP shares the same bearer-token arrangement, so a seat may find these tools on its ST-MCP connection, while other seats run GR-MCP directly as its own stdio server — the tool names are the same either way.  **The relay is not deployed yet** (verified Oct 23 2026): there is no daemon, no launchd job and no seat pointed at it, so the sweep's `gh` and shell path is still the one that works, until the owner decides the cutover.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` — Linear and the claim lifecycle; merge requirements
- `docs/PR-SWEEP.md`, `scripts/pr_sweep/rules.md`, `scripts/pr_sweep/platforms.json`
- `docs/protocols/lane-map.md` (Retiring A Lane)
- Skills: `unstick-pr`, `codex-triage`, `land-lane`, `deploy-verify`, `closeout`
