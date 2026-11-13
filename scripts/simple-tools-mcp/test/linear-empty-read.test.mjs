// AFC-575: a Linear read never succeeds as {}.  GraphQL errors with data:null,
// a partial payload, and a result over max_chars each come back as data or as
// a typed error.  selection is a field list, or a typed refusal.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools, HOSTED_TOOLS_BY_NAME } from "../src/hosted-tools.js";
import { ZulipClient } from "../src/zulip.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { ERROR_META_KEY } from "../src/contract.js";
import { textFirst } from "../src/delegated-identities.js";
import { FakeZulip, BOT_EMAIL } from "./fake-zulip.mjs";
import { FakeLinear, opOf } from "./fake-linear.mjs";
import { testEnv } from "./helpers.mjs";
import { memoryQuota } from "./quota-helper.mjs";
import { LINEAR_MARKER_BEGIN, LinearClient } from "../src/linear.js";

const config = loadConfig(testEnv());
const CLIENT_ID = "lin_fake_client_id";
const CLIENT_SECRET = "fake-secret-value";
const PROJECT = "44412252-b36b-4705-90f5-7e3f7a17b77c";
const FILTER = { project: { id: { eq: PROJECT } } };
const QUERY = '{ viewer { name } projects(filter: { name: { contains: "Native macOS" } }, first: 3) { nodes { id name } } }';

function memoryGate(clock) {
  const store = S.memoryStore();
  const g = { store };
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) {
    g[name] = (options) => S[name](store, options, clock.now);
  }
  g.roleGet = () => S.roleGet(store, clock.now);
  g.callTail = (n) => S.callTail(store, n);
  g.linearTokenGet = () => S.linearTokenGet(store);
  g.linearTokenSet = (entry) => S.linearTokenSet(store, entry);
  g.linearTokenClear = () => S.linearTokenClear(store);
  return g;
}

function harness() {
  const fake = new FakeLinear();
  const zulip = new FakeZulip();
  const clock = { now: Date.parse("2026-10-09T10:00:00Z") };
  const gate = memoryGate(clock);
  const client = new LinearClient({
    quota: memoryQuota(), seat: "GROK-WEB", clientId: CLIENT_ID, clientSecret: CLIENT_SECRET,
    gate, fetch: fake.fetch, now: () => clock.now,
  });
  const tools = new HostedTools({
    seat: "GROK-WEB",
    scopes: ["zulip:read", "zulip:write"],
    clientId: "https://grok.com/oauth/mcp-client.json",
    grantRef: "abcdef012345",
    paused: false,
    config,
    zulip: new ZulipClient({ email: BOT_EMAIL, key: zulip.key, fetch: zulip.fetch, sleep: async (ms) => (clock.now += ms), now: () => clock.now }),
    key: zulip.key,
    gate,
    cf: { asn: 13335, country: "US" },
    linear: client,
    linearWrites: true,
    now: () => clock.now,
    sleep: async (ms) => { clock.now += ms; },
    log: () => {},
    usersCache: new Map(),
    channelsCache: new Map(),
    linearWorkspaceCache: new Map(),
  });
  return { fake, tools };
}

function scripted(fake, body) {
  fake.failNext(
    (entry) => entry.kind === "graphql" && opOf(entry.body?.query)?.name !== "BoardWhoami",
    () => fake.json(200, body),
  );
}

function shown(result, name) {
  const client = textFirst(result, HOSTED_TOOLS_BY_NAME[name]);
  assert.notDeepEqual(client, {}, "the client-facing result is not {}");
  assert.notEqual(JSON.stringify(client), "{}");
  return client;
}

function toolError(result, name, code) {
  const client = shown(result, name);
  assert.equal(client.isError, true, JSON.stringify(client));
  assert.equal(client.structuredContent, undefined);
  const obj = JSON.parse(client.content[0].text);
  assert.equal(obj.code, code, JSON.stringify(obj));
  assert.deepEqual(client._meta[ERROR_META_KEY], obj);
  assert.notEqual(client.content[0].text.trim(), "{}");
  return obj;
}

function payload(result, name) {
  const client = shown(result, name);
  assert.notEqual(client.isError, true, client.content?.[0]?.text);
  assert.equal(client.structuredContent, undefined);
  const lines = client.content[0].text.split("\n");
  const at = lines.findIndex((line) => line.startsWith(LINEAR_MARKER_BEGIN));
  assert.ok(at > 0, client.content[0].text);
  return { client, data: JSON.parse(lines[at + 1]), text: client.content[0].text };
}

function namedQuery(fake) {
  return fake.calls().find((call) => opOf(call.body?.query)?.name === "SimpleToolsNamed");
}

function flat(query) {
  return String(query).replace(/\s+/g, " ");
}

test("linear_list_issues with a selection and GraphQL data:null is a typed error, not {}", async () => {
  const { fake, tools } = harness();
  scripted(fake, {
    data: null,
    errors: [{ message: "Cannot query field", path: ["issues"], extensions: { code: "GRAPHQL_VALIDATION_FAILED" } }],
  });
  const error = toolError(
    await tools.call("linear_list_issues", { filter: FILTER, first: 40, selection: "nodes { id title }" }),
    "linear_list_issues",
    "invalid_argument",
  );
  assert.equal(error.linear_code, "GRAPHQL_VALIDATION_FAILED");
  assert.match(error.message, /Linear refused the request/);
  assert.match(flat(namedQuery(fake).body.query), /nodes \{ id title \}/);
});

test("linear_list_issues without a selection returns partial data instead of {}", async () => {
  const { fake, tools } = harness();
  scripted(fake, {
    data: { issues: { nodes: [{ id: "issue-1", title: "Native" }] } },
    errors: [{ message: "assignee failed", path: ["issues", "nodes", 0, "assignee"], extensions: { code: "INTERNAL" } }],
  });
  const { data, text } = payload(
    await tools.call("linear_list_issues", { filter: FILTER, first: 15, max_chars: 60000 }),
    "linear_list_issues",
  );
  assert.equal(data.issues.nodes[0].id, "issue-1");
  assert.match(text, /Partial Linear data/);
  assert.match(text, /INTERNAL at issues\.nodes\.0\.assignee/);
  assert.doesNotMatch(text, /assignee failed/);
  assert.match(flat(namedQuery(fake).body.query), /assignee \{ id name \}/);
});

test("linear_query over max_chars is response_too_large, not {}", async () => {
  const { fake, tools } = harness();
  scripted(fake, {
    data: { viewer: { name: "Ada" }, projects: { nodes: [{ id: "p", name: "n".repeat(60000) }] } },
  });
  const error = toolError(await tools.call("linear_query", { query: QUERY, max_chars: 60000, full: true }), "linear_query", "response_too_large");
  assert.match(error.message, /max_chars/);
});

test("a compact selection is sent and its payload is fenced", async () => {
  const { fake, tools } = harness();
  scripted(fake, { data: { issues: { nodes: [{ id: "issue-1", title: "Native" }] } } });
  const { data } = payload(
    await tools.call("linear_list_issues", { filter: FILTER, first: 40, selection: "nodes { id title }" }),
    "linear_list_issues",
  );
  assert.equal(data.issues.nodes[0].title, "Native");
  const query = flat(namedQuery(fake).body.query);
  assert.match(query, /nodes \{ id title \}/);
  assert.doesNotMatch(query, /assignee/);
  assert.deepEqual(namedQuery(fake).body.variables.filter, FILTER);
  assert.equal(namedQuery(fake).body.variables.first, 40);
});

test("one pair of wrapping braces is a selection set", async () => {
  const { fake, tools } = harness();
  scripted(fake, { data: { issues: { nodes: [{ id: "issue-1", title: "Native" }] } } });
  const { data } = payload(
    await tools.call("linear_list_issues", { filter: FILTER, first: 40, selection: "{ nodes { id title } }" }),
    "linear_list_issues",
  );
  assert.equal(data.issues.nodes[0].id, "issue-1");
  const query = flat(namedQuery(fake).body.query);
  assert.match(query, /\{ nodes \{ id title \} \}/);
  assert.doesNotMatch(query, /\{\s*\{/);
});

test("a selection that escapes its field list is invalid_argument and is not sent", async () => {
  const { fake, tools } = harness();
  const error = toolError(
    await tools.call("linear_list_issues", { filter: FILTER, selection: 'id } } mutation { issueDelete(id: "x") { success }' }),
    "linear_list_issues",
    "invalid_argument",
  );
  assert.match(error.message, /selection must contain only GraphQL response fields/);
  assert.equal(namedQuery(fake), undefined);
});

test("Linear data:null with no errors is upstream_error, not {}", async () => {
  const { fake, tools } = harness();
  scripted(fake, { data: null });
  const error = toolError(await tools.call("linear_query", { query: QUERY }), "linear_query", "upstream_error");
  assert.match(error.message, /Linear returned no data/);
});
