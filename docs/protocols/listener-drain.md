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
- Broadcast admissions (chat fan-out, DM inbox copies, broadcast audits) are claimed
  atomically against `listener/broadcast_effects.json` - the durable effect ledger -
  before any seen-ring is touched, and park as pending under the marker instead of
  driving. They re-drive at boot and on tick once the marker lifts. Admission is
  durable with or without the marker (a plain crash is fenced the same way); the
  marker only gates DRIVING. An ordinary pause (no `--defer`) leaves broadcast
  driving behavior exactly as it was.
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
- `drain.broadcasts`: wake_handoffs, inbox_pending, audits all 0 (audits counts
  DRIVING audit reminders from the ledger; a pre-window audit due inside the window
  still fires on tick, so wait for it)
- `drain.effect_threads`: chat-fanout, inbox-delivery, inbox-delivery-retry,
  broadcast-audit, broadcast-wake-unsupported all 0
- `drain.armed` is true. An owner DM pause mid-drain overwrites the `"*"` pause entry
  and silently drops the defer marker; `armed` flipping false is how you detect that.
  Re-arm with `pause --defer` before continuing, or the stop is unqualified.
- `drain.coalescer_pending`: under an armed drain, deferred wakes re-arm INTO the
  coalescer and stay nonzero until the resume. A nonzero value is EXPECTED and does
  NOT block the stop - those wakes are durable "queued" ledger rows and recover
  post-restart. (The `drain.armed` check above, not this gauge, covers them.)
- `drain.broadcast_effects`: pending 0 and driving 0 (the durable ledger gauges;
  terminal rows do not block: `broadcast_effects_terminal` names any failed/unknown
  effects for the operator)
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
   - `drain.broadcast_effects.pending` drains to 0 (re-drive runs on tick once the
     marker lifts), except rows whose payload fetch stays inconclusive - item 4.
   - the Linear hook answers 200 queued again (only if it was configured and enabled
     in the prior state)
   - ingress healthy per the prior config: cursors advance, spool drains or stays 0
4. A parked effect whose payload proof is inconclusive STAYS parked and visible as a
   nonzero `drain.broadcast_effects.pending`. Investigate the named message; never delete
   the file to make the gauge read zero. A row is removed by hand only after the
   operator has confirmed the payload is genuinely gone, and only while the listener
   is STOPPED - the file's updates are atomic single-writer writes, so a hand edit
   concurrent with the running daemon is a lost-update race.

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
- `drain.broadcast_effects.pending` drains to 0 (re-drive runs at boot and on tick);
  rows whose payload fetch stays inconclusive remain parked and visible;
  `broadcast_effects_terminal` lists any effect that finished failed/unknown
- deferred wakes fire once the marker is gone - unless the restored prior pause.json
  carried a user pause, under which they correctly stay deferred
- the Linear hook accepts new events again (200 queued), if it is configured and
  enabled in the restored prior config

## First adoption (EXPLORATORY - NOT QUALIFIED, do not use)

PRECISE BLOCKER: native long-poll ingress has no supported fence short of stopping
the process, and no observation is atomic with the stop, so a provably lossless
first adoption does not exist on the old runtime. Every zero-loss alternative needs
authority or mutations worse than the restart: stopping hosted Zulip (not
available), rotating 26 bot credentials to force reconnect loops (a larger
production mutation than the restart, itself moratorium-covered), or hotpatching
the defer machinery into the running process (untestable). Accepting a one-time
bounded residual is an OWNER-LEVEL decision that has NOT been made, and this
document does not treat it as accepted.

Fence claims, stated exactly:

- The outgoing webhook CAN be fenced (config disable + SIGHUP) - not because Zulip
  retries (its retry behavior is limited), but because webhook-triggering messages
  also arrive on the native event queue, which is cursor-tracked and replays
  post-boot under this change's F4/dedupe fences. Disabling the webhook does NOT
  reduce native-path admissions during the window.
- The Linear hook toggle does NOT give a clean fence: config-disable leaves the
  listener API serving an error status (404-class) at /linear/v1/, and Linear's
  retry/auto-disable behavior for that status is unverified - a disabled-in-Linear
  webhook would be worse than the residual it tries to avoid. Until verified, the
  honest option is to LEAVE THE HOOK ENABLED and name the bounded loss below. (The
  new runtime's 503 gate is the right fence precisely because 503 has unambiguous
  retry semantics.)

Loss classes in the one-time stop (all bounded to the planned window):

- a wake admitted between the last observable check and SIGTERM finalizes into a
  RECORDED paused drop (pre-existing semantics);
- a Linear event acknowledged 200 and held in memory is lost on stop. Recovery is
  CONTINGENT: reconcile is read-idempotent and self-heals only when a LATER project
  event arrives; if none arrives the loss persists indefinitely, and a reconcile
  interrupted mid-write can itself leave partial linkifier changes;
- the SILENT class: a native-path broadcast (an owner or fleet-bot channel-wide
  mention landing inside the gap) starts fanout/inbox/audit effects with no gate
  and no durable record on the old runtime, and a stop mid-effect is a silent
  partial broadcast (missing nudges for un-enqueued chats, lost inbox copies, lost
  in-memory wake hand-off). Controllable on the owner side by not sending a
  channel-wide mention during the window; a fleet-bot @all is the remaining tail.

Candidate minimal-loss route (for the owner's decision - NOT qualified, not a
drain): quiet window; capture listener.toml + pause.json bytes; disable the
outgoing webhook via config + SIGHUP (native redundancy above); leave the Linear
hook enabled; ordinary pause (new wake admissions become RECORDED drops, the
pre-existing semantics); wait for per-seat jobs 0 / running null plus log
quiescence (the only gauges the old status has); SIGTERM; recreate on the CI-green
head; restore exact prior bytes (lifting the adoption pause, preserving any
pre-existing user pause); boot; verify seats connected, cursors advancing, drain
block present with armed false. After this stop, every later stop uses the
qualified procedure above.

Rollback of the adoption itself: the supported route is reverting main and a
manual full source rebuild/redeploy, plus restoring the exact captured
config/pause bytes again - NOT a prior-image rollback, which is unverified. A
rollback has its own stop window with the same one-time loss classes above.

## Broadcast effect ledger (durable admission and completion)

Every broadcast (owner or fleet-bot channel-wide mention) is claimed against
`listener/broadcast_effects.json` at admission, before any seen-ring or ring of any
kind is touched, with a sha256 snapshot of the message at admission. The claim is
atomic with a seen-marker: the SAME message id is only admitted when it was not
claimed before (done/failed/unknown rows are durable and never resurrect).

Each row carries a per-kind state machine: pending -> driving ->
done / failed / unknown.

- The drain marker gates DRIVING only. Admission is durable with or without it, so
  an ordinary crash fences exactly the same as a planned drain.
- Boot reconciles: stale claims are cleared, driving effects re-park as pending,
  and a broadcast-audit effect driving with send_attempted_at set becomes UNKNOWN
  (the send already happened or was attempted; re-sending would duplicate).
- A parked kind never expires (GATE 2). Re-drive runs at boot and on tick (when the
  marker is gone) with budgeted rotated payload probes (F8). A payload that stays
  inconclusive keeps the kind parked and visible; it is never dropped.
- A crash mid-effect is fenced per class:
  - chat fan-out / nudges: the nudge dedupe key is durable and actual, so a
    re-driven walk never double-nudges; a walk that ends with catalog failure or
    failed enqueues finishes FAILED with the per-target reasons, never done.
  - inbox copies: every inbox write is idempotent (a subscription and a read
    flag), so a crash after the effect but before the completion receipt
    re-delivers safely; a delivery that exhausts its retry bound finishes FAILED
    and is surfaced. Per-seat receipts are recorded durably.
  - wake hand-offs: re-offers pass through the F4 trigger-id fence, so a re-drive
    can never double a wake.
  - audit reminders: send_attempted_at is written durably BEFORE the send call. A
    crash after that write but before the call can LOSE one reminder - the row
    becomes UNKNOWN, is surfaced, and is never resent. A crash after the call is
    the same class. UNKNOWN is unresolved loss: it is surfaced in status, not
    hidden.
- Terminal rows are never pruned by age except rows whose EVERY kind is done, after
  30 days (BROADCAST_LEDGER_DONE_KEEP_SECONDS). Failed, unknown and pending rows
  never age out. Pruned all-done rows drop the seen-fence with them; re-admission
  after 30 days requires a genuinely new event with the same message id, which no
  current route can produce (message edits never re-admit broadcasts).
- On first boot after this change, the legacy `deferred_broadcasts.json` store is
  copied into the ledger, then renamed to `.migrated` only after the durable write
  completes. If the copy fails or is partial, the legacy file is left in place and
  no migration is recorded, so the next boot retries (fail-closed). Parked legacy
  entries re-drive through the same gate; completed broadcast ids keep their
  seen-marker.

## Named residuals (explicit; owner acceptance pending)

- An event acknowledged 200 by the Linear hook and held only in memory is lost on an
  UNPLANNED stop; reconcile is read-idempotent but self-heals only IF a later project
  event arrives - with no later event the loss persists indefinitely, and an
  interrupted reconcile can leave partial linkifier changes (see First adoption). The
  503 gate removes the planned-stop case.
- Re-drive probes are budgeted and rotated (F8): a payload only fetchable by a subset
  of runners can wait successive rotated probes at backoff pace (30s doubling to a 5
  minute cap per id; with many parked kinds or a large fleet that can add up to
  hours) before its effects re-drive. The parked kind is retained visibly throughout
  (nonzero drain.broadcast_effects.pending naming the message) rather than lost.
- Re-drive does not re-run the route's admission predicates (including the stale
  exclusions) on the parked message; it re-routes only to recover the parked
  classification. A parked kind can now be old (kinds never expire), so the
  admission-time judgment is preserved rather than re-evaluated - an explicit
  design choice.
- A crash between the audit marker write and the send call can LOSE one audit
  reminder (the row is UNKNOWN and never resent); a crash between the send and the
  receipt lands in the same class, so a reminder is never duplicated. Both are
  surfaced via drain.broadcast_effects_terminal.
- SIGKILL/crash can duplicate a seat-INBOX row only for a replay older than the
  dedup window (the last 2000 keys or 256KB of the file): the chunked flush keeps
  crash-time loss far inside that window, and no current caller replays from beyond
  it. Torn lines (partial chunk at tail) are skipped on read and deduped on the
  re-append. Duplication beyond that boundary is not claimed.
- Passive unleased-chatter rows in the router queue at stop are lost, as with any
  crash; every wake/nudge-relevant class is fenced by post-restart backfill.
- The spool can starve under sustained native load and stall step 3 into its abort
  path (safe).

