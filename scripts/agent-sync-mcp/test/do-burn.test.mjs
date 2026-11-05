// DO burn reduction (R1 + R2), state layer:  the composed check+reserve,
// the pause flag folded into gbBearerCheck, and the batched audit write.
// Each composed operation is pinned against the sequential operations it
// replaces, over the same store, so the equivalence is executable.

import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import { PROPS_PHASE } from "../src/config.js";
import { newGbBearer } from "../src/gb-bearer.js";
import { GB_IDENTITIES } from "../src/delegated-identities.js";

const T0 = Date.parse("2026-10-09T06:00:00Z");

async function grantedStore() {
  const store = S.memoryStore();
  await S.arm(store, { by: "jay" }, T0);
  const { epoch } = await S.approve(store, { by: "jay" }, T0);
  return { store, props: { epoch, approvedAt: T0, phase: PROPS_PHASE } };
}

test("checkAndReserve on a live grant takes exactly the slot reserve takes", async () => {
  const { store, props } = await grantedStore();
  const solo = await grantedStore();
  const composed = await S.checkAndReserve(store, { ...props, kind: "read" }, T0 + 1000);
  const sequential = await S.reserve(solo.store, { kind: "read" }, T0 + 1000);
  assert.deepEqual(composed, { code: "ok", slot: sequential });
  // The slot was really consumed:  the next read waits out the read spacing.
  const next = await S.reserve(store, { kind: "read" }, T0 + 1000);
  assert.equal(next.ok, true);
  assert.equal(next.waitMs, S.READ_SPACING_MS);
});

test("checkAndReserve refuses like check and spends no slot, across the refusal matrix", async () => {
  const cases = [
    [{ epoch: "1" }, "bad_props"],
    [{ phase: 0 }, "stale_phase"],
    [{ epoch: 99 }, "stale_epoch"],
    [{ approvedAt: T0 - 100 * 24 * 3600 * 1000 }, "grant_too_old"],
  ];
  for (const [patch, code] of cases) {
    const { store, props } = await grantedStore();
    const composed = await S.checkAndReserve(store, { ...props, ...patch, kind: "read" }, T0 + 1000);
    assert.deepEqual(composed, { code }, code);
    assert.deepEqual(composed.code, (await S.check(store, { ...props, ...patch }, T0 + 1000)).code);
    // No slot, no budget:  a following reserve is a first slot, not a spaced one.
    const slot = await S.reserve(store, { kind: "read" }, T0 + 1000);
    assert.deepEqual(slot, { ok: true, waitMs: 0 }, code);
  }
});

test("checkAndReserve on a paused seat reports the pause and spends no slot", async () => {
  const { store, props } = await grantedStore();
  await S.setPaused(store, { by: "jay", paused: true }, T0 + 500);
  assert.deepEqual(await S.checkAndReserve(store, { ...props, kind: "write" }, T0 + 1000), { code: "paused" });
  await S.setPaused(store, { by: "jay", paused: false }, T0 + 1500);
  // Still a first slot after unpausing:  the paused attempt reserved nothing.
  assert.deepEqual(await S.reserve(store, { kind: "write" }, T0 + 2000), { ok: true, waitMs: 0 });
});

test("checkAndReserve surfaces reserve's own refusals in the slot, unchanged", async () => {
  const { store, props } = await grantedStore();
  await store.put("cooldown_until", T0 + 60_000);
  const composed = await S.checkAndReserve(store, { ...props, kind: "read" }, T0 + 1000);
  assert.equal(composed.code, "ok");
  assert.equal(composed.slot.ok, false);
  assert.equal(composed.slot.code, "rate_limited");
  assert.equal(composed.slot.retryAfterS, 59);
});

test("checkAndReserve is atomic:  a state change between check and reserve cannot split it", async () => {
  const { store, props } = await grantedStore();
  // Ten composed calls in flight at once all validate the same grant state;
  // each takes a distinct, spacing-ordered slot (the transaction serializes).
  const results = await Promise.all(
    Array.from({ length: 10 }, () => S.checkAndReserve(store, { ...props, kind: "read" }, T0 + 1000)),
  );
  assert.ok(results.every((r) => r.code === "ok" && r.slot.ok));
  const waits = results.map((r) => r.slot.waitMs).sort((a, b) => a - b);
  assert.deepEqual(waits, Array.from({ length: 10 }, (_, i) => i * S.READ_SPACING_MS));
});

test("checkAndReserve requires a transactional store", async () => {
  const bare = { get: async () => undefined, put: async () => {} };
  await assert.rejects(() => S.checkAndReserve(bare, { epoch: 0, approvedAt: 0, phase: PROPS_PHASE, kind: "read" }, T0), TypeError);
});

test("gbBearerCheck reports the pause flag only on an accepted bearer", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(GB_IDENTITIES[0]);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: T0 + 1e9, mcpAccept: true }, T0);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, T0 + 5), { ok: true, mcp_accept: true, paused: false });
  await S.setPaused(store, { by: "jay", paused: true }, T0 + 6);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, T0 + 7), { ok: true, mcp_accept: true, paused: true });
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
