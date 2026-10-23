import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import * as state from "../src/seat-state.js";
import * as wa from "../src/wa/state.js";
import { WA_EVENT, WA_HEADER, signature, verifyRequest, binding } from "../src/wa/contract.js";
import { serveWa, deliverPending } from "../src/wa/index.js";
import { SeatEvents, EVENT_NAME, SPIKE_EVENT_NAME } from "../src/events.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";
import { HostedTools } from "../src/hosted-tools.js";
import { toolListFor } from "../src/delegated-identities.js";
import { classifyPath } from "../src/policy.js";

const NOW = Date.now();
const HASH = "a".repeat(64), SENDER = "b".repeat(64), KEY = "c".repeat(64), OPKEY = "d".repeat(64);
const owner = { agent_hash: HASH, sender_hash: SENDER, cutoff: Math.floor(NOW / 1000) - 20, verified_via: "code+creator_check" };
const callback = "https://chatgpt.com/private-jet-conversation";
const config = { ...loadConfig(testEnv({ WHATSAPP_BRIDGE_ENABLED: "on", WHATSAPP_BRIDGE_WAKE_ENABLED: "on" })), eventSeat: "JET" };
const env = { WA_BRIDGE_HMAC_KEY_JET: KEY, WA_OPERATOR_HMAC_KEY_JET: OPKEY, WA_OWNER_BINDING_JET: JSON.stringify(owner) };
const msg = (id, text = "owner message", over = {}) => ({ id_hash: String(id).padStart(64, "0"), sender_hash: SENDER, timestamp: Math.floor(NOW / 1000), type: "text", text, ...over });
const batch = messages => ({ contract: "wa-ingest/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000), agent_hash: HASH, next_offset: "9223372036854775807", messages });
function setup(clock = () => Date.now()) {
  const store = state.memoryStore(); const gate = { store };
  for (const name of ["eventSubRoom", "eventSubUpsert", "eventSubDelete", "eventVerifiedGet", "eventVerifiedSet", "eventSubscriptions", "eventSubLive", "eventDeliveryResult", "audit", "setPaused"]) gate[name] = opts => state[name](store, opts, clock());
  for (const [name, fn] of Object.entries({ waIngest: wa.ingest, waRead: wa.read, waOperator: wa.operator, waClaim: wa.claim, waSettle: wa.settle })) gate[name] = opts => fn(store, opts, clock());
  const calls = [];
  const fetchFn = async (_url, init) => {
    const b = JSON.parse(init.body);
    if (b.type === "verification") return Response.json({ challenge: b.challenge });
    calls.push(b);
    return new Response("ok");
  };
  const ev = new SeatEvents({ seat: "JET", scopes: ["zulip:read"], epoch: 0, approvedAt: clock(), gate, config, channels: async () => null, fetchFn, now: clock, logRefusal: async () => {} });
  const sub = (name, url = callback) => ev.subscribe({ name, arguments: {}, delivery: { mode: "webhook", url, secret: `whsec_${Buffer.alloc(32, 9).toString("base64")}` } });
  return { store, gate, calls, fetchFn, ev, sub };
}
async function request(gate, fetchFn, body, kind = "ingest", overrides = {}) {
  const bytes = new TextEncoder().encode(JSON.stringify(body));
  const req = new Request(`https://example.test/internal/wa-${kind}/JET`, { method: "POST", headers: { [WA_HEADER]: await signature(kind === "operator" ? OPKEY : KEY, bytes) }, body: bytes });
  return serveWa(req, { ...env, ...overrides }, config, gate, fetchFn);
}

test("automatic owner wake; leading eyes retained silently; replay does not ring again; Zulip isolated", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT); await s.sub(SPIKE_EVENT_NAME).catch(() => {});
  const b = batch([msg(1), msg(2, "👀 read later")]);
  const first = await request(s.gate, s.fetchFn, b);
  assert.equal(first.status, 200);
  assert.deepEqual(await first.json(), { ok: true, processed: true, next_offset: b.next_offset, stored: 2, delivered: 1, pending: false });
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0].name, WA_EVENT);
  assert.deepEqual(s.calls[0].data, { source: "authenticated_owner", owner_authorized: true });
  assert.ok(!JSON.stringify(s.calls).includes("owner message"));
  assert.equal((await (await request(s.gate, s.fetchFn, b)).json()).stored, 0);
  assert.equal(s.calls.length, 1);
  const read = await s.gate.waRead({});
  assert.equal(read.items.length, 2);
  assert.equal(read.items[1].text, "👀 read later");
  assert.equal(read.items[1].silent, true);
  assert.equal((await s.gate.waRead({})).items.length, 0);
});

test("silent-only batch rings zero times and an interior emoji still wakes", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await request(s.gate, s.fetchFn, batch([msg(3, "👀")]));
  assert.equal(s.calls.length, 0);
  await request(s.gate, s.fetchFn, batch([msg(4, "look 👀")]));
  assert.equal(s.calls.length, 1);
});

test("Operator request is idempotent and never owner authority; ingest key cannot sign it", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  const b = { contract: "wa-operator/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000), request_id: randomUUID() };
  await request(s.gate, s.fetchFn, b, "operator");
  await request(s.gate, s.fetchFn, b, "operator");
  assert.equal(s.calls.length, 1);
  assert.deepEqual(s.calls[0].data, { source: "operator", owner_authorized: false });
  const bytes = new TextEncoder().encode(JSON.stringify(b));
  assert.equal((await verifyRequest(bytes, await signature(KEY, bytes), OPKEY, "operator")).status, 401);
  assert.equal((await request(s.gate, s.fetchFn, b, "operator", { WA_OPERATOR_HMAC_KEY_JET: KEY })).status, 404);
});

test("WhatsApp uses Jet's existing callback, rejects a second conversation at both checks", async () => {
  const s = setup();
  await assert.rejects(s.sub(WA_EVENT));
  await s.sub(EVENT_NAME);
  await assert.rejects(s.sub(WA_EVENT, callback + "other"));
  await s.sub(WA_EVENT);
  const result = await s.gate.eventSubUpsert({ sub: { id: "other", name: WA_EVENT, url: callback + "other" }, ttlMs: 100000 });
  assert.equal(result.ok, false);
  await s.gate.eventSubDelete({ id: (await s.gate.eventSubscriptions({})).find(s => s.name === EVENT_NAME).id });
  await assert.rejects(s.sub(EVENT_NAME, callback + "other"));
  assert.equal((await s.sub(EVENT_NAME)).id.length > 0, true);
});

test("offline subscription keeps pending; restart and replay deliver once with stable id", async () => {
  const s = setup();
  const b = batch([msg(5)]);
  assert.equal((await (await request(s.gate, s.fetchFn, b)).json()).delivered, 0);
  await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await request(s.gate, s.fetchFn, b);
  assert.equal(s.calls.length, 1);
  await request(s.gate, s.fetchFn, b);
  assert.equal(s.calls.length, 1);
});

test("callback failure retries use stable event ids and queue leases prevent concurrent duplicates", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await s.gate.waIngest({ body: batch([msg(6), msg(7)]), owner });
  const pending = await s.gate.waClaim({});
  assert.equal(pending.keys.length, 2, "coalesce one batch into one turn");
  assert.equal(await s.gate.waClaim({}), null);
  await s.gate.waSettle({ ...pending, delivered: false });
  const ids = [];
  const failure = async (_url, init) => { ids.push(JSON.parse(init.body).eventId); return new Response("down", { status: 400 }); };
  await deliverPending(s.gate, failure);
  await deliverPending(s.gate, s.fetchFn);
  assert.equal(ids[0], s.calls[0].eventId);
});

test("owner and agent pins, cutoff, stale timestamps, bad signature, replay window and strict schemas", async () => {
  const s = setup();
  const b = batch([msg(8, "foreign", { sender_hash: "e".repeat(64) }), msg(9, "old", { timestamp: owner.cutoff })]);
  assert.equal((await (await request(s.gate, s.fetchFn, b)).json()).stored, 0);
  assert.equal((await request(s.gate, s.fetchFn, { ...b, agent_hash: "f".repeat(64) })).status, 403);
  const bytes = new TextEncoder().encode(JSON.stringify(batch([])));
  assert.equal((await verifyRequest(bytes, "0".repeat(64), KEY, "ingest")).status, 401);
  assert.equal((await verifyRequest(bytes, await signature(KEY, bytes), KEY, "ingest", Date.now() + 301000)).status, 401);
  for (const invalid of [{ next_offset: 1 }, { extra: true }, { messages: [msg(10, null)] }, { messages: [msg(11, "x".repeat(4097))] }]) assert.equal((await request(s.gate, s.fetchFn, { ...batch([]), ...invalid })).status, 400);
  assert.equal(binding(JSON.stringify({ ...owner, verified_via: "first_message" })), null);
  assert.equal((await request(s.gate, s.fetchFn, batch([]), "ingest", { WA_OWNER_BINDING_JET: "" })).status, 404);
});

test("read acknowledgement requires earlier surfacing; bodies expire, tombstones prevent replay", async () => {
  let now = NOW; const s = setup(() => now);
  const b = batch([msg(12), msg(13, "👀 silent")]);
  await s.gate.waIngest({ body: b, owner });
  assert.equal((await s.gate.waRead({ ack_through: "2" })).reason, "not_surfaced");
  await s.gate.waRead({ limit: 1 });
  assert.equal((await s.gate.waRead({ ack_through: "2" })).reason, "not_surfaced");
  await s.gate.waRead({});
  assert.equal((await s.gate.waRead({ ack_through: "2" })).ok, true);
  now += 8 * 86400000;
  await wa.sweep(s.store, now);
  assert.equal((await s.store.get("wa:message:00000000000000000001")).text, null);
  assert.equal((await s.gate.waIngest({ body: b, owner })).stored, 0);
});

test("pause and disabled defaults; Jet-only event and read tool; provenance enclosed as untrusted data", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await s.gate.setPaused({ paused: true, by: "test" });
  await request(s.gate, s.fetchFn, batch([msg(14)]));
  assert.equal(s.calls.length, 0);
  assert.equal(loadConfig(testEnv()).whatsappEnabled, false);
  for (const seat of ["CODEX", "CURSOR", "GROK-WEB"]) assert.ok(!toolListFor(seat).byName.whatsapp_read);
  assert.ok(toolListFor("JET").byName.whatsapp_read);
  const tools = new HostedTools({ seat: "JET", scopes: ["zulip:read"], config, gate: s.gate, key: "", zulip: null, log: () => {} });
  const result = await tools.call("whatsapp_read", {});
  assert.equal(result.isError, undefined);
  assert.match(result.content[0].text, /owner message/);
  assert.match(result.content[0].text, /nonce=/);
  const foreign = new HostedTools({ seat: "CODEX", scopes: ["zulip:read"], config, gate: s.gate, key: "", zulip: null, log: () => {} });
  assert.equal((await foreign.call("whatsapp_read", {})).isError, true);
  assert.equal(classifyPath("/internal/wa-ingest/JET"), "wa");
  assert.equal(classifyPath("/internal/wa-ingest/CODEX"), null);
});


test("loaded signing credentials are redacted before storage and retention checkpoints sweep acknowledged bodies", async () => {
  let now = NOW; const s = setup(() => now);
  const b = batch([msg(20, `credential ${KEY}`)]);
  await s.gate.waIngest({ body: b, owner, known: [KEY] });
  const item = (await s.gate.waRead({})).items[0];
  assert.match(item.text, /^\[redacted:/);
  assert.ok(!JSON.stringify([...s.store.map]).includes(KEY));
  await s.gate.waRead({ ack_through: "1" });
  now += 86400001;
  await wa.sweep(s.store, now);
  assert.equal((await s.store.get("wa:message:00000000000000000001")).text, null);
});
