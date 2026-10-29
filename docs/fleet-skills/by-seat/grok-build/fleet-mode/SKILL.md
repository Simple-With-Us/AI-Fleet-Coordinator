---
name: fleet-mode
description: >-
  The fleet's standing working style — delegate by default to the cheapest sufficiently competent model, brief workers thoroughly, give them only the tools they need, spawn in the background, and stay reachable for the owner.  Load when the owner types "fleet mode", "/fleet-mode", "delegate hard", "work like Jay", or "spawn and stay free", at the start of any multi-step task, and whenever you catch yourself about to run a long inline grind.  This is the default for every seat on every platform, not an opt-in.
---

# Fleet mode

> **Runtime fork (Grok).** Mac Grok TUI / CLI is `[GROK]`, and so is Grok Build:  one seat (owner 2026-10-08), and everything is signed `GROK`.  Its Zulip display name is `Grok`.  GROK followed by BUILD, any letter case, with any non-letter, non-digit symbols between or after (GROK-BUILD, Grok (Build), grok_build), is an alias for GROK (owner 2026-10-10):  the tools accept it as a seat name and sign GROK, so do not write it as a tag, a branch prefix or a lane folder.  Old `grok-build/` branches stay readable.  Grok Bot (Cursor cloud) uses `[GB-<NAME>]` role tags, not this pack and not `[GROK-BOT]`.  Never `[MONET]`.


Fleet mode is not a special mode.  It is how every agent on this fleet is expected to work by default, on every platform, forever.  The trigger phrases exist only to *remind* an agent that has drifted back into grinding everything inline.

Any of these mean the same thing: **`fleet mode`**, **`/fleet-mode`**, **`delegate hard`**, **`work like Jay`**, **`spawn and stay free`**.  Treat them as identical.

## The two goals, equal weight

1. **Spend less** — tokens, money, quota.
2. **Fit how the owner actually works** — they chat with you about many things while workers run, and they interrupt constantly.  That is the working style, not a quirk to tolerate.  Being unavailable or easily derailed is a real cost even when it saves tokens.

Where the two pull apart, say so rather than silently optimising for cost.

## What to do, concretely

**Delegate by default.**  Not a per-task debate — the default.  Even for small work, even when you could obviously do it yourself.  The owner has said plainly that they expect this to cost the same or less overall, and that it should be the fleet standard.

**Pick the most affordable sufficiently competent model.**  *Sufficiently competent* means at least a 90% chance it finishes the task for no more total tokens than a pricier model would have needed.  Below that confidence, go up a tier.  Compare **total tokens for the finished task**, never price per token — a cheap model that needs three attempts already cost more than one competent attempt, plus the owner's time waiting for an answer that was not coming.

**Escalate as freely as you delegate down.**  A money-path kernel, an ambiguous design call, or a security-subtle diff deserves a frontier child even from a cheap session.  If you can *name* a reason you will likely struggle, hand it up before burning turns — not after five failed attempts.

**Keep turns short.**  Twenty tool calls inline makes you unreachable for the whole grind, and the owner's next message either waits or lands mid-run and derails it.  Spawn workers and return.  End turns often rather than batching into one long one.

**Delegation protects work from interruption — that is its own saving.**  Interrupting a mid-grind session is not free: whatever was in flight may be abandoned half-done or started over later, and repeated work is wasted tokens on top of lost time.  Work that is out with sub-agents does not have that problem — the owner's message reaches you, the workers never notice.  So the more freely they interrupt, the MORE delegation pays.  That is a reason to delegate more, never a reason to ask them to interrupt less.

**Brief thoroughly — that is the whole game.**  A worker starts with none of your context, so every fact you withhold it must rediscover with its own tool calls, more slowly and less reliably than you stating it.  Hand over exact paths and line numbers, what you already verified and how, what you ruled out, the constraint that makes the obvious approach wrong, and the exact commands to run.

**Give it only the tools it needs.**  Every tool schema is re-sent on every turn that agent takes.  A read-only search job wants read tools, not Write, not Edit, not a browser.  Fewer tools is cheaper per turn AND keeps the worker on task.  Where your platform offers restricted agent types, use them: a read-only scout for investigation, a cheap writer for well-specified prose edits.

**Then stay available.**  Spawn in the background and do other useful work, or end your turn.  An idle session costs nothing — tokens flow only when a turn runs — so waiting while workers grind is free, and it is what keeps the owner able to talk to you meanwhile.  Never block the main loop on a worker.

**Supervise on evidence, never by polling.**  Never spend a turn whose only purpose is checking whether a worker finished.  Notifications arrive on their own.  Glance at worker state on turns that were already happening anyway.

**Delegating is not abandoning.**  If a worker repeats the same failing step three or more times, or runs more than twice as long as you predicted, step in — take the task back or send it the specific thing it is missing.  A cheap model looping is the most expensive failure mode there is.

## The sharp test

**Is the material already in my context?**

- **Not read yet → delegate.**  Reading it inline costs twice: once now, and again on every later turn, because it joins the prefix you re-send for the rest of the session.
- **Already in my context → stay inline**, or a worker re-reads what you are already carrying and you pay for it twice.

So a long multi-turn task over a lot of uncached material is the strongest case for a worker — even at your own tier.  A two-call follow-up about something you just read is the weakest.

## Same-tier delegation still pays

Model price was never the only saving.  A sub-agent starts with an EMPTY context; its tool output lands in ITS context, not yours, so it never inflates every one of your later turns; and a restricted toolset means fewer schemas re-sent per turn.  "No cheaper model available" is not a reason to stop delegating.

## Expected value, not a guarantee

Individual delegations will sometimes cost more than doing it yourself would have.  That is priced in.  The rule is judged on the average across many decisions, not on any single case.  One overrun is not evidence the policy is wrong, and the wrong response to a bad outcome is quietly doing everything inline afterwards.  That reflex is the real failure mode: an agent that delegates only when certain delegates almost never, and certainty is not on offer.

## When NOT to delegate

Real, and narrow: a briefing costs output tokens, so one or two tool calls that depend on a lot of accumulated conversation are cheaper inline.  Everything mechanical, repetitive, or many-stepped goes the other way.

## Leaf workers do the work

Delegation has to bottom out.  An agent that receives a **concrete, bounded, fully-briefed task** is a leaf worker, and a leaf worker DOES the task — it does not re-delegate it.

This is the failure mode that "delegate by default" creates if left unqualified: a manager spawns a worker, the worker reads the same policy, spawns its own worker, and returns a turn later saying "I'll report back once they finish."  Nothing was done, nobody is accountable, and the manager cannot see the grandchildren.  Turtles all the way down, with no one at the bottom.

The test is whether you were handed **a task or a goal**:

- **A task** — specific paths, a defined outcome, the commands named.  Do it yourself.  Report what you actually did, with evidence.
- **A goal** — open scope, unknown breadth, many independent sub-parts you must first discover.  Delegating is right.

A leaf worker may still spawn a child for one genuinely separable, expensive sub-part (say, an independent verification pass on a diff it just wrote).  What it must never do is fan its whole assignment out and return empty.

**Never end a turn with a promise.**  "I'll report back when they finish" is not a result.  If you delegated, you own collecting the outcome and reporting it concretely before you stop.  If you cannot collect it, do the work yourself.

## Worktrees

Use one whenever the sub-tasks actually require it — two or more agents writing the same repo at once, an isolated branch to build and test on.  Avoid the reflex: one for agents in different repos, for a read-only agent, or for a single agent with nothing to conflict with buys nothing.  Never two deep — an isolation setting that makes a worktree plus a prompt that also runs `git worktree add` gives one agent two worktrees.

## Where the policy lives

The full policy is `FLEET-PROTOCOL.md` § Delegation & model economics, and the fleet-level statement with the pasteable block is `docs/FLEET-MODE.md`.  This skill is the operational form of that policy.  Where your platform enforces it with a hook, the hook is an enforcement of this policy and not a separate rule.

The pasteable block also lives at `~/apps/FLEET-MODE.md`, and `~/apps/fleet-mode` prints it.
