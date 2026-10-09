import test from "node:test";
import assert from "node:assert/strict";
import TOOLS from "../src/tools.phase0.json" with { type: "json" };
import { hello, helloWrite, HANDLERS, instructionsFor } from "../src/tool-logic.js";

const auth = (over = {}) => ({
  token: "t",
  clientId: "https://chatgpt.com/oauth/client.json",
  scopes: ["zulip:read", "zulip:write"],
  extra: { props: { seat: "JET" }, paused: false },
  ...over,
});

test("tool schemas:  two tools, closed inputs, no identity arguments", () => {
  assert.deepEqual(TOOLS.map((t) => t.name), ["hello", "hello_write"]);
  for (const tool of TOOLS) {
    assert.equal(tool.inputSchema.type, "object");
    assert.equal(tool.inputSchema.additionalProperties, false, tool.name);
    for (const banned of ["seat", "as", "rc", "no_tag", "fleet"]) assert.ok(!(banned in tool.inputSchema.properties), `${tool.name} must not take ${banned}`);
    assert.ok(HANDLERS[tool.name], tool.name);
  }
  const [h, w] = TOOLS;
  assert.equal(h.annotations.readOnlyHint, true);
  // hello_write must look like a real write so clients ask to confirm.
  assert.equal("readOnlyHint" in w.annotations, false);
  assert.equal(w.annotations.destructiveHint, false);
});

test("hello returns the seat from props, never from arguments", () => {
  const out = hello({ seat: "GROK-WEB" }, auth());
  assert.equal(out.isError, undefined);
  assert.deepEqual(out.structuredContent, { seat: "JET", scopes: ["zulip:read", "zulip:write"], client_id: "https://chatgpt.com/oauth/client.json" });
  assert.deepEqual(JSON.parse(out.content[0].text), out.structuredContent);
});

test("hello_write acknowledges and posts nothing", () => {
  const out = helloWrite({ note: "phase 0 test" }, auth());
  assert.deepEqual(out.structuredContent, { ack: true, seat: "JET", posted: false, note_length: 12 });
  assert.ok(!out.content[0].text.includes("phase 0 test"), "the note is never echoed");
});

test("paused seat:  tool error with no structuredContent", () => {
  for (const fn of [hello, helloWrite]) {
    const out = fn({}, auth({ extra: { props: { seat: "JET" }, paused: true } }));
    assert.equal(out.isError, true);
    assert.equal(out.structuredContent, undefined);
    assert.equal(out._meta["agent-sync/error"].code, "paused");
    assert.deepEqual(JSON.parse(out.content[0].text), out._meta["agent-sync/error"]);
  }
});

test("missing scope carries the mcp/www_authenticate challenge", () => {
  const out = helloWrite({}, auth({ scopes: ["zulip:read"] }));
  assert.equal(out.isError, true);
  assert.equal(out._meta["agent-sync/error"].code, "not_authorized");
  const [challenge] = out._meta["mcp/www_authenticate"];
  assert.match(challenge, /^Bearer error="insufficient_scope", scope="zulip:write", resource_metadata="https:\/\/agent-sync\.jays\.services\/\.well-known\/oauth-protected-resource\/mcp"/);
});

test("no seat in props is not authorized", () => {
  const out = hello({}, auth({ extra: { props: {} } }));
  assert.equal(out._meta["agent-sync/error"].code, "not_authorized");
});

test("server instructions name the seat", () => {
  assert.match(instructionsFor("JET"), /as one bot, JET\./);
});
