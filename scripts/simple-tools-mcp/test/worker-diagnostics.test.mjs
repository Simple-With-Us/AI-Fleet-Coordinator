// worker_diagnostics:  the hosted read tool for the Worker's wake and runtime diagnostics (src/worker-diagnostics.js).
// It reads the refusal-log gate's aggregated rows (seat-state.js recordDiagnostic / diagnosticTail), so these tests
// record through the real writer into an in-memory store and read back through the tool.
// The module imports zod (as hosted-tools.js already does), so this runs after `npm ci`, as ci.yml does.
// Every seat tag here is synthetic (TESTALPHA, TESTBETA, TESTGAMMA);  none is a production seat.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools, HOSTED_TOOLS_BY_NAME, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS } from "../src/hosted-tools.js";
import { WORKER_DIAGNOSTICS_TOOLS, WORKER_DIAGNOSTICS_METHODS, boundedDiagnosticsInput, diagnosticRow } from "../src/worker-diagnostics.js";
import { ERROR_META_KEY, ToolError, schemaProblem } from "../src/contract.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());
const NOW = Date.parse("2026-11-12T16:00:00Z");

function harness({ scopes = ["zulip:read"], withGate = true, logGate: customGate = null } = {}) {
  const store = S.memoryStore();
  const seatGate = {};
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) seatGate[name] = (o) => S[name](store, o, NOW);
  seatGate.roleGet = () => S.roleGet(store, NOW);
  const logStore = S.memoryStore();
  const logGate = customGate ?? {
    recordDiagnostic: (entry) => S.recordDiagnostic(logStore, entry, NOW),
    diagnosticTail: (limit) => S.diagnosticTail(logStore, limit, NOW),
  };
  const logged = [];
  const tools = new HostedTools({
    seat: "TESTALPHA", scopes, clientId: "c", grantRef: "abcdef012345", paused: false, config, zulip: null, key: "", gate: seatGate,
    diagnosticsGate: withGate ? logGate : null, now: () => NOW, sleep: async () => {}, log: (row) => logged.push(row),
  });
  return { tools, logGate, logStore, logged };
}

const record = (gate, entry) => gate.recordDiagnostic(entry);
/** The handler itself, called directly, so its own bounds and zod parse are exercised without call()'s checks. */
const direct = (tools, args) => WORKER_DIAGNOSTICS_METHODS.tool_worker_diagnostics.call(tools, args, {});

function ok(result) {
  assert.ok(!result.isError, result.content?.[0]?.text);
  assert.equal(schemaProblem(HOSTED_TOOLS_BY_NAME.worker_diagnostics.outputSchema, result.structuredContent, "structuredContent"), null);
  assert.deepEqual(JSON.parse(result.content[0].text), result.structuredContent);
  return result.structuredContent;
}

function toolError(result, code) {
  assert.equal(result.isError, true, JSON.stringify(result));
  const obj = JSON.parse(result.content[0].text);
  assert.deepEqual(result._meta[ERROR_META_KEY], obj);
  assert.equal(obj.code, code, JSON.stringify(obj));
  return obj;
}

async function rejectsWith(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof ToolError, String(error));
    assert.equal(error.code, code, error.message);
    return true;
  });
}

test("worker_diagnostics is one closed-input hosted read tool, never a write", () => {
  assert.equal(WORKER_DIAGNOSTICS_TOOLS.length, 1);
  const tool = HOSTED_TOOLS_BY_NAME.worker_diagnostics;
  assert.ok(tool);
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.equal(tool.annotations.readOnlyHint, true);
  assert.ok(HOSTED_READ_TOOLS.has("worker_diagnostics") && !HOSTED_WRITE_TOOLS.has("worker_diagnostics"));
  assert.deepEqual(Object.keys(tool.inputSchema.properties).sort(), ["limit", "seat"]);
});

test("a seat with the read scope reads wake rows newest first, plus the recent rows of every kind", async () => {
  const { tools, logGate } = harness();
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "TESTBETA", status: 404 });
  await record(logGate, { kind: "token", reason: "invalid_content_type", status: 400 });
  await record(logGate, { kind: "wake", reason: "payload_too_large", seat: "TESTGAMMA", status: 413 });
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "TESTBETA", status: 404 });
  const out = ok(await tools.call("worker_diagnostics", {}));
  assert.equal(out.count, 2); // two distinct wake rows (TESTBETA wake_key_missing aggregates to count 2)
  assert.deepEqual(out.wake.map((r) => [r.reason, r.seat, r.count, r.status]), [["wake_key_missing", "TESTBETA", 2, 404], ["payload_too_large", "TESTGAMMA", 1, 413]]);
  assert.equal(out.recent.length, 3);
  assert.deepEqual(out.recent.map((r) => r.kind), ["wake", "wake", "token"]);
  for (const row of out.wake) assert.match(row.time, /^[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} (AM|PM)$/);
});

test("the seat argument filters the wake rows;  an unknown seat is an empty list, not an error", async () => {
  const { tools, logGate } = harness();
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "TESTBETA", status: 404 });
  await record(logGate, { kind: "wake", reason: "method_not_allowed", seat: "TESTBETA", status: 405 });
  await record(logGate, { kind: "wake", reason: "payload_too_large", seat: "TESTGAMMA", status: 413 });
  const beta = ok(await tools.call("worker_diagnostics", { seat: "TESTBETA" }));
  assert.equal(beta.count, 2);
  assert.ok(beta.wake.every((r) => r.seat === "TESTBETA" && r.kind === "wake"));
  const none = ok(await tools.call("worker_diagnostics", { seat: "TESTNOBODY" }));
  assert.equal(none.count, 0);
  assert.equal(none.total, 0);
  // recent stays global:  the filter narrows the wake list only.
  assert.equal(none.recent.length, 3);
});

test("the limit defaults to 50 only when omitted, allows 1 to 100, and total keeps the unclipped count", async () => {
  const { tools, logGate } = harness();
  for (let i = 0; i < 70; i += 1) await record(logGate, { kind: "wake", reason: `reason_${i}`, seat: "TESTBETA", status: 404 });
  const dflt = ok(await tools.call("worker_diagnostics", {}));
  assert.equal(dflt.count, 50);
  assert.equal(dflt.total, 70);
  assert.equal(dflt.recent.length, 50);
  assert.equal(ok(await tools.call("worker_diagnostics", { limit: 100 })).count, 70);
  assert.equal(ok(await tools.call("worker_diagnostics", { limit: 3 })).count, 3);
  // Newest first:  the last one recorded leads.
  assert.equal(ok(await tools.call("worker_diagnostics", { limit: 1 })).wake[0].reason, "reason_69");
  // The handler's own parse applies the same default when called with no arguments at all.
  assert.equal(ok(await direct(tools, {})).count, 50);
  assert.equal(ok(await direct(tools, undefined)).count, 50);
});

test("a limit that is not an integer from 1 to 100 is refused, never clamped or defaulted", async () => {
  const { tools, logGate } = harness();
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "TESTBETA", status: 404 });
  const bad = [0, 101, -1, 1.5, "5", "10", null, true, Number.NaN, Number.POSITIVE_INFINITY];
  // Through call():  the JSON-Schema check and then the handler.
  for (const limit of [0, 101, -1, 1.5, "5", "10"]) toolError(await tools.call("worker_diagnostics", { limit }), "invalid_argument");
  // Directly:  the handler's own zod parse refuses each one, with a message that names the field, not the value.
  for (const limit of bad) {
    await assert.rejects(direct(tools, { limit }), (error) => error instanceof ToolError && error.code === "invalid_argument" && /^limit must be an integer from 1 to 100$/.test(error.message));
  }
});

test("a seat that is not an upper-case seat tag is refused, through call() and by the handler itself", async () => {
  const { tools } = harness();
  for (const seat of ["testbeta", "TestBeta", "", "1TESTBETA", "TEST BETA"]) {
    toolError(await tools.call("worker_diagnostics", { seat }), "invalid_argument");
    await rejectsWith(direct(tools, { seat }), "invalid_argument");
  }
  for (const seat of [7, null, ["TESTBETA"], { tag: "TESTBETA" }]) await rejectsWith(direct(tools, { seat }), "invalid_argument");
});

test("it takes no secret-bearing argument:  unknown arguments are refused", async () => {
  const { tools } = harness();
  for (const args of [{ kind: "token" }, { token: "x" }, { authorization: "Bearer x" }, { seat: "testbeta" }, { seat: "A".repeat(41) }]) {
    toolError(await tools.call("worker_diagnostics", args), "invalid_argument");
    await rejectsWith(direct(tools, args), "invalid_argument");
  }
  for (const args of [null, [], "TESTBETA", 5]) await rejectsWith(direct(tools, args), "invalid_argument");
});

test("oversized, wide or deep arguments are refused by the structural bounds before any schema runs", async () => {
  const { tools } = harness();
  const deep = { seat: { a: { b: { c: "TESTBETA" } } } };
  const wide = Object.fromEntries(Array.from({ length: 500 }, (_, i) => [`k${i}`, i]));
  const long = { seat: "A".repeat(100000) };
  const three = { seat: "TESTBETA", limit: 5, extra: 1 };
  for (const args of [deep, wide, long, three]) {
    assert.equal(boundedDiagnosticsInput(args), false);
    const refused = toolError(await tools.call("worker_diagnostics", args), "invalid_argument");
    assert.match(refused.message, /structural limits/);
    await assert.rejects(direct(tools, args), (error) => error instanceof ToolError && error.code === "invalid_argument" && /structural limits/.test(error.message));
  }
  // The largest legal input fits the bounds.
  assert.equal(boundedDiagnosticsInput({ seat: `T${"A".repeat(39)}`, limit: 100 }), true);
});

test("the output carries only allowlisted fields and no key-like material, even if a stored row did", async () => {
  const { tools, logStore } = harness();
  const secret = `ghp_${"a".repeat(36)}`;
  const hmac = "f".repeat(64);
  await logStore.put("diagnostics", [
    { kind: "wake", reason: "wake_key_missing", seat: "TESTBETA", count: 2, ts: NOW, status: 404, authorization: `Bearer ${secret}`, hmac, body: "private body", key: secret, headers: { "x-simple-tools-signature": hmac } },
    { kind: "wake", reason: "bad reason with spaces", seat: "TESTBETA", count: 1, ts: NOW },
    { kind: "wake", reason: "ok_reason", seat: "not a seat", count: "many", ts: NOW, status: 99999 },
    { kind: "wake", reason: "no_time", seat: "TESTBETA", count: 1 },
    null, "text", 7,
  ]);
  const out = ok(await tools.call("worker_diagnostics", {}));
  const text = JSON.stringify(out);
  for (const leaked of [secret, hmac, "private body", "authorization", "Bearer", "signature"]) assert.ok(!text.includes(leaked), leaked);
  for (const row of [...out.wake, ...out.recent]) assert.deepEqual(Object.keys(row).sort(), ["count", "kind", "reason", "seat", "status", "time", "ts"]);
  assert.deepEqual(out.wake.map((r) => r.reason).sort(), ["ok_reason", "wake_key_missing"]);
  const sloppy = out.wake.find((r) => r.reason === "ok_reason");
  assert.deepEqual([sloppy.seat, sloppy.count, sloppy.status], [null, 1, null]);
  assert.equal(diagnosticRow({ kind: "wake", reason: "x", ts: 1.5 }), null);
});

test("a principal without the read scope is refused, and a missing store answers not_configured", async () => {
  const noScope = harness({ scopes: [] });
  await record(noScope.logGate, { kind: "wake", reason: "wake_key_missing", seat: "TESTBETA", status: 404 });
  const refused = toolError(await noScope.tools.call("worker_diagnostics", {}), "not_authorized");
  assert.match(refused.message, /zulip:read/);
  assert.ok(!JSON.stringify(refused).includes("wake_key_missing"));
  // A write-only grant is not enough either:  it is a read tool.
  const writeOnly = harness({ scopes: ["zulip:write"] });
  toolError(await writeOnly.tools.call("worker_diagnostics", {}), "not_authorized");
  const bare = harness({ withGate: false });
  const missing = toolError(await bare.tools.call("worker_diagnostics", {}), "not_configured");
  assert.equal(missing.retryable, false);
  await rejectsWith(direct(bare.tools, {}), "not_configured");
  // A gate object without the read method is treated the same way.
  const partial = harness({ logGate: { recordDiagnostic: async () => {} } });
  toolError(await partial.tools.call("worker_diagnostics", {}), "not_configured");
});

test("a store read that fails is a retryable unavailable, and nothing from the failure is echoed", async () => {
  const leak = `ghp_${"b".repeat(36)}`;
  const rejecting = harness({ logGate: { diagnosticTail: async () => { throw new Error(`storage exploded ${leak}`); } } });
  const obj = toolError(await rejecting.tools.call("worker_diagnostics", {}), "unavailable");
  assert.equal(obj.retryable, true);
  assert.equal(obj.message, "The Worker's diagnostics store is temporarily unavailable.  Try again shortly.");
  assert.ok(!JSON.stringify(obj).includes(leak) && !JSON.stringify(obj).includes("exploded"));
  assert.deepEqual(rejecting.logged.filter((row) => row.event === "worker_diagnostics_unavailable"), [{ event: "worker_diagnostics_unavailable" }]);
  await assert.rejects(direct(rejecting.tools, {}), (error) => error instanceof ToolError && error.code === "unavailable" && error.retryable === true);
  // A read that resolves to something other than a list is the same transient failure.
  const malformed = harness({ logGate: { diagnosticTail: async () => ({ rows: [] }) } });
  assert.equal(toolError(await malformed.tools.call("worker_diagnostics", {}), "unavailable").retryable, true);
});
