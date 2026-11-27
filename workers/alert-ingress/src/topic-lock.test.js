// The per-topic lock that serializes standing-status refreshes, and the delivery-barrier
// claim that must stay atomic when the storage has no transaction.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import { __testing } from "./index.js";

const { withTopicLock, claimDeliveryBarrier } = __testing;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
};

test("callers on one key never overlap and run in arrival order", async () => {
  let active = 0;
  let peak = 0;
  const order = [];
  const job = (n) => () =>
    (async () => {
      active += 1;
      peak = Math.max(peak, active);
      await sleep(2);
      order.push(n);
      active -= 1;
      return n;
    })();
  const results = await Promise.all([1, 2, 3, 4, 5].map((n) => withTopicLock("order", job(n), { waitMs: 5000 })));
  assert.deepEqual(results, [1, 2, 3, 4, 5]);
  assert.deepEqual(order, [1, 2, 3, 4, 5]);
  assert.equal(peak, 1);
});

test("a waiter behind a hung holder is refused as busy after its wait, and its work never runs", async () => {
  const hang = deferred();
  const holder = withTopicLock("hung", () => hang.promise, { waitMs: 20 });
  let ran = false;
  await assert.rejects(
    withTopicLock("hung", async () => { ran = true; }, { waitMs: 20, source: "datadog" }),
    (error) => {
      assert.equal(error.code, "status_delivery_failed");
      assert.equal(error.outcome, "busy");
      assert.equal(error.source, "datadog");
      assert.equal(error.message, "status delivery failed");
      return true;
    },
  );
  // Even once the holder lets go, the refused waiter does not run:  no late duplicate work.
  hang.resolve();
  await holder;
  await sleep(10);
  assert.equal(ran, false);
});

test("a refused waiter leaves the queue, so the next caller runs once the holder is done", async () => {
  const hang = deferred();
  const holder = withTopicLock("recover", () => hang.promise, { waitMs: 20 });
  await assert.rejects(withTopicLock("recover", async () => "never", { waitMs: 15 }));
  await assert.rejects(withTopicLock("recover", async () => "never", { waitMs: 15 }));
  hang.resolve();
  await holder;
  assert.equal(await withTopicLock("recover", async () => "next", { waitMs: 20 }), "next");
});

test("a waiter that gets its turn in time runs exactly once and its timer is cleared", async () => {
  const gate = deferred();
  const holder = withTopicLock("turn", () => gate.promise, { waitMs: 50 });
  let runs = 0;
  const waiter = withTopicLock("turn", async () => { runs += 1; return "ok"; }, { waitMs: 50 });
  setTimeout(gate.resolve, 10);
  await holder;
  assert.equal(await waiter, "ok");
  await sleep(70); // past the waiter's own wait:  a stale timer would reject or double-run
  assert.equal(runs, 1);
});

test("a failing holder releases the lock to the next caller", async () => {
  const failed = withTopicLock("fail", async () => { throw new Error("boom"); }, { waitMs: 100 });
  const next = withTopicLock("fail", async () => "after", { waitMs: 100 });
  await assert.rejects(failed, /boom/);
  assert.equal(await next, "after");
});

test("different keys do not wait for each other", async () => {
  const hang = deferred();
  const holder = withTopicLock("a", () => hang.promise, { waitMs: 1000 });
  assert.equal(await withTopicLock("b", async () => "free", { waitMs: 20 }), "free");
  hang.resolve();
  await holder;
});

// A storage with no transaction and a real gap between a read and the write after it.
function gappedStorage() {
  const data = new Map();
  return {
    async get(key) { await sleep(3); return data.get(key); },
    async put(key, value) { await sleep(3); data.set(key, value); },
    data,
  };
}
const claimArgs = { source: "datadog", outcomeKey: "o", barrierKey: "b", revision: 1, statusId: undefined };

test("without a transaction, every claim fails closed without writing", async () => {
  const env = { WRITER_STORAGE: gappedStorage() };
  const results = await Promise.allSettled([claimDeliveryBarrier(env, claimArgs), claimDeliveryBarrier(env, claimArgs)]);
  assert.ok(results.every((result) => result.status === "rejected"));
  assert.equal(await env.WRITER_STORAGE.get(claimArgs.barrierKey), undefined);
});

test("with a transaction, the claim goes through it and never takes the fallback path", async () => {
  const calls = [];
  const txn = { get: async () => undefined, put: async (k, v) => calls.push([k, v]) };
  const env = { WRITER_STORAGE: { transaction: async (fn) => fn(txn) } };
  await claimDeliveryBarrier(env, claimArgs);
  assert.deepEqual(calls.map(([k]) => k), ["o", "b"]);
});
