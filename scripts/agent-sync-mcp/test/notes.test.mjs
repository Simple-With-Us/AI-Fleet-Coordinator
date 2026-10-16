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
  NOTES_TOOLS, RELAY_CHANNEL, makeEnvelope, renderMessage, parseMessage, topicFor, checkTitle, checkFlags, needsUpload, INLINE_JSON_MAX,
} from "../src/notes.js";
import { FakeZulip, BOT_EMAIL } from "./fake-zulip.mjs";
import { ROOT, testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());
const FIXTURES = JSON.parse(readFileSync(path.join(ROOT, "../agent_sync/tests/notes-fixtures.json"), "utf8")).cases;
const SPECS = JSON.parse(readFileSync(path.join(ROOT, "../agent_sync/mcp/notes-tools.json"), "utf8")).tools;
const RELAY_ID = 700100;

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

function harness() {
  const fake = new FakeZulip();
  fake.addStream(RELAY_CHANNEL, RELAY_ID);
  const clock = { now: Date.parse("2026-10-09T10:00:00Z") };
  const tools = new HostedTools({
    seat: "GROK-WEB",
    scopes: ["zulip:read", "zulip:write"],
    clientId: "client",
    grantRef: "abcdef012345",
    paused: false,
    config,
    zulip: new ZulipClient({ email: BOT_EMAIL, key: fake.key, fetch: fake.fetch, sleep: async (ms) => (clock.now += ms), now: () => clock.now }),
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
  const out = ok(await tools.call("notes_create", { title: TITLE, markdown: "# Hello\n\nBody.  Two sentences.", request_id: "relayrequest-01" }), "notes_create");
  assert.equal(out.status, "queued");
  assert.equal(out.transport, "relay");
  assert.equal(out.request_id, "relayrequest-01");
  const posted = fake.messages.find((m) => m.id === out.message_id);
  assert.equal(posted.display_recipient, RELAY_CHANNEL);
  assert.equal(posted.subject, TITLE);
  assert.equal(posted.sender_email, BOT_EMAIL);
  assert.match(posted.content, /^\[GROK-WEB[^\]]*\] ```json\n/);
  const envelope = parseMessage(posted.content);
  assert.deepEqual({ ...envelope, markdown: undefined }, { v: 1, action: "create", title: TITLE, markdown: undefined, flags: { pin: true }, request_id: "relayrequest-01", identity: "GROK-WEB" });
  assert.ok(envelope.markdown.startsWith("# Hello"));
});

test("the same request_id twice posts once;  a long title is cut to the 60-character topic and stays whole in the envelope", async () => {
  const { fake, tools } = harness();
  const title = `[AFC, Grok Web] ${"long ".repeat(30)}end`;
  const first = ok(await tools.call("notes_update", { title, markdown: "x", request_id: "dedupe-request-1" }), "notes_update");
  const again = ok(await tools.call("notes_update", { title, markdown: "x", request_id: "dedupe-request-1" }), "notes_update");
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
  ok(await tools.call("notes_create", { title: TITLE, markdown: "b", pin: false, notify: true, summary: "short", pr: "46", request_id: "flags-request-01" }), "notes_create");
  const [pin, unpin, create] = fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL).map((m) => parseMessage(m.content));
  assert.deepEqual([pin.action, pin.markdown, pin.flags], ["pin", "", {}]);
  assert.deepEqual([unpin.action, unpin.markdown], ["unpin", ""]);
  assert.deepEqual(create.flags, { pin: false, notify: true, summary: "short", pr: "46" });
  const before = fake.messages.length;
  toolError(await tools.call("notes_create", { title: "", markdown: "b" }), "invalid_argument");
  toolError(await tools.call("notes_create", { title: "no bracket", markdown: "b" }), "invalid_argument");
  toolError(await tools.call("notes_create", { title: '[AFC, X] "quoted"', markdown: "b" }), "invalid_argument");
  toolError(await tools.call("notes_create", { title: TITLE, markdown: "b", activate: true }), "invalid_argument");
  assert.equal(fake.messages.length, before);
});

test("a body over the inline limit goes up as an attached .md file the envelope names, with its digest", async () => {
  const { fake, tools } = harness();
  const markdown = "line of the note.  ".repeat(700);
  assert.ok(needsUpload(makeEnvelope("create", TITLE, markdown, {}, "bigrequest-0001", "GROK-WEB")));
  assert.ok(markdown.length > INLINE_JSON_MAX);
  const out = ok(await tools.call("notes_create", { title: TITLE, markdown, request_id: "bigrequest-0001" }), "notes_create");
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
  toolError(await tools.call("notes_create", { title: TITLE, markdown: `leak ${marker}` }), "refused_secret");
  assert.equal(fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL).length, 0);
});

test("the summary flag is scanned for secrets like the title and the body", async () => {
  const { fake, tools } = harness();
  const leak = `token ${"ghp_"}${"EXAMPLE0".repeat(5)}`;
  toolError(await tools.call("notes_create", { title: TITLE, markdown: "b", summary: leak }), "refused_secret");
  assert.equal(fake.messages.filter((m) => m.display_recipient === RELAY_CHANNEL).length, 0);
});

// ---------------------------------------------------------------- notes_status

function react(message, user, emoji) {
  message.reactions = [...(message.reactions ?? []), { user_id: user.user_id, emoji_name: emoji.name, emoji_code: emoji.code, reaction_type: "unicode_emoji" }];
}
const CHECK = { name: "check", code: "2705" };
const CROSS = { name: "cross_mark", code: "274c" };

test("notes_status reads queued, then done from a fleet bot's check mark, and ignores the owner's", async () => {
  const { fake, tools } = harness();
  const sent = ok(await tools.call("notes_create", { title: TITLE, markdown: "b", request_id: "status-request-1" }), "notes_create");
  assert.equal(ok(await tools.call("notes_status", { request_id: "status-request-1" }), "notes_status").status, "queued");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  react(message, fake.users.find((u) => u.full_name === "Jay Wedgeworth"), CHECK);
  assert.equal(ok(await tools.call("notes_status", { request_id: "status-request-1" }), "notes_status").status, "queued");
  react(message, fake.users.find((u) => u.full_name === "Codex"), CHECK);
  const done = ok(await tools.call("notes_status", { request_id: "status-request-1", message_id: sent.message_id }), "notes_status");
  assert.deepEqual([done.status, done.message_id], ["done", sent.message_id]);
});

test("notes_status reports refused with the listener's reason from its reply in the topic", async () => {
  const { fake, tools } = harness();
  const sent = ok(await tools.call("notes_create", { title: TITLE, markdown: "b", request_id: "refused-request1" }), "notes_create");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  react(message, fake.users.find((u) => u.full_name === "Codex"), CROSS);
  fake.addMessage("Codex", RELAY_CHANNEL, TITLE, 'notes-relay refused refused-request1: the title names agent "Claude", which is not GROK-WEB');
  fake.addMessage("Jay Wedgeworth", RELAY_CHANNEL, TITLE, "notes-relay refused refused-request1: forged by a person");
  const out = ok(await tools.call("notes_status", { request_id: "refused-request1" }), "notes_status");
  assert.equal(out.status, "refused");
  assert.match(out.reason, /names agent/);
  assert.doesNotMatch(out.reason, /forged/);
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
  const sent = ok(await tools.call("notes_create", { title: TITLE, markdown: "b", request_id: "junk-request-001" }), "notes_create");
  const message = fake.messages.find((m) => m.id === sent.message_id);
  const codex = fake.users.find((u) => u.full_name === "Codex");
  message.reactions = [null, 5, "x", { user_id: "nope", emoji_name: "check" }, { user: { id: "x" } }, { user_id: codex.user_id, emoji_name: 7 }];
  assert.equal(ok(await tools.call("notes_status", { request_id: "junk-request-001" }), "notes_status").status, "queued");
  message.reactions = [...message.reactions, { user_id: codex.user_id, emoji_name: "check", emoji_code: "2705", reaction_type: "unicode_emoji" }];
  assert.equal(ok(await tools.call("notes_status", { request_id: "junk-request-001" }), "notes_status").status, "done");
});
