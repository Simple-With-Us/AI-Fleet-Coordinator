// AFC-331 section 5:  the notes_* tools on the hosted server, against the fake Zulip.  The envelope format is
// shared with the Python side through scripts/agent_sync/tests/notes-fixtures.json.  No dependencies:  plain
// `node --test`.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { HostedTools, HOSTED_TOOLS_BY_NAME, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS } from "../src/hosted-tools.js";
import { ZulipClient } from "../src/zulip.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { ERROR_META_KEY, schemaProblem } from "../src/contract.js";
import {
  NOTES_TOOLS, NOTES_FIND_PAGE, NOTES_REASON_PAGE, NOTES_REASON_PAGES, MessagesResponse, readMessagesPage, readOneMessage, RELAY_CHANNEL, makeEnvelope, renderMessage, parseMessage, topicFor, checkTitle, checkFlags, needsUpload, INLINE_JSON_MAX,
} from "../src/notes.js";
import { FakeZulip, BOT_EMAIL } from "./fake-zulip.mjs";
import { ROOT, testEnv, wranglerConfig } from "./helpers.mjs";

const config = loadConfig(testEnv());
const FIXTURES = JSON.parse(readFileSync(path.join(ROOT, "../agent_sync/tests/notes-fixtures.json"), "utf8")).cases;
const SPECS = JSON.parse(readFileSync(path.join(ROOT, "../agent_sync/mcp/notes-tools.json"), "utf8")).tools;
const RELAY_ID = 700100;
const SESSION = "grokweb-session-0001"; // AFC-361:  GROK-WEB is not exempt and has no environment variable of its own

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

// The fake Zulip answers for one bot, and the key check compares its address with the seat's:  another seat in a test
// gets the bot address the real registry gives it.
const BOT_FOR = { "GROK-BOT": "grok-bot-bot", "GB-FIXER": "fixer-grok-bot", MA: "muse-assist-bot", JET: "openai-dot-bot", WISE: "instinct-owl-bot", ECHO: "instinct-bat-bot", CODEX: "codex-bot", CLAUDE: "claude-bot" };
const botEmail = (seat) => (BOT_FOR[seat] ? `${BOT_FOR[seat]}@simplewithus.zulipchat.com` : BOT_EMAIL);
const withBot = (base, seat) => (base.botEmails[seat] === botEmail(seat) ? base : { ...base, botEmails: { ...base.botEmails, [seat]: botEmail(seat) } });

function harness({ consumer, seat = "GROK-WEB" } = {}) {
  const fake = new FakeZulip(botEmail(seat) === BOT_EMAIL ? {} : { me: { email: botEmail(seat), full_name: seat } });
  fake.addStream(RELAY_CHANNEL, RELAY_ID);
  const clock = { now: Date.parse("2026-10-09T10:00:00Z") };
  const tools = new HostedTools({
    seat,
    scopes: ["zulip:read", "zulip:write"],
    clientId: "client",
    grantRef: "abcdef012345",
    paused: false,
    config: withBot(consumer !== undefined ? loadConfig(testEnv({ NOTES_RELAY_CONSUMER: consumer })) : config, seat),
    zulip: new ZulipClient({ email: botEmail(seat), key: fake.key, fetch: fake.fetch, sleep: async (ms) => (clock.now += ms), now: () => clock.now }),
    key: fake.key,
    gate: memoryGate(clock),
    now: () => clock.now,
    sleep: async (ms) => { clock.now += ms; },
    log: () => {},
    usersCache: new Map(),
    channelsCache: new Map(),
  });
  return { fake, tools, clock };
}

const TITLE = "[AFC, Grok Web] relay check";

function ok(result, tool) {
  assert.ok(!result.isError, `expected success, got ${result.content?.[0]?.text}`);
  assert.equal(schemaProblem(HOSTED_TOOLS_BY_NAME[tool].outputSchema, result.structuredContent, "structuredContent"), null);
  return result.structuredContent;
}

function toolError(result, code) {
  assert.equal(result.isError, true, JSON.stringify(result));
  const obj = JSON.parse(result.content[0].text);
  assert.deepEqual(result._meta[ERROR_META_KEY], obj);
  assert.equal(obj.code, code, JSON.stringify(obj));
  return obj;
}

// ---------------------------------------------------------------- the contract

test("the hosted notes tools are the shared notes-tools.json, classified as writes except notes_status", () => {
  assert.deepEqual(NOTES_TOOLS, SPECS);
  assert.deepEqual(NOTES_TOOLS.map((t) => t.name), ["notes_create", "notes_update", "notes_pin", "notes_unpin", "notes_status"]);
  for (const tool of NOTES_TOOLS) {
    assert.ok(HOSTED_TOOLS_BY_NAME[tool.name], tool.name);
    const write = tool.name !== "notes_status";
    assert.equal(HOSTED_WRITE_TOOLS.has(tool.name), write, tool.name);
    assert.equal(HOSTED_READ_TOOLS.has(tool.name), !write, tool.name);
    assert.equal(tool.annotations.readOnlyHint, !write, tool.name);
    assert.equal(tool.inputSchema.additionalProperties, false, tool.name);
  }
});

test("golden envelopes render to exactly the message the Python side writes and parse back to exactly the envelope", () => {
  for (const { name, envelope, message } of FIXTURES) {
    assert.equal(renderMessage(envelope), message, name);
    assert.deepEqual(parseMessage(`[GROK-WEB·abc12345] ${message}`), envelope, name);
    assert.deepEqual(parseMessage(message), envelope, name);
  }
});

test("titles, flags and envelopes are refused the way notes.py refuses them", () => {
  assert.throws(() => checkTitle(""), /must not be empty/);
  assert.throws(() => checkTitle("no bracket"), /must start with/);
  assert.throws(() => checkTitle('[AFC, Claude] a "quote"'), /double quote/);
  assert.throws(() => checkTitle("[AFC, Claude] back\\slash"), /backslash/);
  assert.throws(() => checkTitle("[AFC, Claude] line\nbreak"), /control character/);
  assert.throws(() => checkFlags({ activate: true }), /unknown flag 'activate'/);
  assert.throws(() => checkFlags({ summary: 'say "hi"' }), /summary/);
  assert.throws(() => checkFlags({ pr: "46; rm" }), /pull request/);
  assert.equal([...topicFor(`[AFC, Claude] ${"x".repeat(80)}`)].length, 60);
  assert.equal(parseMessage("no fence here"), null);
  assert.equal(parseMessage('```json\n{"v":2}\n```'), null);
  assert.equal(parseMessage(`${"```json\n"}{"v":1,"action":"create","title":"[AFC, Claude] t","markdown":"","flags":{"activate":true},"request_id":"abcdefgh12","identity":"CLAUDE"}\n${"```"}`), null);
});

// ---------------------------------------------------------------- notes_create and friends

test("notes_create posts the envelope to #notes-relay as the identity, topic = the title, and returns queued", async () => {
  const { fake, tools } = harness();
  const out = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "# Hello\n\nBody.  Two sentences.", request_id: "relayrequest-01" }), "notes_create");
  assert.equal(out.status, "queued");
  assert.equal(out.transport, "relay");
  assert.equal(out.request_id, "relayrequest-01");
  const posted = fake.messages.find((m) => m.id === out.message_id);
  assert.equal(posted.display_recipient, RELAY_CHANNEL);
  assert.equal(posted.subject, TITLE);
  assert.equal(posted.sender_email, BOT_EMAIL);
  assert.match(posted.content, /^\[GROK-WEB[^\]]*\] ```json\n/);
  const envelope = parseMessage(posted.content);
  assert.deepEqual({ ...envelope, markdown: undefined }, { v: 1, action: "create", title: TITLE, markdown: undefined, flags: { pin: true }, request_id: "relayrequest-01", identity: "GROK-WEB", session: SESSION });
  assert.ok(envelope.markdown.startsWith("# Hello"));
});

test("the same request_id twice posts once;  a long title is cut to the 60-character topic and stays whole in the envelope", async () => {
  const { fake, tools } = harness();
  const title = `[AFC, Grok Web] ${"long ".repeat(30)}end`;
  const first = ok(await tools.call("notes_update", { session: SESSION, title, markdown: "x", request_id: "dedupe-request-1" }), "notes_update");
  const again = ok(await tools.call("notes_update", { session: SESSION, title, markdown: "x", request_id: "dedupe-request-1" }), "notes_update");
  assert.equal(again.message_id, first.message_id);
  assert.equal(again.duplicate, true);
  const posted = fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL);
  assert.equal(posted.length, 1);
  assert.equal([...posted[0].subject].length, 60);
  assert.equal(parseMessage(posted[0].content).title, title.trim());
});

test("pin and unpin carry no body;  flags survive;  refused titles send nothing", async () => {
  const { fake, tools } = harness();
  ok(await tools.call("notes_pin", { title: TITLE, request_id: "pin-request-001" }), "notes_pin");
  ok(await tools.call("notes_unpin", { title: TITLE, request_id: "unpin-request-01" }), "notes_unpin");
  ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", pin: false, notify: true, summary: "short", pr: "46", request_id: "flags-request-01" }), "notes_create");
  const [pin, unpin, create] = fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL).map((m) => parseMessage(m.content));
  assert.deepEqual([pin.action, pin.markdown, pin.flags], ["pin", "", {}]);
  assert.deepEqual([unpin.action, unpin.markdown], ["unpin", ""]);
  assert.deepEqual(create.flags, { pin: false, notify: true, summary: "short", pr: "46" });
  const before = fake.messages.length;
  toolError(await tools.call("notes_create", { session: SESSION, title: "", markdown: "b" }), "invalid_argument");
  toolError(await tools.call("notes_create", { session: SESSION, title: "no bracket", markdown: "b" }), "invalid_argument");
  toolError(await tools.call("notes_create", { session: SESSION, title: '[AFC, X] "quoted"', markdown: "b" }), "invalid_argument");
  toolError(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", activate: true }), "invalid_argument");
  assert.equal(fake.messages.length, before);
});

test("a body over the inline limit goes up as an attached .md file the envelope names, with its digest", async () => {
  const { fake, tools } = harness();
  const markdown = "line of the note.  ".repeat(700);
  assert.ok(needsUpload(makeEnvelope("create", TITLE, markdown, {}, "bigrequest-0001", "GROK-WEB")));
  assert.ok(markdown.length > INLINE_JSON_MAX);
  const out = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown, request_id: "bigrequest-0001" }), "notes_create");
  assert.equal(fake.uploads.length, 1);
  assert.equal(fake.uploads[0].name, "note-bigrequest-0001.md");
  const posted = fake.messages.find((m) => m.id === out.message_id);
  const envelope = parseMessage(posted.content);
  assert.equal(envelope.markdown, "");
  assert.match(envelope.markdown_url, /^\/user_uploads\/2\/ab\/1\/note-bigrequest-0001\.md$/);
  assert.equal(envelope.markdown_bytes, new TextEncoder().encode(markdown).length);
  const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", fake.uploads[0].bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
  assert.equal(envelope.markdown_sha256, digest);
  assert.ok(posted.content.length < 9000);
});

test("a body that looks like a private key is refused before anything is posted", async () => {
  const { fake, tools } = harness();
  const marker = `-----BEGIN ${"RSA "}PRIVATE KEY-----`;
  toolError(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: `leak ${marker}` }), "refused_secret");
  assert.equal(fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL).length, 0);
});

test("the summary flag is scanned for secrets like the title and the body", async () => {
  const { fake, tools } = harness();
  const leak = `token ${"ghp_"}${"EXAMPLE0".repeat(5)}`;
  toolError(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", summary: leak }), "refused_secret");
  assert.equal(fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL).length, 0);
});

// ---------------------------------------------------------------- AFC-361:  the session id

const relayPosts = (fake) => fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL);

test("a non-exempt hosted seat is refused without a session, with the text that says how to pass it, and nothing is posted", async () => {
  const { fake, tools } = harness();
  for (const tool of ["notes_create", "notes_update"]) {
    const obj = toolError(await tools.call(tool, { title: TITLE, markdown: "the secret-free body text" }), "session_required");
    assert.match(obj.message, /session id is required/);
    assert.match(obj.message, /`session` argument/);
    assert.doesNotMatch(obj.message, /body text/, "the note body is never echoed");
  }
  assert.equal(relayPosts(fake).length, 0);
  // a CLAUDE grant names its own platform variable
  const claude = harness({ seat: "CLAUDE" });
  const obj = toolError(await claude.tools.call("notes_create", { title: "[AFC, Claude] t", markdown: "b" }), "session_required");
  assert.match(obj.message, /CLAUDE_CODE_SESSION_ID/);
});

test("an invalid session id is refused for everyone, exempt seats included", async () => {
  for (const seat of ["GROK-WEB", "GB-FIXER"]) {
    const { fake, tools } = harness({ seat });
    const title = seat === "GB-FIXER" ? "[AFC, GB-FIXER] t" : TITLE;
    for (const session of ["abc", "-leading-dash", "has space inside", "x".repeat(129), "bad/char"]) {
      const result = await tools.call("notes_create", { title, markdown: "b", session });
      assert.equal(result.isError, true, `${seat} ${session}`);
      assert.match(result.content[0].text, /invalid_argument|session/);
    }
    assert.equal(relayPosts(fake).length, 0);
  }
});

test("exempt single-thread identities write without a session, and an id they do send is still recorded", async () => {
  for (const seat of ["GROK-BOT", "GB-FIXER", "MA", "JET", "WISE", "ECHO"]) {
    const { fake, tools } = harness({ seat });
    const title = `[AFC, ${seat}] exempt`;
    for (const tool of ["notes_create", "notes_update"]) {
      const request_id = `exempt-${tool}-${seat.toLowerCase()}`.slice(0, 60);
      const out = ok(await tools.call(tool, { title, markdown: "b", request_id }), tool);
      const envelope = parseMessage(fake.messages.find((m) => m.id === out.message_id).content);
      assert.equal(envelope.identity, seat);
      assert.equal("session" in envelope, false, `${seat} ${tool}`);
    }
    const out = ok(await tools.call("notes_create", { title, markdown: "b", session: "optional-id-0001", request_id: `with-id-${seat.toLowerCase()}` }), "notes_create");
    assert.equal(parseMessage(fake.messages.find((m) => m.id === out.message_id).content).session, "optional-id-0001");
  }
});

test("the exemption comes from the resolved seat, never from an argument:  no argument makes a non-exempt seat exempt", async () => {
  assert.equal(NOTES_TOOLS.find((t) => t.name === "notes_create").inputSchema.additionalProperties, false);
  const { fake, tools } = harness({ seat: "CODEX" });
  for (const extra of [{ exempt: true }, { identity: "GB-FIXER" }, { seat: "MA" }, { single_thread: true }]) {
    const result = await tools.call("notes_create", { title: "[AFC, Codex] t", markdown: "b", ...extra });
    assert.equal(result.isError, true, JSON.stringify(extra));
  }
  toolError(await tools.call("notes_create", { title: "[AFC, Codex] t", markdown: "b" }), "session_required");
  assert.equal(relayPosts(fake).length, 0);
});

test("pin and unpin need no session, and a session in the envelope round-trips through render and parse", async () => {
  const { fake, tools } = harness();
  ok(await tools.call("notes_pin", { title: TITLE, request_id: "pin-nosession-01" }), "notes_pin");
  ok(await tools.call("notes_unpin", { title: TITLE, request_id: "unpin-nosession-1" }), "notes_unpin");
  for (const m of relayPosts(fake)) assert.equal("session" in parseMessage(m.content), false);
  const envelope = makeEnvelope("create", TITLE, "body", {}, "roundtrip-0001", "GROK-WEB", "thread:abc.123_x-9");
  assert.equal(envelope.session, "thread:abc.123_x-9");
  assert.deepEqual(parseMessage(renderMessage(envelope)), envelope);
  assert.throws(() => makeEnvelope("create", TITLE, "b", {}, "roundtrip-0002", "GROK-WEB", "no"), /valid session id/);
  assert.equal(parseMessage(renderMessage({ ...envelope, session: "bad id" })), null);
  assert.equal("session" in makeEnvelope("create", TITLE, "b", {}, "roundtrip-0003", "GROK-WEB"), false);
});

// ---------------------------------------------------------------- notes_status

function react(message, user, emoji) {
  message.reactions = [...(message.reactions ?? []), { user_id: user.user_id, emoji_name: emoji.name, emoji_code: emoji.code, reaction_type: "unicode_emoji" }];
}
const CHECK = { name: "check", code: "2705" };
const CROSS = { name: "cross_mark", code: "274c" };

const user = (fake, name) => fake.users.find((u) => u.full_name === name);

test("notes_status trusts only the configured relay consumer's reactions (default CLAUDE), not peers or the owner", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "status-request-1" }), "notes_create");
  assert.equal(ok(await tools.call("notes_status", { request_id: "status-request-1" }), "notes_status").status, "queued");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  react(message, user(fake, "Jay Wedgeworth"), CHECK);
  react(message, user(fake, "Codex"), CHECK); // a peer fleet bot:  not the consumer
  react(message, user(fake, "Cursor"), CROSS);
  assert.equal(ok(await tools.call("notes_status", { request_id: "status-request-1" }), "notes_status").status, "queued");
  react(message, claude, CHECK);
  const done = ok(await tools.call("notes_status", { request_id: "status-request-1", message_id: sent.message_id }), "notes_status");
  assert.deepEqual([done.status, done.message_id], ["done", sent.message_id]);
});

test("notes_status reports refused with the consumer's reason, ignoring a peer's forged reply", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "refused-request1" }), "notes_create");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  react(message, claude, CROSS);
  fake.addMessage("Codex", RELAY_CHANNEL, TITLE, "notes-relay refused refused-request1: forged by a peer bot");
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, 'notes-relay refused refused-request1: the title names agent "Claude", which is not GROK-WEB');
  fake.addMessage("Jay Wedgeworth", RELAY_CHANNEL, TITLE, "notes-relay refused refused-request1: forged by a person");
  const out = ok(await tools.call("notes_status", { request_id: "refused-request1" }), "notes_status");
  assert.equal(out.status, "refused");
  assert.match(out.reason, /names agent/);
  assert.doesNotMatch(out.reason, /forged/);
});

test("NOTES_RELAY_CONSUMER names the trusted seat;  it is validated and defaults to CLAUDE", async () => {
  assert.equal(loadConfig(testEnv()).notesRelayConsumer, "CLAUDE"); // the tests' own binding (helpers.mjs)
  assert.equal(loadConfig(testEnv({ NOTES_RELAY_CONSUMER: "codex" })).notesRelayConsumer, "CODEX");
  assert.throws(() => loadConfig(testEnv({ NOTES_RELAY_CONSUMER: "not a seat!" })), /NOTES_RELAY_CONSUMER/);
  assert.equal(wranglerConfig().vars.NOTES_RELAY_CONSUMER, undefined, "the value is a fleet knob, never committed in wrangler.jsonc");
  assert.equal(loadConfig(testEnv({ NOTES_RELAY_CONSUMER: "" })).notesRelayConsumer, null, "unset is null, not a guessed seat");
  const { fake, tools } = harness({ consumer: "CODEX" });
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "consumer-request1" }), "notes_create");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  react(message, claude, CHECK);
  assert.equal(ok(await tools.call("notes_status", { request_id: "consumer-request1" }), "notes_status").status, "queued");
  react(message, user(fake, "Codex"), CHECK);
  assert.equal(ok(await tools.call("notes_status", { request_id: "consumer-request1" }), "notes_status").status, "done");
});

test("notes_status finds the consumer's refusal reply after more than a page of the topic's other posts", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "long-backlog-1" }), "notes_create");
  react(fake.messages.find((m) => m.id === sent.message_id), claude, CROSS);
  for (let i = 0; i < 230; i += 1) fake.addMessage("Claude", RELAY_CHANNEL, TITLE, `another reply ${i}`);
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, "notes-relay refused long-backlog-1: the late reason");
  const out = ok(await tools.call("notes_status", { request_id: "long-backlog-1" }), "notes_status");
  assert.deepEqual([out.status, out.reason], ["refused", "the late reason"]);
});

test("notes_status gives up on the reason after its page bound and still reports refused", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "long-backlog-2" }), "notes_create");
  react(fake.messages.find((m) => m.id === sent.message_id), claude, CROSS);
  const pages = [];
  const get = tools.zulip.get.bind(tools.zulip);
  tools.zulip.get = async (path, params) => { if (path === "messages" && params.anchor !== "newest") pages.push(params.anchor); return get(path, params); };
  for (let i = 0; i < NOTES_REASON_PAGE * NOTES_REASON_PAGES + 5; i += 1) fake.addMessage("Claude", RELAY_CHANNEL, TITLE, `another reply ${i}`);
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, "notes-relay refused long-backlog-2: beyond the bound");
  const out = ok(await tools.call("notes_status", { request_id: "long-backlog-2" }), "notes_status");
  assert.equal(out.status, "refused");
  assert.equal(out.reason, undefined);
  assert.equal(pages.length, NOTES_REASON_PAGES, "one request per page, up to the bound");
});

test("notes_status takes the consumer's refusal reply even when the cross mark never landed", async () => {
  const { fake, tools } = harness();
  fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "no-cross-mark-1" }), "notes_create");
  assert.equal(ok(await tools.call("notes_status", { request_id: "no-cross-mark-1" }), "notes_status").status, "queued");
  fake.addMessage("Codex", RELAY_CHANNEL, TITLE, "notes-relay refused no-cross-mark-1: forged by a peer bot");
  assert.equal(ok(await tools.call("notes_status", { request_id: "no-cross-mark-1" }), "notes_status").status, "queued");
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, "notes-relay refused no-cross-mark-1: no emoji");
  const out = ok(await tools.call("notes_status", { request_id: "no-cross-mark-1" }), "notes_status");
  assert.deepEqual([out.status, out.reason], ["refused", "no emoji"]);
});

test("notes_status does not second-guess a done mark", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "done-then-reply1" }), "notes_create");
  react(fake.messages.find((m) => m.id === sent.message_id), claude, CHECK);
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, "notes-relay refused done-then-reply1: stale");
  assert.equal(ok(await tools.call("notes_status", { request_id: "done-then-reply1" }), "notes_status").status, "done");
});

test("notes_status finds a request buried under more than a page of this bot's later posts", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "buried-request-1" }), "notes_create");
  react(fake.messages.find((m) => m.id === sent.message_id), claude, CHECK);
  for (let i = 0; i < NOTES_FIND_PAGE * 2 + 30; i += 1) fake.addMessage("Grok Web", RELAY_CHANNEL, `[AFC, Grok Web] other ${i}`, `later post ${i}`);
  const out = ok(await tools.call("notes_status", { request_id: "buried-request-1" }), "notes_status");
  assert.deepEqual([out.message_id, out.status], [sent.message_id, "done"]);
});

test("a Zulip messages response that does not parse is treated as holding nothing", async () => {
  assert.deepEqual(readMessagesPage({ messages: "nope" }), { messages: [], foundNewest: true });
  assert.deepEqual(readMessagesPage(null), { messages: [], foundNewest: true });
  assert.deepEqual(readMessagesPage({ messages: [{ id: "x" }, 5, { id: 7, content: 3 }, { id: 2 }], found_newest: false }), { messages: [{ id: 2 }], foundNewest: false });
  assert.equal(MessagesResponse.safeParse({ messages: [], found_newest: "yes" }).success, false);
  assert.equal(readOneMessage({ message: 5 }), null);
  assert.equal(readOneMessage(undefined), null);
  assert.equal(readOneMessage({ message: { id: 4 } }).id, 4);
  const { fake, tools } = harness();
  fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "garbled-reply-1" }), "notes_create");
  const get = tools.zulip.get.bind(tools.zulip);
  tools.zulip.get = async (path, params) => (path === "messages" && params.anchor === "newest" ? { messages: "garbage" } : get(path, params));
  toolError(await tools.call("notes_status", { request_id: "garbled-reply-1" }), "not_found");
});

test("notes_status does not take the reply for a longer request id", async () => {
  const { fake, tools } = harness();
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "exact-status-1" }), "notes_create");
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, "notes-relay refused exact-status-12: not mine");
  assert.equal(ok(await tools.call("notes_status", { request_id: "exact-status-1" }), "notes_status").status, "queued");
  react(fake.messages.find((m) => m.id === sent.message_id), claude, CROSS);
  const refused = ok(await tools.call("notes_status", { request_id: "exact-status-1" }), "notes_status");
  assert.deepEqual([refused.status, refused.reason], ["refused", undefined]);
  fake.addMessage("Claude", RELAY_CHANNEL, TITLE, "notes-relay refused exact-status-1: its own");
  assert.equal(ok(await tools.call("notes_status", { request_id: "exact-status-1" }), "notes_status").reason, "its own");
});

test("notes_status for an unknown request id is not_found", async () => {
  const { tools } = harness();
  toolError(await tools.call("notes_status", { request_id: "nobody-asked-01" }), "not_found");
});

// ---------------------------------------------------------------- zod at the boundary

test("tool arguments, relay envelopes and reaction objects are parsed by strict zod schemas", async () => {
  const { tools } = harness();
  // The arguments reach the method un-schema'd here (HostedTools.call checks the JSON schema first):  the zod gate still holds.
  const audit = {};
  await assert.rejects(tools.notesWrite("create", { title: TITLE, markdown: 5 }, audit), (e) => e.code === "invalid_argument" && /argument 'markdown' must be of type string/.test(e.message));
  await assert.rejects(tools.notesWrite("create", { title: TITLE, extra: true }, audit), (e) => /unknown argument 'extra'/.test(e.message));
  await assert.rejects(tools.notesWrite("pin", { title: TITLE, markdown: "a pin takes no body" }, audit), (e) => /unknown argument 'markdown'/.test(e.message));
  await assert.rejects(tools.tool_notes_status({ request_id: "abcdefgh12", message_id: "7" }, audit), (e) => e.code === "invalid_argument");
  await assert.rejects(tools.tool_notes_status({ request_id: 5 }, audit), (e) => e.code === "invalid_argument");
  const good = '{"v":1,"action":"create","title":"[AFC, Claude] t","markdown":"","flags":{},"request_id":"abcdefgh12","identity":"CLAUDE"';
  assert.ok(parseMessage(`${"```json\n"}${good}}\n${"```"}`));
  for (const tampered of [`${good},"markdown_url":"/user_uploads/2/a/b.md","markdown_bytes":"3","markdown_sha256":"${"a".repeat(64)}"}`, `${good},"markdown_bytes":3}`, `${good.replace('"markdown":""', '"markdown":7')}}`, `${good.replace('"v":1', '"v":"1"')}}`]) {
    assert.equal(parseMessage(`${"```json\n"}${tampered}\n${"```"}`), null, tampered);
  }
});

test("notes_status skips malformed reactions and messages instead of reading them", async () => {
  const { fake, tools } = harness();
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "junk-request-001" }), "notes_create");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  const codex = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true }); // the consumer
  message.reactions = [null, 5, "x", { user_id: "nope", emoji_name: "check" }, { user: { id: "x" } }, { user_id: codex.user_id, emoji_name: 7 }];
  assert.equal(ok(await tools.call("notes_status", { request_id: "junk-request-001" }), "notes_status").status, "queued");
  message.reactions = [...message.reactions, { user_id: codex.user_id, emoji_name: "check", emoji_code: "2705", reaction_type: "unicode_emoji" }];
  assert.equal(ok(await tools.call("notes_status", { request_id: "junk-request-001" }), "notes_status").status, "done");
});

test("notes_status answers not_configured, and trusts nobody, when the consumer binding is not set", async () => {
  const { fake, tools } = harness({ consumer: "" });
  const claude = fake.addUser("claude-bot@simplewithus.zulipchat.com", "Claude", { isBot: true });
  const sent = ok(await tools.call("notes_create", { session: SESSION, title: TITLE, markdown: "b", request_id: "unset-request-01" }), "notes_create");
  react(fake.messages.find((m) => m.id === sent.message_id), claude, CHECK);
  const error = toolError(await tools.call("notes_status", { request_id: "unset-request-01" }), "not_configured");
  assert.match(error.message, /NOTES_RELAY_CONSUMER/);
});
