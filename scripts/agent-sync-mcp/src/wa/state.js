// Transactional inbox and 35-day ledgers.  Timestamp cutoff rejects old replay
// after ledger expiry.  No daily/hourly quota on owner messages.
import { redact } from "./contract.js";
const RETENTION = 7 * 86400000;
const LEDGER_RETENTION = 35 * 86400000;
export const RERING_MS = 10 * 60000;
export const OPERATOR_DEBOUNCE_MS = 30000;
const key = seq => `wa:message:${String(seq).padStart(20, "0")}`;

export async function ingest(store, { body, owner, known = [] }, now = Date.now()) {
  return store.transaction(async tx => {
    if (!owner || body.agent_hash !== owner.agent_hash) return { ok: false, reason: "binding_mismatch" };
    let seq = (await tx.get("wa:seq")) ?? 0;
    let stored = 0; let skipped = 0;
    for (const m of body.messages) {
      if (m.sender_hash !== owner.sender_hash || m.timestamp <= owner.cutoff || now / 1000 - m.timestamp > 31 * 86400) continue;
      const seen = `wa:seen:${m.id_hash}`;
      if (await tx.get(seen)) continue;
      if (m.timestamp > Math.floor(now / 1000) + 60) { await tx.put(seen, now); skipped++; continue; }
      const silent = m.text?.replace(/^[\s\p{Cf}]+/u, "").startsWith("👀") === true;
      seq++;
      await tx.put(key(seq), { seq: String(seq), text: redact(m.text, known), type: m.type, timestamp: m.timestamp, received_at: now, silent, surfaced: false });
      // Do not keep raw WhatsApp ids or senders, even in the tombstones.
      await tx.put(seen, now);
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
    if (await tx.get(id)) return { ok: true, duplicate: true };
    const pending = await tx.list({ prefix: "wa:pending:operator:", limit: 1 });
    const nextAt = (await tx.get("wa:operator_next_at")) ?? 0;
    if (pending.size || now < nextAt) return { ok: false, reason: "operator_busy", retry_after_s: Math.max(1, Math.ceil((nextAt - now) / 1000)) };
    await tx.put(id, now);
    await tx.put("wa:operator_next_at", now + OPERATOR_DEBOUNCE_MS);
    await tx.put(`wa:pending:operator:${requestId}`, { id, source: "operator", created_at: now });
    return { ok: true };
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
      await tx.delete("wa:owner_gate");
      // Only returned owner messages may retire their outstanding doorbells.
      let start = "wa:pending:owner:";
      while (true) {
        const page = await tx.list({ prefix: "wa:pending:owner:", start, limit: 100 });
        for (const [k, row] of page) if (Number(row.seq) <= through) await tx.delete(k);
        if (page.size < 100) break;
        start = [...page.keys()].at(-1) + "\u0000";
      }
      // Retention starts when the acknowledgement is accepted.  The sweep
      // processes its bounded range using these content-free checkpoints.
      await tx.put(`wa:ack_at:${String(through).padStart(20, "0")}`, now);
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
    const rows = await tx.list({ prefix: "wa:pending:", limit: 100 });
    const ownerGate = (await tx.get("wa:owner_gate")) ?? {};
    for (const [k, row] of rows) {
      if (row.lease_until > now) continue;
      if (row.source === "authenticated_owner" && ownerGate.destination === destination && (ownerGate.next_at > now || ownerGate.blocked)) continue;
      if (row.destination === destination && (row.next_ring_at > now || row.blocked)) continue;
      const token = crypto.randomUUID();
      const keys = [];
      for (const [candidate, item] of rows) {
        if (item.source !== row.source || item.lease_until > now || (item.destination === destination && (item.next_ring_at > now || item.blocked))) continue;
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

// A bounded alarm page removes expired bodies even if Jet never reads again.
// Tombstones and expiry stubs survive, so unread messages remain visible.
export async function sweep(store, now = Date.now()) {
  const start = (await store.get("wa:sweep_cursor")) ?? "wa:";
  const page = await store.list({ prefix: "wa:", start, limit: 100 });
  let last;
  for (const [k, row] of page) {
    last = k;
    if (k.startsWith("wa:message:")) {
      const checkpoints = await store.list({ prefix: "wa:ack_at:", start: `wa:ack_at:${row.seq.padStart(20, "0")}`, limit: 1 });
      const ackAt = checkpoints.values().next().value;
      if (now - row.received_at >= LEDGER_RETENTION) { await store.delete(k); continue; }
      if (now - row.received_at >= RETENTION || (ackAt && now - ackAt >= 86400000)) {
        if (!row.expired && !ackAt) console.log(JSON.stringify({ event: "wa_unread_expired", count: 1 }));
        await store.put(k, { ...row, text: null, expired: true });
      }
    } else if (/^wa:(seen:|operator:|ack_at:)/.test(k) && typeof row === "number" && now - row >= LEDGER_RETENTION) await store.delete(k);
    else if (k.startsWith("wa:pending:") && now - row.created_at >= LEDGER_RETENTION) await store.delete(k);
  }
  const more = page.size === 100;
  await store.put("wa:sweep_cursor", more ? last + "\u0000" : "wa:");
  return more;
}
