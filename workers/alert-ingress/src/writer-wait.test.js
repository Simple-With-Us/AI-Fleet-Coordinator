// The writer serializes every request so that read/decide/write is atomic.  A holder that never
// settles must not stall later requests for ever, and a waiter that gives up must never run
// afterwards, so the bound cannot create overlapping writes.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import { AlertWriter } from "./index.js";

const legacy = { async get() { return null; }, async list() { return { keys: [], list_complete: true }; } };
const env = { ALERT_STATE: legacy, INGRESS_SHARED_TOKEN: "fixture" };

function writerWith(getImpl) {
  const data = new Map();
  const storage = {
    async get(k) { return getImpl ? getImpl(k, data) : data.get(k); },
    async put(k, v) { data.set(k, v); },
    async list() { return new Map(); },
  };
  const writer = new AlertWriter({ storage }, env);
  writer.waitMs = 40;
  return writer;
}

const call = (writer, path = "/nope") => writer.fetch(new Request("https://writer" + path, { method: "POST" }));

test("a request behind a hung holder gets a retryable 503 after the bound", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const writer = writerWith(() => gate);
  const holder = call(writer);
  const waiter = await call(writer);
  assert.equal(waiter.status, 503);
  assert.equal(waiter.headers.get("Retry-After"), "30");
  assert.deepEqual(await waiter.json(), { error: "writer busy" });
  release();
  await holder;
});

test("a refused waiter never runs, even after the holder finishes", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let dispatches = 0;
  const writer = writerWith(() => gate);
  const original = writer.dispatch.bind(writer);
  writer.dispatch = (request) => { dispatches += 1; return original(request); };
  const holder = call(writer);
  const refused = await call(writer);
  assert.equal(refused.status, 503);
  release();
  await holder.catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(dispatches, 1, "only the holder was dispatched");
});

test("dispatches never overlap, and the queue recovers once the holder settles", async () => {
  let active = 0, peak = 0, release;
  const gate = new Promise((resolve) => { release = resolve; });
  const writer = writerWith(async () => { active += 1; peak = Math.max(peak, active); await gate; active -= 1; });
  writer.waitMs = 5000;
  const first = call(writer), second = call(writer), third = call(writer);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(active, 1);
  release();
  await Promise.all([first, second, third]);
  assert.equal(peak, 1);
  const later = writerWith();
  assert.notEqual((await call(later)).status, 503);
});
