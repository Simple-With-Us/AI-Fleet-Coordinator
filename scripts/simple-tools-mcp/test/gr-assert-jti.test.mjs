// The gr-assert one-use jti store (src/seat-state.js claimGrAssert):  the
// atomic gate-check + jti claim behind /internal/gr-assert-verify.  Covers
// replay, restart durability, overflow fail-closed, pruning, and concurrent
// claims under a serializing store.
//
// Concurrency note:  the real store is a Durable Object - the runtime's input
// gate processes one RPC of one object at a time, and claimGrAssert
// additionally wraps its reads and write in store.transaction.  The harness
// here models exactly that with a serialized transaction queue;  workerd
// integration coverage is the host's integration step.

import test from "node:test";
import assert from "node:assert/strict";
import { claimGrAssert } from "../src/seat-state.js";

const GATE = { epoch: 7, approvedAt: Date.now(), phase: 2 };

// A memStore whose transactions SERIALIZE, matching the Durable Object input
// gate + storage transaction semantics claimGrAssert relies on.
function serialStore(shared = new Map()) {
  let chain = Promise.resolve();
  const store = {
    get: async (k) => (shared.has(k) ? shared.get(k) : undefined),
    put: async (k, v) => void shared.set(k, v),
    delete: async (k) => void shared.delete(k),
    transaction: (cb) => {
      const result = chain.then(() => cb(store));
      chain = result.then(() => {}, () => {});
      return result;
    },
  };
  return store;
}

async function armedStore(shared) {
  const store = serialStore(shared);
  await store.put("epoch", 7);
  return store;
}

test("the gate check and the claim are one decision:  bad epoch spends nothing", async () => {
  const store = await armedStore();
  const r1 = await claimGrAssert(store, { ...GATE, epoch: 8, jti: "a".repeat(16), expiresAt: Date.now() + 60_000 });
  assert.deepEqual(r1, { ok: false, reason: "grant_invalid" });
  // The jti was not claimed:  with the right epoch it still goes through.
  const r2 = await claimGrAssert(store, { ...GATE, jti: "a".repeat(16), expiresAt: Date.now() + 60_000 });
  assert.deepEqual(r2, { ok: true });
});

test("a paused seat refuses and spends nothing", async () => {
  const store = await armedStore();
  await store.put("paused", true);
  const r = await claimGrAssert(store, { ...GATE, jti: "b".repeat(16), expiresAt: Date.now() + 60_000 });
  assert.deepEqual(r, { ok: false, reason: "paused" });
  await store.put("paused", false);
  assert.deepEqual(await claimGrAssert(store, { ...GATE, jti: "b".repeat(16), expiresAt: Date.now() + 60_000 }), { ok: true });
});

test("one jti is claimed exactly once, under concurrent claims", async () => {
  const store = await armedStore();
  const jti = "c".repeat(16);
  const expiresAt = Date.now() + 60_000;
  const results = await Promise.all([
    claimGrAssert(store, { ...GATE, jti, expiresAt }),
    claimGrAssert(store, { ...GATE, jti, expiresAt }),
    claimGrAssert(store, { ...GATE, jti, expiresAt }),
  ]);
  assert.deepEqual(results.filter((r) => r.ok).length, 1);
  assert.deepEqual(results.filter((r) => !r.ok && r.reason === "replayed").length, 2);
});

test("the claim survives a restart (a fresh store over the same storage)", async () => {
  const shared = new Map();
  const before = await armedStore(shared);
  const jti = "d".repeat(16);
  assert.deepEqual(await claimGrAssert(before, { ...GATE, jti, expiresAt: Date.now() + 60_000 }), { ok: true });
  // Process restarts:  a new store object over the same durable storage.
  const after = await armedStore(shared);
  assert.deepEqual(await claimGrAssert(after, { ...GATE, jti, expiresAt: Date.now() + 60_000 }), { ok: false, reason: "replayed" });
});

test("at capacity the store fails closed and NEVER evicts a live record", async () => {
  const store = await armedStore();
  const now = Date.now();
  const live = {};
  for (let i = 0; i < 512; i++) live[`flood${String(i).padStart(11, "0")}`] = now + 90_000;
  await store.put("gr_assert_jtis", live);
  // A 513th distinct jti refuses rather than discarding a live record.
  const flooded = await claimGrAssert(store, { ...GATE, jti: "e".repeat(16), expiresAt: now + 60_000 }, now);
  assert.deepEqual(flooded, { ok: false, reason: "jti_store_full" });
  // And every one of the 512 originals is still claimed:  none was evicted.
  const kept = await store.get("gr_assert_jtis");
  assert.equal(Object.keys(kept).length, 512);
  assert.deepEqual(await claimGrAssert(store, { ...GATE, jti: "flood00000000001", expiresAt: now + 90_000 }, now), { ok: false, reason: "replayed" });
});

test("expired records prune, so capacity refills within one max TTL", async () => {
  const store = await armedStore();
  const now = Date.now();
  const stale = {};
  for (let i = 0; i < 512; i++) stale[`old${String(i).padStart(13, "0")}`] = now - 1_000;
  await store.put("gr_assert_jtis", stale);
  const r = await claimGrAssert(store, { ...GATE, jti: "f".repeat(16), expiresAt: now + 60_000 }, now);
  assert.deepEqual(r, { ok: true });
  assert.equal(Object.keys(await store.get("gr_assert_jtis")).length, 1);
});

test("a record outlives its assertion:  replay after expiry is the window's job", async () => {
  const store = await armedStore();
  const now = Date.now();
  const jti = "g".repeat(16);
  // Claimed with an expiry in the near future;  a replay INSIDE the record's
  // life still refuses even after the assertion's own exp would have passed,
  // because the token itself is expired by then (the route refuses expired
  // tokens before the claim).  Here the record simply stays consistent.
  assert.deepEqual(await claimGrAssert(store, { ...GATE, jti, expiresAt: now + 60_000 }, now), { ok: true });
  assert.deepEqual(await claimGrAssert(store, { ...GATE, jti, expiresAt: now + 60_000 }, now + 30_000), { ok: false, reason: "replayed" });
});
