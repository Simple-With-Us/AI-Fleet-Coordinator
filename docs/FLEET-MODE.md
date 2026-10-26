# Fleet mode

The fleet's standing working style, for **every agent on every platform** — Claude, Cursor,
Grok, Codex, Antigravity/Gemini, DeepSeek Harness, MiniMax, and anything added later.  It is
the default, not an opt-in.  The trigger phrases below exist only to remind an agent that has
drifted back into grinding everything inline.

## Trigger phrases — all equivalent

`fleet mode` · `/fleet-mode` · `delegate hard` · `work like Jay` · `spawn and stay free`

Typing any one of them at any agent means: read this file (or your platform's copy) and work
this way for the rest of the session.

- **Claude Code** — the `fleet-mode` skill at `~/.claude/skills/fleet-mode/SKILL.md` loads on
  any of the phrases, and `/fleet-mode` invokes it directly.
- **Any other platform** — paste the block below, or run `~/apps/fleet-mode` to print it.
- **Repos** — the same rule is binding via `AGENTS.md`, which points here.

## The pasteable block

> **Fleet mode.**  Two goals, equal weight: spend fewer tokens, and stay reachable for me —
> I chat with you about other things while work runs, and I interrupt constantly.  That is my
> working style, not a quirk to tolerate.
>
> So: delegate by default, even for small work you could obviously do yourself.  Pick the most
> affordable model with at least a 90% chance of finishing the task for no more total tokens
> than a pricier one would need — compare total tokens for the finished task, never price per
> token.  Below that confidence, go up a tier; hand genuinely hard, money-path, or
> security-subtle work UP to a frontier model even from a cheap session.
>
> Brief each worker thoroughly — exact paths, what you already verified, what you ruled out,
> the exact commands to run — because every fact you withhold it must rediscover at its own
> cost.  Give it only the tools it needs; a read-only job gets read-only tools.  Spawn in the
> background and either do other useful work or end your turn.  Never block on a worker, never
> spend a turn just checking whether one finished, and never say "standing by."
>
> The sharp test: if the material is not already in your context, delegate — reading it inline
> costs you again on every later turn.  If it already is, stay inline.
>
> Step in only on evidence: a worker repeating the same failing step 3+ times, or running more
> than twice as long as you predicted.  Take it back or send it the specific thing it is
> missing.

## Delegation has to bottom out

An agent handed a **concrete, bounded, fully-briefed task** is a leaf worker, and a leaf worker
does the task itself.  It does not re-delegate it and return a turn later saying "I'll report
back once they finish" — that is a promise, not a result, and it leaves nobody accountable.

The test is whether you were handed a task or a goal.  **A task** — specific paths, a defined
outcome, the commands named — you do yourself.  **A goal** — open scope, unknown breadth, many
sub-parts you must first discover — you delegate.  A leaf worker may still spawn one child for a
genuinely separable, expensive sub-part; it must never fan out its whole assignment and return
empty.

Whoever delegates owns collecting the result and reporting it concretely before ending the turn.

## Why this is the standard

Delegation protects work from interruption, and that is its own saving.  Interrupting a
mid-grind session is not free — whatever was in flight may be abandoned half-done or redone
later, and repeated work is wasted tokens on top of lost time.  Work that is out with
sub-agents does not have that problem: the owner's message reaches the manager, the workers
never notice and keep going.  **The more freely the owner interrupts, the more delegation
pays.**  That is a reason to delegate more, never a reason to ask him to interrupt less.

Same-tier delegation still pays, so "no cheaper model available" is not a reason to stop.  A
sub-agent starts with an empty context, its tool output never inflates the manager's later
turns, and a restricted toolset means fewer schemas re-sent per turn.

It is expected value, not a guarantee.  Some delegations will cost more than doing it inline
would have.  That is priced in and judged on the average.  One overrun is not evidence the
policy is wrong — and the real failure mode is quietly reverting to all-inline afterwards,
because an agent that delegates only when certain delegates almost never.

## Canonical sources

- Full policy: `/Users/jay/apps/FLEET-PROTOCOL.md` § Delegation & model economics
- Claude Code skill: `~/.claude/skills/fleet-mode/SKILL.md`
- Claude Code enforcement hook: `~/.claude/hooks/subagent-economy-pretooluse.py`
- Printer: `~/apps/fleet-mode` (on-demand helper script)

## Related fleet rules (binding)

- **`~/Code/` is for integration trees only** — no new top-level folder may be
  added to `~/Code/` unless it is the integration tree for a brand-new fleet
  app being onboarded.  Per-seat worktrees, lane checkouts, scratch clones,
  and `*-wt-*` directories all belong in `~/apps/`.  See `FLEET-PROTOCOL.md` §
  *"Hard rule: `~/Code/` is for integration trees only"* and the
  `code-main-keeper.sh` `STRAY-WORKTREE` enforcement scan.  Owner ruling
  2026-09-25, all agents, all apps, forever.
