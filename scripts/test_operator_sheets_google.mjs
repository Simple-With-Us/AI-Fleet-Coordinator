// Tests for agent_sync/operator_sheets_google.mjs (AFC-428).  No network: fetch is injected.
// Run: node --test scripts/test_operator_sheets_google.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, createVerify } from "node:crypto";
import {
  OperatorSheetsGoogle, OperatorSheetsError, createServiceAccountTokenProvider,
  SESSION_HEADERS, EVENT_HEADERS,
} from "./agent_sync/operator_sheets_google.mjs";

const SPREADSHEET = "sheet_ABC-123";
const IDS = { sessions: 2033000102, events: 2033000103 };

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), { status, headers });
}

function metadata({ sessionsTitle = "Operator Sessions", sessionsId = IDS.sessions } = {}) {
  return {
    sheets: [
      { properties: { sheetId: sessionsId, title: sessionsTitle, gridProperties: { rowCount: 50, columnCount: 20 } } },
      { properties: { sheetId: IDS.events, title: "Operator Events", gridProperties: { rowCount: 50, columnCount: 12 } } },
    ],
  };
}

// A scripted fake of the Sheets API.  Records every request; `overrides` can replace a handler.
function fakeSheets({ sessionsHeader = SESSION_HEADERS, meta = metadata(), write } = {}) {
  const requests = [];
  const fetch = async (url, init = {}) => {
    const method = init.method ?? "GET";
    requests.push({ url: String(url), method, init });
    const u = new URL(url);
    const path = decodeURIComponent(u.pathname);
    if (!path.includes("/values/")) return jsonResponse(meta);
    if (method === "GET") {
      const range = path.split("/values/")[1];
      if (range === "'Operator Sessions'!A1:T1") return jsonResponse({ values: [[...sessionsHeader]] });
      if (range === "'Operator Events'!A1:L1") return jsonResponse({ values: [[...EVENT_HEADERS]] });
      if (range.startsWith("'Operator Sessions'!A2:P")) {
        return jsonResponse({ values: [["rec-1", "one"], ["rec-2", "two", "s"]] });
      }
      return jsonResponse({ values: [] });
    }
    return write ? write(path, init) : jsonResponse({});
  };
  return { fetch, requests };
}

function sheetsWith(fake, extra = {}) {
  return new OperatorSheetsGoogle({
    spreadsheetId: SPREADSHEET, sheetIds: IDS, token: async () => "tok", fetch: fake.fetch, ...extra,
  });
}

const sessionRow = (id = "rec-1") => [id, ...Array(SESSION_HEADERS.length - 1).fill("")];
const eventRow = (id = "evt-1") => [id, ...Array(EVENT_HEADERS.length - 1).fill("")];

test("importing performs no I/O and exposes frozen headers", () => {
  assert.equal(SESSION_HEADERS.length, 16);
  assert.equal(EVENT_HEADERS.length, 9);
  assert.ok(Object.isFrozen(SESSION_HEADERS));
  assert.ok(Object.isFrozen(EVENT_HEADERS));
});

test("constructor rejects bad configuration", () => {
  const ok = { spreadsheetId: SPREADSHEET, sheetIds: IDS, token: async () => "t", fetch: async () => {} };
  assert.throws(() => new OperatorSheetsGoogle({ ...ok, spreadsheetId: "bad id!" }), { code: "invalid_config" });
  assert.throws(() => new OperatorSheetsGoogle({ ...ok, token: null }), { code: "invalid_config" });
  assert.throws(() => new OperatorSheetsGoogle({ ...ok, sheetIds: { sessions: 1 } }), { code: "invalid_sheet_id" });
  assert.throws(() => new OperatorSheetsGoogle({ ...ok, sheetIds: { sessions: 5, events: 5 } }), { code: "duplicate_sheet_id" });
  assert.throws(() => new OperatorSheetsGoogle({ ...ok, timeoutMs: 0 }), { code: "invalid_timeout" });
  assert.throws(() => new OperatorSheetsGoogle({ ...ok, timeoutMs: 61_000 }), { code: "invalid_timeout" });
});

test("inspect returns grid bounds and sends the bearer token", async () => {
  const fake = fakeSheets();
  const bounds = await sheetsWith(fake).inspect();
  assert.deepEqual(bounds, {
    sessions: { rowCount: 50, columnCount: 20 }, events: { rowCount: 50, columnCount: 12 },
  });
  assert.equal(fake.requests[0].init.headers.Authorization, "Bearer tok");
  assert.equal(fake.requests[0].init.redirect, "error");
  assert.ok(fake.requests[0].url.startsWith(`https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET}`));
});

test("validateHeaders fails closed on a changed header, tab title or tab id", async () => {
  const renamed = [...SESSION_HEADERS]; renamed[1] = "Name";
  await assert.rejects(sheetsWith(fakeSheets({ sessionsHeader: renamed })).validateHeaders(), { code: "header_mismatch" });
  await assert.rejects(sheetsWith(fakeSheets({ sessionsHeader: [...SESSION_HEADERS, "Extra"] })).validateHeaders(),
    { code: "header_mismatch" });
  await assert.rejects(sheetsWith(fakeSheets({ meta: metadata({ sessionsTitle: "Other" }) })).validateHeaders(),
    { code: "sheet_identity_mismatch" });
  await assert.rejects(sheetsWith(fakeSheets({ meta: metadata({ sessionsId: 7 }) })).validateHeaders(),
    { code: "sheet_identity_mismatch" });
  assert.equal(await sheetsWith(fakeSheets()).validateHeaders(), true);
});

test("readRows numbers rows physically and pads short rows", async () => {
  const rows = await sheetsWith(fakeSheets()).readRows("sessions", { startRow: 2, limit: 10 });
  assert.deepEqual(rows.map(r => r.rowNumber), [2, 3]);
  assert.equal(rows[0].cells.length, SESSION_HEADERS.length);
  assert.equal(rows[0].cells[0], "rec-1");
  assert.equal(rows[1].cells[2], "s");
  assert.equal(rows[1].cells[15], "");
});

test("readRows validates its arguments", async () => {
  const s = sheetsWith(fakeSheets());
  await assert.rejects(s.readRows("reference"), { code: "invalid_table" });
  await assert.rejects(s.readRows("constructor"), { code: "invalid_table" });
  await assert.rejects(s.readRows("sessions", { startRow: 1 }), { code: "invalid_start_row" });
  await assert.rejects(s.readRows("sessions", { limit: 501 }), { code: "invalid_read_limit" });
  await assert.rejects(s.readRows("sessions", { limit: 0 }), { code: "invalid_read_limit" });
});

test("readRows rejects non-scalar cells", async () => {
  const fetch = async () => jsonResponse({ values: [[{ evil: 1 }]] });
  const s = sheetsWith({ fetch });
  await assert.rejects(s.readRows("events"), { code: "invalid_rows_response" });
});

test("appendSession uses RAW, INSERT_ROWS and confirms the row number", async () => {
  const fake = fakeSheets({
    write: () => jsonResponse({
      spreadsheetId: SPREADSHEET,
      updates: { updatedRange: "'Operator Sessions'!A7:P7", updatedRows: 1, updatedColumns: 16, updatedCells: 16 },
    }),
  });
  const result = await sheetsWith(fake).appendSession(sessionRow());
  assert.deepEqual(result, { rowNumber: 7 });
  const write = fake.requests.at(-1);
  assert.equal(write.method, "POST");
  assert.match(write.url, /valueInputOption=RAW/);
  assert.match(write.url, /insertDataOption=INSERT_ROWS/);
  assert.match(decodeURIComponent(write.url), /:append/);
  assert.equal(JSON.parse(write.init.body).values[0][0], "rec-1");
});

test("every mutation revalidates headers first", async () => {
  const fake = fakeSheets({
    write: () => jsonResponse({
      spreadsheetId: SPREADSHEET,
      updates: { updatedRange: "'Operator Events'!A3:I3", updatedRows: 1, updatedColumns: 9, updatedCells: 9 },
    }),
  });
  await sheetsWith(fake).appendEvent(eventRow());
  const firstWrite = fake.requests.findIndex(r => r.method !== "GET");
  assert.ok(firstWrite > 2, "metadata and header reads must precede the write");
});

test("a header change blocks the write before any mutation is sent", async () => {
  const bad = [...SESSION_HEADERS]; bad[0] = "ID";
  const fake = fakeSheets({ sessionsHeader: bad });
  await assert.rejects(sheetsWith(fake).appendSession(sessionRow()), (e) => {
    assert.equal(e.code, "header_mismatch");
    assert.equal(e.uncertain, false);
    return true;
  });
  assert.equal(fake.requests.filter(r => r.method !== "GET").length, 0);
});

test("updateSessionRow targets the given row with PUT", async () => {
  const fake = fakeSheets({
    write: (path) => {
      assert.match(path, /'Operator Sessions'!A5:P5$/);
      return jsonResponse({
        spreadsheetId: SPREADSHEET, updatedRange: "'Operator Sessions'!A5:P5",
        updatedRows: 1, updatedColumns: 16, updatedCells: 16,
      });
    },
  });
  assert.deepEqual(await sheetsWith(fake).updateSessionRow(5, sessionRow()), { rowNumber: 5 });
  assert.equal(fake.requests.at(-1).method, "PUT");
});

test("updateSessionRow refuses the header row and non-integers", async () => {
  const s = sheetsWith(fakeSheets());
  await assert.rejects(s.updateSessionRow(1, sessionRow()), { code: "invalid_row_number" });
  await assert.rejects(s.updateSessionRow(2.5, sessionRow()), { code: "invalid_row_number" });
});

test("writes reject malformed rows", async () => {
  const s = sheetsWith(fakeSheets());
  await assert.rejects(s.appendSession(["only-one"]), { code: "invalid_row" });
  await assert.rejects(s.appendSession(sessionRow("")), { code: "invalid_row" });
  const numeric = sessionRow(); numeric[3] = 5;
  await assert.rejects(s.appendSession(numeric), { code: "invalid_row" });
  const huge = sessionRow(); huge[3] = "x".repeat(50_001);
  await assert.rejects(s.appendSession(huge), { code: "invalid_row" });
});

test("a write confirmation that does not match is an uncertain error", async () => {
  const fake = fakeSheets({
    write: () => jsonResponse({
      spreadsheetId: SPREADSHEET,
      updates: { updatedRange: "'Operator Sessions'!A7:P7", updatedRows: 2, updatedColumns: 16, updatedCells: 32 },
    }),
  });
  await assert.rejects(sheetsWith(fake).appendSession(sessionRow()), (e) => {
    assert.equal(e.code, "write_confirmation_mismatch");
    assert.equal(e.uncertain, true);
    return true;
  });
});

test("a confirmation for the wrong tab is rejected", async () => {
  const fake = fakeSheets({
    write: () => jsonResponse({
      spreadsheetId: SPREADSHEET,
      updates: { updatedRange: "'Operator Events'!A7:P7", updatedRows: 1, updatedColumns: 16, updatedCells: 16 },
    }),
  });
  await assert.rejects(sheetsWith(fake).appendSession(sessionRow()), { code: "write_confirmation_mismatch" });
});

test("http failures: rejected statuses are certain, server errors are uncertain, body is never leaked", async () => {
  for (const [status, uncertain] of [[400, false], [403, false], [429, true], [500, true], [503, true]]) {
    const fake = fakeSheets({
      write: () => new Response("SECRET-ECHO cell content", { status, headers: { "retry-after": "12" } }),
    });
    await assert.rejects(sheetsWith(fake).appendEvent(eventRow()), (e) => {
      assert.ok(e instanceof OperatorSheetsError);
      assert.equal(e.code, "http_error");
      assert.equal(e.status, status);
      assert.equal(e.uncertain, uncertain, `status ${status}`);
      assert.equal(e.retryAfterSeconds, 12);
      assert.ok(!e.message.includes("SECRET-ECHO"));
      return true;
    });
  }
});

test("retry-after text that is not a number is dropped, and large values are capped", async () => {
  const cases = [["Wed, 21 Oct 2026 07:28:00 GMT", null], ["999999", 3600]];
  for (const [header, expected] of cases) {
    const fake = fakeSheets({ write: () => new Response("", { status: 429, headers: { "retry-after": header } }) });
    await assert.rejects(sheetsWith(fake).appendEvent(eventRow()), e => e.retryAfterSeconds === expected);
  }
});

test("a transport failure on a read is certain, on a write is uncertain", async () => {
  const boom = async () => { throw new Error("secret-token-in-cause"); };
  await assert.rejects(sheetsWith({ fetch: boom }).inspect(), (e) => {
    assert.equal(e.code, "transport_or_response_error");
    assert.equal(e.uncertain, false);
    assert.ok(!String(e.message).includes("secret"));
    assert.equal(e.cause, undefined);
    return true;
  });
  const fake = fakeSheets({ write: () => { throw new Error("socket closed"); } });
  await assert.rejects(sheetsWith(fake).appendEvent(eventRow()), e => e.uncertain === true);
});

test("a request that exceeds the timeout aborts", async () => {
  const hang = (url, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(new Error("aborted")));
  });
  await assert.rejects(sheetsWith({ fetch: hang }, { timeoutMs: 20 }).inspect(),
    { code: "transport_or_response_error" });
});

test("token failures and bad tokens never reach the network", async () => {
  const fake = fakeSheets();
  const failing = new OperatorSheetsGoogle({
    spreadsheetId: SPREADSHEET, sheetIds: IDS, token: async () => { throw new Error("nope"); }, fetch: fake.fetch,
  });
  await assert.rejects(failing.inspect(), { code: "token_unavailable" });
  const injected = new OperatorSheetsGoogle({
    spreadsheetId: SPREADSHEET, sheetIds: IDS, token: async () => "a\r\nX-Evil: 1", fetch: fake.fetch,
  });
  await assert.rejects(injected.inspect(), { code: "invalid_access_token" });
  assert.equal(fake.requests.length, 0);
});

// ---- Token provider ------------------------------------------------------------------------

function serviceAccount() {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", {
    modulusLength: 2048,
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
  return { sa: { type: "service_account", client_email: "exporter@example.invalid", private_key: privateKey }, publicKey };
}

test("token provider signs a verifiable assertion for the fixed endpoint and caches the token", async () => {
  const { sa, publicKey } = serviceAccount();
  let calls = 0; let seen;
  const fetch = async (url, init) => {
    calls += 1; seen = { url, init };
    return jsonResponse({ access_token: "ya29.test", token_type: "Bearer", expires_in: 3600 });
  };
  let clock = 1_800_000_000_000;
  const provider = createServiceAccountTokenProvider({ loadServiceAccount: async () => sa, fetch, now: () => clock });
  const token = provider;
  assert.equal(await token(), "ya29.test");
  assert.equal(await token(), "ya29.test");
  assert.equal(calls, 1);
  assert.equal(seen.url, "https://oauth2.googleapis.com/token");
  const form = new URLSearchParams(seen.init.body);
  assert.equal(form.get("grant_type"), "urn:ietf:params:oauth:grant-type:jwt-bearer");
  const [h, p, s] = form.get("assertion").split(".");
  const verify = createVerify("RSA-SHA256"); verify.update(`${h}.${p}`); verify.end();
  assert.ok(verify.verify(publicKey, s, "base64url"));
  const claims = JSON.parse(Buffer.from(p, "base64url"));
  assert.equal(claims.iss, sa.client_email);
  assert.equal(claims.scope, "https://www.googleapis.com/auth/spreadsheets");
  assert.equal(claims.aud, "https://oauth2.googleapis.com/token");
  assert.equal(claims.sub, undefined, "no user impersonation");
  assert.equal(claims.exp - claims.iat, 3600);
  clock += 3_600_000; // past the early refresh point
  await token();
  assert.equal(calls, 2);
});

test("concurrent token requests share one refresh", async () => {
  const { sa } = serviceAccount();
  let calls = 0;
  const fetch = async () => {
    calls += 1;
    await new Promise(r => setTimeout(r, 10));
    return jsonResponse({ access_token: "t", token_type: "bearer", expires_in: 3600 });
  };
  const provider = createServiceAccountTokenProvider({ loadServiceAccount: async () => sa, fetch });
  const token = provider;
  await Promise.all([token(), token(), token()]);
  assert.equal(calls, 1);
});

test("token provider rejects bad config, bad service accounts and bad responses", async () => {
  assert.throws(() => createServiceAccountTokenProvider({}), { code: "invalid_token_provider_config" });
  const { sa } = serviceAccount();
  const bad = createServiceAccountTokenProvider({ loadServiceAccount: async () => ({ type: "user" }), fetch: async () => {} });
  await assert.rejects(bad(), { code: "invalid_service_account" });
  const loadFails = createServiceAccountTokenProvider({ loadServiceAccount: async () => { throw new Error("x"); }, fetch: async () => {} });
  await assert.rejects(loadFails(), { code: "service_account_load_or_sign_failed" });
  const badKey = createServiceAccountTokenProvider({
    loadServiceAccount: async () => ({ ...sa, private_key: "not a key" }), fetch: async () => {},
  });
  await assert.rejects(badKey(), { code: "service_account_load_or_sign_failed" });
  for (const response of [
    { access_token: "t", token_type: "mac", expires_in: 3600 },
    { access_token: "t\nx", token_type: "Bearer", expires_in: 3600 },
    { access_token: "t", token_type: "Bearer", expires_in: 30 },
    { token_type: "Bearer", expires_in: 3600 },
  ]) {
    const p = createServiceAccountTokenProvider({ loadServiceAccount: async () => sa, fetch: async () => jsonResponse(response) });
    await assert.rejects(p(), { code: "invalid_token_response" });
  }
});

test("a failed refresh is not cached and the next call retries", async () => {
  const { sa } = serviceAccount();
  let calls = 0;
  const fetch = async () => (++calls === 1
    ? new Response("", { status: 500 })
    : jsonResponse({ access_token: "ok", token_type: "Bearer", expires_in: 3600 }));
  const provider = createServiceAccountTokenProvider({ loadServiceAccount: async () => sa, fetch });
  const token = provider;
  await assert.rejects(token(), { code: "http_error" });
  assert.equal(await token(), "ok");
});

test("credential-shaped cell text is refused before any request, naming the field only", async () => {
  const fake = fakeSheets();
  const row = sessionRow();
  row[6] = "see gh" + "p_" + "x".repeat(36); // built in parts: no credential literal in the fixture
  const error = await sheetsWith(fake).appendSession(row).catch(e => e);
  assert.equal(error.code, "credential_in_cell");
  assert.equal(error.field, "Title");
  assert.ok(!error.message.includes("x".repeat(36)));
  assert.equal(fake.requests.length, 0); // refused before validateHeaders or any write
});

test("readRows clamps the range to the grid and returns an empty page beyond it", async () => {
  const fake = fakeSheets();
  const sheets = sheetsWith(fake);
  const rows = await sheets.readRows("sessions", { startRow: 2, limit: 500, rowCount: 3 });
  assert.ok(decodeURIComponent(fake.requests.at(-1).url).includes("A2:P3")); // clamped from P501 to the grid height
  assert.equal(rows.length <= 2, true);
  const sent = fake.requests.length;
  const beyond = await sheets.readRows("sessions", { startRow: 60, limit: 10, rowCount: 50 });
  assert.deepEqual(beyond, []);
  assert.equal(fake.requests.length, sent); // no request for the empty page
});

test("token provider errors keep their status and retry metadata", async () => {
  const fake = fakeSheets();
  const failing = async () => {
    throw new OperatorSheetsError("http_error", { status: 429, retryAfterSeconds: 30 });
  };
  const error = await sheetsWith(fake, { token: failing }).appendSession(sessionRow()).catch(e => e);
  assert.equal(error.code, "http_error");
  assert.equal(error.status, 429);
  assert.equal(error.retryAfterSeconds, 30);
});

test("a batch-validated write does not repeat header validation", async () => {
  const write = () => jsonResponse({
    spreadsheetId: SPREADSHEET,
    updates: { updatedRange: "'Operator Sessions'!A3:P3", updatedRows: 1, updatedColumns: 16, updatedCells: 16 },
  });
  const fake = fakeSheets({ write });
  const sheets = sheetsWith(fake);
  await sheets.validateHeaders();
  fake.requests.length = 0;
  await sheets.appendSession(sessionRow(), { validate: false });
  // No reads of any kind after validation:  the append is the only request, and it is a POST.
  assert.equal(fake.requests.filter(r => r.method === "GET").length, 0);
  assert.equal(fake.requests.length, 1);
  assert.equal(fake.requests[0].method, "POST");
});

test("oversized, deep or malformed responses fail closed", async () => {
  const sheets = sheetsWith({ fetch: async () => new Response("x".repeat(4 * 1024 * 1024 + 1), { status: 200 }), requests: [] });
  await assert.rejects(sheets.readRows("sessions", { startRow: 2, limit: 1 }), { code: "response_too_large" });

  let deep = 1;
  for (let i = 0; i < 40; i++) deep = { nest: deep };
  const deepSheets = sheetsWith({ fetch: async () => jsonResponse(deep), requests: [] });
  await assert.rejects(deepSheets.readRows("sessions", { startRow: 2, limit: 1 }), { code: "invalid_response" });

  const wrongShape = sheetsWith({ fetch: async () => jsonResponse({ values: "not-an-array" }), requests: [] });
  await assert.rejects(wrongShape.readRows("sessions", { startRow: 2, limit: 1 }), { code: "invalid_response" });
});

test("the header contract's three copies stay equal (registry, exporter, adapter)", async () => {
  const { readFileSync } = await import("node:fs");
  const exporter = await import("./agent_sync/operator_sheets_export.mjs");
  const registry = readFileSync(new URL("./agent_sync/operator_registry.py", import.meta.url), "utf8");
  const tuple = (name) => {
    const block = registry.match(new RegExp(name + " = \\((.*?)\\)", "s"));
    assert.ok(block, name + " tuple present in operator_registry.py");
    return [...block[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  };
  assert.deepEqual([...SESSION_HEADERS], [...exporter.SESSIONS_HEADERS]);
  assert.deepEqual([...EVENT_HEADERS], [...exporter.EVENTS_HEADERS]);
  assert.deepEqual([...SESSION_HEADERS], tuple("SESSIONS_HEADERS"));
  assert.deepEqual([...EVENT_HEADERS], tuple("EVENTS_HEADERS"));
});
// ---- OPS-1037: workbook-profile tables (the live SWU Operator workbook shape) ----

const WB_SESSIONS_HEADERS = ['Record ID', 'Label', 'Seat', 'Platform', 'Host', 'Native Session ID',
  'Title', 'Project', 'Thread Link', 'Apple Note Link'];
const WB_TABLES = {
  sessions: { title: 'Sessions', headers: WB_SESSIONS_HEADERS },
  events: { title: 'Events', headers: EVENT_HEADERS },
};
const WB_IDS = { sessions: 20964676, events: 1449318075 };
const WB_SESSION_ROW = ['r1', 'A', 'MM', 'zulip', 'mac', 'native-1', 'Title', '', '', ''];

function fakeWorkbookFetch({ metadataSheets = null } = {}) {
  const sheets = metadataSheets ?? [
    { properties: { sheetId: WB_IDS.sessions, title: 'Sessions', gridProperties: { rowCount: 50, columnCount: 26 } } },
    { properties: { sheetId: WB_IDS.events, title: 'Events', gridProperties: { rowCount: 50, columnCount: 26 } } },
  ];
  const calls = [];
  const fetch = async (url) => {
    calls.push(String(url));
    const parsed = new URL(url);
    const path = decodeURIComponent(parsed.pathname);
    if (!path.includes('/values/')) return jsonResponse({ spreadsheetId: SPREADSHEET, sheets });
    const range = path.slice(path.indexOf('/values/') + 8);
    if (range.endsWith(':append')) {
      const sessions = range.includes('Sessions');
      const cols = sessions ? WB_SESSIONS_HEADERS.length : EVENT_HEADERS.length;
      return jsonResponse({ spreadsheetId: SPREADSHEET, updates: {
        spreadsheetId: SPREADSHEET, tableRange: sessions ? 'Sessions!A1:J1' : 'Events!A1:I1',
        updatedRange: sessions ? 'Sessions!A2:J2' : "'Events'!A2:I2",
        updatedRows: 1, updatedColumns: cols, updatedCells: cols } });
    }
    if (range.startsWith("'Sessions'!") || range.startsWith('Sessions!')) {
      const values = range.endsWith(':Z1') ? [WB_SESSIONS_HEADERS] : [];
      return jsonResponse({ spreadsheetId: SPREADSHEET, range, majorDimension: 'ROWS', values });
    }
    if (range.startsWith("'Events'!") || range.startsWith('Events!')) {
      const values = range.endsWith(':Z1') ? [EVENT_HEADERS] : [];
      return jsonResponse({ spreadsheetId: SPREADSHEET, range, majorDimension: 'ROWS', values });
    }
    throw new Error(`unexpected URL ${url}`);
  };
  return { fetch, calls };
}

test('workbook profile: inspect, validate and appends confirm on real tab titles', async () => {
  const { fetch } = fakeWorkbookFetch();
  const adapter = new OperatorSheetsGoogle({ spreadsheetId: SPREADSHEET, sheetIds: WB_IDS, tables: WB_TABLES,
    token: async () => 'token', fetch });
  const view = await adapter.inspect();
  assert.deepEqual(view, {
    sessions: { rowCount: 50, columnCount: 26 },
    events: { rowCount: 50, columnCount: 26 },
  });
  await adapter.validateHeaders();
  assert.deepEqual(await adapter.appendSession(WB_SESSION_ROW), { rowNumber: 2 });
  assert.deepEqual(await adapter.appendEvent(eventRow()), { rowNumber: 2 });
});

test('workbook profile: a renamed tab fails identity validation', async () => {
  const { fetch } = fakeWorkbookFetch({ metadataSheets: [
    { properties: { sheetId: WB_IDS.sessions, title: 'Session', gridProperties: { rowCount: 50, columnCount: 26 } } },
    { properties: { sheetId: WB_IDS.events, title: 'Events', gridProperties: { rowCount: 50, columnCount: 26 } } },
  ] });
  const adapter = new OperatorSheetsGoogle({ spreadsheetId: SPREADSHEET, sheetIds: WB_IDS, tables: WB_TABLES,
    token: async () => 'token', fetch });
  await assert.rejects(adapter.inspect(), /sheet_identity_mismatch/);
});

test('workbook profile: 16-column canonical rows are refused by the 10-column profile', async () => {
  const { fetch } = fakeWorkbookFetch();
  const adapter = new OperatorSheetsGoogle({ spreadsheetId: SPREADSHEET, sheetIds: WB_IDS, tables: WB_TABLES,
    token: async () => 'token', fetch });
  await assert.rejects(adapter.appendSession(sessionRow()), /invalid_row/);
});
