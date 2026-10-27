// Transactional inbox and 35-day ledgers.  Timestamp cutoff rejects old replay
// after ledger expiry.  No daily/hourly quota on owner messages.
import { redact, scanReply } from "./contract.js";
import { sha256Hex } from "../contract.js";
const RETENTION = 7 * 86400000;
const LEDGER_RETENTION = 35 * 86400000;
export const RERING_MS = 10 * 60000;
export const OPERATOR_DEBOUNCE_MS = 30000;
const key = seq => `wa:message:${String(seq).padStart(20, "0")}`;
const expiryKey = (at, id = "") => `wa:expire:${String(at).padStart(20, "0")}:${id}`;
const schedule = (store, at, id, job) => store.put(expiryKey(at, id), job);


export async function ingest(store, { body, owner, known = [] }, now = Date.now()) {
  return store.transaction(async tx => {
    if (!owner || body.agent_hash !== owner.agent_hash || body.sender_hash !== owner.sender_hash) return { ok: false, reason: "binding_mismatch" };
    let seq = (await tx.get("wa:seq")) ?? 0;
    let stored = 0; let skipped = 0;
    for (const m of body.messages) {
      if (m.sender_hash !== owner.sender_hash || m.timestamp <= owner.cutoff || now / 1000 - m.timestamp > 31 * 86400) continue;
      const seen = `wa:seen:${m.id_hash}`;
      if (await tx.get(seen)) continue;
      if (m.timestamp > Math.floor(now / 1000) + 60) {
        await tx.put(seen, now);
        await schedule(tx, now + LEDGER_RETENTION, `future:${m.id_hash}`, { kind: "delete", keys: [seen] });
        skipped++; continue;
      }
      const silent = m.text?.replace(/^[\s\p{Cf}]+/u, "").startsWith("👀") === true;
      seq++;
      await tx.put(key(seq), { seq: String(seq), text: redact(m.text, known), type: m.type, timestamp: m.timestamp, received_at: now, silent, surfaced: false, id_hash: m.id_hash });
      // Do not keep raw WhatsApp ids or senders, even in the tombstones.
      await tx.put(seen, now);
      await schedule(tx, now + RETENTION, `body:${seq}`, { kind: "body", key: key(seq) });
      await schedule(tx, now + LEDGER_RETENTION, `ledger:${seq}`, { kind: "delete", keys: [key(seq), seen, `wa:pending:owner:${String(seq).padStart(20, "0")}`] });
      if (!silent) await tx.put(`wa:pending:owner:${String(seq).padStart(20, "0")}`, { id: `wa:owner:${seq}`, source: "authenticated_owner", seq: String(seq), created_at: now });
      stored++;
    }
    await tx.put("wa:seq", seq);
    await tx.put("wa:last_ingest", now);
    if (skipped) console.log(JSON.stringify({ event: "wa_invalid_items_skipped", count: skipped }));
    return { ok: true, processed: true, next_offset: body.next_offset, stored, ...(skipped ? { skipped } : {}) };
  });
}
export async function operator(store, { requestId }, now = Date.now()) {
  return store.transaction(async tx => {
    const id = `wa:operator:${requestId}`;
    const prior = await tx.get(id);
    if (prior) return { ...(prior.result ?? { ok: true }), outcome: prior.outcome ?? "ok", duplicate: true };
    const pending = await tx.list({ prefix: "wa:pending:operator:", limit: 1 });
    const nextAt = (await tx.get("wa:operator_next_at")) ?? 0;
    const row = pending.values().next().value;
    const retryAt = Math.max(nextAt, row?.next_ring_at ?? 0, row?.lease_until ?? 0, row ? now + (row.blocked ? RERING_MS : OPERATOR_DEBOUNCE_MS) : now);
    if (pending.size || now < nextAt) return { ok: false, reason: "operator_busy", retry_after_s: Math.max(1, Math.ceil((retryAt - now) / 1000)), ...(row?.blocked ? { requires_subscription_refresh: true } : {}) };
    await tx.put(id, { created_at: now, outcome: "ok", result: { ok: true } });
    await schedule(tx, now + LEDGER_RETENTION, `operator:${requestId}`, { kind: "delete", keys: [id, `wa:pending:operator:${requestId}`] });
    await tx.put("wa:operator_next_at", now + OPERATOR_DEBOUNCE_MS);
    await tx.put(`wa:pending:operator:${requestId}`, { id, source: "operator", created_at: now });
    return { ok: true, outcome: "ok" };
  });
}

// The acknowledgement checkpoint, shared by whatsapp_read and
// whatsapp_reply.  Only a message an earlier read returned can be
// acknowledged: the surfaced watermark is that record.
async function applyAck(tx, ack, previousAck, now) {
  const through = Math.max(previousAck, Number(ack ??  0));
  if (through <= previousAck) return through;
  await tx.put("wa:ack", through);
  const gate = await tx.get("wa:owner_gate");
  if (gate) await tx.put("wa:owner_gate", { ...gate, next_at: 0 });
  // Logical retirement is the ack checkpoint; physical cleanup is bounded.
  // Claim starts after that checkpoint, so old rows cannot hide new wakes.
  const cleanup = await tx.list({ prefix: "wa:pending:owner:", end: `wa:pending:owner:${String(through + 1).padStart(20, "0")}`, limit: 100 });
  if (cleanup.size) await tx.delete([...cleanup.keys()]);
  if (cleanup.size === 100) await schedule(tx, now, `pending:${through}`, { kind: "pending_ack", through });
  // Retention starts when the acknowledgement is accepted.  The sweep
  // processes its bounded range using these content-free checkpoints.
  const checkpoint = `wa:ack_at:${String(through).padStart(20, "0")}`;
  await tx.put(checkpoint, now);
  await schedule(tx, now + 86400000, `ack:${through}`, { kind: "ack", after: previousAck, through });
  await schedule(tx, now + LEDGER_RETENTION, `checkpoint:${through}`, { kind: "delete", keys: [checkpoint] });
  return through;
}

export async function read(store, args, now = Date.now()) {
  return store.transaction(async tx => {
    const surfaced = (await tx.get("wa:surfaced")) ?? 0;
    const previousAck = (await tx.get("wa:ack")) ?? 0;
    const ack = args.ack_through;
    if (ack !== undefined && (typeof ack !== "string" || !/^[1-9][0-9]*$/.test(ack) || BigInt(ack) > BigInt(surfaced))) return { ok: false, reason: "not_surfaced" };
    const through = await applyAck(tx, ack, previousAck, now);
    const after = args.include_surfaced ? through : Math.max(through, surfaced);
    const rows = await tx.list({ prefix: "wa:message:", start: key(after + 1), limit: args.limit ?? 20 });
    const items = [];
    let last = surfaced;
    for (const [k, row] of rows) {
      if (now - row.received_at >= RETENTION) { row.text = null; row.expired = true; }
      items.push({ ...row, source: "authenticated_owner", untrusted: true });
      row.surfaced = true;
      last = Math.max(last, Number(row.seq));
      await tx.put(k, row);
    }
    await tx.put("wa:surfaced", last);
    return { ok: true, items, acknowledged_through: String(through) };
  });
}
export async function claim(store, { destination = "", source } = {}, now = Date.now()) {
  return store.transaction(async tx => {
    if (await tx.get("paused")) return null;
    const ack = (await tx.get("wa:ack")) ?? 0;
    const owners = source === "operator" ? new Map() : await tx.list({ prefix: "wa:pending:owner:", start: `wa:pending:owner:${String(ack + 1).padStart(20, "0")}`, limit: 100 });
    const operators = source === "authenticated_owner" ? new Map() : await tx.list({ prefix: "wa:pending:operator:", limit: 1 });
    const rows = new Map([...owners, ...operators]);
    const ownerGate = (await tx.get("wa:owner_gate")) ?? {};
    for (const [k, row] of rows) {
      if (row.lease_until > now) continue;
      if (row.source === "authenticated_owner" && (ownerGate.next_at > now || (ownerGate.destination === destination && ownerGate.blocked))) continue;
      if (row.next_ring_at > now || (row.destination === destination && row.blocked)) continue;
    const token = crypto.randomUUID();
      const wake_id = row.wake_id ?? row.id;
      const keys = [];
      for (const [candidate, item] of rows) {
        if (item.source !== row.source || item.lease_until > now || item.next_ring_at > now || (item.destination === destination && item.blocked)) continue;
        await tx.put(candidate, { ...item, ...(item.destination !== destination ? { permanent_failures: 0, blocked: false } : {}), token, wake_id, destination, ring: row.ring ?? 0, lease_until: now + 30000 });
        keys.push(candidate);
      }
      return { ...row, key: k, keys, token, wake_id };
    }
    return null;
  });
}
export async function settle(store, { keys, key: k, token, delivered, permanent = false, noSubscriber = false }, now = Date.now()) {
  if (typeof token !== "string" || !token) return;
  return store.transaction(async tx => {
    const claimed = [];
    for (const pendingKey of keys ?? [k]) {
      const row = await tx.get(pendingKey);
      if (row?.token === token) claimed.push([pendingKey, row]);
    }
    const owners = claimed.filter(([, row]) => row.source === "authenticated_owner");
    const destination = owners[0]?.[1].destination;
    const gate = owners.length ? (await tx.get("wa:owner_gate")) ?? {} : {};
    const prior = Math.max(0, gate.destination === destination ? gate.permanent_failures ?? 0 : 0, ...owners.map(([, row]) => row.permanent_failures ?? 0));
    const ownerFailures = delivered ? 0 : prior + (permanent ? 1 : 0);
    const ownerBlocked = !delivered && (ownerFailures >= 3 || (gate.destination === destination && gate.blocked === true));
    const nextAt = now + (delivered || permanent ? RERING_MS : 30000);
    for (const [pendingKey, row] of claimed) {
      const settled = { ...row, lease_until: 0 };
      delete settled.token; // A claim may settle only once, including lost replies.
      if (noSubscriber) { await tx.put(pendingKey, settled); continue; }
      if (delivered && row.source === "operator") { await tx.delete(pendingKey); continue; }
      const failures = row.source === "authenticated_owner" ? ownerFailures : (row.permanent_failures ?? 0) + (permanent ? 1 : 0);
      const blocked = row.source === "authenticated_owner" ? ownerBlocked : failures >= 3;
      if (blocked) console.log(JSON.stringify({ event: "wa_delivery_blocked", source: row.source }));
      await tx.put(pendingKey, { ...settled, next_ring_at: nextAt, permanent_failures: failures, blocked, ring: (row.ring ?? 0) + (delivered ? 1 : 0) });
    }
    // One attempt has one owner rejection history, independent of batch order.
    // Missing subscribers/transient failures cannot erase an earlier rejection.
    if (owners.length && !noSubscriber) await tx.put("wa:owner_gate", { destination, next_at: nextAt, permanent_failures: ownerFailures, blocked: ownerBlocked });
  });
}

// ---------------------------------------------------------------- AFC-545: the outbound leg
//
// The reply text lives in the outbox until the reply is final, and the send
// ledger keeps the hashes for 35 days.  A reply whose outcome is unknown
// stays unknown for good: it is never sent again, and there is no resend.
const REPLY_TEXT_RETENTION = 24 * 3600000;
const OUTBOX_LEASE_MS = 5 * 60000;
const OUTBOX_MAX_ATTEMPTS = 3;
const RECENT_LIMIT = 50;
const FINAL_STATES = ["sent", "refused", "outcome_unknown", "cancelled", "expired"];
const replyKey = intentId => `wa:reply:${intentId}`;
const queueKey = (queuedAt, intentId) => `wa:queue:${String(queuedAt).padStart(20, "0")}:${intentId}`;
const clientRefKey = hash => `wa:clientref:${hash}`;
const unknownBodyKey = hash => `wa:unknownbody:${hash}`;
const inflightKey = intentId => `wa:inflight:${intentId}`;

/** The binding identity the suspect flag belongs to.  A new binding clears it.  */
export function bindingIdentity(env) {
  const raw = typeof env?.WA_OWNER_BINDING_JET === "string" ? env.WA_OWNER_BINDING_JET : "";
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch { return null; }
  if (!parsed || typeof parsed.agent_hash !== "string" || typeof parsed.sender_hash !== "string" || !Number.isSafeInteger(parsed.cutoff)) return null;
  return `${parsed.agent_hash}|${parsed.sender_hash}|${parsed.cutoff}`;
}

/** The Worker side SEND switch.  Off is the default, and every read fails
 *  closed: a missing or unreadable value is off, never on.  */
export function sendEnabled(env) {
  return String(env?.WHATSAPP_BRIDGE_SEND_ENABLED ??  "").trim() === "on";
}

async function suspectHeld(tx, binding, now) {
  if (!binding) return false;
  const mark = await tx.get("wa:binding_suspect");
  return Boolean(mark && mark.binding === binding);
}

/** The public view of one reply.  It never carries the text.  */
function publicReply(row) {
  return {
    intent_id: row.intent_id,
    state: row.state,
    reason: row.reason ?? null,
    attempts: row.attempts ??  0,
    created_at: row.created_at,
    queued_at: row.queued_at,
    sent_at: row.sent_at ?? null,
    message_id: row.message_id ?? null,
    delivered_at: row.delivered_at ?? null,
    read_at: row.read_at ?? null,
    error_code: row.error_code ?? null,
    error_detail: row.error_detail ?? null,
    reply_to_seq: row.reply_to_seq ?? null,
  };
}

async function createIntent(tx, { text, bodyHash, refHash, replyToSeq, replyToIdHash, now, sendEnabled, suspect, binding, previousIntentId }) {
  const intentId = `wa_reply_${crypto.randomUUID().replace(/-/g, "")}`;
  const held = !sendEnabled || suspect;
  const row = {
    intent_id: intentId,
    client_ref_hash: refHash,
    body_hash: bodyHash,
    state: held ?  "held" : "queued",
    reason: held ? (suspect ?  "binding_suspect" : "send_disabled") : null,
    attempts: 0,
    created_at: now,
    updated_at: now,
    queued_at: now,
    text,
    reply_to_seq: replyToSeq ?? null,
    reply_to_id_hash: replyToIdHash ?? null,
    sent_at: null,
    message_id: null,
    delivered_at: null,
    read_at: null,
    error_code: null,
    error_detail: null,
    retry_after_s: null,
    previous_intent_id: previousIntentId ?? null,
    binding: binding ?? null,
    lease_until: 0,
    token: null,
  };
  await tx.put(replyKey(intentId), row);
  await tx.put(clientRefKey(refHash), { intent_id: intentId, created_at: now });
  if (!held) await tx.put(queueKey(now, intentId), {});
  const recent = (await tx.get("wa:reply_recent")) ??  [];
  recent.push(intentId);
  while (recent.length > RECENT_LIMIT) recent.shift();
  await tx.put("wa:reply_recent", recent);
  await schedule(tx, now + LEDGER_RETENTION, `reply:${intentId}`, { kind: "reply_ledger", intent_id: intentId, queued_at: now, client_ref_hash: refHash });
  if (held) await schedule(tx, now + REPLY_TEXT_RETENTION, `replyheld:${intentId}`, { kind: "reply_expire", intent_id: intentId });
  return { ok: true, intent_id: intentId, state: row.state, reply: publicReply(row) };
}

/**
 * whatsapp_reply: queue one reply to the bound conversation.  There is no
 * recipient argument.  The call is idempotent on client_ref, and a repeat
 * after a definite refusal makes a new attempt linked to the old one.
 */
export async function reply(store, { args, known = [], sendEnabled = false, binding = null }, now = Date.now()) {
  const bodyHash = await sha256Hex(args.text);
  const refHash = await sha256Hex(args.client_ref);
  // The reply text gets the same secret scan as an inbound body: a hit is
  // refused with the rule name, and nothing is stored.
  const rule = scanReply(args.text, known);
  if (rule) return { ok: false, reason: "secret_in_text", rule };
  return store.transaction(async tx => {
    if (args.ack_through !== undefined) {
      if (typeof args.ack_through !== "string" || !/^[1-9][0-9]{0,15}$/.test(args.ack_through)) return { ok: false, reason: "invalid_ack_through" };
      const surfaced = (await tx.get("wa:surfaced")) ??  0;
      if (BigInt(args.ack_through) > BigInt(surfaced)) return { ok: false, reason: "not_surfaced" };
      await applyAck(tx, args.ack_through, (await tx.get("wa:ack")) ??  0, now);
    }
    let replyToIdHash = null;
    if (args.reply_to_seq !== undefined) {
      const message = await tx.get(key(args.reply_to_seq));
      if (!message) return { ok: false, reason: "unknown_seq" };
      replyToIdHash = message.id_hash ?? null;
    }
    const mapping = await tx.get(clientRefKey(refHash));
    if (mapping) {
      const prior = await tx.get(replyKey(mapping.intent_id));
      if (!prior) {
        await tx.delete(clientRefKey(refHash));
      } else if (prior.state === "refused") {
        // A definite refusal releases the bucket: the next attempt is a new
        // intent, linked to the refused one, which stays in the ledger.
        return createIntent(tx, { text: args.text, bodyHash, refHash, replyToSeq: args.reply_to_seq, replyToIdHash, now, sendEnabled, suspect: await suspectHeld(tx, binding, now), binding, previousIntentId: prior.intent_id });
      } else if (prior.state === "outcome_unknown") {
        // Unknown for good: nothing is sent, and nothing is queued.
        return { ok: false, reason: "outcome_unknown", intent_id: prior.intent_id, state: prior.state };
      } else {
        return { ok: true, intent_id: prior.intent_id, state: prior.state, duplicate: true, reply: publicReply(prior) };
      }
    }
    // A reply whose text is identical to an unknown-outcome reply is refused:
    // the system never sends that text again.
    if (await tx.get(unknownBodyKey(bodyHash))) return { ok: false, reason: "same_text_as_unknown" };
    return createIntent(tx, { text: args.text, bodyHash, refHash, replyToSeq: args.reply_to_seq, replyToIdHash, now, sendEnabled, suspect: await suspectHeld(tx, binding, now), binding, previousIntentId: null });
  });
}

/**
 * whatsapp_send_status: list the recent replies and their states, or act on
 * one held reply.  The action applies only to a held reply.
 */
export async function sendStatus(store, { intent_id, action, sendEnabled = false }, now = Date.now()) {
  return store.transaction(async tx => {
    if (action !== undefined) {
      const row = await tx.get(replyKey(intent_id));
      if (!row) return { ok: false, reason: "unknown_intent" };
      if (row.state !== "held") return { ok: false, reason: "not_held", state: row.state, reply: publicReply(row) };
      if (action === "cancel") {
        const settled = { ...row, state: "cancelled", updated_at: now, cancelled_at: now, text: null, token: null, lease_until: 0 };
        await tx.put(replyKey(intent_id), settled);
        await tx.delete(inflightKey(intent_id));
        return { ok: true, intent_id, state: "cancelled", reply: publicReply(settled) };
      }
      if (!sendEnabled) return { ok: false, reason: "send_disabled", state: row.state, reply: publicReply(row) };
      const settled = { ...row, state: "queued", reason: null, updated_at: now, released_at: now };
      await tx.put(replyKey(intent_id), settled);
      await tx.put(queueKey(row.queued_at, intent_id), {});
      return { ok: true, intent_id, state: "queued", reply: publicReply(settled) };
    }
    if (intent_id !== undefined) {
      const row = await tx.get(replyKey(intent_id));
      if (!row) return { ok: false, reason: "unknown_intent" };
      return { ok: true, replies: [publicReply(row)] };
    }
    const recent = (await tx.get("wa:reply_recent")) ??  [];
    const replies = [];
    for (const id of recent.slice(-25).reverse()) {
      const row = await tx.get(replyKey(id));
      if (row) replies.push(publicReply(row));
    }
    return { ok: true, replies };
  });
}

/**
 * The outbox claim: one queued reply at a time, with a five minute lease.
 * While SEND is off, nothing is handed out and the queued replies are held.
 */
export async function outboxClaim(store, { sendEnabled = false, binding = null } = {}, now = Date.now()) {
  return store.transaction(async tx => {
    // A claim whose lease ended with no result is unknown for good.
    await expireInflight(tx, now);
    const suspect = await suspectHeld(tx, binding, now);
    if (!sendEnabled || suspect) {
      const queued = await tx.list({ prefix: "wa:queue:", limit: 100 });
      let held = 0;
      for (const [queueKeyValue] of queued) {
        const id = queueKeyValue.split(":").pop();
        const row = await tx.get(replyKey(id));
        if (!row || row.state !== "queued") { await tx.delete(queueKeyValue); continue; }
        await tx.put(replyKey(id), { ...row, state: "held", reason: suspect ?  "binding_suspect" : "send_disabled", updated_at: now });
        await tx.delete(queueKeyValue);
        held++;
      }
      return { ok: true, reply: null, held };
    }
    const queued = await tx.list({ prefix: "wa:queue:", limit: 1 });
    const entry = [...queued.entries()][0];
    if (!entry) return { ok: true, reply: null };
    const [queueKeyValue] = entry;
    const intentId = queueKeyValue.split(":").pop();
    const row = await tx.get(replyKey(intentId));
    if (!row || row.state !== "queued") { await tx.delete(queueKeyValue); return { ok: true, reply: null }; }
    if ((row.not_before ?? 0) > now) return { ok: true, reply: null };
    const token = crypto.randomUUID();
    const claimed = { ...row, state: "claimed", updated_at: now, lease_until: now + OUTBOX_LEASE_MS, token, attempts: (row.attempts ??  0) + 1 };
    await tx.put(replyKey(intentId), claimed);
    await tx.delete(queueKeyValue);
    await tx.put(inflightKey(intentId), { intent_id: intentId, lease_until: claimed.lease_until });
    return {
      ok: true,
      reply: {
        intent_id: intentId,
        text: row.text ??  "",
        reply_to_seq: row.reply_to_seq ?? null,
        reply_to_id_hash: row.reply_to_id_hash ?? null,
        queued_at: row.queued_at,
        lease_until: claimed.lease_until,
        attempts: claimed.attempts ??  1,
        token,
      },
    };
  });
}

/** A claim that ended with no result: unknown for good, never sent again.  */
async function expireInflight(tx, now) {
  const rows = await tx.list({ prefix: "wa:inflight:", limit: 100 });
  for (const [keyValue, item] of rows) {
    if ((item.lease_until ??  0) > now) continue;
    await tx.delete(keyValue);
    const row = await tx.get(replyKey(item.intent_id));
    if (!row || FINAL_STATES.includes(row.state)) continue;
    await tx.put(replyKey(item.intent_id), { ...row, state: "outcome_unknown", reason: "lease_expired", updated_at: now, token: null, lease_until: 0 });
    await tx.put(unknownBodyKey(row.body_hash), { intent_id: item.intent_id, created_at: now });
    await schedule(tx, now + REPLY_TEXT_RETENTION, `replytext:${item.intent_id}`, { kind: "reply_text", intent_id: item.intent_id });
    await schedule(tx, now + LEDGER_RETENTION, `unknownbody:${item.intent_id}`, { kind: "delete", keys: [unknownBodyKey(row.body_hash)] });
  }
}

/**
 * The result the bridge posts back.  A final state never changes again, and
 * an unknown outcome is never turned back into a queued reply.
 */
export async function outboxResult(store, { body, binding = null }, now = Date.now()) {
  const { intent_id, outcome } = body;
  return store.transaction(async tx => {
    const row = await tx.get(replyKey(intent_id));
    if (!row) return { ok: false, reason: "unknown_intent" };
    if (row.token && row.token !== body.token) return { ok: false, reason: "stale_claim" };
    if (FINAL_STATES.includes(row.state)) {
      await tx.delete(inflightKey(intent_id));
      return { ok: true, state: row.state, duplicate: true };
    }
    if (outcome === "sent") {
      const settled = { ...row, state: "sent", reason: null, updated_at: now, sent_at: now, message_id: body.message_id ?? null, text: null, token: null, lease_until: 0, context_missing: body.context_missing === true };
      await tx.put(replyKey(intent_id), settled);
      await tx.delete(inflightKey(intent_id));
      return { ok: true, state: "sent" };
    }
    if (outcome === "refused") {
      const settled = { ...row, state: "refused", reason: "provider_refused", updated_at: now, error_code: body.code ?? null, error_detail: body.error ?? null, retry_after_s: body.retry_after_s ?? null, text: null, token: null, lease_until: 0 };
      await tx.put(replyKey(intent_id), settled);
      await tx.delete(inflightKey(intent_id));
      // A definite refusal releases the client_ref: a later authorized
      // attempt gets a new intent, and this one stays in the ledger.  The
      // bucket keeps the refused intent, so the next attempt can link to it.
      await tx.put(clientRefKey(row.client_ref_hash), { intent_id, released: true, created_at: now });
      if (body.status === 403 && body.code === 131005) await tx.put("wa:binding_suspect", { binding: binding ?? row.binding ?? null, at: now });
      return { ok: true, state: "refused" };
    }
    if (outcome === "outcome_unknown") {
      // The text goes after 24 hours:  Jet keeps its own copy, and the
      // unknown outcome stays unknown for good.
      const settled = { ...row, state: "outcome_unknown", reason: "outcome_unknown", updated_at: now, error_code: body.code ?? null, error_detail: body.error ?? null, token: null, lease_until: 0 };
      await tx.put(replyKey(intent_id), settled);
      await tx.delete(inflightKey(intent_id));
      await tx.put(unknownBodyKey(row.body_hash), { intent_id, created_at: now });
      await schedule(tx, now + REPLY_TEXT_RETENTION, `replytext:${intent_id}`, { kind: "reply_text", intent_id });
      await schedule(tx, now + LEDGER_RETENTION, `unknownbody:${intent_id}`, { kind: "delete", keys: [unknownBodyKey(row.body_hash)] });
      return { ok: true, state: "outcome_unknown" };
    }
    if (outcome === "declined") {
      // The bridge found SEND off before its POST: a decline is a result, so
      // it never becomes an unknown outcome.
      const settled = { ...row, state: "held", reason: "send_disabled", updated_at: now, token: null, lease_until: 0 };
      await tx.put(replyKey(intent_id), settled);
      await tx.delete(inflightKey(intent_id));
      return { ok: true, state: "held" };
    }
    // requeue: the request never left the bridge, so the reply is queued
    // again.  The attempt cap is the same as the bridge's own: past it the
    // reply is refused, and the client_ref is released.
    if ((row.attempts ??  0) >= OUTBOX_MAX_ATTEMPTS) {
      const settled = { ...row, state: "refused", reason: "retry_cap_reached", updated_at: now, error_code: null, error_detail: "retry_cap_reached", text: null, token: null, lease_until: 0 };
      await tx.put(replyKey(intent_id), settled);
      await tx.delete(inflightKey(intent_id));
      await tx.put(clientRefKey(row.client_ref_hash), { intent_id, released: true, created_at: now });
      return { ok: true, state: "refused" };
    }
    const delay = Number.isInteger(body.retry_after_s) ? Math.max(0, Math.min(3600, body.retry_after_s)) : 0;
    const settled = { ...row, state: "queued", reason: null, updated_at: now, token: null, lease_until: 0, attempts: row.attempts ?? 0, not_before: now + delay * 1000 };
    await tx.put(replyKey(intent_id), settled);
    await tx.delete(inflightKey(intent_id));
    await tx.put(queueKey(row.queued_at, intent_id), {});
    return { ok: true, state: "queued" };
  });
}

// Only due expiry jobs are read.  Each acknowledgement job pages its range;
// old messages are never re-scanned hourly and no per-row ack lookup is needed.
export async function sweep(store, now = Date.now()) {
  const end = expiryKey(now + 1);
  const jobs = await store.list({ prefix: "wa:expire:", end, limit: 100 });
  const ack = (await store.get("wa:ack")) ?? 0;
  // A claim whose lease ended with no result is unknown for good, even when
  // no bridge ever asks again.
  await store.transaction(tx => expireInflight(tx, now));
  for (const [jobKey, job] of jobs) {
    try {
      if (job.kind === "delete") await store.delete(job.keys);
      else if (job.kind === "body") {
        const row = await store.get(job.key);
        if (row && !row.expired) {
          if (Number(row.seq) > ack) console.log(JSON.stringify({ event: "wa_unread_expired", count: 1 }));
          await store.put(job.key, { ...row, text: null, expired: true });
        }
      } else if (job.kind === "reply_ledger") {
        await store.delete(replyKey(job.intent_id));
        await store.delete([queueKey(job.queued_at, job.intent_id), inflightKey(job.intent_id)]);
        const mapping = await store.get(clientRefKey(job.client_ref_hash));
        // The bucket belongs to this intent only: a later attempt that took
        // the same client_ref keeps its own mapping.
        if (mapping && mapping.intent_id === job.intent_id) await store.delete(clientRefKey(job.client_ref_hash));
      } else if (job.kind === "reply_text") {
        // An unknown reply keeps its text for 24 hours, and then it goes.
        const row = await store.get(replyKey(job.intent_id));
        if (row && row.state === "outcome_unknown") {
          await store.put(replyKey(job.intent_id), { ...row, text: null });
        }
      } else if (job.kind === "reply_expire") {
        const row = await store.get(replyKey(job.intent_id));
        // A held reply expires unsent 24 hours after it was queued.
        if (row && row.state === "held") {
          await store.put(replyKey(job.intent_id), { ...row, state: "expired", reason: row.reason ??  "held_expired", updated_at: now, expired_at: now, text: null });
        }
      } else if (job.kind === "pending_ack") {
        const page = await store.list({ prefix: "wa:pending:owner:", end: `wa:pending:owner:${String(job.through + 1).padStart(20, "0")}`, limit: 100 });
        if (page.size) await store.delete([...page.keys()]);
        if (page.size === 100) continue;
      } else if (job.kind === "ack") {
        const page = await store.list({ prefix: "wa:message:", start: key(job.after + 1), end: key(job.through + 1), limit: 100 });
        let last = job.after;
        for (const [k, row] of page) { await store.put(k, { ...row, text: null, expired: true }); last = Number(row.seq); }
        if (page.size === 100 && last < job.through) {
          await store.put(jobKey, { ...job, after: last });
          continue;
        }
      } else throw new Error("invalid_expiry_job");
      await store.delete(jobKey);
    } catch {
      // Retry a poison/transient job later, without a one-second alarm loop.
      console.log(JSON.stringify({ event: "wa_expiry_job_failed" }));
      const failures = Math.min(7, (job?.failures ?? 0) + 1);
      const retryAt = now + Math.min(3600000, 60000 * 2 ** (failures - 1));
      try {
        await store.put(expiryKey(retryAt, jobKey.split(":").slice(3).join(":")), { ...job, failures });
        await store.delete(jobKey);
      } catch { /* Storage outage: preserve the original job for platform retry. */ }
    }
  }
  return (await store.list({ prefix: "wa:expire:", end, limit: 1 })).size > 0;
}
