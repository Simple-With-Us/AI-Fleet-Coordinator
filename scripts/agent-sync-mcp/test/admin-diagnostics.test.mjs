import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { ToolError } from "../src/contract.js";
import * as state from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

function harness() {
  const store = state.memoryStore();
  const now = Date.parse("2026-10-15T12:00:00Z");
  const gate = {
    auditCall: (row) => state.auditCall(store, row, now),
    errorTail: (limit) => state.errorTail(store, limit, now),
    diagnosticTail: (limit) => state.diagnosticTail(store, limit, now),
  };
  const tools = new HostedTools({
    seat: "GROK-WEB", scopes: ["zulip:read", "zulip:write"], config: loadConfig(testEnv()),
    zulip: null, key: "", gate, now: () => now, log: () => {},
  });
  return { store, tools, gate, now };
}

test("call failures retain bounded safe diagnostics and retry metadata", async () => {
  const { tools, gate } = harness();
  tools.tool_topics = async () => {
    throw new ToolError("rate_limited", "Too many calls.", {
      retryable: true, retryAfterS: 17, resetAt: "2026-10-15T12:01:00Z", extra: { status: 429 },
    });
  };
  const result = await tools.call("topics", {});
  assert.equal(result.isError, true);
  const [row] = await gate.errorTail(50);
  assert.equal(row.error_code, "rate_limited");
  assert.equal(row.error_message, "Too many calls.");
  assert.equal(row.retryable, true);
  assert.equal(row.retry_after_s, 17);
  assert.equal(row.reset_at, "2026-10-15T12:01:00Z");
  assert.equal(row.upstream_status, 429);
});

test("scanner-flagged diagnostics use generic text and request values are omitted", async () => {
  const { tools, gate } = harness();
  const token = `ghp_${"a".repeat(36)}`;
  tools.tool_topics = async () => { throw new ToolError("upstream_error", `upstream rejected ${token}`); };
  await tools.call("topics", {});
  let rows = await gate.errorTail(50);
  assert.equal(rows[0].error_message, "Error message omitted after secret scan.");
  assert.ok(!JSON.stringify(rows).includes(token));

  tools.tool_reply = async (args) => { throw new ToolError("invalid_argument", `bad message ${args.message_id}: ${args.text}`); };
  await tools.call("reply", { message_id: 987654, text: "private reply body" });
  rows = await gate.errorTail(50);
  assert.ok(!JSON.stringify(rows).includes("987654"));
  assert.ok(!JSON.stringify(rows).includes("private reply body"));
  assert.equal(rows[0].error_message, "bad message [argument]: [argument]");
});

test("audit message redaction is bounded for deep and cyclic request objects", async () => {
  const { tools } = harness();
  let deep = { secret: "private-value" };
  for (let i = 0; i < 20; i++) deep = { child: deep };
  assert.equal(
    tools.auditErrorMessage("request failed", deep),
    "Error message omitted because request arguments exceeded audit bounds.",
  );
  const cyclic = { text: "private-value" };
  cyclic.self = cyclic;
  assert.equal(
    tools.auditErrorMessage("request failed", cyclic),
    "Error message omitted because request arguments exceeded audit bounds.",
  );
  const broad = Array.from({ length: 1001 }, (_, i) => `value-${i}`);
  assert.equal(
    tools.auditErrorMessage("request failed", broad),
    "Error message omitted because request arguments exceeded audit bounds.",
  );
});

test("auditCall validates diagnostics, bounds error text, and caps retry metadata", async () => {
  const store = state.memoryStore();
  const now = Date.parse("2026-10-15T12:00:00Z");
  await state.auditCall(store, {
    tool: "topics", outcome: "error", error_code: "a".repeat(300),
    error_message: "x".repeat(500), retryable: "yes", retry_after_s: 99999,
    reset_at: "x".repeat(100), upstream_status: 700, exception_class: "Bad Class",
  }, now);
  const [row] = await state.errorTail(store, 50, now);
  assert.equal(row.error_code, undefined);
  assert.equal(row.error_message.length, 300);
  assert.equal(row.retryable, undefined);
  assert.equal(row.retry_after_s, 3600);
  assert.equal(row.reset_at, undefined);
  assert.equal(row.upstream_status, undefined);
  assert.equal(row.exception_class, undefined);
  assert.deepEqual(await state.errorTail(store, 0, now), []);

  await state.auditCall(store, { outcome: "error", error_message: `contains ghp_${"b".repeat(36)}` }, now + 1);
  assert.equal((await state.errorTail(store, 1, now + 1))[0].error_message, "Error message omitted after secret scan.");
});

test("last 100 errors are independent of success volume and expire after 90 days", async () => {
  const store = state.memoryStore();
  const now = Date.parse("2026-10-15T12:00:00Z");
  for (let i = 0; i < 105; i++) {
    await state.auditCall(store, { tool: "topics", outcome: "error", error_code: "unavailable", error_message: `failure ${i}` }, now + i);
  }
  for (let i = 0; i < 150; i++) await state.auditCall(store, { tool: "topics", outcome: "ok" }, now + 200 + i);
  const recent = await state.errorTail(store, 200, now + 400);
  assert.equal(recent.length, 100);
  assert.equal(recent[0].error_message, "failure 104");
  assert.equal(recent.at(-1).error_message, "failure 5");
  assert.deepEqual(await state.errorTail(store, 50, now + 90 * state.DAY_MS + 105), []);
});

test("global diagnostics aggregate safe dimensions and retain at most 100 rows for 90 days", async () => {
  const store = state.memoryStore();
  const now = Date.parse("2026-10-15T12:00:00Z");
  await state.recordDiagnostic(store, { kind: "token", reason: "provider_unavailable", seat: "GROK-WEB", status: 503 }, now);
  await state.recordDiagnostic(store, { kind: "token", reason: "provider_unavailable", seat: "GROK-WEB", status: 503 }, now + 1);
  await state.recordDiagnostic(store, { kind: "token", reason: "provider_unavailable", seat: "CODEX", status: 502 }, now + 2);
  let rows = await state.diagnosticTail(store, 40, now + 3);
  assert.equal(rows.length, 2);
  assert.equal(rows[1].count, 2);
  assert.equal(rows[1].seat, "GROK-WEB");
  assert.equal(rows[1].status, 503);
  await assert.rejects(() => state.recordDiagnostic(store, { kind: "token", reason: "free text with spaces" }, now), /invalid diagnostic reason/);
  await assert.rejects(() => state.recordDiagnostic(store, { kind: "token", reason: "bad", seat: "codex" }, now), /invalid diagnostic seat/);
  await assert.rejects(() => state.recordDiagnostic(store, { kind: "token", reason: "bad", status: 99 }, now), /invalid diagnostic status/);
  for (let i = 0; i < 105; i++) await state.recordDiagnostic(store, { kind: "wake", reason: `failure_${i}` }, now + 10 + i);
  rows = await state.diagnosticTail(store, 200, now + 200);
  assert.equal(rows.length, 100);
  assert.equal(rows[0].reason, "failure_104");
  assert.deepEqual(await state.diagnosticTail(store, 40, now + 90 * state.DAY_MS + 200), []);
});

test("unknown tool names are audited only with the generic safe identity", async () => {
  const { tools, store } = harness();
  await assert.rejects(() => tools.call("untrusted-submitted-tool-name", {}));
  const [row] = await state.errorTail(store, 1, Date.parse("2026-10-15T12:00:00Z"));
  assert.equal(row.tool, "unknown_tool");
  assert.equal(row.error_code, "unknown_tool");
  assert.equal(row.error_message, "Unknown tool name.");
  assert.ok(!JSON.stringify(row).includes("untrusted-submitted-tool-name"));
});

test("concurrent call audits and diagnostic increments are atomic", async () => {
  const store = state.memoryStore();
  const now = Date.parse("2026-10-15T12:00:00Z");
  await Promise.all(Array.from({ length: 100 }, (_, i) => state.auditCall(store, {
    tool: "topics", outcome: "error", error_code: "unavailable", client_id: "client-safe-id",
  }, now + i)));
  const errors = await state.errorTail(store, 100, now + 100);
  assert.equal(errors.length, 100);
  assert.ok(errors.every((row) => row.client_id === "client-safe-id"));

  await Promise.all(Array.from({ length: 100 }, (_, i) => state.recordDiagnostic(store, {
    kind: "token", reason: "provider_unavailable", seat: "GROK-WEB", status: 503,
  }, now + i)));
  const [diag] = await state.diagnosticTail(store, 40, now + 100);
  assert.equal(diag.count, 100);
});

test("call and error audit arrays stay within independent UTF-8 storage byte caps", async () => {
  const store = state.memoryStore();
  const now = Date.parse("2026-10-15T12:00:00Z");
  const unicode = "🚀".repeat(120);
  const message = "🧪".repeat(300);
  for (let i = 0; i < 400; i++) {
    await state.auditCall(store, {
      seat: unicode, grant_ref: unicode, client_id: unicode, tool: "topics", topic: unicode,
      outcome: "error", error_code: `failure_${i}`, error_message: message, message_id: i,
    }, now + i);
  }

  const day = new Date(now).toISOString().slice(0, 10);
  let calls = await store.get(`calls:${day}`);
  let errors = await store.get("errors");
  const bytes = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength;
  assert.ok(bytes(calls) <= state.CALLS_STORAGE_MAX_BYTES);
  assert.ok(bytes(errors) <= state.ERRORS_STORAGE_MAX_BYTES);
  assert.ok(calls.length < 400, "large call bucket was trimmed by bytes");
  assert.ok(errors.length < 100, "error array was trimmed by bytes before reaching its row cap");
  assert.equal(errors.at(-1).error_code, "failure_399", "newest retained error stays at the tail");
  assert.equal(calls.at(-1).message_id, 399, "newest call row is retained");

  for (let i = 400; i < 430; i++) {
    await state.auditCall(store, {
      seat: unicode, client_id: unicode, tool: "topics", topic: unicode, outcome: "ok", message_id: i,
    }, now + i);
  }
  calls = await store.get(`calls:${day}`);
  errors = await store.get("errors");
  assert.ok(bytes(calls) <= state.CALLS_STORAGE_MAX_BYTES);
  assert.ok(bytes(errors) <= state.ERRORS_STORAGE_MAX_BYTES);
  assert.equal(errors.at(-1).error_code, "failure_399", "success traffic does not displace newest retained error");
  assert.equal(calls.at(-1).message_id, 429);
});
