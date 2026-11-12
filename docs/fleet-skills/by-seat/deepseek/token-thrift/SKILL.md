---
name: token-thrift
description: Use before any automated, scheduled, webhook or sweep turn, before polling or long waits, and before posting a status update; it keeps fleet token spend down without dropping real work.
---

# Token Thrift (DSH)

> **Retired seat.**  DSH (DeepSeek Harness) is retired (2026-09-19): use `CLUTCH` for `Simple-With-Us/Clutch`.  Do not take work as DSH and do not install this pack to `~/.deepseek/skills`.  This catalog copy is inactive.


Most waste happens before real work starts, in replayed history, no-op wakes, long waits and output no verdict needed.  Cut those first and save the model for work only a model can do.

## 1. One-Shot Wakes

Run classify, gate and UI-pass wakes fresh, with no history replay (BotFleet `oneShotWake`).  Fresh turns are cheaper and carry no stale context, and the median fresh webhook turn is ~115k input vs ~300k on a resumed Compiler thread.  Never continue a forever thread for automation, and roll over when a thread passes ~150k input.

## 2. Filter Before the Model

Drop deliveries that cannot change the verdict before a model sees them, including bot-sender review churn, duplicate (repo, PR, head SHA) wakes and in-progress check runs.  A filter costs no model turn and drops no real work.  86 no-op wakes cost 24.1M input in two days.

## 3. No Canned Status

Post or comment only with a real result, a decision, an approval ask or a breakage, and say it in your own words (Jay, Oct 30).  Do not post "no change" heartbeats on Linear, Zulip or PRs.  Each one costs a full model turn and adds noise for Jay.

## 4. Start a Job, End the Turn

For waits over a few minutes, such as CI, deploys and long builds, start the job, end the turn and let the completion wake you.  Sleeping in a turn burns the time limit without moving the work.  46 turns hit the 900 s ceiling in two days.

## 5. Bounded Tool Output

Read files by range, filter logs with grep or tail and page API results before they reach context.  Never paste whole JSON dumps or full test logs into context.  Output kept small stays out of every later prompt in the thread.

## 6. Memory Is Not a Ledger

Keep MEMORY.md short, and put per-PR "seen" state in a store or a label instead of in memory.  Memory rides every prompt, so ledger entries tax every turn after they are written.  There were 1,165 memory tool calls in two days, and Compiler's MEMORY.md was ~20 KB per prompt.

## 7. Recall Before Re-deriving

Run `recall_search` (`fleet-recall`) before you re-investigate something familiar.  A prior lesson is cheaper and safer than a fresh investigation of the same ground.  After a reusable lesson, contribute one paragraph so the next seat does not pay for it again.

## 8. Right-Size the Model

Use non-thinking cheap models for classify and triage, and keep strong or max effort for merges, conflicts and real debugging.  Small jobs do not gain from top effort, and they are the jobs automation runs most.  Spend and caps are Monitor's lane, not this skill's.

## Checklist Before Ending an Automated Turn

- Drop any wake that cannot change the verdict, and post none of them as status.
- Start long jobs and end the turn instead of polling or sleeping in place.
- Keep tool output, file reads and log pastes bounded to what the verdict needs.
- Post only a real result, decision, approval ask or breakage, in your own words.
- Leave per-PR state in a store or label, and keep MEMORY.md short.
- Check recall was searched before re-deriving, and contribute any reusable lesson found.

Source:  Oracle's BotFleet waste and fix/perf reports (Sep 30 and Oct 6, 2026) and Jay's Oct 30 no-canned-status rule.
