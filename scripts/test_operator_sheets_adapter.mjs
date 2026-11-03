// Tests for simple_tools/operator_sheets_adapter.mjs (AFC-428).
// Run: node --test scripts/test_operator_sheets_adapter.mjs
// Everything is faked:  no network, no service account, no token.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createAdapter } from "./simple_tools/operator_sheets_adapter.mjs";
import { SESSIONS_HEADERS, EVENTS_HEADERS } from "./simple_tools/operator_sheets_export.mjs";

const SID = "test-spreadsheet";

// A scripted fetch:  handlers match [method, path-prefix] in order of registration.
function fakeFetch(handlers) {
  const calls = [];
  const fetchImpl = async (url, opts = {}) => {
    const method = opts.method ?? "GET";
    calls.push({ method, url, body: opts.body ? JSON.parse(opts.body) : undefined });
    const path = url.includes("/v4/spreadsheets/")
      ? url.slice(url.indexOf(SID) + SID.length) : url;
    for (const h of handlers) {
      const match = h.exact ? path === h.path : path.startsWith(h.path);
      if (h.method === method && match) {
        const body = typeof h.data === "function" ? h.data(calls.length - 1) : h.data;
        return { ok: h.ok ?? true, status: h.status ?? 200, json: async () => body };
      }
    }
    return { ok: false, status: 404,
      json: async () => ({ error: { message: `unfaked ${method} ${path}` } }) };
  };
  return { calls, fetchImpl };
}

const WORKBOOK_META = {
  properties: { title: "Fleet Reference" },
  sheets: [
    { properties: { title: "Reference", sheetId: 2033000101 } },
    { properties: { title: "Sessions", sheetId: 2033000102 } },
    { properties: { title: "Events", sheetId: 2033000103 } },
  ],
};
const metaHandler = { method: "GET", path: "", exact: true, data: WORKBOOK_META };

const tokenProvider = async () => "test-token";

test("validateHeaders verifies the workbook identity, then both header rows", async () => {
  const { fetchImpl, calls } = fakeFetch([
    metaHandler,
    { method: "GET", path: "/values/Sessions!A1:P1",
      data: { range: "Sessions!A1:P1", values: [SESSIONS_HEADERS] } },
    { method: "GET", path: "/values/Events!A1:I1",
      data: { range: "Events!A1:I1", values: [EVENTS_HEADERS] } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  await adapter.validateHeaders();
  assert.equal(calls[0].url, `https://sheets.googleapis.com/v4/spreadsheets/${SID}`,
    "workbook identity is verified before any header read");
});

test("validateHeaders refuses a wrong workbook (tab sheetId mismatch)", async () => {
  const wrong = { ...WORKBOOK_META,
    sheets: [...WORKBOOK_META.sheets.slice(0, 1),
      { properties: { title: "Sessions", sheetId: 999 } }] };
  const { fetchImpl } = fakeFetch([{ method: "GET", path: "", exact: true, data: wrong }]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  await assert.rejects(() => adapter.validateHeaders(), /not the fleet reference/);
});

test("validateHeaders refuses header drift before any write", async () => {
  const drifted = [...SESSIONS_HEADERS];
  drifted[0] = "Record Id";
  const { fetchImpl } = fakeFetch([
    metaHandler,
    { method: "GET", path: "/values/Sessions!A1:P1",
      data: { range: "Sessions!A1:P1", values: [drifted] } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  await assert.rejects(() => adapter.validateHeaders(), /header drift/);
});

test("readRows pages carry 1-based sheet row numbers", async () => {
  const { fetchImpl } = fakeFetch([
    { method: "GET", path: "/values/Sessions!A7:P206",
      data: { range: "Sessions!A7:P206", values: [["r1", "A"], ["r2", "B"]] } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  const page = await adapter.readRows("sessions", { startRow: 7, limit: 200 });
  assert.deepEqual(page.rows, [
    { rowNumber: 7, cells: ["r1", "A"] },
    { rowNumber: 8, cells: ["r2", "B"] },
  ]);
});

test("readRows: a 2xx without the echoed range is malformed, never an empty table", async () => {
  const { fetchImpl } = fakeFetch([
    { method: "GET", path: "/values/Sessions!A2:P201", data: {} },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  await assert.rejects(() => adapter.readRows("sessions"), /not Sessions/);
});

test("readRows: a proven-empty page (echoed range, no values) is empty", async () => {
  const { fetchImpl } = fakeFetch([
    { method: "GET", path: "/values/Sessions!A2:P201",
      data: { range: "Sessions!A2:P201" } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  assert.deepEqual((await adapter.readRows("sessions")).rows, []);
});

test("readRows rejects an unknown table", async () => {
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl: fakeFetch([]).fetchImpl,
    tokenProvider });
  await assert.rejects(() => adapter.readRows("reference"), /unknown table/);
});

test("appends write RAW with INSERT_ROWS and return the confirmed row number", async () => {
  const { fetchImpl, calls } = fakeFetch([
    { method: "POST", path: "/values/Sessions!A1:append",
      data: { updates: { updatedRange: "Sessions!A37:P37", updatedCells: 16 } } },
    { method: "POST", path: "/values/Events!A1:append",
      data: { updates: { updatedRange: "Events!A410:I410", updatedCells: 9 } } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  assert.equal(await adapter.appendSession(["r1", "A"]), 37);
  assert.equal(await adapter.appendEvent(["e1"]), 410);
  for (const c of calls) {
    assert.ok(c.url.includes("valueInputOption=RAW"));
    assert.ok(c.url.includes("insertDataOption=INSERT_ROWS"));
  }
  assert.deepEqual(calls[0].body.values, [["r1", "A"]]);
});

test("an append with a malformed 2xx is an unconfirmed write, never an acknowledgement", async () => {
  for (const data of [
    {},                                                      // empty object
    { updates: {} },                                         // no updatedRange
    { updates: { updatedRange: "OtherTab!A3:P3", updatedCells: 16 } },  // wrong tab
    { updates: { updatedRange: "Sessions!A37:P37" } },       // no updatedCells
  ]) {
    const { fetchImpl } = fakeFetch([
      { method: "POST", path: "/values/Sessions!A1:append", data },
    ]);
    const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
    await assert.rejects(() => adapter.appendSession(["r1"]), /unconfirmed/,
      JSON.stringify(data));
  }
});

test("updateSessionRow requires the exact echoed range and full cell count", async () => {
  const { fetchImpl, calls } = fakeFetch([
    { method: "PUT", path: "/values/Sessions!A12:P12",
      data: { range: "Sessions!A12:P12", updatedRange: "Sessions!A12:P12",
        updatedCells: 2 } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  assert.equal(await adapter.updateSessionRow(12, ["r1", "A"]), 12);
  assert.ok(calls[0].url.includes("valueInputOption=RAW"));
});

test("updateSessionRow: a partial or wrong echo is an unconfirmed write", async () => {
  for (const data of [
    { range: "Sessions!A12:P12", updatedRange: "Sessions!A12:P12", updatedCells: 3 },
    { range: "Sessions!A12:P12", updatedRange: "Sessions!A13:P13", updatedCells: 16 },
    { updatedRange: "Sessions!A12:P12", updatedCells: 16 },  // no range echo
  ]) {
    const { fetchImpl } = fakeFetch([
      { method: "PUT", path: "/values/Sessions!A12:P12", data },
    ]);
    const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
    await assert.rejects(() => adapter.updateSessionRow(12, ["r1"]),
      /unconfirmed|not Sessions/, JSON.stringify(data));
  }
  await assert.rejects(
    () => createAdapter({ spreadsheetId: SID, fetchImpl: fakeFetch([]).fetchImpl,
      tokenProvider }).updateSessionRow(1, ["x"]), /data row/);
});

test("a malformed JSON body on a 2xx is a failure, not a success", async () => {
  const fetchImpl = async () => ({ ok: true, status: 200,
    json: async () => { throw new SyntaxError("bad json"); } });
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  await assert.rejects(() => adapter.readRows("sessions"), /malformed JSON/);
});

test("an API error surfaces its message without the token or headers", async () => {
  const { fetchImpl } = fakeFetch([
    { method: "GET", path: "", exact: true, ok: false, status: 403,
      data: { error: { message: "The caller does not have permission" } } },
  ]);
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider });
  try {
    await adapter.validateHeaders();
    assert.fail("should have thrown");
  } catch (err) {
    assert.match(err.message, /does not have permission/);
    assert.ok(!err.message.includes("test-token"), "the token never appears in an error");
    assert.ok(!err.message.includes("Authorization"), "the auth header never appears");
  }
});

test("a 401 refreshes the token and retries exactly once", async () => {
  let tokens = 0;
  const freshProvider = async () => { tokens += 1; return `token-${tokens}`; };
  const auths = [];
  const fetchImpl = async (url, opts = {}) => {
    auths.push(opts.headers.Authorization);
    if (auths.length === 1)
      return { ok: false, status: 401, json: async () => ({ error: { message: "unauthorized" } }) };
    return { ok: true, status: 200,
      json: async () => ({ range: "Sessions!A2:P201" }) };
  };
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider: freshProvider });
  await adapter.readRows("sessions");
  assert.deepEqual(auths, ["Bearer token-1", "Bearer token-2"],
    "the cached token was invalidated and refreshed");
});

test("a second 401 does not retry again", async () => {
  const fetchImpl = async () => ({ ok: false, status: 401,
    json: async () => ({ error: { message: "unauthorized" } }) });
  let calls = 0;
  const counting = async (url, opts) => { calls += 1; return fetchImpl(url, opts); };
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl: counting, tokenProvider });
  await assert.rejects(() => adapter.readRows("sessions"), /unauthorized/);
  assert.equal(calls, 2, "one retry only");
});

test("an expiring cached token is refreshed (the --loop case)", async () => {
  let now = 1000;
  let issued = 0;
  const provider = async () => { issued += 1; return { token: `t${issued}`, expiresAt: now + 60 }; };
  const fetchImpl = async () => ({ ok: true, status: 200,
    json: async () => ({ range: "Sessions!A2:P201" }) });
  const adapter = createAdapter({ spreadsheetId: SID, fetchImpl, tokenProvider: provider,
    nowImpl: () => now });
  await adapter.readRows("sessions");
  await adapter.readRows("sessions");
  assert.equal(issued, 1, "within expiry:  cached");
  now += 61; // past the early-refresh horizon
  await adapter.readRows("sessions");
  assert.equal(issued, 2, "past expiry:  refreshed");
});

test("the default binding is the fleet reference workbook", () => {
  const adapter = createAdapter({ env: {}, tokenProvider });
  assert.equal(adapter.spreadsheetId, "1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY");
});

test("credential custody: Infisical fleet-secrets only, scrubbed child env, no env fallback", async () => {
  // GOOGLE_SHEETS_SA in the ambient environment must NOT be consulted.
  const seen = [];
  const execImpl = (bin, args, opts) => {
    seen.push({ bin, args, env: opts.env });
    throw new Error("not found"); // no secret in this test
  };
  const fetchImpl = fakeFetch([]).fetchImpl;
  const adapter = createAdapter({
    spreadsheetId: SID,
    env: { GOOGLE_SHEETS_SA: '{"client_email":"ambient@example.com"}', FLEET_SECRETS: "/bin/fleet-secrets" },
    execFileSyncImpl: execImpl,
    fetchImpl,
    // No tokenProvider:  force the real custody path (which will fail at fleet-secrets).
  });
  await assert.rejects(() => adapter.readRows("sessions"), /Infisical/);
  assert.equal(seen.length, 1, "fleet-secrets was consulted");
  assert.deepEqual(seen[0].args, ["get", "GOOGLE_SHEETS_SA", "--path", "/google-sheets-sa"]);
  assert.deepEqual(Object.keys(seen[0].env), ["PATH"],
    "the secrets child inherits no ambient environment");
});
