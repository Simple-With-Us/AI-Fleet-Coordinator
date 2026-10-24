import test from "node:test";
import assert from "node:assert/strict";
import { parse, valueFromASTUntyped } from "graphql";
import { HostedTools, HOSTED_TOOLS_BY_NAME } from "../src/hosted-tools.js";
import { loadConfig } from "../src/config.js";
import { LinearError, LINEAR_MARKER_BEGIN } from "../src/linear.js";
import * as S from "../src/seat-state.js";
import { testEnv } from "./helpers.mjs";

function harness({ scopes = ["zulip:read", "zulip:write"], writes = true } = {}) {
  const clock = { now: Date.parse("2026-10-15T09:00:00Z") };
  const store = S.memoryStore();
  const gate = {};
  for (const name of ["reserve", "auditCall", "linearApiBegin", "linearApiSet", "linearApiDrop"]) {
    gate[name] = (args) => S[name](store, args, clock.now);
  }
  const requests = [];
  const labels = new Map([["ordinary", "feature"], ["claimed", "claimed:CURSOR"]]);
  const client = {
    secrets: () => ["fake-linear-client-secret"],
    confirmViewer: async () => ({ name: "Grok Web", app: true }),
    result: { projectUpdate: { success: true, project: { id: "p1", name: "Updated" } } },
    async call(query, variables) {
      requests.push({ query, variables: structuredClone(variables) });
      if (query.includes("AgentSyncLabel")) {
        const name = labels.get(variables.id);
        return { issueLabel: name ? { id: variables.id, name } : null };
      }
      if (query.includes("BoardIssue")) return { issue: { labels: { nodes: client.issueLabels ?? [] } } };
      if (query.includes("AgentSyncSchema")) return { __type: client.type };
      if (client.failure) throw client.failure;
      return client.result;
    },
  };
  const tools = new HostedTools({ seat: "GROK-WEB", scopes, config: loadConfig(testEnv()),
    zulip: null, key: "", gate, linear: client, linearWrites: writes,
    now: () => clock.now, sleep: async (ms) => { clock.now += ms; }, log: () => {},
  });
  return { tools, client, requests, store, clock };
}

function payload(result) {
  assert.notEqual(result.isError, true, JSON.stringify(result));
  const lines = result.content[0].text.split("\n");
  return JSON.parse(lines[lines.findIndex((line) => line.includes(LINEAR_MARKER_BEGIN)) + 1]);
}

const mutation = "mutation Update($id: String!, $input: ProjectUpdateInput!) { projectUpdate(id: $id, input: $input) { success project { id name } } }";
const input = (overrides = {}) => ({ mutation, variables: { id: "p1", input: { name: "Updated", description: "Details" } }, idempotency_key: "project-update-1", ...overrides });

test("general query preserves variables and fences arbitrary API resources", async () => {
  const { tools, client, requests } = harness();
  client.result = { projects: { nodes: [{ id: "p1", name: "Example" }], pageInfo: { endCursor: "next", hasNextPage: true } } };
  const query = "query Projects($after: String) { projects(first: 2, after: $after) { nodes { id name } pageInfo { endCursor hasNextPage } } }";
  const result = await tools.call("linear_query", { query, variables: { after: "cursor" } });
  assert.equal(payload(result).projects.nodes[0].id, "p1");
  assert.deepEqual(requests[0].variables, { after: "cursor" });
  assert.deepEqual(result.structuredContent, {});
});

test("read tool rejects mutations before any API call", async () => {
  const { tools, requests } = harness();
  assert.equal((await tools.call("linear_query", { query: mutation })).isError, true);
  assert.equal(requests.length, 0);
  assert.equal(HOSTED_TOOLS_BY_NAME.linear_query.annotations.readOnlyHint, true);
  assert.equal(HOSTED_TOOLS_BY_NAME.linear_mutation.annotations.destructiveHint, true);
});

test("schema discovery filters and paginates without returning the entire schema", async () => {
  const { tools, client } = harness();
  client.type = { name: "Query", kind: "OBJECT", fields: [{ name: "issues" }, { name: "projects" }, { name: "project" }] };
  const result = payload(await tools.call("linear_schema", { field: "project", limit: 1 }));
  assert.equal(result.total, 2);
  assert.equal(result.next_offset, 1);
  assert.deepEqual(result.fields, [{ name: "projects" }]);
});

test("API result secret fields and installed credentials are redacted", async () => {
  const { tools, client } = harness();
  client.result = { description: "fake-linear-client-secret", accessToken: "opaque", nested: { clientSecret: "opaque" } };
  const result = payload(await tools.call("linear_query", { query: "{ viewer { id } }" }));
  assert.equal(result.description, "[redacted]");
  assert.equal(result.accessToken, "[redacted secret]");
  assert.equal(result.nested.clientSecret, "[redacted secret]");
});

test("read scope and server write switch both prevent generic mutations", async () => {
  for (const options of [{ scopes: ["zulip:read"] }, { writes: false }]) {
    const { tools, requests } = harness(options);
    assert.equal((await tools.call("linear_mutation", input())).isError, true);
    assert.equal(requests.length, 0);
  }
});

test("wrong app identity blocks mutations", async () => {
  const { tools, client, requests } = harness();
  client.confirmViewer = async () => { throw new LinearError("swapped"); };
  assert.equal((await tools.call("linear_mutation", input())).isError, true);
  assert.equal(requests.length, 0);
});

test("mutation text is tagged without mutating caller variables and exact retries do not repeat it", async () => {
  const { tools, requests } = harness();
  const args = input();
  payload(await tools.call("linear_mutation", args));
  assert.match(requests[0].variables.input.description, /^\[GROK-WEB/);
  assert.equal(args.variables.input.description, "Details");
  payload(await tools.call("linear_mutation", args));
  assert.equal(requests.length, 1);
  const conflict = await tools.call("linear_mutation", input({ variables: { id: "p2", input: { name: "Other" } } }));
  assert.equal(conflict.isError, true);
  assert.equal(requests.length, 1);
});

test("inline and default input descriptions are seat tagged", async () => {
  for (const document of [
    'mutation { projectUpdate(id: "p1", input: {description: "hello"}) { success } }',
    'mutation M($input: ProjectUpdateInput! = {description: "hello"}) { projectUpdate(id: "p1", input: $input) { success } }',
  ]) {
    const { tools, requests } = harness();
    payload(await tools.call("linear_mutation", input({ mutation: document, variables: {} })));
    const operation = parse(requests[0].query).definitions[0];
    const argument = operation.selectionSet.selections[0].arguments.find((a) => a.name.value === "input");
    assert.match(valueFromASTUntyped(argument.value, requests[0].variables).description, /^\[GROK-WEB/);
  }
});

test("claim label IDs, author overrides and protected label renames are refused", async () => {
  for (const [document, variables] of [
    ['mutation M($input: IssueCreateInput!) { issueCreate(input:$input) { success } }', { input: { labelIds: ["claimed"] } }],
    ['mutation { issueLabelCreate(input:{name:"claimed:CURSOR"}) { success } }', {}],
    ['mutation { issueLabelUpdate(id:"claimed",input:{name:"renamed"}) { success } }', {}],
    ['mutation { issueCreate(input:{createAsUser:"Jay",title:"x"}) { success } }', {}],
    ['mutation { issueUpdate(id:"i1",input:{delegateId:"user-x"}) { success } }', {}],
    ['mutation { issueUpdate(id:"i1",input:{delegateId:null}) { success } }', {}],
    ['mutation M($input: IssueUpdateInput!) { issueUpdate(id:"i1",input:$input) { success } }', { input: { delegateId: "user-x" } }],
  ]) {
    const { tools, requests } = harness();
    const result = await tools.call("linear_mutation", input({ mutation: document, variables }));
    assert.equal(result.isError, true, document);
    assert.ok(requests.every((request) => !request.query.startsWith("mutation")));
  }
});

test("ordinary label additions are allowed but replacing a claimed issue's labels is refused", async () => {
  const first = harness();
  payload(await first.tools.call("linear_mutation", input({ mutation: 'mutation { issueAddLabel(id:"i1",labelId:"ordinary") { success } }', variables: {} })));
  const second = harness();
  second.client.issueLabels = [{ id: "claimed", name: "claimed:CURSOR" }];
  assert.equal((await second.tools.call("linear_mutation", input({ mutation: 'mutation { issueUpdate(id:"i1",input:{labelIds:["ordinary"]}) { success } }', variables: {} }))).isError, true);
  assert.ok(second.requests.every((request) => !request.query.startsWith("mutation")));
});

test("project label IDs are not looked up as issue labels", async () => {
  const { tools, requests } = harness();
  const document = 'mutation Update($input: ProjectUpdateInput!) { projectUpdate(id: "p1", input: $input) { success project { id name } } }';
  const result = await tools.call("linear_mutation", input({
    mutation: document,
    variables: { input: { labelIds: ["project-label-id"] } },
  }));
  assert.equal(result.isError, undefined);
  assert.equal(requests.length, 1);
  assert.match(requests[0].query, /^mutation/);
  assert.deepEqual(requests[0].variables.input.labelIds, ["project-label-id"]);
});

test("unknown mutation outcomes are retained and never automatically replayed", async () => {
  const { tools, client, clock, requests } = harness();
  client.failure = new LinearError("timeout");
  assert.equal((await tools.call("linear_mutation", input())).isError, true);
  client.failure = null;
  clock.now += 31000;
  assert.equal((await tools.call("linear_mutation", input())).isError, true);
  assert.equal(requests.length, 1);
});

test("HTTP 401 and 429 failures release the idempotency key and preserve mapped retry details", async () => {
  for (const [failure, code, retryAfter] of [
    [new LinearError("unauthorized", { status: 401 }), "not_authorized", undefined],
    [new LinearError("rate_limited", { status: 429, retryAfterS: 17 }), "rate_limited", 17],
  ]) {
    const { tools, client, requests } = harness();
    client.failure = failure;
    const first = await tools.call("linear_mutation", input());
    assert.equal(first.isError, true);
    const firstError = JSON.parse(first.content[0].text);
    assert.equal(firstError.code, code);
    if (retryAfter !== undefined) assert.equal(firstError.retry_after_s, retryAfter);

    client.failure = null;
    const retry = await tools.call("linear_mutation", input());
    assert.equal(retry.isError, undefined, JSON.stringify(retry));
    assert.equal(requests.length, 2);
  }
});

test("GraphQL execution errors remain unknown and the same key is not replayed", async () => {
  const { tools, client, requests } = harness();
  client.failure = new LinearError("unexpected", { status: 200, message: "resolver failed" });
  const first = await tools.call("linear_mutation", input());
  assert.equal(first.isError, true);
  assert.equal(JSON.parse(first.content[0].text).code, "outcome_unknown");

  client.failure = null;
  const retry = await tools.call("linear_mutation", input());
  assert.equal(retry.isError, true);
  assert.equal(JSON.parse(retry.content[0].text).code, "outcome_unknown");
  assert.equal(requests.length, 1);
});

test("an oversized first response can be recovered with the same key and a larger output limit", async () => {
  const { tools, client, requests } = harness();
  client.result = { data: "x".repeat(2000) };
  assert.equal((await tools.call("linear_mutation", input({ max_chars: 1000 }))).isError, true);
  assert.equal(payload(await tools.call("linear_mutation", input({ max_chars: 3000 }))).data.length, 2000);
  assert.equal(requests.length, 1);
});
