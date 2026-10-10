// Per-seat state machine:  arming window, grant epoch, pause flag and audit
// (Phase 0), plus the Phase 2 call gate:  write and read spacing, budgets, the
// 429 cooldown, idempotency rows, the role cache and the per-call audit log.
//
// Pure module:  it runs against any async key-value `store` with `get(key)` and
// `put(key, value)`.  The SeatGate Durable Object hands it `ctx.storage`, whose
// input and output gates make each method below atomic (no external I/O runs
// between the read and the write).  Tests hand it a Map.
// Spec:  docs/protocols/agent-sync-mcp.md sections 3.4, 3.5 and 3.10.

import { ARM_WINDOW_MS, GRANT_MAX_AGE_MS, PROPS_PHASE } from "./config.js";

const AUDIT_CAP = 200;
// Authorize refusals come from a request that passed Cloudflare Access, so the
// log is Jay's own traffic and stays small.  Token refusals are unauthenticated
// internet traffic:  they are counted per (reason, client_id) in a separate,
// smaller table so they can never push Grok's first attempt out of view.
const REFUSAL_CAP = 100;
const TOKEN_REFUSAL_CAP = 40;

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
 * `code` is "ok", "stale_epoch", "stale_phase", "grant_too_old", "bad_props" or "paused".
 * Pause is checked last:  a stale grant is reported as stale even when paused,
 * so the caller revokes it instead of keeping it for later.
 */
export async function check(store, { epoch, approvedAt, phase }, now = Date.now()) {
  const s = await read(store);
  if (!Number.isInteger(epoch) || !Number.isFinite(approvedAt)) return { code: "bad_props" };
  // A grant minted before the real tools (Phase 0 props carry no phase) never
  // reaches them:  spec section 6, "no stub grant survives into Phase 2".
  if (phase !== PROPS_PHASE) return { code: "stale_phase" };
  if (epoch !== s.epoch) return { code: "stale_epoch" };
  if (!(now - approvedAt < GRANT_MAX_AGE_MS) || approvedAt > now + 60_000) return { code: "grant_too_old" };
  if (s.paused) return { code: "paused" };
  return { code: "ok" };
}

export async function tail(store, limit = 50) {
  const rows = (await store.get("audit")) ?? [];
  return rows.slice(-limit).reverse();
}

/**
 * Refusal log (spec 3.3, 3.4 step 4):  raw ids only, never bodies or tokens.
 * `where: "token"` rows are unauthenticated and go to the counted table;  every
 * other row is an Access-authenticated /authorize refusal and keeps its order.
 */
export async function logRefusal(store, entry, now = Date.now()) {
  if (entry.where === "token") {
    const rows = (await store.get("token_refusals")) ?? [];
    const key = `${entry.reason}|${entry.client_id ?? ""}`;
    const at = rows.findIndex((r) => `${r.reason}|${r.client_id ?? ""}` === key);
    const prior = at >= 0 ? rows.splice(at, 1)[0] : null;
    rows.push({ ts: now, where: "token", reason: entry.reason, client_id: entry.client_id ?? "", count: (prior?.count ?? 0) + 1 });
    while (rows.length > TOKEN_REFUSAL_CAP) rows.shift();
    await store.put("token_refusals", rows);
    return;
  }
  await appendCapped(store, "refusals", { ts: now, ...entry }, REFUSAL_CAP);
}

export async function refusals(store, limit = 50) {
  const rows = (await store.get("refusals")) ?? [];
  return rows.slice(-limit).reverse();
}

export async function tokenRefusals(store, limit = 20) {
  const rows = (await store.get("token_refusals")) ?? [];
  return rows.slice(-limit).reverse();
}

// ---------------------------------------------------------------- Phase 2 call gate

export const MINUTE_MS = 60 * 1000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;
export const WRITE_SPACING_MS = 3000; // posts, replies and reactions (spec 3.7)
export const READ_SPACING_MS = 500;
export const MAX_WAIT_MS = 6000; // a call waits at most this long for its slot
export const PENDING_FRESH_MS = 30_000; // a pending row younger than this is a write in flight
export const ROLE_TTL_MS = 10 * MINUTE_MS; // spec 3.6:  users/me on first use and every 10 minutes
export const CALL_RETENTION_DAYS = 90; // spec 3.9
const CALLS_PER_DAY_CAP = 5000;
const GEO_CAP = 50;

// D5 budgets, per seat.  `write` is post and reply, `react` is reactions,
// `recall` is recall_contribute (spec 1.1;  it takes the write spacing too).
export const BUDGETS = Object.freeze({
  write: Object.freeze([
    { windowMs: HOUR_MS, limit: 20 },
    { windowMs: DAY_MS, limit: 120 },
  ]),
  react: Object.freeze([{ windowMs: HOUR_MS, limit: 60 }]),
  recall: Object.freeze([
    { windowMs: HOUR_MS, limit: 10 },
    { windowMs: DAY_MS, limit: 40 },
  ]),
  read: Object.freeze([{ windowMs: HOUR_MS, limit: 300 }]),
});

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

/**
 * Take a slot for one tool call of `kind` ("read", "write", "react" or "recall").
 * Returns {ok: true, waitMs} (the caller sleeps that long, at most 6 seconds,
 * then calls Zulip), or {ok: false, code, retryAfterS?, resetAt?}.  The slot
 * and the budget entry are written before this returns, and the method does no
 * outside I/O, so concurrent calls of one seat get distinct slots.
 */
export async function reserve(store, { kind }, now = Date.now()) {
  if (!Object.hasOwn(BUDGETS, kind)) throw new Error("unknown call kind");
  if ((await store.get("paused")) === true) return { ok: false, code: "paused" };
  const cooldown = await store.get("cooldown_until");
  if (Number.isFinite(cooldown) && cooldown > now) {
    return { ok: false, code: "rate_limited", retryAfterS: Math.max(1, Math.ceil((cooldown - now) / 1000)) };
  }
  const windows = BUDGETS[kind];
  const longest = Math.max(...windows.map((w) => w.windowMs));
  const history = ((await store.get(`budget:${kind}`)) ?? []).filter((t) => Number.isFinite(t) && t > now - longest && t <= now + MINUTE_MS);
  for (const { windowMs, limit } of windows) {
    const inWindow = history.filter((t) => t > now - windowMs);
    if (inWindow.length >= limit) {
      return { ok: false, code: "budget_exhausted", resetAt: iso(Math.min(...inWindow) + windowMs), window: windowMs === HOUR_MS ? "hour" : "day" };
    }
  }
  const lane = kind === "read" ? "read" : "write";
  const spacing = lane === "read" ? READ_SPACING_MS : WRITE_SPACING_MS;
  let last = await store.get(`last:${lane}`);
  if (!Number.isFinite(last) || last > now + 10 * spacing) last = null; // the clock moved back
  const slot = last === null ? now : Math.max(now, last + spacing);
  const waitMs = slot - now;
  if (waitMs > MAX_WAIT_MS) {
    return { ok: false, code: "rate_limited", retryAfterS: Math.max(1, Math.ceil((waitMs - MAX_WAIT_MS) / 1000)) };
  }
  history.push(now);
  await store.put(`last:${lane}`, slot);
  await store.put(`budget:${kind}`, history);
  return { ok: true, waitMs };
}

/** After Zulip's 429 outlasted the per-call budget:  a seat-wide cooldown. */
export async function noteRateLimited(store, { retryAfterS }, now = Date.now()) {
  const seconds = Number.isFinite(retryAfterS) ? Math.min(Math.max(retryAfterS, 1), 3600) : 30;
  const until = now + seconds * 1000;
  const current = await store.get("cooldown_until");
  if (!Number.isFinite(current) || current < until) await store.put("cooldown_until", until);
  await audit(store, { event: "zulip_rate_limited", cooldown_s: seconds }, now);
  return { until };
}

// ---- idempotency rows (spec 1:  pending, sent(id) or unknown)

async function idemRows(store, now) {
  const rows = (await store.get("idem")) ?? {};
  for (const [key, row] of Object.entries(rows)) {
    if (!row || !Number.isFinite(row.expires) || row.expires <= now) delete rows[key];
  }
  return rows;
}

/**
 * Start a write under `key`.  Returns {verdict: "send"}, {verdict:
 * "duplicate", row} for a sent key, {verdict: "reconcile", row} for an
 * unknown or abandoned one, or {verdict: "busy"} while another call of the
 * same write is in flight.
 */
export async function idemBegin(store, { key, ttlMs, fields }, now = Date.now()) {
  const rows = await idemRows(store, now);
  const row = rows[key];
  let result;
  if (row && row.state === "sent") result = { verdict: "duplicate", row: { ...row } };
  else if (row && row.state === "pending" && now - (row.claimed_at ?? 0) < PENDING_FRESH_MS) result = { verdict: "busy", row: { ...row } };
  else if (row && (row.state === "unknown" || row.state === "pending")) {
    row.state = "pending";
    row.claimed_at = now;
    result = { verdict: "reconcile", row: { ...row } };
  } else {
    rows[key] = { ...fields, state: "pending", claimed_at: now, ts: now, expires: now + ttlMs };
    result = { verdict: "send" };
  }
  await store.put("idem", rows);
  return result;
}

export async function idemSet(store, { key, changes }, now = Date.now()) {
  const rows = await idemRows(store, now);
  if (rows[key]) Object.assign(rows[key], changes);
  await store.put("idem", rows);
}

export async function idemDrop(store, { key }, now = Date.now()) {
  const rows = await idemRows(store, now);
  delete rows[key];
  await store.put("idem", rows);
}

/** The row for `key` when it is sent and unexpired, else null. */
export async function idemPeek(store, { key }, now = Date.now()) {
  const row = (await idemRows(store, now))[key];
  return row && row.state === "sent" ? { ...row } : null;
}

// ---- role cache (spec 3.6)

export async function roleGet(store, now = Date.now()) {
  const cached = await store.get("role");
  if (!cached || !Number.isFinite(cached.checked_at) || now - cached.checked_at >= ROLE_TTL_MS || cached.checked_at > now + MINUTE_MS) return null;
  return { ...cached };
}

/** The last role check, fresh or not, for /admin. */
export async function roleStatus(store) {
  const cached = await store.get("role");
  return cached ? { ...cached } : null;
}

export async function roleSet(store, entry, now = Date.now()) {
  const prior = await store.get("role");
  const row = { ok: entry.ok === true, user_id: entry.user_id ?? null, role: entry.role ?? null, reason: entry.reason ?? "", checked_at: now };
  await store.put("role", row);
  if (!prior || prior.ok !== row.ok || prior.role !== row.role) {
    await audit(store, { event: row.ok ? "role_ok" : "role_refused", role: row.role, reason: row.reason }, now);
  }
  return row;
}

// ---- per-call audit (spec 3.9), kept 90 days

const dayKey = (ms) => new Date(ms).toISOString().slice(0, 10);
const CALL_FIELDS = ["seat", "grant_ref", "client_id", "tool", "channel_id", "topic", "message_id", "outcome", "error_code", "latency_ms", "body_len", "idem_ref", "asn", "country"];

/**
 * One row per tool call.  Only the spec 3.9 fields are kept (never a body, a
 * key, a token or a header), and a change of ASN or country within one grant
 * is flagged in the event log.
 */
export async function auditCall(store, row, now = Date.now()) {
  const clean = { ts: now };
  for (const field of CALL_FIELDS) {
    const value = row[field];
    if (value === undefined || value === null) continue;
    clean[field] = typeof value === "string" ? value.slice(0, 120) : value;
  }
  const day = dayKey(now);
  const days = ((await store.get("call_days")) ?? []).filter((d) => typeof d === "string");
  if (!days.includes(day)) days.push(day);
  const cutoff = dayKey(now - CALL_RETENTION_DAYS * DAY_MS);
  const keep = [];
  for (const d of days.sort()) {
    if (d < cutoff) await store.put(`calls:${d}`, null);
    else keep.push(d);
  }
  await store.put("call_days", keep);
  const bucket = (await store.get(`calls:${day}`)) ?? [];
  bucket.push(clean);
  while (bucket.length > CALLS_PER_DAY_CAP) bucket.shift();
  await store.put(`calls:${day}`, bucket);

  let geoChanged = false;
  if (clean.grant_ref && (clean.asn !== undefined || clean.country !== undefined)) {
    const geo = (await store.get("grant_geo")) ?? {};
    const prior = geo[clean.grant_ref];
    if (prior && (prior.asn !== (clean.asn ?? null) || prior.country !== (clean.country ?? null))) {
      geoChanged = true;
      await audit(store, { event: "geo_change", grant_ref: clean.grant_ref, from_asn: prior.asn, to_asn: clean.asn ?? null, from_country: prior.country, to_country: clean.country ?? null }, now);
    }
    delete geo[clean.grant_ref];
    geo[clean.grant_ref] = { asn: clean.asn ?? null, country: clean.country ?? null };
    const refs = Object.keys(geo);
    while (refs.length > GEO_CAP) delete geo[refs.shift()];
    await store.put("grant_geo", geo);
  }
  return { geoChanged };
}

/** The newest call rows, newest first. */
export async function callTail(store, limit = 40) {
  const days = ((await store.get("call_days")) ?? []).filter((d) => typeof d === "string").sort().reverse();
  const out = [];
  for (const d of days) {
    const bucket = (await store.get(`calls:${d}`)) ?? [];
    for (let i = bucket.length - 1; i >= 0 && out.length < limit; i--) out.push(bucket[i]);
    if (out.length >= limit) break;
  }
  return out;
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
