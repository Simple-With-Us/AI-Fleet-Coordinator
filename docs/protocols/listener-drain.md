# Qualified listener drain procedure (AFC-652)

How to stop and recreate the listener without losing admitted work. This is the only
supported planned-restart shape; it relies on the drain status surface and the
defer-marked pause, both in `simple-chat daemon status` ("drain" block) and
`simple-chat daemon pause --defer`.

Do not improvise a restart outside this procedure. An unplanned stop (SIGKILL, host
loss) has its own bounded residuals, listed at the end; nothing here claims to close
those.

## 1. Arm the drain

```
simple-chat daemon pause --defer
```

The defer marker makes this pause a drain window:

- Wakes whose quiet window ends inside the window RE-ARM into the coalescer instead
  of dropping (the "queued" ledger row already exists; no row per re-arm). An
  ordinary pause keeps its recorded-drop semantics; only the marker defers.
- Broadcast admissions (chat fan-out, DM inbox copies, broadcast audits) park in
  `listener/deferred_broadcasts.json` before their seen-rings are touched, and
  re-drive at boot and on tick once the marker lifts. Only the marker parks them: an
  ordinary pause (no `--defer`) leaves broadcast behavior exactly as it was.
- The Linear projects hook answers new Project events with 503, so Linear keeps
  custody and retries. (Notes/registry POSTs are internal durable state and are not
  gated.)

Keep the window short: messages that only arrive during it wait for the resume.

## 2. Stop ingress

Disable the outgoing webhook in listener.toml and SIGHUP the listener. Verify in
`status` that `drain.outgoing_webhook.listening` is false before continuing: a
partition-refused reload keeps the old config and the old server UP, and the status
field is the only thing that distinguishes that.

## 3. Quiesce - bounded wait, abort never stop

Wait until ALL of the following read true in `status` (ticks every 2s; two snapshots
at least 35s apart to bound observability lag):

- `drain.outgoing_webhook.spool.pending` is 0
- per seat: `seats.X.cursor >= seats.X.webhook_high_water`
- `drain.broadcasts`: wake_handoffs, inbox_pending, audits all 0
- `drain.effect_threads`: chat-fanout, inbox-delivery, inbox-delivery-retry,
  broadcast-audit, broadcast-wake-unsupported all 0
- `drain.armed` is true. An owner DM pause mid-drain overwrites the `"*"` pause entry
  and silently drops the defer marker; `armed` flipping false is how you detect that.
  Re-arm with `pause --defer` before continuing, or the stop is unqualified.
- `drain.coalescer_pending`: under an armed drain, deferred wakes re-arm INTO the
  coalescer and stay nonzero until the resume. A nonzero value is EXPECTED and does
  NOT block the stop - those wakes are durable "queued" ledger rows and recover
  post-restart. (The `drain.armed` check above, not this gauge, covers them.)
- `drain.router_queue` is 0
- `drain.children` is 0
- per seat: `jobs` 0, `running` null
- chat_nudges pending/dispatching 0 ("unknown" nudges are durably held, not
  in-flight; they persist and do not block)
- `drain.linear_projects_queued` is false (when the hook is configured)

The wait is BOUNDED: if the full condition is not met within 15 minutes of arming,
take the Abort path below. If the cursor never reaches the high-water, a native event
was lost by Zulip's queue: reconnect the seat (backfill advances the cursor through
ring dedup) or ABORT. Under sustained native traffic the spool only drains on
webhook-arrival or queue-empty iterations, so a stall can be starvation, not loss -
the abort path covers it. Never stop with the condition unmet.

## Abort (cancel the drain)

Take this path when the 15-minute bound expires, or any check says the window should
not proceed. The abort returns to normal operation WITHOUT a restart:

1. If step 2 already disabled the outgoing webhook: restore the prior listener.toml
   bytes and SIGHUP; verify `drain.outgoing_webhook.listening` matches the PRIOR
   configured state (true only if the restored config enabled it - a deployment whose
   webhook was already off must read false again).
2. Restore the prior pause.json bytes, removing ONLY the drain marker and preserving
   any pre-drain user pause. Do NOT bare `daemon resume`: a blanket resume wipes a
   user pause that predates the drain.
3. Verify recovery against the restored prior state (two snapshots at least 35s
   apart):
   - with no pre-drain user pause restored: deferred wakes fire (the parked re-arms
     finalize once the marker is gone). With a prior pause restored, they stay
     deferred under it - that is their correct behavior, not a failed check.
   - `drain.deferred_broadcasts` drains to 0 (re-drive runs on tick once the marker
     lifts), except rows whose payload fetch stays inconclusive - item 4.
   - the Linear hook answers 200 queued again (only if it was configured and enabled
     in the prior state)
   - ingress healthy per the prior config: cursors advance, spool drains or stays 0
4. A parked effect whose payload proof is inconclusive STAYS parked and visible as a
   nonzero `drain.deferred_broadcasts`. Investigate the named message; never delete
   the file to make the gauge read zero. A row is removed by hand only after the
   operator has confirmed the payload is genuinely gone.

An abandoned marker is loud, not silent: the hook keeps answering 503 and wakes keep
deferring. Supervise the window; if it cannot complete inside the bound, abort.

## 4. Stop and recreate

SIGTERM the listener (its shutdown force-flushes the cursor) and recreate the
service. Do not edit durable state while it is down.

## 5. Restore EXACT prior bytes around boot

The config disable from step 2 edited a persistent file on the /data volume, and the
drain pause sits in pause.json. Both must come back byte-for-byte:

- restore the prior listener.toml bytes (the sample in the repo is NOT the live
  volume's file; seat renames in either direction are load-bearing)
- restore the prior pause.json bytes (including any pre-drain user pause), removing
  the drain marker

Restore BEFORE or AT boot. Wakes queued before the drain defer under a still-present
marker, so ordering is not load-bearing for wake LOSS - but exact prior bytes stay
mandatory for fidelity: the restored pause state must be the one the owner set.

## 6. Post-boot checks

- seats connected, cursors advancing
- `drain.deferred_broadcasts` drains to 0 (re-drive runs at boot and on tick);
  rows whose payload fetch stays inconclusive remain parked and visible
- deferred wakes fire once the marker is gone - unless the restored prior pause.json
  carried a user pause, under which they correctly stay deferred
- the Linear hook accepts new events again (200 queued), if it is configured and
  enabled in the restored prior config

## First adoption (installing this change)

The qualified procedure cannot install itself: the runtime being replaced has no
`--defer` marker, no drain status surface, and no hook gate. The FIRST stop that
adopts this code is an ordinary pre-drain stop, carrying the pre-drain residuals
(paused wakes drop with a recorded row; a Linear event acknowledged 200 and held in
memory is lost on stop and self-heals on the next project event). Keep it small:

1. Pick a quiet window (low fleet traffic) and capture the current listener.toml and
   pause.json bytes.
2. `simple-chat daemon pause` (ordinary pause; drops are recorded, as before this
   change).
3. Wait for the existing work to empty (no coalescer pending, no running jobs, effect
   threads at 0) - minutes, not the 15-minute drain bound.
4. SIGTERM, recreate with the new build, restore the exact prior listener.toml bytes
   and the pause.json bytes captured at step 1 (lifting the adoption pause while
   preserving any earlier user pause), boot.
5. Verify: seats connected, cursors advancing, `simple-chat daemon status` shows the
   "drain" block (armed false).
6. Every stop after this one uses the qualified drain procedure above.

## Named residuals (explicit, accepted)

- An event acknowledged 200 by the Linear hook and held only in memory is lost on an
  UNPLANNED stop; reconcile is read-idempotent and self-heals on the next project
  event. The 503 gate removes the planned-stop case.
- Admission is fenced, a mid-effect crash is not: the deferred-broadcast record marks
  an effect done when its thread STARTS, so an unplanned stop inside a
  fanout/inbox/audit effect can leave it partial (missing nudges for not-yet-enqueued
  chats, a lost in-memory wake hand-off, a lost inbox copy) and it is never
  re-driven. Step 3's quiesce closes this window for the planned stop. What durably
  fences re-drive duplicates is the nudge dedupe key and the ledger trigger-id fence
  (F4), and only when a re-drive happens.
- Re-drive does not re-run the route's admission predicates (including the stale
  exclusions) on the parked message. Parked effects are bounded to the drain window,
  so this is an explicit accept, not an age fence: keep the window short.
- A crash mid-re-drive can re-send a bot its unread inbox copy (duplicate, cosmetic,
  attempt-capped); nudge re-delivery is fenced durably by dedupe key; a double wake
  hand-off is fenced by the ledger trigger-id check (F4).
- Broadcast-audit reminders are lost on an unplanned stop: one unexplained
  broadcast, never a wrong wake (documented upstream).
- SIGKILL/crash can duplicate a seat-INBOX row (append has no id dedup; cosmetic).
- Passive unleased-chatter rows in the router queue at stop are lost, as with any
  crash; every wake/nudge-relevant class is fenced by post-restart backfill.
- The spool can starve under sustained native load and stall step 3 into its abort
  path (safe).
