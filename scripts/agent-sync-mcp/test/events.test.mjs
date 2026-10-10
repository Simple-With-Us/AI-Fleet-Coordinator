// MCP Events (src/events.js) and its SeatGate storage (src/seat-state.js):
// signing (checked against the standardwebhooks reference when installed),
// the callback URL guard, subscribe verification, delivery retries and 410,
// expiry, epoch revocation, pause, the wake_id claim, the channel allowlist,
// the access re-check before every attempt, and the answer to the listener.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EVENT_NAME,
  canonicalJson,
  subscriptionId,
  eventIdFor,
  parseWhsec,
  webhookSignature,
  signingSecrets,
  callbackUrlProblem,
  isHostPattern,
  grantTtl,
  DEFAULT_TTL_MS,
  MIN_TTL_MS,
  MAX_TTL_MS,
  DEFAULT_CALLBACK_HOSTS,
  verifyCallback,
  deliverEvent,
  fanOut,
  deliverWake,
  wakeReply,
  wakeOutcome,
  wakeChannelRefusal,
  WAKE_BUDGET_MS,
  DELIVERY_BACKOFF_MS,
  eventData,
  wakeMatches,
  validateArguments,
  eventDefinition,
  SeatEvents,
  EventError,
  CALLBACK_ENDPOINT_ERROR,
  INVALID_PARAMS,
} from "../src/events.js";
import * as state from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const SECRET = `whsec_${Buffer.alloc(32, 7).toString("base64")}`;
const SECRET2 = `whsec_${Buffer.alloc(32, 9).toString("base64")}`;
const CALLBACK = "https://chatgpt.com/backend-api/mcp-events/callback_123";
const config = loadConfig(testEnv());

/** A SeatGate stand-in:  every method runs seat-state against one memory store. */
function memoryGate(now = () => Date.now()) {
  const store = state.memoryStore();
  const gate = { store };
  for (const name of ["eventSubRoom", "eventSubUpsert", "eventSubDelete", "eventVerifiedGet", "eventVerifiedSet", "eventClaimWake", "eventSettleWake", "eventSubLive", "eventDeliveryResult", "eventSubsSummary", "audit", "tail", "bumpEpoch", "setPaused"]) {
    gate[name] = (opts) => state[name](store, opts, now());
  }
  return gate;
}

function wake(overrides = {}) {
  return {
    contract: "agent-sync-wake/1",
    seat: "JET",
    wake_id: "w-1",
    message_id: 77,
    dm: false,
    channel: "agent-sync",
    topic: "t",
    stream_id: 642232,
    sender_full_name: "Jay",
    sender_user_id: 1211974,
    is_bot: false,
    owner: true,
    excerpt: "<<<x>>>",
    zulip_link: "https://simplewithus.zulipchat.com/#narrow/near/77",
    reply_to: { type: "stream", channel: "agent-sync", topic: "t" },
    sent_at: Math.floor(Date.now() / 1000),
    ...overrides,
  };
}

/** A fake ChatGPT callback:  echoes verification challenges and records deliveries. */
function fakeCallback({ statuses = [], verifyEcho = true } = {}) {
  const calls = [];
  const fetchFn = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, headers: init.headers, body, redirect: init.redirect });
    if (body.type === "verification") return Response.json({ challenge: verifyEcho ? body.challenge : "nope" });
    const status = statuses.length ? statuses.shift() : 200;
    return new Response("", { status });
  };
  return { calls, fetchFn };
}

// ---------------------------------------------------------------- signing

test("whsec_ secrets must decode to 24-64 bytes", () => {
  assert.equal(parseWhsec(SECRET).length, 32);
  assert.equal(parseWhsec(`whsec_${Buffer.alloc(24).toString("base64")}`).length, 24);
  assert.equal(parseWhsec(`whsec_${Buffer.alloc(23).toString("base64")}`), null);
  assert.equal(parseWhsec(`whsec_${Buffer.alloc(65).toString("base64")}`), null);
  assert.equal(parseWhsec(Buffer.alloc(32).toString("base64")), null);
  assert.equal(parseWhsec("whsec_not base64!"), null);
});

test("signatures verify with the standardwebhooks reference library", async (t) => {
  let Webhook;
  try {
    ({ Webhook } = await import("standardwebhooks"));
  } catch {
    t.skip("standardwebhooks not installed (CI without npm ci)");
    return;
  }
  const body = JSON.stringify({ eventId: "evt_1", name: EVENT_NAME, timestamp: "2026-10-09T15:00:00Z", data: { a: 1 }, cursor: null });
  const ts = Math.floor(Date.now() / 1000);
  const sig = await webhookSignature([SECRET], "evt_1", ts, body);
  const headers = { "webhook-id": "evt_1", "webhook-timestamp": String(ts), "webhook-signature": sig };
  assert.deepEqual(new Webhook(SECRET).verify(body, headers), JSON.parse(body));
  // The reference's own signature is byte-identical to ours.
  assert.equal(new Webhook(SECRET).sign("evt_1", new Date(ts * 1000), body), sig);
  // During rotation both keys sign, and either verifies.
  const both = await webhookSignature([SECRET2, SECRET], "evt_1", ts, body);
  assert.equal(both.split(" ").length, 2);
  assert.ok(new Webhook(SECRET).verify(body, { ...headers, "webhook-signature": both }));
  assert.ok(new Webhook(SECRET2).verify(body, { ...headers, "webhook-signature": both }));
  assert.throws(() => new Webhook(SECRET2).verify(body, headers));
});

test("the previous secret signs only inside its rotation window", () => {
  const now = 1_000_000;
  assert.deepEqual(signingSecrets({ secret: SECRET2, prev_secret: SECRET, prev_until: now + 1 }, now), [SECRET2, SECRET]);
  assert.deepEqual(signingSecrets({ secret: SECRET2, prev_secret: SECRET, prev_until: now }, now), [SECRET2]);
});

// ---------------------------------------------------------------- identity

test("canonical JSON ignores key order, and ids are deterministic", async () => {
  assert.equal(canonicalJson({ b: 1, a: { d: [1, { z: 1, y: 2 }], c: null } }), '{"a":{"c":null,"d":[1,{"y":2,"z":1}]},"b":1}');
  const a = await subscriptionId("JET", CALLBACK, EVENT_NAME, { channel: "sandbox" });
  assert.equal(a, await subscriptionId("JET", CALLBACK, EVENT_NAME, { channel: "sandbox" }));
  assert.notEqual(a, await subscriptionId("GROK-WEB", CALLBACK, EVENT_NAME, { channel: "sandbox" }));
  assert.notEqual(a, await subscriptionId("JET", CALLBACK + "x", EVENT_NAME, { channel: "sandbox" }));
  assert.notEqual(a, await subscriptionId("JET", CALLBACK, EVENT_NAME, {}));
  assert.match(a, /^sub_[0-9a-f]{32}$/);
  assert.equal(await eventIdFor("w-1", a), await eventIdFor("w-1", a));
  assert.notEqual(await eventIdFor("w-1", a), await eventIdFor("w-2", a));
});

// ---------------------------------------------------------------- SSRF guard

test("callback URLs:  https, 443, public allowlisted host only", () => {
  const p = DEFAULT_CALLBACK_HOSTS;
  assert.equal(callbackUrlProblem(CALLBACK, p), null);
  assert.equal(callbackUrlProblem("https://api.openai.com/x", p), null);
  assert.equal(callbackUrlProblem("https://chatgpt.com:443/x", p), null);
  const reason = (u) => callbackUrlProblem(u, p)?.reason;
  assert.equal(reason("http://chatgpt.com/x"), "not_https");
  assert.equal(reason("https://chatgpt.com:8443/x"), "port");
  assert.equal(reason("https://user:pw@chatgpt.com/x"), "userinfo");
  assert.equal(reason("https://127.0.0.1/x"), "ip_literal");
  assert.equal(reason("https://[::1]/x"), "ip_literal");
  assert.equal(reason("https://169.254.169.254/latest"), "ip_literal");
  assert.equal(reason("https://localhost/x"), "local_name");
  assert.equal(reason("https://metadata.internal/x"), "local_name");
  assert.equal(reason("https://intranet/x"), "local_name");
  assert.equal(reason("https://evil.com/x"), "host_not_allowed");
  assert.equal(reason("https://chatgpt.com.evil.com/x"), "host_not_allowed");
  assert.equal(reason("https://evilchatgpt.com/x"), "host_not_allowed");
  assert.equal(reason("https://agent-sync.jays.services/internal/wake/JET"), "host_not_allowed");
  assert.equal(reason("https://chatgpt.com/a b"), "malformed");
  assert.equal(reason("not a url"), "malformed");
  assert.equal(reason(`https://chatgpt.com/${"a".repeat(2100)}`), "malformed");
  // A wildcard never matches its own apex unless listed.
  assert.equal(callbackUrlProblem("https://openai.com/x", ["*.openai.com"])?.reason, "host_not_allowed");
});

test("EVENT_CALLBACK_HOSTS is validated and defaults when unset", () => {
  assert.ok(isHostPattern("*.chatgpt.com"));
  assert.ok(!isHostPattern("*"));
  assert.ok(!isHostPattern("*.com.") && !isHostPattern("chatgpt"));
  assert.deepEqual([...loadConfig(testEnv({ EVENT_CALLBACK_HOSTS: undefined })).eventCallbackHosts], [...DEFAULT_CALLBACK_HOSTS]);
  assert.deepEqual([...loadConfig(testEnv({ EVENT_CALLBACK_HOSTS: "a.example.org,*.b.example.org" })).eventCallbackHosts], ["a.example.org", "*.b.example.org"]);
  assert.throws(() => loadConfig(testEnv({ EVENT_CALLBACK_HOSTS: "*" })), /EVENT_CALLBACK_HOSTS/);
});

// ---------------------------------------------------------------- ttl, arguments, payload

test("ttl:  default, clamped, never unlimited", () => {
  assert.equal(grantTtl(undefined), DEFAULT_TTL_MS);
  assert.equal(grantTtl(null), DEFAULT_TTL_MS);
  assert.equal(grantTtl(1000), MIN_TTL_MS);
  assert.equal(grantTtl(10 * MAX_TTL_MS), MAX_TTL_MS);
  assert.equal(grantTtl(2 * MIN_TTL_MS), 2 * MIN_TTL_MS);
  assert.throws(() => grantTtl("1h"), EventError);
});

test("arguments:  only an allowlisted channel", () => {
  assert.deepEqual(validateArguments(undefined, config), {});
  assert.deepEqual(validateArguments({ channel: "sandbox" }, config), { channel: "sandbox" });
  assert.throws(() => validateArguments({ channel: "general" }, config), EventError);
  assert.throws(() => validateArguments({ stream_id: 1 }, config), EventError);
  assert.deepEqual(eventDefinition(config).inputSchema.properties.channel.enum, [...config.channels.keys()]);
});

test("the channel filter matches stream messages in that channel only", () => {
  assert.ok(wakeMatches({}, wake(), config));
  assert.ok(wakeMatches({}, wake({ dm: true, stream_id: null, reply_to: { type: "direct", to: [1] } }), config));
  assert.ok(wakeMatches({ channel: "agent-sync" }, wake(), config));
  assert.ok(!wakeMatches({ channel: "sandbox" }, wake(), config));
  assert.ok(!wakeMatches({ channel: "agent-sync" }, wake({ dm: true, stream_id: null, reply_to: { type: "direct", to: [1] } }), config));
});

test("a channel filter needs both the name and the stream id CHANNELS pins for it", () => {
  const sandbox = (over) => wake({ channel: "sandbox", reply_to: { type: "stream", channel: "sandbox", topic: "t" }, stream_id: 642167, ...over });
  assert.ok(wakeMatches({ channel: "sandbox" }, sandbox(), config));
  // The name says sandbox but the id is agent-sync's (or another stream's, or missing):  no match.
  assert.ok(!wakeMatches({ channel: "sandbox" }, sandbox({ stream_id: 642232 }), config));
  assert.ok(!wakeMatches({ channel: "sandbox" }, sandbox({ stream_id: 999001 }), config));
  assert.ok(!wakeMatches({ channel: "sandbox" }, sandbox({ stream_id: undefined }), config));
  // The id is sandbox's but the name is not:  no match either.
  assert.ok(!wakeMatches({ channel: "sandbox" }, sandbox({ reply_to: { type: "stream", channel: "sandbox-2", topic: "t" } }), config));
});

test("a channel wake needs an allowlisted stream id;  a DM needs none and stays metadata-only", () => {
  assert.equal(wakeChannelRefusal(wake(), config), null);
  assert.equal(wakeChannelRefusal(wake({ stream_id: 642167 }), config), null);
  assert.equal(wakeChannelRefusal(wake({ stream_id: 999001 }), config), "stream_not_allowlisted");
  assert.equal(wakeChannelRefusal(wake({ stream_id: undefined }), config), "no_stream_id", "an older listener's body fails closed");
  assert.equal(wakeChannelRefusal(wake({ stream_id: null }), config), "no_stream_id");
  assert.equal(wakeChannelRefusal(wake({ stream_id: "642232" }), config), "no_stream_id");
  // dm alone is not enough:  a stream reply_to makes it a channel wake.
  assert.equal(wakeChannelRefusal(wake({ dm: true, stream_id: undefined }), config), "no_stream_id");
  const dm = wake({ dm: true, channel: null, topic: null, stream_id: undefined, excerpt: "<<<secret plans>>>", reply_to: { type: "direct", to: [5] } });
  assert.equal(wakeChannelRefusal(dm, config), null);
  const data = eventData(dm);
  assert.deepEqual([data.excerpt, data.channel, data.topic, data.reply_to], ["", null, null, { type: "direct", to: [5] }]);
});

test("event data carries Zulip fields only, matching payloadSchema", () => {
  const data = eventData(wake({ reply_prefix: "[JET·wake] re=77", trigger_ids: [77], wake_id: "w-9" }));
  const schema = eventDefinition(config).payloadSchema;
  assert.deepEqual(Object.keys(data).sort(), [...schema.required].sort());
  assert.equal(data.owner_hint, true);
  assert.equal(data.sender_is_bot, false);
  const dm = eventData(wake({ dm: true, channel: null, topic: null, excerpt: "<<<secret plans>>>", reply_to: { type: "direct", to: [5, "x"] } }));
  assert.deepEqual(dm.reply_to, { type: "direct", to: [5] });
  assert.equal(dm.channel, null);
  assert.equal(dm.excerpt, "", "a DM's text never leaves through the event (the seat cannot read DMs)");
  assert.equal(dm.message_id, 77);
  assert.equal(dm.sender_full_name, "Jay");
});

// ---------------------------------------------------------------- verification

test("callback verification:  signed challenge echoed in constant time", async () => {
  const cb = fakeCallback();
  assert.equal(await verifyCallback(cb.fetchFn, { url: CALLBACK, subId: "sub_x", secret: SECRET }), null);
  const call = cb.calls[0];
  assert.equal(call.body.type, "verification");
  assert.equal(call.redirect, "manual");
  assert.match(call.headers["webhook-id"], /^msg_verification_/);
  assert.equal(call.headers["X-MCP-Subscription-Id"], "sub_x");
  assert.match(call.headers["webhook-signature"], /^v1,/);
  const bad = fakeCallback({ verifyEcho: false });
  assert.equal(await verifyCallback(bad.fetchFn, { url: CALLBACK, subId: "sub_x", secret: SECRET }), "challenge_failed");
  assert.equal(await verifyCallback(async () => new Response("", { status: 302, headers: { location: "https://x" } }), { url: CALLBACK, subId: "s", secret: SECRET }), "redirect_refused");
  assert.equal(await verifyCallback(async () => new Response("no", { status: 500 }), { url: CALLBACK, subId: "s", secret: SECRET }), "bad_status");
  const timeout = async () => {
    throw Object.assign(new Error("t"), { name: "TimeoutError" });
  };
  assert.equal(await verifyCallback(timeout, { url: CALLBACK, subId: "s", secret: SECRET }), "timeout");
  assert.equal(await verifyCallback(async () => { throw new TypeError("dns"); }, { url: CALLBACK, subId: "s", secret: SECRET }), "unreachable");
});

// ---------------------------------------------------------------- subscribe

function seatEvents(gate, overrides = {}) {
  return new SeatEvents({ seat: "JET", scopes: ["zulip:read", "zulip:write"], epoch: 0, approvedAt: Date.now() - 1000, paused: false, gate, config, ...overrides });
}
const SUB = (extra = {}) => ({ name: EVENT_NAME, arguments: {}, delivery: { mode: "webhook", url: CALLBACK, secret: SECRET }, cursor: null, ...extra });

test("subscribe verifies once, stores, and is idempotent", async () => {
  const gate = memoryGate();
  const cb = fakeCallback();
  const ev = seatEvents(gate, { fetchFn: cb.fetchFn });
  const first = await ev.subscribe(SUB());
  assert.match(first.id, /^sub_/);
  assert.equal(first.cursor, null);
  assert.equal(first.truncated, false);
  assert.ok(Date.parse(first.refreshBefore) > Date.now() + DEFAULT_TTL_MS - 5000);
  const again = await ev.subscribe(SUB({ ttlMs: 2 * MIN_TTL_MS }));
  assert.equal(again.id, first.id);
  assert.equal(cb.calls.filter((c) => c.body.type === "verification").length, 1, "verification is cached per URL");
  assert.equal((await gate.eventSubsSummary()).length, 1);
});

test("subscribe refuses a bad secret, mode, name, host, scope or a paused seat", async () => {
  const gate = memoryGate();
  const refusals = [];
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn, logRefusal: async (r) => refusals.push(r) });
  const code = async (p, e = ev) => {
    try {
      await e.subscribe(p);
      return "ok";
    } catch (error) {
      return error.code;
    }
  };
  assert.equal(await code(SUB({ delivery: { mode: "webhook", url: CALLBACK, secret: "whsec_short" } })), INVALID_PARAMS);
  assert.equal(await code(SUB({ delivery: { mode: "poll", url: CALLBACK, secret: SECRET } })), INVALID_PARAMS);
  assert.equal(await code(SUB({ name: "zulip.other" })), INVALID_PARAMS);
  assert.equal(await code(SUB({ delivery: { mode: "webhook", url: "https://evil.example.org/cb", secret: SECRET } })), CALLBACK_ENDPOINT_ERROR);
  assert.deepEqual(refusals[0], { where: "events", reason: "callback_host_not_allowed", clientId: "", redirectUri: "evil.example.org" });
  assert.equal(await code(SUB({ delivery: { mode: "webhook", url: "http://chatgpt.com/cb", secret: SECRET } })), INVALID_PARAMS);
  assert.equal(await code(SUB(), seatEvents(gate, { scopes: ["zulip:write"] })), -32600);
  assert.equal(await code(SUB(), seatEvents(gate, { paused: true })), -32600);
  assert.deepEqual(seatEvents(gate, { scopes: [] }).list(), { events: [] });
  assert.equal(seatEvents(gate).list().events[0].name, EVENT_NAME);
});

test("a failed challenge is -32015 with data.reason, and nothing is stored", async () => {
  const gate = memoryGate();
  const ev = seatEvents(gate, { fetchFn: fakeCallback({ verifyEcho: false }).fetchFn });
  await assert.rejects(ev.subscribe(SUB()), (e) => e.code === CALLBACK_ENDPOINT_ERROR && e.data.reason === "challenge_failed");
  assert.equal((await gate.eventSubsSummary()).length, 0);
});

test("one live destination per seat:  a second chat is refused, never swapped in", async () => {
  const gate = memoryGate();
  const cb = fakeCallback();
  const ev = seatEvents(gate, { fetchFn: cb.fetchFn });
  const first = await ev.subscribe(SUB());
  await assert.rejects(ev.subscribe(SUB({ delivery: { mode: "webhook", url: `${CALLBACK}_other`, secret: SECRET } })), (e) => e.code === -32600 && /Stop monitoring there first/.test(e.message) && /lapses at/.test(e.message));
  assert.ok(!cb.calls.some((c) => c.url.endsWith("_other")), "a refused second chat is not even verified");
  // A different filter on the same callback is a second destination too.
  await assert.rejects(ev.subscribe(SUB({ arguments: { channel: "sandbox" } })), (e) => e.code === -32600);
  // Refreshing the live one still works, and it is still the only one.
  assert.equal((await ev.subscribe(SUB())).id, first.id);
  const summary = await gate.eventSubsSummary();
  assert.equal(summary.length, 1);
  // Once it is unsubscribed, another chat may subscribe.
  await ev.unsubscribe({ name: EVENT_NAME, arguments: {}, delivery: { mode: "webhook", url: CALLBACK } });
  await ev.subscribe(SUB({ delivery: { mode: "webhook", url: `${CALLBACK}_other`, secret: SECRET } }));
});

test("a revoked epoch frees the slot for a new grant's chat", async () => {
  const gate = memoryGate();
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn });
  await ev.subscribe(SUB());
  await gate.bumpEpoch({ by: "test", reason: "revoke" });
  const fresh = seatEvents(gate, { fetchFn: fakeCallback().fetchFn, epoch: 1 });
  await fresh.subscribe(SUB({ delivery: { mode: "webhook", url: `${CALLBACK}_new`, secret: SECRET } }));
  assert.ok((await gate.tail(10)).some((r) => r.event === "event_sub_dropped" && r.reason === "stale_epoch"));
});

test("a refresh with a new secret keeps the old one for the rotation window", async () => {
  let now = Date.now();
  const gate = memoryGate(() => now);
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn, now: () => now });
  const { id } = await ev.subscribe(SUB());
  await ev.subscribe(SUB({ delivery: { mode: "webhook", url: CALLBACK, secret: SECRET2 } }));
  const claim = await gate.eventClaimWake({ wakeId: "w-rot" });
  const sub = claim.subs.find((s) => s.id === id);
  assert.deepEqual(signingSecrets(sub, now), [SECRET2, SECRET]);
  now += 11 * 60 * 1000;
  assert.deepEqual(signingSecrets(sub, now), [SECRET2]);
});

test("unsubscribe is idempotent and stops delivery", async () => {
  const gate = memoryGate();
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn });
  await ev.subscribe(SUB({ arguments: { channel: "sandbox" } }));
  assert.deepEqual(await ev.unsubscribe({ name: EVENT_NAME, arguments: { channel: "sandbox" }, delivery: { mode: "webhook", url: CALLBACK } }), {});
  assert.deepEqual(await ev.unsubscribe({ name: EVENT_NAME, arguments: { channel: "sandbox" }, delivery: { mode: "webhook", url: CALLBACK } }), {});
  const claim = await gate.eventClaimWake({ wakeId: "w-u" });
  assert.equal(claim.verdict, "go");
  assert.deepEqual(claim.subs, []);
  assert.ok(!(await gate.tail(10)).some((r) => r.reason === "stale_epoch"), "removed by unsubscribe, not by the epoch check");
});

// ---------------------------------------------------------------- wake claim and delivery

test("a wake_id is claimed once;  a repeat is in flight until it settles, then a duplicate", async () => {
  const gate = memoryGate();
  const claim = await gate.eventClaimWake({ wakeId: "w-1" });
  assert.equal(claim.verdict, "go");
  assert.equal((await gate.eventClaimWake({ wakeId: "w-1" })).verdict, "in_flight");
  await gate.eventSettleWake({ wakeId: "w-1", token: claim.token, outcome: "delivered", delivered: 1 });
  assert.equal((await gate.eventClaimWake({ wakeId: "w-1" })).verdict, "duplicate");
  assert.equal((await gate.eventClaimWake({ wakeId: "w-2" })).verdict, "go");
});

test("the dedupe forgets after WAKE_SEEN_MS (longer than the sent_at window)", async () => {
  let now = Date.now();
  const gate = memoryGate(() => now);
  await gate.eventClaimWake({ wakeId: "w-1" });
  now += state.WAKE_SEEN_MS + 1;
  assert.equal((await gate.eventClaimWake({ wakeId: "w-1" })).verdict, "go");
  assert.ok(state.WAKE_SEEN_MS > 6 * 60 * 1000);
});

test("expired subscriptions and ones from a bumped epoch are dropped at delivery", async () => {
  let now = Date.now();
  const gate = memoryGate(() => now);
  // Epoch 0 is the store's starting epoch;  bump once so the current epoch is 1.
  await gate.bumpEpoch({ by: "test", reason: "start" });
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn, epoch: 1, now: () => now });
  await ev.subscribe(SUB({ ttlMs: MIN_TTL_MS }));
  assert.equal((await gate.eventClaimWake({ wakeId: "a" })).subs.length, 1);
  now += MIN_TTL_MS + 1;
  assert.equal((await gate.eventClaimWake({ wakeId: "b" })).subs.length, 0);
  assert.ok((await gate.tail(10)).some((r) => r.event === "event_sub_dropped" && r.reason === "expired"));

  await ev.subscribe(SUB());
  await gate.bumpEpoch({ by: "test", reason: "revoke" });
  assert.equal((await gate.eventClaimWake({ wakeId: "c" })).subs.length, 0);
  assert.ok((await gate.tail(10)).some((r) => r.event === "event_sub_dropped" && r.reason === "stale_epoch"));
});

test("a subscription from a grant past its 90-day age is dropped at delivery", async () => {
  let now = Date.now();
  const gate = memoryGate(() => now);
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn, approvedAt: now - (90 * 24 * 3600 * 1000 - 60_000), now: () => now });
  await ev.subscribe(SUB({ ttlMs: MAX_TTL_MS }));
  assert.equal((await gate.eventClaimWake({ wakeId: "g1" })).subs.length, 1);
  now += 2 * 60_000;
  assert.equal((await gate.eventClaimWake({ wakeId: "g2" })).subs.length, 0);
  assert.ok((await gate.tail(10)).some((r) => r.reason === "grant_too_old"));
});

test("a paused seat delivers nothing", async () => {
  const gate = memoryGate();
  await gate.setPaused({ by: "test", paused: true });
  assert.equal((await gate.eventClaimWake({ wakeId: "p" })).verdict, "paused");
});

test("delivery:  2xx once, 5xx retried with the same id and a fresh signature, 410 gone, 413 not retried", async () => {
  const sub = { id: "sub_a", url: CALLBACK, secret: SECRET };
  const event = { eventId: "evt_1", name: EVENT_NAME, timestamp: "2026-10-09T15:00:00Z", data: {}, cursor: null };
  const sleeps = [];
  const sleep = async (ms) => sleeps.push(ms);
  let clock = 1_800_000_000_000;
  const now = () => (clock += 1000);

  const ok = fakeCallback();
  assert.deepEqual(await deliverEvent(ok.fetchFn, sub, event, { sleep, now }), { outcome: "delivered", status: 200, attempts: 1 });
  assert.equal(ok.calls[0].headers["webhook-id"], "evt_1");
  assert.equal(ok.calls[0].headers["X-MCP-Subscription-Id"], "sub_a");
  assert.equal(ok.calls[0].headers["Content-Type"], "application/json");

  // Two attempts, 1 second apart:  delivery runs inside the listener's request now,
  // and the listener's own retries are the longer path.
  assert.deepEqual([...DELIVERY_BACKOFF_MS], [1000]);
  const flaky = fakeCallback({ statuses: [503, 200] });
  const r = await deliverEvent(flaky.fetchFn, sub, event, { sleep, now });
  assert.equal(r.outcome, "delivered");
  assert.equal(r.attempts, 2);
  assert.deepEqual(sleeps, [1000]);
  const ids = new Set(flaky.calls.map((c) => c.headers["webhook-id"]));
  const stamps = new Set(flaky.calls.map((c) => c.headers["webhook-timestamp"]));
  const sigs = new Set(flaky.calls.map((c) => c.headers["webhook-signature"]));
  assert.equal(ids.size, 1);
  assert.equal(stamps.size, 2);
  assert.equal(sigs.size, 2);

  const down = fakeCallback({ statuses: [500, 500, 500, 500] });
  assert.equal((await deliverEvent(down.fetchFn, sub, event, { sleep, now })).outcome, "failed");
  assert.equal(down.calls.length, 2);

  const gone = fakeCallback({ statuses: [410] });
  assert.equal((await deliverEvent(gone.fetchFn, sub, event, { sleep, now })).outcome, "gone");
  assert.equal(gone.calls.length, 1);

  const big = fakeCallback({ statuses: [413] });
  assert.deepEqual(await deliverEvent(big.fetchFn, sub, event, { sleep, now }), { outcome: "rejected", status: 413, attempts: 1 });

  const redirect = fakeCallback({ statuses: [307] });
  assert.equal((await deliverEvent(redirect.fetchFn, sub, event, { sleep, now })).outcome, "rejected");

  const huge = { ...event, data: { x: "a".repeat(300 * 1024) } };
  assert.equal((await deliverEvent(ok.fetchFn, sub, huge, { sleep, now })).status, 413);
});

test("fan-out:  matching subscriptions get one signed event each;  410 drops the subscription", async () => {
  const gate = memoryGate();
  await gate.bumpEpoch({ by: "t", reason: "start" });
  // Stored straight through SeatGate with a larger cap:  fan-out itself handles many.
  for (const [url, args] of [[CALLBACK, {}], [`${CALLBACK}_s`, { channel: "sandbox" }], [`${CALLBACK}_gone`, {}]]) {
    const id = await subscriptionId("JET", url, EVENT_NAME, args);
    await gate.eventSubUpsert({ sub: { id, name: EVENT_NAME, arguments: args, url, host: "chatgpt.com", secret: SECRET, epoch: 1, approved_at: Date.now() }, ttlMs: DEFAULT_TTL_MS, max: 10 });
  }

  const deliveries = [];
  const fetchFn = async (url, init) => {
    deliveries.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    return new Response("", { status: url.endsWith("_gone") ? 410 : 202 });
  };
  const w = wake();
  const claim = await gate.eventClaimWake({ wakeId: w.wake_id });
  const results = await fanOut({ gate, wake: w, subs: claim.subs, config, fetchFn, sleep: async () => {} });
  assert.equal(results.length, 2, "the sandbox-only subscription does not match an agent-sync mention");
  assert.deepEqual(results.map((r) => r.outcome).sort(), ["delivered", "gone"]);
  const body = deliveries.find((d) => d.url === CALLBACK).body;
  assert.equal(body.name, EVENT_NAME);
  assert.equal(body.cursor, null);
  assert.match(body.eventId, /^evt_/);
  assert.match(body.timestamp, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.equal(body.data.message_id, 77);
  assert.equal(deliveries.find((d) => d.url === CALLBACK).headers["webhook-id"], body.eventId);
  assert.equal((await gate.eventSubsSummary()).length, 2, "410 removed one");
  const audit = await gate.tail(20);
  assert.ok(audit.some((r) => r.event === "event_sub_dropped" && r.reason === "gone_410"));
  assert.ok(!JSON.stringify(audit).includes(SECRET), "no secret in the audit");
  assert.ok(!JSON.stringify(audit).includes("callback_123"), "no callback path in the audit");
});

// ---------------------------------------------------------------- the listener's wake, end to end

/** One seat with one live, unfiltered subscription, and a callback that answers `statuses` in turn. */
async function wakeSetup({ statuses = [], args = {} } = {}) {
  const gate = memoryGate();
  const ev = seatEvents(gate, { fetchFn: fakeCallback().fetchFn });
  await ev.subscribe(SUB({ arguments: args }));
  const deliveries = [];
  const fetchFn = async (url, init) => {
    deliveries.push({ url, body: JSON.parse(init.body) });
    return new Response("", { status: statuses.length ? statuses.shift() : 200 });
  };
  return { gate, ev, deliveries, fetchFn };
}

/** The listener's request:  claim, deliver inline, answer. */
async function listenerPost(gate, w, fetchFn, sleep = async () => {}) {
  const claim = await gate.eventClaimWake({ wakeId: w.wake_id, messageId: w.message_id });
  const result = claim.verdict === "go" ? await deliverWake({ gate, wake: w, claim, config, fetchFn, sleep }) : claim;
  return { result, reply: wakeReply(result) };
}

test("finding 1:  a wholly failed delivery is forgotten and answered 503;  the retry delivers;  a further retry is a duplicate", async () => {
  const { gate, deliveries, fetchFn } = await wakeSetup({ statuses: [503, 500] });
  const w = wake({ wake_id: "w-retry" });
  const first = await listenerPost(gate, w, fetchFn);
  assert.equal(deliveries.length, 2, "two attempts inside the request");
  assert.deepEqual(first.reply, { status: 503, body: { ok: false, retry: true, subscribers: 1, delivered: 0 } });
  const second = await listenerPost(gate, w, fetchFn);
  assert.equal(deliveries.length, 3);
  assert.deepEqual(second.reply, { status: 202, body: { ok: true, subscribers: 1, delivered: 1, outcome: "delivered" } });
  assert.equal(new Set(deliveries.map((d) => d.body.eventId)).size, 1, "every attempt and retry carries the same event id");
  const third = await listenerPost(gate, w, fetchFn);
  assert.deepEqual(third.reply, { status: 200, body: { ok: true, duplicate: true } });
  assert.equal(deliveries.length, 3);
  const events = (await gate.tail(20)).map((r) => r.event);
  for (const name of ["event_wake_captured", "event_delivery", "event_wake_released", "event_wake_settled"]) assert.ok(events.includes(name), name);
});

test("finding 1:  a 4xx or a 410 is a permanent drop (202, delivered 0), never a retry", async () => {
  for (const [status, outcome] of [[400, "rejected"], [413, "rejected"], [410, "gone"]]) {
    const { gate, deliveries, fetchFn } = await wakeSetup({ statuses: [status] });
    const w = wake({ wake_id: `w-${status}` });
    const { reply } = await listenerPost(gate, w, fetchFn);
    assert.deepEqual(reply, { status: 202, body: { ok: true, subscribers: 1, delivered: 0, outcome } }, String(status));
    assert.equal(deliveries.length, 1, String(status));
    assert.equal((await listenerPost(gate, w, fetchFn)).reply.status, 200, "settled:  a retry is a duplicate");
  }
});

test("finding 2:  a mention outside the allowlist is settled as refused and nothing is sent", async () => {
  for (const [streamId, audited] of [[999001, 999001], [undefined, null]]) {
    const { gate, deliveries, fetchFn } = await wakeSetup();
    const w = wake({ wake_id: `w-ch-${streamId}`, channel: "secret-plans", topic: "launch", stream_id: streamId, excerpt: "<<<the excerpt>>>", reply_to: { type: "stream", channel: "secret-plans", topic: "launch" } });
    const { reply } = await listenerPost(gate, w, fetchFn);
    assert.deepEqual(reply, { status: 202, body: { ok: true, subscribers: 1, delivered: 0, outcome: "channel_refused" } });
    assert.equal(deliveries.length, 0, "no callback hit");
    const row = (await gate.tail(1))[0];
    assert.deepEqual([row.event, row.stream_id], ["event_wake_channel_refused", audited]);
    const audit = JSON.stringify(await gate.tail(20));
    for (const leak of ["secret-plans", "launch", "the excerpt"]) assert.ok(!audit.includes(leak), `${leak} is not in the audit`);
    assert.equal((await listenerPost(gate, w, fetchFn)).reply.status, 200);
  }
  // fanOut refuses on its own too, whoever calls it.
  const { gate, deliveries, fetchFn } = await wakeSetup();
  const claim = await gate.eventClaimWake({ wakeId: "w-direct" });
  assert.deepEqual(await fanOut({ gate, wake: wake({ stream_id: 999001 }), subs: claim.subs, config, fetchFn }), []);
  assert.equal(deliveries.length, 0);
});

test("finding 2:  a DM wake still reaches an unfiltered subscription with no text, channel or topic", async () => {
  const { gate, deliveries, fetchFn } = await wakeSetup();
  const dm = wake({ wake_id: "w-dm", dm: true, channel: null, topic: null, stream_id: null, excerpt: "<<<private>>>", reply_to: { type: "direct", to: [1211974] } });
  const { reply } = await listenerPost(gate, dm, fetchFn);
  assert.equal(reply.status, 202);
  assert.equal(deliveries.length, 1);
  const { data } = deliveries[0].body;
  assert.deepEqual([data.dm, data.excerpt, data.channel, data.topic, data.reply_to], [true, "", null, null, { type: "direct", to: [1211974] }]);
});

test("finding 3:  an unsubscribe, a revoke or a pause during the backoff stops the next attempt", async () => {
  const flips = {
    unsubscribe: ({ ev }) => ev.unsubscribe({ name: EVENT_NAME, arguments: {}, delivery: { mode: "webhook", url: CALLBACK } }),
    revoke: ({ gate }) => gate.bumpEpoch({ by: "jay", reason: "revoke" }),
    pause: ({ gate }) => gate.setPaused({ by: "jay", paused: true }),
  };
  for (const [name, flip] of Object.entries(flips)) {
    const setup = await wakeSetup({ statuses: [503, 200] });
    const w = wake({ wake_id: `w-${name}` });
    const { result, reply } = await listenerPost(setup.gate, w, setup.fetchFn, async () => {
      await flip(setup);
    });
    assert.equal(setup.deliveries.length, 1, `${name}:  no attempt after the flip`);
    assert.equal(result.outcome, "not_live", name);
    assert.deepEqual(reply, { status: 202, body: { ok: true, subscribers: 1, delivered: 0, outcome: "not_live" } }, `${name}:  settled, not released`);
    const delivery = (await setup.gate.tail(5)).find((r) => r.event === "event_delivery");
    assert.equal(delivery.outcome, "not_live", name);
    assert.equal(delivery.reason, { unsubscribe: "removed", revoke: "stale_epoch", pause: "paused" }[name]);
  }
});

test("finding 3:  the check runs before the first attempt too, and fails closed", async () => {
  const sub = { id: "sub_a", url: CALLBACK, secret: SECRET };
  const event = { eventId: "evt_1", name: EVENT_NAME, timestamp: "2026-10-09T15:00:00Z", data: {}, cursor: null };
  const cb = fakeCallback({ statuses: [503, 200] });
  const flips = [true, false];
  const r = await deliverEvent(cb.fetchFn, sub, event, { sleep: async () => {}, isLive: async () => (flips.shift() ? { live: true } : { live: false, reason: "stale_epoch" }) });
  assert.deepEqual([r.outcome, r.reason, r.attempts, cb.calls.length], ["not_live", "stale_epoch", 1, 1]);
  const none = fakeCallback();
  assert.equal((await deliverEvent(none.fetchFn, sub, event, { isLive: async () => ({ live: false, reason: "paused" }) })).outcome, "not_live");
  assert.equal(none.calls.length, 0, "revoked after the claim, before the first attempt:  nothing is sent");
  const broken = await deliverEvent(none.fetchFn, sub, event, { isLive: async () => { throw new Error("gate down"); } });
  assert.deepEqual([broken.outcome, broken.error, none.calls.length], ["failed", "live_check_failed", 0], "an unanswered check sends nothing and asks for a retry");
});

test("the inline budget bounds the attempts and the answer comes in time", async () => {
  const sub = { id: "sub_a", url: CALLBACK, secret: SECRET };
  const event = { eventId: "evt_1", name: EVENT_NAME, timestamp: "2026-10-09T15:00:00Z", data: {}, cursor: null };
  const run = async (fetchMs, deadline) => {
    let t = 0;
    const calls = [];
    const fetchFn = async (_url, init) => {
      calls.push(init.signal);
      t += fetchMs;
      return new Response("", { status: 503 });
    };
    const r = await deliverEvent(fetchFn, sub, event, { now: () => t, sleep: async (ms) => { t += ms; }, deadline });
    return { r, calls: calls.length, t };
  };
  // Two fast failures fit;  a slow first one leaves no room for the second.
  assert.deepEqual(await run(500, WAKE_BUDGET_MS).then(({ r, calls }) => [r.outcome, calls]), ["failed", 2]);
  const slow = await run(8500, WAKE_BUDGET_MS);
  assert.deepEqual([slow.r.outcome, slow.calls], ["failed", 1]);
  assert.ok(slow.t <= WAKE_BUDGET_MS);
  // Nothing starts with less than a second left.
  assert.deepEqual(await run(100, 500).then(({ r, calls }) => [r.outcome, r.attempts, calls]), ["failed", 0, 0]);
});

test("the answer to the listener:  every verdict and outcome", () => {
  assert.deepEqual(wakeReply({ verdict: "duplicate" }), { status: 200, body: { ok: true, duplicate: true } });
  assert.deepEqual(wakeReply({ verdict: "in_flight", retryAfterS: 9 }), { status: 503, body: { ok: false, in_flight: true }, retryAfterS: 9 });
  assert.deepEqual(wakeReply({ verdict: "paused" }), { status: 202, body: { ok: true, paused: true, subscribers: 0 } });
  assert.deepEqual(wakeReply({ verdict: "go", outcome: "retry", subscribers: 1, delivered: 0 }), { status: 503, body: { ok: false, retry: true, subscribers: 1, delivered: 0 } });
  for (const outcome of ["delivered", "no_subscriber", "rejected", "gone", "not_live", "channel_refused"]) {
    assert.equal(wakeReply({ verdict: "go", outcome, subscribers: 1, delivered: outcome === "delivered" ? 1 : 0 }).status, 202, outcome);
  }
  assert.equal(wakeOutcome([]), "no_subscriber");
  assert.equal(wakeOutcome([{ outcome: "failed" }, { outcome: "delivered" }]), "delivered");
  assert.equal(wakeOutcome([{ outcome: "failed" }, { outcome: "rejected" }]), "retry");
  assert.equal(wakeOutcome([{ outcome: "gone" }, { outcome: "not_live" }]), "gone");
  assert.equal(wakeOutcome([{ outcome: "not_live" }]), "not_live");
});
