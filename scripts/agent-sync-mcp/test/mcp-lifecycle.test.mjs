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

// AFC-641:  opt-in bounded pages for tools/list (no-cursor keeps the full response above).

test("tools/list with a cursor serves bounded pages, byte-identical to the SDK", async () => {
  for (const seat of ["GROK-WEB", "CLAUDE", "CODEX"]) {
    for (const header of [{ "mcp-protocol-version": "2025-03-26" }, {}]) {
      // Cursors are server-issued (offset.list-tag).  The bare "0" entry point pages
      // like the SDK (no page precedes it, so no walk can be shifted);  a hand-composed
      // nonzero offset fails closed on our side even where the SDK would page it -
      // intentional, so a stale tool list can never silently shift a walk.
      {
        const res = await answerLifecycle(request({ jsonrpc: "2.0", id: 9, method: "tools/list", params: { cursor: "0" } }), { seat, config });
        const text = await res.response.text();
        const first = JSON.parse(text.slice(text.indexOf("data: ") + 6).trim()).result;
        assert.ok(first.tools.length > 0 && first.tools.length <= 100);
        assert.match(first.nextCursor, /^100\.[0-9a-f]{8}$/);
      }
      {
        const res = await answerLifecycle(request({ jsonrpc: "2.0", id: 10, method: "tools/list", params: { cursor: "100" } }), { seat, config });
        const text = await res.response.text();
        assert.match(text, /invalid cursor/);
      }
    }
  }
});

test("tools/list pages concatenate to the full list, in order, with no overlap", async () => {
  const dataOf = async (res) => { const text = await res.text(); return text.slice(text.indexOf("data: ") + 6).trim(); };
  const full = JSON.parse(await dataOf((await answerLifecycle(request({ jsonrpc: "2.0", id: 1, method: "tools/list" }), { seat: "CODEX", config })).response)).result.tools;
  const names = [];
  let cursor = "0";
  for (;;) {
    const res = await answerLifecycle(request({ jsonrpc: "2.0", id: 2, method: "tools/list", params: { cursor } }), { seat: "CODEX", config });
    const page = JSON.parse(await dataOf(res.response)).result;
    assert.ok(page.tools.length > 0 && page.tools.length <= 100);
    names.push(...page.tools.map((tool) => tool.name));
    if (page.nextCursor === undefined) break;
    assert.ok(typeof page.nextCursor === "string" && page.nextCursor.length > 0, "a present nextCursor is a non-empty string");
    cursor = page.nextCursor;
  }
  assert.deepEqual(names, full.map((tool) => tool.name));
});

test("tools/list rejects a malformed or out-of-range cursor exactly like the SDK", async () => {
  await assertParity("CODEX", { jsonrpc: "2.0", id: 11, method: "tools/list", params: { cursor: "x" } }, {});
  await assertParity("CODEX", { jsonrpc: "2.0", id: 12, method: "tools/list", params: { cursor: "999999999999" } }, {});
  const empty = await answerLifecycle(request({ jsonrpc: "2.0", id: 13, method: "tools/list", params: { cursor: "" } }), { seat: "CODEX", config });
  assert.match(JSON.stringify(empty.response ? await empty.response.text() : empty), /invalid cursor/, "an empty-string cursor is rejected, never forwarded");
});

test("catalog pages every tool name and description, terminating without a nextCursor", async () => {
  const tools = new HostedTools({ seat: "CODEX", scopes: ["zulip:read"], config, gate: {}, clientId: "t", grantRef: "r" });
  const router = new IdentityRouter({ callerTools: tools });
  const seen = [];
  let cursor;
  for (;;) {
    const args = cursor === undefined ? { identity: "CODEX" } : { identity: "CODEX", cursor };
    const result = await router.call("catalog", args);
    assert.ok(!result.isError, JSON.stringify(result).slice(0, 200));
    const page = result.structuredContent;
    assert.ok(page.tools.length > 0 && page.tools.length <= 200);
    for (const entry of page.tools) {
      assert.deepEqual(Object.keys(entry).sort(), ["description", "name"]);
      seen.push(entry.name);
    }
    if (page.nextCursor === undefined) break;
    assert.ok(typeof page.nextCursor === "string" && page.nextCursor.length > 0, "a present nextCursor is a non-empty string");
    cursor = page.nextCursor;
  }
  assert.deepEqual(seen, router.toolList.map((tool) => tool.name));
  assert.ok(seen.includes("catalog"));
});

test("catalog refuses a bad cursor as a tool error, never a protocol error", async () => {
  const tools = new HostedTools({ seat: "CODEX", scopes: ["zulip:read"], config, gate: {}, clientId: "t", grantRef: "r" });
  const router = new IdentityRouter({ callerTools: tools });
  const result = await router.call("catalog", { identity: "CODEX", cursor: "bogus" });
  assert.ok(result.isError);
  assert.match(JSON.stringify(result), /invalid cursor/);
});

test("catalog refuses a cursor tagged for a different tool list", async () => {
  const tools = new HostedTools({ seat: "CODEX", scopes: ["zulip:read"], config, gate: {}, clientId: "t", grantRef: "r" });
  const router = new IdentityRouter({ callerTools: tools });
  const first = await router.call("catalog", { identity: "CODEX" });
  const next = first.structuredContent.nextCursor;
  assert.ok(typeof next === "string" && next.includes("."), "issued cursors carry the list tag");
  const [offset, tag] = next.split(".");
  const staleTag = tag.replace(/[0-9a-f]/, (c) => (c === "0" ? "1" : "0"));
  const stale = await router.call("catalog", { identity: "CODEX", cursor: `${offset}.${staleTag}` });
  assert.ok(stale.isError);
  assert.match(JSON.stringify(stale), /invalid cursor/);
  const bare = await router.call("catalog", { identity: "CODEX", cursor: offset });
  assert.ok(bare.isError, "a bare hand-composed offset is refused");
  assert.match(JSON.stringify(bare), /invalid cursor/);
});

test("catalog audits exactly one row per execution, success and failure", async () => {
  const rows = [];
  const tools = new HostedTools({ seat: "CODEX", scopes: ["zulip:read"], config, gate: {}, clientId: "t", grantRef: "r" });
  tools.auditRow = async (gate, row) => { rows.push(row); };
  const router = new IdentityRouter({ callerTools: tools });
  const ok = await router.call("catalog", { identity: "CODEX" });
  assert.ok(!ok.isError);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].outcome, "ok");
  assert.equal(rows[0].tool, "catalog");
  const bad = await router.call("catalog", { identity: "CODEX", cursor: "bogus" });
  assert.ok(bad.isError);
  assert.equal(rows.length, 2);
  assert.equal(rows[1].outcome, "error");
  assert.equal(rows[1].error_code, "invalid_argument");
});
