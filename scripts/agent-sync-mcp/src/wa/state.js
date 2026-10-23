// Transactional inbox and 35-day ledgers.  Timestamp cutoff rejects old replay
// after ledger expiry.  No daily/hourly quota on owner messages.
import { redact } from "./contract.js";
const RETENTION = 7 * 86400000;
const LEDGER_RETENTION = 35 * 86400000;
export const RERING_MS = 10 * 60000;
export const OPERATOR_DEBOUNCE_MS = 30000;
const key = seq => `wa:message:${String(seq).padStart(20, "0")}`;
const expiryKey = (at, id = "") => `wa:expire:${String(at).padStart(20, "0")}:${id}`;
const schedule = (store, at, id, job) => store.put(expiryKey(at, id), job);


export async function ingest(store, { body, owner, known = [] }, now = Date.now()) {
  return store.transaction(async tx => {
    if (!owner || body.agent_hash !== owner.agent_hash) return { ok: false, reason: "binding_mismatch" };
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
      await tx.put(key(seq), { seq: String(seq), text: redact(m.text, known), type: m.type, timestamp: m.timestamp, received_at: now, silent, surfaced: false });
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
export async function read(store, args, now = Date.now()) {
  return store.transaction(async tx => {
    const surfaced = (await tx.get("wa:surfaced")) ?? 0;
    const previousAck = (await tx.get("wa:ack")) ?? 0;
    const ack = args.ack_through;
    if (ack !== undefined && (typeof ack !== "string" || !/^[1-9][0-9]*$/.test(ack) || BigInt(ack) > BigInt(surfaced))) return { ok: false, reason: "not_surfaced" };
    const through = Math.max(previousAck, Number(ack ?? 0));
    if (through > previousAck) {
      await tx.put("wa:ack", through);
      const gate = await tx.get("wa:owner_gate");
      if (gate) await tx.put("wa:owner_gate", { ...gate, next_at: 0 });
      // Logical retirement is the ack checkpoint; physical cleanup is bounded.
      // Claim starts after that checkpoint, so old rows cannot hide new wakes.
      const cleanup = await tx.list({ prefix: "wa:pending:owner:", end: `wa:pending:owner:${String(through + 1).padStart(20, "0")}`, limit: 100 });
      if (cleanup.size) await tx.delete([...cleanup.keys()]);
      if (cleanup.size === 100) await schedule(tx, now, `pending:${through}`, { kind: "pending_ack", through });
      // Retention starts when the acknowledgement is accepted.  The sweep
      // processes its bounded range using these content-free checkpoints.
      const checkpoint = `wa:ack_at:${String(through).padStart(20, "0")}`;
      await tx.put(checkpoint, now);
      await schedule(tx, now + 86400000, `ack:${through}`, { kind: "ack", after: previousAck, through });
      await schedule(tx, now + LEDGER_RETENTION, `checkpoint:${through}`, { kind: "delete", keys: [checkpoint] });
    }
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
export async function claim(store, { destination = "" } = {}, now = Date.now()) {
  return store.transaction(async tx => {
    if (await tx.get("paused")) return null;
    const ack = (await tx.get("wa:ack")) ?? 0;
    const owners = await tx.list({ prefix: "wa:pending:owner:", start: `wa:pending:owner:${String(ack + 1).padStart(20, "0")}`, limit: 100 });
    const operators = await tx.list({ prefix: "wa:pending:operator:", limit: 1 });
    const rows = new Map([...owners, ...operators]);
    const ownerGate = (await tx.get("wa:owner_gate")) ?? {};
    for (const [k, row] of rows) {
      if (row.lease_until > now) continue;
      if (row.source === "authenticated_owner" && (ownerGate.next_at > now || (ownerGate.destination === destination && ownerGate.blocked))) continue;
      if (row.next_ring_at > now || (row.destination === destination && row.blocked)) continue;
      const token = crypto.randomUUID();
      const keys = [];
      for (const [candidate, item] of rows) {
        if (item.source !== row.source || item.lease_until > now || item.next_ring_at > now || (item.destination === destination && item.blocked)) continue;
        await tx.put(candidate, { ...item, token, destination, ring: row.ring ?? 0, lease_until: now + 30000 });
        keys.push(candidate);
      }
      return { ...row, key: k, keys, token };
    }
    return null;
  });
}
export async function settle(store, { keys, key: k, token, delivered, permanent = false, noSubscriber = false }, now = Date.now()) {
  return store.transaction(async tx => {
    for (const pendingKey of keys ?? [k]) {
      const row = await tx.get(pendingKey);
      if (!row || row.token !== token) continue;
      if (delivered && row.source === "operator") { await tx.delete(pendingKey); continue; }
      const failures = permanent ? (row.permanent_failures ?? 0) + 1 : 0;
      const blocked = failures >= 3;
      if (row.source === "authenticated_owner") await tx.put("wa:owner_gate", { destination: row.destination, next_at: noSubscriber ? 0 : now + (delivered || permanent ? RERING_MS : 30000), blocked });
      if (blocked) console.log(JSON.stringify({ event: "wa_delivery_blocked", source: row.source }));
      await tx.put(pendingKey, { ...row, lease_until: 0, next_ring_at: noSubscriber ? 0 : now + (delivered || permanent ? RERING_MS : 30000), permanent_failures: failures, blocked, ring: (row.ring ?? 0) + (delivered ? 1 : 0) });
    }
  });
}

// Only due expiry jobs are read.  Each acknowledgement job pages its range;
// old messages are never re-scanned hourly and no per-row ack lookup is needed.
export async function sweep(store, now = Date.now()) {
  const end = expiryKey(now + 1);
  const jobs = await store.list({ prefix: "wa:expire:", end, limit: 100 });
  const ack = (await store.get("wa:ack")) ?? 0;
  for (const [jobKey, job] of jobs) {
    try {
      if (job.kind === "delete") await store.delete(job.keys);
      else if (job.kind === "body") {
        const row = await store.get(job.key);
        if (row && !row.expired) {
          if (Number(row.seq) > ack) console.log(JSON.stringify({ event: "wa_unread_expired", count: 1 }));
          await store.put(job.key, { ...row, text: null, expired: true });
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
      }
      await store.delete(jobKey);
    } catch {
      // One retryable storage/job failure must not prevent other due deletion.
      console.log(JSON.stringify({ event: "wa_expiry_job_failed" }));
    }
  }
  return (await store.list({ prefix: "wa:expire:", end, limit: 1 })).size > 0;
}
