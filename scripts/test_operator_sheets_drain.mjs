// Tests for the OPS-1037 drain runner's static contract: the workbook profile it
// ships must be the exporter's canonical layout (one copy of the headers), and the
// writer lease must exclude a second live drain while recovering from a stale one.
// Run: node --test scripts/test_operator_sheets_drain.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  drainOutbox, EVENTS_HEADERS, PAYLOAD_HEADERS, projectCells, SESSIONS_HEADERS,
} from "./agent_sync/operator_sheets_export.mjs";
import { OperatorSheetsError, OperatorSheetsGoogle } from "./agent_sync/operator_sheets_google.mjs";
import {
  acquireLease, PACING_DEFAULTS, pacedAdapter, TABLES, workbookProfileFromEnv,
} from "./agent_sync/operator_sheets_drain.mjs";

test("runner profile: workbook identity resolves from env and fails closed", () => {
  const profile = workbookProfileFromEnv({
    OPERATOR_SHEETS_WORKBOOK_ID: "workbook_ABC-123",
    OPERATOR_SHEETS_SESSIONS_SHEET_ID: "1000000001",
    OPERATOR_SHEETS_EVENTS_SHEET_ID: "1000000002",
  });
  assert.deepEqual(profile, {
    spreadsheetId: "workbook_ABC-123",
    sheetIds: { sessions: 1000000001, events: 1000000002 },
  });
  assert.throws(() => workbookProfileFromEnv({}), /OPERATOR_SHEETS_WORKBOOK_ID is required/);
  assert.throws(() => workbookProfileFromEnv({
    OPERATOR_SHEETS_WORKBOOK_ID: "workbook_ABC-123",
    OPERATOR_SHEETS_SESSIONS_SHEET_ID: "5",
    OPERATOR_SHEETS_EVENTS_SHEET_ID: "5",
  }), /distinct/);
});

test("runner profile: the workbook tables are the exporter's canonical headers, not a copy", () => {
  assert.equal(TABLES.sessions.title, "Sessions");
  assert.equal(TABLES.events.title, "Events");
  // The same array objects: there is one header list, owned by the exporter.
  assert.equal(TABLES.sessions.headers, SESSIONS_HEADERS);
  assert.equal(TABLES.events.headers, EVENTS_HEADERS);
  assert.equal(SESSIONS_HEADERS.length, 16);
  assert.equal(EVENTS_HEADERS.length, 9);
  for (const table of ["sessions", "events"]) {
    const layout = TABLES[table].headers;
    assert.deepEqual(layout, PAYLOAD_HEADERS[table]);
    const cells = PAYLOAD_HEADERS[table].map((_, i) => `v${i}`);
    assert.deepEqual(projectCells(table, cells, layout), cells); // nothing dropped
  }
});

test("systemd unit: the service passes no flags the runner ignores", async () => {
  const unit = await readFile(
    new URL("./agent_sync/server/operator-sheets-drain.service", import.meta.url), "utf8");
  const exec = unit.split("\n").find((line) => line.startsWith("ExecStart="));
  assert.ok(exec, "ExecStart present");
  assert.match(exec, /operator_sheets_drain\.mjs$/);
  assert.doesNotMatch(unit, /--once/);
});

test("writer lease: a live holder excludes, a stale lock is stolen", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-"));
  const lock = join(dir, "drain.lock");
  const first = await acquireLease(lock);
  assert.ok(first);
  assert.equal(await acquireLease(lock), null); // our own pid is alive
  await first.close();
  // Simulate a crashed holder: lock file names a pid that cannot exist.
  await writeFile(lock, "999999999");
  const stolen = await acquireLease(lock);
  assert.ok(stolen);
  assert.equal((await readFile(lock, "utf8")).trim(), String(process.pid));
  await stolen.close();
});

// ---- write pacing and HTTP 429 back-off (no real sleeping) ----

// A fake clock that only moves when the injected sleep is called.
function fakeClock(start = 1_000_000) {
  const clock = { t: start, sleeps: [] };
  clock.now = () => clock.t;
  clock.sleep = async (ms) => { clock.sleeps.push(ms); clock.t += ms; };
  return clock;
}

const quota = (retryAfterSeconds = null) =>
  new OperatorSheetsError("http_error", { status: 429, retryAfterSeconds });

// A scripted adapter: each method answers from its own queue (an Error is thrown).
function scriptedAdapter(script = {}) {
  const calls = [];
  const method = (name) => async (...args) => {
    calls.push(name);
    const next = script[name]?.shift();
    if (next instanceof Error) throw next;
    return next ?? { rowNumber: 2 };
  };
  const names = ["inspect", "validateHeaders", "readRows", "appendSession", "updateSessionRow", "appendEvent"];
  return { calls, adapter: Object.fromEntries(names.map((n) => [n, method(n)])) };
}

test("pacing defaults: about 1.1 s between writes, bounded 429 back-off", () => {
  assert.deepEqual({ ...PACING_DEFAULTS }, {
    writeIntervalMs: 1_100, rateLimitRetries: 5, rateLimitBaseDelayMs: 2_000, rateLimitMaxDelayMs: 60_000,
  });
  const { adapter } = scriptedAdapter();
  assert.throws(() => pacedAdapter(adapter, { writeIntervalMs: -1 }), /writeIntervalMs/);
  assert.throws(() => pacedAdapter(adapter, { rateLimitRetries: 11 }), /rateLimitRetries/);
  assert.throws(() => pacedAdapter(adapter, { rateLimitBaseDelayMs: 5_000, rateLimitMaxDelayMs: 1_000 }),
    /rateLimitMaxDelayMs/);
  assert.throws(() => pacedAdapter(adapter, { sleep: "no" }), /functions/);
});

test("429: a quota refusal sleeps, then the same write is retried and succeeds", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ appendEvent: [quota(3), { rowNumber: 7 }] });
  const logs = [];
  const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now, log: (m) => logs.push(m) });
  assert.deepEqual(await paced.appendEvent(["e1"], { validate: false }), { rowNumber: 7 });
  assert.deepEqual(calls, ["appendEvent", "appendEvent"]);
  assert.deepEqual(clock.sleeps, [3_000]); // Retry-After (3 s) beats the 2 s base
  assert.match(logs[0], /429 quota refusal, nothing written; retry 1 of 5 in 3000 ms/);
});

test("429: repeated refusals stop at the bound, with exponential capped sleeps", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ appendSession: [quota(), quota(), quota(), quota(), quota()] });
  // Pacing off here: a retry is still a write, and a 1 s back-off would otherwise be
  // topped up to the 1.1 s write spacing.
  const paced = pacedAdapter(adapter, {
    sleep: clock.sleep, now: clock.now, writeIntervalMs: 0,
    rateLimitRetries: 3, rateLimitBaseDelayMs: 1_000, rateLimitMaxDelayMs: 3_000,
  });
  const error = await paced.appendSession(["r1"]).catch((e) => e);
  assert.equal(error.status, 429);
  assert.equal(error.uncertain, false);
  assert.equal(calls.length, 4); // the first try plus three retries
  assert.deepEqual(clock.sleeps, [1_000, 2_000, 3_000]);
});

test("429: a huge Retry-After is capped at the maximum delay", async () => {
  const clock = fakeClock();
  const { adapter } = scriptedAdapter({ appendEvent: [quota(3600)] });
  await pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now }).appendEvent(["e1"]);
  assert.deepEqual(clock.sleeps, [60_000]);
});

test("429: reads refused for quota are retried too, without write pacing", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ inspect: [quota(), { sessions: { rowCount: 1 } }] });
  const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now });
  assert.deepEqual(await paced.inspect(), { sessions: { rowCount: 1 } });
  assert.deepEqual(calls, ["inspect", "inspect"]);
  assert.deepEqual(clock.sleeps, [2_000]);
});

test("429: uncertain writes and every other failure go straight back to the drain", async () => {
  const failures = [
    new OperatorSheetsError("http_error", { status: 429, uncertain: true }), // never retried if uncertain
    new OperatorSheetsError("http_error", { status: 503, uncertain: true }),
    new OperatorSheetsError("transport_or_response_error", { uncertain: true }),
    new OperatorSheetsError("header_mismatch"),
  ];
  for (const failure of failures) {
    const clock = fakeClock();
    const { adapter, calls } = scriptedAdapter({ appendEvent: [failure] });
    const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now });
    await assert.rejects(paced.appendEvent(["e1"]), (e) => e === failure);
    assert.equal(calls.length, 1, `${failure.code} ${failure.status} sent once`);
    assert.deepEqual(clock.sleeps, []);
  }
});

test("pacing: writes are spaced by the interval; time already spent counts; reads are not paced", async () => {
  const clock = fakeClock();
  const { adapter } = scriptedAdapter();
  const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now });
  await paced.appendSession(["r1"]);
  assert.deepEqual(clock.sleeps, []); // the first write never waits
  await paced.updateSessionRow(2, ["r1"]);
  await paced.appendEvent(["e1"]);
  assert.deepEqual(clock.sleeps, [1_100, 1_100]); // the default interval, across write kinds
  clock.t += 800;
  await paced.appendEvent(["e2"]);
  assert.deepEqual(clock.sleeps, [1_100, 1_100, 300]);
  clock.t += 5_000;
  await paced.appendEvent(["e3"]);
  await paced.readRows("events", { startRow: 2, limit: 1 });
  await paced.validateHeaders();
  assert.equal(clock.sleeps.length, 3); // past the interval, and reads go out unpaced
});

// End to end through drainOutbox and the real transport, with a scripted Sheets API.
function fakeWorkbook({ appendStatuses = [] } = {}) {
  const sheetIds = { sessions: 1000000001, events: 1000000002 };
  const appends = [];
  const respond = (body, status = 200) => new Response(JSON.stringify(body), { status });
  const fetch = async (url, init = {}) => {
    const path = decodeURIComponent(new URL(url).pathname);
    if (!path.includes("/values/")) {
      return respond({ sheets: [
        { properties: { sheetId: sheetIds.sessions, title: "Sessions", gridProperties: { rowCount: 2, columnCount: 16 } } },
        { properties: { sheetId: sheetIds.events, title: "Events", gridProperties: { rowCount: 2, columnCount: 9 } } },
      ] });
    }
    const range = path.slice(path.indexOf("/values/") + 8);
    if (range.endsWith(":append")) {
      appends.push(JSON.parse(init.body).values[0]);
      const status = appendStatuses.shift();
      if (status) return new Response("", { status });
      return respond({ spreadsheetId: "wb_1", updates: {
        updatedRange: "'Sessions'!A2:P2", updatedRows: 1, updatedColumns: 16, updatedCells: 16,
      } });
    }
    if (range === "'Sessions'!A1:P1") return respond({ values: [SESSIONS_HEADERS] });
    if (range === "'Events'!A1:I1") return respond({ values: [EVENTS_HEADERS] });
    return respond({ values: [] });
  };
  return { sheetIds, fetch, appends };
}

function oneSessionOutbox() {
  const cells = ["rec-1", "L", "CLAUDE", "code", "srv", "n1", "T", "P", "", "", "open", "none",
    "2026-11-07T16:00:00Z", "", "", "test"];
  const state = { pending: [{ outbox_id: "ob-1", kind: "session_upsert", payload: { cells } }], marked: [] };
  return {
    state,
    outbox: {
      pending: async () => state.pending,
      markExported: async (ids) => { state.marked.push(...ids); },
    },
  };
}

test("drain: a 429 on a 16-column session append is retried and the row exports", async () => {
  const clock = fakeClock();
  const book = fakeWorkbook({ appendStatuses: [429] });
  const { outbox, state } = oneSessionOutbox();
  const adapter = pacedAdapter(new OperatorSheetsGoogle({
    spreadsheetId: "wb_1", sheetIds: book.sheetIds, tables: TABLES, token: async () => "tok", fetch: book.fetch,
  }), { sleep: clock.sleep, now: clock.now });
  const res = await drainOutbox({ outbox, adapter, layout: { sessions: SESSIONS_HEADERS, events: EVENTS_HEADERS } });
  assert.deepEqual(res, { exported: 1, failed: 0 });
  assert.deepEqual(state.marked, ["ob-1"]);
  assert.equal(book.appends.length, 2); // refused once, then written
  assert.equal(book.appends[1].length, 16); // the whole canonical row, nothing projected away
  assert.deepEqual(clock.sleeps, [2_000]);
});

test("drain: a genuinely uncertain write still stops the drain for reconcile-by-id", async () => {
  const clock = fakeClock();
  const book = fakeWorkbook({ appendStatuses: [503] });
  const { outbox, state } = oneSessionOutbox();
  const adapter = pacedAdapter(new OperatorSheetsGoogle({
    spreadsheetId: "wb_1", sheetIds: book.sheetIds, tables: TABLES, token: async () => "tok", fetch: book.fetch,
  }), { sleep: clock.sleep, now: clock.now });
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 0, failed: 1 });
  assert.deepEqual(state.marked, []); // left pending: the next drain reconciles by Record ID
  assert.equal(book.appends.length, 1); // never resent blind
  assert.deepEqual(clock.sleeps, []);
});
