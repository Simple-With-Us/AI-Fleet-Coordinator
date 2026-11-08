// A tool call answered without the SDK (src/mcp-lifecycle.js, AFC-617).  The pin that matters is parity:
// every answer is compared with the real SDK path's answer to the same frame (status, every header, every
// byte), for the outcomes a tool can have, so an SDK upgrade that changes one fails here, not in a client.

import test from "node:test";
import assert from "node:assert/strict";
import { Server } from "@modelcontextprotocol/server";
import { answerLifecycle, projectToolResult, serveRoutedMcp, takeToolCall } from "../src/mcp-lifecycle.js";
import { serveMcpRequest } from "../src/mcp.js";
import { HostedTools, UnknownTool } from "../src/hosted-tools.js";
import { IdentityRouter } from "../src/delegated-identities.js";
import { loadConfig } from "../src/config.js";
import { ToolError, errorResult, okResult } from "../src/contract.js";
import { testEnv } from "./helpers.mjs";

const URL_MCP = "https://agents.simplewithus.com/mcp";
const config = loadConfig(testEnv({ HOSTED_SEATS: "JET,GROK-WEB,CLAUDE,CODEX", EVENTS_ONLY_SEATS: "JET", ZULIP_CLAUDE_EMAIL: "claude-bot@simplewithus.zulipchat.com", GR_WORKFLOW_TOOLS: "on" }));
const AUTH = (seat) => ({ token: "r", clientId: "t", scopes: ["zulip:read", "zulip:write"], extra: { seat } });

function request(frame, headers = {}) {
  const body = typeof frame === "string" ? frame : JSON.stringify(frame);
  return new Request(URL_MCP, {
    method: "POST",
    headers: { host: "agents.simplewithus.com", "content-type": "application/json", accept: "application/json, text/event-stream", "content-length": String(Buffer.byteLength(body)), ...headers },
    body,
  });
}

function routerFor(seat, call, specs = null) {
  const router = new IdentityRouter({ callerTools: new HostedTools({ seat, scopes: ["zulip:read", "zulip:write"], config, gate: {}, clientId: "t", grantRef: "r" }) });
  router.call = call;
  if (specs) router.toolsByName = specs(router.toolsByName);
  return router;
}

const call = (name, args, id = 5) => ({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });
const flat = (res) => Object.fromEntries([...res.headers].map(([k, v]) => [k.toLowerCase(), v]));

async function viaFast(seat, frame, headers, make, specs) {
  const seen = await answerLifecycle(request(frame, headers), { seat, config });
  assert.equal(seen.response, undefined, "a tool call is never answered by the lifecycle path");
  return { call: takeToolCall(seen.request), res: await serveRoutedMcp(seen.request, routerFor(seat, make, specs), AUTH(seat), null) };
}

async function assertParity(seat, frame, headers, make, specs, { fast = true } = {}) {
  const taken = await viaFast(seat, frame, headers, make, specs);
  assert.equal(taken.call !== null, fast, `recognised as a plain call: ${JSON.stringify(frame).slice(0, 100)}`);
  const real = await serveMcpRequest(request(frame, headers), routerFor(seat, make, specs), AUTH(seat), null);
  const [fastText, realText] = [await taken.res.text(), await real.text()];
  assert.equal(taken.res.status, real.status);
  assert.deepEqual(flat(taken.res), flat(real));
  assert.equal(fastText, realText);
}

const text = (value) => ({ type: "text", text: value });
const OUTCOMES = {
  // The two shapes every tool builds (src/contract.js), then the odd ones the SDK's schema also normalizes.
  "okResult": async () => okResult("hi", { a: 1, b: [1, { c: "d" }] }),
  "errorResult": async () => errorResult(new ToolError("invalid_argument", "bad thing", { extra: { hint: "x" } })),
  "errorResult, retryable": async () => errorResult(new ToolError("rate_limited", "slow", { retryable: true, retryAfterS: 3 })),
  "a _meta ahead of everything": async () => ({ _meta: { k: 1 }, isError: true, structuredContent: { a: 1 }, content: [text("t")] }),
  "an extra top-level key": async () => ({ content: [text("t")], foo: 1 }),
  "a text block with extra keys": async () => ({ content: [{ type: "text", text: "t", annotations: { audience: ["user"] }, extra: 1 }] }),
  "a text block with its _meta": async () => ({ content: [{ type: "text", text: "t", _meta: { m: 1 } }] }),
  "no content": async () => ({ structuredContent: { a: 1 } }),
  "an empty result": async () => ({}),
  "isError false": async () => ({ content: [text("t")], isError: false }),
  "text only": async () => ({ content: [text("hello")] }),
  "an object": async () => ({ content: [text("{}")], structuredContent: { a: 1, b: [1, 2, { c: null }] } }),
  "an array": async () => ({ content: [], structuredContent: [1, 2] }),
  "an array beside text": async () => ({ content: [text("kept")], structuredContent: [1, 2] }),
  "a string": async () => ({ content: [], structuredContent: "plain" }),
  "a number": async () => ({ content: [text("n")], structuredContent: 7 }),
  "null": async () => ({ content: [], structuredContent: null }),
  "an array with no content key": async () => ({ structuredContent: [3] }),
  "a tool error": async () => ({ isError: true, content: [text("nope")], structuredContent: { code: "x" } }),
  "a tool error with an array": async () => ({ isError: true, content: [], structuredContent: [9] }),
  "a thrown Error": async () => { throw new Error("boom"); },
  "a thrown error with a code and data": async () => { throw Object.assign(new Error("coded"), { code: -32001, data: { x: 1, y: [2] } }); },
  "a thrown error with the moved code": async () => { throw Object.assign(new Error("moved"), { code: -32002 }); },
  "a thrown error with a fractional code": async () => { throw Object.assign(new Error("frac"), { code: 1.5 }); },
  "a thrown string": async () => { throw "plain string"; },
  "a thrown unknown tool": async (name) => { throw new UnknownTool(name); },
  "a large result": async () => ({ content: [text("x".repeat(200_000))], structuredContent: { rows: Array.from({ length: 2000 }, (_, i) => ({ i, s: "y".repeat(40) })) } }),
};

test("a plain tool call matches the SDK, byte for byte, for every outcome a tool can have", async () => {
  for (const seat of ["CLAUDE", "GB-FIXER"]) {
    for (const [label, make] of Object.entries(OUTCOMES)) {
      for (const id of [5, "five", 0, -3]) {
        try {
          await assertParity(seat, call("whoami", { x: 1 }, id), {}, make);
        } catch (error) {
          error.message = `${seat} / ${label} / id ${JSON.stringify(id)}: ${error.message}`;
          throw error;
        }
      }
    }
  }
});

test("the protocol header and a missing arguments key change nothing", async () => {
  const header = { "mcp-protocol-version": "2025-06-18" };
  await assertParity("CLAUDE", call("whoami", {}), header, OUTCOMES["an object"]);
  await assertParity("CLAUDE", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "whoami" } }, header, OUTCOMES["text only"]);
  await assertParity("CLAUDE", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "whoami", arguments: {} } }, { "mcp-method": "tools/call", "mcp-name": "whoami", ...header }, OUTCOMES["text only"]);
  // The legacy path ignores the name header, so a disagreeing one is the same call on both paths.
  await assertParity("CLAUDE", call("whoami", {}), { "mcp-method": "tools/call", "mcp-name": "other" }, OUTCOMES["text only"]);
});

test("the router's own arguments reach the tool exactly as parsed", async () => {
  const seen = [];
  const make = async (name, args) => { seen.push([name, args]); return { content: [text("ok")] }; };
  const args = { channel: "work-in-progress", limit: 5, nested: { a: [1, { b: "c" }] }, identity: "CLAUDE", n: null };
  await viaFast("CLAUDE", call("whoami", args), {}, make).then((r) => r.res.text());
  await serveMcpRequest(request(call("whoami", args)), routerFor("CLAUDE", make), AUTH("CLAUDE"), null).then((r) => r.text());
  assert.equal(seen.length, 2);
  assert.deepEqual(seen[0], seen[1]);
  assert.deepEqual(seen[0], ["whoami", args]);
});

test("the output schema decides the wrapping, as it does for the SDK", async () => {
  const withSchema = (schema) => (specs) => ({ ...specs, whoami: schema === undefined ? (({ outputSchema: _drop, ...rest }) => rest)(specs.whoami) : { ...specs.whoami, outputSchema: schema } });
  for (const schema of [undefined, { type: "object" }, { type: "array" }, { type: "string" }, {}, { type: "object", properties: {} }, { anyOf: [{ type: "object" }] }]) {
    for (const outcome of ["an object", "an array", "text only", "a tool error", "a string"]) {
      await assertParity("CLAUDE", call("whoami", {}), {}, OUTCOMES[outcome], withSchema(schema));
    }
  }
});

test("a frame with anything unusual in it stays with the SDK, which answers it", async () => {
  const good = { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "whoami", arguments: {} } };
  const make = OUTCOMES["text only"];
  const cases = [
    ["an extra top-level key", { ...good, extra: 1 }, {}],
    ["a _meta", { ...good, params: { ...good.params, _meta: { progressToken: "p" } } }, {}],
    ["a task", { ...good, params: { ...good.params, task: { ttl: 5 } } }, {}],
    ["an extra params key", { ...good, params: { ...good.params, other: 1 } }, {}],
    ["arguments as an array", { ...good, params: { name: "whoami", arguments: [] } }, {}],
    ["arguments as null", { ...good, params: { name: "whoami", arguments: null } }, {}],
    ["arguments as a string", { ...good, params: { name: "whoami", arguments: "x" } }, {}],
    ["an empty name", { ...good, params: { name: "", arguments: {} } }, {}],
    ["a numeric name", { ...good, params: { name: 5, arguments: {} } }, {}],
    ["no name", { ...good, params: { arguments: {} } }, {}],
    ["no params", { jsonrpc: "2.0", id: 1, method: "tools/call" }, {}],
    ["a null id", { ...good, id: null }, {}],
    ["a fractional id", { ...good, id: 1.5 }, {}],
    ["no id", { jsonrpc: "2.0", method: "tools/call", params: good.params }, {}],
    ["a wrong jsonrpc", { ...good, jsonrpc: "1.0" }, {}],
    ["a batch", [good], {}],
    ["a browser Origin", good, { origin: "https://evil.example" }],
    ["a wrong Accept", good, { accept: "application/json" }],
    ["an unsupported protocol header", good, { "mcp-protocol-version": "1999-01-01" }],
    ["a __proto__ key in the arguments", '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{"__proto__":{"x":1},"y":2}}}', {}],
  ];
  for (const [label, frame, headers] of cases) {
    try {
      await assertParity("CLAUDE", frame, headers, make, null, { fast: false });
    } catch (error) {
      error.message = `${label}: ${error.message}`;
      throw error;
    }
  }
});

test("a name the seat's list does not hold goes to the SDK, which answers Unknown tool", async () => {
  const make = async (name) => { throw new UnknownTool(name); };
  for (const name of ["no_such_tool", "constructor", "__proto__", "toString"]) {
    const taken = await viaFast("CLAUDE", call(name, {}), {}, make);
    assert.notEqual(taken.call, null, `${name}: the frame is recognised, the list decides`);
    const real = await serveMcpRequest(request(call(name, {})), routerFor("CLAUDE", make), AUTH("CLAUDE"), null);
    assert.equal(await taken.res.text(), await real.text(), name);
    assert.match(await (await serveMcpRequest(request(call(name, {})), routerFor("CLAUDE", make), AUTH("CLAUDE"), null)).text(), /Unknown tool/);
  }
});

test("a seat with events never takes the fast path, and an events-only seat is never read", async () => {
  const seen = await answerLifecycle(request(call("whoami", {})), { seat: "JET", config });
  assert.equal(takeToolCall(seen.request), null);
  // events !== null goes to the SDK:  the call still runs once and answers like any other.
  const make = OUTCOMES["text only"];
  const ok = await answerLifecycle(request(call("whoami", {})), { seat: "CLAUDE", config });
  assert.notEqual(takeToolCall(ok.request), null);
  const events = { list: async () => ({ events: [] }), subscribe: async () => ({}), unsubscribe: async () => ({}) };
  const answered = await serveRoutedMcp(ok.request, routerFor("CLAUDE", make), AUTH("CLAUDE"), events);
  assert.match(await answered.text(), /hello/);
});

test("the headers go out before a slow tool finishes", async () => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const make = async () => { await held; return { content: [text("late")] }; };
  const seen = await answerLifecycle(request(call("whoami", {})), { seat: "CLAUDE", config });
  const res = await Promise.race([serveRoutedMcp(seen.request, routerFor("CLAUDE", make), AUTH("CLAUDE"), null), new Promise((resolve) => setTimeout(() => resolve("headers waited for the tool"), 200))]);
  assert.notEqual(res, "headers waited for the tool");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/event-stream");
  release();
  assert.match(await res.text(), /^event: message\ndata: .*late.*\n\n$/s);
});

test("a call is taken once:  the tool runs exactly once on either path", async () => {
  let runs = 0;
  const make = async () => { runs += 1; return { content: [text("once")] }; };
  const taken = await viaFast("CLAUDE", call("whoami", {}), {}, make);
  await taken.res.text();
  assert.equal(runs, 1);
});

test("the request the route carries on with still holds its body for the SDK", async () => {
  const seen = await answerLifecycle(request(call("whoami", { a: 1 })), { seat: "CLAUDE", config });
  assert.equal(seen.request.bodyUsed, false);
  assert.equal(await seen.request.text(), JSON.stringify(call("whoami", { a: 1 })));
});

// projectToolResult against the SDK's own method, on every shape the SDK handles.
test("projectToolResult equals the SDK's projectCallToolResult", () => {
  const server = new Server({ name: "t", version: "1" }, { capabilities: { tools: {} } });
  const structured = [undefined, {}, { a: 1 }, [], [1, { b: 2 }], "s", 0, 7, false, true, null];
  const contents = [undefined, [], [text("t")], [{ type: "image", data: "x", mimeType: "image/png" }], [{ type: "image", data: "x", mimeType: "image/png" }, text("t")]];
  const schemas = [undefined, { type: "object" }, { type: "array" }, { type: "string" }, {}, { type: "object", properties: { a: { type: "number" } } }];
  for (const sc of structured) {
    for (const content of contents) {
      for (const schema of schemas) {
        for (const isError of [undefined, true]) {
          const result = { ...(content !== undefined ? { content } : {}), ...(sc !== undefined ? { structuredContent: sc } : {}), ...(isError ? { isError } : {}) };
          const label = `${JSON.stringify(result)} / ${JSON.stringify(schema)}`;
          assert.deepEqual(projectToolResult(result, schema), server.projectCallToolResult(result, schema), label);
        }
      }
    }
  }
});
