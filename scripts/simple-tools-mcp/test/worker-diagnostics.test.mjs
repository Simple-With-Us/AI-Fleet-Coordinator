// worker_diagnostics:  the hosted read tool for the Worker's wake and runtime diagnostics (src/worker-diagnostics.js).
// It reads the refusal-log gate's aggregated rows (seat-state.js recordDiagnostic / diagnosticTail), so these tests
// record through the real writer into an in-memory store and read back through the tool.
// No dependencies:  CI runs this with plain `node --test`.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools, HOSTED_TOOLS_BY_NAME, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS } from "../src/hosted-tools.js";
import { WORKER_DIAGNOSTICS_TOOLS, diagnosticRow } from "../src/worker-diagnostics.js";
import { ERROR_META_KEY, schemaProblem } from "../src/contract.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());
const NOW = Date.parse("2026-11-12T16:00:00Z");

function harness({ scopes = ["zulip:read"], withGate = true } = {}) {
  const store = S.memoryStore();
  const seatGate = {};
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) seatGate[name] = (o) => S[name](store, o, NOW);
  seatGate.roleGet = () => S.roleGet(store, NOW);
  const logStore = S.memoryStore();
  const logGate = {
    recordDiagnostic: (entry) => S.recordDiagnostic(logStore, entry, NOW),
    diagnosticTail: (limit) => S.diagnosticTail(logStore, limit, NOW),
  };
  const tools = new HostedTools({
    seat: "CLAUDE", scopes, clientId: "c", grantRef: "abcdef012345", paused: false, config, zulip: null, key: "", gate: seatGate,
    diagnosticsGate: withGate ? logGate : null, now: () => NOW, sleep: async () => {}, log: () => {},
  });
  return { tools, logGate, logStore };
}

const record = (gate, entry) => gate.recordDiagnostic(entry);

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
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "JET", status: 404 });
  await record(logGate, { kind: "token", reason: "invalid_content_type", status: 400 });
  await record(logGate, { kind: "wake", reason: "payload_too_large", seat: "MINIMAX", status: 413 });
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "JET", status: 404 });
  const out = ok(await tools.call("worker_diagnostics", {}));
  assert.equal(out.count, 2); // two distinct wake rows (JET wake_key_missing aggregates to count 2)
  assert.deepEqual(out.wake.map((r) => [r.reason, r.seat, r.count, r.status]), [["wake_key_missing", "JET", 2, 404], ["payload_too_large", "MINIMAX", 1, 413]]);
  assert.equal(out.recent.length, 3);
  assert.deepEqual(out.recent.map((r) => r.kind), ["wake", "wake", "token"]);
  for (const row of out.wake) assert.match(row.time, /^[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2}[ap]m$/);
});

test("the seat argument filters the wake rows;  an unknown seat is an empty list, not an error", async () => {
  const { tools, logGate } = harness();
  await record(logGate, { kind: "wake", reason: "wake_key_missing", seat: "JET", status: 404 });
  await record(logGate, { kind: "wake", reason: "method_not_allowed", seat: "JET", status: 405 });
  await record(logGate, { kind: "wake", reason: "payload_too_large", seat: "MINIMAX", status: 413 });
  const jet = ok(await tools.call("worker_diagnostics", { seat: "JET" }));
  assert.equal(jet.count, 2);
  assert.ok(jet.wake.every((r) => r.seat === "JET" && r.kind === "wake"));
  const none = ok(await tools.call("worker_diagnostics", { seat: "NOBODY" }));
  assert.equal(none.count, 0);
  assert.equal(none.total, 0);
  // recent stays global:  the filter narrows the wake list only.
  assert.equal(none.recent.length, 3);
});

test("the limit is clamped:  default 50, at most 100, at least 1;  total keeps the unclipped count", async () => {
  const { tools, logGate } = harness();
  for (let i = 0; i < 70; i += 1) await record(logGate, { kind: "wake", reason: `reason_${i}`, seat: "JET", status: 404 });
  const dflt = ok(await tools.call("worker_diagnostics", {}));
  assert.equal(dflt.count, 50);
  assert.equal(dflt.total, 70);
  assert.equal(dflt.recent.length, 50);
  assert.equal(ok(await tools.call("worker_diagnostics", { limit: 100 })).count, 70);
  assert.equal(ok(await tools.call("worker_diagnostics", { limit: 3 })).count, 3);
  for (const bad of [0, 101, -1, 1.5, "10"]) toolError(await tools.call("worker_diagnostics", { limit: bad }), "invalid_argument");
  // Newest first:  the last one recorded leads.
  assert.equal(ok(await tools.call("worker_diagnostics", { limit: 1 })).wake[0].reason, "reason_69");
});

test("the output carries only allowlisted fields and no key-like material, even if a stored row did", async () => {
  const { tools, logStore } = harness();
  const secret = `ghp_${"a".repeat(36)}`;
  const hmac = "f".repeat(64);
  await logStore.put("diagnostics", [
    { kind: "wake", reason: "wake_key_missing", seat: "JET", count: 2, ts: NOW, status: 404, authorization: `Bearer ${secret}`, hmac, body: "private body", key: secret, headers: { "x-simple-tools-signature": hmac } },
    { kind: "wake", reason: "bad reason with spaces", seat: "JET", count: 1, ts: NOW },
    { kind: "wake", reason: "ok_reason", seat: "not a seat", count: "many", ts: NOW, status: 99999 },
    { kind: "wake", reason: "no_time", seat: "JET", count: 1 },
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

test("a principal without the read scope is refused, and an unreachable store answers not_configured", async () => {
  const noScope = harness({ scopes: [] });
  await record(noScope.logGate, { kind: "wake", reason: "wake_key_missing", seat: "JET", status: 404 });
  const refused = toolError(await noScope.tools.call("worker_diagnostics", {}), "not_authorized");
  assert.match(refused.message, /zulip:read/);
  assert.ok(!JSON.stringify(refused).includes("wake_key_missing"));
  // A write-only grant is not enough either:  it is a read tool.
  const writeOnly = harness({ scopes: ["zulip:write"] });
  toolError(await writeOnly.tools.call("worker_diagnostics", {}), "not_authorized");
  const bare = harness({ withGate: false });
  toolError(await bare.tools.call("worker_diagnostics", {}), "not_configured");
});

test("it takes no secret-bearing argument:  unknown arguments are refused", async () => {
  const { tools } = harness();
  for (const args of [{ kind: "token" }, { token: "x" }, { authorization: "Bearer x" }, { seat: "jet" }, { seat: "A".repeat(41) }]) {
    toolError(await tools.call("worker_diagnostics", args), "invalid_argument");
  }
});
