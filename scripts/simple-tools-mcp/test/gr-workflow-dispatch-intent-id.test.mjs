// gr_workflow_dispatch must accept optional intent_id alone (AFC-727): the
// advertised MCP schema names it without requiring expected_head_sha; intent
// alone binds idempotency, and expected_head_sha is an optional source fence.

import test from "node:test";
import assert from "node:assert/strict";
import { GR_TOOLS, GrRelayClient } from "../src/gr-tools.js";
import { HostedTools } from "../src/hosted-tools.js";
import { loadConfig } from "../src/config.js";
import * as S from "../src/seat-state.js";
import { testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());

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

function grHarness(gr) {
  const clock = { now: Date.parse("2026-10-25T14:00:00Z") };
  return new HostedTools({
    seat: "JET-CHAT",
    scopes: ["zulip:read", "zulip:write"],
    clientId: "sbt-bearer",
    grantRef: "abc123",
    paused: false,
    config,
    zulip: null,
    key: "",
    gate: memoryGate(clock),
    cf: {},
    gr,
    now: () => clock.now,
    sleep: async () => {},
    log: () => {},
  });
}

const INTENT = "qualification-one";

test("gr_workflow_dispatch advertises optional intent_id with the dispatch pattern", () => {
  const tool = GR_TOOLS.find((t) => t.name === "gr_workflow_dispatch");
  assert.deepEqual(tool.inputSchema.properties.intent_id, {
    type: "string",
    pattern: "^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$",
  });
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.ok(!tool.inputSchema.required.includes("intent_id"));
});

test("intent_id alone reaches the relay request body unchanged", async () => {
  const args = { repo: "Simple-With-Us/AI-Fleet-Coordinator", workflow: "ci.yml", ref: "main", intent_id: INTENT };
  let sent = null;
  const client = new GrRelayClient({
    baseUrl: "https://relay.invalid",
    accessId: "test-access-id",
    accessSecret: "test-access-secret",
    bearer: "test-client-bearer",
    fetch: async (url, init) => {
      assert.equal(url, "https://relay.invalid/v1/workflow_dispatch");
      sent = JSON.parse(init.body);
      return new Response(JSON.stringify({ ok: true }));
    },
  });
  const result = await client.call("workflow_dispatch", args);
  assert.equal(result.status, 200);
  assert.equal(sent.intent_id, INTENT);
  assert.equal(sent.workflow, "ci.yml");
  assert.equal(sent.ref, "main");
});

test("gr_workflow_dispatch accepts intent_id without expected_head_sha at the Worker boundary", async () => {
  let upstream = 0;
  const tools = grHarness(new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "test-access-id",
    accessSecret: "test-access-secret",
    bearer: "sbt_jet-chat_testbearer",
    fetch: async (_url, init) => {
      upstream++;
      const body = JSON.parse(init.body);
      assert.equal(body.intent_id, INTENT);
      assert.equal(body.expected_head_sha, undefined);
      return new Response(JSON.stringify({ accepted: true, replayed: false }), { status: 200 });
    },
  }));
  const result = await tools.call("gr_workflow_dispatch", {
    repo: "a/b",
    workflow: "ci.yml",
    ref: "main",
    intent_id: INTENT,
  });
  assert.ok(!result.isError);
  assert.equal(upstream, 1);
});
