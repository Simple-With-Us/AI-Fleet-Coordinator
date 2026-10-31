// Per-seat state machine:  arming window, grant epoch, pause flag and audit
// (Phase 0), plus the Phase 2 call gate:  write and read spacing, budgets, the
// 429 cooldown, idempotency rows, the role cache and the per-call audit log.
//
// Pure module:  it runs against any async key-value `store` with `get(key)` and
// `put(key, value)`.  The SeatGate Durable Object hands it `ctx.storage`, whose
// input and output gates make each method below atomic (no external I/O runs
// between the read and the write).  Tests hand it a Map.
// Spec:  docs/protocols/agent-sync-mcp.md sections 3.4, 3.5 and 3.10.

import { WA_EVENT } from "./wa/contract.js";
import { ARM_WINDOW_MS, GRANT_MAX_AGE_MS, PROPS_PHASE } from "./config.js";
import { scanSecret } from "./contract.js";
import { EVENT_NAME, SPIKE_EVENT_NAME, subEventName } from "./events.js";

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
const LINEAR_API_DATA_MAX_BYTES = 8192;
const LINEAR_API_CLEANUP_INTERVAL_MS = MINUTE_MS;
export const ROLE_TTL_MS = 10 * MINUTE_MS; // spec 3.6:  users/me on first use and every 10 minutes
export const CALL_RETENTION_DAYS = 90; // spec 3.9
const CALLS_PER_DAY_CAP = 5000;
const ERROR_AUDIT_CAP = 100;
export const CALLS_STORAGE_MAX_BYTES = 1024 * 1024;
export const ERRORS_STORAGE_MAX_BYTES = 256 * 1024;
const ERROR_MESSAGE_MAX = 300;
const GEO_CAP = 50;

// D5 budgets, per seat.  `write` is post and reply, `react` is reactions,
// `recall` is recall_contribute (spec 1.1;  it takes the write spacing too).
// The counts are generous on purpose: routine work must not meet them.  The slot spacing (WRITE_SPACING_MS,
// READ_SPACING_MS) and the bounded wait for a slot already throttle bursts, so the write-lane hourly count is
// what that spacing allows (3600 s / 3 s = 1200) and the read-lane count is 3600 s / 0.5 s = 7200.  The daily
// count is the runaway guard:  twelve hours at that hourly count, then the seat waits for the window to roll.
// These are gateway counts of logical tool calls, not provider request counts.  Provider limits are not set
// here and are answered by the cooldown (noteRateLimited).  Linear's documented OAuth-app limit is 5,000
// requests an hour per app user, read and write together;  linear_read + linear_write is sized below that as a
// heuristic only, since one tool call can make several requests.  FleetLink's own backend limits are separate.
const WRITES_PER_HOUR = HOUR_MS / WRITE_SPACING_MS;
const READS_PER_HOUR = HOUR_MS / READ_SPACING_MS;
export const BUDGETS = Object.freeze({
  write: Object.freeze([
    { windowMs: HOUR_MS, limit: WRITES_PER_HOUR },
    { windowMs: DAY_MS, limit: WRITES_PER_HOUR * 12 },
  ]),
  react: Object.freeze([
    { windowMs: HOUR_MS, limit: WRITES_PER_HOUR },
    { windowMs: DAY_MS, limit: WRITES_PER_HOUR * 12 },
  ]),
  recall: Object.freeze([
    { windowMs: HOUR_MS, limit: WRITES_PER_HOUR },
    { windowMs: DAY_MS, limit: WRITES_PER_HOUR * 12 },
  ]),
  read: Object.freeze([
    { windowMs: HOUR_MS, limit: READS_PER_HOUR },
    { windowMs: DAY_MS, limit: READS_PER_HOUR * 12 },
  ]),
  linear_write: Object.freeze([
    { windowMs: HOUR_MS, limit: WRITES_PER_HOUR },
    { windowMs: DAY_MS, limit: WRITES_PER_HOUR * 12 },
  ]),
  linear_read: Object.freeze([
    { windowMs: HOUR_MS, limit: 3000 },
    { windowMs: DAY_MS, limit: 36000 },
  ]),
  // FleetLink writes (AFC-328):  shares, files, upload URLs.  Own lane, read spacing.
  fleetlink: Object.freeze([
    { windowMs: HOUR_MS, limit: 3600 },
    { windowMs: DAY_MS, limit: 43200 },
  ]),
});

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

/**
 * Take a slot for one tool call of `kind` ("read", "write", "react" or "recall").
 * Returns {ok: true, waitMs} (the caller sleeps that long, at most 6 seconds,
 * then calls Zulip), or {ok: false, code, retryAfterS?, resetAt?}.  The slot
 * and the budget entry are written before this returns, and the method does no
 * outside I/O, so concurrent calls of one seat get distinct slots.
 */
export async function reserve(store, { kind, policy }, now = Date.now()) {
  if (!Object.hasOwn(BUDGETS, kind)) throw new Error("unknown call kind");
  if ((await store.get("paused")) === true) return { ok: false, code: "paused" };
  const cooldown = await store.get("cooldown_until");
  if (Number.isFinite(cooldown) && cooldown > now) {
    return { ok: false, code: "rate_limited", retryAfterS: Math.max(1, Math.ceil((cooldown - now) / 1000)) };
  }
  // A seat's policy only ever raises a default (owner 2026-10-19):  each window is the larger of the two.
  const atLeast = (windowMs, limit) => Math.max(limit, BUDGETS[kind].find((w) => w.windowMs === windowMs)?.limit ?? 0);
  const windows = policy
    ? [{ windowMs: HOUR_MS, limit: atLeast(HOUR_MS, policy.hour) },
      ...(policy.day ? [{ windowMs: DAY_MS, limit: atLeast(DAY_MS, policy.day) }]
        : BUDGETS[kind].filter((w) => w.windowMs === DAY_MS))]
    : BUDGETS[kind];
  const longest = Math.max(...windows.map((w) => w.windowMs));
  const stamp = (entry) => Array.isArray(entry) ? entry[0] : entry;
  const count = (entry) => Array.isArray(entry) ? entry[1] : 1;
  let history = ((await store.get(`budget:${kind}`)) ?? []).filter((entry) =>
    Number.isFinite(stamp(entry)) && Number.isInteger(count(entry)) && count(entry) > 0
    && stamp(entry) > now - longest && stamp(entry) <= now + MINUTE_MS);
  // Minute buckets bound storage for every budget (a day holds at most 1,440 buckets however many calls were
  // made), so raised counts cannot grow a seat's state toward the Durable Object storage limit.  Round up so
  // expiry is conservative by at most a minute, never an early extra allowance.
  {
    const buckets = new Map();
    for (const entry of history) {
      const minute = Math.ceil(stamp(entry) / MINUTE_MS) * MINUTE_MS;
      buckets.set(minute, (buckets.get(minute) ?? 0) + count(entry));
    }
    history = [...buckets];
  }
  for (const { windowMs, limit } of windows) {
    const inWindow = history.filter((entry) => stamp(entry) > now - windowMs);
    if (inWindow.reduce((total, entry) => total + count(entry), 0) >= limit) {
      return { ok: false, code: "budget_exhausted", resetAt: iso(Math.min(...inWindow.map(stamp)) + windowMs), window: windowMs === HOUR_MS ? "hour" : "day" };
    }
  }
  const lane = kind.startsWith("linear_") || kind === "fleetlink" ? kind : kind === "read" ? "read" : "write";
  const spacing = policy?.spacingMs ?? (kind.endsWith("read") || kind === "fleetlink" ? READ_SPACING_MS : WRITE_SPACING_MS);
  let last = await store.get(`last:${lane}`);
  if (!Number.isFinite(last) || last > now + Math.max(MAX_WAIT_MS, 10 * spacing)) last = null; // the clock moved back
  const slot = last === null ? now : Math.max(now, last + spacing);
  const waitMs = slot - now;
  if (waitMs > MAX_WAIT_MS) {
    return { ok: false, code: "rate_limited", retryAfterS: Math.max(1, Math.ceil((waitMs - MAX_WAIT_MS) / 1000)) };
  }
  {
    const minute = Math.ceil(now / MINUTE_MS) * MINUTE_MS;
    const bucket = history.find((entry) => stamp(entry) === minute);
    if (bucket) bucket[1]++;
    else history.push([minute, 1]);
  }
  await store.put(`last:${lane}`, slot);
  await store.put(`budget:${kind}`, history);
  return { ok: true, waitMs };
}

// ---- shared provider quota (a separate Durable Object name per provider identity, never a seat)
//
// The Linear API limits requests per user or app user (5,000 an hour, plus a complexity budget), so the
// gateway meters ACTUAL provider requests, one per fetch, charged before the fetch is sent.  The bucket lives
// in a SeatGate instance named `providerquota:linear:<identity>` (src/gate.js), where <identity> is a hash of
// the workspace and the app user id that Linear itself returned, or the shared "unknown" bucket used until
// that answer is known.  Storage operations only, so the input gate makes each reserve atomic:  two concurrent
// requests can never both take the last slot.  Minute buckets bound the stored row to 60 entries.
export const PROVIDER_QUOTA_MAX_LIMIT = 1_000_000;
export async function providerQuotaReserve(store, { limit, n = 1, force = false } = {}, now = Date.now()) {
  if (!Number.isInteger(limit) || limit < 1 || limit > PROVIDER_QUOTA_MAX_LIMIT) throw new Error("invalid provider quota limit");
  if (!Number.isInteger(n) || n < 1 || n > 100) throw new Error("invalid provider quota count");
  const cooldown = await store.get("quota_cooldown_until");
  // `force` is a DEBIT, not a reservation:  the request already went out (a whoami that proved which user it
  // spent), so it is recorded even over the limit or in a cooldown, and the next reserve then refuses.
  if (!force && Number.isFinite(cooldown) && cooldown > now) {
    return { ok: false, code: "rate_limited", retryAfterS: Math.max(1, Math.ceil((cooldown - now) / 1000)) };
  }
  const buckets = new Map();
  for (const entry of (await store.get("quota_buckets")) ?? []) {
    const minute = Array.isArray(entry) ? entry[0] : NaN;
    const count = Array.isArray(entry) ? entry[1] : NaN;
    if (!Number.isFinite(minute) || !Number.isInteger(count) || count < 1) continue;
    if (minute <= now - HOUR_MS || minute > now + MINUTE_MS) continue;
    buckets.set(minute, (buckets.get(minute) ?? 0) + count);
  }
  let used = 0;
  for (const count of buckets.values()) used += count;
  if (!force && used + n > limit) {
    return { ok: false, code: "budget_exhausted", window: "hour", resetAt: iso(Math.min(...buckets.keys()) + HOUR_MS) };
  }
  const minute = Math.ceil(now / MINUTE_MS) * MINUTE_MS;
  buckets.set(minute, (buckets.get(minute) ?? 0) + n);
  await store.put("quota_buckets", [...buckets]);
  return { ok: true, used: used + n };
}

/** The provider answered 429:  every caller of this identity backs off for its retry-after (1 s to 1 h). */
export async function providerQuotaNoteRateLimited(store, { retryAfterS } = {}, now = Date.now()) {
  const seconds = Number.isFinite(retryAfterS) ? Math.min(Math.max(Math.ceil(retryAfterS), 1), 3600) : 30;
  const until = now + seconds * 1000;
  const current = await store.get("quota_cooldown_until");
  if (!Number.isFinite(current) || current < until) await store.put("quota_cooldown_until", until);
  return { until };
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

// ---- per-key Linear GraphQL mutation idempotency rows

const linearApiDay = (now) => Math.floor(now / DAY_MS);
const linearApiPrefix = "linear-api:";
const linearApiStorageKey = (day, key) => `${linearApiPrefix}${String(day).padStart(10, "0")}:${key}`;

function validLinearApiKey(key) {
  return typeof key === "string" && /^[a-f0-9]{64}$/i.test(key);
}

async function cleanupLinearApiRows(store, day, now) {
  if (typeof store.list !== "function" || typeof store.delete !== "function") return;
  const last = await store.get("linear-api-cleanup-at");
  if (Number.isFinite(last) && now - last < LINEAR_API_CLEANUP_INTERVAL_MS) return;
  const end = linearApiStorageKey(day - 1, "");
  const rows = await store.list({ prefix: linearApiPrefix, end, limit: 64 });
  const keys = [...rows.keys()];
  if (keys.length) await store.delete(keys);
  await store.put("linear-api-cleanup-at", now);
}

/** Start or inspect one generic Linear mutation idempotency record. */
export async function linearApiBegin(store, { key, request_sha, operation }, now = Date.now()) {
  if (!validLinearApiKey(key)) throw new TypeError("Linear API idempotency key must be a 64-character SHA-256 hex digest.");
  await cleanupLinearApiRows(store, linearApiDay(now), now);
  const day = linearApiDay(now);
  return store.transaction(async (txn) => {
    const candidates = [linearApiStorageKey(day, key), linearApiStorageKey(day - 1, key)];
    let storageKey;
    let row;
    for (const candidate of candidates) {
      const existing = await txn.get(candidate);
      if (existing && existing.expires > now) {
        storageKey = candidate;
        row = existing;
        break;
      }
    }

    if (row) {
      if (row.request_sha !== request_sha) return { verdict: "conflict", storageKey, row: { ...row } };
      if (row.state === "sent") return { verdict: "duplicate", storageKey, row: { ...row } };
      if (row.state === "pending" && now - (row.claimed_at ?? 0) < PENDING_FRESH_MS) {
        return { verdict: "busy", storageKey, row: { ...row } };
      }
      if (row.state === "pending" || row.state === "unknown") return { verdict: "reconcile", storageKey, row: { ...row } };
    }

    storageKey = linearApiStorageKey(day, key);
    row = { request_sha, operation: String(operation ?? ""), state: "pending", claimed_at: now, expires: now + DAY_MS };
    await txn.put(storageKey, row);
    return { verdict: "send", storageKey, row: { ...row } };
  });
}

/** Update a per-key Linear mutation record without enlarging it unboundedly. */
export async function linearApiSet(store, { storageKey, changes }, now = Date.now()) {
  if (typeof storageKey !== "string" || !storageKey.startsWith(linearApiPrefix)) throw new TypeError("Invalid Linear API storage key.");
  if (!changes || typeof changes !== "object" || Array.isArray(changes)) throw new TypeError("Linear API changes must be an object.");
  for (const name of Object.keys(changes)) {
    if (name !== "state" && name !== "data") throw new TypeError("Linear API changes may update only state and data.");
  }
  if (Object.hasOwn(changes, "state") && !["pending", "unknown", "sent"].includes(changes.state)) {
    throw new TypeError("Invalid Linear API mutation state.");
  }
  if (Object.hasOwn(changes, "data")) {
    let encoded;
    try { encoded = JSON.stringify(changes.data); } catch { throw new TypeError("Linear API cached data must be JSON data."); }
    if (typeof encoded !== "string" || new TextEncoder().encode(encoded).byteLength > LINEAR_API_DATA_MAX_BYTES) {
      throw new TypeError("Linear API cached data exceeds 8192 bytes.");
    }
  }
  const row = await store.get(storageKey);
  if (!row) return null;
  Object.assign(row, changes);
  await store.put(storageKey, row);
  return { ...row };
}

/** Remove one exact per-key Linear mutation record. */
export async function linearApiDrop(store, { storageKey }, now = Date.now()) {
  if (typeof storageKey !== "string" || !storageKey.startsWith(linearApiPrefix)) throw new TypeError("Invalid Linear API storage key.");
  await store.delete(storageKey);
  return { dropped: true };
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
  const row = { ok: entry.ok === true, user_id: entry.user_id ?? null, role: entry.role ?? null, reason: entry.reason ?? "", owner: entry.owner ?? null, checked_at: now };
  await store.put("role", row);
  if (!prior || prior.ok !== row.ok || prior.role !== row.role) {
    await audit(store, { event: row.ok ? "role_ok" : "role_refused", role: row.role, reason: row.reason }, now);
  }
  return row;
}

// ---- the seat's Linear token (AFC-289)

// The cached Linear token for one seat.  It lives in the Durable Object, not in
// KV, because the DO's input gate is what stops two requests of one seat from
// minting at the same time:  Linear allows one mint per app per 24 hours, and
// what happens past that cap is undocumented.  The row holds only what this
// server needs to decide whether it may reuse the token.
export async function linearTokenGet(store) {
  const cached = await store.get("linear_token");
  if (!cached || typeof cached.access_token !== "string" || !cached.access_token) return null;
  return {
    access_token: cached.access_token,
    minted_at: Number(cached.minted_at) || 0,
    expires_at: Number(cached.expires_at) || 0,
    mint_count: Number(cached.mint_count) || 0,
    cred_fp: String(cached.cred_fp ?? ""),
  };
}

export async function linearTokenSet(store, entry) {
  const row = {
    access_token: String(entry?.access_token ?? ""),
    minted_at: Number(entry?.minted_at) || 0,
    expires_at: Number(entry?.expires_at) || 0,
    mint_count: Number(entry?.mint_count) || 0,
    cred_fp: String(entry?.cred_fp ?? ""),
  };
  await store.put("linear_token", row);
  return { ...row };
}

// The last mint outcome per seat, for the health route (src/health.js).  Only a
// fixed kind code and two times:  never Linear's message text.
export async function linearMintNote(store, { ok, kind = null, at } = {}) {
  const row = (await store.get("linear_mint_note")) ?? {};
  const t = Math.floor(Number(at) / 1000) || 0;
  if (ok) row.last_ok_at = t;
  else {
    row.last_fail_at = t;
    row.last_fail_kind = typeof kind === "string" ? kind.slice(0, 20) : "unexpected";
  }
  await store.put("linear_mint_note", {
    last_ok_at: Number(row.last_ok_at) || 0,
    last_fail_at: Number(row.last_fail_at) || 0,
    last_fail_kind: typeof row.last_fail_kind === "string" ? row.last_fail_kind : null,
  });
}

/** Token ages and the last mint outcome, with no token value. */
export async function linearHealth(store) {
  const cached = await linearTokenGet(store);
  const note = (await store.get("linear_mint_note")) ?? {};
  return {
    token: cached ? { minted_at: cached.minted_at, expires_at: cached.expires_at, mint_count: cached.mint_count } : null,
    note: { last_ok_at: Number(note.last_ok_at) || 0, last_fail_at: Number(note.last_fail_at) || 0, last_fail_kind: typeof note.last_fail_kind === "string" ? note.last_fail_kind : null },
  };
}

// Which Linear user the cached token acts as, proven by a whoami and bound to the token's own fingerprint, so
// every isolate of the seat reads one proof (src/linear.js).  Holds no token, only a fingerprint and a hash.
export async function linearIdentityGet(store) {
  const row = await store.get("linear_identity");
  if (!row || typeof row.fp !== "string" || !/^[0-9a-f]{32}$/.test(row.fp) || typeof row.identity !== "string" || !/^[0-9a-f]{64}$/.test(row.identity)) return null;
  return { fp: row.fp, identity: row.identity, proven_at: Number(row.proven_at) || 0, valid_until: Number(row.valid_until) || 0 };
}

export async function linearIdentitySet(store, entry) {
  const row = {
    fp: String(entry?.fp ?? ""),
    identity: String(entry?.identity ?? ""),
    proven_at: Number(entry?.proven_at) || 0,
    valid_until: Number(entry?.valid_until) || 0,
  };
  if (!/^[0-9a-f]{32}$/.test(row.fp) || !/^[0-9a-f]{64}$/.test(row.identity)) throw new Error("invalid linear identity");
  await store.put("linear_identity", row);
  return { ...row };
}

export async function linearIdentityClear(store) {
  await store.delete("linear_identity");
  return { cleared: true };
}

/** Forget the token, so the next call mints.  Only the owner needs this. */
export async function linearTokenClear(store) {
  await store.delete("linear_token");
  return { cleared: true };
}

// ---- per-call audit (spec 3.9), kept 90 days

const dayKey = (ms) => new Date(ms).toISOString().slice(0, 10);
const CALL_FIELDS = ["seat", "authenticated_seat", "acting_identity", "grant_ref", "client_id", "tool", "channel_id", "topic", "message_id", "outcome", "error_code", "error_message", "retryable", "retry_after_s", "reset_at", "upstream_status", "exception_class", "latency_ms", "body_len", "idem_ref", "recipient_ids", "asn", "country"];
const utf8JsonBytes = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength;
const arrayJsonBytes = (rows) => 2 + rows.reduce((bytes, row) => bytes + utf8JsonBytes(row), 0) + Math.max(0, rows.length - 1);

/**
 * One row per tool call.  Only the spec 3.9 fields are kept (never a body, a
 * key, a token or a header), and a change of ASN or country within one grant
 * is flagged in the event log.
 */
async function auditCallInner(store, row, now) {
  const clean = { ts: now };
  for (const field of CALL_FIELDS) {
    const value = row[field];
    if (value === undefined || value === null) continue;
    if (field === "error_message") {
      if (typeof value !== "string") continue;
      const message = value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
      clean[field] = scanSecret(message) ? "Error message omitted after secret scan." : [...message].slice(0, ERROR_MESSAGE_MAX).join("");
      continue;
    }
    if (field === "retryable") {
      if (typeof value === "boolean") clean[field] = value;
      continue;
    }
    if (field === "retry_after_s") {
      if (Number.isFinite(value) && value >= 0) clean[field] = Math.min(value, 3600);
      continue;
    }
    if (field === "reset_at") {
      if (Number.isFinite(value)) clean[field] = value;
      else if (typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value))) clean[field] = value;
      continue;
    }
    if (field === "upstream_status") {
      if (Number.isInteger(value) && value >= 100 && value <= 599) clean[field] = value;
      continue;
    }
    if (field === "exception_class") {
      if (typeof value === "string" && /^[A-Za-z][A-Za-z0-9_.]{0,39}$/.test(value)) clean[field] = value;
      continue;
    }
    if (field === "error_code") {
      if (typeof value === "string" && /^[a-z][a-z0-9_]{0,59}$/.test(value)) clean[field] = value;
      continue;
    }
    if (field === "recipient_ids") {
      // The user ids a DM tool addressed (spec 1.2):  integers only, at most 8.  Never a name or a body.
      const ids = Array.isArray(value) ? value.filter((v) => Number.isSafeInteger(v)).slice(0, 8) : [];
      if (ids.length) clean[field] = ids;
      continue;
    }
    if (typeof value === "string") clean[field] = [...value].slice(0, 120).join("");
    else if (typeof value === "number" && Number.isFinite(value)) clean[field] = value;
    else if (typeof value === "boolean") clean[field] = value;
  }
  const day = dayKey(now);
  const days = ((await store.get("call_days")) ?? []).filter((d) => typeof d === "string");
  if (!days.includes(day)) days.push(day);
  const cutoff = dayKey(now - CALL_RETENTION_DAYS * DAY_MS);
  const keep = [];
  for (const d of days.sort()) {
    if (d < cutoff) {
      await store.put(`calls:${d}`, null);
      await store.put(`calls_bytes:${d}`, null);
    }
    else keep.push(d);
  }
  await store.put("call_days", keep);
  const bucket = (await store.get(`calls:${day}`)) ?? [];
  const meta = await store.get(`calls_bytes:${day}`);
  let encodedBytes = meta && meta.count === bucket.length && Number.isSafeInteger(meta.bytes) && meta.bytes >= 0
    ? meta.bytes
    : arrayJsonBytes(bucket);
  bucket.push(clean);
  encodedBytes += utf8JsonBytes(clean) + (bucket.length > 1 ? 1 : 0);
  while (bucket.length > CALLS_PER_DAY_CAP || encodedBytes > CALLS_STORAGE_MAX_BYTES) {
    if (bucket.length <= 1) break; // A single sanitized audit row is far below the byte cap.
    encodedBytes -= utf8JsonBytes(bucket.shift()) + 1;
  }
  await store.put(`calls:${day}`, bucket);
  await store.put(`calls_bytes:${day}`, { count: bucket.length, bytes: encodedBytes });

  if (clean.outcome === "error") {
    const cutoffMs = now - CALL_RETENTION_DAYS * DAY_MS;
    const errors = ((await store.get("errors")) ?? []).filter((entry) => entry && Number.isFinite(entry.ts) && entry.ts > cutoffMs);
    const errorRow = { ts: clean.ts };
    for (const field of ["seat", "authenticated_seat", "acting_identity", "grant_ref", "client_id", "tool", "error_code", "error_message", "retryable", "retry_after_s", "reset_at", "upstream_status", "exception_class"]) {
      if (clean[field] !== undefined) errorRow[field] = clean[field];
    }
    errors.push(errorRow);
    let encodedErrorBytes = arrayJsonBytes(errors);
    while (errors.length > ERROR_AUDIT_CAP || encodedErrorBytes > ERRORS_STORAGE_MAX_BYTES) {
      if (errors.length <= 1) break; // Sanitized error rows are individually well below the cap.
      encodedErrorBytes -= utf8JsonBytes(errors.shift()) + 1;
    }
    await store.put("errors", errors);
  }

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

/** Serialize the full audit read/modify/write sequence across concurrent RPCs. */
export async function auditCall(store, row, now = Date.now()) {
  if (typeof store.transaction !== "function") throw new TypeError("transactional store required");
  return store.transaction((txn) => auditCallInner(txn, row, now));
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

/** The newest retained diagnostic error rows, independent of successful-call volume. */
export async function errorTail(store, limit = 50, now = Date.now()) {
  const max = Number.isInteger(limit) ? Math.max(0, Math.min(limit, ERROR_AUDIT_CAP)) : 50;
  if (max === 0) return [];
  const cutoff = now - CALL_RETENTION_DAYS * DAY_MS;
  const errors = ((await store.get("errors")) ?? []).filter((entry) => entry && Number.isFinite(entry.ts) && entry.ts > cutoff);
  return errors.slice(-max).reverse();
}

const DIAGNOSTIC_CAP = 100;
const SAFE_DIAGNOSTIC_KIND = /^[a-z][a-z0-9_-]{0,39}$/i;
const SAFE_DIAGNOSTIC_REASON = /^[a-z0-9_:-]{1,80}$/i;
const SAFE_DIAGNOSTIC_SEAT = /^[A-Z][A-Z0-9_-]{0,39}$/;

/** Aggregate safe, low-cardinality failures in the global log gate. */
async function recordDiagnosticInner(store, { kind, reason, seat, status } = {}, now = Date.now()) {
  if (typeof kind !== "string" || !SAFE_DIAGNOSTIC_KIND.test(kind)) throw new TypeError("invalid diagnostic kind");
  if (typeof reason !== "string" || !SAFE_DIAGNOSTIC_REASON.test(reason)) throw new TypeError("invalid diagnostic reason");
  if (seat !== undefined && seat !== null && (typeof seat !== "string" || !SAFE_DIAGNOSTIC_SEAT.test(seat))) throw new TypeError("invalid diagnostic seat");
  if (status !== undefined && status !== null && (!Number.isInteger(status) || status < 100 || status > 599)) throw new TypeError("invalid diagnostic status");

  const cutoff = now - CALL_RETENTION_DAYS * DAY_MS;
  const rows = ((await store.get("diagnostics")) ?? []).filter((row) => row && Number.isFinite(row.ts) && row.ts > cutoff);
  const rowSeat = seat ?? null;
  const key = `${kind}\0${reason}\0${rowSeat ?? ""}`;
  const index = rows.findIndex((row) => `${row.kind}\0${row.reason}\0${row.seat ?? ""}` === key);
  if (index >= 0) {
    const [row] = rows.splice(index, 1);
    row.count = Math.min(Number.MAX_SAFE_INTEGER, (Number.isSafeInteger(row.count) ? row.count : 0) + 1);
    row.ts = now;
    if (status !== undefined && status !== null) row.status = status;
    rows.push(row);
  } else {
    const row = { kind, reason, seat: rowSeat, count: 1, ts: now };
    if (status !== undefined && status !== null) row.status = status;
    rows.push(row);
  }
  while (rows.length > DIAGNOSTIC_CAP) rows.shift();
  await store.put("diagnostics", rows);
}

/** Serialize diagnostic aggregation so concurrent failures cannot lose counts. */
export async function recordDiagnostic(store, entry, now = Date.now()) {
  if (typeof store.transaction !== "function") throw new TypeError("transactional store required");
  return store.transaction((txn) => recordDiagnosticInner(txn, entry, now));
}

/** Newest aggregated diagnostics first, bounded to the retained 90-day window. */
export async function diagnosticTail(store, limit = 40, now = Date.now()) {
  const max = Number.isInteger(limit) ? Math.max(0, Math.min(limit, DIAGNOSTIC_CAP)) : 40;
  if (max === 0) return [];
  const cutoff = now - CALL_RETENTION_DAYS * DAY_MS;
  const rows = ((await store.get("diagnostics")) ?? []).filter((row) => row && Number.isFinite(row.ts) && row.ts > cutoff);
  return rows.slice(-max).reverse();
}

// ---------------------------------------------------------------- MCP Events (src/events.js)
//
// Subscriptions, the callback verification cache and the wake claims.  Storage
// only, like everything above:  the verification and delivery fetches run in
// the Worker, which hands the results back here.

export const WAKE_SEEN_MS = 15 * MINUTE_MS; // > the wake's 5-minute sent_at window
// How long one request holds a wake it claimed.  Longer than the Worker's
// inline delivery (WAKE_BUDGET_MS in events.js, 10 seconds from the request's
// arrival), so a live delivery is never claimed twice.  Shorter than the
// listener's retry span (2, 5 and 10 seconds of backoff after its first
// failure), so the listener's last retry claims again a wake whose isolate died
// mid-delivery.
export const WAKE_LEASE_MS = 15 * 1000;
const WAKE_SEEN_CAP = 500;
// Every way a claim can end (eventSettleWake).  "retry" forgets the wake;  the rest settle it.
export const WAKE_OUTCOMES = Object.freeze(["delivered", "no_subscriber", "rejected", "gone", "not_live", "channel_refused", "retry"]);
const VERIFIED_CAP = 20;
const subRef = (id) => String(id ?? "").slice(0, 16);

/**
 * The seat's subscriptions that may still receive events.  `dropped` maps an
 * id to why it may not:  past refresh_before ("expired"), made under an older
 * grant epoch ("stale_epoch", Revoke All And Bump Epoch), or made by a grant
 * past GRANT_MAX_AGE_MS ("grant_too_old").  The caller persists the removal.
 */
async function liveSubs(store, now) {
  const subs = (await store.get("event_subs")) ?? {};
  const { epoch } = await read(store);
  const dropped = {};
  for (const [id, sub] of Object.entries(subs)) {
    let why = null;
    if (!sub || !Number.isFinite(sub.refresh_before) || sub.refresh_before <= now) why = "expired";
    else if (sub.epoch !== epoch) why = "stale_epoch";
    else if (!Number.isFinite(sub.approved_at) || !(now - sub.approved_at < GRANT_MAX_AGE_MS)) why = "grant_too_old";
    if (why) {
      dropped[id] = why;
      delete subs[id];
    }
  }
  return { subs, dropped };
}

async function persistDropped(store, subs, dropped, now) {
  const ids = Object.keys(dropped);
  if (ids.length === 0) return;
  await store.put("event_subs", subs);
  for (const id of ids) await audit(store, { event: "event_sub_dropped", sub_ref: subRef(id), reason: dropped[id] }, now);
}

/** The live subscriptions for one event name (AFC-477:  the cap and the fan-out are per name). */
const subsNamed = (subs, name) => Object.values(subs).filter((sub) => subEventName(sub) === name);

function waConversation(subs, name, url) {
  const wa = subsNamed(subs, WA_EVENT);
  if (name === WA_EVENT) {
    const zulip = subsNamed(subs, EVENT_NAME);
    return zulip.length === 1 && zulip[0].url === url;
  }
  return name !== EVENT_NAME || wa.every(sub => sub.url === url);
}

export async function eventSubscriptions(store, {}, now = Date.now()) {
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  return Object.values(subs);
}

/**
 * Is there room for subscription `id` of event `name`?  It exists already (a
 * refresh), or the seat is under the cap for that name.  With a cap of one, a
 * second chat for the same event is refused rather than replacing the first
 * (`until` says when the live one lapses).  Another event's subscription
 * never counts.
 */
export async function eventSubRoom(store, { id, max = 1, name = EVENT_NAME, url }, now = Date.now()) {
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  if (!waConversation(subs, name, url)) return { ok: false, reason: "conversation" };
  const same = subsNamed(subs, name);
  if (Object.hasOwn(subs, id) || same.length < max) return { ok: true };
  return { ok: false, until: Math.min(...same.map((sub) => sub.refresh_before)) };
}

/**
 * Create or refresh a subscription (idempotent by its deterministic id).  A
 * refresh with a new secret keeps the old one for `rotationMs` so deliveries
 * carry both signatures.  Returns {ok: true, refreshBefore, refreshed} or
 * {ok: false, reason: "cap"}.
 */
export async function eventSubUpsert(store, { sub, ttlMs, rotationMs = 10 * MINUTE_MS, max = 1 }, now = Date.now()) {
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  if (!waConversation(subs, subEventName(sub), sub.url)) return { ok: false, reason: "conversation" };
  const prior = subs[sub.id];
  if (!prior && subsNamed(subs, subEventName(sub)).length >= max) {
    await audit(store, { event: "event_subscribe_refused", sub_ref: subRef(sub.id), reason: "cap" }, now);
    return { ok: false, reason: "cap" };
  }
  const row = {
    id: sub.id,
    name: sub.name,
    arguments: sub.arguments ?? {},
    url: sub.url,
    host: sub.host,
    secret: sub.secret,
    epoch: sub.epoch,
    approved_at: sub.approved_at,
    client_id: sub.client_id ?? "",
    created_at: prior?.created_at ?? now,
    refresh_before: now + ttlMs,
    last_outcome: prior?.last_outcome ?? null,
    last_status: prior?.last_status ?? null,
    last_at: prior?.last_at ?? null,
  };
  if (prior && prior.secret !== sub.secret) {
    row.prev_secret = prior.secret;
    row.prev_until = now + rotationMs;
  } else if (prior && Number.isFinite(prior.prev_until) && prior.prev_until > now) {
    row.prev_secret = prior.prev_secret;
    row.prev_until = prior.prev_until;
  }
  subs[sub.id] = row;
  await store.put("event_subs", subs);
  await audit(store, { event: prior ? "event_refresh" : "event_subscribe", sub_ref: subRef(sub.id), host: sub.host, ttl_s: Math.round(ttlMs / 1000), ...(row.prev_secret && !prior?.prev_secret ? { rotated: true } : {}) }, now);
  return { ok: true, refreshBefore: row.refresh_before, refreshed: Boolean(prior) };
}

/** Remove a subscription.  Idempotent. */
export async function eventSubDelete(store, { id, reason = "unsubscribe" }, now = Date.now()) {
  const subs = (await store.get("event_subs")) ?? {};
  if (!Object.hasOwn(subs, id)) return { removed: false };
  delete subs[id];
  await store.put("event_subs", subs);
  await audit(store, { event: reason === "unsubscribe" ? "event_unsubscribe" : "event_sub_dropped", sub_ref: subRef(id), reason }, now);
  return { removed: true };
}

export async function eventVerifiedGet(store, { ref }, now = Date.now()) {
  const cache = (await store.get("event_verified")) ?? {};
  return Number.isFinite(cache[ref]) && cache[ref] > now;
}

export async function eventVerifiedSet(store, { ref, ttlMs }, now = Date.now()) {
  const cache = (await store.get("event_verified")) ?? {};
  for (const [k, until] of Object.entries(cache)) if (!(until > now)) delete cache[k];
  delete cache[ref];
  cache[ref] = now + ttlMs;
  const keys = Object.keys(cache);
  while (keys.length > VERIFIED_CAP) delete cache[keys.shift()];
  await store.put("event_verified", cache);
}

const wakeRefOf = (wakeId) => String(wakeId ?? "").slice(0, 40);

/**
 * One `wake_seen` entry:  {s: "claimed", at, until, token} while a request
 * delivers the wake, {s: "settled", at, outcome} once it is done.  A bare
 * number (what the Worker wrote before claims had a lease) counts as settled.
 * Anything else is null.
 */
function wakeEntry(value) {
  if (Number.isFinite(value)) return { s: "settled", at: value, outcome: "legacy" };
  if (!value || typeof value !== "object" || !Number.isFinite(value.at)) return null;
  return value.s === "claimed" || value.s === "settled" ? value : null;
}

/** The claims of the last WAKE_SEEN_MS, oldest first. */
async function wakeSeen(store, now) {
  const seen = (await store.get("wake_seen")) ?? {};
  for (const [k, value] of Object.entries(seen)) {
    const entry = wakeEntry(value);
    if (!entry || !(entry.at > now - WAKE_SEEN_MS && entry.at <= now + MINUTE_MS)) delete seen[k];
  }
  return seen;
}

async function putWakeSeen(store, seen) {
  const keys = Object.keys(seen);
  while (keys.length > WAKE_SEEN_CAP) delete seen[keys.shift()];
  await store.put("wake_seen", seen);
}

/**
 * Claim one wake for delivery.  A settled wake_id (the listener retrying after
 * a lost response) is "duplicate".  A claim whose lease is still running is
 * "in_flight":  another request is delivering it, and the listener retries
 * later.  A claim whose lease ran out (that request died mid-delivery) is
 * claimed again.  The claim holds until eventSettleWake, which only its
 * `token` may call, so a wake is remembered once it was delivered or dropped,
 * never merely because it arrived.
 * A paused seat delivers nothing:  its wake settles at once.  Subscriptions
 * past refresh_before, from a grant epoch that is no longer current (Revoke
 * All And Bump Epoch), or from a grant past its 90-day age are dropped here
 * (liveSubs), and eventSubLive checks each one again before every attempt.
 * Only subscriptions to `name` (zulip.mention) are returned and counted, so a
 * spike subscription (AFC-477) never changes a wake's answer.
 * Returns {verdict: "go", subs, token}, {verdict: "duplicate"},
 * {verdict: "in_flight", retryAfterS} or {verdict: "paused"}.
 */
export async function eventClaimWake(store, { wakeId, messageId = null, name = EVENT_NAME }, now = Date.now()) {
  const seen = await wakeSeen(store, now);
  const prior = wakeEntry(seen[wakeId]);
  if (prior?.s === "settled") return { verdict: "duplicate" };
  if (prior && Number.isFinite(prior.until) && prior.until > now) {
    return { verdict: "in_flight", retryAfterS: Math.max(1, Math.ceil((prior.until - now) / 1000)) };
  }
  delete seen[wakeId]; // written again last, so the cap trims older wakes first
  const wakeRef = wakeRefOf(wakeId);
  const s = await read(store);
  if (s.paused) {
    seen[wakeId] = { s: "settled", at: now, outcome: "paused" };
    await putWakeSeen(store, seen);
    await audit(store, { event: "event_wake_paused", wake_ref: wakeRef }, now);
    return { verdict: "paused" };
  }
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  const mine = subsNamed(subs, name);
  const token = crypto.randomUUID();
  seen[wakeId] = { s: "claimed", at: now, until: now + WAKE_LEASE_MS, token };
  await putWakeSeen(store, seen);
  await audit(store, { event: "event_wake_captured", wake_ref: wakeRef, message_id: messageId ?? null, subscribers: mine.length, ...(prior ? { reclaimed: true } : {}) }, now);
  return { verdict: "go", token, subs: mine.map((sub) => ({ ...sub })) };
}

/**
 * End a claim (WAKE_OUTCOMES).  Only the request that holds it (`token`) may:
 * one whose lease ran out and was claimed again changes nothing.
 * "retry" (no delivery, and one failed in a way worth retrying) forgets the
 * wake, so the listener's retry claims it again.  Every other outcome settles
 * it, and a repeat is "duplicate" from then on.  "channel_refused" is audited
 * with the stream's numeric id only.
 * Returns {ok: true}, or {ok: false} when the claim is no longer this one's.
 */
export async function eventSettleWake(store, { wakeId, token, outcome, delivered = 0, streamId = null }, now = Date.now()) {
  if (!WAKE_OUTCOMES.includes(outcome)) throw new Error("unknown wake outcome");
  const seen = await wakeSeen(store, now);
  const entry = wakeEntry(seen[wakeId]);
  const wakeRef = wakeRefOf(wakeId);
  if (!entry || entry.s !== "claimed" || typeof token !== "string" || entry.token !== token) {
    await audit(store, { event: "event_wake_settle_stale", wake_ref: wakeRef, outcome }, now);
    return { ok: false };
  }
  if (outcome === "retry") {
    delete seen[wakeId];
    await putWakeSeen(store, seen);
    await audit(store, { event: "event_wake_released", wake_ref: wakeRef }, now);
    return { ok: true };
  }
  delete seen[wakeId];
  seen[wakeId] = { s: "settled", at: now, outcome };
  await putWakeSeen(store, seen);
  if (outcome === "channel_refused") {
    await audit(store, { event: "event_wake_channel_refused", wake_ref: wakeRef, stream_id: Number.isSafeInteger(streamId) ? streamId : null }, now);
  } else {
    await audit(store, { event: "event_wake_settled", wake_ref: wakeRef, outcome, delivered: Number.isSafeInteger(delivered) ? delivered : 0 }, now);
  }
  return { ok: true };
}

/**
 * May subscription `subId` still receive an event?  The Worker asks before
 * every delivery attempt, the first included, so a pause, Revoke All And Bump
 * Epoch, an unsubscribe, a lapsed refresh_before or a grant past its 90-day
 * age also stops the retries of a wake claimed before it.
 * Returns {live: true} or {live: false, reason}.
 */
export async function eventSubLive(store, { subId }, now = Date.now()) {
  if ((await store.get("paused")) === true) return { live: false, reason: "paused" };
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  if (Object.hasOwn(subs, subId)) return { live: true };
  return { live: false, reason: dropped[subId] ?? "removed" };
}

/** Record one delivery.  410 Gone drops the subscription;  nothing else does. */
export async function eventDeliveryResult(store, { subId, outcome, status, attempts, reason, wakeRef, messageId }, now = Date.now()) {
  const subs = (await store.get("event_subs")) ?? {};
  const sub = subs[subId];
  if (sub) {
    if (outcome === "gone") delete subs[subId];
    else Object.assign(sub, { last_outcome: outcome, last_status: status ?? null, last_at: now });
    await store.put("event_subs", subs);
  }
  await audit(store, { event: "event_delivery", sub_ref: subRef(subId), outcome, status: status ?? null, attempts: attempts ?? 0, ...(reason ? { reason: String(reason).slice(0, 40) } : {}), wake_ref: wakeRef ?? "", message_id: messageId ?? null }, now);
  if (sub && outcome === "gone") await audit(store, { event: "event_sub_dropped", sub_ref: subRef(subId), reason: "gone_410" }, now);
}

// ---------------------------------------------------------------- AFC-477 spike (src/spike.js)
//
// One signed, hand-fired `fleet.spike.ping` at a time.  A ledger of the ids
// fired in the last `windowMs` is both the dedupe (a replayed id is a
// duplicate, and the body's 5-minute sent_at window is far shorter than the
// ledger) and the rate limit (at most `max` ids per window).  Temporary:  it
// goes with the spike.

export const SPIKE_LEDGER_CAP = 50;

/**
 * Claim one spike for delivery.  A repeated id is "duplicate".  A paused seat
 * delivers nothing ("paused", and the id is spent).  Past `max` ids in the
 * window it is "rate_limited", with the seconds until the oldest leaves the
 * window, and the id is not spent.  Otherwise the id is recorded and the live
 * `fleet.spike.ping` subscriptions come back, never another event's.
 * Returns {verdict: "go", subs}, {verdict: "duplicate"}, {verdict: "paused"}
 * or {verdict: "rate_limited", retryAfterS}.
 */
export async function spikeClaim(store, { spikeId, max = 3, windowMs = 60 * MINUTE_MS }, now = Date.now()) {
  if (typeof spikeId !== "string" || spikeId.length === 0 || spikeId.length > 64) throw new Error("bad spike id");
  const ledger = (await store.get("spike_seen")) ?? {};
  for (const [id, at] of Object.entries(ledger)) if (!(Number.isFinite(at) && at > now - windowMs && at <= now + MINUTE_MS)) delete ledger[id];
  const ref = spikeId.slice(0, 8);
  if (Object.hasOwn(ledger, spikeId)) return { verdict: "duplicate" };
  const putLedger = async () => {
    const keys = Object.keys(ledger);
    while (keys.length > SPIKE_LEDGER_CAP) delete ledger[keys.shift()];
    await store.put("spike_seen", ledger);
  };
  if ((await read(store)).paused) {
    ledger[spikeId] = now;
    await putLedger();
    await audit(store, { event: "spike_paused", spike_ref: ref }, now);
    return { verdict: "paused" };
  }
  const times = Object.values(ledger);
  if (times.length >= max) {
    await audit(store, { event: "spike_rate_limited", spike_ref: ref }, now);
    return { verdict: "rate_limited", retryAfterS: Math.max(1, Math.ceil((Math.min(...times) + windowMs - now) / 1000)) };
  }
  ledger[spikeId] = now;
  await putLedger();
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  const mine = subsNamed(subs, SPIKE_EVENT_NAME);
  await audit(store, { event: "spike_captured", spike_ref: ref, subscribers: mine.length }, now);
  return { verdict: "go", subs: mine.map((sub) => ({ ...sub })) };
}

/** Live subscriptions for /admin:  never the URL path or the secret. */
export async function eventSubsSummary(store, now = Date.now()) {
  const { subs } = await liveSubs(store, now);
  return Object.values(subs).map((sub) => ({
    sub_ref: subRef(sub.id),
    name: subEventName(sub),
    host: sub.host,
    arguments: sub.arguments,
    refresh_before: sub.refresh_before,
    last_outcome: sub.last_outcome,
    last_status: sub.last_status,
    last_at: sub.last_at,
  }));
}

/** A Map-backed store for tests. */
export function memoryStore() {
  const map = new Map();
  let transactionTail = Promise.resolve();
  const store = {
    map,
    async get(key) {
      const v = map.get(key);
      return v === undefined ? undefined : structuredClone(v);
    },
    async put(key, value) {
      map.set(key, structuredClone(value));
    },
    async delete(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) map.delete(key);
    },
    async list({ prefix = "", start, end, limit = 1000 } = {}) {
      return new Map([...map.entries()].filter(([key]) => key.startsWith(prefix) && (!start || key >= start) && (!end || key < end))
        .sort(([a], [b]) => a.localeCompare(b)).slice(0, limit).map(([key, value]) => [key, structuredClone(value)]));
    },
    transaction(callback) {
      const result = transactionTail.then(() => callback(store));
      transactionTail = result.catch(() => undefined);
      return result;
    },
  };
  return store;
}

// ---- per-bot MCP bearer (src/gb-bearer.js):  the SHA-256 only, never the token

const GB_BEARER_KEY = "gb_bearer";

function sameHash(a, b) {
  if (a.length !== b.length || a.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Install `hash` as the persona's bearer, replacing any earlier one (rotate).  Audits who and when, never the hash. */
export async function gbBearerMint(store, { hash, by, expiresAt, mcpAccept }, now = Date.now()) {
  const prev = (await store.get(GB_BEARER_KEY)) ?? null;
  const row = {
    hash: String(hash),
    created_at: now,
    created_by: String(by ?? ""),
    expires_at: Number(expiresAt) || now,
    last_used_at: 0,
    mint_count: Number(prev?.mint_count ?? 0) + 1,
    // Whether this bearer is ALSO accepted on /mcp itself.  A Grok Bot persona's
    // bearer is its only credential, so persona mints carry mcp_accept: true from
    // the existing admin UI.  For an OAuth-capable seat this is the recorded
    // owner choice (owner direction 2026-10-23:  one credential per identity,
    // shared between ST-MCP and GR-MCP):  accepting a bearer in place of the
    // seat's consent flow is opt-in per seat, never inferred.  Absent on rows
    // minted before this field;  the /mcp seat path must treat absent as false.
    mcp_accept: mcpAccept === true,
  };
  await store.put(GB_BEARER_KEY, row);
  await audit(store, { event: "gb_bearer_minted", by: row.created_by, expires_at: row.expires_at, replaced: Boolean(prev), mcp_accept: row.mcp_accept }, now);
  return { created_at: row.created_at, expires_at: row.expires_at, mint_count: row.mint_count, mcp_accept: row.mcp_accept };
}

// ---------------------------------------------------------------- single-use mint forms
//
// Every rendered mint form carries a single-use mint_id (12 hex ms timestamp +
// 20 random hex).  Check-consume-mint is ONE transaction in the identity's own
// gate, so a concurrent or retried submission can interleave nowhere and a
// failure rolls the consume back together with the mint.  A stale (older than
// the form TTL) or already-consumed form is REFUSED:  the bearer row is never
// touched and the refusal is only audited, so an accidental refresh, resubmit
// or days-later replay can never rotate a live bearer.

const GB_MINT_FORMS_KEY = "gb_bearer_mint_forms";
export const GB_MINT_FORM_TTL_MS = 60 * 60 * 1000;
// Fail-closed capacity:  a consumed form's record is kept for its WHOLE
// validity window - only entries already too old to ever mint again may be
// evicted - and when 64 still-valid records stand, further mints are refused
// ("busy") rather than evicting a live tombstone.  Single-use never lapses
// early.  64 real mints inside one hour is far past any deliberate use;  each
// one needs an authenticated admin POST.
const GB_MINT_FORM_KEEP = 64;
const GB_MINT_FORM_SKEW_MS = 60_000;

function mintFormTs(mintId) {
  const ts = Number.parseInt(String(mintId ?? "").slice(0, 12), 16);
  return Number.isFinite(ts) ? ts : null;
}

const GB_MINT_ID_RE = /^[0-9a-f]{32}$/;

export async function gbBearerMintChecked(store, { mintId, hash, by, expiresAt, mcpAccept }, now = Date.now()) {
  if (typeof store.transaction !== "function") throw new TypeError("transactional store required");
  // Exactly one outcome-bearing audit row per mint ATTEMPT, from a central
  // finally - never from a branch (kody discussion_r4372747686).  The finally
  // sits INSIDE the transaction:  the outcome row is atomic with the mint, so
  // an audit write failure aborts the whole attempt - no bearer rotation, no
  // tombstone, no reveal without its audit row.  The one row that cannot live
  // in an aborted transaction is the unexpected-failure row, written outside
  // by the catch below (best-effort;  the store may be why we are there).
  let outcome = null;
  try {
    return await store.transaction(async (txn) => {
    try {
    const refuse = (reason) => {
      outcome = { event: "gb_bearer_mint_refused", by: String(by ?? ""), reason };
      return { minted: false, reason };
    };
    // Strict shape before any timestamp math:  a mint id is exactly the
    // 32-hex form the admin page renders (12-hex ms + 20 random hex).  A
    // malformed id is refused as invalid, not misread as "stale".
    if (typeof mintId !== "string" || !GB_MINT_ID_RE.test(mintId)) return refuse("invalid");
    const forms = (await txn.get(GB_MINT_FORMS_KEY)) ?? { consumed: [] };
    // A tombstone lives exactly as long as its own form could possibly be
    // accepted, derived from the form id's timestamp - never from consumption
    // time:  a form future-dated within the 60s skew outlives a record timed
    // by `at`, and evicting that record early would reopen a replay window.
    // Only records whose form is already too old to mint may drop off.
    forms.consumed = forms.consumed.filter((c) => {
      const cts = mintFormTs(c.mint_id);
      return cts !== null && now - cts <= GB_MINT_FORM_TTL_MS;
    });
    const consumed = forms.consumed.find((c) => c.mint_id === mintId);
    if (consumed) {
      // A retry of a form whose mint COMPLETED is acked from the tombstone's
      // outcome metadata (kody discussion_r4372748176):  the route generates
      // a fresh raw token per POST, so a real HTTP resubmit always carries a
      // NEW hash - comparing hashes can never recognize it.  The ack is
      // caller-bound by the single-use mint id itself (rendered only in the
      // owner's authenticated admin page) and carries only what /admin status
      // already shows:  times, count, acceptance.  No new mint, no mutation,
      // and the raw token is not stored anywhere, so there is nothing new to
      // reveal;  an owner who lost it rotates from a fresh form.
      outcome = { event: "gb_bearer_mint_replay", by: String(by ?? "") };
      return { minted: false, reason: "completed", result: consumed.result ?? null };
    }
    const ts = mintFormTs(mintId);
    if (ts === null || now - ts > GB_MINT_FORM_TTL_MS || ts - now > GB_MINT_FORM_SKEW_MS) return refuse("stale");
    if (forms.consumed.length >= GB_MINT_FORM_KEEP) return refuse("busy");
    const prev = (await txn.get(GB_BEARER_KEY)) ?? null;
    const row = {
      hash: String(hash),
      created_at: now,
      created_by: String(by ?? ""),
      expires_at: Number(expiresAt) || now,
      last_used_at: 0,
      mint_count: Number(prev?.mint_count ?? 0) + 1,
      mcp_accept: mcpAccept === true,
    };
    // Write order inside the one transaction:  bearer row, then the consumed
    // marker (the outcome audit is written once, by the finally below).  A
    // real Durable Object transaction aborts as a unit on any throw;  even
    // under a non-transactional store every partial is fail-safe (an unseen
    // bearer nobody can use, or a dead form).
    await txn.put(GB_BEARER_KEY, row);
    const result = { created_at: row.created_at, expires_at: row.expires_at, mint_count: row.mint_count, mcp_accept: row.mcp_accept };
    await txn.put(GB_MINT_FORMS_KEY, { consumed: [...forms.consumed, { mint_id: String(mintId), at: now, result }] });
    outcome = { event: "gb_bearer_minted", by: row.created_by, expires_at: row.expires_at, replaced: Boolean(prev), mcp_accept: row.mcp_accept };
    return { minted: true, created_at: row.created_at, expires_at: row.expires_at, mint_count: row.mint_count, mcp_accept: row.mcp_accept };
    } finally {
      // The one outcome row for this attempt, inside the transaction (see the
      // header comment).  A throw here aborts the attempt:  a real Durable
      // Object transaction rolls back as a unit, so no bearer, tombstone or
      // half-written audit survives;  the catch records the attempt outside.
      if (outcome) await audit(txn, outcome, now);
    }
    });
  } catch (err) {
    // The transaction aborted:  every write inside it rolled back, including
    // the finally's row.  A tracked "minted" therefore never landed - the
    // attempt's only true record is the error row.  Refusal and replay
    // outcomes mutate nothing, so their row stays true even when its
    // in-transaction write died;  re-writing it here keeps exactly one
    // durable row per attempt.  Best-effort, and the error still propagates.
    if (!outcome || outcome.event === "gb_bearer_minted") {
      outcome = { event: "gb_bearer_mint_error", by: String(by ?? ""), error: err instanceof Error ? err.name : "unknown" };
    }
    try { await audit(store, outcome, now); } catch { /* the store is the failure */ }
    throw err;
  }
}

export async function gbBearerRevoke(store, { by }, now = Date.now()) {
  const had = (await store.get(GB_BEARER_KEY)) ?? null;
  await store.delete(GB_BEARER_KEY);
  await audit(store, { event: "gb_bearer_revoked", by: String(by ?? ""), had: Boolean(had) }, now);
  return { revoked: Boolean(had) };
}

const GB_MISMATCH_KEY = "gb_bearer_mismatches";

/**
 * Count one wrong-secret presentation against the identity it CLAIMED (kody
 * The narrowing review verdict:  a guessing storm was invisible;  gbBearerCheck deliberately
 * writes nothing on mismatch to protect the capped audit log).  Counts and a
 * last-seen time only - never a hash, a token, or an audit row.
 *
 * `configured` is the caller's set of currently configured identity names.
 * A claimed name outside it lands in the single "unconfigured" bucket, so
 * attacker-chosen names can never grow the map past |configured| + 1, and
 * keys dropped from the configuration are evicted on the next write so the
 * persisted map stays bounded as seats change.  The read-modify-write runs
 * in one storage transaction so concurrent mismatches cannot lose a count.
 */
export async function gbBearerMismatch(store, identity, configured, now = Date.now()) {
  const names = configured instanceof Set ? configured : new Set(configured ?? []);
  const bucket = names.has(identity) ? identity : "unconfigured";
  const ts = Number.isFinite(now) ? now : Date.now();
  await store.transaction(async (txn) => {
    const map = (await txn.get(GB_MISMATCH_KEY)) ?? {};
    const pruned = {};
    for (const [key, row] of Object.entries(map)) {
      if (key === "unconfigured" || names.has(key)) pruned[key] = row;
    }
    const row = pruned[bucket];
    const count = Number(row?.count);
    pruned[bucket] = {
      count: (Number.isFinite(count) && count >= 0 ? count : 0) + 1,
      last_at: ts,
    };
    await txn.put(GB_MISMATCH_KEY, pruned);
  });
}

/**
 * The write-throttled counting layer (fresh-head review:  one storage write
 * per wrong guess lets an anonymous caller burn the shared DO write quota and
 * serialize the identity's writes).  The Durable Object instance buffers
 * increments in memory and flushes at most once per GB_MISMATCH_FLUSH_MS,
 * exactly the discipline last_used_at already keeps in gbBearerCheck.
 *
 * HONEST ACCOUNTING:  persisted totals are exact at flush points;  increments
 * buffered since the last flush live in instance memory only and are lost if
 * the instance is evicted, so the stored count is a LOWER BOUND, not an exact
 * lifetime total.  For the metric's purpose - is this identity under a
 * guessing storm, and how recently - a lower bound plus last_at is the
 * signal;  exact per-guess accounting is not what /admin shows.  Reads never
 * flush, so refreshing /admin or reading the state API can never cause a
 * write.  A failed flush merges its batch back into the buffer (nothing is
 * dropped), backs off to the next interval so a busy alarm chain cannot
 * spin retries, and (via the caller's catch) never weakens bearer rejection.
 */
export const GB_MISMATCH_FLUSH_MS = 60_000;

/** The per-instance buffer a gate holds:  pending deltas, the last flush,
 * the configured set to prune with, and the in-flight flush guard. */
export function gbMismatchBuffer() {
  return { pending: new Map(), lastFlush: 0, configured: new Set(), flushing: false, retryAt: 0 };
}

/**
 * Count one wrong-secret presentation.  Returns true when this call flushed
 * to storage, false when the increment sits in memory.  The first call after
 * an instance starts (or after a quiet interval) flushes immediately, so a
 * storm's onset is visible within one call and a steady stream can never
 * produce more than one write per interval per instance.
 */
export async function gbMismatchCount(store, buffer, identity, configured, now = Date.now(), flushMs = GB_MISMATCH_FLUSH_MS) {
  const ts = Number.isFinite(now) ? now : Date.now();
  buffer.configured = configured instanceof Set ? configured : new Set(configured ?? []);
  const row = buffer.pending.get(identity) ?? { count: 0, last_at: 0 };
  row.count += 1;
  row.last_at = ts;
  buffer.pending.set(identity, row);
  if (!gbMismatchDue(buffer, ts, flushMs)) return false;
  return gbMismatchFlushGuarded(store, buffer, ts, flushMs);
}

/**
 * Flush under the in-memory guard.  lastFlush advances only AFTER the
 * transaction resolves, so two calls racing a cold start (each synchronous
 * section is atomic, but awaits interleave) must not both write:  the first
 * sets buffer.flushing in its synchronous section and the second sees it
 * and keeps buffering.  Exactly one write per flush window per instance,
 * demonstrated by the overlapping-calls unit test and the real-SeatGate
 * integration step.  A failure backs the retry off to the interval
 * (retryAt) so a busy alarm chain cannot spin it.
 */
export async function gbMismatchFlushGuarded(store, buffer, now = Date.now(), flushMs = GB_MISMATCH_FLUSH_MS) {
  if (buffer.flushing || buffer.pending.size === 0) return false;
  buffer.flushing = true;
  const ts = Number.isFinite(now) ? now : Date.now();
  try {
    await gbMismatchFlush(store, buffer, buffer.configured, ts);
    buffer.retryAt = 0;
    return true;
  } catch (err) {
    // Back off to the interval after a failure:  an immediate retry under
    // the wa sweep's busy (one-second) alarm chain would be a write attempt
    // per second - the very amplification the buffer exists to bound.
    buffer.retryAt = ts + flushMs;
    throw err;
  } finally {
    buffer.flushing = false;
  }
}

/**
 * The moment the buffered tail is due to persist:  the flush interval after
 * the last successful flush, or NOW for a cold instance whose first flush
 * has not landed - but never before retryAt, the backoff a failed flush
 * sets.  null when nothing is pending (no alarm work needed).
 */
export function gbMismatchDueAt(buffer, now = Date.now(), flushMs = GB_MISMATCH_FLUSH_MS) {
  if (!buffer || buffer.pending.size === 0) return null;
  const ts = Number.isFinite(now) ? now : Date.now();
  const base = buffer.lastFlush === 0 ? ts : buffer.lastFlush + flushMs;
  return Math.max(base, buffer.retryAt || 0);
}

/** True when a flush is due at `now` - the single rule the count path and
 * the alarm path share, so NEITHER can write more than once per interval. */
export function gbMismatchDue(buffer, now = Date.now(), flushMs = GB_MISMATCH_FLUSH_MS) {
  const due = gbMismatchDueAt(buffer, now, flushMs);
  return due !== null && due <= now;
}

/**
 * The alarm time that guarantees a buffered tail persists at its due time
 * (gbMismatchDueAt:  the flush interval after the last flush, or the
 * failure backoff), or null when no (re)schedule is needed.  An EXISTING
 * earlier alarm (e.g. the wa sweep's 1s busy chain) already covers the
 * tail - when it fires early the handler's due check finds nothing to do
 * and its reschedule carries the metric deadline forward - so it must not
 * be pushed later;  an existing LATER alarm (the sweep's hourly idle) must
 * be pulled earlier to keep the guarantee.
 */
export function gbMismatchAlarmDeadline(buffer, existingAlarm, now = Date.now(), flushMs = GB_MISMATCH_FLUSH_MS) {
  const due = gbMismatchDueAt(buffer, now, flushMs);
  if (due === null) return null;
  // An existing EARLIER alarm still covers the tail:  when it fires, the
  // handler runs its due check and reschedules to the metric deadline (see
  // SeatGate.alarm), so the tail persists on time without a second
  // scheduling write here.  A LATER existing alarm (the wa sweep's idle
  // hour) is pulled in to the due time.  The deadline itself is never
  // pushed out.
  if (existingAlarm !== null && existingAlarm <= due) return null;
  return due;
}

/**
 * Persist every pending delta in one transaction, applying the same prune
 * (drop rows for identities that left the configured set) as the direct
 * counter.  The pending map is swapped out synchronously BEFORE the first
 * await;  on success lastFlush advances, on failure the batch merges back
 * into the live buffer and the exception propagates for the caller's
 * backoff.
 */
export async function gbMismatchFlush(store, buffer, configured, now = Date.now()) {
  if (buffer.pending.size === 0) return;
  const names = configured instanceof Set ? configured : new Set(configured ?? []);
  const ts = Number.isFinite(now) ? now : Date.now();
  // Synchronous hand-off:  the batch leaves the live map BEFORE the first
  // await.  A delta counted while the transaction is in flight lands in the
  // fresh map and survives BOTH outcomes - a clear() after the await would
  // wipe it unwritten.  Correctness here rests on JavaScript's run-to-
  // completion for synchronous sections, not on DO input-gate behavior.
  const batch = buffer.pending;
  buffer.pending = new Map();
  try {
    await store.transaction(async (txn) => {
      const map = (await txn.get(GB_MISMATCH_KEY)) ?? {};
      const pruned = {};
      for (const [key, row] of Object.entries(map)) {
        if (key === "unconfigured" || names.has(key)) pruned[key] = row;
      }
      for (const [identity, delta] of batch) {
        const bucket = names.has(identity) ? identity : "unconfigured";
        const row = pruned[bucket];
        const count = Number(row?.count);
        pruned[bucket] = {
          count: (Number.isFinite(count) && count >= 0 ? count : 0) + delta.count,
          last_at: Math.max(Number(row?.last_at) || 0, delta.last_at),
        };
      }
      await txn.put(GB_MISMATCH_KEY, pruned);
    });
  } catch (err) {
    // The transaction rolled back as a unit.  Merge the batch back UNDER
    // anything counted while it was in flight, so no delta is ever dropped.
    for (const [identity, delta] of batch) {
      const current = buffer.pending.get(identity);
      if (current) {
        current.count += delta.count;
        current.last_at = Math.max(current.last_at, delta.last_at);
      } else buffer.pending.set(identity, delta);
    }
    throw err;
  }
  buffer.lastFlush = ts;
}

/** For /admin:  the per-identity mismatch counts, times and counts only. */
export async function gbBearerMismatches(store) {
  return (await store.get(GB_MISMATCH_KEY)) ?? {};
}

/**
 * Check a presented hash.  {ok: true} or {ok: false, reason} with reason one of
 * none, mismatch, expired.  last_used_at is written at most once an hour, so a busy bot
 * does not turn every call into a storage write.
 */
export async function gbBearerCheck(store, hash, now = Date.now()) {
  const row = (await store.get(GB_BEARER_KEY)) ?? null;
  if (!row?.hash) return { ok: false, reason: "none" };
  if (!sameHash(String(row.hash), String(hash))) return { ok: false, reason: "mismatch" };
  if (Number(row.expires_at) <= now) return { ok: false, reason: "expired" };
  if (now - Number(row.last_used_at || 0) >= 3_600_000) await store.put(GB_BEARER_KEY, { ...row, last_used_at: now });
  return { ok: true, mcp_accept: row.mcp_accept === true };
}

/** For /admin:  times and counts only, no hash. */
export async function gbBearerStatus(store, now = Date.now()) {
  const row = (await store.get(GB_BEARER_KEY)) ?? null;
  if (!row?.hash) return { active: false };
  return {
    active: Number(row.expires_at) > now,
    expired: Number(row.expires_at) <= now,
    mcp_accept: row.mcp_accept === true,
    mcp_accept_recorded: typeof row.mcp_accept === "boolean",
    created_at: Number(row.created_at) || 0,
    created_by: String(row.created_by ?? ""),
    expires_at: Number(row.expires_at) || 0,
    last_used_at: Number(row.last_used_at) || 0,
    mint_count: Number(row.mint_count) || 0,
  };
}
// ---------------------------------------------------------------- gr-assert one-use jti claims
//
// A gr-assert/2 assertion is one-use:  the /internal/gr-assert-verify route
// claims its jti here, atomically with the live gate check, before it answers
// ok.  Two layers of serialization make the check-and-claim safe:
//   - the Durable Object runtime processes one RPC of one object at a time
//     (the input gate), so two verify calls of one seat cannot interleave;
//   - store.transaction additionally makes the reads and the write one
//     storage transaction, so even an interleaving runtime could never split
//     the jti read from its claim write.
//
// Entries live until their assertion's own expiry (a later replay is expired
// anyway).  The map is BOUNDED and fails closed:  at capacity of live
// (unexpired) records a new claim refuses - live nonce records are NEVER
// evicted to make room, because discarding one would reopen its replay
// window.  Capacity refills on its own within one max TTL (<=120s).

const GR_JTI_KEY = "gr_assert_jtis";
const GR_JTI_MAX = 512;

/**
 * Run the seat's live gate check and, only when it passes, claim one
 * assertion jti - a single transaction, so the check and the claim can never
 * be split.  `expiresAt` is the assertion's exp in epoch milliseconds.
 * Returns { ok: true } exactly once per jti, or { ok: false, reason } with
 * "paused", "grant_invalid", "replayed" or "jti_store_full".
 */
export async function claimGrAssert(store, { epoch, approvedAt, phase, jti, expiresAt }, now = Date.now()) {
  if (typeof store.transaction !== "function") throw new TypeError("transactional store required");
  return store.transaction(async (txn) => {
    // Revocation equivalence, per call:  the same gate check serveMcp runs.
    // A refusal spends nothing - a paused-then-unpaused assertion still works.
    const gate = await check(txn, { epoch, approvedAt, phase }, now);
    if (gate.code !== "ok") return { ok: false, reason: gate.code === "paused" ? "paused" : "grant_invalid" };
    const seen = (await txn.get(GR_JTI_KEY)) ?? {};
    for (const [k, v] of Object.entries(seen)) {
      if (!(Number.isFinite(v) && v > now)) delete seen[k];
    }
    if (Object.hasOwn(seen, jti)) return { ok: false, reason: "replayed" };
    if (Object.keys(seen).length >= GR_JTI_MAX) return { ok: false, reason: "jti_store_full" };
    seen[jti] = expiresAt;
    await txn.put(GR_JTI_KEY, seen);
    return { ok: true };
  });
}

