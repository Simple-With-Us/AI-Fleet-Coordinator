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
const CALL_FIELDS = ["seat", "grant_ref", "client_id", "tool", "channel_id", "topic", "message_id", "outcome", "error_code", "latency_ms", "body_len", "idem_ref", "recipient_ids", "asn", "country"];

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
    if (field === "recipient_ids") {
      // The user ids a DM tool addressed (spec 1.2):  integers only, at most 8.  Never a name or a body.
      const ids = Array.isArray(value) ? value.filter((v) => Number.isSafeInteger(v)).slice(0, 8) : [];
      if (ids.length) clean[field] = ids;
      continue;
    }
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

/**
 * Is there room for subscription `id`?  It exists already (a refresh), or the
 * seat is under the cap.  With a cap of one, a second chat is refused rather
 * than replacing the first (`until` says when the live one lapses).
 */
export async function eventSubRoom(store, { id, max = 1 }, now = Date.now()) {
  const { subs, dropped } = await liveSubs(store, now);
  await persistDropped(store, subs, dropped, now);
  if (Object.hasOwn(subs, id) || Object.keys(subs).length < max) return { ok: true };
  return { ok: false, until: Math.min(...Object.values(subs).map((sub) => sub.refresh_before)) };
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
  const prior = subs[sub.id];
  if (!prior && Object.keys(subs).length >= max) {
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
 * Returns {verdict: "go", subs, token}, {verdict: "duplicate"},
 * {verdict: "in_flight", retryAfterS} or {verdict: "paused"}.
 */
export async function eventClaimWake(store, { wakeId, messageId = null }, now = Date.now()) {
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
  const token = crypto.randomUUID();
  seen[wakeId] = { s: "claimed", at: now, until: now + WAKE_LEASE_MS, token };
  await putWakeSeen(store, seen);
  await audit(store, { event: "event_wake_captured", wake_ref: wakeRef, message_id: messageId ?? null, subscribers: Object.keys(subs).length, ...(prior ? { reclaimed: true } : {}) }, now);
  return { verdict: "go", token, subs: Object.values(subs).map((sub) => ({ ...sub })) };
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

/** Live subscriptions for /admin:  never the URL path or the secret. */
export async function eventSubsSummary(store, now = Date.now()) {
  const { subs } = await liveSubs(store, now);
  return Object.values(subs).map((sub) => ({
    sub_ref: subRef(sub.id),
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
