// SeatGate:  one SQLite-backed Durable Object per seat (`idFromName(seat)`),
// plus one instance named GATE_LOG_NAME for the refusal log.  It owns the
// arming window, the grant epoch, the pause flag and the audit tail (spec 3.7,
// Phase 0 subset:  no Zulip calls, spacing or budgets yet).  The logic lives in
// seat-state.js;  this class only exposes it over RPC.

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
}
