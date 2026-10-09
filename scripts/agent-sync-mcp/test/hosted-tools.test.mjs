// The seven hosted tools against a fake Zulip, with the shared golden fixtures
// (scripts/agent_sync/mcp/fixtures.jsonl) and the hosted rules:  seat binding,
// the D4 allowlist, spacing, budgets, idempotency, reconcile, 429, audit.
// No dependencies:  CI runs this with plain `node --test`.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { HostedTools, seatProblem, UnknownTool } from "../src/hosted-tools.js";
import { ZulipClient } from "../src/zulip.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import {
  TOOLS,
  TOOLS_BY_NAME,
  ERROR_META_KEY,
  schemaProblem,
  mapException,
  ApiError,
  NetworkError,
  UsageError,
  CredentialError,
  basicForms,
} from "../src/contract.js";
import { FakeZulip, BOT_EMAIL, OWNER_ID, STREAMS, zulipShapedKey } from "./fake-zulip.mjs";
import { ROOT, testEnv } from "./helpers.mjs";

const FIXTURES_PATH = path.join(ROOT, "../agent_sync/mcp/fixtures.jsonl");
const fixtures = (kind) =>
  readFileSync(FIXTURES_PATH, "utf8")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("//"))
    .map((l) => JSON.parse(l))
    .filter((c) => c.kind === kind && (c.transports ?? ["hosted"]).includes("hosted"));

const config = loadConfig(testEnv());
const MARKER_RE = /(BEGIN|END)[^\p{L}\p{N}]*UNTRUSTED[^\p{L}\p{N}]*ZULIP/iu;

/** A gate over seat-state.js with a Map store and the test clock. */
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

function harness({ fakeOptions = {}, scopes = ["zulip:read", "zulip:write"], paused = false, noKey = false, seat = "GROK-WEB" } = {}) {
  const fake = new FakeZulip(fakeOptions);
  const clock = { now: Date.parse("2026-10-09T10:00:00Z") };
  const logs = [];
  const gate = memoryGate(clock);
  const usersCache = new Map();
  const make = (over = {}) =>
    new HostedTools({
      seat,
      scopes,
      clientId: "https://grok.com/oauth/mcp-client.json",
      grantRef: "abcdef012345",
      paused,
      config,
      zulip: noKey ? null : new ZulipClient({ email: BOT_EMAIL, key: fake.key, fetch: fake.fetch, sleep: async (ms) => (clock.now += ms), now: () => clock.now }),
      key: noKey ? "" : fake.key,
      gate,
      cf: { asn: 13335, country: "US" },
      now: () => clock.now,
      sleep: async (ms) => {
        clock.now += ms;
      },
      log: (row) => logs.push(JSON.stringify(row)),
      usersCache,
      ...over,
    });
  return { fake, clock, logs, gate, tools: make(), make };
}

function structured(result, tool) {
  assert.ok(!result.isError, `expected success, got ${result.content?.[0]?.text}`);
  const problem = schemaProblem(TOOLS_BY_NAME[tool].outputSchema, result.structuredContent, "structuredContent");
  assert.equal(problem, null, problem);
  return result.structuredContent;
}

function toolError(result, code) {
  assert.equal(result.isError, true, JSON.stringify(result));
  assert.equal(result.structuredContent, undefined, "an error carries no structuredContent");
  const obj = JSON.parse(result.content[0].text);
  assert.deepEqual(result._meta[ERROR_META_KEY], obj);
  assert.equal(obj.code, code, JSON.stringify(obj));
  return obj;
}

/** The items between the one BEGIN and one END line, which share a nonce. */
function fence(text) {
  const lines = text.split("\n");
  const begins = lines.filter((l) => l.startsWith("BEGIN_UNTRUSTED_ZULIP"));
  const ends = lines.filter((l) => l.startsWith("END_UNTRUSTED_ZULIP"));
  assert.equal(begins.length, 1);
  assert.equal(ends.length, 1);
  assert.equal(begins[0].split("nonce=")[1], ends[0].split("nonce=")[1]);
  assert.match(begins[0], /^BEGIN_UNTRUSTED_ZULIP nonce=[0-9a-f]{16}$/);
  const start = lines.indexOf(begins[0]);
  const end = lines.indexOf(ends[0]);
  assert.ok(start < end);
  for (const l of lines.slice(0, start)) assert.ok(!MARKER_RE.test(l), `header line holds marker text:  ${l}`);
  return lines.slice(start + 1, end).map((l) => {
    assert.ok(!MARKER_RE.test(l), `fenced line holds marker text:  ${l}`);
    return JSON.parse(l);
  });
}

// ---------------------------------------------------------------- contract

test("tools.json is the stdio server's file, seven tools, closed inputs, no identity arguments", () => {
  const file = JSON.parse(readFileSync(path.join(ROOT, "../agent_sync/mcp/tools.json"), "utf8"));
  assert.deepEqual(TOOLS, file);
  assert.deepEqual(TOOLS.tools.map((t) => t.name), ["whoami", "topics", "read_topic", "inbox", "post", "reply", "react"]);
  for (const tool of TOOLS.tools) {
    assert.equal(tool.inputSchema.additionalProperties, false, tool.name);
    for (const banned of ["seat", "as", "rc", "no_tag", "fleet"]) assert.ok(!(banned in (tool.inputSchema.properties ?? {})), `${tool.name} takes ${banned}`);
  }
});

test("an unknown tool is a protocol error, not a tool error", async () => {
  const { tools } = harness();
  await assert.rejects(tools.call("hello", {}), UnknownTool);
  await assert.rejects(tools.call("__proto__", {}), UnknownTool);
});

test("fixtures:  fence (bodies, topic names and display names stay inside one nonce fence)", async () => {
  const cases = fixtures("fence");
  assert.ok(cases.length >= 8);
  const { fake, tools } = harness();
  for (const [n, c] of cases.entries()) {
    let topic = `fence ${n}`;
    let sender = "Codex";
    let body = "plain";
    if (c.field === "body") body = c.input;
    else if (c.field === "topic") topic = c.input;
    else sender = fake.addUser(`weird${n}@zulip.test`, c.input);
    fake.addMessage(sender, "agent-sync", topic, body);
    const result = await tools.call("read_topic", { topic: topic.slice(0, 60) });
    const items = fence(result.content[0].text);
    assert.equal(items.length, 1, c.case);
    assert.ok(items[0][c.field].includes(c.expect_contains), `${c.case}:  ${items[0][c.field]}`);
    assert.ok(!MARKER_RE.test(items[0][c.field]), c.case);
    if (c.field === "topic") fence((await tools.call("topics", { limit: 100 })).content[0].text);
  }
});

test("fixtures:  secret (refused before any request, naming the field)", async () => {
  const { fake, tools } = harness();
  const [, basic] = basicForms(BOT_EMAIL, fake.key);
  const fill = (s) => s.replaceAll("$FIXTURE_KEY", fake.key).replaceAll("$FIXTURE_BASIC", basic).replaceAll("$FIXTURE_ZULIP_SHAPED", zulipShapedKey(77));
  const cases = fixtures("secret");
  assert.ok(cases.length >= 4);
  for (const c of cases) {
    const args = { topic: "secret test", text: "fine" };
    args[c.field] = fill(c.input);
    const before = fake.requests.length;
    const error = toolError(await tools.call("post", args), c.expect);
    assert.ok(error.message.includes(c.field), c.case);
    assert.equal(fake.requests.length, before, `${c.case}:  a request went out before the scan`);
    assert.ok(!error.message.includes(fake.key));
  }
  const reply = toolError(await tools.call("reply", { message_id: 1, text: `k ${fake.key}` }), "refused_secret");
  assert.match(reply.message, /a loaded credential/);
});

test("fixtures:  mentions (raw mentions silenced, wildcards and groups neutralized), and `to`", async () => {
  const { fake, tools } = harness();
  for (const [n, c] of fixtures("mentions").entries()) {
    structured(await tools.call("post", { channel: "sandbox", topic: `m${n}`, text: c.input }), "post");
    assert.equal(fake.messages.at(-1).content, `[GROK-WEB] ${c.expect}`, c.case);
  }
  structured(await tools.call("post", { channel: "sandbox", topic: "to", text: "please look", to: ["codex"] }), "post");
  assert.equal(fake.messages.at(-1).content, "[GROK-WEB→CODEX] @**Codex** please look");
  const before = fake.requestsTo("POST", "messages").length;
  const error = toolError(await tools.call("post", { channel: "sandbox", topic: "to", text: "x", to: ["Codex", "Nobody At All"] }), "invalid_argument");
  assert.match(error.message, /to\[1\]/);
  for (const name of ["Codex", "Cursor", "MiniMax", "Jay"]) assert.ok(!error.message.includes(name), "never a member name");
  assert.equal(fake.requestsTo("POST", "messages").length, before);
});

test("fixtures:  schema (tool errors, nothing sent, the value never echoed)", async () => {
  const { fake, tools } = harness();
  for (const c of fixtures("schema")) {
    const before = fake.requests.length;
    const error = toolError(await tools.call(c.tool, c.arguments), c.expect);
    assert.equal(fake.requests.length, before, c.case);
    assert.ok(!error.message.includes("CODEX"), c.case);
  }
  assert.match(toolError(await tools.call("post", { topic: "t", text: "x", seat: "CODEX" }), "invalid_argument").message, /unknown argument 'seat'/);
  toolError(await tools.call("post", { topic: "t", text: "-" }), "invalid_argument");
  toolError(await tools.call("post", { topic: "t", text: "@*fleet*" }), "invalid_argument");
  toolError(await tools.call("post", { topic: "   ", text: "x" }), "invalid_argument");
  toolError(await tools.call("topics", "not an object"), "invalid_argument");
  assert.equal(fake.requestsTo("POST", "messages").length, 0);
});

test("fixtures:  error_map (CLI exception classes to error codes)", () => {
  const build = (c) => {
    switch (c.exception) {
      case "UsageError":
        return new UsageError("bad");
      case "CredentialError":
        return new CredentialError("no key");
      case "ApiError":
        return new ApiError("x", { status: c.status, code: null, data: c.data ?? {} });
      case "NetworkError":
        return new NetworkError("x", { timeout: c.timeout === true, maybeSent: c.maybe_sent === true });
      default:
        return new Error("boom");
    }
  };
  const cases = fixtures("error_map");
  assert.ok(cases.length >= 10);
  for (const c of cases) {
    const mapped = mapException(build(c), { write: c.tool === "write", sent: c.sent === true });
    assert.equal(mapped.code, c.expect, c.case);
    if ("retryable" in c) assert.equal(mapped.retryable, c.retryable, c.case);
    if ("retry_after_s" in c) assert.equal(mapped.retryAfterS, c.retry_after_s, c.case);
  }
});

test("fixtures:  inbox drops DMs, and the hosted allowlist drops off-list channels", async () => {
  for (const c of [...fixtures("inbox"), ...fixtures("allowlist").filter((x) => x.tool === "inbox")]) {
    const { fake, tools } = harness({ fakeOptions: { me: { full_name: "Claude" } } });
    const ids = c.messages.map((m) => fake.addMessage(m.from, m.dm ? "DM" : m.channel, m.topic ?? "", m.content, { dm: m.dm === true }).id);
    const data = structured(await tools.call("inbox", {}), "inbox");
    assert.deepEqual(data.ids, c.expect_indexes.map((i) => ids[i]), c.case);
    // One stream-scoped query per allowlisted channel, never `is:mentioned` alone.
    for (const r of fake.requestsTo("GET", "messages")) {
      const narrow = JSON.parse(r.params.narrow);
      assert.ok(narrow.some((o) => o.operator === "channel" && config.channelIds.includes(o.operand)), r.params.narrow);
    }
  }
});

test("fixtures:  allowlist (an off-list channel is refused before any request, on every channel tool)", async () => {
  const { fake, tools } = harness();
  for (const c of fixtures("allowlist").filter((x) => x.tool !== "inbox")) {
    const before = fake.requests.length;
    toolError(await tools.call(c.tool, c.arguments), c.expect);
    assert.equal(fake.requests.length, before, c.case);
  }
  for (const [tool, args] of [["topics", { channel: "other" }], ["read_topic", { channel: "general", topic: "t" }], ["post", { channel: "#Other", topic: "t", text: "x" }]]) {
    toolError(await tools.call(tool, args), "channel_not_allowed");
  }
  // reply and react fetch the target first and check its stream.
  const off = fake.addMessage("Codex", "other", "elsewhere", "hello");
  toolError(await tools.call("reply", { message_id: off.id, text: "x" }), "channel_not_allowed");
  toolError(await tools.call("react", { message_id: off.id, emoji: "eyes" }), "channel_not_allowed");
  const dm = fake.addMessage("Codex", "DM", "", "secret DM", { dm: true });
  toolError(await tools.call("reply", { message_id: dm.id, text: "x" }), "invalid_argument");
  toolError(await tools.call("react", { message_id: dm.id, emoji: "eyes" }), "channel_not_allowed");
  assert.equal(fake.requestsTo("POST", "messages").length, 0);
  assert.equal(fake.requests.filter((r) => /reactions$/.test(r.path)).length, 0);
});

// ---------------------------------------------------------------- seat binding

test("seat binding:  only a member bot whose users/me is this seat's email and tag", () => {
  const good = { is_bot: true, email: BOT_EMAIL, role: 400, is_admin: false, is_owner: false };
  const seat = { seat: "GROK-WEB", email: BOT_EMAIL };
  assert.equal(seatProblem(good, seat), null);
  assert.match(seatProblem({ ...good, role: 200, is_admin: true }, seat), /role is 200; hosted seats accept member \(400\) only/);
  assert.match(seatProblem({ ...good, role: 300 }, seat), /member \(400\) only/);
  assert.match(seatProblem({ ...good, is_bot: false }, seat), /not a bot/);
  assert.match(seatProblem({ ...good, email: "claude-bot@simplewithus.zulipchat.com" }, seat), /another bot/);
  assert.match(seatProblem({ ...good, email: "openai-dot-bot@simplewithus.zulipchat.com" }, { seat: "GROK-WEB", email: "openai-dot-bot@simplewithus.zulipchat.com" }), /another seat/);
});

test("an admin bot key is refused with no post, and the refusal is cached and audited", async () => {
  const { fake, tools, gate } = harness({ fakeOptions: { me: { role: 200, is_admin: true } } });
  const error = toolError(await tools.call("post", { channel: "sandbox", topic: "t", text: "hi" }), "not_authorized");
  assert.match(error.message, /role is 200/);
  toolError(await tools.call("whoami", {}), "not_authorized");
  assert.equal(fake.requestsTo("GET", "users/me").length, 1, "the refusal is cached for 10 minutes");
  assert.equal(fake.requestsTo("POST", "messages").length, 0);
  assert.equal((await S.tail(gate.store, 5)).find((r) => r.event === "role_refused")?.role, 200);
});

test("the role is re-checked every 10 minutes", async () => {
  const { fake, tools, clock } = harness();
  structured(await tools.call("whoami", {}), "whoami");
  structured(await tools.call("whoami", {}), "whoami");
  assert.equal(fake.requestsTo("GET", "users/me").length, 1);
  clock.now += S.ROLE_TTL_MS;
  fake.users[0].role = 200;
  fake.users[0].is_admin = true;
  toolError(await tools.call("whoami", {}), "not_authorized");
  assert.equal(fake.requestsTo("GET", "users/me").length, 2);
});

test("no key installed, a missing scope and a paused seat are tool errors", async () => {
  toolError(await harness({ noKey: true }).tools.call("whoami", {}), "not_authorized");
  const readOnly = harness({ scopes: ["zulip:read"] });
  const error = toolError(await readOnly.tools.call("post", { topic: "t", text: "x" }), "not_authorized");
  const result = await readOnly.tools.call("post", { topic: "t", text: "x" });
  assert.match(result._meta["mcp/www_authenticate"][0], /insufficient_scope.*zulip:write/);
  assert.match(error.message, /zulip:write/);
  structured(await readOnly.tools.call("whoami", {}), "whoami");
  toolError(await harness({ paused: true }).tools.call("whoami", {}), "paused");
});

test("whoami:  the seat from the grant, the hosted transport and the bare tag", async () => {
  const { tools } = harness();
  const data = structured(await tools.call("whoami", {}), "whoami");
  assert.deepEqual(data, {
    seat: "GROK-WEB",
    email: BOT_EMAIL,
    user_id: 1212596,
    role: 400,
    realm: "https://simplewithus.zulipchat.com",
    transport: "hosted",
    tag_prefix: "[GROK-WEB]",
    session_source: "none",
  });
});

// ---------------------------------------------------------------- reads

test("read_topic:  stateless since_id, own posts hidden unless include_self, owner flag by id and client", async () => {
  const { fake, tools } = harness();
  const a = fake.addMessage("Jay Wedgeworth", "agent-sync", "work", "from the web app");
  const b = fake.addMessage("Jay Wedgeworth", "agent-sync", "work", "from a script", { client: "python-requests" });
  const c = fake.addMessage(fake.me, "agent-sync", "work", "[GROK-WEB] my own post");
  const first = await tools.call("read_topic", { topic: "work" });
  assert.deepEqual(structured(first, "read_topic"), { count: 2, ids: [a.id, b.id], next_since_id: c.id });
  const items = fence(first.content[0].text);
  assert.deepEqual(items.map((i) => i.owner), [true, false]);
  assert.equal(items[0].sender_id, OWNER_ID);
  assert.equal(items[0].is_bot, false);
  assert.match(items[0].time, /^[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2}(am|pm)$/);
  assert.deepEqual(structured(await tools.call("read_topic", { topic: "work", include_self: true, since_id: b.id }), "read_topic"), { count: 1, ids: [c.id], next_since_id: c.id });
  assert.deepEqual(structured(await tools.call("read_topic", { topic: "work", since_id: c.id }), "read_topic"), { count: 0, ids: [], next_since_id: c.id });
  // The narrow uses the pinned stream id, never a name lookup.
  const narrow = JSON.parse(fake.requestsTo("GET", "messages")[0].params.narrow);
  assert.deepEqual(narrow[0], { operator: "channel", operand: STREAMS["agent-sync"] });
  assert.equal(fake.requests.filter((r) => r.path === "get_stream_id").length, 0);
});

test("a long body is cut and flagged, and every request is redirect manual on the realm", async () => {
  const { fake, tools } = harness();
  fake.addMessage("Codex", "agent-sync", "long", "x".repeat(5000));
  const item = fence((await tools.call("read_topic", { topic: "long" })).content[0].text)[0];
  assert.equal(item.body.length, 4000);
  assert.equal(item.truncated, true);
  assert.ok(fake.requests.every((r) => r.redirect === "manual"));
});

test("topics:  newest first, resolved flag, capped", async () => {
  const { fake, tools } = harness();
  fake.addMessage("Codex", "agent-sync", "old", "1");
  fake.addMessage("Codex", "agent-sync", "✔ done thing", "2");
  const newest = fake.addMessage("Codex", "agent-sync", "newest", "3");
  const result = await tools.call("topics", { limit: 2 });
  assert.deepEqual(structured(result, "topics"), { count: 2, max_ids: [newest.id, newest.id - 1] });
  assert.deepEqual(fence(result.content[0].text).map((i) => [i.name, i.resolved]), [["newest", false], ["✔ done thing", true]]);
});

// ---------------------------------------------------------------- writes

test("post:  tag, sentence gap, ids only in the answer, the pinned stream", async () => {
  const { fake, tools } = harness();
  const result = await tools.call("post", { channel: "sandbox", topic: "AFC work", text: "Ready.  Please review.", session: "abc12345" });
  const data = structured(result, "post");
  assert.deepEqual(data, { id: fake.messages.at(-1).id, channel_id: STREAMS.sandbox, duplicate: false });
  assert.equal(fake.messages.at(-1).content, "[GROK-WEB·abc12345] Ready.  Please review.");
  assert.equal(fake.requestsTo("POST", "messages")[0].params.to, String(STREAMS.sandbox));
  assert.ok(!result.content[0].text.includes("AFC work"), "writes echo no channel or topic text");
});

test("idempotency:  an explicit key and the implicit 10-minute key never post twice", async () => {
  const { fake, tools, clock } = harness();
  const one = structured(await tools.call("post", { channel: "sandbox", topic: "idem", text: "one", idempotency_key: "key-0001" }), "post");
  const again = structured(await tools.call("post", { channel: "sandbox", topic: "idem", text: "one", idempotency_key: "key-0001" }), "post");
  assert.deepEqual(again, { ...one, duplicate: true });
  const two = structured(await tools.call("post", { channel: "sandbox", topic: "idem", text: "two" }), "post");
  const twoAgain = structured(await tools.call("post", { channel: "sandbox", topic: "idem", text: "two" }), "post");
  assert.deepEqual(twoAgain, { ...two, duplicate: true });
  assert.equal(fake.requestsTo("POST", "messages").length, 2);
  clock.now += 11 * 60 * 1000;
  assert.equal(structured(await tools.call("post", { channel: "sandbox", topic: "idem", text: "two" }), "post").duplicate, false);
  assert.equal(fake.requestsTo("POST", "messages").length, 3);
});

test("outcome_unknown, then a retry with the same key reconciles from the topic and never sends twice", async () => {
  const { fake, tools, clock } = harness();
  // The POST lands, but the answer is lost.
  fake.failNext("POST", "messages", (entry) => {
    fake.addMessage(fake.me, "sandbox", entry.params.topic, entry.params.content);
    throw Object.assign(new Error("timed out"), { name: "TimeoutError" });
  });
  const error = toolError(await tools.call("post", { channel: "sandbox", topic: "flaky", text: "once only", idempotency_key: "flaky-0001" }), "outcome_unknown");
  assert.deepEqual(error.check, { channel: "sandbox", topic: "flaky", include_self: true });
  const landed = fake.messages.at(-1).id;
  const started = clock.now;
  const again = structured(await tools.call("post", { channel: "sandbox", topic: "flaky", text: "once only", idempotency_key: "flaky-0001" }), "post");
  assert.deepEqual(again, { id: landed, channel_id: STREAMS.sandbox, duplicate: true });
  assert.ok(clock.now - started >= 0);
  assert.equal(fake.requestsTo("POST", "messages").length, 1, "nothing is re-sent without the read");
  // When the first attempt did not land, the retry sends exactly once.
  fake.failNext("POST", "messages", () => {
    throw Object.assign(new Error("timed out"), { name: "TimeoutError" });
  });
  toolError(await tools.call("post", { channel: "sandbox", topic: "flaky", text: "second", idempotency_key: "flaky-0002" }), "outcome_unknown");
  const sent = structured(await tools.call("post", { channel: "sandbox", topic: "flaky", text: "second", idempotency_key: "flaky-0002" }), "post");
  assert.equal(sent.duplicate, false);
  assert.equal(fake.requestsTo("POST", "messages").length, 3);
});

test("a gateway status while sending is outcome_unknown;  a 400 is zulip_error without Zulip's msg", async () => {
  const { fake, tools } = harness();
  fake.failNext("POST", "messages", () => fake.error(504, "GATEWAY"));
  toolError(await tools.call("post", { channel: "sandbox", topic: "gw", text: "x" }), "outcome_unknown");
  fake.failNext("POST", "messages", () => fake.error(400, "BAD_REQUEST"));
  const error = toolError(await tools.call("post", { channel: "sandbox", topic: "gw", text: "y" }), "zulip_error");
  assert.equal(error.zulip_code, "BAD_REQUEST");
  assert.ok(!JSON.stringify(error).includes("secret topic"), "Zulip's msg can quote a topic and is never passed on");
});

test("429:  retried within the 20-second budget, else rate_limited with a seat-wide cooldown", async () => {
  const { fake, tools } = harness();
  fake.addMessage("Codex", "agent-sync", "busy", "hello");
  fake.failNext("GET", "messages", () => fake.json(429, { result: "error", code: "RATE_LIMIT_HIT", "retry-after": 2 }));
  structured(await tools.call("read_topic", { topic: "busy" }), "read_topic");
  fake.failNext("GET", "messages", () => fake.json(429, { result: "error", code: "RATE_LIMIT_HIT", "retry-after": 12 }));
  fake.failNext("GET", "messages", () => fake.json(429, { result: "error", code: "RATE_LIMIT_HIT", "retry-after": 12 }));
  const error = toolError(await tools.call("read_topic", { topic: "busy" }), "rate_limited");
  assert.equal(error.retry_after_s, 12);
  const before = fake.requests.length;
  const cooled = toolError(await tools.call("read_topic", { topic: "busy" }), "rate_limited");
  assert.ok(cooled.retry_after_s >= 1);
  assert.equal(fake.requests.length, before, "the cooldown refuses before any request");
});

test("reply:  the original channel and topic, and react is idempotent", async () => {
  const { fake, tools } = harness();
  const original = fake.addMessage("Codex", "agent-sync", "ask", "can you look");
  const data = structured(await tools.call("reply", { message_id: original.id, text: "Looking now." }), "reply");
  const posted = fake.messages.at(-1);
  assert.equal(data.id, posted.id);
  assert.equal(data.channel_id, STREAMS["agent-sync"]);
  assert.equal(posted.subject, "ask");
  assert.equal(posted.content, "[GROK-WEB] Looking now.");
  structured(await tools.call("react", { message_id: original.id, emoji: "eyes" }), "react");
  assert.deepEqual(structured(await tools.call("react", { message_id: original.id, emoji: "eyes" }), "react"), { id: original.id, emoji: "eyes" });
});

test("spacing:  writes 3 seconds apart, reads half a second, past 6 seconds rate_limited", async () => {
  const store = S.memoryStore();
  const t = Date.parse("2026-10-09T10:00:00Z");
  assert.deepEqual(await S.reserve(store, { kind: "write" }, t), { ok: true, waitMs: 0 });
  assert.deepEqual(await S.reserve(store, { kind: "react" }, t), { ok: true, waitMs: 3000 });
  assert.deepEqual(await S.reserve(store, { kind: "write" }, t), { ok: true, waitMs: 6000 });
  const refused = await S.reserve(store, { kind: "write" }, t);
  assert.equal(refused.code, "rate_limited");
  assert.ok(refused.retryAfterS >= 1);
  assert.deepEqual(await S.reserve(store, { kind: "read" }, t), { ok: true, waitMs: 0 });
  assert.deepEqual(await S.reserve(store, { kind: "read" }, t), { ok: true, waitMs: 500 });
});

test("budgets (D5):  20 writes an hour, 120 a day, 60 reactions and 300 reads an hour", async () => {
  const store = S.memoryStore();
  let t = Date.parse("2026-10-09T10:00:00Z");
  for (let i = 0; i < 20; i++) {
    assert.equal((await S.reserve(store, { kind: "write" }, t)).ok, true);
    t += 3000;
  }
  const out = await S.reserve(store, { kind: "write" }, t);
  assert.equal(out.code, "budget_exhausted");
  assert.equal(out.resetAt, "2026-10-09T11:00:00Z");
  assert.deepEqual(S.BUDGETS.write.map((w) => w.limit), [20, 120]);
  assert.equal(S.BUDGETS.react[0].limit, 60);
  assert.equal(S.BUDGETS.read[0].limit, 300);
  // Across a day:  six hours of 20 is 120, and the seventh hour is refused by the day window.
  const day = S.memoryStore();
  let d = Date.parse("2026-10-09T00:00:00Z");
  for (let h = 0; h < 6; h++) {
    for (let i = 0; i < 20; i++) assert.equal((await S.reserve(day, { kind: "write" }, d + i * 3000)).ok, true);
    d += S.HOUR_MS;
  }
  const dayOut = await S.reserve(day, { kind: "write" }, d);
  assert.equal(dayOut.code, "budget_exhausted");
  assert.equal(dayOut.window, "day");
});

test("a budget refusal surfaces as budget_exhausted with reset_at, and a duplicate spends no budget", async () => {
  const { tools, gate } = harness();
  const posted = structured(await tools.call("post", { channel: "sandbox", topic: "b", text: "first" }), "post");
  assert.equal(posted.duplicate, false);
  for (let i = 0; i < 25; i++) assert.equal(structured(await tools.call("post", { channel: "sandbox", topic: "b", text: "first" }), "post").duplicate, true);
  assert.equal((await gate.store.get("budget:write")).length, 1, "duplicates spend no write budget");
  for (let i = 0; i < 19; i++) structured(await tools.call("post", { channel: "sandbox", topic: "b", text: `n${i}` }), "post");
  const error = toolError(await tools.call("post", { channel: "sandbox", topic: "b", text: "one too many" }), "budget_exhausted");
  assert.match(error.reset_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
});

// ---------------------------------------------------------------- audit and leaks

test("audit:  one row per call with the spec 3.9 fields, no body, no key, a secret-looking topic hashed", async () => {
  const { fake, tools, gate, logs } = harness();
  structured(await tools.call("post", { channel: "sandbox", topic: "audited", text: "body text that must not be stored" }), "post");
  toolError(await tools.call("post", { channel: "sandbox", topic: `k ${fake.key}`, text: "x" }), "refused_secret");
  structured(await tools.call("read_topic", { topic: `k ${zulipShapedKey(9)}` }), "read_topic");
  const rows = await S.callTail(gate.store, 10);
  assert.equal(rows.length, 3);
  const [read, refused, post] = rows;
  assert.equal(post.tool, "post");
  assert.equal(post.topic, "audited");
  assert.equal(post.channel_id, STREAMS.sandbox);
  assert.equal(post.outcome, "ok");
  assert.equal(post.grant_ref, "abcdef012345");
  assert.equal(post.asn, 13335);
  assert.equal(post.country, "US");
  assert.ok(Number.isInteger(post.body_len) && Number.isInteger(post.message_id) && /^[0-9a-f]{12}$/.test(post.idem_ref));
  assert.equal(refused.error_code, "refused_secret");
  assert.match(refused.topic, /^sha256:[0-9a-f]{12}$/);
  assert.match(read.topic, /^sha256:[0-9a-f]{12}$/, "a read's topic is hashed too when it looks like a secret");
  const everything = JSON.stringify([rows, logs, await S.tail(gate.store, 50)]);
  const [, basic] = basicForms(BOT_EMAIL, fake.key);
  for (const leak of [fake.key, basic, "body text that must not be stored"]) assert.ok(!everything.includes(leak), `leaked:  ${leak.slice(0, 6)}`);
});

test("an ASN or country change within one grant is flagged", async () => {
  const store = S.memoryStore();
  const t = Date.parse("2026-10-09T10:00:00Z");
  assert.equal((await S.auditCall(store, { grant_ref: "g1", tool: "whoami", asn: 13335, country: "US" }, t)).geoChanged, false);
  assert.equal((await S.auditCall(store, { grant_ref: "g1", tool: "whoami", asn: 13335, country: "US" }, t + 1)).geoChanged, false);
  assert.equal((await S.auditCall(store, { grant_ref: "g1", tool: "whoami", asn: 4134, country: "CN" }, t + 2)).geoChanged, true);
  assert.equal((await S.tail(store, 1))[0].event, "geo_change");
});

test("call rows are kept 90 days and no longer", async () => {
  const store = S.memoryStore();
  const t = Date.parse("2026-01-01T12:00:00Z");
  await S.auditCall(store, { tool: "whoami" }, t);
  await S.auditCall(store, { tool: "topics" }, t + 89 * S.DAY_MS);
  assert.equal((await S.callTail(store, 10)).length, 2);
  await S.auditCall(store, { tool: "inbox" }, t + 92 * S.DAY_MS);
  assert.deepEqual((await S.callTail(store, 10)).map((r) => r.tool), ["inbox", "topics"]);
});

test("idempotency rows:  a pending write in flight is busy, an abandoned one reconciles", async () => {
  const store = S.memoryStore();
  const t = 1_000_000;
  assert.deepEqual(await S.idemBegin(store, { key: "k", ttlMs: 60_000, fields: { body_sha: "x" } }, t), { verdict: "send" });
  assert.equal((await S.idemBegin(store, { key: "k", ttlMs: 60_000, fields: {} }, t + 1000)).verdict, "busy");
  assert.equal((await S.idemBegin(store, { key: "k", ttlMs: 60_000, fields: {} }, t + S.PENDING_FRESH_MS + 1)).verdict, "reconcile");
  await S.idemSet(store, { key: "k", changes: { state: "sent", id: 7 } }, t + 40_000);
  assert.equal((await S.idemPeek(store, { key: "k" }, t + 40_001)).id, 7);
  assert.equal(await S.idemPeek(store, { key: "k" }, t + 60_001), null, "expired");
});
