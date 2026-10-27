// AFC-545: the outbound WhatsApp leg.  A reply has no recipient argument,
// the binding is the only conversation it can reach, and a reply whose
// outcome is unknown stays unknown for good.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as state from "../src/seat-state.js";
import * as wa from "../src/wa/state.js";
import { WA_HEADER, signature, replyArgs, statusArgs } from "../src/wa/contract.js";
import { serveWa } from "../src/wa/index.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";
import { HostedTools, HOSTED_TOOLS_BY_NAME, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS } from "../src/hosted-tools.js";
import { toolListFor } from "../src/delegated-identities.js";
import { classifyPath } from "../src/policy.js";

const NOW = Date.now();
const HASH = "a".repeat(64), SENDER = "b".repeat(64), KEY = "c".repeat(64), OPKEY = "d".repeat(64);
const owner = { agent_hash: HASH, sender_hash: SENDER, cutoff: Math.floor(NOW / 1000) - 20, verified_via: "code+creator_check" };
const config = { ...loadConfig(testEnv({ WHATSAPP_BRIDGE_ENABLED: "on", WHATSAPP_BRIDGE_WAKE_ENABLED: "on" })), eventSeat: "JET" };
const env = { WA_BRIDGE_HMAC_KEY_JET: KEY, WA_OPERATOR_HMAC_KEY_JET: OPKEY, WA_OWNER_BINDING_JET: JSON.stringify(owner) };

function setup(clock = () => Date.now(), switches = { send: true }) {
  const store = state.memoryStore();
  const gate = { store };
  const binding = wa.bindingIdentity(env);
  gate.waIngest = opts => wa.ingest(store, opts, clock());
  gate.waRead = opts => wa.read(store, opts, clock());
  gate.waReply = opts => wa.reply(store, { ...opts, sendEnabled: switches.send, binding }, clock());
  gate.waSendStatus = opts => wa.sendStatus(store, { ...opts, sendEnabled: switches.send }, clock());
  gate.waOutboxClaim = opts => wa.outboxClaim(store, { ...opts, sendEnabled: switches.send, binding }, clock());
  gate.waOutboxResult = opts => wa.outboxResult(store, { ...opts, binding }, clock());
  for (const name of ["audit"]) gate[name] = opts => state[name](store, opts, clock());
  return { store, gate, switches };
}

function tools(s, scopes = ["zulip:read", "zulip:write"]) {
  return new HostedTools({ seat: "JET-CHAT", scopes, config, gate: s.gate, whatsappGate: s.gate, key: "", zulip: null, log: () => {} });
}

const ingestOne = async (s, text = "owner message") => {
  const body = {
    contract: "wa-ingest/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000),
    agent_hash: HASH, sender_hash: SENDER, next_offset: "9223372036854775807",
    messages: [{ id_hash: "1".padStart(64, "0"), sender_hash: SENDER, timestamp: Math.floor(Date.now() / 1000), type: "text", text }],
  };
  await s.gate.waIngest({ body, owner, known: [KEY] });
  await s.gate.waRead({});
};

test("the reply tool has no recipient argument and only the full Jet login serves it", async () => {
  const spec = HOSTED_TOOLS_BY_NAME.whatsapp_reply;
  assert.ok(spec, "the tool is served");
  assert.deepEqual(Object.keys(spec.inputSchema.properties).sort(), ["ack_through", "client_ref", "reply_to_seq", "text"]);
  assert.ok(!("to" in spec.inputSchema.properties) && !("recipient" in spec.inputSchema.properties));
  assert.equal(HOSTED_READ_TOOLS.has("whatsapp_reply"), false);
  assert.equal(HOSTED_WRITE_TOOLS.has("whatsapp_reply"), true);
  assert.ok(!toolListFor("JET", config).byName.whatsapp_reply);
  assert.ok(toolListFor("JET-CHAT", config).byName.whatsapp_reply);
  const s = setup();
  const foreign = new HostedTools({ seat: "CODEX", scopes: ["zulip:read", "zulip:write"], config, gate: s.gate, whatsappGate: s.gate, key: "", zulip: null, log: () => {} });
  assert.equal((await foreign.call("whatsapp_reply", { text: "hi", client_ref: "a1" })).isError, true);
});

test("the argument rules refuse a bad client_ref, a bad seq, an unknown field and a secret before any row is touched", async () => {
  const s = setup();
  const t = tools(s);
  const bad = async (args) => (await t.call("whatsapp_reply", args)).isError === true;
  assert.equal(await bad({ text: "hi", client_ref: "" }), true);
  assert.equal(await bad({ text: "hi", client_ref: "a b" }), true);
  assert.equal(await bad({ text: "hi", client_ref: "a".repeat(129) }), true);
  assert.equal(await bad({ text: "hi", client_ref: "a1", to: "user:1" }), true);
  assert.equal(await bad({ text: "hi", client_ref: "a1", reply_to_seq: "042" }), true);
  assert.equal(await bad({ text: "hi", client_ref: "a1", ack_through: "7" }), true);
  assert.equal(await bad({ text: "", client_ref: "a1" }), true);
  assert.equal(s.store.map.size, 0, "nothing was stored");
  // A secret-shaped reply is refused with the rule name, and nothing is stored.
  const refused = await t.call("whatsapp_reply", { text: `key ${"AK" + "IA" + "IOSFODNN7EXAMPLE"} here`, client_ref: "a1" });
  assert.equal(refused.isError, true);
  assert.match(refused.content[0].text, /AWS access key/);
  assert.equal(s.store.map.size, 0);
  // The contract check names the field.
  assert.deepEqual(replyArgs({ text: "hi", client_ref: "a1", nope: 1 }), { ok: false, field: "nope" });
  assert.deepEqual(statusArgs({ action: "release" }), { ok: false, field: "intent_id" });
});

test("a reply is idempotent on client_ref and two ids that differ only in punctuation never share a bucket", async () => {
  const s = setup();
  const t = tools(s);
  const first = await t.call("whatsapp_reply", { text: "one", client_ref: "abc-1" });
  assert.equal(first.isError, undefined);
  const again = await t.call("whatsapp_reply", { text: "one", client_ref: "abc-1" });
  assert.equal(again.isError, undefined);
  assert.equal(JSON.parse(first.content[0].text.split("\n").at(-2)).intent_id, JSON.parse(again.content[0].text.split("\n").at(-2)).intent_id);
  // A different punctuation is a different bucket and a different intent.
  const other = await t.call("whatsapp_reply", { text: "one", client_ref: "abc.1" });
  assert.notEqual(JSON.parse(other.content[0].text.split("\n").at(-2)).intent_id, JSON.parse(first.content[0].text.split("\n").at(-2)).intent_id);
  // The claim hands out one reply at a time.
  const claim = await s.gate.waOutboxClaim({});
  assert.equal(claim.reply.text, "one");
  const second = await s.gate.waOutboxClaim({});
  assert.notEqual(second.reply.intent_id, claim.reply.intent_id);
  assert.equal((await s.gate.waOutboxClaim({})).reply, null, "one reply at a time");
});

test("only a message whatsapp_read returned can be acknowledged", async () => {
  const s = setup();
  const t = tools(s);
  await ingestOne(s);
  const refused = await t.call("whatsapp_reply", { text: "hi", client_ref: "a1", ack_through: "2" });
  assert.equal(refused.isError, true);
  assert.match(refused.content[0].text, /has not returned/);
  const ok = await t.call("whatsapp_reply", { text: "hi", client_ref: "a1", ack_through: "1" });
  assert.equal(ok.isError, undefined);
  assert.equal((await s.store.get("wa:ack")), 1);
});

test("the outbox claim hands out one reply, and the result records the send", async () => {
  const s = setup();
  await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  const claim = await s.gate.waOutboxClaim({});
  assert.equal(claim.reply.text, "hello Jay");
  assert.ok(claim.reply.token);
  const result = await s.gate.waOutboxResult({ body: { intent_id: claim.reply.intent_id, outcome: "sent", message_id: "wamid.1", token: claim.reply.token } });
  assert.equal(result.state, "sent");
  const status = await s.gate.waSendStatus({});
  assert.equal(status.replies[0].state, "sent");
  assert.equal(status.replies[0].message_id, "wamid.1");
  assert.ok(!JSON.stringify(status).includes("hello Jay"), "the status never carries the text");
  // The text is gone once the reply is final.
  const row = await s.store.get(`wa:reply:${claim.reply.intent_id}`);
  assert.equal(row.text, null);
});

test("an unknown outcome stays unknown for good and the same text is refused afterwards", async () => {
  const s = setup();
  await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  const claim = await s.gate.waOutboxClaim({});
  await s.gate.waOutboxResult({ body: { intent_id: claim.reply.intent_id, outcome: "outcome_unknown", token: claim.reply.token } });
  // The claim is never handed out again.
  assert.equal((await s.gate.waOutboxClaim({})).reply, null);
  // The same client_ref is refused.
  const repeat = await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  assert.equal(repeat.ok, false);
  assert.equal(repeat.reason, "outcome_unknown");
  // A new reply with the same text is refused as well.
  const same = await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a2" }, known: [] });
  assert.equal(same.ok, false);
  assert.equal(same.reason, "same_text_as_unknown");
  // A different text with a new client_ref is a new intent.
  const fresh = await s.gate.waReply({ args: { text: "did you get my last one", client_ref: "a3" }, known: [] });
  assert.equal(fresh.ok, true);
  assert.equal(fresh.state, "queued");
});

test("a definite refusal releases the client_ref and the next attempt is linked to it", async () => {
  const s = setup();
  const first = await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  const claim = await s.gate.waOutboxClaim({});
  await s.gate.waOutboxResult({ body: { intent_id: claim.reply.intent_id, outcome: "refused", status: 403, code: 131005, token: claim.reply.token } });
  const second = await s.gate.waReply({ args: { text: "hello again", client_ref: "a1" }, known: [] });
  assert.equal(second.ok, true);
  assert.notEqual(second.intent_id, first.intent_id);
  const row = await s.store.get(`wa:reply:${second.intent_id}`);
  assert.equal(row.previous_intent_id, first.intent_id);
  const old = await s.store.get(`wa:reply:${first.intent_id}`);
  assert.equal(old.state, "refused");
  assert.equal(old.error_code, 131005);
});

test("SEND off holds the replies, hands out nothing, and only a release queues one again", async () => {
  const s = setup(() => Date.now(), { send: false });
  const t = tools(s);
  const held = await t.call("whatsapp_reply", { text: "hello Jay", client_ref: "a1" });
  assert.equal(held.isError, undefined);
  assert.match(held.content[0].text, /held/);
  const claim = await s.gate.waOutboxClaim({});
  assert.equal(claim.reply, null, "nothing is handed out while SEND is off");
  const status = await s.gate.waSendStatus({});
  assert.equal(status.replies[0].state, "held");
  // The action applies only to a held reply.
  const wrong = await t.call("whatsapp_send_status", { intent_id: status.replies[0].intent_id, action: "cancel" });
  assert.equal(wrong.isError, undefined, "a held reply takes the action");
  const status2 = await s.gate.waSendStatus({});
  assert.equal(status2.replies[0].state, "cancelled");
  const again = await t.call("whatsapp_send_status", { intent_id: status2.replies[0].intent_id, action: "release" });
  assert.equal(again.isError, true, "a cancelled reply is not held any more");
});

test("a released reply is queued again and a decline from the bridge is a result, never an unknown outcome", async () => {
  let now = NOW;
  const s = setup(() => now, { send: false });
  const created = await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  assert.equal(created.state, "held");
  // The switch comes back on.
  s.switches.send = true;
  const released = await s.gate.waSendStatus({ intent_id: created.intent_id, action: "release" });
  assert.equal(released.state, "queued");
  const claim = await s.gate.waOutboxClaim({});
  assert.equal(claim.reply.text, "hello Jay");
  // The bridge found SEND off before its POST and declined.
  const declined = await s.gate.waOutboxResult({ body: { intent_id: claim.reply.intent_id, outcome: "declined", token: claim.reply.token } });
  assert.equal(declined.state, "held");
  const status = await s.gate.waSendStatus({});
  assert.equal(status.replies[0].state, "held");
  assert.equal(status.replies[0].reason, "send_disabled");
});

test("a claim whose lease ends with no result is unknown for good", async () => {
  let now = NOW;
  const s = setup(() => now);
  await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  await s.gate.waOutboxClaim({});
  now += 5 * 60000 + 1;
  assert.equal((await s.gate.waOutboxClaim({})).reply, null, "nothing is handed out again");
  const status = await s.gate.waSendStatus({});
  assert.equal(status.replies[0].state, "outcome_unknown");
});

test("a held reply expires unsent after 24 hours and its text is deleted", async () => {
  let now = NOW;
  const s = setup(() => now, { send: false });
  const created = await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  now += 24 * 3600000 + 1000;
  await wa.sweep(s.store, now);
  const row = await s.store.get(`wa:reply:${created.intent_id}`);
  assert.equal(row.state, "expired");
  assert.equal(row.text, null);
});

test("a reply to a message quotes it through the keyed id the bridge still holds", async () => {
  const s = setup();
  await ingestOne(s);
  const created = await s.gate.waReply({ args: { text: "answering", client_ref: "a1", reply_to_seq: "1" }, known: [] });
  assert.equal(created.ok, true);
  const claim = await s.gate.waOutboxClaim({});
  assert.equal(claim.reply.reply_to_seq, "1");
  assert.equal(claim.reply.reply_to_id_hash, "1".padStart(64, "0"));
});

test("an unknown reply keeps its text for 24 hours and then it goes", async () => {
  let now = NOW;
  const s = setup(() => now);
  await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  const claim = await s.gate.waOutboxClaim({});
  await s.gate.waOutboxResult({ body: { intent_id: claim.reply.intent_id, outcome: "outcome_unknown", token: claim.reply.token } });
  let row = await s.store.get(`wa:reply:${claim.reply.intent_id}`);
  assert.equal(row.text, "hello Jay", "the text stays for a day");
  now += 24 * 3600000 + 1000;
  await wa.sweep(s.store, now);
  row = await s.store.get(`wa:reply:${claim.reply.intent_id}`);
  assert.equal(row.text, null, "and then it goes");
});

test("the retry cap is the Worker's: a reply that never left the bridge is refused at the cap", async () => {
  const s = setup();
  const created = await s.gate.waReply({ args: { text: "hello Jay", client_ref: "a1" }, known: [] });
  for (let round = 1; round <= 3; round++) {
    const claim = await s.gate.waOutboxClaim({});
    assert.ok(claim.reply, `round ${round} claims the reply`);
    assert.equal(claim.reply.attempts, round);
    const result = await s.gate.waOutboxResult({ body: { intent_id: claim.reply.intent_id, outcome: "requeue", token: claim.reply.token } });
    assert.equal(result.state, round < 3 ?  "queued" : "refused");
  }
  const status = await s.gate.waSendStatus({});
  assert.equal(status.replies[0].state, "refused");
  assert.equal(status.replies[0].intent_id, created.intent_id);
});

test("the WhatsApp module never reaches Zulip, the listener or the wake", async () => {
  const source = ["contract.js", "index.js", "state.js"].map((name) => readFileSync(new URL(`../src/wa/${name}`, import.meta.url), "utf8")).join("\n");
  for (const banned of ["zulip", "listener", "eavesdropper", "wake", "notes", "recall"]) {
    assert.ok(!new RegExp(`from "\\.\\./${banned}`).test(source), `the WhatsApp module imports ${banned}`);
  }
});

test("the signed outbox routes answer the bridge and carry no wake", async () => {
  const s = setup();
  const body = { contract: "wa-outbox/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000) };
  const bytes = new TextEncoder().encode(JSON.stringify(body));
  const req = new Request("https://example.test/internal/wa-outbox/JET", { method: "POST", headers: { [WA_HEADER]: await signature(KEY, bytes) }, body: bytes });
  const res = await serveWa(req, env, config, s.gate);
  assert.equal(res.status, 200);
  const answer = await res.json();
  assert.equal(answer.ok, true);
  assert.equal(answer.reply, null);
  // A body signed with the wrong key is refused.
  const bad = new Request("https://example.test/internal/wa-outbox/JET", { method: "POST", headers: { [WA_HEADER]: await signature(OPKEY, bytes) }, body: bytes });
  assert.equal((await serveWa(bad, env, config, s.gate)).status, 401);
  assert.equal(classifyPath("/internal/wa-outbox/JET"), "wa");
  assert.equal(classifyPath("/internal/wa-outbox/JET/result"), "wa");
  assert.equal(classifyPath("/internal/wa-outbox/CODEX"), null);
});

test("the result route accepts only the signed contract of the bridge", async () => {
  const s = setup();
  const body = { contract: "wa-outbox-result/1", seat: "JET", sent_at: Math.floor(Date.now() / 1000), intent_id: `wa_reply_${"a".repeat(32)}`, outcome: "sent" };
  const bytes = new TextEncoder().encode(JSON.stringify(body));
  const req = new Request("https://example.test/internal/wa-outbox/JET/result", { method: "POST", headers: { [WA_HEADER]: await signature(KEY, bytes) }, body: bytes });
  const res = await serveWa(req, env, config, s.gate);
  assert.equal(res.status, 403, "an unknown intent is refused");
});


test("state ack cursor fails closed and intent status honors its filter", async () => {
 const s=setup();
 for(const ack_through of ["bad", "01", "-1", "999999999999999999999999", 1]) {
  assert.equal((await s.gate.waReply({args:{text:"hi",client_ref:"a",ack_through}})).reason,"invalid_ack_through");
 }
 const a=await s.gate.waReply({args:{text:"first",client_ref:"a"}});
 await s.gate.waReply({args:{text:"second",client_ref:"b"}});
 const result=await s.gate.waSendStatus({intent_id:a.intent_id});
 assert.equal(result.replies.length,1);assert.equal(result.replies[0].intent_id,a.intent_id);
});

test("active claim refuses omitted or stale tokens without finalizing",async()=>{
 const s=setup();
 await s.gate.waReply({args:{text:"first",client_ref:"a"}});
 const claim=await s.gate.waOutboxClaim({});
 for(const token of [undefined,"wrong"]) {
  const result=await s.gate.waOutboxResult({body:{intent_id:claim.reply.intent_id,outcome:"sent",token}});
  assert.equal(result.reason,"stale_claim");
 }
 const result=await s.gate.waOutboxResult({body:{intent_id:claim.reply.intent_id,outcome:"sent",token:claim.reply.token}});
 assert.equal(result.state,"sent");
});

test("a refused retry keeps the quoted message id hash",async()=>{
 const s=setup();
 // Synthetic queued message record, independent of transport.
 await s.store.put("wa:message:00000000000000000001",{id_hash:"q".repeat(64)});
 const first=await s.gate.waReply({args:{text:"first",client_ref:"a",reply_to_seq:"1"}});
 assert.equal(first.ok,true);
 const claim=await s.gate.waOutboxClaim({});
 await s.gate.waOutboxResult({body:{intent_id:claim.reply.intent_id,outcome:"refused",token:claim.reply.token}});
 const retry=await s.gate.waReply({args:{text:"again",client_ref:"a",reply_to_seq:"1"}});
 const row=await s.store.get(`wa:reply:${retry.intent_id}`);
 assert.equal(row.reply_to_id_hash,"q".repeat(64));
});
