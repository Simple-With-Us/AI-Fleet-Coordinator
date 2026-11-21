// Failure-path tests for the thread-registry Sheets drain:  thrown errors must carry
// the Google error body for diagnosis, bounded and with credential-shaped content
// redacted, and a non-JSON error body must never mask the original failure.
import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { getGoogleAccessToken, makeSheetsAdapter, quotedTab, redactForError, runCli, THREAD_HEADERS, validatePayload } from "./thread_registry_export.mjs";
import { SPREADSHEET_ID, SHEET_TAB } from "./thread_registry_export.mjs";

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

test("token endpoint non-JSON 502 names the status and leaks no body secrets", async () => {
  const { privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const private_key = privateKey.export({ type: "pkcs8", format: "pem" });
  const savedEnv = process.env.GOOGLE_SHEETS_SA;
  process.env.GOOGLE_SHEETS_SA = JSON.stringify({
    client_email: "t@example.iam.gserviceaccount.com", private_key,
  });
  const saved = globalThis.fetch;
  globalThis.fetch = async () => fakeResponse(502, '<html>Bad Gateway</html> {"access_token": "ya29.SUPERSECRETVALUE123"}');
  try {
    await assert.rejects(getGoogleAccessToken(), (err) => {
      assert.match(err.message, /Google OAuth error: HTTP 502/);
      assert.ok(!err.message.includes("SUPERSECRETVALUE123"), "error leaked body credential");
      return true;
    });
  } finally {
    globalThis.fetch = saved;
    if (savedEnv === undefined) delete process.env.GOOGLE_SHEETS_SA;
    else process.env.GOOGLE_SHEETS_SA = savedEnv;
  }
});

test("tab titles are quoted for A1 ranges, with quotes doubled", () => {
  assert.equal(quotedTab("Zulip Threads"), "'Zulip Threads'");
  assert.equal(quotedTab("Simple"), "'Simple'");
  assert.equal(quotedTab("Bob's Tab"), "'Bob''s Tab'");
});

test("a payload with the wrong workbook fails validation before the workbook check", async () => {
  const calls = [];
  const good = Object.fromEntries(THREAD_HEADERS.map((h, i) => [h, `c${i}`]));
  void good;
  const row = THREAD_HEADERS.map((_, i) => (i === 0 ? "thread-1" : "v"));
  await assert.rejects(
    runCli(["payload.json"], {
      readText: () => JSON.stringify({ spreadsheetId: "nope", tab: SHEET_TAB, rows: [{ nope: 1 }] }),
      getToken: async () => { throw new Error("must not fetch a token"); },
      makeAdapter: () => { throw new Error("must not build an adapter"); },
      out: (m) => calls.push(["out", m]),
      err: (m) => calls.push(["err", m]),
    }),
    /thread-rows row 0 is not 15 cells/,
  );
  assert.deepEqual(calls, []);
  // The same shape with a well-formed row must reach the workbook and tab guard.
  await assert.rejects(
    runCli(["payload.json"], {
      readText: () => JSON.stringify({ spreadsheetId: "nope", tab: SHEET_TAB, rows: [row] }),
      getToken: async () => { throw new Error("must not fetch a token"); },
      makeAdapter: () => { throw new Error("must not build an adapter"); },
      out: (m) => calls.push(["out", m]),
      err: (m) => calls.push(["err", m]),
    }),
    /different workbook or tab/,
  );
  assert.deepEqual(calls, []);
});

test("the manual write drops credential-shaped cells and counts them refused", async () => {
  const seen = {};
  const good = THREAD_HEADERS.map((_, i) => (i === 0 ? "thread-1" : "v"));
  const bad = THREAD_HEADERS.map((_, i) => (i === 0 ? "thread-9" : "ghp_" + "x".repeat(30)));
  let outText = "";
  const code = await runCli(["payload.json", "--write"], {
    readText: () => JSON.stringify({ spreadsheetId: SPREADSHEET_ID, tab: SHEET_TAB, rows: [good, bad] }),
    getToken: async () => "test-token",
    makeAdapter: () => ({
      async validateHeaders() { return THREAD_HEADERS; },
      async readRows() { return []; },
      async appendRow(cells) { seen.appended = cells; return { rowNumber: 2 }; },
      async updateRow() { throw new Error("must not update on a fresh tab"); },
    }),
    takeLease: async () => ({ close: async () => {} }),
    out: (m) => { outText = m; },
    err: () => {},
  });
  assert.equal(code, 0);
  const result = JSON.parse(outText);
  assert.equal(result.refused, 1);
  assert.equal(result.appended, 1);
  assert.deepEqual(seen.appended[0], "thread-1");
});

test("the drain log carries the code, never the response body", async () => {
  const { drainThreadRows } = await import("./thread_registry_export.mjs");
  const logged = [];
  const err = Object.assign(new Error("sheets blew up: SECRET-BODY-XYZ"), { code: "write_failed", status: 500 });
  const result = await drainThreadRows({
    adapter: {
      async validateHeaders() { return THREAD_HEADERS; },
      async readRows() { return []; },
      async appendRow() { throw err; },
    },
    desired: [THREAD_HEADERS.map((_, i) => (i === 0 ? "thread-1" : "v"))],
    log: (m) => logged.push(m),
  });
  assert.equal(result.failed, 1);
  assert.equal(result.error, "write_failed");
  assert.match(logged.join("\n"), /code=write_failed/);
  assert.doesNotMatch(logged.join("\n"), /SECRET-BODY-XYZ/);
});
