// Per-seat state machine:  arming window, grant epoch, pause flag and audit.
//
// Pure module:  it runs against any async key-value `store` with `get(key)` and
// `put(key, value)`.  The SeatGate Durable Object hands it `ctx.storage`, whose
// input and output gates make each method below atomic (no external I/O runs
// between the read and the write).  Tests hand it a Map.
// Spec:  docs/protocols/agent-sync-mcp.md sections 3.4, 3.5 and 3.10.

import { ARM_WINDOW_MS, GRANT_MAX_AGE_MS } from "./config.js";

const AUDIT_CAP = 200;
const REFUSAL_CAP = 200;

async function read(store) {
  const [epoch, paused, arm] = await Promise.all([store.get("epoch"), store.get("paused"), store.get("arm")]);
  return { epoch: Number.isInteger(epoch) ? epoch : 0, paused: paused === true, arm: arm ?? null };
}

async function appendCapped(store, key, entry, cap) {
  const rows = (await store.get(key)) ?? [];
  rows.push(entry);
  while (rows.length > cap) rows.shift();
  await store.put(key, rows);
}

export async function audit(store, event, now = Date.now()) {
  await appendCapped(store, "audit", { ts: now, ...event }, AUDIT_CAP);
}

/** Public state for /admin.  `armed` is true only inside an unexpired window. */
export async function getState(store, now = Date.now()) {
  const s = await read(store);
  const armed = s.arm !== null && s.arm.until > now;
  return { epoch: s.epoch, paused: s.paused, armed, armedUntil: armed ? s.arm.until : null, armedBy: armed ? s.arm.by : null };
}

/** Open a single-use arming window (spec 3.4 step 1). */
export async function arm(store, { by, ttlMs = ARM_WINDOW_MS }, now = Date.now()) {
  const until = now + Math.min(Math.max(ttlMs, 1000), ARM_WINDOW_MS);
  await store.put("arm", { until, by: String(by ?? "") });
  await audit(store, { event: "arm", by, until }, now);
  return getState(store, now);
}

/** Close the window without approving (deny, expiry seen, or Jay's disarm). */
export async function disarm(store, { by, reason }, now = Date.now()) {
  const s = await read(store);
  if (s.arm !== null) await store.put("arm", null);
  await audit(store, { event: "disarm", by, reason: String(reason ?? "") }, now);
  return getState(store, now);
}

/**
 * Approve:  consume the window and bump the epoch in one step (spec 3.5 step 1).
 * Returns `{ ok: true, epoch }`, or `{ ok: false, reason }` when no window is
 * open, so of two parallel approvals exactly one wins.
 */
export async function approve(store, { by }, now = Date.now()) {
  const s = await read(store);
  if (s.arm === null || !(s.arm.until > now)) {
    if (s.arm !== null) await store.put("arm", null);
    await audit(store, { event: "approve_refused", by, reason: s.arm === null ? "not_armed" : "window_expired" }, now);
    return { ok: false, reason: s.arm === null ? "not_armed" : "window_expired" };
  }
  const epoch = s.epoch + 1;
  await store.put("epoch", epoch);
  await store.put("arm", null);
  await audit(store, { event: "approve", by, epoch }, now);
  return { ok: true, epoch };
}

/** Bump the epoch:  every token and refresh token from before stops working. */
export async function bumpEpoch(store, { by, reason }, now = Date.now()) {
  const s = await read(store);
  const epoch = s.epoch + 1;
  await store.put("epoch", epoch);
  await audit(store, { event: "bump_epoch", by, reason: String(reason ?? ""), epoch }, now);
  return { epoch };
}

export async function setPaused(store, { by, paused }, now = Date.now()) {
  await store.put("paused", paused === true);
  await audit(store, { event: paused ? "pause" : "unpause", by }, now);
  return getState(store, now);
}

/**
 * Check a grant's props against the seat's live state (spec 3.5 steps 2 and 5).
 * `code` is "ok", "stale_epoch", "grant_too_old", "bad_props" or "paused".
 * Pause is checked last:  a stale grant is reported as stale even when paused,
 * so the caller revokes it instead of keeping it for later.
 */
export async function check(store, { epoch, approvedAt }, now = Date.now()) {
  const s = await read(store);
  if (!Number.isInteger(epoch) || !Number.isFinite(approvedAt)) return { code: "bad_props" };
  if (epoch !== s.epoch) return { code: "stale_epoch" };
  if (!(now - approvedAt < GRANT_MAX_AGE_MS) || approvedAt > now + 60_000) return { code: "grant_too_old" };
  if (s.paused) return { code: "paused" };
  return { code: "ok" };
}

export async function tail(store, limit = 50) {
  const rows = (await store.get("audit")) ?? [];
  return rows.slice(-limit).reverse();
}

/** Refusal log (spec 3.3, 3.4 step 4):  raw ids only, never bodies or tokens. */
export async function logRefusal(store, entry, now = Date.now()) {
  await appendCapped(store, "refusals", { ts: now, ...entry }, REFUSAL_CAP);
}

export async function refusals(store, limit = 50) {
  const rows = (await store.get("refusals")) ?? [];
  return rows.slice(-limit).reverse();
}

/** A Map-backed store for tests. */
export function memoryStore() {
  const map = new Map();
  return {
    map,
    async get(key) {
      const v = map.get(key);
      return v === undefined ? undefined : structuredClone(v);
    },
    async put(key, value) {
      map.set(key, structuredClone(value));
    },
  };
}
