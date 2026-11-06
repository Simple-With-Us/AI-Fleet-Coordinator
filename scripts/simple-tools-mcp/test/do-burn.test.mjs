// DO burn reduction (R1 + R2), state layer:  the pause flag folded into
// gbBearerCheck and the batched audit write.  Each composed operation is pinned
// against the sequential operations it replaces, over the same store, so the
// equivalence is executable.

import { withoutExpiry } from "./helpers.mjs";
import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import { newGbBearer } from "../src/gb-bearer.js";
import { GB_IDENTITIES } from "../src/delegated-identities.js";

const T0 = Date.parse("2026-10-09T06:00:00Z");

test("gbBearerCheck reports the pause flag only on an accepted bearer", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(GB_IDENTITIES[0]);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: T0 + 1e9, mcpAccept: true }, T0);
  assert.deepEqual(withoutExpiry(await S.gbBearerCheck(store, a.hash, T0 + 5)), { ok: true, mcp_accept: true, paused: false });
  await S.setPaused(store, { by: "jay", paused: true }, T0 + 6);
  assert.deepEqual(withoutExpiry(await S.gbBearerCheck(store, a.hash, T0 + 7)), { ok: true, mcp_accept: true, paused: true });
  // A wrong guess learns nothing about the seat:  the refusal shape is
  // byte-identical to before the fold, paused seat or not.
  const b = await newGbBearer(GB_IDENTITIES[0]);
  assert.deepEqual(await S.gbBearerCheck(store, b.hash, T0 + 8), { ok: false, reason: "mismatch" });
  assert.deepEqual(await S.gbBearerCheck(store, "0".repeat(64), T0 + 9), { ok: false, reason: "mismatch" });
});

test("auditCallBatch lands every row exactly as sequential auditCall writes would", async () => {
  const rows = [
    { seat: "ECHO", tool: "whoami", outcome: "ok", latency_ms: 12 },
    { seat: "ECHO", tool: "zulip_post", outcome: "error", error_code: "rate_limited", error_message: "slow down", retryable: true, retry_after_s: 3 },
    { seat: "ECHO", tool: "zulip_read_topic", outcome: "ok", grant_ref: "abcdef012345", asn: 13335, country: "US" },
  ];
  const batched = S.memoryStore();
  const written = await S.auditCallBatch(batched, rows.map((row, i) => ({ row, at: T0 + i * 1000 })));
  assert.deepEqual(written, { written: 3 });
  const sequential = S.memoryStore();
  for (const [i, row] of rows.entries()) await S.auditCall(sequential, row, T0 + i * 1000);
  assert.deepEqual(await S.callTail(batched, 10), await S.callTail(sequential, 10));
  assert.deepEqual(await S.errorTail(batched, 10), await S.errorTail(sequential, 10));
  // The per-entry time is the row's time, not the flush time.
  const tail = await S.callTail(batched, 10);
  assert.deepEqual(tail.map((r) => r.ts), [T0 + 2000, T0 + 1000, T0]);
});

test("auditCallBatch lands every entry, however large the batch - no silent truncation", async () => {
  const store = S.memoryStore();
  const entries = Array.from({ length: 1001 }, (_, i) => ({ row: { seat: "ECHO", tool: "whoami", outcome: "ok" }, at: T0 + i }));
  assert.deepEqual(await S.auditCallBatch(store, entries), { written: 1001 });
  assert.equal((await S.callTail(store, 2000)).length, 1001);
});

test("auditCallBatch of nothing writes nothing, and a non-array is refused", async () => {
  const store = S.memoryStore();
  assert.deepEqual(await S.auditCallBatch(store, []), { written: 0 });
  assert.deepEqual(await S.callTail(store, 10), []);
  await assert.rejects(() => S.auditCallBatch(store, null), TypeError);
});

test("gbBearerCheck carries the bearer's expiry, and only on an accepted bearer", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(GB_IDENTITIES[0]);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: T0 + 1e9, mcpAccept: true }, T0);
  assert.equal((await S.gbBearerCheck(store, a.hash, T0 + 5)).expires_at, T0 + 1e9);
  const b = await newGbBearer(GB_IDENTITIES[0]);
  assert.equal((await S.gbBearerCheck(store, b.hash, T0 + 5)).expires_at, undefined);
});

/** Writes per key, over a real memory store:  what a Durable Object bills as rows. */
function countingStore() {
  const store = S.memoryStore();
  const writes = {};
  const put = store.put;
  store.put = (key, value) => {
    writes[key] = (writes[key] ?? 0) + 1;
    return put(key, value);
  };
  return { store, writes };
}

test("a batch writes each changed key once, however many rows it holds", async () => {
  const { store, writes } = countingStore();
  const row = { seat: "ECHO", tool: "whoami", outcome: "ok", grant_ref: "abcdef012345", asn: 13335, country: "US" };
  await S.auditCallBatch(store, Array.from({ length: 50 }, (_, i) => ({ row, at: T0 + i })));
  assert.deepEqual(writes, { call_days: 1, [`calls:2026-10-09`]: 1, [`calls_bytes:2026-10-09`]: 1, grant_geo: 1 });
  assert.equal((await S.callTail(store, 100)).length, 50);
});

test("a later batch on the same day and grant writes only the call rows", async () => {
  const { store, writes } = countingStore();
  const row = { seat: "ECHO", tool: "whoami", outcome: "ok", grant_ref: "abcdef012345", asn: 13335, country: "US" };
  await S.auditCallBatch(store, [{ row, at: T0 }]);
  for (const key of Object.keys(writes)) delete writes[key];
  await S.auditCallBatch(store, [{ row, at: T0 + 1000 }, { row, at: T0 + 2000 }]);
  assert.deepEqual(writes, { [`calls:2026-10-09`]: 1, [`calls_bytes:2026-10-09`]: 1 });
});

test("a geo change still writes the geo row and audits it", async () => {
  const store = S.memoryStore();
  const row = { seat: "ECHO", tool: "whoami", outcome: "ok", grant_ref: "abcdef012345", asn: 13335, country: "US" };
  await S.auditCallBatch(store, [{ row, at: T0 }, { row: { ...row, asn: 7018 }, at: T0 + 1 }]);
  assert.equal((await store.get("grant_geo")).abcdef012345.asn, 7018);
  assert.ok((await S.tail(store, 10)).some((entry) => entry.event === "geo_change"));
});

test("expired call rows are deleted, not stored as null", async () => {
  const store = S.memoryStore();
  await S.auditCall(store, { seat: "ECHO", tool: "whoami", outcome: "ok" }, T0);
  const day = [...store.map.keys()].find((key) => key.startsWith("calls:"));
  assert.ok(day);
  await S.auditCall(store, { seat: "ECHO", tool: "whoami", outcome: "ok" }, T0 + 400 * 24 * 3600 * 1000);
  assert.equal(store.map.has(day), false);
  assert.equal(store.map.has(day.replace("calls:", "calls_bytes:")), false);
  assert.equal((await store.get("call_days")).length, 1);
});
