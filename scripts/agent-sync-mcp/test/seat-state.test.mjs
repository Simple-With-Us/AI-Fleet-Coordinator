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
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0, phase: 2 }, T0 + 1000), { code: "ok" });
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0, phase: 2 }, T0 + GRANT_MAX_AGE_MS), { code: "grant_too_old" });
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0 + 10 * 60_000, phase: 2 }, T0), { code: "grant_too_old" });
  await S.bumpEpoch(store, { by: "jay", reason: "revoke" }, T0 + 2000);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0, phase: 2 }, T0 + 3000), { code: "stale_epoch" });
  assert.deepEqual(await S.check(store, { epoch: "1", approvedAt: T0, phase: 2 }, T0), { code: "bad_props" });
  assert.deepEqual(await S.check(store, {}, T0), { code: "bad_props" });
  // A Phase 0 stub grant (no phase in its props) never reaches the real tools.
  await S.arm(store, { by: "jay" }, T0 + 4000);
  const fresh = await S.approve(store, { by: "jay" }, T0 + 4000);
  assert.deepEqual(await S.check(store, { epoch: fresh.epoch, approvedAt: T0 + 4000 }, T0 + 5000), { code: "stale_phase" });
  assert.deepEqual(await S.check(store, { epoch: fresh.epoch, approvedAt: T0 + 4000, phase: 1 }, T0 + 5000), { code: "stale_phase" });
  assert.deepEqual(await S.check(store, { epoch: fresh.epoch, approvedAt: T0 + 4000, phase: 2 }, T0 + 5000), { code: "ok" });
});

test("pause reports paused for a live grant, stale for a dead one", async () => {
  const store = S.memoryStore();
  await S.arm(store, { by: "jay" }, T0);
  const { epoch } = await S.approve(store, { by: "jay" }, T0);
  await S.setPaused(store, { by: "jay", paused: true }, T0);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0, phase: 2 }, T0 + 1), { code: "paused" });
  assert.deepEqual(await S.check(store, { epoch: epoch - 1, approvedAt: T0, phase: 2 }, T0 + 1), { code: "stale_epoch" });
  await S.setPaused(store, { by: "jay", paused: false }, T0 + 2);
  assert.deepEqual(await S.check(store, { epoch, approvedAt: T0, phase: 2 }, T0 + 3), { code: "ok" });
});

test("audit and refusal logs are capped and newest first", async () => {
  const store = S.memoryStore();
  for (let i = 0; i < 250; i++) await S.logRefusal(store, { where: "authorize", reason: `r${i}` }, T0 + i);
  const rows = await S.refusals(store, 500);
  assert.equal(rows.length, 100);
  assert.equal(rows[0].reason, "r249");
  await S.arm(store, { by: "jay" }, T0);
  const tail = await S.tail(store, 5);
  assert.equal(tail[0].event, "arm");
});

// ---------------------------------------------------------------- wake claims (MCP Events)

/** A subscription stored straight through seat-state, live at epoch 0. */
async function withSub(store, id = "sub_a", now = T0) {
  await S.eventSubUpsert(store, { sub: { id, name: "zulip.mention", arguments: {}, url: "https://chatgpt.com/cb", host: "chatgpt.com", secret: "whsec_x", epoch: 0, approved_at: now }, ttlMs: 24 * 3600 * 1000 }, now);
}

test("a wake is remembered once it is settled, not when it arrives", async () => {
  const store = S.memoryStore();
  await withSub(store);
  const claim = await S.eventClaimWake(store, { wakeId: "w-1", messageId: 77 }, T0);
  assert.equal(claim.verdict, "go");
  assert.equal(claim.subs.length, 1);
  assert.match(claim.token, /^[0-9a-f-]{36}$/);
  // Claimed but not settled:  another request is delivering it.
  assert.deepEqual(await S.eventClaimWake(store, { wakeId: "w-1" }, T0 + 1000), { verdict: "in_flight", retryAfterS: Math.ceil((S.WAKE_LEASE_MS - 1000) / 1000) });
  assert.deepEqual(await S.eventSettleWake(store, { wakeId: "w-1", token: claim.token, outcome: "delivered", delivered: 1 }, T0 + 2000), { ok: true });
  assert.deepEqual(await S.eventClaimWake(store, { wakeId: "w-1" }, T0 + 3000), { verdict: "duplicate" });
  const tail = await S.tail(store, 5);
  assert.deepEqual(tail.map((r) => r.event).slice(0, 2), ["event_wake_settled", "event_wake_captured"]);
  assert.deepEqual([tail[0].outcome, tail[0].delivered, tail[1].message_id, tail[1].subscribers], ["delivered", 1, 77, 1]);
});

test("a released wake (every delivery failed retryably) is claimed again by the listener's retry", async () => {
  const store = S.memoryStore();
  const first = await S.eventClaimWake(store, { wakeId: "w-2" }, T0);
  assert.deepEqual(await S.eventSettleWake(store, { wakeId: "w-2", token: first.token, outcome: "retry" }, T0 + 6000), { ok: true });
  assert.equal((await S.tail(store, 1))[0].event, "event_wake_released");
  const second = await S.eventClaimWake(store, { wakeId: "w-2" }, T0 + 8000);
  assert.equal(second.verdict, "go");
  assert.notEqual(second.token, first.token);
  await S.eventSettleWake(store, { wakeId: "w-2", token: second.token, outcome: "delivered", delivered: 1 }, T0 + 9000);
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-2" }, T0 + 20_000)).verdict, "duplicate");
});

test("a claim nobody settles (the isolate died) is in flight for its lease, then claimed again", async () => {
  const store = S.memoryStore();
  const lost = await S.eventClaimWake(store, { wakeId: "w-3" }, T0);
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-3" }, T0 + S.WAKE_LEASE_MS - 1)).verdict, "in_flight");
  const again = await S.eventClaimWake(store, { wakeId: "w-3" }, T0 + S.WAKE_LEASE_MS);
  assert.equal(again.verdict, "go");
  assert.equal((await S.tail(store, 1))[0].reclaimed, true);
  // The dead request's late settle or release changes nothing:  the new claim is not its own.
  assert.deepEqual(await S.eventSettleWake(store, { wakeId: "w-3", token: lost.token, outcome: "retry" }, T0 + S.WAKE_LEASE_MS + 1), { ok: false });
  assert.equal((await S.tail(store, 1))[0].event, "event_wake_settle_stale");
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-3" }, T0 + S.WAKE_LEASE_MS + 2)).verdict, "in_flight", "the new claim still holds");
  await assert.rejects(S.eventSettleWake(store, { wakeId: "w-3", token: again.token, outcome: "maybe" }, T0), /unknown wake outcome/);
});

test("the lease outlives the inline delivery budget and ends before the listener's last retry", async () => {
  const { WAKE_BUDGET_MS } = await import("../src/events.js");
  const listenerRetrySpanMs = (2 + 5 + 10) * 1000; // adapters.py ROUTINE_BACKOFF, after the first failure
  const listenerTimeoutMs = 15_000; // JET's routine timeout_seconds
  assert.ok(S.WAKE_LEASE_MS >= WAKE_BUDGET_MS + 3000, "a live delivery is never claimed twice");
  assert.ok(S.WAKE_LEASE_MS < listenerRetrySpanMs, "a dead delivery is claimed again by the last retry");
  assert.ok(WAKE_BUDGET_MS <= listenerTimeoutMs - 3000, "the answer beats the listener's timeout");
});

test("a wake_seen number written by the deployed Worker counts as settled", async () => {
  const store = S.memoryStore();
  await store.put("wake_seen", { "w-old": T0 - 60_000 });
  assert.deepEqual(await S.eventClaimWake(store, { wakeId: "w-old" }, T0), { verdict: "duplicate" });
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-new" }, T0)).verdict, "go");
});

test("a paused seat settles the wake at once:  nothing is delivered, and a retry is a duplicate", async () => {
  const store = S.memoryStore();
  await withSub(store);
  await S.setPaused(store, { by: "jay", paused: true }, T0);
  assert.deepEqual(await S.eventClaimWake(store, { wakeId: "w-p" }, T0), { verdict: "paused" });
  assert.equal((await S.tail(store, 1))[0].event, "event_wake_paused");
  await S.setPaused(store, { by: "jay", paused: false }, T0 + 1000);
  assert.deepEqual(await S.eventClaimWake(store, { wakeId: "w-p" }, T0 + 2000), { verdict: "duplicate" });
});

test("a channel refusal settles the wake and audits the stream id only", async () => {
  const store = S.memoryStore();
  const claim = await S.eventClaimWake(store, { wakeId: "w-c" }, T0);
  await S.eventSettleWake(store, { wakeId: "w-c", token: claim.token, outcome: "channel_refused", streamId: 999001 }, T0 + 10);
  const row = (await S.tail(store, 1))[0];
  assert.deepEqual(Object.keys(row).sort(), ["event", "stream_id", "ts", "wake_ref"]);
  assert.equal(row.stream_id, 999001);
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-c" }, T0 + 20)).verdict, "duplicate");
});

test("wake claims still expire after WAKE_SEEN_MS and stay capped at 500", async () => {
  const store = S.memoryStore();
  const claim = await S.eventClaimWake(store, { wakeId: "w-ttl" }, T0);
  await S.eventSettleWake(store, { wakeId: "w-ttl", token: claim.token, outcome: "delivered" }, T0);
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-ttl" }, T0 + S.WAKE_SEEN_MS - 1)).verdict, "duplicate");
  assert.equal((await S.eventClaimWake(store, { wakeId: "w-ttl" }, T0 + S.WAKE_SEEN_MS + 1)).verdict, "go");
  for (let i = 0; i < 520; i++) await S.eventClaimWake(store, { wakeId: `w-cap-${i}` }, T0 + S.WAKE_SEEN_MS + 2);
  const seen = await store.get("wake_seen");
  assert.equal(Object.keys(seen).length, 500);
  assert.ok(!Object.hasOwn(seen, "w-cap-0"), "the oldest claims go first");
  assert.ok(Object.hasOwn(seen, "w-cap-519"));
});

test("eventSubLive says why a subscription may no longer receive an event", async () => {
  const store = S.memoryStore();
  await withSub(store, "sub_live");
  assert.deepEqual(await S.eventSubLive(store, { subId: "sub_live" }, T0), { live: true });
  await S.setPaused(store, { by: "jay", paused: true }, T0);
  assert.deepEqual(await S.eventSubLive(store, { subId: "sub_live" }, T0), { live: false, reason: "paused" });
  await S.setPaused(store, { by: "jay", paused: false }, T0);
  await S.bumpEpoch(store, { by: "jay", reason: "revoke" }, T0);
  assert.deepEqual(await S.eventSubLive(store, { subId: "sub_live" }, T0), { live: false, reason: "stale_epoch" });
  assert.deepEqual(await S.eventSubLive(store, { subId: "sub_live" }, T0), { live: false, reason: "removed" }, "dropped for good");
  assert.deepEqual(await S.eventSubLive(store, { subId: "sub_never" }, T0), { live: false, reason: "removed" });
});

test("token refusals are counted apart and cannot push an authorize row out of the log", async () => {
  const store = S.memoryStore();
  await S.logRefusal(store, { where: "authorize", reason: "redirect_not_allowlisted", client_id: "grok", redirect_uri: "https://grok.com/cb", by: "mail@jays.services" }, T0);
  // A distributed sender floods /oauth/token with 500 refusals across 300 distinct ids.
  for (let i = 0; i < 500; i++) await S.logRefusal(store, { where: "token", reason: "cimd_client_not_allowlisted", client_id: `https://evil.example/${i % 300}.json` }, T0 + 1 + i);
  const authorize = await S.refusals(store, 50);
  assert.equal(authorize.length, 1);
  assert.equal(authorize[0].by, "mail@jays.services");
  const token = await S.tokenRefusals(store, 100);
  assert.ok(token.length <= 40, `token table is capped (${token.length})`);
  assert.ok(token.every((r) => r.where === "token"));
  // Repeats collapse into one row with a count and the latest time.
  const store2 = S.memoryStore();
  for (let i = 0; i < 7; i++) await S.logRefusal(store2, { where: "token", reason: "authorization_not_basic", client_id: "" }, T0 + i);
  await S.logRefusal(store2, { where: "token", reason: "client_id_shape", client_id: "a b" }, T0 + 10);
  const rows = await S.tokenRefusals(store2);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => [r.reason, r.count]), [["client_id_shape", 1], ["authorization_not_basic", 7]]);
  assert.equal((await S.refusals(store2)).length, 0, "token rows never reach the authorize log");
});
