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

export class SeatGate extends DurableObject {
  get store() {
    return this.ctx.storage;
  }

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

  roleGet() {
    return state.roleGet(this.store);
  }

  roleSet(entry) {
    return state.roleSet(this.store, entry);
  }

  roleStatus() {
    return state.roleStatus(this.store);
  }

  auditCall(row) {
    return state.auditCall(this.store, row);
  }

  callTail(limit) {
    return state.callTail(this.store, limit);
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

  eventDeliveryResult(options) {
    return state.eventDeliveryResult(this.store, options);
  }

  eventSubsSummary() {
    return state.eventSubsSummary(this.store);
  }
}
