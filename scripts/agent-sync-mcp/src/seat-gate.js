// SeatGate:  one SQLite-backed Durable Object per seat (`idFromName(seat)`),
// plus one instance named GATE_LOG_NAME for the refusal log.  It owns the
// arming window, the grant epoch, the pause flag and the audit tail (Phase 0),
// and the call gate (spec 3.7):  write and read spacing, budgets, the 429
// cooldown, idempotency rows, the role cache and the per-call audit log.
//
// Every method is storage operations only, with no outside I/O, so the
// Durable Object's input gate makes each one atomic:  two calls of one seat
// can never take the same write slot.  The Worker makes the Zulip calls
// after its slot comes back.  The logic lives in seat-state.js;  this class
// only exposes it over RPC.

import { DurableObject } from "cloudflare:workers";
import * as state from "./seat-state.js";
import * as wa from "./wa/state.js";

export class SeatGate extends DurableObject {
  // AFC-545: the WhatsApp send switch and the owner binding are Worker
  // bindings, and the Durable Object is the one place both the tool path and
  // the signed outbox route can read them the same way.
  constructor(ctx, env, ...rest) {
    super(ctx, env, ...rest);
    this.env = env;
  }

  get store() {
    return this.ctx.storage;
  }

  async waIngest(options) {
    const result = await wa.ingest(this.store, options);
    if (result.ok && await this.store.getAlarm() === null) await this.store.setAlarm(Date.now() + 3600000);
    return result;
  }
  async alarm() {
    // The mismatch counter flushes ONLY when due:  this handler also fires
    // every second while the wa sweep's busy chain drains, and an
    // unconditional flush would turn that cadence back into a write per
    // second - the amplification the buffer exists to bound (fresh-head
    // review).  A failed flush keeps its deltas and backs off to the
    // interval (retryAt), which the joint reschedule below honors.
    const now = Date.now();
    if (state.gbMismatchDue(this._mmBuf, now)) {
      try { await state.gbMismatchFlushGuarded(this.store, this._mmBuf, now); } catch { /* deltas kept; the retry comes due at buffer.retryAt, scheduled below. */ }
    }
    let more = false;
    try { more = await wa.sweep(this.store); } finally {
      // One alarm serves both features:  the EARLIEST of the sweep's own
      // cadence and the counter's due time.  The idle hour can no longer
      // bury a pending tail or a retry, and the busy chain can no longer
      // pull a flush forward - the due check above decides, not the cadence.
      // finally:  a sweep throw must not orphan a buffered tail or a backoff
      // retry - the reschedule runs even then, and the error still propagates
      // so the runtime's own alarm retry applies on top (fresh-head review).
      const waNext = Date.now() + (more ? 1000 : 3600000);
      const mmNext = state.gbMismatchDueAt(this._mmBuf, Date.now());
      await this.store.setAlarm(mmNext === null ? waNext : Math.min(waNext, mmNext));
    }
  }
  waRead(options) { return wa.read(this.store, options); }
  // AFC-545: the outbound leg.  The send switch and the binding come from
  // the Worker bindings, so the tool path and the signed outbox route always
  // agree on them.
  waReply(options) { return wa.reply(this.store, { ...options, sendEnabled: wa.sendEnabled(this.env), binding: wa.bindingIdentity(this.env) }); }
  waSendStatus(options) { return wa.sendStatus(this.store, { ...options, sendEnabled: wa.sendEnabled(this.env) }); }
  waOutboxClaim(options) { return wa.outboxClaim(this.store, { ...options, sendEnabled: wa.sendEnabled(this.env), binding: wa.bindingIdentity(this.env) }); }
  waOutboxResult(options) { return wa.outboxResult(this.store, { ...options, binding: wa.bindingIdentity(this.env) }); }
  async waOperator(options) {
    const result = await wa.operator(this.store, options);
    if (result.ok && await this.store.getAlarm() === null) await this.store.setAlarm(Date.now() + 3600000);
    return result;
  }
  waClaim(options) { return wa.claim(this.store, options); }
  waSettle(options) { return wa.settle(this.store, options); }
  eventSubscriptions(options) { return state.eventSubscriptions(this.store, options); }

  getState() {
    return state.getState(this.store);
  }

  arm(options) {
    return state.arm(this.store, options);
  }

  disarm(options) {
    return state.disarm(this.store, options);
  }

  approve(options) {
    return state.approve(this.store, options);
  }

  bumpEpoch(options) {
    return state.bumpEpoch(this.store, options);
  }

  setPaused(options) {
    return state.setPaused(this.store, options);
  }

  check(options) {
    return state.check(this.store, options);
  }

  audit(event) {
    return state.audit(this.store, event);
  }

  tail(limit) {
    return state.tail(this.store, limit);
  }

  logRefusal(entry) {
    return state.logRefusal(this.store, entry);
  }

  refusals(limit) {
    return state.refusals(this.store, limit);
  }

  tokenRefusals(limit) {
    return state.tokenRefusals(this.store, limit);
  }

  reserve(options) {
    return state.reserve(this.store, options);
  }

  providerQuotaReserve(options) { return state.providerQuotaReserve(this.store, options); }
  providerQuotaNoteRateLimited(options) { return state.providerQuotaNoteRateLimited(this.store, options); }

  noteRateLimited(options) {
    return state.noteRateLimited(this.store, options);
  }

  idemBegin(options) {
    return state.idemBegin(this.store, options);
  }

  idemSet(options) {
    return state.idemSet(this.store, options);
  }

  idemDrop(options) {
    return state.idemDrop(this.store, options);
  }

  idemPeek(options) {
    return state.idemPeek(this.store, options);
  }

  linearApiBegin(options) {
    return state.linearApiBegin(this.store, options);
  }

  linearApiSet(options) {
    return state.linearApiSet(this.store, options);
  }

  linearApiDrop(options) {
    return state.linearApiDrop(this.store, options);
  }

  roleGet() {
    return state.roleGet(this.store);
  }

  roleSet(entry) {
    return state.roleSet(this.store, entry);
  }

  roleStatus() {
    return state.roleStatus(this.store);
  }

  linearTokenGet() {
    return state.linearTokenGet(this.store);
  }

  linearTokenSet(entry) {
    return state.linearTokenSet(this.store, entry);
  }

  linearMintNote(entry) {
    return state.linearMintNote(this.store, entry);
  }

  linearHealth() {
    return state.linearHealth(this.store);
  }

  gbBearerMint(entry) {
    return state.gbBearerMint(this.store, entry);
  }

  gbBearerRevoke(entry) {
    return state.gbBearerRevoke(this.store, entry);
  }

  gbBearerCheck(hash) {
    return state.gbBearerCheck(this.store, hash);
  }

  gbBearerMintChecked(entry) {
    return state.gbBearerMintChecked(this.store, entry);
  }

  claimGrAssert(options) {
    return state.claimGrAssert(this.store, options);
  }
  gbBearerStatus() {
    return state.gbBearerStatus(this.store);
  }

  // Buffered:  one in-memory increment per call, at most one storage write
  // per GB_MISMATCH_FLUSH_MS per instance (fresh-head review - an anonymous
  // caller must not turn wrong guesses into unbounded DO storage writes).
  // A buffered (unflushed) increment arms the alarm so even a quiet tail -
  // one probe and then silence - persists when it comes due;  an existing
  // earlier alarm covers it (the handler reschedules to the counter's due
  // time) without a second scheduling write.
  async gbBearerMismatch(identity, configured) {
    const buf = (this._mmBuf ??= state.gbMismatchBuffer());
    let flushed = false;
    try {
      flushed = await state.gbMismatchCount(this.store, buf, identity, configured);
    } catch {
      // A failed flush keeps every delta in the buffer;  the alarm armed
      // below drives the retry at the backoff time (retryAt).  The route
      // caller's own catch keeps the counter off the security path, so
      // swallowing here only adds the retry arming.
    }
    if (!flushed) {
      // Earlier-of semantics:  an existing earlier alarm covers the tail;
      // a later one (the wa sweep's idle hour) is pulled to the due time.
      const setAt = state.gbMismatchAlarmDeadline(buf, await this.store.getAlarm());
      if (setAt !== null) await this.store.setAlarm(setAt);
    }
    return flushed;
  }

  gbBearerMismatches() {
    return state.gbBearerMismatches(this.store);
  }

  linearIdentityGet() {
    return state.linearIdentityGet(this.store);
  }

  linearIdentitySet(entry) {
    return state.linearIdentitySet(this.store, entry);
  }

  linearIdentityClear() {
    return state.linearIdentityClear(this.store);
  }

  linearTokenClear() {
    return state.linearTokenClear(this.store);
  }

  auditCall(row) {
    return state.auditCall(this.store, row);
  }

  callTail(limit) {
    return state.callTail(this.store, limit);
  }

  errorTail(limit) {
    return state.errorTail(this.store, limit);
  }

  recordDiagnostic(entry) {
    return state.recordDiagnostic(this.store, entry);
  }

  diagnosticTail(limit) {
    return state.diagnosticTail(this.store, limit);
  }

  eventSubRoom(options) {
    return state.eventSubRoom(this.store, options);
  }

  eventSubUpsert(options) {
    return state.eventSubUpsert(this.store, options);
  }

  eventSubDelete(options) {
    return state.eventSubDelete(this.store, options);
  }

  eventVerifiedGet(options) {
    return state.eventVerifiedGet(this.store, options);
  }

  eventVerifiedSet(options) {
    return state.eventVerifiedSet(this.store, options);
  }

  eventClaimWake(options) {
    return state.eventClaimWake(this.store, options);
  }

  eventSettleWake(options) {
    return state.eventSettleWake(this.store, options);
  }

  eventSubLive(options) {
    return state.eventSubLive(this.store, options);
  }

  eventDeliveryResult(options) {
    return state.eventDeliveryResult(this.store, options);
  }

  eventSubsSummary() {
    return state.eventSubsSummary(this.store);
  }

  // AFC-477 spike (temporary, src/spike.js).
  spikeClaim(options) {
    return state.spikeClaim(this.store, options);
  }
}
