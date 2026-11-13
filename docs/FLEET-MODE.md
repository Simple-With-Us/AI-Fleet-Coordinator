# Fleet mode

The fleet's standing working style, for **every agent on every platform** — Claude, Cursor,
Grok, Codex, Antigravity/Gemini, DeepSeek Harness, MiniMax, and anything added later.  It is
the default, not an opt-in.  The trigger phrases below exist only to remind an agent that has
drifted back into grinding everything inline.

## Trigger phrases — all equivalent

`fleet mode` · `/fleet-mode` · `delegate hard` · `work like Jay` · `spawn and stay free`

Typing any one of them at any agent means: read this file (or your platform's copy) and work
this way for the rest of the session.

- **Every platform** — the `fleet-mode` skill is installed into every seat's skill home by the
  fleet skill installer, so the phrases load it directly. 
- **Any platform without that skill** — paste the block below, or run `~/apps/fleet-mode` to
  print it. 
- **Repos** — the same rule is binding via `AGENTS.md`, which points here.

## The pasteable block

> **Fleet mode.**  Two core points.  First, choose the most economical model that can do each
> task very competently, judged by total tokens to finish the task, never price per token.
> Second, give each worker only the tools and context its task needs.
>
> Delegate by default, even for small work you could obviously do yourself.  Pick the model with
> at least a 90% chance of finishing the task for no more total tokens than a pricier one would
> need.  Below that confidence, go up a tier; hand genuinely hard, money-path, or
> security-subtle work UP to a frontier model even from a cheap session.
>
> Brief each worker thoroughly: exact paths, what you already verified, what you ruled out, the
> exact commands to run, because every fact you withhold it must rediscover at its own cost.
> Give it only the tools it needs; a read-only job gets read-only tools.  Spawn in the
> background and do other useful work while it runs.  When only their results remain, end
> the turn.  Their completion notification starts the turn that collects them.  Never spend
> a turn just checking whether a worker finished.
>
> Carry the work to done.  I often want agents to do entire projects autonomously without any
> input from me, so never cut a turn short, stop early, or end a turn to "stay available" while
> work you can still do remains.  Keep going until that work is done or a decision is truly mine.
>
> The sharp test: if the material is not already in your context, delegate — reading it inline
> costs you again on every later turn.  If it already is, stay inline.
>
> Step in only on evidence: a worker repeating the same failing step 3+ times, or running more
> than twice as long as you predicted.  Take it back or send it the specific thing it is
> missing.

## Delegation has to bottom out

An agent handed a **concrete, bounded, fully-briefed task** is a leaf worker, and a leaf worker
does the task itself.  It does not re-delegate it and return a turn later saying "I'll report
back once they finish" — that is a promise, not a result, and it leaves nobody accountable.

The test is whether you were handed a task or a goal.  **A task** — specific paths, a defined
outcome, the commands named — you do yourself.  **A goal** — open scope, unknown breadth, many
sub-parts you must first discover — you delegate.  A leaf worker may still spawn one child for a
genuinely separable, expensive sub-part; it must never fan out its whole assignment and return
empty.

Whoever delegates owns the result.  When the workers are still running and nothing else is left to do, end the turn so their completion notification starts the turn that collects the result and reports it.  Do not end the turn while you still have work you can do.

## Why this is the standard

The owner often has agents carry whole projects to done without any input from him.  So the
standard is not short turns or staying available: it is to never stop early, and to spend
tokens well while doing so.  Choosing the most economical model that can do each task very
competently, and giving each worker only the tools and context the task needs, is how the
fleet does that.  Background workers are part of it, because they run in parallel and keep
their tool output out of the manager's context.

Same-tier delegation still pays, so "no cheaper model available" is not a reason to stop.  A
sub-agent starts with an empty context, its tool output never inflates the manager's later
turns, and a restricted toolset means fewer schemas re-sent per turn.

It is expected value, not a guarantee.  Some delegations will cost more than doing it inline
would have.  That is priced in and judged on the average.  One overrun is not evidence the
policy is wrong — and the real failure mode is quietly reverting to all-inline afterwards,
because an agent that delegates only when certain delegates almost never.

## Canonical sources

- Full policy: `/Users/jay/apps/FLEET-PROTOCOL.md` § Delegation & model economics
- Skill source: `docs/fleet-skills/fleet-mode/SKILL.md`, installed into every seat's skill home
- Claude Code enforcement hook: `~/.claude/hooks/subagent-economy-pretooluse.py`
- Printer: `~/apps/fleet-mode` (on-demand helper script)

## Related fleet rules (binding)

- **`~/Code/` is for integration trees only** — no new top-level folder may be
  added to `~/Code/` unless it is the integration tree for a brand-new fleet
  app being onboarded.  Per-seat worktrees, lane checkouts, scratch clones,
  and `*-wt-*` directories all belong in `~/apps/`.  See `FLEET-PROTOCOL.md` §
  *"Hard rule: `~/Code/` is for integration trees only"* and the
  `code-main-keeper.sh` `STRAY-WORKTREE` enforcement scan.  Owner ruling
  2026-09-25, all agents, all apps, forever.
