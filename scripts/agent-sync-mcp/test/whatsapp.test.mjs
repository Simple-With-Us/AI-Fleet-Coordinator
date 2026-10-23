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
const batch = messages => ({ contract: "wa-ingest/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000), agent_hash: HASH, sender_hash: SENDER, next_offset: "9223372036854775807", messages });
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
  assert.deepEqual(await first.json(), { ok: true, processed: true, next_offset: b.next_offset, stored: 2, delivered: 1, pending: true });
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
  const busy = await request(s.gate, s.fetchFn, { ...b, request_id: randomUUID() }, "operator");
  assert.equal(busy.status, 429); assert.match(busy.headers.get("Retry-After"), /^[1-9][0-9]*$/);
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
  let now = NOW; const s = setup(() => now); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await s.gate.waIngest({ body: batch([msg(6), msg(7)]), owner });
  const pending = await s.gate.waClaim({});
  assert.equal(pending.keys.length, 2, "coalesce one batch into one turn");
  assert.equal(await s.gate.waClaim({}), null);
  await s.gate.waSettle({ ...pending, delivered: false });
  now += 30001;
  const ids = [];
  const failure = async (_url, init) => { ids.push(JSON.parse(init.body).eventId); return new Response("down", { status: 400 }); };
  await deliverPending(s.gate, failure);
  now += wa.RERING_MS + 1;
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
  assert.ok(!toolListFor("JET").byName.whatsapp_read);
  assert.ok(toolListFor("JET", config).byName.whatsapp_read);
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
  assert.match(item.text, /^credential \[redacted:/);
  assert.ok(!JSON.stringify([...s.store.map]).includes(KEY));
  await s.gate.waRead({ ack_through: "1" });
  now += 86400001;
  await wa.sweep(s.store, now);
  assert.equal((await s.store.get("wa:message:00000000000000000001")).text, null);
});

test("flags refuse ingestion or suppress delivery independently; future item does not poison a batch", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  const b = batch([msg(30)]), bytes = new TextEncoder().encode(JSON.stringify(b));
  const make = () => new Request("https://test/internal/wa-ingest/JET", { method: "POST", body: bytes, headers: { [WA_HEADER]: signatureValue } });
  const signatureValue = await signature(KEY, bytes);
  assert.equal((await serveWa(make(), env, { ...config, whatsappEnabled: false }, s.gate, s.fetchFn)).status, 404);
  const held = await (await serveWa(make(), env, { ...config, whatsappWakeEnabled: false }, s.gate, s.fetchFn)).json();
  assert.equal(held.stored, 1); assert.equal(held.delivered, 0); assert.equal(s.calls.length, 0);
  const next = await request(s.gate, s.fetchFn, batch([msg(31, "future", { timestamp: Math.floor(Date.now()/1000) + 3600 }), msg(32)]));
  assert.equal(next.status, 200); assert.equal((await next.json()).stored, 1);
});

test("full 100-message emoji batch exceeds old 1 MiB limit and is acknowledged", async () => {
  const s = setup();
  const b = batch(Array.from({ length: 100 }, (_, i) => msg(100 + i, "😀".repeat(4096))));
  assert.ok(new TextEncoder().encode(JSON.stringify(b)).length > 1024 * 1024);
  const result = await (await request(s.gate, s.fetchFn, b)).json();
  assert.equal(result.processed, true); assert.equal(result.stored, 100);
});

test("silent prefix tolerates whitespace, invisible marks and non-text captions", async () => {
  const s = setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await request(s.gate, s.fetchFn, batch([msg(40, " \n\u200b👀 later"), msg(41, "👀 photo", { type: "image" })]));
  assert.equal(s.calls.length, 0); assert.equal((await s.gate.waRead({})).items.length, 2);
});

test("one outstanding owner ring survives delivery until read/ack, re-rings with a fresh event id", async () => {
  let now = NOW; const s = setup(() => now); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await request(s.gate, s.fetchFn, batch([msg(50)]));
  const id = s.calls[0].eventId;
  await request(s.gate, s.fetchFn, batch([msg(51)]));
  assert.equal(s.calls.length, 1, "new messages coalesce while Jet's original ring is outstanding");
  now += wa.RERING_MS + 1;
  await deliverPending(s.gate, s.fetchFn);
  assert.equal(s.calls.length, 2); assert.notEqual(s.calls[1].eventId, id);
  await s.gate.waRead({}); await s.gate.waRead({ ack_through: "2" });
  now += wa.RERING_MS + 1;
  assert.equal(await s.gate.waClaim({}), null);
});

test("Operator flood receives explicit backpressure without daily quotas or permanent rejected callbacks", async () => {
  let now = NOW; const s = setup(() => now); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await s.gate.waOperator({ requestId: randomUUID() });
  const seen = [...s.store.map.keys()].length;
  for (let i=0;i<30;i++) assert.equal((await s.gate.waOperator({ requestId: randomUUID() })).reason, "operator_busy");
  assert.equal([...s.store.map.keys()].length, seen, "refused floods consume no ledger rows");
  const reject = async () => new Response("no", { status: 400 });
  for (let i=0;i<3;i++) { await deliverPending(s.gate, reject); now += wa.RERING_MS + 1; }
  assert.equal(await s.gate.waClaim({ destination: (await s.gate.eventSubscriptions({})).filter(x=>x.name===WA_EVENT).map(x=>`${x.id}:${x.refresh_before}`).join(",") }), null);
});

test("35-day content-free ledgers expire; provider-age cutoff still prevents historical replay", async () => {
  let now = NOW; const s = setup(() => now);
  const b = batch([msg(60)]); await s.gate.waIngest({ body:b,owner });
  now += 36*86400000;
  while (await wa.sweep(s.store,now)) {}
  assert.equal((await s.gate.waIngest({ body:b,owner })).stored,0);
  assert.equal((await s.store.list({prefix:"wa:seen:"})).size,0);
});

test("subscription refresh preserves the re-ring delay while allowing blocked delivery recovery", async () => {
  let now = NOW; const s = setup(() => now);
  await s.gate.waIngest({ body: batch([msg(62)]), owner });
  const first = await s.gate.waClaim({ destination: "sub:version1" });
  await s.gate.waSettle({ ...first, delivered: true });
  assert.equal(await s.gate.waClaim({ destination: "sub:version2" }), null);
  now += wa.RERING_MS + 1;
  for (let i = 0; i < 3; i++) {
    const pending = await s.gate.waClaim({ destination: "sub:version2" });
    await s.gate.waSettle({ ...pending, delivered: false, permanent: true });
    if (i < 2) now += wa.RERING_MS + 1;
  }
  assert.equal(await s.gate.waClaim({ destination: "sub:version3" }), null);
  now += wa.RERING_MS + 1;
  assert.equal(await s.gate.waClaim({ destination: "sub:version2" }), null);
  assert.equal((await s.gate.waClaim({ destination: "sub:version3" })).source, "authenticated_owner");
});

test("Operator requests recover an older pending wake without an ingestion poller", async () => {
  let now = NOW; const s = setup(() => now);
  const op = () => ({ contract: "wa-operator/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000), request_id: randomUUID() });
  const original = op();
  assert.equal((await request(s.gate, s.fetchFn, original, "operator")).status, 200);
  assert.equal(s.calls.length, 0, "no subscription yet");
  now += wa.OPERATOR_DEBOUNCE_MS + 1;
  await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  const refused = op();
  assert.equal((await request(s.gate, s.fetchFn, refused, "operator")).status, 429);
  assert.equal(s.calls.length, 1, "busy request recovers the original pending id");
  assert.equal(await s.store.get(`wa:operator:${refused.request_id}`), undefined);
  assert.equal((await s.store.list({ prefix: "wa:pending:operator:" })).size, 0);
  assert.equal((await request(s.gate, s.fetchFn, original, "operator")).status, 200);
  assert.equal(s.calls.length, 1, "the recovered id remains deduplicated");
});

test("temporary MCP stop is retryable; audit/delivery errors preserve ingestion acknowledgments", async () => {
  const s=setup(); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  const b=batch([msg(70)]), raw=new TextEncoder().encode(JSON.stringify(b));
  const make=async()=>new Request("https://test/internal/wa-ingest/JET",{method:"POST",body:raw,headers:{[WA_HEADER]:await signature(KEY,raw)}});
  assert.equal((await serveWa(await make(),env,{...config,mcpDisabled:true},s.gate,s.fetchFn)).status,503);
  s.gate.eventDeliveryResult=async()=>{throw new Error("storage unavailable")};
  const ack=await (await serveWa(await make(),env,config,s.gate,s.fetchFn)).json();
  assert.equal(ack.processed,true); assert.equal(ack.delivered,1);
  s.gate.waClaim=async()=>{throw new Error("queue unavailable")};
  assert.equal((await (await serveWa(await make(),env,config,s.gate,s.fetchFn)).json()).processed,true);
});

test("existing transport limiter rejects large requests before body read; leased Operator does not block owner", async () => {
  const s=setup(); const b=batch([msg(71)]); const raw=new TextEncoder().encode(JSON.stringify(b));
  let key;
  const limiter={limit:async arg=>{key=arg.key;return {success:false}}};
  const req=new Request("https://test/internal/wa-ingest/JET",{method:"POST",body:raw,headers:{[WA_HEADER]:await signature(KEY,raw),"CF-Connecting-IP":"192.0.2.1"}});
  assert.equal((await serveWa(req,{...env,TOKEN_RATE_LIMITER:limiter},config,s.gate,s.fetchFn)).status,429);
  assert.equal(req.bodyUsed,false); assert.equal(key,"wa:ingest:192.0.2.1");
  await s.gate.waOperator({requestId:randomUUID()}); assert.equal((await s.gate.waClaim({})).source,"operator");
  await s.gate.waIngest({body:b,owner}); assert.equal((await s.gate.waClaim({})).source,"authenticated_owner");
});

test("expiry uses due index and a single acknowledgment range rather than per-row checkpoint scans", async()=>{
  let now=NOW; const s=setup(()=>now);
  await s.gate.waIngest({body:batch(Array.from({length:50},(_,i)=>msg(200+i))),owner});
  await s.gate.waRead({limit:50}); await s.gate.waRead({ack_through:"50"}); now+=86400001;
  const calls=[];const list=s.store.list.bind(s.store);s.store.list=async opts=>{calls.push(opts);return list(opts)};
  await wa.sweep(s.store,now);
  assert.ok(!calls.some(c=>c.prefix==="wa:ack_at:"));
  assert.equal((await s.store.get("wa:message:00000000000000000050")).text,null);
  calls.length=0; await wa.sweep(s.store,now);
  assert.ok(!calls.some(c=>c.prefix==="wa:message:"),"nothing due: no historical inbox scan");
});

test("signed JSON depth/node limits run before parsing and unsafe timestamps fail closed", async () => {
  for (const text of ['['.repeat(30) + '0' + ']'.repeat(30), '[' + '0,'.repeat(21000) + '0]']) {
    const raw = new TextEncoder().encode(text); const sig = await signature(KEY, raw);
    const original = JSON.parse;
    try {
      JSON.parse = () => { throw new Error('must reject before JSON.parse'); };
      assert.equal((await verifyRequest(raw, sig, KEY, "ingest")).status, 400);
    } finally { JSON.parse = original; }
  }
  const s = setup();
  for (const body of [{ ...batch([]), sent_at: Number.MAX_SAFE_INTEGER + 1 }, batch([msg(80, "x", { timestamp: Number.MAX_SAFE_INTEGER + 1 })])]) {
    assert.equal((await request(s.gate, s.fetchFn, body)).status, 400);
  }
  assert.equal((await request(s.gate, s.fetchFn, batch([msg(81, '{["comma, colon: text')]))).status, 200);
});

test("eligible owner doorbell precedes Operator and acknowledgment preserves rejection block", async () => {
  let now = NOW; const s = setup(() => now);
  await s.gate.waOperator({ requestId: randomUUID() });
  await s.gate.waIngest({ body: batch([msg(82)]), owner });
  const first = await s.gate.waClaim({ destination: "sub:1" });
  assert.equal(first.source, "authenticated_owner");
  await s.gate.waSettle({ ...first, delivered: false, permanent: true });
  for (let i = 0; i < 2; i++) {
    now += wa.RERING_MS + 1;
    const pending = await s.gate.waClaim({ destination: "sub:1" });
    await s.gate.waSettle({ ...pending, delivered: false, permanent: true });
  }
  await s.gate.waRead({}); await s.gate.waRead({ ack_through: "1" });
  assert.equal((await s.store.get("wa:owner_gate")).blocked, true);
  await s.gate.waIngest({ body: batch([msg(83)]), owner });
  assert.equal((await s.gate.waClaim({ destination: "sub:1" })).source, "operator");
  assert.equal((await s.gate.waClaim({ destination: "sub:2" })).source, "authenticated_owner");
});

test("acknowledgment cleanup is bounded, skips retired wakes immediately and sweep isolates failures", async () => {
  const s = setup();
  for (let page = 0; page < 3; page++) await s.gate.waIngest({ body: batch(Array.from({ length: 100 }, (_, i) => msg(1000 + 100 * page + i))), owner });
  for (let page = 0; page < 6; page++) await s.gate.waRead({ limit: 50 });
  await s.gate.waRead({ ack_through: "300" });
  assert.equal((await s.store.list({ prefix: "wa:pending:owner:" })).size, 200);
  assert.equal(await s.gate.waClaim({}), null);
  await s.gate.waIngest({ body: batch([msg(1500)]), owner });
  assert.equal((await s.gate.waClaim({})).seq, "301");
  while (await wa.sweep(s.store, Date.now() + 1)) {}
  assert.equal((await s.store.list({ prefix: "wa:pending:owner:" })).size, 1);
  await s.store.put("wa:expire:00000000000000000001:broken", null);
  await s.store.put("wa:expire:00000000000000000002:valid", { kind: "delete", keys: ["delete-me"] });
  await s.store.put("delete-me", true);
  assert.equal(await wa.sweep(s.store), false);
  assert.equal(await s.store.get("delete-me"), undefined);
  assert.ok(!s.store.map.has("wa:expire:00000000000000000001:broken"));
  const retry = [...s.store.map].find(([k]) => k.endsWith(":broken"));
  assert.equal(retry[1].failures, 1);
  assert.ok(Number(retry[0].split(":")[2]) > Date.now() + 50000);
});

test("Operator idempotency caches acceptance and retry hints reflect pending callback state", async () => {
  let now = NOW; const s = setup(() => now); const id = randomUUID();
  assert.equal((await s.gate.waOperator({ requestId: id })).outcome, "ok");
  assert.deepEqual((await s.store.get(`wa:operator:${id}`)).result, { ok: true });
  assert.equal((await s.gate.waOperator({ requestId: id })).duplicate, true);
  const pending = await s.gate.waClaim({ destination: "sub:1" });
  await s.gate.waSettle({ ...pending, delivered: false, permanent: true });
  now += 31000;
  const busy = await s.gate.waOperator({ requestId: randomUUID() });
  assert.ok(busy.retry_after_s >= 560);
});

test("Operator works without owner binding and its recovery cannot consume an owner wake", async () => {
  let now = NOW; const s = setup(() => now);
  const op = () => ({ contract: "wa-operator/1", seat: "JET", request_id: randomUUID(), sent_at: Math.floor(Date.now()/1000) });
  assert.equal((await request(s.gate, s.fetchFn, op(), "operator", { WA_OWNER_BINDING_JET: "" })).status, 200);
  now += 31000;
  await s.gate.waIngest({ body: batch([msg(1600)]), owner });
  await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  assert.equal((await request(s.gate, s.fetchFn, op(), "operator")).status, 429);
  assert.deepEqual(s.calls[0].data, { source: "operator", owner_authorized: false });
  await deliverPending(s.gate, s.fetchFn);
  assert.deepEqual(s.calls[1].data, { source: "authenticated_owner", owner_authorized: true });
});

test("signed sender-filter mismatch is refused even when the filtered batch is empty", async () => {
  const s = setup();
  const response = await request(s.gate, s.fetchFn, { ...batch([]), sender_hash: "f".repeat(64) });
  assert.equal(response.status, 403);
  assert.equal((await response.json()).reason, "binding_mismatch");
  assert.equal(await s.store.get("wa:last_ingest"), undefined);
});

test("ambiguous callback delivery is audited explicitly and reconciles with a stable event id", async () => {
  let now = NOW; const s = setup(() => now); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  let attempted;
  const unknown = async (_url, init) => { attempted = JSON.parse(init.body).eventId; throw new Error("response lost"); };
  const result = await (await request(s.gate, unknown, batch([msg(1601)]))).json();
  assert.equal(result.processed, true); assert.equal(result.delivered, 0);
  assert.equal((await s.gate.eventSubscriptions({})).find(x => x.name === WA_EVENT).last_outcome, "outcome_unknown");
  now += 31000; await deliverPending(s.gate, s.fetchFn);
  assert.equal(s.calls[0].eventId, attempted);
});

test("all Zulip-shaped keys are removed while surrounding owner text remains", async () => {
  const { redactSecret, scanSecret } = await import("../src/contract.js");
  // Synthetic scanner fixtures assembled at runtime; never real credentials.
  const first = "Aa1" + "b".repeat(29), second = "Bb2" + "c".repeat(29);
  const text = redactSecret(`before ${first} middle ${second} after`);
  assert.ok(!text.includes(first) && !text.includes(second));
  assert.ok(text.startsWith("before ") && text.endsWith(" after"));
  assert.equal(scanSecret(text), null);
});

test("owner permanent-rejection history survives transient failures, new rows and subscriber gaps", async () => {
  let now = NOW; const s = setup(() => now); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  const rejected = async () => new Response("refused", { status: 400 });
  await s.gate.waIngest({ body: batch([msg(1700)]), owner });
  await deliverPending(s.gate, rejected);
  assert.equal((await s.store.get("wa:owner_gate")).permanent_failures, 1);
  now += wa.RERING_MS + 1;
  await deliverPending(s.gate, async () => new Response("temporary", { status: 503 }));
  assert.equal((await s.store.get("wa:owner_gate")).permanent_failures, 1);
  now += 31000;
  await s.gate.waIngest({ body: batch([msg(1701), msg(1702)]), owner });
  await deliverPending(s.gate, rejected);
  const beforeGap = await s.store.get("wa:owner_gate");
  assert.equal(beforeGap.permanent_failures, 2);
  const subscriptions = s.gate.eventSubscriptions;
  s.gate.eventSubscriptions = async () => [];
  await deliverPending(s.gate, rejected);
  assert.deepEqual(await s.store.get("wa:owner_gate"), beforeGap);
  s.gate.eventSubscriptions = subscriptions;
  now += wa.RERING_MS + 1;
  await deliverPending(s.gate, rejected);
  const blocked = await s.store.get("wa:owner_gate");
  assert.equal(blocked.permanent_failures, 3); assert.equal(blocked.blocked, true);
  const rows = await s.store.list({ prefix: "wa:pending:owner:" });
  assert.equal(rows.size, 3);
  for (const row of rows.values()) assert.equal(row.permanent_failures, 3);
  now += wa.RERING_MS + 1;
  assert.equal(await s.gate.waClaim({ destination: blocked.destination }), null);
  const refreshed = await s.gate.waClaim({ destination: "fresh-subscription" });
  await s.gate.waSettle({ ...refreshed, delivered: false, permanent: true });
  assert.equal((await s.store.get("wa:owner_gate")).permanent_failures, 1);
  assert.equal((await s.store.get("wa:owner_gate")).blocked, false);
});


test("partial owner acknowledgment preserves the unknown coalesced ring event id", async () => {
  let now = NOW; const s = setup(() => now); await s.sub(EVENT_NAME); await s.sub(WA_EVENT);
  await s.gate.waIngest({ body: batch([msg(901), msg(902)]), owner });
  const unknownCalls = [];
  const unknown = async (_url, init) => { unknownCalls.push(JSON.parse(init.body)); throw new Error("synthetic lost reply"); };
  await deliverPending(s.gate, unknown);
  const firstId = unknownCalls[0].eventId;
  await s.gate.waRead({}); await s.gate.waRead({ ack_through: "1" });
  now += 30001;
  await deliverPending(s.gate, s.fetchFn);
  assert.equal(s.calls[0].eventId, firstId, "same coalesced ring despite its first row being acknowledged");
  await s.gate.waRead({ ack_through: "2" });
  await s.gate.waIngest({ body: batch([msg(903)]), owner });
  await deliverPending(s.gate, s.fetchFn);
  assert.notEqual(s.calls[1].eventId, firstId, "later independent work gets a new identity");
});

test("a consumed claim token cannot settle twice or change owner rejection history", async () => {
  let now = NOW; const s = setup(() => now);
  await s.gate.waIngest({ body: batch([msg(910), msg(911)]), owner });
  const claim = await s.gate.waClaim({ destination: "synthetic" });
  const refusal = { ...claim, delivered: false, permanent: true };
  await s.gate.waSettle(refusal);
  const once = [...s.store.map].map(([k,v]) => [k,structuredClone(v)]);
  now += 1000;
  await s.gate.waSettle(refusal);
  await s.gate.waSettle({ ...refusal, token: undefined });
  assert.deepEqual([...s.store.map], once, "replayed or absent token cannot mutate settled state");
  assert.equal((await s.store.get("wa:owner_gate")).permanent_failures, 1);
  now += wa.RERING_MS;
  const next = await s.gate.waClaim({ destination: "synthetic" });
  assert.notEqual(next.token, claim.token);
  await s.gate.waSettle({ ...next, delivered: false, permanent: true });
  assert.equal((await s.store.get("wa:owner_gate")).permanent_failures, 2);
});
