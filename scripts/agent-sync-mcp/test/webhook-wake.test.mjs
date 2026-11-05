// Jet's additive webhook wake (owner ask 2026-11-04:  "jet needs to wake also
// his own way"):  the `fleet.webhook` event (src/events.js), its signed route
// contract (src/webhook-wake.js) and its claim gate (SeatGate.webhookWakeClaim).
// DRAFT:  not deployed, not subscribed; these tests pin the unit behavior only.
// The load-bearing pins:  ONE canonical ledger and hourly envelope across the
// listener and webhook paths, a pure doorbell (no caller content), and the
// custody lattice (uncertain tombstones, only provable zero-dispatch re-arms).

import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import {
  EVENT_NAME,
  WEBHOOK_EVENT_NAME,
  KNOWN_EVENT_NAMES,
  eventDefinitions,
  webhookEventDefinition,
  validateWebhookWakeArguments,
  eventIdFor,
  INVALID_PARAMS,
} from "../src/events.js";
import {
  verifyWebhookWake,
  webhookWakeAdmission,
  webhookWakeData,
  deliverWebhookWake,
  webhookWakeReply,
  webhookWakePathSeat,
  webhookWakeKey,
  WEBHOOK_WAKE_CONTRACT,
  WEBHOOK_WAKE_MAX_BODY,
  WEBHOOK_WAKE_RATE_LIMIT,
  WEBHOOK_WAKE_RATE_WINDOW_MS,
} from "../src/webhook-wake.js";
import { wakeSignature } from "../src/wake.js";
import * as state from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { classifyPath } from "../src/policy.js";
import { testEnv } from "./helpers.mjs";

const KEY = "c".repeat(64);
const NOW_S = 1792000000;
const NOW_MS = NOW_S * 1000;
const CALLBACK = "https://chatgpt.com/backend-api/mcp-events/callback_webhook";
const SECRET = `whsec_${Buffer.alloc(32, 7).toString("base64")}`;

const wakeBody = (extra = {}) => JSON.stringify({ contract: WEBHOOK_WAKE_CONTRACT, seat: "JET", wake_id: randomUUID(), sent_at: NOW_S, ...extra });
const sign = async (body, key = KEY) => wakeSignature(key, new TextEncoder().encode(body));
const check = (body, signature, extra = {}) => verifyWebhookWake({ bytes: new TextEncoder().encode(body), signature, key: KEY, seat: "JET", now: NOW_MS, ...extra });

/** A gate over one memory store at a fixed clock, backed by the real state module. */
function memoryGate(store, { subLive } = {}) {
  const now = NOW_MS;
  return {
    store,
    eventClaimWake: (opts) => state.eventClaimWake(store, opts, now),
    webhookWakeClaim: (opts) => state.webhookWakeClaim(store, opts, now),
    eventSettleWake: (opts) => state.eventSettleWake(store, opts, now),
    eventMarkDispatched: (opts) => state.eventMarkDispatched(store, opts, now),
    setPaused: (opts) => state.setPaused(store, opts, now),
    eventSubLive: subLive ?? ((opts) => state.eventSubLive(store, opts, now)),
    eventDeliveryResult: (opts) => state.eventDeliveryResult(store, opts, now),
  };
}

/** Put one fleet.webhook subscription into the store, as subscribe would. */
async function upsertSub(store, sub = SUB(), max = 1) {
  const saved = await state.eventSubUpsert(store, { sub: { ...sub, arguments: {}, epoch: 0, approved_at: NOW_MS, client_id: "test" }, ttlMs: 60 * 60 * 1000, max }, NOW_MS);
  assert.equal(saved.ok, true, JSON.stringify(saved));
  return sub;
}

const SUB = (name = WEBHOOK_EVENT_NAME, url = CALLBACK) => ({ id: "sub_" + randomUUID().replaceAll("-", ""), name, url, host: "chatgpt.com", secret: SECRET });
const okResponse = (status) => ({ status, body: { cancel: async () => {} } });
const fetchWith = (status, posts = []) => async (url, init) => {
  posts.push({ url, body: JSON.parse(init.body), headers: init.headers });
  return okResponse(status);
};

test("fleet.webhook is a known event, offered only when enabled for JET's connection, a pure doorbell", () => {
  assert.ok(KNOWN_EVENT_NAMES.includes(WEBHOOK_EVENT_NAME));
  const off = loadConfig(testEnv());
  assert.equal(eventDefinitions({ ...off, eventSeat: "JET" }).map((d) => d.name).includes(WEBHOOK_EVENT_NAME), false);
  const on = loadConfig(testEnv({ WEBHOOK_WAKE_ENABLED: "on" }));
  assert.equal(eventDefinitions({ ...on, eventSeat: "JET" }).map((d) => d.name).includes(WEBHOOK_EVENT_NAME), true);
  // JET-CHAT's connection never offers it:  the wake-only seat is JET.
  assert.equal(eventDefinitions({ ...on, eventSeat: "JET-CHAT" }).map((d) => d.name).includes(WEBHOOK_EVENT_NAME), false);
  const def = webhookEventDefinition();
  assert.equal(def.name, WEBHOOK_EVENT_NAME);
  assert.deepEqual(def.delivery, ["webhook"]);
  assert.deepEqual(def.payloadSchema.required, ["id", "fired_at"]);
  assert.deepEqual(Object.keys(def.payloadSchema.properties).sort(), ["fired_at", "id"], "no caller content:  no note, ever");
  assert.equal(/note/i.test(JSON.stringify(def)), false);
});

test("fleet.webhook takes no arguments, and the refusal names none", () => {
  assert.deepEqual(validateWebhookWakeArguments(undefined), {});
  assert.deepEqual(validateWebhookWakeArguments(null), {});
  assert.deepEqual(validateWebhookWakeArguments({}), {});
  assert.throws(() => validateWebhookWakeArguments("x"), { code: INVALID_PARAMS });
  assert.throws(() => validateWebhookWakeArguments(["x"]), { code: INVALID_PARAMS });
  try {
    validateWebhookWakeArguments({ channel: "work-in-progress" });
    assert.unreachable();
  } catch (err) {
    assert.equal(err.code, INVALID_PARAMS);
    assert.equal(err.message.includes("channel"), false, "the refusal never echoes the argument's name");
  }
});

test("webhookWakePathSeat and webhookWakeKey", () => {
  assert.equal(webhookWakePathSeat("/internal/webhook-wake/JET"), "JET");
  assert.equal(webhookWakePathSeat("/internal/webhook-wake/JET-CHAT"), "JET-CHAT");
  assert.equal(webhookWakePathSeat("/internal/webhook-wake/jet"), null);
  assert.equal(webhookWakePathSeat("/internal/wake/JET"), null);
  assert.equal(webhookWakePathSeat("/internal/webhook-wake/"), null);
  assert.equal(webhookWakeKey("  " + KEY + "  "), KEY);
  assert.equal(webhookWakeKey("short"), "");
  assert.equal(webhookWakeKey(undefined), "");
});

test("classifyPath routes the webhook wake, and only that shape", () => {
  assert.equal(classifyPath("/internal/webhook-wake/JET"), "webhook_wake");
  assert.equal(classifyPath("/internal/webhook-wake/JET/extra"), null);
  assert.equal(classifyPath("/internal/webhook-wake"), null);
  assert.equal(classifyPath("/internal/wake/JET"), "wake");
  assert.equal(classifyPath("/internal/spike/JET"), "spike");
});

test("a correctly signed wake verifies; every tamper is refused", async () => {
  const body = wakeBody();
  const ok = await check(body, await sign(body));
  assert.equal(ok.ok, true);
  assert.equal(ok.wake.seat, "JET");

  assert.equal((await check(body, await sign(wakeBody()))).reason, "bad_signature");
  assert.equal((await check(body, "not-hex")).reason, "bad_signature");
  assert.equal((await check(body, await sign(body, "d".repeat(64)))).reason, "bad_signature");
  assert.equal((await check("", await sign(body))).reason, "empty_body");
  assert.equal((await check("{", await sign("{"))).reason, "bad_json");
  assert.equal((await check("[]", await sign("[]"))).reason, "bad_json");

  const extra = JSON.stringify({ ...JSON.parse(body), note: "caller content is refused" });
  assert.equal((await check(extra, await sign(extra))).reason, "bad_fields", "no note field:  a label is not isolation");
  const missing = JSON.stringify({ contract: WEBHOOK_WAKE_CONTRACT, seat: "JET", sent_at: NOW_S });
  assert.equal((await check(missing, await sign(missing))).reason, "bad_fields");

  const wrongContract = wakeBody({ contract: "agent-sync-wake/1" });
  assert.equal((await check(wrongContract, await sign(wrongContract))).reason, "bad_contract");
  const wrongSeat = wakeBody({ seat: "ECHO" });
  assert.equal((await check(wrongSeat, await sign(wrongSeat))).reason, "seat_mismatch");
  const badId = wakeBody({ wake_id: "has spaces" });
  assert.equal((await check(badId, await sign(badId))).reason, "bad_wake_id");
  const stale = wakeBody({ sent_at: NOW_S - 301 });
  assert.equal((await check(stale, await sign(stale))).reason, "stale");
  const future = wakeBody({ sent_at: NOW_S + 61 });
  assert.equal((await check(future, await sign(future))).reason, "future");

  const noKey = await verifyWebhookWake({ bytes: new TextEncoder().encode(body), signature: await sign(body), key: "short", seat: "JET", now: NOW_MS });
  assert.deepEqual([noKey.status, noKey.reason], [404, "not_configured"]);
  const big = await verifyWebhookWake({ bytes: new Uint8Array(WEBHOOK_WAKE_MAX_BODY + 1), signature: "0".repeat(64), key: KEY, seat: "JET", now: NOW_MS });
  assert.equal(big.reason, "too_large", "size is refused before the signature is compared");
});

test("the event data is the id and the time - nothing else", async () => {
  const body = wakeBody();
  const { wake } = await check(body, await sign(body));
  assert.deepEqual(Object.keys(webhookWakeData(wake)).sort(), ["fired_at", "id"]);
  assert.equal(webhookWakeData(wake).id, wake.wake_id);
  assert.match(webhookWakeData(wake).fired_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
});

test("delivery goes to fleet.webhook subscriptions only, with the listener's event-id derivation", async () => {
  const posts = [];
  const store = state.memoryStore();
  const gate = memoryGate(store);
  const mine = SUB(WEBHOOK_EVENT_NAME);
  const other = SUB(EVENT_NAME, "https://chatgpt.com/backend-api/mcp-events/callback_zulip");
  await upsertSub(store, mine);
  await upsertSub(store, other);
  const body = wakeBody();
  const { wake } = await check(body, await sign(body));
  const claim = await gate.webhookWakeClaim({ wakeId: wake.wake_id });
  assert.equal(claim.verdict, "go");
  assert.equal(claim.subs.length, 1, "only the fleet.webhook subscription is even claimed");
  const result = await deliverWebhookWake({ gate, wake, claim, fetchFn: fetchWith(200, posts), sleep: async () => {} });
  assert.deepEqual(result, { outcome: "delivered", subscribers: 1, delivered: 1 });
  assert.equal(posts.length, 1, "the zulip.mention subscription never receives it");
  assert.equal(posts[0].body.name, WEBHOOK_EVENT_NAME);
  assert.deepEqual(Object.keys(posts[0].body), ["eventId", "name", "timestamp", "data", "cursor"]);
  assert.equal(posts[0].body.eventId, await eventIdFor(wake.wake_id, mine.id), "one wake id names one event id, as the listener derives it");
  assert.equal(typeof posts[0].headers["webhook-signature"], "string");
});

test("canonical cross-path dedupe:  one wake id fires once across BOTH paths", async () => {
  const store = state.memoryStore();
  const gate = memoryGate(store);
  // Listener claims and settles w-1:  the webhook path sees the duplicate.
  const listener = await gate.eventClaimWake({ wakeId: "w-1", messageId: 1, name: EVENT_NAME });
  assert.equal(listener.verdict, "go");
  await gate.eventSettleWake({ wakeId: "w-1", token: listener.token, outcome: "delivered", delivered: 1 });
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-1" })).verdict, "duplicate");
  // Webhook claims w-2:  the listener sees it in flight, then as a duplicate.
  const hook = await gate.webhookWakeClaim({ wakeId: "w-2" });
  assert.equal(hook.verdict, "go");
  const during = await gate.eventClaimWake({ wakeId: "w-2", messageId: 2, name: EVENT_NAME });
  assert.equal(during.verdict, "in_flight");
  assert.ok(during.retryAfterS > 0);
  await gate.eventSettleWake({ wakeId: "w-2", token: hook.token, outcome: "delivered", delivered: 1 });
  assert.equal((await gate.eventClaimWake({ wakeId: "w-2", messageId: 2, name: EVENT_NAME })).verdict, "duplicate");
  // A listener wake in flight blocks the webhook path too.
  const flying = await gate.eventClaimWake({ wakeId: "w-3", messageId: 3, name: EVENT_NAME });
  assert.equal(flying.verdict, "go");
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-3" })).verdict, "in_flight");
});

test("one hourly envelope across both paths:  listener wakes spend the webhook budget", async () => {
  const store = state.memoryStore();
  const gate = memoryGate(store);
  for (let i = 0; i < WEBHOOK_WAKE_RATE_LIMIT; i++) {
    const claim = await gate.eventClaimWake({ wakeId: "lw-" + i, messageId: i, name: EVENT_NAME });
    assert.equal(claim.verdict, "go");
    await gate.eventSettleWake({ wakeId: "lw-" + i, token: claim.token, outcome: "delivered", delivered: 1 });
  }
  const limited = await gate.webhookWakeClaim({ wakeId: "w-over", max: WEBHOOK_WAKE_RATE_LIMIT, windowMs: WEBHOOK_WAKE_RATE_WINDOW_MS });
  assert.equal(limited.verdict, "rate_limited", "six listener wakes in the hour leave no webhook budget");
  assert.ok(limited.retryAfterS > 0);
  // A rate-limited wake was never claimed:  the same id may come back later.
  const later = NOW_MS + WEBHOOK_WAKE_RATE_WINDOW_MS + 1;
  assert.equal((await state.webhookWakeClaim(store, { wakeId: "w-over", max: WEBHOOK_WAKE_RATE_LIMIT, windowMs: WEBHOOK_WAKE_RATE_WINDOW_MS }, later)).verdict, "go");
  // Mixed paths share the envelope:  three and three is the same six.
  const store2 = state.memoryStore();
  const gate2 = memoryGate(store2);
  for (let i = 0; i < 3; i++) assert.equal((await gate2.webhookWakeClaim({ wakeId: "h-" + i })).verdict, "go");
  for (let i = 0; i < 3; i++) assert.equal((await gate2.eventClaimWake({ wakeId: "l-" + i, name: EVENT_NAME })).verdict, "go");
  assert.equal((await gate2.webhookWakeClaim({ wakeId: "h-over" })).verdict, "rate_limited");
});

test("a paused seat claims nothing and the id stays spent (canonical claim)", async () => {
  const gate = memoryGate(state.memoryStore());
  await gate.setPaused({ paused: true });
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-p" })).verdict, "paused");
  await gate.setPaused({ paused: false });
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-p" })).verdict, "duplicate");
});

test("custody:  delivered settles and spends;  uncertain TOMBSTONES and never re-arms", async () => {
  // 5xx after a real dispatch:  fate unknown.  The claim settles "uncertain"
  // and the same id stays spent - firing again could repeat a turn.
  const store = state.memoryStore();
  const gate = memoryGate(store);
  await upsertSub(store);
  const claim = await gate.webhookWakeClaim({ wakeId: "w-uncertain" });
  assert.equal(claim.subs.length, 1);
  const body = wakeBody({ wake_id: "w-uncertain" });
  const { wake } = await check(body, await sign(body));
  const result = await deliverWebhookWake({ gate, wake, claim, fetchFn: fetchWith(500), sleep: async () => {} });
  assert.equal(result.outcome, "uncertain");
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-uncertain" })).verdict, "duplicate", "tombstoned:  the same id never fires again on its own");

  // Delivered settles and spends the id.
  const store2 = state.memoryStore();
  const gate2 = memoryGate(store2);
  await upsertSub(store2);
  const claim2 = await gate2.webhookWakeClaim({ wakeId: "w-ok" });
  const body2 = wakeBody({ wake_id: "w-ok" });
  const { wake: wake2 } = await check(body2, await sign(body2));
  const ok = await deliverWebhookWake({ gate: gate2, wake: wake2, claim: claim2, fetchFn: fetchWith(200), sleep: async () => {} });
  assert.equal(ok.outcome, "delivered");
  assert.equal((await gate2.webhookWakeClaim({ wakeId: "w-ok" })).verdict, "duplicate");

  // Authoritative answers settle:  410 is gone, 400 rejected;  both spent.
  for (const [status, outcome] of [[410, "gone"], [400, "rejected"]]) {
    const s = state.memoryStore();
    const g = memoryGate(s);
    await upsertSub(s);
    const id = "w-" + outcome;
    const c = await g.webhookWakeClaim({ wakeId: id });
    const b = wakeBody({ wake_id: id });
    const { wake: w } = await check(b, await sign(b));
    const r = await deliverWebhookWake({ gate: g, wake: w, claim: c, fetchFn: fetchWith(status), sleep: async () => {} });
    assert.equal(r.outcome, outcome);
    assert.equal((await g.webhookWakeClaim({ wakeId: id })).verdict, "duplicate");
  }
});

test("custody:  provable zero-dispatch releases the claim for the SAME wake", async () => {
  // The liveness re-check throws before anything is sent:  no remote effect
  // exists, so the claim is released and the same id may come back.
  const store = state.memoryStore();
  const gate = memoryGate(store, { subLive: async () => { throw new Error("store down"); } });
  await upsertSub(store);
  const claim = await gate.webhookWakeClaim({ wakeId: "w-nosend" });
  assert.equal(claim.subs.length, 1);
  const body = wakeBody({ wake_id: "w-nosend" });
  const { wake } = await check(body, await sign(body));
  const result = await deliverWebhookWake({ gate, wake, claim, fetchFn: fetchWith(200), sleep: async () => {} });
  assert.equal(result.outcome, "retry");
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-nosend" })).verdict, "go", "nothing was dispatched:  the same wake may be sent again");

  // A mixed zero-dispatch (one sub not live, one liveness failure) is still
  // zero remote effect:  released, not tombstoned.
  const store2 = state.memoryStore();
  let calls = 0;
  const gate2 = memoryGate(store2, {
    subLive: async () => {
      calls += 1;
      if (calls === 1) return { live: false, reason: "revoked" };
      throw new Error("store down");
    },
  });
  const claim2 = await gate2.webhookWakeClaim({ wakeId: "w-mixed" });
  claim2.subs = [SUB(WEBHOOK_EVENT_NAME), SUB(WEBHOOK_EVENT_NAME)];
  const result2 = await deliverWebhookWake({ gate: gate2, wake: { ...wake, wake_id: "w-mixed" }, claim: claim2, fetchFn: fetchWith(200), sleep: async () => {} });
  assert.equal(result2.outcome, "retry");
});

test("no fleet.webhook subscription:  no_subscriber, settled, id spent", async () => {
  const gate = memoryGate(state.memoryStore());
  const claim = await gate.webhookWakeClaim({ wakeId: "w-none" });
  assert.equal(claim.verdict, "go");
  assert.deepEqual(claim.subs, []);
  const body = wakeBody({ wake_id: "w-none" });
  const { wake } = await check(body, await sign(body));
  const result = await deliverWebhookWake({ gate, wake, claim, fetchFn: fetchWith(200), sleep: async () => {} });
  assert.equal(result.outcome, "no_subscriber");
  assert.equal((await gate.webhookWakeClaim({ wakeId: "w-none" })).verdict, "duplicate");
});

test("route admission:  flag, JET-only, bindings and the key domain", () => {
  const WAKE_KEY2 = "b".repeat(64);
  const base = { enabled: true, seat: "JET", hostedSeats: ["JET", "GROK-WEB"], bound: true, wakeKey: WAKE_KEY2, key: KEY, wakeKeys: [WAKE_KEY2] };
  assert.equal(webhookWakeAdmission(base), null, "admitted:  flag on, JET, both keys, distinct domains");
  assert.equal(webhookWakeAdmission({ ...base, enabled: false }), "not_admitted", "flag off is a 404");
  assert.equal(webhookWakeAdmission({ ...base, seat: "JET-CHAT", bound: false }), "not_admitted", "no webhook binding for JET-CHAT:  JET-only matches the offer");
  assert.equal(webhookWakeAdmission({ ...base, seat: null }), "not_admitted");
  assert.equal(webhookWakeAdmission({ ...base, hostedSeats: ["GROK-WEB"] }), "not_admitted", "an unhosted seat is a 404");
  assert.equal(webhookWakeAdmission({ ...base, wakeKey: null }), "not_admitted", "no wake key:  the seat is offered no events at all");
  assert.equal(webhookWakeAdmission({ ...base, key: "" }), "not_admitted", "no webhook key");
  assert.equal(webhookWakeAdmission({ ...base, key: WAKE_KEY2 }), "key_reused", "a webhook key equal to ANY wake key refuses to fire");
});

test("webhookWakeReply:  counts only, custody-honest, no secrets or URLs", () => {
  assert.deepEqual(webhookWakeReply({ verdict: "duplicate" }), { status: 200, body: { ok: true, duplicate: true } });
  const flying = webhookWakeReply({ verdict: "in_flight", retryAfterS: 9 });
  assert.deepEqual([flying.status, flying.retryAfterS], [503, 9]);
  const limited = webhookWakeReply({ verdict: "rate_limited", retryAfterS: 42 });
  assert.deepEqual([limited.status, limited.retryAfterS], [429, 42]);
  assert.deepEqual(webhookWakeReply({ verdict: "paused" }), { status: 202, body: { ok: true, paused: true, subscribers: 0 } });
  const retry = webhookWakeReply({ verdict: "go", outcome: "retry", subscribers: 1, delivered: 0 });
  assert.deepEqual([retry.status, retry.body.retry], [503, true], "zero-dispatch:  send the SAME wake again");
  const uncertain = webhookWakeReply({ verdict: "go", outcome: "uncertain", subscribers: 1, delivered: 0 });
  assert.equal(uncertain.status, 202);
  assert.equal(uncertain.body.outcome, "uncertain", "tombstone is named, never hidden");
  assert.deepEqual(webhookWakeReply({ verdict: "go", outcome: "delivered", subscribers: 1, delivered: 1 }), { status: 202, body: { ok: true, subscribers: 1, delivered: 1, outcome: "delivered" } });
});

// ---------------------------------------------------------------- reviewer blockers (2026-11-05)

test("the shared envelope binds the LISTENER path too: eventClaimWake with budget", async () => {
  const store = state.memoryStore();
  // Six webhook wakes claim the hour...
  for (let i = 0; i < WEBHOOK_WAKE_RATE_LIMIT; i++) {
    const c = await state.webhookWakeClaim(store, { wakeId: `wh-${i}` }, NOW_MS);
    assert.equal(c.verdict, "go");
  }
  // ...and the seventh LISTENER wake is rate_limited at the canonical entry.
  const limited = await state.eventClaimWake(store, { wakeId: "listener-7", messageId: null, name: EVENT_NAME, budget: { max: WEBHOOK_WAKE_RATE_LIMIT, windowMs: WEBHOOK_WAKE_RATE_WINDOW_MS } }, NOW_MS);
  assert.equal(limited.verdict, "rate_limited");
  assert.ok(limited.retryAfterS >= 1);
  // Without the budget argument the listener claim path is unchanged.
  const store2 = state.memoryStore();
  const plain = await state.eventClaimWake(store2, { wakeId: "listener-plain", messageId: null, name: EVENT_NAME }, NOW_MS);
  assert.equal(plain.verdict, "go");
});

test("dedupe before budget: a settled duplicate at the cap is a duplicate, never 429", async () => {
  const store = state.memoryStore();
  for (let i = 0; i < WEBHOOK_WAKE_RATE_LIMIT; i++) {
    const c = await state.webhookWakeClaim(store, { wakeId: `wh-${i}` }, NOW_MS);
    assert.equal(c.verdict, "go");
    await state.eventSettleWake(store, { wakeId: `wh-${i}`, token: c.token, outcome: "delivered", delivered: 1 }, NOW_MS);
  }
  // The window is full, but this id is already settled:  duplicate wins.
  const dup = await state.webhookWakeClaim(store, { wakeId: "wh-0" }, NOW_MS + 1000);
  assert.equal(dup.verdict, "duplicate");
  // An in-flight id is in_flight, not a budget charge either.
  const store2 = state.memoryStore();
  const live = await state.webhookWakeClaim(store2, { wakeId: "wh-live" }, NOW_MS);
  assert.equal(live.verdict, "go");
  for (let i = 0; i < WEBHOOK_WAKE_RATE_LIMIT - 1; i++) {
    assert.equal((await state.webhookWakeClaim(store2, { wakeId: `wh-x${i}` }, NOW_MS)).verdict, "go");
  }
  const inflight = await state.webhookWakeClaim(store2, { wakeId: "wh-live" }, NOW_MS + 1000);
  assert.equal(inflight.verdict, "in_flight");
  // A genuinely new wake IS rate_limited.
  const limited = await state.webhookWakeClaim(store2, { wakeId: "wh-new" }, NOW_MS + 1000);
  assert.equal(limited.verdict, "rate_limited");
});

test("UNKNOWN fence: a claim marked dispatched is never re-armed after a crash", async () => {
  const store = state.memoryStore();
  const gate = memoryGate(store);
  await upsertSub(store);
  const wake = { wake_id: "w-crash", sent_at: NOW_S };
  const claim = await gate.webhookWakeClaim({ wakeId: wake.wake_id });
  assert.equal(claim.verdict, "go");
  // The fence lands, then the Worker "crashes":  no settle ever happens.
  assert.equal((await gate.eventMarkDispatched({ wakeId: wake.wake_id, token: claim.token })).ok, true);
  // The lease expires; a retry of the SAME wake must NOT claim again.
  const later = NOW_MS + 60 * 1000;
  const reclaim = await state.webhookWakeClaim(store, { wakeId: wake.wake_id }, later);
  assert.equal(reclaim.verdict, "duplicate", "a dispatched-but-unsettled wake stays spent");
  // The listener path agrees:  same ledger, same fence.
  const reclaim2 = await state.eventClaimWake(store, { wakeId: wake.wake_id, messageId: null, name: EVENT_NAME, budget: { max: 6, windowMs: 60 * 60 * 1000 } }, later);
  assert.equal(reclaim2.verdict, "duplicate");
  // Without the mark (crash BEFORE dispatch) the claim still re-arms:  no
  // remote effect exists, so retrying the same id is safe.
  const store2 = state.memoryStore();
  const c2 = await state.webhookWakeClaim(store2, { wakeId: "w-pre" }, NOW_MS);
  assert.equal(c2.verdict, "go");
  const reclaim3 = await state.webhookWakeClaim(store2, { wakeId: "w-pre" }, later);
  assert.equal(reclaim3.verdict, "go", "crash before dispatch re-arms");
  // A wrong token cannot mark.
  const store3 = state.memoryStore();
  const c3 = await state.webhookWakeClaim(store3, { wakeId: "w-tok" }, NOW_MS);
  assert.equal(c3.verdict, "go");
  assert.equal((await state.eventMarkDispatched(store3, { wakeId: "w-tok", token: "wrong" }, NOW_MS)).ok, false);
  assert.equal((await state.eventMarkDispatched(store3, { wakeId: "w-tok", token: c3.token }, NOW_MS)).ok, true, "the right token marks");
});

test("delivery makes exactly ONE attempt: an uncertain 5xx is never retried inside delivery", async () => {
  const store = state.memoryStore();
  const gate = memoryGate(store);
  await upsertSub(store);
  const wake = { wake_id: "w-once", sent_at: NOW_S };
  const claim = await gate.webhookWakeClaim({ wakeId: wake.wake_id });
  const posts = [];
  const result = await deliverWebhookWake({ gate, wake, claim, fetchFn: fetchWith(500, posts), sleep: async () => {} });
  assert.equal(posts.length, 1, "backoff []: no internal uncertain retry");
  assert.equal(result.outcome, "uncertain");
  // Tombstoned:  the same id stays spent.
  assert.equal((await gate.webhookWakeClaim({ wakeId: wake.wake_id })).verdict, "duplicate");
});

test("a failed dispatch fence dispatches nothing and releases the claim", async () => {
  const store = state.memoryStore();
  const gate = memoryGate(store);
  await upsertSub(store);
  gate.eventMarkDispatched = async () => ({ ok: false }); // store lost the claim
  const wake = { wake_id: "w-markfail", sent_at: NOW_S };
  const claim = await gate.webhookWakeClaim({ wakeId: wake.wake_id });
  const posts = [];
  const result = await deliverWebhookWake({ gate, wake, claim, fetchFn: fetchWith(200, posts), sleep: async () => {} });
  assert.equal(posts.length, 0, "no fence, no dispatch");
  assert.equal(result.outcome, "retry");
  // The claim was released:  the SAME wake may be sent again.
  assert.equal((await gate.webhookWakeClaim({ wakeId: wake.wake_id })).verdict, "go");
});

test("listener wakeReply maps rate_limited to 429", async () => {
  const { wakeReply } = await import("../src/events.js");
  const reply = wakeReply({ verdict: "rate_limited", retryAfterS: 42 });
  assert.equal(reply.status, 429);
  assert.equal(reply.body.rate_limited, true);
  assert.equal(reply.retryAfterS, 42);
});

