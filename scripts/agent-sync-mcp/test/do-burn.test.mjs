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
  // First write of the day:  the day list, the index, the head chunk and the geo row.
  assert.deepEqual(writes, { call_days: 1, "cidx:2026-10-09": 1, "cchunk:2026-10-09:000000": 1, grant_geo: 1 });
  assert.equal((await S.callTail(store, 100)).length, 50);
});

test("a later batch on the same day and grant writes only the head chunk:  one row", async () => {
  const { store, writes } = countingStore();
  const row = { seat: "ECHO", tool: "whoami", outcome: "ok", grant_ref: "abcdef012345", asn: 13335, country: "US" };
  await S.auditCallBatch(store, [{ row, at: T0 }]);
  for (const key of Object.keys(writes)) delete writes[key];
  await S.auditCallBatch(store, [{ row, at: T0 + 1000 }, { row, at: T0 + 2000 }]);
  assert.deepEqual(writes, { "cchunk:2026-10-09:000000": 1 });
});

test("a full head chunk seals into the index:  the index is written once per chunk", async () => {
  const { store, writes } = countingStore();
  const row = { seat: "ECHO", tool: "whoami", outcome: "ok" };
  const n = 2 * S.CALL_CHUNK_ROWS + 50;
  for (let i = 0; i < n; i++) await S.auditCallBatch(store, [{ row: { ...row, message_id: i }, at: T0 + i }]);
  assert.equal(writes["cidx:2026-10-09"], 3); // created, then sealed twice
  const tail = await S.callTail(store, 10000);
  assert.equal(tail.length, n);
  assert.deepEqual(tail.map((r) => r.message_id), Array.from({ length: n }, (_, i) => n - 1 - i));
});

test("the day cap drops whole oldest chunks and keeps the newest rows", async () => {
  const { store, writes } = countingStore();
  const row = { seat: "ECHO", tool: "whoami", outcome: "ok" };
  const entries = Array.from({ length: 5250 }, (_, i) => ({ row: { ...row, message_id: i }, at: T0 + i }));
  for (let i = 0; i < entries.length; i += 50) await S.auditCallBatch(store, entries.slice(i, i + 50));
  const tail = await S.callTail(store, 10000);
  assert.ok(tail.length <= 5000 && tail.length > 5000 - S.CALL_CHUNK_ROWS, `kept ${tail.length}`);
  assert.equal(tail[0].message_id, 5249, "the newest row is kept");
  assert.deepEqual(tail.map((r) => r.message_id), Array.from({ length: tail.length }, (_, i) => 5249 - i), "contiguous newest rows");
  const chunkKeys = [...store.map.keys()].filter((k) => k.startsWith("cchunk:"));
  assert.ok(chunkKeys.length <= 51, `${chunkKeys.length} chunk keys`);
});

const blobRows = (n, from, tool) => Array.from({ length: n }, (_, i) => ({ ts: T0 + from + i, seat: "ECHO", tool, outcome: "ok", message_id: from + i }));

test("an old-layout day is folded into chunks on the next flush and capped 200 rows at a time", async () => {
  const store = S.memoryStore();
  const day = "2026-10-09";
  await store.put(`calls:${day}`, blobRows(4950, 0, "old"));
  await store.put(`calls_bytes:${day}`, { count: 4950, bytes: 1 });
  await store.put("call_days", [day]);
  // Before any new flush callTail still reads the blob.
  assert.deepEqual((await S.callTail(store, 2)).map((r) => r.message_id), [4949, 4948]);
  await S.auditCall(store, { seat: "ECHO", tool: "new", outcome: "ok", message_id: 99999 }, T0 + 10_000);
  assert.equal(store.map.has(`calls:${day}`), false);
  assert.equal(store.map.has(`calls_bytes:${day}`), false);
  let tail = await S.callTail(store, 10000);
  assert.equal(tail.length, 4951);
  assert.deepEqual(tail.slice(0, 3).map((r) => r.message_id), [99999, 4949, 4948]);
  // Past the cap the oldest whole chunk goes, never the whole pre-deploy day.
  for (let i = 0; i < 60; i++) await S.auditCall(store, { seat: "ECHO", tool: "new", outcome: "ok", message_id: 100000 + i }, T0 + 20_000 + i);
  tail = await S.callTail(store, 10000);
  assert.ok(tail.length <= 5000 && tail.length > 5000 - S.CALL_CHUNK_ROWS, `kept ${tail.length}`);
  assert.equal(tail[0].message_id, 100059);
});

test("a full 5000-row old-layout day keeps its newest rows when the first chunked row lands", async () => {
  const store = S.memoryStore();
  await store.put("calls:2026-10-09", blobRows(5000, 0, "old"));
  await store.put("call_days", ["2026-10-09"]);
  await S.auditCallBatch(store, [{ row: { seat: "ECHO", tool: "new", outcome: "ok", message_id: 99999 }, at: T0 + 10_000 }]);
  const tail = await S.callTail(store, 10000);
  assert.ok(tail.length > 5000 - S.CALL_CHUNK_ROWS, `kept ${tail.length}`);
  assert.deepEqual(tail.slice(0, 2).map((r) => r.message_id), [99999, 4999]);
});

test("rows a same-day rollback wrote to the blob are shown, then folded in order", async () => {
  const store = S.memoryStore();
  const day = "2026-10-09";
  for (let i = 0; i < 3; i++) await S.auditCall(store, { seat: "ECHO", tool: "new", outcome: "ok", message_id: i }, T0 + i);
  // The rollback's code appends to calls:<day> next to the chunks.
  await store.put(`calls:${day}`, blobRows(4, 10, "rollback"));
  assert.deepEqual((await S.callTail(store, 10)).map((r) => r.message_id), [13, 12, 11, 10, 2, 1, 0]);
  await S.auditCall(store, { seat: "ECHO", tool: "new", outcome: "ok", message_id: 20 }, T0 + 100);
  assert.equal(store.map.has(`calls:${day}`), false);
  assert.deepEqual((await S.callTail(store, 10)).map((r) => r.message_id), [20, 13, 12, 11, 10, 2, 1, 0]);
});

test("an in-use grant is never the one evicted from the geo map", async () => {
  const store = S.memoryStore();
  const geo = (ref, asn = 13335) => ({ seat: "ECHO", tool: "whoami", outcome: "ok", grant_ref: ref, asn, country: "US" });
  await S.auditCall(store, geo("a00000000000"), T0);
  // The busy grant is used between every new grant, so it is skipped while the map
  // has room;  the 50th new grant overflows the map and evicts one entry.
  for (let i = 0; i < 50; i++) {
    await S.auditCall(store, geo("a00000000000"), T0 + 1000 + i * 1000);
    await S.auditCall(store, geo(`b${String(i).padStart(11, "0")}`), T0 + 1500 + i * 1000);
  }
  const map = await store.get("grant_geo");
  assert.equal(Object.keys(map).length, 50);
  assert.ok(map.a00000000000, "the busy grant is not the one evicted");
  assert.equal(map.b00000000000, undefined, "the grant seen longest ago is");
  // Its very next call from a new place is still caught.
  await S.auditCall(store, geo("a00000000000", 4134), T0 + 70_000);
  assert.ok((await S.tail(store, 10)).some((e) => e.event === "geo_change" && e.grant_ref === "a00000000000"));
});

test("auditCallBatch matches sequential auditCall across chunk boundaries", async () => {
  const rows = Array.from({ length: 333 }, (_, i) => ({ seat: "ECHO", tool: "t", outcome: i % 37 === 0 ? "error" : "ok", error_code: i % 37 === 0 ? "boom" : undefined, message_id: i, grant_ref: "abcdef012345", asn: i % 100 === 0 ? 7018 : 13335, country: "US" }));
  const batched = S.memoryStore();
  await S.auditCallBatch(batched, rows.map((row, i) => ({ row, at: T0 + i })));
  const sequential = S.memoryStore();
  for (const [i, row] of rows.entries()) await S.auditCall(sequential, row, T0 + i);
  assert.deepEqual(await S.callTail(batched, 1000), await S.callTail(sequential, 1000));
  assert.deepEqual(await S.errorTail(batched, 100, T0 + 1000), await S.errorTail(sequential, 100, T0 + 1000));
  assert.deepEqual(new Map([...batched.map].sort()), new Map([...sequential.map].sort()));
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
  const oldKeys = [...store.map.keys()].filter((key) => key.startsWith("cidx:") || key.startsWith("cchunk:"));
  assert.equal(oldKeys.length, 2);
  await S.auditCall(store, { seat: "ECHO", tool: "whoami", outcome: "ok" }, T0 + 400 * 24 * 3600 * 1000);
  for (const key of oldKeys) assert.equal(store.map.has(key), false, key);
  assert.equal((await store.get("call_days")).length, 1);
});

test("two grants of one seat alternating do not rewrite the geo row every flush", async () => {
  const { store, writes } = countingStore();
  const a = { seat: "ECHO", tool: "whoami", outcome: "ok", grant_ref: "aaaaaaaaaaaa", asn: 13335, country: "US" };
  const b = { ...a, grant_ref: "bbbbbbbbbbbb" };
  for (let i = 0; i < 40; i++) await S.auditCallBatch(store, [{ row: i % 2 ? b : a, at: T0 + i * 6000 }]);
  assert.equal(writes.grant_geo, 2, "one write per grant, then none for the rest of the hour");
  // After an hour the newest-order position is refreshed at most once per grant.
  for (let i = 0; i < 4; i++) await S.auditCallBatch(store, [{ row: i % 2 ? b : a, at: T0 + 3_700_000 + i * 6000 }]);
  assert.ok(writes.grant_geo <= 4, `${writes.grant_geo} geo writes`);
  // A change of place is still caught at once.
  await S.auditCallBatch(store, [{ row: { ...a, asn: 7018 }, at: T0 + 3_800_000 }]);
  assert.ok((await S.tail(store, 10)).some((entry) => entry.event === "geo_change" && entry.grant_ref === "aaaaaaaaaaaa"));
});

test("chunked days left behind by a rollback to the blob layout are swept once past retention", async () => {
  const store = S.memoryStore();
  await S.auditCall(store, { seat: "ECHO", tool: "whoami", outcome: "ok" }, T0);
  // A rollback's retention dropped the day from call_days but knew nothing of cidx:/cchunk:.
  await store.put("call_days", []);
  assert.ok([...store.map.keys()].some((k) => k.startsWith("cchunk:2026-10-09")));
  await S.auditCall(store, { seat: "ECHO", tool: "whoami", outcome: "ok" }, T0 + 100 * 24 * 3600 * 1000);
  assert.deepEqual([...store.map.keys()].filter((k) => k.includes("2026-10-09")), []);
  assert.equal((await S.callTail(store, 10)).length, 1);
});

test("an audit flush at the day cap stays well under a millisecond of work", async () => {
  const store = S.memoryStore();
  const row = { seat: "ECHO", tool: "gr_wf_checks", outcome: "ok", grant_ref: "abcdef012345", asn: 13335, country: "US", latency_ms: 700, client_id: "c".repeat(40) };
  const all = Array.from({ length: 5000 }, (_, i) => ({ row: { ...row, message_id: i }, at: T0 + i }));
  for (let i = 0; i < all.length; i += 100) await S.auditCallBatch(store, all.slice(i, i + 100));
  const times = [];
  for (let i = 0; i < 50; i++) {
    const t = performance.now();
    await S.auditCallBatch(store, [{ row: { ...row, message_id: 10_000 + i }, at: T0 + 6000 + i }]);
    times.push(performance.now() - t);
  }
  times.sort((x, y) => x - y);
  // memoryStore clones every value on get and put, as the DO's serializer does, so this bounds the real cost.
  assert.ok(times[25] < 1, `median ${times[25].toFixed(3)} ms`);
});
