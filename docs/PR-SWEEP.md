# PR Sweep

`pr-sweep` lands the open pull requests across the fleet: it finds every open PR, decides which ones need work, launches one agent per PR on whichever platform you run it from, reads the result back from GitHub, repairs what is still open, audits what the workers did and checks production after merges.  It started as a Claude-only Workflow script (the 2026-10-16 and 17 sweeps) and is now a platform-neutral tool: a Python core (`scripts/pr_sweep/`, stdlib plus the `gh` CLI), one rules file, and thin adapters.

| Piece | Where |
|---|---|
| The CLI | `scripts/pr-sweep` (shim) and `scripts/pr_sweep/` (`python3 -m pr_sweep` from `scripts/`) |
| The worker rules, the only copy | `scripts/pr_sweep/rules.md` (printed by `pr-sweep prompt`) |
| Agent command templates | `scripts/pr_sweep/platforms.json` |
| Claude adapter | `scripts/pr_sweep/adapters/claude/pr-sweep.workflow.js` (a Workflow script) |
| Other platforms | `scripts/pr_sweep/adapters/generic/` (recipes, and a copy-paste prompt for chat-only seats) |
| Fleet skill | `pr-sweep` (`docs/fleet-skills/pr-sweep/SKILL.md`, rendered to every seat) |
| Tests | `cd scripts && python3 -m unittest discover -s pr_sweep/tests -t . -v` (no network, no agent CLI) |

## Running It

Run it from a fresh checkout of `origin/main`, never from `~/Code` (that tree lags `main`):

```bash
d=$(~/apps/lane new AI-Fleet-Coordinator pr-sweep-run) && cd "$d"
PS="$PWD/scripts/pr-sweep"
```

`AGENT_SEAT` must be your own seat tag.  `pr-sweep` never guesses it, every worker inherits it, and it never sets `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`.

### Read-only subcommands

| Command | What it prints |
|---|---|
| `$PS inventory [--fleet-only] [--repos A,B] [--owner O]` | Every open PR of Simple-With-Us, jaywedgeworth22 and the registry owner (one GraphQL search per owner), as JSON records: repo, number, head, base, author, labels, draft, mergeStateStatus, reviewDecision, unresolved threads, CI rollup, auto-merge, `fleet` (registered in `fleet-apps.json`). |
| `$PS plan [--chains REPO=N]` | `skip` (draft, `deferred` label, fork head) or `merge`; a model tier per PR (small for Renovate, Dependabot and Actions; frontier for changes requested, five or more threads, or a conflict with red CI); the chains, with a stack (a PR based on another open PR's head) kept together parent first, and the chain count per repo (default 1; Socratic-Trade 3, BotFleet and AI-Fleet-Coordinator 2). |
| `$PS status [O/R#N ...]` | CURRENT facts per PR from GraphQL, one batched call per repo: state, mergeStateStatus, reviewDecision, unresolved thread count, CI rollup, auto-merge, base, human CHANGES_REQUESTED, pending review requests, minutes since the last activity and `fresh`.  `missing` lists any PR without an answer (exit 69). |
| `$PS needs-work [O/R#N ...]` | The repair decision per PR, with the reason.  A stacked PR waits while its parent is open and is retargeted after it merges. |
| `$PS prompt O/R#N [--platform P] [--round N] [--ours ISO]` | The full worker prompt for any agent: the task, the PR snapshot, every rule from `rules.md` filled in for your seat, and the result format.  `--deploy O/R --merged 1,2` prints a deploy check; `--common` the identity, lane, forbidden and writing rules only; `--offline` skips the GitHub read. |
| `$PS audit O/R#N ... [--since ISO] [--allow-dismisser LOGIN]` | The deterministic audit (below). |
| `$PS platforms` | The agent templates. |
| `$PS lanes [--apply]` | Your lanes, through `~/apps/lane prune` (dry run unless `--apply`). |

### The sweep

```bash
$PS run --platform codex --dry-run                   # each decision and the exact command; launches nothing
$PS run --platform codex --apply --auto-approve      # the sweep
```

`run` does, in order: inventory (or the PRs you name), plan, then per chain one PR after another with the chains (so the repos) in parallel, at most `--max-parallel` (4) at once.  Right before each launch it reads that PR's status: no status means nothing is launched and the run ends 69; activity in the last 15 minutes by anyone but this sweep means `in-flight-skipped`; `needs-work` false means nothing to do.  Otherwise it launches the platform's agent with the worker prompt and waits (`--agent-timeout`, 45 minutes).  The outcome is read back from GitHub, never taken from the agent's answer.  A PR that merged gets `lane retire pr<N>-sweep` for its worker's lane.  Repair rounds follow for what still needs work (`--rounds`, 3 in all); a repair worker is told which activity was the sweep's own, so the freshness guard does not stop it.  Then the audit of every PR it touched, and one deploy-check worker per repo that merged something (`--no-deploy` skips them).  Agent output is kept in `~/apps/scratch/<seat>/pr-sweep/<stamp>/` (0700).

### Per platform

| Platform | Command | How the prompt reaches it |
|---|---|---|
| Claude Code | Workflow tool, script `scripts/pr_sweep/adapters/claude/pr-sweep.workflow.js`, `args: {cli: "<PS>", started: "<date -u +%FT%TZ>"}` (optional `prs`, `repos`, `rounds`, `deploy: false`, `prune: "apply"`) | each worker runs `pr-sweep prompt` itself |
| Claude Code (no workflow) | `$PS run --platform claude --apply` | `claude -p`, stdin, `--model` per tier |
| Codex | `$PS run --platform codex --apply --auto-approve` | `codex exec`, stdin |
| Cursor | `$PS run --platform cursor --apply --auto-approve` | `cursor-agent -p --output-format text <prompt>` |
| Grok (terminal) | `$PS run --platform grok --apply --auto-approve` | `grok --prompt-file <0600 file>` |
| Antigravity | `$PS run --platform antigravity --apply` | `agy -p <prompt>` |
| MiniMax Code | `$PS run --platform minimax --apply` | `mcode exec --input -`, stdin |
| Muse Code | `$PS run --platform muse --apply` | `muse exec <prompt>` |
| FX | `$PS run --platform fx --apply` | `fx ask <prompt>` |
| OpenCode | `$PS run --platform opencode --apply` | `opencode run <prompt>` |
| Anything else | `$PS run --platform codex --apply --agent-cmd 'tool run {prompt}'` | `{prompt}`, `{prompt_file}`, or stdin |
| Clutch, Grok web or iOS, Jet | `adapters/generic/chat-prompt.md`: paste `$PS prompt <pr>` into a session with a terminal tool | by hand |

`--auto-approve` adds each CLI's own auto-approve flags from `platforms.json` (`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`, `--force`, `--always-approve`, `--permission full`, `--yolo`).  Without them a headless CLI may stop at its first permission prompt.  Using them is the operator's decision, per run.  The `checked` field of each template says what was verified on this Mac from the CLI's own `--help` on Sat, Oct 17, 2026; the approval flags of `muse exec`, `fx ask` and `opencode run` are marked UNVERIFIED there.

## The Rules

The worker rules are `scripts/pr_sweep/rules.md`, rendered for your seat by `pr-sweep prompt`.  Read them there (`$PS prompt --common`, or any PR with `--offline`); this page only names them, so they cannot drift.  They are: 1 identity (the launching seat), 2 where to work (the worker's own lane, plain push only), 3 read first, 4 the 15-minute freshness guard, 5 conflicts (merge, never rebase or force-push, no markers left), 6 review threads (fix, or a checked reply, before resolving), 7 reviews (and when a stale Kody review may be dismissed), 8 forbidden actions, 9 CI, 10 the owner-needed test, 11 merging (what the labels and the CODEOWNERS gate do and do not stop), 12 one bounded wait, 13 writing, 14 report what is true now and retire the lane.  `## DEPLOY` in the same file is the deploy check.

Owner rulings they carry (2026-10-17 unless noted): merge by default ("deploy it unless it really needs me, for every app"); a hold only for the reasons in rule 10; `do-not-automerge`, `needs-human-merge` and the CODEOWNERS gate stop only auto-merge, so a verified PR may be merged directly (never `--admin`); a stale `kody-ai[bot]` CHANGES_REQUESTED review may be dismissed under rule 7's four conditions and never a human review; every thread gets a real reply before it is resolved; no Codex fix requests (2026-10-11); Renovate major bumps only with green CI and no breaking change that applies; merged PRs get a deploy check.

## Safety Properties

- **Status is computed in code.**  Each PR is asked for under the GraphQL alias `p<number>` and mapped back by the numbers requested, never by names an agent returns.  A PR without an answer is an error that stops the launch for it; it is never read as "needs work".  (The first sweep's LLM status step silently missed, every PR was reworked each round, and 203 agents ran.)
- **Read-only core.**  Everything but `run --apply` and `lanes --apply` only reads: `gh api graphql` refuses a mutation document, and REST calls are GET.  The workers write, under the rules.
- **Freshness guard.**  A PR with activity in the last 15 minutes by anyone else is skipped before launch; workers apply the same guard (rule 4).
- **One worker per repo at a time per chain**, stacks parent first, so two workers never rebase the same branch.
- **Seat discipline.**  Workers inherit the launching seat; a retired seat or one with no branch prefix is refused.
- **The answer is GitHub's.**  After each worker, the PR is read back; the report and the deploy list come from that.
- **Lanes are retired** with `lane retire` (and `lane prune`), which keeps anything dirty, unpushed, open or in use (`docs/protocols/lane-map.md`, Retiring A Lane).
- **The audit is deterministic** (`pr-sweep audit`): V1 a force push; V2 a dismissal of any review but `kody-ai[bot]`'s, or by an account not on the allowed list (default `jaywedgeworth22`, the account `gh` runs as); V3 a thread resolved with no reply from anyone but its author and no later commit (GitHub records who resolved a thread, not when, so this errs toward not flagging); V4 a merge with a human CHANGES_REQUESTED, a pending human review request or an unresolved thread; V5 conflict markers on lines the merge commit added to the default branch; V6 a baseline file changed or a test file deleted (warning); V7 CODEOWNERS or a merge-gate or auto-merge workflow changed (warning); V8 a comment by a person asking Codex or an autofix bot to fix something.  Whether a fix or a reply is actually right stays a reviewer's call.

## How It Relates To The Other Tools

| Tool | Relation |
|---|---|
| `unstick-pr`, `codex-triage` skills | The by-hand version of one worker's job.  Use them for one PR; a sweep worker follows the same practice under `rules.md`. |
| `land-lane` skill, `scripts/land.sh` | Landing your own branch.  The sweep lands other seats' open PRs and never runs `land.sh`. |
| `merge-shepherd` (`com.jay.merge-shepherd`, see `MAC-LOCAL-PROCESSES.md`) | Runs `gh pr update-branch` so bot merges retrigger verify.  The sweep neither replaces nor calls it. |
| `scripts/branch-landed.sh` | The squash-safe "did this branch land?" check.  `lane retire` uses the same PR-state evidence. |
| `lane retire` / `lane prune` | Called by the sweep after a merge, and by `pr-sweep lanes`. |
| `fleet_facts.github_prs` | The read-only org census behind the digest and open loops (sanitized for publishing).  The sweep keeps its own inventory because it needs base, head, stack and author data. |
| `fleet_facts.activity` | The idle definition for Linear claims (36 and 48 hours).  The sweep's 15-minute guard is a different, much shorter question: is someone editing this PR right now. |
| `scripts/kody-deferred-sweep.mjs` | Ranks deferred Kody issues, not PRs. |
| `deploy-verify` skill | What each deploy-check worker follows. |

## Not Done Yet

- Platform templates whose approval flags are UNVERIFIED (`muse exec`, `fx ask`, `opencode run`) were read from the top-level help only.
- The Claude adapter needs `args.cli`: a Workflow script cannot run commands itself, so a cheap agent runs each `pr-sweep` command and the script parses its one-line JSON, checking that it answers exactly the PRs it asked about.
