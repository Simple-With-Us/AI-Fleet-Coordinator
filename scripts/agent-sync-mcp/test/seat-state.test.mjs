import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import { ARM_WINDOW_MS, GRANT_MAX_AGE_MS } from "../src/config.js";

const T0 = Date.parse("2026-10-09T06:00:00Z");

test("unarmed seat refuses approval", async () => {
  const store = S.memoryStore();
  assert.deepEqual(await S.approve(store, { by: "jay" }, T0), { ok: false, reason: "not_armed" });
  assert.equal((await S.getState(store, T0)).epoch, 0);
});

test("arm, approve once, and the window is single-use", async () => {
  const store = S.memoryStore();
  const armed = await S.arm(store, { by: "jay" }, T0);
  assert.equal(armed.armed, true);
  assert.equal(armed.armedUntil, T0 + ARM_WINDOW_MS);
  const first = await S.approve(store, { by: "jay" }, T0 + 1000);
  assert.deepEqual(first, { ok: true, epoch: 1 });
  // Two parallel approvals:  the second finds the window consumed.
  assert.deepEqual(await S.approve(store, { by: "jay" }, T0 + 1001), { ok: false, reason: "not_armed" });
  assert.equal((await S.getState(store, T0 + 1002)).armed, false);
});

test("an expired window refuses and closes", async () => {
  const store = S.memoryStore();
  await S.arm(store, { by: "jay" }, T0);
  assert.equal((await S.getState(store, T0 + ARM_WINDOW_MS + 1)).armed, false);
  assert.deepEqual(await S.approve(store, { by: "jay" }, T0 + ARM_WINDOW_MS + 1), { ok: false, reason: "window_expired" });
});

test("arming cannot exceed 10 minutes", async () => {
  const store = S.memoryStore();
  const st = await S.arm(store, { by: "jay", ttlMs: 60 * 60 * 1000 }, T0);
  assert.equal(st.armedUntil, T0 + ARM_WINDOW_MS);
});

test("disarm closes the window", async () => {
  const store = S.memoryStore();
  await S.arm(store, { by: "jay" }, T0);
  await S.disarm(store, { by: "jay", reason: "denied" }, T0 + 5);
  assert.deepEqual(await S.approve(store, { by: "jay" }, T0 + 6), { ok: false, reason: "not_armed" });
});

test("epoch check:  current grant ok, bumped epoch stale, old grant too old", async () => {
  const store = S.memoryStore();
  await S.arm(store, { by: "jay" }, T0);
  const { epoch } = await S.approve(store, { by: "jay" }, T0);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 }, T0 + 1000), { code: "ok" });
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 }, T0 + GRANT_MAX_AGE_MS), { code: "grant_too_old" });
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 + 10 * 60_000 }, T0), { code: "grant_too_old" });
  await S.bumpEpoch(store, { by: "jay", reason: "revoke" }, T0 + 2000);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 }, T0 + 3000), { code: "stale_epoch" });
  assert.deepEqual(await S.check(store, { epoch: "1", approvedAt: T0 }, T0), { code: "bad_props" });
  assert.deepEqual(await S.check(store, {}, T0), { code: "bad_props" });
});

test("pause reports paused for a live grant, stale for a dead one", async () => {
  const store = S.memoryStore();
  await S.arm(store, { by: "jay" }, T0);
  const { epoch } = await S.approve(store, { by: "jay" }, T0);
  await S.setPaused(store, { by: "jay", paused: true }, T0);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 }, T0 + 1), { code: "paused" });
  assert.deepEqual(await S.check(store, { epoch: epoch - 1, approvedAt: T0 }, T0 + 1), { code: "stale_epoch" });
  await S.setPaused(store, { by: "jay", paused: false }, T0 + 2);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 }, T0 + 3), { code: "ok" });
});

test("audit and refusal logs are capped and newest first", async () => {
  const store = S.memoryStore();
  for (let i = 0; i < 250; i++) await S.logRefusal(store, { where: "authorize", reason: `r${i}` }, T0 + i);
  const rows = await S.refusals(store, 500);
  assert.equal(rows.length, 200);
  assert.equal(rows[0].reason, "r249");
  await S.arm(store, { by: "jay" }, T0);
  const tail = await S.tail(store, 5);
  assert.equal(tail[0].event, "arm");
});
