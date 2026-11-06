// Lifecycle frames answered without the SDK (src/mcp-lifecycle.js).  The pin that
// matters is parity:  each answer is compared with the real SDK path's answer to
// the same frame, status, every header and every byte, so an SDK upgrade that
// changes a frame fails here rather than in a client.

import test from "node:test";
import assert from "node:assert/strict";
import { SUPPORTED_PROTOCOL_VERSIONS } from "@modelcontextprotocol/server";
import { answerLifecycle, LIFECYCLE_MAX_BYTES } from "../src/mcp-lifecycle.js";
import { serveMcpRequest } from "../src/mcp.js";
import { HostedTools } from "../src/hosted-tools.js";
import { IdentityRouter } from "../src/delegated-identities.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const URL_MCP = "https://agents.simplewithus.com/mcp";
const config = loadConfig(testEnv({ HOSTED_SEATS: "JET,GROK-WEB,CLAUDE,CODEX", EVENTS_ONLY_SEATS: "JET", ZULIP_CLAUDE_EMAIL: "claude-bot@simplewithus.zulipchat.com", ZULIP_EMAIL_CODEX: "codex-bot@simplewithus.zulipchat.com", GR_WORKFLOW_TOOLS: "on" }));

function request(frame, headers = {}) {
  const body = typeof frame === "string" ? frame : JSON.stringify(frame);
  return new Request(URL_MCP, {
    method: "POST",
    headers: { host: "agents.simplewithus.com", "content-type": "application/json", accept: "application/json, text/event-stream", "content-length": String(Buffer.byteLength(body)), ...headers },
    body,
  });
}

function sdk(seat, req) {
  const tools = new HostedTools({ seat, scopes: ["zulip:read", "zulip:write"], config, gate: {}, clientId: "t", grantRef: "r" });
  const router = new IdentityRouter({ callerTools: tools });
  return serveMcpRequest(req, router, { token: "r", clientId: "t", scopes: ["zulip:read", "zulip:write"], extra: { seat } }, null);
}

const flat = (res) => Object.fromEntries([...res.headers].map(([k, v]) => [k.toLowerCase(), v]));

async function assertParity(seat, frame, headers) {
  const fast = await answerLifecycle(request(frame, headers), { seat, config });
  assert.ok(fast.response, `answered here: ${JSON.stringify(frame)}`);
  const real = await sdk(seat, request(frame, headers));
  const [fastText, realText] = [await fast.response.text(), await real.text()];
  assert.equal(fast.response.status, real.status);
  assert.deepEqual(flat(fast.response), flat(real));
  assert.equal(fastText, realText);
}

const init = (protocolVersion, id = 1) => ({ jsonrpc: "2.0", id, method: "initialize", params: { protocolVersion, capabilities: {}, clientInfo: { name: "t", version: "1" } } });

test("initialize matches the SDK, byte for byte, for every protocol version on the legacy path", async () => {
  for (const seat of ["CLAUDE", "CODEX", "GB-FIXER"]) {
    for (const version of SUPPORTED_PROTOCOL_VERSIONS) {
      const fast = await answerLifecycle(request(init(version)), { seat, config });
      if (!fast.response) continue; // A version the SDK does not serve this way stays with the SDK.
      await assertParity(seat, init(version));
    }
  }
});

test("at least the versions in live use are answered here", async () => {
  for (const version of ["2025-06-18", "2025-03-26", "2024-11-05"]) {
    assert.ok((await answerLifecycle(request(init(version)), { seat: "CLAUDE", config })).response, version);
  }
});

test("ping, notifications and tools/list match the SDK, byte for byte", async () => {
  const header = { "mcp-protocol-version": "2025-06-18" };
  for (const seat of ["CLAUDE", "GB-FIXER"]) {
    await assertParity(seat, { jsonrpc: "2.0", id: 7, method: "ping" }, header);
    await assertParity(seat, { jsonrpc: "2.0", id: "seven", method: "ping", params: {} }, header);
    await assertParity(seat, { jsonrpc: "2.0", method: "notifications/initialized" }, header);
    await assertParity(seat, { jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: 1 } }, header);
    await assertParity(seat, { jsonrpc: "2.0", id: 8, method: "tools/list", params: {} }, header);
    await assertParity(seat, { jsonrpc: "2.0", id: "list", method: "tools/list" }, header);
  }
  await assertParity("CLAUDE", { jsonrpc: "2.0", id: 8, method: "tools/list", params: {} });
});

test("tools/list is serialized once per key:  a second id reuses the bytes", async () => {
  const list = async (id) => (await (await answerLifecycle(request({ jsonrpc: "2.0", id, method: "tools/list" }), { seat: "CODEX", config })).response.text());
  const realStringify = JSON.stringify;
  await list(1);
  let big = 0;
  JSON.stringify = (...args) => { const out = realStringify(...args); if (out.length > 1000) big += 1; return out; };
  try {
    const text = await list(2);
    assert.match(text, /,"jsonrpc":"2\.0","id":2\}\n\n$/);
  } finally {
    JSON.stringify = realStringify;
  }
  assert.equal(big, 0);
});

test("everything else goes on to the SDK, with its body intact", async () => {
  const seat = "CLAUDE";
  const cases = [
    ["a browser Origin", init("2025-06-18"), { origin: "https://evil.example" }],
    ["a wrong Accept", init("2025-06-18"), { accept: "application/json" }],
    ["a wrong Content-Type", init("2025-06-18"), { "content-type": "text/plain" }],
    ["an unsupported protocol header", { jsonrpc: "2.0", id: 1, method: "ping" }, { "mcp-protocol-version": "1999-01-01" }],
    ["a foreign Host", init("2025-06-18"), { host: "evil.example" }],
    ["a batch", [{ jsonrpc: "2.0", id: 1, method: "ping" }], {}],
    ["invalid JSON", "{nope", {}],
    ["a tool call", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "guide", arguments: {} } }, {}],
    ["a modern request", { jsonrpc: "2.0", id: 1, method: "tools/list", params: { _meta: { "io.modelcontextprotocol/protocolVersion": "2026-07-28" } } }, { "mcp-protocol-version": "2026-07-28", "mcp-method": "tools/list" }],
    ["params on ping", { jsonrpc: "2.0", id: 1, method: "ping", params: { extra: 1 } }, {}],
    ["a list cursor", { jsonrpc: "2.0", id: 1, method: "tools/list", params: { cursor: "x" } }, {}],
    ["a null id", { jsonrpc: "2.0", id: null, method: "ping" }, {}],
    ["a fractional id", { jsonrpc: "2.0", id: 1.5, method: "ping" }, {}],
    ["an extra top-level key", { jsonrpc: "2.0", id: 1, method: "ping", extra: 1 }, {}],
    ["a wrong jsonrpc", { jsonrpc: "1.0", id: 1, method: "ping" }, {}],
    ["a malformed initialize", { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } }, {}],
    ["an unknown protocol in initialize", init("1999-01-01"), {}],
    ["server/discover", { jsonrpc: "2.0", id: 1, method: "server/discover" }, {}],
  ];
  for (const [name, frame, headers] of cases) {
    const body = typeof frame === "string" ? frame : JSON.stringify(frame);
    const seen = await answerLifecycle(request(frame, headers), { seat, config });
    assert.equal(seen.response, undefined, name);
    if (seen.request.bodyUsed) assert.fail(`${name}: body consumed`);
    assert.equal(await seen.request.text(), body, name);
  }
});

test("an events-only seat, a non-POST, a missing length and a large body are not read here", async () => {
  const eventsOnly = request(init("2025-06-18"));
  assert.equal((await answerLifecycle(eventsOnly, { seat: "JET", config })).request, eventsOnly);
  const get = new Request(URL_MCP, { method: "GET" });
  assert.equal((await answerLifecycle(get, { seat: "CLAUDE", config })).request, get);
  const big = request(init("2025-06-18"), { "content-length": String(LIFECYCLE_MAX_BYTES + 1) });
  assert.equal((await answerLifecycle(big, { seat: "CLAUDE", config })).request, big);
  assert.equal(big.bodyUsed, false);
});

test("a body longer than its Content-Length claimed is refused, never forwarded empty", async () => {
  const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping", params: { pad: "x".repeat(LIFECYCLE_MAX_BYTES) } });
  const lying = new Request(URL_MCP, {
    method: "POST",
    headers: { host: "agents.simplewithus.com", "content-type": "application/json", accept: "application/json, text/event-stream", "content-length": "100" },
    body,
  });
  const seen = await answerLifecycle(lying, { seat: "CLAUDE", config });
  assert.equal(seen.response?.status, 413);
});
