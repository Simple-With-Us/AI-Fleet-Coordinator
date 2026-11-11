// The DM rule on the hosted transport (owner decision, Oct 22 2026, hard cutover).
// A seat may not open a new direct message here either:  the refusal, its guidance and the fact
// that nothing was sent all match the stdio rule in scripts/simple_tools/dm_policy.py.
//
//   (f2)  a seat-initiated DM is refused and names Side-Chats
//   (f3)  a seat-to-seat attempt is refused and no message of any kind is sent
//   (f4)  a seat's ping to Jay is refused and points at Pending-for-Jay
//         a reply inside a DM Operator opened stays allowed
//   OPERATOR may open only a clarifying question
//
// No dependencies:  CI runs this with plain `node --test`.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { ZulipClient } from "../src/zulip.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { ERROR_META_KEY } from "../src/contract.js";
import { FakeZulip, BOT_EMAIL } from "./fake-zulip.mjs";
import { testEnv } from "./helpers.mjs";

// A synthetic Operator id, never the production one:  the fake operator user below is given this
// id and the harness pins OPERATOR_ZULIP_USER_ID to the same value, so `config.operatorUserId` and
// the conversation opener stay the same id without either being a live user (mirrors
// SYNTHETIC_OPERATOR_ID in scripts/simple_tools/tests/test_dm_rule.py).
const OPERATOR_USER_ID = 999000001;
const SIDE_CHATS_STREAM_ID = 646987;

function memoryGate(clock) {
  const store = S.memoryStore();
  const g = { store };
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) {
    g[name] = (options) => S[name](store, options, clock.now);
  }
  g.roleGet = () => S.roleGet(store, clock.now);
  g.callTail = (n) => S.callTail(store, n);
  return g;
}

/** The same GROK-WEB harness as dm.test.mjs, with the DM rule ON (the shared default is off there
 *  because those tests are about tagging and idempotency, not the rule). */
function harness({ seat = "GROK-WEB", rule = "on" } = {}) {
  const fake = new FakeZulip();
  const clock = { now: Date.parse("2026-10-22T10:00:00Z") };
  const logs = [];
  const config = loadConfig(testEnv({ SIMPLE_TOOLS_DM_RULE: rule, HOSTED_SEATS: "JET,GROK-WEB", OPERATOR_ZULIP_USER_ID: String(OPERATOR_USER_ID) }));
  const user = (name) => fake.users.find((u) => u.full_name === name);
  const operator = fake.addUser("operator-bot@zulip.test", "Operator");
  operator.is_bot = true;
  operator.role = 200;
  operator.is_admin = true;
  operator.user_id = OPERATOR_USER_ID;
  const tools = new HostedTools({
    seat,
    scopes: ["zulip:read", "zulip:write"],
    clientId: "https://grok.com/oauth/mcp-client.json",
    grantRef: "abcdef012345",
    paused: false,
    config,
    zulip: new ZulipClient({ email: BOT_EMAIL, key: fake.key, ownerUserId: config.ownerUserId, fetch: fake.fetch, sleep: async (ms) => (clock.now += ms), now: () => clock.now }),
    key: fake.key,
    gate: memoryGate(clock),
    cf: { asn: 13335, country: "US" },
    now: () => clock.now,
    sleep: async (ms) => { clock.now += ms; },
    log: (row) => logs.push(JSON.stringify(row)),
    usersCache: new Map(),
    channelsCache: new Map(),
  });
  const jay = user("Jay Wedgeworth");
  return { fake, tools, clock, logs, jay, codex: user("Codex"), cursor: user("Cursor"), operator };
}

function errorOf(result, code) {
  assert.equal(result.isError, true, JSON.stringify(result));
  const obj = JSON.parse(result.content[0].text);
  assert.deepEqual(result._meta[ERROR_META_KEY], obj);
  assert.equal(obj.code, code, JSON.stringify(obj));
  return obj;
}

const directMessages = (fake) => fake.messages.filter((m) => m.type === "private");

test("(f2) a seat-initiated DM is refused and names Side-Chats", async () => {
  const { tools, codex } = harness();
  const error = errorOf(await tools.call("zulip_dm_send", { user_ids: [codex.user_id], text: "hello" }), "dm_not_allowed");
  assert.match(error.message, /may not open a new direct message/);
  assert.match(error.message, /Side-Chats/);
  assert.ok(error.message.includes(String(SIDE_CHATS_STREAM_ID)), "the guidance names the stream id");
});

test("(f3) a seat-to-seat attempt sends nothing at all", async () => {
  const { tools, fake, codex } = harness();
  errorOf(await tools.call("zulip_dm_send", { user_ids: [codex.user_id], text: "handing this over" }), "dm_not_allowed");
  assert.deepEqual(directMessages(fake), [], "no DM was created; the message belongs in a Side-Chats topic");
});

test("(f4) a seat's ping to Jay is refused and points at Pending-for-Jay", async () => {
  const { tools, fake, jay } = harness();
  const error = errorOf(await tools.call("zulip_dm_send", { user_ids: [jay.user_id], text: "AFC-1 needs you" }), "dm_not_allowed");
  assert.match(error.message, /Pending-for-Jay/);
  assert.deepEqual(directMessages(fake), []);
});

test("a reply inside a DM Operator opened stays allowed", async () => {
  const { tools, fake, operator, jay } = harness();
  // Operator opened it:  the oldest message in that conversation came from Operator's user id.
  // Jay is in the conversation too (AFC-469):  a direct message goes out only when the owner
  // takes part in it.
  fake.addDm(operator, [fake.me, jay], "which seat is this for?");
  const result = await tools.call("zulip_dm_send", { user_ids: [operator.user_id, jay.user_id], text: "CODEX" });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  assert.ok(directMessages(fake).some((m) => m.sender_id === fake.me.user_id), "the seat answered in the conversation");
});

test("a reply inside a long DM Operator opened stays allowed (more than 50 messages)", async () => {
  const { tools, fake, operator, jay } = harness();
  // Operator opens the conversation;  the seat then keeps answering in it until the thread is long
  // enough that Operator's opener has scrolled out of the newest-50 window the rule used to read.
  // Read that way the lowest id left is one this seat sent, so the reply was refused as though the
  // seat had opened the DM.  Only anchoring at the oldest message still finds the true opener.
  fake.addDm(operator, [fake.me, jay], "which seat is this for?");
  for (let i = 0; i < 60; i += 1) fake.addDm(fake.me, [operator, jay], `answer ${i}`);
  const result = await tools.call("zulip_dm_send", { user_ids: [operator.user_id, jay.user_id], text: "CODEX" });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  assert.ok(directMessages(fake).some((m) => m.sender_id === fake.me.user_id), "the seat answered in the conversation");
});

test("a group DM whose 1:1 threads are older still finds the opener (union narrow)", async () => {
  const { tools, fake, codex, operator, jay } = harness();
  // Real Zulip answers a `dm:[...]` narrow with a **union**:  for a group conversation it also
  // returns the 1:1 threads the caller has with each named person, oldest first.  FakeZulip's own
  // narrow is an exact participant match, so this one read is answered here instead, the way the
  // live API behaves.
  //
  // The order matters:  a 1:1 thread with Codex goes in first, then Operator opens the group
  // {me, Codex, Operator}.  Asked for a single oldest message (num_after 1) the opener read got the
  // 1:1 message, the exact-participant filter threw it away, and dmRuleCheck then refused a reply
  // inside a conversation Operator had opened.  Reading a window finds the real opener.
  fake.addDm(codex, [], "earlier, just the two of us");
  fake.addDm(operator, [fake.me, codex, jay], "which seat is this for?");
  fake.failNext("GET", "messages", (entry) => {
    const operand = (JSON.parse(entry.params.narrow)[0].operand ?? []).map(Number);
    const union = fake.messages.filter(
      (m) => m.type === "private" && m.display_recipient.some((p) => operand.includes(p.id)),
    );
    const after = Number(entry.params.num_after);
    return fake.ok({ messages: union.slice(0, Math.max(after, 1)) });
  });
  const result = await tools.call("zulip_dm_send", { user_ids: [codex.user_id, operator.user_id, jay.user_id], text: "CODEX" });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  assert.ok(directMessages(fake).some((m) => m.sender_id === fake.me.user_id), "the seat answered in the conversation");
});

test("a conversation the seat opened itself is still refused after 50 messages", async () => {
  const { tools, fake } = harness();
  // The opener here is the seat, not Operator:  the long thread must not turn into a free pass.
  for (let i = 0; i < 60; i += 1) fake.addDm(fake.me, [], `mine ${i}`);
  const error = errorOf(await tools.call("zulip_dm_send", { user_ids: [fake.users.find((u) => u.full_name === "Codex").user_id], text: "hello" }), "dm_not_allowed");
  assert.match(error.message, /may not open a new direct message/);
});

// Operator is deliberately NOT in KNOWN_SEATS:  it is the server-side admin seat, not an OAuth
// connector seat, so no hosted grant may act as it.  The one-DM-it-may-open rule is therefore
// unreachable from here and is proven in scripts/simple_tools/tests/test_dm_rule.py instead;  what
// this transport must prove is that no hosted seat can open a DM at all.
test("no hosted seat can act as Operator to reach the clarifying-question allowance", async () => {
  const { tools, codex } = harness();
  const error = errorOf(await tools.call("zulip_dm_send", { user_ids: [codex.user_id], text: "which seat?", clarifying_question: true }), "dm_not_allowed");
  assert.match(error.message, /may not open a new direct message/);
});

test("the DM-rule check is metered as a read, and a cached send does not pay for it twice", async () => {
  const { tools, fake, operator, jay } = harness();
  fake.addDm(operator, [fake.me, jay], "which seat is this for?");
  const kinds = [];
  const reserve = tools.gate.reserve;
  tools.gate.reserve = (options) => { kinds.push(options.kind); return reserve(options); };
  const openerReads = () => fake.requestsTo("GET", "/messages").length;

  const first = await tools.call("zulip_dm_send", { user_ids: [operator.user_id, jay.user_id], text: "CODEX", idempotency_key: "cached-key-1" });
  assert.notEqual(first.isError, true, JSON.stringify(first));
  assert.ok(kinds.includes("read"), `the rule's Zulip read must be charged to the budget, saw ${JSON.stringify(kinds)}`);
  const readsAfterFirst = openerReads();

  // The retry sends nothing, so it must not repeat the DM-rule lookup either.
  kinds.length = 0;
  const again = await tools.call("zulip_dm_send", { user_ids: [operator.user_id, jay.user_id], text: "CODEX", idempotency_key: "cached-key-1" });
  assert.notEqual(again.isError, true, JSON.stringify(again));
  assert.equal(again.structuredContent.duplicate, true, "the cached row answers the retry");
  assert.deepEqual(kinds, [], "a cached send reserves nothing");
  assert.equal(openerReads(), readsAfterFirst, "a cached send issues no further Zulip read");
});

test("a malformed messages answer is a retryable read failure, never a terminal refusal", async () => {
  const { tools, fake, codex } = harness();
  // Zulip answered 200 with a body that holds no message list.  Reading that as "no
  // conversation exists" would refuse a seat's legitimate reply inside an Operator-opened DM
  // with a terminal dm_not_allowed no retry can clear, so the rule fails retryable instead.
  fake.failNext("GET", "messages", () => fake.ok({ messages: "not-a-list" }));
  const error = errorOf(await tools.call("zulip_dm_send", { user_ids: [codex.user_id], text: "hello" }), "unavailable");
  assert.equal(error.retryable, true, "the seat retries once the connection is back");
  assert.deepEqual(directMessages(fake), [], "nothing was sent while the rule could not decide");
});

test("a 429 on the DM-rule read stays a rate limit with its retry hint", async () => {
  const { tools, fake, codex } = harness();
  // Zulip answered, so this is not an outage:  the client retries once inside its budget and
  // then the ApiError reaches mapException, which renders rate_limited with the retry_after_s
  // hint instead of a generic unavailable.  Fail both attempts so the retry is exhausted too.
  fake.failNext("GET", "messages", () => fake.error(429, "RATE_LIMIT_HIT", { "retry-after": 7 }));
  fake.failNext("GET", "messages", () => fake.error(429, "RATE_LIMIT_HIT", { "retry-after": 7 }));
  const error = errorOf(await tools.call("zulip_dm_send", { user_ids: [codex.user_id], text: "hello" }), "rate_limited");
  assert.equal(error.retryable, true, "the seat backs off and retries");
  assert.equal(error.retry_after_s, 7, "the back-off hint survives");
  assert.deepEqual(directMessages(fake), [], "nothing was sent while the rule could not decide");
});

test("SIMPLE_TOOLS_DM_RULE=off restores the pre-rule behaviour, and the owner-in-the-conversation guard still holds", async () => {
  const fake = new FakeZulip();
  const clock = { now: Date.parse("2026-10-22T10:00:00Z") };
  const ruleOff = loadConfig(testEnv({ SIMPLE_TOOLS_DM_RULE: "off" }));
  const tools = new HostedTools({
    seat: "GROK-WEB",
    scopes: ["zulip:read", "zulip:write"],
    clientId: "https://grok.com/oauth/mcp-client.json",
    grantRef: "abcdef012345",
    paused: false,
    config: ruleOff,
    zulip: new ZulipClient({ email: BOT_EMAIL, key: fake.key, ownerUserId: ruleOff.ownerUserId, fetch: fake.fetch, sleep: async (ms) => (clock.now += ms), now: () => clock.now }),
    key: fake.key,
    gate: memoryGate(clock),
    cf: { asn: 13335, country: "US" },
    now: () => clock.now,
    sleep: async (ms) => { clock.now += ms; },
    log: () => {},
    usersCache: new Map(),
    channelsCache: new Map(),
  });
  const codex = fake.users.find((u) => u.full_name === "Codex");
  const jay = fake.users.find((u) => u.full_name === "Jay Wedgeworth");
  const result = await tools.call("zulip_dm_send", { user_ids: [codex.user_id, jay.user_id], text: "hello" });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  //  The older seat DM rule is off, but the owner-in-the-conversation guard (AFC-469) is not:  a
  //  direct message with nobody from the owner's side in it still goes out nowhere.
  const refused = await tools.call("zulip_dm_send", { user_ids: [codex.user_id], text: "hello" });
  assert.equal(refused.isError, true, JSON.stringify(refused));
  assert.equal(JSON.parse(refused.content[0].text).code, "dm_owner_not_participant");
});

// ---------------------------------------------------------------- scheduled messages and drafts
//
// A scheduled or draft direct message is the same act as sending it now, so the DM rule covers every
// direct target they build (the stdio twin does the same in _target, scripts/simple_tools/mcp/tools.py).
// Before this, only zulip_dm_send applied the rule here, and a seat could schedule or draft a DM to a
// conversation that included Jay.  The refusal is the same code and the same message, nothing is
// scheduled or drafted, and a channel target and a typing notice are not affected.

const deliverAt = (clock) => Math.floor(clock.now / 1000) + 3600;
const sortedIds = (...ids) => [...ids].sort((a, b) => a - b);

/** The three tools that build a direct target, each given the recipient ids. */
const DM_TARGET_TOOLS = {
  zulip_scheduled_create: (ids, { clock }) => ({ user_ids: ids, text: "later", deliver_at: deliverAt(clock) }),
  zulip_draft_create: (ids) => ({ user_ids: ids, text: "note" }),
  // An edit rewrites a draft's recipient set, so it is the same act as creating one.
  zulip_draft_edit: (ids, { draftId }) => ({ draft_id: draftId, user_ids: ids, text: "note two" }),
};

/** Call one of those tools against a harness.  A draft edit first needs an existing channel draft. */
async function callDmTarget(h, name, ids) {
  let draftId;
  if (name === "zulip_draft_edit") {
    const made = await h.tools.call("zulip_draft_create", { topic: "d", text: "channel draft" });
    assert.notEqual(made.isError, true, JSON.stringify(made));
    draftId = made.structuredContent.id;
  }
  return h.tools.call(name, DM_TARGET_TOOLS[name](ids, { clock: h.clock, draftId }));
}

const nothingCreated = (fake) => {
  assert.equal(fake.scheduled.size, 0, "nothing was scheduled");
  assert.equal(fake.requestsTo("POST", "/scheduled_messages").length, 0, "Zulip was not asked to schedule anything");
  for (const draft of fake.drafts.values()) assert.equal(draft.type, "stream", "no draft was rewritten into a direct message");
};

for (const name of Object.keys(DM_TARGET_TOOLS)) {
  test(`${name}: a seat opening a DM is refused with the same code and message as zulip_dm_send`, async () => {
    const h = harness();
    const { tools, fake, codex, jay } = h;
    // A seat, the owner and a group with both:  each kind of recipient has its own guidance.
    for (const ids of [[codex.user_id], [jay.user_id], [codex.user_id, jay.user_id]]) {
      const sent = errorOf(await tools.call("zulip_dm_send", { user_ids: ids, text: "hello" }), "dm_not_allowed");
      const refused = errorOf(await callDmTarget(h, name, ids), "dm_not_allowed");
      assert.equal(refused.message, sent.message, "the same message as zulip_dm_send");
      assert.match(refused.message, /may not open a new direct message/);
    }
    nothingCreated(fake);
  });

  test(`${name}: a reply inside a DM Operator opened stays allowed`, async () => {
    const h = harness();
    const { fake, operator, jay } = h;
    fake.addDm(operator, [fake.me, jay], "which seat is this for?");
    const result = await callDmTarget(h, name, [operator.user_id, jay.user_id]);
    assert.notEqual(result.isError, true, JSON.stringify(result));
    const to = sortedIds(operator.user_id, jay.user_id);
    if (name === "zulip_scheduled_create") assert.deepEqual(fake.scheduled.get(result.structuredContent.id).to, to);
    else assert.deepEqual(fake.drafts.get(result.structuredContent.id).to, to);
    // A schedule and a draft only store the message:  the seat has sent nothing yet.
    assert.deepEqual(directMessages(fake).filter((m) => m.sender_id === fake.me.user_id), []);
  });

  test(`${name}: a conversation the seat opened itself is still refused`, async () => {
    const h = harness();
    const { fake, codex, jay } = h;
    // The opener here is the seat, not Operator:  answering inside it is not the allowed reply.
    fake.addDm(fake.me, [codex, jay], "mine");
    const error = errorOf(await callDmTarget(h, name, [codex.user_id, jay.user_id]), "dm_not_allowed");
    assert.match(error.message, /may not open a new direct message/);
    nothingCreated(fake);
  });

  test(`${name}: the refusal is audited with its code and its recipients`, async () => {
    const h = harness();
    const { tools, fake, codex } = h;
    errorOf(await callDmTarget(h, name, [codex.user_id]), "dm_not_allowed");
    const row = (await tools.gate.callTail(10)).find((r) => r.tool === name && r.outcome === "error");
    assert.ok(row, "the refused call left an audit row");
    assert.equal(row.error_code, "dm_not_allowed");
    assert.deepEqual(row.recipient_ids, [codex.user_id]);
    nothingCreated(fake);
  });

  test(`${name}: with SIMPLE_TOOLS_DM_RULE=off it goes out again, and the owner-in-the-conversation guard still holds`, async () => {
    const h = harness({ rule: "off" });
    const { codex, jay } = h;
    const allowed = await callDmTarget(h, name, [codex.user_id, jay.user_id]);
    assert.notEqual(allowed.isError, true, JSON.stringify(allowed));
    // The rule is off, but the owner guard (AFC-469) is not:  a conversation without the owner goes nowhere.
    const refused = await callDmTarget(h, name, [codex.user_id]);
    assert.equal(refused.isError, true, JSON.stringify(refused));
    assert.equal(JSON.parse(refused.content[0].text).code, "dm_owner_not_participant");
  });
}

test("scheduled and draft messages to a channel are not affected by the DM rule", async () => {
  const { tools, fake, clock } = harness();
  const messageReads = () => fake.requestsTo("GET", "/messages").length;
  const before = messageReads();
  const scheduled = await tools.call("zulip_scheduled_create", { topic: "later", text: "channel message", deliver_at: deliverAt(clock) });
  assert.notEqual(scheduled.isError, true, JSON.stringify(scheduled));
  assert.equal(fake.scheduled.get(scheduled.structuredContent.id).type, "stream");
  const draft = await tools.call("zulip_draft_create", { topic: "d", text: "channel draft" });
  assert.notEqual(draft.isError, true, JSON.stringify(draft));
  const edited = await tools.call("zulip_draft_edit", { draft_id: draft.structuredContent.id, topic: "d2", text: "channel draft two" });
  assert.notEqual(edited.isError, true, JSON.stringify(edited));
  assert.equal(fake.drafts.get(draft.structuredContent.id).type, "stream");
  assert.equal(messageReads(), before, "a channel target looks up no direct message conversation");
});

test("a typing notice to a DM is not a message, and the DM rule does not refuse it", async () => {
  const { tools, fake, codex, jay } = harness();
  const before = fake.requestsTo("GET", "/messages").length;
  for (const ids of [[codex.user_id], [codex.user_id, jay.user_id]]) {
    const result = await tools.call("zulip_typing", { op: "start", user_ids: ids });
    assert.notEqual(result.isError, true, JSON.stringify(result));
  }
  assert.equal(fake.typing.length, 2, "both notices reached Zulip");
  assert.equal(fake.requestsTo("GET", "/messages").length, before, "no conversation lookup for a typing notice");
});
