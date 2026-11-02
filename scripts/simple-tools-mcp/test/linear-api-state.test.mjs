import test from "node:test";
import assert from "node:assert/strict";
import * as state from "../src/seat-state.js";

class FullStore {
  constructor() {
    this.values = new Map();
    this.listCalls = [];
    this.deleted = [];
    this.transactionTail = Promise.resolve();
  }
  async get(key) { return this.values.get(key); }
  async put(key, value) { this.values.set(key, structuredClone(value)); }
  async delete(keyOrKeys) {
    if (Array.isArray(keyOrKeys)) {
      for (const key of keyOrKeys) {
        this.values.delete(key);
        this.deleted.push(key);
      }
      return keyOrKeys.length;
    }
    this.values.delete(keyOrKeys);
    return true;
  }
  async list(options = {}) {
    this.listCalls.push(options);
    const rows = [...this.values.entries()]
      .filter(([key]) => key.startsWith(options.prefix ?? ""))
      .filter(([key]) => options.end === undefined || key < options.end)
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(0, options.limit ?? Infinity);
    return new Map(rows.map(([key, value]) => [key, structuredClone(value)]));
  }
  transaction(callback) {
    const result = this.transactionTail.then(() => callback(this));
    this.transactionTail = result.catch(() => undefined);
    return result;
  }
}

const key = "a".repeat(64);
const otherKey = "b".repeat(64);
const requestSha = "c".repeat(64);
const DAY = state.DAY_MS;
const physicalKey = (day, digest = key) => `linear-api:${String(day).padStart(10, "0")}:${digest}`;

test("same-key mutation retry is a duplicate and stores its receipt outside the shared idem blob", async () => {
  const store = new FullStore();
  await store.put("idem", { unrelated: { state: "sent", id: "old" } });
  const before = await store.get("idem");
  const first = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, 10 * DAY);
  assert.equal(first.verdict, "send");
  await state.linearApiSet(store, { storageKey: first.storageKey, changes: { state: "sent", data: { success: true, id: "p1" } } }, 10 * DAY + 1);
  const again = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, 10 * DAY + 2);
  assert.equal(again.verdict, "duplicate");
  assert.deepEqual(again.row.data, { success: true, id: "p1" });
  assert.deepEqual(await store.get("idem"), before);
  assert.equal(first.storageKey, physicalKey(10));
});

test("same key with different request conflicts, while a fresh pending row is busy", async () => {
  const store = new FullStore();
  const now = 12 * DAY;
  const first = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now);
  assert.equal(first.verdict, "send");
  assert.equal((await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now + 1)).verdict, "busy");
  assert.equal((await state.linearApiBegin(store, { key, request_sha: "d".repeat(64), operation: "projectUpdate" }, now + 2)).verdict, "conflict");
});

test("concurrent begins for one key are serialized to one send and one busy result", async () => {
  const store = new FullStore();
  const now = 13 * DAY;
  const results = await Promise.all([
    state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now),
    state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now),
  ]);
  assert.deepEqual(results.map((result) => result.verdict).sort(), ["busy", "send"]);
  assert.equal([...store.values.keys()].filter((name) => name.startsWith("linear-api:") && name !== "linear-api-cleanup-at").length, 1);
});

test("stale pending and unknown rows reconcile without being replayed", async () => {
  const store = new FullStore();
  const now = 14 * DAY;
  const storageKey = physicalKey(14);
  await store.put(storageKey, { request_sha: requestSha, operation: "projectUpdate", state: "pending", claimed_at: now - state.PENDING_FRESH_MS, expires: now + DAY });
  const stale = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now);
  assert.equal(stale.verdict, "reconcile");
  assert.equal((await store.get(storageKey)).claimed_at, now - state.PENDING_FRESH_MS);

  await store.put(storageKey, { request_sha: requestSha, operation: "projectUpdate", state: "unknown", claimed_at: now - DAY, expires: now + DAY });
  const unknown = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now + 1);
  assert.equal(unknown.verdict, "reconcile");
  assert.equal((await store.get(storageKey)).state, "unknown");
});

test("a key remains discoverable across midnight in the prior-day bucket", async () => {
  const store = new FullStore();
  const beforeMidnight = 20 * DAY + DAY - 5000;
  const first = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, beforeMidnight);
  assert.equal(first.storageKey, physicalKey(20));
  await state.linearApiSet(store, { storageKey: first.storageKey, changes: { state: "sent", data: { success: true } } }, beforeMidnight + 1);
  const afterMidnight = 21 * DAY + 2000;
  const again = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, afterMidnight);
  assert.equal(again.verdict, "duplicate");
  assert.equal(again.storageKey, physicalKey(20));
});

test("expired prior records start a fresh row in today's bucket", async () => {
  const store = new FullStore();
  const now = 30 * DAY;
  await store.put(physicalKey(28), { request_sha: requestSha, operation: "projectUpdate", state: "sent", expires: now - 1 });
  const result = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now);
  assert.equal(result.verdict, "send");
  assert.equal(result.storageKey, physicalKey(30));
});

test("begin cleans at most 64 keys older than yesterday and throttles cleanup to once a minute", async () => {
  const store = new FullStore();
  const now = 50 * DAY;
  const oldDay = 47;
  for (let i = 0; i < 70; i++) await store.put(physicalKey(oldDay, i.toString(16).padStart(64, "0")), { state: "sent" });
  await store.put(physicalKey(49, otherKey), { state: "sent", expires: now + DAY });
  await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, now);
  assert.equal(store.deleted.length, 64);
  assert.equal(store.listCalls[0].limit, 64);
  assert.equal(store.listCalls[0].end, "linear-api:0000000049:");
  assert.equal([...store.values.keys()].filter((name) => name.startsWith("linear-api:")).length, 8);

  await state.linearApiBegin(store, { key: otherKey, request_sha: requestSha, operation: "projectUpdate" }, now + 30_000);
  assert.equal(store.listCalls.length, 1);
});

test("rejects invalid keys and cached data larger than 8192 UTF-8 bytes", async () => {
  const store = new FullStore();
  await assert.rejects(state.linearApiBegin(store, { key: "short", request_sha: requestSha, operation: "projectUpdate" }, 1), /64-character SHA-256/);
  const created = await state.linearApiBegin(store, { key, request_sha: requestSha, operation: "projectUpdate" }, 60 * DAY);
  await assert.rejects(state.linearApiSet(store, { storageKey: created.storageKey, changes: { state: "sent", data: "é".repeat(4100) } }, 60 * DAY), /exceeds 8192 bytes/);
  assert.equal((await store.get(created.storageKey)).state, "pending");
});
