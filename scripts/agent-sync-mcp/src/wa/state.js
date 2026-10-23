// Transactions persist the inbox and permanent content-free replay tombstones
// together.  Body expiry never removes dedupe.  No arbitrary wake budget.
import { redact } from "./contract.js";
const RETENTION = 7 * 86400000;
const key = seq => `wa:message:${String(seq).padStart(20, "0")}`;

export async function ingest(store, { body, owner, known = [] }, now = Date.now()) {
  return store.transaction(async tx => {
    if (!owner || body.agent_hash !== owner.agent_hash) return { ok: false, reason: "binding_mismatch" };
    let seq = (await tx.get("wa:seq")) ?? 0;
    let stored = 0;
    for (const m of body.messages) {
      if (m.sender_hash !== owner.sender_hash || m.timestamp <= owner.cutoff || now / 1000 - m.timestamp > 31 * 86400) continue;
      const seen = `wa:seen:${m.id_hash}`;
      if (await tx.get(seen)) continue;
      const silent = m.text?.startsWith("👀") === true;
      seq++;
      await tx.put(key(seq), { seq: String(seq), text: redact(m.text, known), type: m.type, timestamp: m.timestamp, received_at: now, silent, surfaced: false });
      // Do not keep raw WhatsApp ids or senders, even in the tombstones.
      await tx.put(seen, true);
      if (!silent) await tx.put(`wa:pending:owner:${String(seq).padStart(20, "0")}`, { id: `wa:owner:${seq}`, source: "authenticated_owner" });
      stored++;
    }
    await tx.put("wa:seq", seq);
    await tx.put("wa:last_ingest", now);
    return { ok: true, processed: true, next_offset: body.next_offset, stored };
  });
}
export async function operator(store, { requestId }) {
  return store.transaction(async tx => {
    const id = `wa:operator:${requestId}`;
    if (await tx.get(id)) return { ok: true, duplicate: true };
    await tx.put(id, true);
    await tx.put(`wa:pending:operator:${requestId}`, { id, source: "operator" });
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
export async function claim(store, {}, now = Date.now()) {
  return store.transaction(async tx => {
    if (await tx.get("paused")) return null;
    const rows = await tx.list({ prefix: "wa:pending:", limit: 100 });
    for (const [k, row] of rows) {
      if (row.lease_until > now) return null;
      const token = crypto.randomUUID();
      const keys = [];
      for (const [candidate, item] of rows) {
        if (item.source !== row.source || item.lease_until > now) continue;
        await tx.put(candidate, { ...item, token, lease_until: now + 30000 });
        keys.push(candidate);
      }
      return { ...row, key: k, keys, token };
    }
    return null;
  });
}
export async function settle(store, { keys, key: k, token, delivered }) {
  return store.transaction(async tx => {
    for (const pendingKey of keys ?? [k]) {
      const row = await tx.get(pendingKey);
      if (!row || row.token !== token) continue;
      if (delivered) await tx.delete(pendingKey);
      else await tx.put(pendingKey, { ...row, lease_until: 0 });
    }
  });
}

// A bounded alarm page removes expired bodies even if Jet never reads again.
// Tombstones and expiry stubs survive, so unread messages remain visible.
export async function sweep(store, now = Date.now()) {
  const start = (await store.get("wa:sweep_cursor")) ?? "wa:message:";
  const page = await store.list({ prefix: "wa:message:", start, limit: 100 });
  let last;
  for (const [k, row] of page) {
    last = k;
    const acknowledgements = await store.list({ prefix: "wa:ack_at:", start: `wa:ack_at:${row.seq.padStart(20, "0")}`, limit: 1 });
    const acknowledgedAt = acknowledgements.values().next().value;
    if (now - row.received_at >= RETENTION || (acknowledgedAt && now - acknowledgedAt >= 86400000)) {
      await store.put(k, { ...row, text: null, expired: true });
    }
  }
  const more = page.size === 100;
  await store.put("wa:sweep_cursor", more ? last + "\u0000" : "wa:message:");
  return more;
}
