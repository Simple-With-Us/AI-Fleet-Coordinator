import test from "node:test";
import assert from "node:assert/strict";
import { serveMcpRequest } from "../src/mcp.js";
import { toolCallEvent } from "../src/mcp-lifecycle.js";
import { HostedTools } from "../src/hosted-tools.js";
import { IdentityRouter } from "../src/delegated-identities.js";
import { loadConfig } from "../src/config.js";
import { okResult } from "../src/contract.js";
import { nativeOriginContext } from "../src/native-origin-context.js";
import * as S from "../src/seat-state.js";
import { testEnv } from "./helpers.mjs";

const child = "019a1234-abcd-7123-8123-0123456789ab";
const root = "019a1234-abcd-7123-8123-0123456789ac";
const base = { repo: "a/b", base: "main", head: "feature", title: "Change", body: "Test change." };
function fixture() {
  const now = Date.parse("2026-11-21T23:00:00Z");
  const store = S.memoryStore();
  const gate = { store };
  for (const key of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) {
    gate[key] = (args) => S[key](store, args, now);
  }
  gate.roleGet = () => S.roleGet(store, now);
  const tools = new HostedTools({ seat: "CODEX", scopes: ["zulip:read", "zulip:write"], clientId: "sbt-bearer",
    grantRef: "fixture", paused: false, config: loadConfig(testEnv()), zulip: null, key: "", gate,
    now: () => now, sleep: async () => {}, log: () => {} });
  return { tools, router: new IdentityRouter({ callerTools: tools }) };
}

test("SDK tools/call receives exact per-call context without replacing arguments", async () => {
  const { router } = fixture();
  const observed = [];
  router.call = async (name, args, context) => {
    observed.push({ name, args, context });
    await new Promise((resolve) => setTimeout(resolve, context.session === child ? 15 : 1));
    return okResult("fixture", { session: context.session });
  };
  const send = async (id, sid) => {
    const request = new Request("https://agents.simplewithus.com/mcp", { method: "POST",
      headers: { host: "agents.simplewithus.com", "content-type": "application/json", accept: "application/json, text/event-stream" },
      body: JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: {
        name: "gr_open_pr", arguments: { identity: "CODEX", ...base }, _meta: { threadId: sid, sessionId: root },
      } }) });
    const response = await serveMcpRequest(request, router, { token: "fixture", clientId: "fixture", scopes: ["zulip:read", "zulip:write"], extra: { seat: "CODEX" } });
    assert.equal(response.status, 200);
    await response.text();
  };
  await Promise.all([send(1, child), send(2, root)]);
  assert.deepEqual(observed.map((x) => x.context.session).sort(), [child, root].sort());
  for (const call of observed) {
    assert.equal(call.name, "gr_open_pr");
    assert.deepEqual(call.args, { identity: "CODEX", ...base });
    assert.deepEqual(Object.keys(call.context).sort(), ["session", "state"]);
  }
});

test("identity and hosted dispatch pass invocation to handler without persisting it", async () => {
  const { tools, router } = fixture();
  const contexts = [];
  tools.tool_gr_open_pr = async (_args, _audit, invocation) => {
    contexts.push(invocation);
    return okResult("fixture", {});
  };
  for (const [sid, head] of [[child, "one"], [root, "two"]]) {
    const reply = await router.call("gr_open_pr", { identity: "CODEX", ...base, head }, nativeOriginContext({ threadId: sid }));
    assert.notEqual(reply.isError, true, JSON.stringify(reply));
  }
  assert.deepEqual(contexts.map((x) => x.session), [child, root]);
  assert.equal(Object.hasOwn(tools, "invocation"), false);
  assert.equal(Object.hasOwn(router, "invocation"), false);
});

test("metadata-free fast call explicitly carries missing origin, never previous call", async () => {
  const calls = [];
  const router = { toolsByName: {}, call: async (_name, _args, context) => {
    calls.push(context);
    return okResult("fixture", {});
  } };
  await toolCallEvent({ id: 1, name: "gr_open_pr", args: base }, router);
  assert.deepEqual(calls, [{ state: "missing", session: null }]);
});
