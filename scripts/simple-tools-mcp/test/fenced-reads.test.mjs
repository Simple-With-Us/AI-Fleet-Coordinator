// Fenced reads reach a client as their text (AFC-637).  A client that shows structuredContent in
// place of the text (Claude Code does) saw only counts, or {}, for every fenced read.  The fenced
// reads advertise no outputSchema and their results carry no structuredContent;  a tool that keeps
// its outputSchema always keeps its structuredContent, which an SDK client requires.

import test from "node:test";
import assert from "node:assert/strict";
import { IdentityRouter, FENCED_READS, textFirst, toolListFor } from "../src/delegated-identities.js";
import { HOSTED_TOOL_LIST, HOSTED_TOOLS_BY_NAME } from "../src/hosted-tools.js";
import { envelope, errorResult, okResult, ToolError } from "../src/contract.js";
import { linearEnvelope } from "../src/linear.js";
import { recallEnvelope } from "../src/recall.js";
import { fleetlinkEnvelope } from "../src/fleetlink.js";
import { notesEnvelope } from "../src/notes.js";

const NONCE = "0123456789abcdef";

/** One identity's tools:  every call answers with the result the test gives it. */
class StubTools {
  constructor(seat, answer) {
    this.seat = seat;
    this.answer = answer;
    this.config = {};
    this.paused = false;
    this.grantRef = "abcdef012345";
    this.clientId = "client";
    this.gate = { auditCall: async () => {} };
    this.auditRow = async (gate, row) => gate.auditCall(row);
  }
  async call(name, args) { return this.answer(name, args); }
  requireScope() {}
  async reserve() {}
  async safely(fn) { return fn(); }
  scrub(text) { return text; }
}

const routerAnswering = (answer, seat = "CLAUDE") => new IdentityRouter({ callerTools: new StubTools(seat, answer) });

test("every fenced read is a hosted tool that had an outputSchema, and no seat's list advertises one for it", () => {
  for (const name of FENCED_READS) {
    assert.ok(HOSTED_TOOLS_BY_NAME[name], `${name} is a hosted tool`);
    assert.ok(HOSTED_TOOLS_BY_NAME[name].outputSchema, `${name} had an outputSchema (else it needs no entry)`);
  }
  for (const seat of ["CLAUDE", "CODEX", "GROK-WEB"]) {
    const { list, byName } = toolListFor(seat);
    for (const name of FENCED_READS) {
      assert.ok(byName[name], `${seat} lists ${name}`);
      assert.equal(byName[name].outputSchema, undefined, `${seat}: ${name} advertises no outputSchema`);
      assert.ok(byName[name].inputSchema.properties.identity, `${seat}: ${name} keeps its identity argument`);
    }
    // Every other tool's outputSchema is untouched.
    for (const spec of list) {
      if (FENCED_READS.has(spec.name) || !HOSTED_TOOLS_BY_NAME[spec.name]) continue;
      assert.deepEqual(spec.outputSchema, HOSTED_TOOLS_BY_NAME[spec.name].outputSchema, `${seat}: ${spec.name} outputSchema unchanged`);
    }
  }
  // Every tool that keeps an outputSchema is one this change did not mean to touch.
  const kept = HOSTED_TOOL_LIST.filter((spec) => spec.outputSchema && !FENCED_READS.has(spec.name)).map((spec) => spec.name);
  assert.ok(kept.includes("whoami") && kept.includes("zulip_post") && kept.includes("notes_read"));
});

test("each fence the server builds is recognised, and its structuredContent dropped", () => {
  const fences = {
    zulip: envelope("read_topic", [["channel", "c"]], "1 message", [{ id: 1, content: "hi" }], NONCE),
    linear: linearEnvelope("simple-tools linear_list", "1 issue", [{ id: "AFC-1" }], NONCE),
    recall: recallEnvelope("simple-tools recall_search", "1 hit", [{ doc_id: "d" }], NONCE),
    fleetlink: fleetlinkEnvelope("simple-tools fleetlink_get_share", { id: "s" }, NONCE),
    notes: notesEnvelope("notes_list", [], { notes: [] }, NONCE),
  };
  for (const [family, text] of Object.entries(fences)) {
    const shown = textFirst(okResult(text, { count: 1, ids: [1] }), undefined);
    assert.equal(shown.structuredContent, undefined, `${family}: no structuredContent`);
    assert.deepEqual(shown.content, [{ type: "text", text }], `${family}: the text is untouched`);
  }
});

test("a fenced read through the router answers with its text alone", async () => {
  const text = envelope("read_topic", [["channel", "c"], ["topic", "t"]], "1 message", [{ id: 7, content: "hello" }], NONCE);
  const r = routerAnswering(() => okResult(text, { count: 1, ids: [7], next_since_id: null }));
  for (const name of ["zulip_read_topic", "linear_list", "recall_search"]) {
    const result = await r.call(name, {});
    assert.deepEqual(result, { content: [{ type: "text", text }] }, name);
  }
});

test("the linear_* passthrough's {} gives way to its fenced text", async () => {
  const text = linearEnvelope("simple-tools linear_get_issue", "Official Linear MCP content", ["{\"id\":\"AFC-637\"}"], NONCE);
  const passthrough = Object.keys(HOSTED_TOOLS_BY_NAME).find((name) => name.startsWith("linear_") && !HOSTED_TOOLS_BY_NAME[name].outputSchema);
  assert.ok(passthrough, "a linear_* tool without an outputSchema exists");
  const r = routerAnswering(() => okResult(text));
  assert.deepEqual(await r.call(passthrough, {}), { content: [{ type: "text", text }] });
});

test("an empty structuredContent gives way to the text even unfenced, when no outputSchema is served", async () => {
  const name = Object.keys(HOSTED_TOOLS_BY_NAME).find((n) => !HOSTED_TOOLS_BY_NAME[n].outputSchema && !n.startsWith("whatsapp_") && !n.startsWith("gr_"));
  const r = routerAnswering(() => okResult("done"));
  assert.deepEqual(await r.call(name, {}), { content: [{ type: "text", text: "done" }] });
});

test("a tool that keeps its outputSchema keeps its structuredContent, fenced or not", async () => {
  const fenced = notesEnvelope("notes_read", [["title", "x"]], { title: "x", body: "b" }, NONCE);
  const r = routerAnswering((name) => name === "notes_read" ? okResult(fenced, { title: "x", body: "b" }) : okResult("{}", {}));
  assert.deepEqual((await r.call("notes_read", { title: "x" })).structuredContent, { title: "x", body: "b" });
  // whoami advertises an outputSchema:  even an empty structuredContent stays, as an SDK client requires.
  assert.deepEqual((await r.call("whoami", {})).structuredContent, {});
});

test("errors and unfenced results with real structured values are left alone", async () => {
  const err = errorResult(new ToolError("invalid_argument", "bad"));
  const r1 = routerAnswering(() => err);
  assert.equal(await r1.call("zulip_read_topic", {}), err);
  const plain = okResult(JSON.stringify({ a: 1 }), { a: 1 });
  const name = Object.keys(HOSTED_TOOLS_BY_NAME).find((n) => !HOSTED_TOOLS_BY_NAME[n].outputSchema && !n.startsWith("whatsapp_") && !n.startsWith("gr_"));
  const r2 = routerAnswering(() => plain);
  assert.deepEqual(await r2.call(name, {}), plain);
  // A fence marker merely quoted inside a line is not a fence.
  const quoted = okResult("see BEGIN_UNTRUSTED_ZULIP nonce=0123456789abcdef above", { n: 1 });
  assert.deepEqual(textFirst(quoted, undefined), quoted);
});
