// The stdio adapter (scripts/mcp-servers/assigned-hosted-stdio.py) against the real SDK path (AFC-617 follow-up).
//
// A request that names a modern protocol version in its `_meta` envelope is served on the SDK's modern path, which refuses
// it with HTTP 400, code -32020 ("the request headers and body disagree") unless MCP-Protocol-Version, Mcp-Method and, for
// some methods, Mcp-Name say what the body says.  The adapter used to send only the version, so every modern frame it
// forwarded got the 400 and the Worker logged an mcp_error.  This runs the REAL adapter (test/stdio_adapter_probe.py, no
// network) on modern and legacy frames, sends exactly what it built through serveMcpRequest, and checks that the headers
// are accepted, and that each header is needed:  the same request without one of them is the 400.
// Skipped where python3 is missing.

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { serveMcpRequest } from "../src/mcp.js";
import { HostedTools } from "../src/hosted-tools.js";
import { IdentityRouter } from "../src/delegated-identities.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const PYTHON = spawnSync("python3", ["--version"]).status === 0;
const URL_MCP = "https://agents.simplewithus.com/mcp";
const VERSION = "2026-07-28";
const META = { "io.modelcontextprotocol/protocolVersion": VERSION, "io.modelcontextprotocol/clientInfo": { name: "assigned-hosted-stdio", version: "1.0" }, "io.modelcontextprotocol/clientCapabilities": {} };
const config = loadConfig(testEnv({ HOSTED_SEATS: "JET,GROK-WEB,CLAUDE,CODEX", EVENTS_ONLY_SEATS: "JET", ZULIP_CLAUDE_EMAIL: "claude-bot@simplewithus.zulipchat.com" }));

const modern = (method, params = {}, id = 1) => ({ jsonrpc: "2.0", id, method, params: { ...params, _meta: META } });
// supported:  the Worker serves the method, so the request must come back 200 and log no mcp_error.  An unsupported method
// (the Worker answers 404 "Method not found") still has to get past the header check, which is what these rows pin.
const MODERN = [
  { label: "tools/call", frame: modern("tools/call", { name: "whoami", arguments: {} }), supported: true, named: true },
  { label: "tools/call, a name that is not ASCII", frame: modern("tools/call", { name: "naïve", arguments: {} }), supported: true, named: true },
  { label: "tools/call, a name with a leading space", frame: modern("tools/call", { name: " lead", arguments: {} }), supported: true, named: true },
  { label: "tools/call, an emoji name", frame: modern("tools/call", { name: "😀", arguments: {} }), supported: true, named: true },
  { label: "tools/call, a name shaped like the base64 form", frame: modern("tools/call", { name: "=?base64?abc?=", arguments: {} }), supported: true, named: true },
  { label: "tools/list", frame: modern("tools/list"), supported: true, named: false },
  { label: "server/discover", frame: modern("server/discover"), supported: true, named: false },
  { label: "ping", frame: modern("ping"), supported: false, named: false },
  // A modern-form initialize needs the version header too:  without it the SDK refuses it with the same -32020, and with
  // both headers it gets past the header check (the Worker serves no initialize on the modern path, so it answers 404).
  { label: "initialize", frame: modern("initialize", { protocolVersion: VERSION, capabilities: {}, clientInfo: { name: "t", version: "1" } }), supported: false, named: false },
  { label: "prompts/get", frame: modern("prompts/get", { name: "daily" }), supported: false, named: true },
  { label: "resources/read", frame: modern("resources/read", { uri: "file:///notes/a b.md" }), supported: false, named: true },
  { label: "tasks/get", frame: modern("tasks/get", { taskId: "task-1" }), supported: false, named: true },
];
const LEGACY = [
  { label: "legacy initialize", frame: { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "1" } } } },
  { label: "legacy tools/list", frame: { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} } },
  { label: "legacy tools/call", frame: { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "whoami", arguments: {} } } },
];

function adapterOutput(frames) {
  const run = spawnSync("python3", ["-I", path.join(here, "stdio_adapter_probe.py")], { input: JSON.stringify(frames), encoding: "utf8" });
  assert.equal(run.status, 0, run.stderr);
  return JSON.parse(run.stdout);
}

function routerFor() {
  const router = new IdentityRouter({ callerTools: new HostedTools({ seat: "CLAUDE", scopes: ["zulip:read", "zulip:write"], config, gate: {}, clientId: "t", grantRef: "r" }) });
  router.call = async () => ({ content: [{ type: "text", text: "ok" }] });
  return router;
}

/** What the SDK path answers to this body and these adapter headers, with the logs it wrote. */
async function send(body, headers) {
  const logged = [];
  const real = console.log;
  console.log = (...args) => logged.push(args.join(" "));
  try {
    const request = new Request(URL_MCP, {
      method: "POST",
      headers: { host: "agents.simplewithus.com", "content-type": "application/json", accept: "application/json, text/event-stream", "content-length": String(Buffer.byteLength(body)), ...headers },
      body,
    });
    const res = await serveMcpRequest(request, routerFor(), { token: "r", clientId: "t", scopes: [], extra: { seat: "CLAUDE" } }, null);
    const text = await res.text();
    const payload = JSON.parse(text.replace(/^event: message\ndata: /, "").trim() || "null");
    return { status: res.status, code: payload?.error?.code, text, mcpError: logged.some((line) => line.includes("mcp_error")) };
  } finally {
    console.log = real;
  }
}

const HEADER_MISMATCH = -32020;

test("the adapter sends the standard headers a modern frame needs, and the SDK path accepts them", { skip: !PYTHON && "python3 is missing" }, async () => {
  const built = adapterOutput(MODERN.map((entry) => entry.frame));
  for (const [index, entry] of MODERN.entries()) {
    const { headers, body } = built[index];
    assert.equal(headers["mcp-protocol-version"], VERSION, entry.label);
    assert.equal(headers["mcp-method"], entry.frame.method, entry.label);
    assert.equal("mcp-name" in headers, entry.named, `${entry.label}: Mcp-Name is sent for the named methods only`);
    const answer = await send(body, headers);
    assert.notEqual(answer.code, HEADER_MISMATCH, `${entry.label}: ${answer.text.slice(0, 200)}`);
    assert.equal(answer.mcpError, false, `${entry.label}: the SDK path logged an mcp_error`);
    if (entry.supported) assert.equal(answer.status, 200, `${entry.label}: ${answer.text.slice(0, 200)}`);
  }
});

test("each header is needed:  the same request without one of them is the 400 the adapter used to cause", { skip: !PYTHON && "python3 is missing" }, async () => {
  const built = adapterOutput(MODERN.map((entry) => entry.frame));
  for (const [index, entry] of MODERN.entries()) {
    const { headers, body } = built[index];
    const without = ["mcp-protocol-version", "mcp-method", ...(entry.named ? ["mcp-name"] : [])];
    for (const name of without) {
      const { [name]: _dropped, ...rest } = headers;
      const answer = await send(body, rest);
      assert.equal(answer.status, 400, `${entry.label} without ${name}`);
      assert.equal(answer.code, HEADER_MISMATCH, `${entry.label} without ${name}`);
      assert.equal(answer.mcpError, true);
    }
  }
});

test("a name header that disagrees with the body is refused, so the base64 form is checked and not just present", { skip: !PYTHON && "python3 is missing" }, async () => {
  const [built] = adapterOutput([modern("tools/call", { name: "naïve", arguments: {} })]);
  const answer = await send(built.body, { ...built.headers, "mcp-name": "=?base64?bmFpdmU=?=" }); // "naive", not "naïve"
  assert.equal(answer.status, 400);
  assert.equal(answer.code, HEADER_MISMATCH);
});

test("legacy frames still go out without a method or name header, and the SDK path answers them", { skip: !PYTHON && "python3 is missing" }, async () => {
  const built = adapterOutput(LEGACY.map((entry) => entry.frame));
  for (const [index, entry] of LEGACY.entries()) {
    const { headers, body } = built[index];
    assert.equal("mcp-method" in headers, false, entry.label);
    assert.equal("mcp-name" in headers, false, entry.label);
    const answer = await send(body, headers);
    assert.equal(answer.status, 200, `${entry.label}: ${answer.text.slice(0, 200)}`);
    assert.equal(answer.mcpError, false, entry.label);
  }
});
