// AFC-477 spike (temporary):  the flag-gated second event `fleet.spike.ping`
// (src/events.js) and its signed route (src/spike.js, SeatGate.spikeClaim).
//
// The regression half pins zulip.mention:  its definition, events/list,
// subscribe, the one-destination refusal, delivery and dedupe give the same
// bytes as origin/main at 2af99bec (values computed there and pinned below),
// with SPIKE_EXTRA_EVENTS off and with it on and a spike subscription live.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, createHmac, randomUUID } from "node:crypto";
import {
  EVENT_NAME,
  SPIKE_EVENT_NAME,
  KNOWN_EVENT_NAMES,
  eventDefinition,
  spikeEventDefinition,
  fanOut,
  deliverWake,
  wakeReply,
  subscriptionId,
  SeatEvents,
  INVALID_PARAMS,
  DEFAULT_TTL_MS,
} from "../src/events.js";
import {
  verifySpike,
  deliverSpike,
  spikeReply,
  spikeData,
  spikePathSeat,
  spikeKey,
  SPIKE_CONTRACT,
  SPIKE_RATE_LIMIT,
  SPIKE_RATE_WINDOW_MS,
  SPIKE_MAX_BODY,
} from "../src/spike.js";
import * as state from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { classifyPath } from "../src/policy.js";
import { ChannelSet } from "../src/channels.js";
import { testEnv } from "./helpers.mjs";

// Synthetic test values, not credentials:  a whsec_ secret of 32 bytes of 7s, and HMAC keys of
// repeated letters.  The golden run below must use exactly these to match origin/main.
const SECRET = `whsec_${Buffer.alloc(32, 7).toString("base64")}`;
const CALLBACK = "https://chatgpt.com/backend-api/mcp-events/callback_123";
const SPIKE_CALLBACK = "https://chatgpt.com/backend-api/mcp-events/callback_spike";
const configOff = loadConfig(testEnv());
const configOn = loadConfig(testEnv({ SPIKE_EXTRA_EVENTS: "on" }));
const allowed = new ChannelSet([{ stream_id: 642232, name: "agent-sync", invite_only: false }]);
const channels = async () => allowed;
const SPIKE_KEY = "a".repeat(64);
const WAKE_KEY = "b".repeat(64);
const sha256 = (text) => createHash("sha256").update(text).digest("hex");

// Pinned from origin/main (2af99bec), before the spike existed.
const GOLDEN = Object.freeze({
  definitionSha: "279b937640baaba8f7d59f310d477f49f07a0b93f7684a02a145f069f00dfff7",
  listSha: "b947bab7776352f318ab67a9d0beb53ed600a9baf78c7ff2a8e4c83636a8352d",
  sub: '{"id":"sub_0127e860dc74c489dc41e43cda5a53e5","refreshBefore":"2026-10-15T17:46:40Z","cursor":null,"truncated":false}',
  second: "-32600 Agent-Sync already sends zulip.mention for this seat to another chat.  Stop monitoring there first, or wait until it lapses at 2026-10-15T17:46:40Z unless refreshed.",
  reply: '{"status":202,"body":{"ok":true,"subscribers":1,"delivered":1,"outcome":"delivered"}}',
  body: '{"eventId":"evt_6f0bc10ec9c7eb441629f4808f96c7bc","name":"zulip.mention","timestamp":"2026-10-14T17:46:40Z","data":{"message_id":77,"dm":false,"channel":"agent-sync","topic":"t","sender_full_name":"Jay","sender_user_id":1211974,"sender_is_bot":false,"owner_hint":true,"excerpt":"<<<x>>>","zulip_link":"https://simplewithus.zulipchat.com/#narrow/near/77","reply_to":{"type":"stream","channel":"agent-sync","topic":"t"}},"cursor":null}',
  signature: "v1,SJUAv0uDXgpua0hwJrswOUEKIeEdqKQLpKLD3mzMzqs=",
  dup: '{"status":200,"body":{"ok":true,"duplicate":true}}',
});

/** A SeatGate stand-in over one memory store, at a clock `now()` returns. */
function memoryGate(now = () => Date.now()) {
  const store = state.memoryStore();
  const gate = { store };
  for (const name of ["eventSubRoom", "eventSubUpsert", "eventSubDelete", "eventVerifiedGet", "eventVerifiedSet", "eventClaimWake", "eventSettleWake", "eventSubLive", "eventDeliveryResult", "eventSubsSummary", "spikeClaim", "audit", "tail", "setPaused"]) {
    gate[name] = (opts) => state[name](store, opts, now());
  }
  return gate;
}

/** A fake ChatGPT callback:  echoes challenges, records every POST, answers deliveries 200. */
function fakeCallback() {
  const calls = [];
  const fetchFn = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, raw: init.body, headers: init.headers, body });
    if (body.type === "verification") return Response.json({ challenge: body.challenge });
    return new Response("", { status: 200 });
  };
  return { calls, fetchFn };
}

function seatEvents(gate, config, extra = {}) {
  return new SeatEvents({ seat: "JET", scopes: ["zulip:read"], epoch: 0, approvedAt: Date.now() - 1000, paused: false, gate, config, channels, ...extra });
}

const SUB = (name = EVENT_NAME, url = CALLBACK, extra = {}) => ({ name, arguments: {}, delivery: { mode: "webhook", url, secret: SECRET }, cursor: null, ...extra });

function goldenWake() {
  return {
    contract: "agent-sync-wake/1", seat: "JET", wake_id: "w-golden", message_id: 77, dm: false, channel: "agent-sync", topic: "t", stream_id: 642232,
    sender_full_name: "Jay", sender_user_id: 1211974, is_bot: false, owner: true, excerpt: "<<<x>>>",
    zulip_link: "https://simplewithus.zulipchat.com/#narrow/near/77", reply_to: { type: "stream", channel: "agent-sync", topic: "t" }, sent_at: 1792000000,
  };
}

/** The golden zulip.mention run.  With `spikeFirst`, a spike subscription to the same chat goes live before anything else. */
async function zulipRun(config, { spikeFirst = false } = {}) {
  const now = 1792000000000;
  const gate = memoryGate(() => now);
  const cb = fakeCallback();
  const ev = seatEvents(gate, config, { approvedAt: now - 1000, fetchFn: cb.fetchFn, now: () => now });
  if (spikeFirst) await ev.subscribe(SUB(SPIKE_EVENT_NAME, CALLBACK));
  const out = {};
  out.definitionSha = sha256(JSON.stringify(ev.list().events.find((e) => e.name === EVENT_NAME)));
  out.listSha = sha256(JSON.stringify({ events: ev.list().events.filter((e) => e.name === EVENT_NAME) }));
  out.sub = JSON.stringify(await ev.subscribe(SUB()));
  try {
    await ev.subscribe(SUB(EVENT_NAME, `${CALLBACK}_b`));
    out.second = "accepted";
  } catch (error) {
    out.second = `${error.code} ${error.message}`;
  }
  const wake = goldenWake();
  const claim = await gate.eventClaimWake({ wakeId: wake.wake_id, messageId: 77 });
  const result = await deliverWake({ gate, wake, claim, channels, fetchFn: cb.fetchFn, now: () => now, sleep: async () => {} });
  out.reply = JSON.stringify(wakeReply(result));
  const deliveries = cb.calls.filter((c) => c.body.type !== "verification");
  assert.equal(deliveries.length, 1, "one delivery, to the zulip.mention subscription only");
  out.body = deliveries[0].raw;
  out.signature = deliveries[0].headers["webhook-signature"];
  out.dup = JSON.stringify(wakeReply(await gate.eventClaimWake({ wakeId: wake.wake_id })));
  return out;
}

// ---------------------------------------------------------------- regression:  zulip.mention unchanged

test("regression:  zulip.mention definition, list, subscribe, refusal, delivery and dedupe match origin/main, flag off", async () => {
  assert.deepEqual(await zulipRun(configOff), GOLDEN);
  assert.equal(sha256(JSON.stringify(eventDefinition(configOff))), GOLDEN.definitionSha);
  assert.equal(sha256(JSON.stringify(seatEvents(memoryGate(), configOff).list())), GOLDEN.listSha, "flag off:  events/list is exactly main's");
});

test("regression:  the same bytes with the flag on, and with a spike subscription live in the same chat", async () => {
  assert.deepEqual(await zulipRun(configOn), GOLDEN);
  assert.deepEqual(await zulipRun(configOn, { spikeFirst: true }), GOLDEN);
});

// ---------------------------------------------------------------- the flag

test("the flag:  only exactly \"on\" turns it on", () => {
  assert.equal(configOff.spikeExtraEvents, false);
  assert.equal(configOn.spikeExtraEvents, true);
  assert.equal(loadConfig(testEnv({ SPIKE_EXTRA_EVENTS: " on " })).spikeExtraEvents, true);
  for (const value of ["", "off", "ON", "1", "true", "yes"]) assert.equal(loadConfig(testEnv({ SPIKE_EXTRA_EVENTS: value })).spikeExtraEvents, false, value);
});

test("flag off:  events/list hides the spike and subscribing to it is an unknown event;  unsubscribing still works", async () => {
  const gate = memoryGate();
  const cb = fakeCallback();
  const ev = seatEvents(gate, configOff, { fetchFn: cb.fetchFn });
  assert.deepEqual(ev.list().events.map((e) => e.name), [EVENT_NAME]);
  await assert.rejects(ev.subscribe(SUB(SPIKE_EVENT_NAME)), (e) => e.code === INVALID_PARAMS && e.message === "Unknown event");
  assert.equal(cb.calls.length, 0, "nothing verified");
  // A spike subscription made while the flag was on can still be removed after it is turned off.
  await seatEvents(gate, configOn, { fetchFn: cb.fetchFn }).subscribe(SUB(SPIKE_EVENT_NAME, SPIKE_CALLBACK));
  assert.equal((await gate.eventSubsSummary()).length, 1);
  assert.deepEqual(await ev.unsubscribe({ name: SPIKE_EVENT_NAME, arguments: {}, delivery: { url: SPIKE_CALLBACK } }), {});
  assert.equal((await gate.eventSubsSummary()).length, 0);
  await assert.rejects(ev.unsubscribe({ name: "unknown.event", arguments: {}, delivery: { url: SPIKE_CALLBACK } }), (e) => e.code === INVALID_PARAMS);
  assert.deepEqual(KNOWN_EVENT_NAMES, ["zulip.mention", "fleet.spike.ping", "whatsapp.message"]);
});

test("flag on:  events/list offers zulip.mention then fleet.spike.ping, a content-free event with no arguments", () => {
  const events = seatEvents(memoryGate(), configOn).list().events;
  assert.deepEqual(events.map((e) => e.name), [EVENT_NAME, SPIKE_EVENT_NAME]);
  const spike = events[1];
  assert.deepEqual(spike, spikeEventDefinition());
  assert.deepEqual(spike.delivery, ["webhook"]);
  assert.deepEqual(spike.inputSchema, { type: "object", properties: {}, additionalProperties: false });
  assert.deepEqual(Object.keys(spike.payloadSchema.properties), ["id", "fired_at"]);
  assert.deepEqual(spike.payloadSchema.required, ["id", "fired_at"]);
  assert.equal(spike.payloadSchema.additionalProperties, false);
  assert.ok(!/whatsapp/i.test(JSON.stringify(spike)), "the spike implies nothing about WhatsApp");
  assert.deepEqual(seatEvents(memoryGate(), configOn, { scopes: ["zulip:write"] }).list(), { events: [] }, "still needs zulip:read");
});

test("flag on:  the spike takes no arguments", async () => {
  const ev = seatEvents(memoryGate(), configOn, { fetchFn: fakeCallback().fetchFn });
  await assert.rejects(ev.subscribe(SUB(SPIKE_EVENT_NAME, CALLBACK, { arguments: { channel: "agent-sync" } })), (e) => e.code === INVALID_PARAMS && e.message === "fleet.spike.ping takes no arguments");
  await assert.rejects(ev.subscribe(SUB(SPIKE_EVENT_NAME, CALLBACK, { arguments: { whsec_leak: 1 } })), (e) => !e.message.includes("whsec_leak"), "the refusal never echoes an argument name");
  await assert.rejects(ev.subscribe(SUB(SPIKE_EVENT_NAME, CALLBACK, { arguments: [] })), (e) => e.code === INVALID_PARAMS);
  assert.match((await ev.subscribe(SUB(SPIKE_EVENT_NAME, CALLBACK, { arguments: null }))).id, /^sub_/);
});

// ---------------------------------------------------------------- the cap is per (seat, event name)

test("the cap is one per event name:  both events in one chat, and a second chat for either is refused", async () => {
  const gate = memoryGate();
  const cb = fakeCallback();
  const ev = seatEvents(gate, configOn, { fetchFn: cb.fetchFn });
  const zulip = await ev.subscribe(SUB());
  const spike = await ev.subscribe(SUB(SPIKE_EVENT_NAME));
  assert.notEqual(zulip.id, spike.id);
  assert.equal(spike.id, await subscriptionId("JET", CALLBACK, SPIKE_EVENT_NAME, {}));
  assert.equal(cb.calls.filter((c) => c.body.type === "verification").length, 1, "the same callback is verified once");
  await assert.rejects(ev.subscribe(SUB(EVENT_NAME, `${CALLBACK}_other`)), (e) => e.code === -32600 && e.message.startsWith("Agent-Sync already sends zulip.mention for this seat to another chat."));
  await assert.rejects(ev.subscribe(SUB(SPIKE_EVENT_NAME, `${CALLBACK}_other`)), (e) => e.code === -32600 && e.message.startsWith("Agent-Sync already sends fleet.spike.ping for this seat to another chat."));
  // Refreshes of both still work.
  assert.equal((await ev.subscribe(SUB())).id, zulip.id);
  assert.equal((await ev.subscribe(SUB(SPIKE_EVENT_NAME))).id, spike.id);
  assert.deepEqual((await gate.eventSubsSummary()).map((s) => s.name).sort(), [SPIKE_EVENT_NAME, EVENT_NAME].sort());
  // The storage cap holds on its own too, per name.
  const extra = await subscriptionId("JET", `${CALLBACK}_x`, SPIKE_EVENT_NAME, {});
  const refused = await gate.eventSubUpsert({ sub: { id: extra, name: SPIKE_EVENT_NAME, arguments: {}, url: `${CALLBACK}_x`, host: "chatgpt.com", secret: SECRET, epoch: 0, approved_at: Date.now() }, ttlMs: DEFAULT_TTL_MS, max: 1 });
  assert.deepEqual(refused, { ok: false, reason: "cap" });
  // Unsubscribing the spike frees only the spike's slot.
  await ev.unsubscribe({ name: SPIKE_EVENT_NAME, arguments: {}, delivery: { url: CALLBACK } });
  await ev.subscribe(SUB(SPIKE_EVENT_NAME, `${CALLBACK}_other`));
  await assert.rejects(ev.subscribe(SUB(EVENT_NAME, `${CALLBACK}_other`)), (e) => e.code === -32600);
});

// ---------------------------------------------------------------- no cross-delivery

test("a wake never reaches a spike subscription, and a spike never reaches a zulip.mention subscription", async () => {
  const gate = memoryGate();
  const ev = seatEvents(gate, configOn, { fetchFn: fakeCallback().fetchFn });
  await ev.subscribe(SUB(EVENT_NAME, CALLBACK));
  await ev.subscribe(SUB(SPIKE_EVENT_NAME, SPIKE_CALLBACK));
  const posts = [];
  const fetchFn = async (url, init) => {
    posts.push({ url, body: JSON.parse(init.body) });
    return new Response("", { status: 200 });
  };

  // The claim hands back zulip.mention subscriptions only, and counts only those.
  const wake = { ...goldenWake(), wake_id: "w-x", sent_at: Math.floor(Date.now() / 1000) };
  const claim = await gate.eventClaimWake({ wakeId: wake.wake_id, messageId: 77 });
  assert.deepEqual(claim.subs.map((s) => s.name), [EVENT_NAME]);
  assert.ok((await gate.tail(5)).some((r) => r.event === "event_wake_captured" && r.subscribers === 1));
  // Even handed every subscription, fanOut sends a wake to zulip.mention only.
  const every = Object.values((await gate.store.get("event_subs")) ?? {});
  assert.equal(every.length, 2);
  const results = await fanOut({ gate, wake, subs: every, allowed, fetchFn, sleep: async () => {} });
  assert.equal(results.length, 1);
  assert.deepEqual(posts.map((p) => [p.url, p.body.name]), [[CALLBACK, EVENT_NAME]]);

  // And a spike, handed every subscription, goes to fleet.spike.ping only, with {id, fired_at} and nothing else.
  posts.length = 0;
  const spike = { contract: SPIKE_CONTRACT, seat: "JET", spike_id: randomUUID(), sent_at: Math.floor(Date.now() / 1000) };
  const fired = await deliverSpike({ gate, spike, subs: every, fetchFn, sleep: async () => {} });
  assert.deepEqual(fired, { outcome: "delivered", subscribers: 1, delivered: 1 });
  assert.deepEqual(posts.map((p) => [p.url, p.body.name]), [[SPIKE_CALLBACK, SPIKE_EVENT_NAME]]);
  assert.deepEqual(Object.keys(posts[0].body), ["eventId", "name", "timestamp", "data", "cursor"]);
  assert.deepEqual(posts[0].body.data, spikeData(spike));
  assert.deepEqual(Object.keys(posts[0].body.data), ["id", "fired_at"]);
  assert.equal(posts[0].body.data.id, spike.spike_id);
  assert.match(posts[0].body.data.fired_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.match(posts[0].body.eventId, /^evt_[0-9a-f]{32}$/);

  // SeatGate's spike claim hands back the spike subscription only.
  const spikeClaim = await gate.spikeClaim({ spikeId: randomUUID() });
  assert.deepEqual(spikeClaim.subs.map((s) => s.name), [SPIKE_EVENT_NAME]);
});

test("a spike with no spike subscription reaches nobody and says so", async () => {
  const gate = memoryGate();
  await seatEvents(gate, configOn, { fetchFn: fakeCallback().fetchFn }).subscribe(SUB());
  const claim = await gate.spikeClaim({ spikeId: randomUUID() });
  assert.deepEqual(claim.subs, []);
  const posts = [];
  const result = await deliverSpike({ gate, spike: { spike_id: randomUUID(), sent_at: 1792000000 }, subs: claim.subs, fetchFn: async (u) => posts.push(u) });
  assert.deepEqual(result, { outcome: "no_subscriber", subscribers: 0, delivered: 0 });
  assert.equal(posts.length, 0);
});

// ---------------------------------------------------------------- the signed route

function signed(body, key = SPIKE_KEY) {
  const bytes = Buffer.from(typeof body === "string" ? body : JSON.stringify(body));
  return { bytes: new Uint8Array(bytes), signature: createHmac("sha256", Buffer.from(key, "utf8")).update(bytes).digest("hex") };
}
const spikeBody = (extra = {}) => ({ contract: SPIKE_CONTRACT, seat: "JET", spike_id: randomUUID(), sent_at: Math.floor(Date.now() / 1000), ...extra });

test("the route:  classified apart from the wake, and only a hosted-seat shape", () => {
  assert.equal(classifyPath("/internal/spike/JET"), "spike");
  assert.equal(classifyPath("/internal/wake/JET"), "wake");
  for (const p of ["/internal/spike/", "/internal/spike/jet", "/internal/spike/JET/x", "/internal/spikes/JET"]) assert.equal(classifyPath(p), null, p);
  assert.equal(spikePathSeat("/internal/spike/JET"), "JET");
  assert.equal(spikePathSeat("/internal/wake/JET"), null);
  assert.equal(spikeKey("short"), "");
  assert.equal(spikeKey(` ${SPIKE_KEY}\n`), SPIKE_KEY);
});

test("the signature:  a good one passes;  bad, missing, the wake key's, or over other bytes is 401", async () => {
  const body = spikeBody();
  const good = signed(body);
  const ok = await verifySpike({ ...good, key: SPIKE_KEY, seat: "JET" });
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.spike, body);
  for (const signature of [undefined, "", "zz", "0".repeat(64), good.signature.toUpperCase(), signed(body, WAKE_KEY).signature, signed({ ...body, sent_at: body.sent_at + 1 }).signature]) {
    assert.deepEqual(await verifySpike({ bytes: good.bytes, signature, key: SPIKE_KEY, seat: "JET" }), { ok: false, status: 401, reason: "bad_signature" }, String(signature));
  }
  // No key, or a short one, is not configured (the route answers 404).
  assert.equal((await verifySpike({ ...good, key: "", seat: "JET" })).status, 404);
  assert.equal((await verifySpike({ ...good, key: "x".repeat(31), seat: "JET" })).status, 404);
});

test("the body:  freshness, shape, seat and size are checked after the signature", async () => {
  const now = Date.now();
  const nowS = Math.floor(now / 1000);
  const reason = async (body, opts = {}) => (await verifySpike({ ...signed(body), key: SPIKE_KEY, seat: "JET", now, ...opts })).reason;
  assert.equal(await reason(spikeBody({ sent_at: nowS - 301 })), "stale");
  assert.equal(await reason(spikeBody({ sent_at: nowS + 61 })), "future");
  assert.equal(await reason(spikeBody({ seat: "GROK-WEB" })), "seat_mismatch");
  assert.equal(await reason(spikeBody({ contract: "agent-sync-wake/1" })), "bad_contract");
  assert.equal(await reason(spikeBody({ spike_id: "not-a-uuid" })), "bad_spike_id");
  assert.equal(await reason(spikeBody({ spike_id: randomUUID().toUpperCase() })), "bad_spike_id");
  assert.equal(await reason(spikeBody({ sent_at: "now" })), "bad_sent_at");
  assert.equal(await reason(spikeBody({ text: "hello" })), "bad_fields", "no field beyond the four, so it can carry no content");
  assert.equal(await reason("[1]"), "bad_json");
  assert.equal(await reason("{"), "bad_json");
  assert.equal((await verifySpike({ bytes: new Uint8Array(0), signature: "", key: SPIKE_KEY, seat: "JET" })).reason, "empty_body");
  assert.equal((await verifySpike({ bytes: new Uint8Array(SPIKE_MAX_BODY + 1), signature: "", key: SPIKE_KEY, seat: "JET" })).reason, "too_large");
});

test("dedupe and rate limit:  a replayed id is a duplicate that costs nothing;  the fourth ping in an hour is 429 until the window moves", async () => {
  let now = Date.now();
  const gate = memoryGate(() => now);
  const ids = [randomUUID(), randomUUID(), randomUUID(), randomUUID()];
  assert.equal(SPIKE_RATE_LIMIT, 3);
  assert.equal((await gate.spikeClaim({ spikeId: ids[0], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS })).verdict, "go");
  assert.equal((await gate.spikeClaim({ spikeId: ids[0], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS })).verdict, "duplicate");
  now += 1000;
  assert.equal((await gate.spikeClaim({ spikeId: ids[1], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS })).verdict, "go");
  assert.equal((await gate.spikeClaim({ spikeId: ids[2], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS })).verdict, "go");
  const limited = await gate.spikeClaim({ spikeId: ids[3], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS });
  assert.equal(limited.verdict, "rate_limited");
  assert.ok(limited.retryAfterS > 3500 && limited.retryAfterS <= 3600, String(limited.retryAfterS));
  assert.deepEqual(spikeReply(limited), { status: 429, body: { ok: false, rate_limited: true }, retryAfterS: limited.retryAfterS });
  // A refused id was not spent:  once the oldest leaves the window, it goes through.
  now += SPIKE_RATE_WINDOW_MS - 1000 + 1;
  assert.equal((await gate.spikeClaim({ spikeId: ids[3], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS })).verdict, "go");
  assert.equal((await gate.spikeClaim({ spikeId: ids[1], max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS })).verdict, "duplicate", "still remembered inside its hour");
  const audit = (await gate.tail(20)).map((r) => r.event);
  for (const name of ["spike_captured", "spike_rate_limited"]) assert.ok(audit.includes(name), name);
});

test("a paused seat fires nothing, and the answers carry counts only", async () => {
  const gate = memoryGate();
  await gate.setPaused({ by: "test", paused: true });
  const paused = await gate.spikeClaim({ spikeId: randomUUID() });
  assert.deepEqual(paused, { verdict: "paused" });
  assert.deepEqual(spikeReply(paused), { status: 202, body: { ok: true, paused: true, subscribers: 0, delivered: 0 } });
  assert.deepEqual(spikeReply({ verdict: "duplicate" }), { status: 200, body: { ok: true, duplicate: true } });
  assert.deepEqual(spikeReply({ verdict: "go", outcome: "delivered", subscribers: 1, delivered: 1 }), { status: 202, body: { ok: true, subscribers: 1, delivered: 1, outcome: "delivered" } });
  assert.deepEqual(spikeReply({ verdict: "go", outcome: "retry", subscribers: 1, delivered: 0 }), { status: 202, body: { ok: true, subscribers: 1, delivered: 0, outcome: "retry" } });
});

test("a spike delivery is audited without the callback path or the secret", async () => {
  const gate = memoryGate();
  await seatEvents(gate, configOn, { fetchFn: fakeCallback().fetchFn }).subscribe(SUB(SPIKE_EVENT_NAME, SPIKE_CALLBACK));
  const claim = await gate.spikeClaim({ spikeId: randomUUID() });
  await deliverSpike({ gate, spike: { spike_id: randomUUID(), sent_at: 1792000000 }, subs: claim.subs, fetchFn: async () => new Response("", { status: 202 }) });
  const audit = JSON.stringify(await gate.tail(20));
  assert.match(audit, /"event":"event_delivery"/);
  assert.match(audit, /"wake_ref":"spike:/);
  assert.ok(!audit.includes("callback_spike") && !audit.includes(SECRET));
});
