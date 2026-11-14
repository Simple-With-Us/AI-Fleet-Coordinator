// Failure-path tests for the thread-registry Sheets drain:  thrown errors must carry
// the Google error body for diagnosis, bounded and with credential-shaped content
// redacted, and a non-JSON error body must never mask the original failure.
import test from "node:test";
import assert from "node:assert/strict";
import { makeSheetsAdapter, redactForError } from "./thread_registry_export.mjs";

function fakeResponse(status, body, { textThrows = false } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: textThrows ? async () => { throw new Error("stream blew up"); } : async () => body,
    json: async () => JSON.parse(body),
  };
}

test("redactForError caps detail at 500 chars", () => {
  const out = redactForError("x".repeat(5000));
  assert.ok(out.length <= 503);
  assert.ok(out.endsWith("..."));
});

test("redactForError tolerates null/undefined as empty", () => {
  assert.equal(redactForError(null), "");
  assert.equal(redactForError(undefined), "");
});

test("Sheets failure carries the response body for diagnosis", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => fakeResponse(403, JSON.stringify({
    error: { code: 403, message: "The caller does not have permission", status: "PERMISSION_DENIED" },
  }));
  try {
    const adapter = makeSheetsAdapter("test-token", "sheet-id", "Tab");
    await assert.rejects(adapter.validateHeaders(), (err) => {
      assert.match(err.message, /Sheets API GET .* -> 403/);
      assert.match(err.message, /PERMISSION_DENIED/);
      return true;
    });
  } finally { globalThis.fetch = saved; }
});

test("Sheets failure detail is capped at 500 chars", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => fakeResponse(500, "y".repeat(20000));
  try {
    const adapter = makeSheetsAdapter("test-token", "sheet-id", "Tab");
    await assert.rejects(adapter.validateHeaders(), (err) => {
      assert.match(err.message, /-> 500/);
      assert.ok(err.message.length < 700, `message too long: ${err.message.length}`);
      return true;
    });
  } finally { globalThis.fetch = saved; }
});

test("non-JSON error body still surfaces and never masks the status", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => fakeResponse(502, "<html><body>Bad Gateway</body></html>");
  try {
    const adapter = makeSheetsAdapter("test-token", "sheet-id", "Tab");
    await assert.rejects(adapter.validateHeaders(), (err) => {
      assert.match(err.message, /-> 502/);
      assert.match(err.message, /Bad Gateway/);
      return true;
    });
  } finally { globalThis.fetch = saved; }
});

test("a body stream failure still throws the status-only error", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => fakeResponse(503, "", { textThrows: true });
  try {
    const adapter = makeSheetsAdapter("test-token", "sheet-id", "Tab");
    await assert.rejects(adapter.validateHeaders(), (err) => {
      assert.match(err.message, /Sheets API GET .* -> 503/);
      assert.ok(!err.message.includes("stream blew up"));
      return true;
    });
  } finally { globalThis.fetch = saved; }
});
