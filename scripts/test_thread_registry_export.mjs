// Tests for the thread registry's Google drain (thread_registry_export.mjs), the
// module the Operator Sheets drain runner schedules on the server (AFC-823): the
// adapter's bounds and error contract, the read of the whole grid, the key re-read and
// the Purpose-safe update, oversized rows, the stop hook between writes, the upsert plan
// (kept in lockstep with thread_registry.py's plan_upserts), the payload checks and the
// CLI's dry-run default and writer lease.
// No network: fetch is injected, and importing the module runs nothing.
// Run: node --test scripts/test_thread_registry_export.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  DEFAULT_LOCK, drainThreadRows, ERROR_BODY_READ_BYTES, KEY_CHECK_CHUNK, makeSheetsAdapter,
  MAX_CELL_CHARS, MAX_READ_ROWS, planUpserts, rowFitsSheet, runCli, SHEET_TAB,
  SHEETS_CALL_TIMEOUT_MS, SPREADSHEET_ID, THREAD_HEADERS, USAGE, validatePayload,
} from "./simple_tools/thread_registry_export.mjs";

// An HTTP failure's message carries the body's detail for diagnosis (#2197), redacted
// and capped by redactForError:  the detail shows, the credential never does.  The drain
// runner logs only fixed codes, never a message (test_operator_sheets_drain.mjs).
const BODY_DETAIL = "PERMISSION_DENIED google-error-detail";
const BODY_SECRET_VALUE = "ya29.secret-never-in-a-message";
const ADAPTER_TOKEN_VALUE = "adapter-token-fixture";

const row = (key, { purpose = "", stamp = "2026-11-17T18:00:00Z", count = "1" } = {}) => [
  key, "Work-In-Progress", "10", `topic ${key}`, "active", "CLAUDE", "1", count,
  "2026-11-17T17:00:00Z", "2026-11-17T17:30:00Z", "1001", "", "", purpose, stamp,
];

const json = (body, init = {}) => new Response(JSON.stringify(body), { status: 200, ...init });

// An in-memory tab with the adapter surface drainThreadRows expects.  Like the real
// adapter, an update never writes the Thread Key and writes Purpose only when asked.
// onKeys runs before each key re-read, so a test can change the tab between the read
// and the writes the way a human would; keys: false leaves readKeys out.
function memorySheet({
  headers = THREAD_HEADERS, rows = [], fail = [], keys = true, onKeys = () => {},
} = {}) {
  const state = { rows: rows.map((r) => [...r]), writes: [], updateOptions: [], keyReads: [] };
  const write = (kind, apply) => async (...args) => {
    state.writes.push(kind);
    const next = fail.shift();
    if (next instanceof Error) throw next;
    return apply(...args);
  };
  const adapter = {
    validateHeaders: async () => [...headers],
    readRows: async () => state.rows.map((cells, i) => ({ rowNumber: i + 2, cells: [...cells] })),
    appendRow: write("append", (cells) => {
      state.rows.push([...cells]);
      return state.rows.length + 1;
    }),
    updateRow: write("update", (rowNumber, cells, options = {}) => {
      state.updateOptions.push(options);
      const current = state.rows[rowNumber - 2] ?? [];
      const next = [...cells];
      next[0] = current[0] ?? "";
      if (!options.writePurpose) next[13] = current[13] ?? "";
      state.rows[rowNumber - 2] = next;
    }),
  };
  if (keys) {
    adapter.readKeys = async (rowNumbers) => {
      onKeys(state);
      state.keyReads.push([...rowNumbers]);
      return rowNumbers.map((r) => state.rows[r - 2]?.[0] ?? "");
    };
  }
  return { state, adapter };
}

// A fake Sheets API over one in-memory tab, answering the way Google does:  the grid row
// count from the workbook metadata, value pages without their trailing blank rows (a
// blank row inside a page comes back as []), appends at the end, batchGet and
// batchUpdate.  A grid entry of null is a blank row.  calls names each request.
function fakeSheetsApi(grid, { blankGridRows = 0 } = {}) {
  const api = { grid: grid.map((cells) => (cells ? [...cells] : [])), calls: [], bodies: [] };
  const rowCount = () => api.grid.length + 1 + blankGridRows;
  const column = (letter) => letter.charCodeAt(0) - 65;
  api.fetchImpl = async (url, init = {}) => {
    const u = new URL(url);
    const path = decodeURIComponent(u.pathname);
    if (path.endsWith(":append")) {
      api.calls.push("append");
      api.grid.push([...JSON.parse(init.body).values[0]]);
      const n = api.grid.length + 1;
      return json({ updates: { updatedRange: `'${SHEET_TAB}'!A${n}:O${n}` } });
    }
    if (path.endsWith("/values:batchUpdate")) {
      api.calls.push("batchUpdate");
      const body = JSON.parse(init.body);
      api.bodies.push(body);
      for (const { range, values } of body.data) {
        const [, letter, at] = /!([A-Z])(\d+):/.exec(range);
        const target = api.grid[Number(at) - 2];
        values[0].forEach((cell, i) => { target[column(letter) + i] = cell; });
      }
      return json({});
    }
    if (path.endsWith("/values:batchGet")) {
      api.calls.push("batchGet");
      return json({
        valueRanges: u.searchParams.getAll("ranges").map((range) => {
          const at = Number(/!A(\d+)$/.exec(range)[1]);
          const key = api.grid[at - 2]?.[0];
          return { range: `'${SHEET_TAB}'!A${at}`, ...(key ? { values: [[key]] } : {}) };
        }),
      });
    }
    if (!path.includes("/values")) {
      api.calls.push("meta");
      return json({
        sheets: [
          { properties: { title: "Notes", gridProperties: { rowCount: 3 } } },
          { properties: { title: SHEET_TAB, gridProperties: { rowCount: rowCount() } } },
        ],
      });
    }
    const [start, end] = /!A(\d+):O(\d+)$/.exec(path).slice(1).map(Number);
    if (start === 1) {
      api.calls.push("header");
      return json({ values: [THREAD_HEADERS] });
    }
    api.calls.push("page");
    const values = api.grid.slice(start - 2, end - 1).map((cells) => [...cells]);
    while (values.length && values.at(-1).length === 0) values.pop();
    return json({ values });
  };
  return api;
}

// ---- makeSheetsAdapter: bounded calls and the error contract the drain's pacing reads ----

test("adapter: each call has a timeout signal, no redirects and the token", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url, init });
    return json({ values: [THREAD_HEADERS] });
  };
  const adapter = makeSheetsAdapter("tok", "workbook_fixture", SHEET_TAB, { fetchImpl });
  assert.deepEqual(await adapter.validateHeaders(), THREAD_HEADERS);
  assert.equal(seen.length, 1);
  assert.ok(seen[0].url.startsWith(
    "https://sheets.googleapis.com/v4/spreadsheets/workbook_fixture/values/"));
  assert.ok(seen[0].init.signal instanceof AbortSignal);
  assert.equal(seen[0].init.redirect, "error");
  assert.equal(seen[0].init.headers.Authorization, "Bearer tok");
  assert.equal(SHEETS_CALL_TIMEOUT_MS, 30_000);
});

test("adapter: a call that never answers is cut off at the timeout", async () => {
  const hang = (url, init) => new Promise((resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason));
  });
  const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: hang, timeoutMs: 20 });
  const write = await adapter.appendRow(row("10:a")).catch((e) => e);
  assert.equal(write.code, "transport_or_response_error");
  assert.equal(write.uncertain, true); // a write that may have landed
  const read = await adapter.validateHeaders().catch((e) => e);
  assert.equal(read.code, "transport_or_response_error");
  assert.equal(read.uncertain, false);
});

test("adapter: HTTP failures carry code, status, certainty and a numeric Retry-After", async () => {
  const cases = [
    // [call, status, Retry-After header, uncertain, retryAfterSeconds]
    ["append", 429, "7", false, 7],
    ["append", 503, null, true, null],
    ["update", 403, null, false, null],
    ["update", 500, null, true, null],
    ["read", 500, "Wed, 21 Oct 2026 07:28:00 GMT", false, null],
    ["append", 429, "999999", false, 3600],
  ];
  for (const [call, status, retry, uncertain, retryAfterSeconds] of cases) {
    const headers = retry === null ? {} : { "retry-after": retry };
    const body = `${BODY_DETAIL} {"access_token": "${BODY_SECRET_VALUE}"}`;
    const fetchImpl = async () => new Response(body, { status, headers });
    const adapter = makeSheetsAdapter(ADAPTER_TOKEN_VALUE, "wb", SHEET_TAB, { fetchImpl });
    const run = {
      append: () => adapter.appendRow(row("10:a")),
      update: () => adapter.updateRow(2, row("10:a")),
      read: () => adapter.readRows(),
    }[call];
    const err = await run().catch((e) => e);
    const label = `${call} ${status}`;
    assert.equal(err.code, "http_error", label);
    assert.equal(err.status, status, label);
    assert.equal(err.uncertain, uncertain, label);
    assert.equal(err.retryAfterSeconds, retryAfterSeconds, label);
    assert.ok(err.message.includes(` -> ${status}: ${BODY_DETAIL} `), label);
    assert.match(err.message, /"access_token": "\[redacted\]"/, label);
    assert.doesNotMatch(err.message, /secret-never-in-a-message|adapter-token-fixture/, label);
  }
});

// The stalled body below ends when the call's signal aborts, the way fetch's does, and
// the test has its own timeout, so a read outside the timer fails instead of hanging.
test("adapter: a failure's body is read bounded, under the call's timer, or left out", {
  timeout: 10_000,
}, async () => {
  const encode = (text) => new TextEncoder().encode(text);
  // A body four times the read bound:  the read stops near ERROR_BODY_READ_BYTES, the
  // rest is never pulled, and the detail is capped.
  const chunks = (4 * ERROR_BODY_READ_BYTES) / 1024;
  let pulled = 0;
  const huge = async () => new Response(new ReadableStream({
    pull(controller) {
      pulled += 1;
      controller.enqueue(encode("z".repeat(1024)));
      if (pulled === chunks) controller.close();
    },
  }), { status: 500 });
  const big = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: huge });
  const capped = await big.validateHeaders().catch((e) => e);
  assert.equal(capped.code, "http_error");
  assert.match(capped.message, / -> 500: z{500}\.\.\.$/);
  assert.ok(pulled <= ERROR_BODY_READ_BYTES / 1024 + 2, `pulled ${pulled} of ${chunks} chunks`);
  // A body that stalls is cut off by the call's one timer:  the HTTP error stands, with
  // no detail and no other error in its place.
  const stalled = async (url, init) => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encode("partial body "));
      init.signal.addEventListener("abort", () => controller.error(init.signal.reason));
    },
  }), { status: 503 });
  const slow = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: stalled, timeoutMs: 20 });
  const cut = await slow.appendRow(row("10:a")).catch((e) => e);
  assert.equal(cut.code, "http_error");
  assert.equal(cut.status, 503);
  assert.equal(cut.uncertain, true);
  assert.match(cut.message, / -> 503$/);
  // A body stream that fails gives an empty detail, never its own error.
  const broken = async () => new Response(new ReadableStream({
    start(controller) {
      controller.error(new Error("stream blew up"));
    },
  }), { status: 500 });
  const bad = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: broken });
  const failed = await bad.validateHeaders().catch((e) => e);
  assert.equal(failed.code, "http_error");
  assert.match(failed.message, / -> 500$/);
});

// A credential that starts inside the visible detail and runs past the read bound loses
// its end (a closing quote, an END line, a third JWT part), so the usual patterns would
// miss it.  A body cut at the bound has such a trailing credential redacted first.
test("adapter: a credential cut by the read bound is still redacted", async () => {
  const encode = (text) => new TextEncoder().encode(text);
  const streamed = (text) => async () => {
    let at = 0;
    return new Response(new ReadableStream({
      pull(controller) {
        if (at >= text.length) {
          controller.close();
          return;
        }
        controller.enqueue(encode(text.slice(at, at + 1024)));
        at += 1024;
      },
    }), { status: 500 });
  };
  const long = 4 * ERROR_BODY_READ_BYTES;
  // The key-block markers are built here, so this file holds no literal key block.
  const fence = "-".repeat(5);
  const keyLabel = ["PRIVATE", "KEY"].join(" ");
  const begin = `${fence}BEGIN ${keyLabel}${fence}`;
  const end = `${fence}END ${keyLabel}${fence}`;
  const cases = [
    ["json field", `{"error": {"session_token": "${"s".repeat(long)}"}}`, /s{20}/],
    ["key block", `${begin}\n${"K".repeat(long)}\n${end}`, /K{20}/],
    ["jwt", `token eyJ${"a".repeat(long)}.eyJbody.sig`, /a{20}/],
  ];
  for (const [label, body, leak] of cases) {
    const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: streamed(body) });
    const err = await adapter.validateHeaders().catch((e) => e);
    assert.equal(err.code, "http_error", label);
    assert.doesNotMatch(err.message, leak, label);
    assert.match(err.message, /\[redacted\]/, label);
  }
  // A body that ends inside the bound is not touched by the cut patterns:  a closed
  // field is redacted by the usual pattern and the text after it stays.
  const whole = streamed('{"session_token": "short"} trailing words');
  const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: whole });
  const err = await adapter.validateHeaders().catch((e) => e);
  assert.match(err.message, /"session_token": "\[redacted\]"\} trailing words$/);
});

test("adapter: readRows reads to the grid's last row, stops on request, has a bound", async () => {
  // 501 rows in a 1,000-row grid: two pages, the second clamped to the grid.
  const grid = Array.from({ length: 501 }, (_, i) => row(`10:p${i}`));
  const api = fakeSheetsApi(grid, { blankGridRows: 498 });
  const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: api.fetchImpl });
  const rows = await adapter.readRows();
  assert.equal(rows.length, 501);
  assert.equal(rows[500].rowNumber, 502);
  assert.deepEqual(api.calls, ["meta", "page", "page"]);
  api.calls.length = 0;
  const stopped = makeSheetsAdapter("tok", "wb", SHEET_TAB, {
    fetchImpl: api.fetchImpl, shouldStop: () => true,
  });
  const err = await stopped.readRows().catch((e) => e);
  assert.equal(err.code, "stopped"); // never a partial index the plan would trust
  assert.deepEqual(api.calls, ["meta", "page"]);
  // Plain response objects, so 200 full pages cost no JSON round trips.
  const fullPage = Array.from({ length: 500 }, (_, i) => row(`10:p${i}`));
  let pages = 0;
  const grid200 = (rowCount) => async (url) => {
    if (!new URL(url).pathname.includes("/values")) {
      const sheets = [{ properties: { title: SHEET_TAB, gridProperties: { rowCount } } }];
      return { ok: true, json: async () => ({ sheets }) };
    }
    pages += 1;
    return { ok: true, json: async () => ({ values: fullPage }) };
  };
  const over = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: grid200(MAX_READ_ROWS + 2) });
  assert.equal((await over.readRows().catch((e) => e)).code, "read_limit");
  assert.equal(pages, 0); // refused from the metadata, before any page
  const full = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: grid200(MAX_READ_ROWS + 1) });
  assert.equal((await full.readRows()).length, MAX_READ_ROWS);
  assert.equal(pages, MAX_READ_ROWS / 500);
});

test("adapter: no tab, two tabs or no row count in the metadata fails closed", async () => {
  const answers = [
    { sheets: [] },
    { sheets: [{ properties: { title: SHEET_TAB, gridProperties: { rowCount: 9 } } },
      { properties: { title: SHEET_TAB, gridProperties: { rowCount: 9 } } }] },
    { sheets: [{ properties: { title: SHEET_TAB } }] },
    {},
  ];
  for (const answer of answers) {
    const fetchImpl = async () => json(answer);
    const err = await makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl }).readRows()
      .catch((e) => e);
    assert.equal(err.code, "grid_unknown", JSON.stringify(answer));
  }
});

test("adapter: a blank row at a page's end hides nothing below it; nothing is appended twice",
  async () => {
    // 700 rows; a human cleared row 501, the last row of the first page, which Google then
    // leaves out of that page.  Paging to the grid's row count still reaches rows 502-701.
    const grid = Array.from({ length: 700 }, (_, i) => (i === 499 ? null : row(`10:r${i}`)));
    const api = fakeSheetsApi(grid);
    const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: api.fetchImpl });
    const desired = [...grid.filter(Boolean).map((cells) => [...cells]), row("10:new")];
    const first = await drainThreadRows({ adapter, desired });
    assert.equal(first.appended, 1);
    assert.equal(first.unchanged, 699);
    const second = await drainThreadRows({ adapter, desired });
    assert.equal(second.appended + second.updated, 0);
    assert.equal(second.unchanged, 700);
    assert.equal(api.grid.filter((cells) => cells.length).length, 700); // no duplicate rows
  });

test("adapter: an update writes B:M and O, never the key, and Purpose only when asked",
  async () => {
    const api = fakeSheetsApi([row("10:a", { purpose: "typed by a human" })]);
    const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: api.fetchImpl });
    const desired = row("10:a", { count: "9", stamp: "later" });
    await adapter.updateRow(2, desired);
    assert.deepEqual(api.bodies[0], {
      valueInputOption: "RAW",
      data: [
        { range: `'${SHEET_TAB}'!B2:M2`, values: [desired.slice(1, 13)] },
        { range: `'${SHEET_TAB}'!O2:O2`, values: [["later"]] },
      ],
    });
    assert.equal(api.grid[0][7], "9");
    assert.equal(api.grid[0][13], "typed by a human");
    await adapter.updateRow(2, row("10:a", { purpose: "from the projection" }), {
      writePurpose: true,
    });
    assert.deepEqual(api.bodies[1].data.map((d) => d.range), [`'${SHEET_TAB}'!B2:O2`]);
    assert.equal(api.grid[0][13], "from the projection");
    assert.deepEqual(api.calls, ["batchUpdate", "batchUpdate"]);
  });

test("adapter: readKeys reads column A at the asked rows, in order, in one call", async () => {
  const api = fakeSheetsApi([row("10:a"), null, row("10:c")]);
  const adapter = makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl: api.fetchImpl });
  assert.deepEqual(await adapter.readKeys([4, 2, 3]), ["10:c", "10:a", ""]);
  assert.deepEqual(await adapter.readKeys([]), []);
  assert.deepEqual(api.calls, ["batchGet"]);
  const answers = [
    { valueRanges: [{ range: `'${SHEET_TAB}'!A9`, values: [["10:x"]] }] }, // another row
    { valueRanges: [] }, // too few
    {},
  ];
  for (const answer of answers) {
    const fetchImpl = async () => json(answer);
    const err = await makeSheetsAdapter("tok", "wb", SHEET_TAB, { fetchImpl }).readKeys([2])
      .catch((e) => e);
    assert.equal(err.code, "invalid_keys_response", JSON.stringify(answer));
  }
});

// ---- drainThreadRows: header first, the stop hook, failures with codes ----

test("drainThreadRows: a header mismatch throws a coded error before any write", async () => {
  const { adapter, state } = memorySheet({ headers: ["Wrong", ...THREAD_HEADERS.slice(1)] });
  const err = await drainThreadRows({ adapter, desired: [row("10:a")] }).catch((e) => e);
  assert.equal(err.code, "header_mismatch");
  assert.deepEqual(state.writes, []);
});

test("drainThreadRows: shouldStop ends it between writes and counts what remains", async () => {
  const { adapter, state } = memorySheet({ rows: [row("10:a")] });
  let checks = 0;
  const result = await drainThreadRows({
    adapter,
    desired: [row("10:a", { count: "2" }), row("10:b"), row("10:c")],
    shouldStop: () => (checks += 1) > 1, // the first write goes, then a stop
  });
  assert.equal(result.updated, 1);
  assert.equal(result.appended, 0);
  assert.equal(result.stopped, true);
  assert.equal(result.remaining, 2);
  assert.equal(result.failed, 0);
  assert.deepEqual(state.writes, ["update"]);
});

test("drainThreadRows: a failed write stops it and records code and status", async () => {
  const http = Object.assign(new Error("Sheets API POST -> 503"), {
    code: "http_error", status: 503, uncertain: true,
  });
  const cases = [[http, "http_error", 503], [new Error("x"), "write_failed", null]];
  for (const [error, code, status] of cases) {
    const { adapter, state } = memorySheet({ fail: [undefined, error] });
    const desired = [row("10:a"), row("10:b"), row("10:c")];
    const result = await drainThreadRows({ adapter, desired });
    assert.equal(result.appended, 1);
    assert.equal(result.failed, 1);
    assert.equal(result.partial, true);
    assert.equal(result.code, code);
    assert.equal(result.status, status);
    assert.equal(result.remaining, 2);
    assert.deepEqual(state.writes, ["append", "append"]); // nothing after the failure
  }
});

test("drainThreadRows: the same rows again write nothing; dry run never writes", async () => {
  const { adapter, state } = memorySheet();
  const desired = [row("10:a"), row("10:b")];
  const first = await drainThreadRows({ adapter, desired });
  assert.equal(first.appended, 2);
  assert.equal(first.remaining, 0);
  const sheet = JSON.stringify(state.rows);
  const restamped = desired.map((r) => [...r.slice(0, 14), "2026-11-17T23:00:00Z"]);
  const second = await drainThreadRows({ adapter, desired: restamped });
  assert.equal(second.unchanged, 2);
  assert.equal(second.updated + second.appended, 0);
  assert.equal(JSON.stringify(state.rows), sheet);
  const dry = await drainThreadRows({ adapter, desired: [row("10:c")], dryRun: true });
  assert.equal(dry.remaining, 1);
  assert.equal(state.writes.length, 2);
});

test("drainThreadRows: an update whose row moved is skipped, never written over another thread",
  async () => {
    let sorted = false;
    const { adapter, state } = memorySheet({
      rows: [row("10:a"), row("10:b")],
      // A human sorts the tab after the read, before the updates.
      onKeys: (s) => {
        if (!sorted) s.rows.reverse();
        sorted = true;
      },
    });
    const desired = [row("10:a", { count: "2" }), row("10:b", { count: "3" })];
    const first = await drainThreadRows({ adapter, desired });
    assert.equal(first.updated, 0);
    assert.equal(first.moved, 2);
    assert.equal(first.remaining, 2);
    assert.equal(first.failed, 0);
    assert.deepEqual(state.writes, []);
    assert.deepEqual(state.rows.map((r) => [r[0], r[7]]), [["10:b", "1"], ["10:a", "1"]]);
    // The next run reads the sorted tab and updates each thread at its new row.
    const second = await drainThreadRows({ adapter, desired });
    assert.equal(second.updated, 2);
    assert.equal(second.moved, 0);
    assert.deepEqual(state.rows.map((r) => [r[0], r[7]]), [["10:b", "3"], ["10:a", "2"]]);
  });

test("drainThreadRows: keys are re-read in chunks just before their updates", async () => {
  const keys = Array.from({ length: 25 }, (_, i) => `10:k${String(i).padStart(2, "0")}`);
  const { adapter, state } = memorySheet({ rows: keys.map((k) => row(k)) });
  const desired = keys.map((k) => row(k, { count: "2" }));
  const result = await drainThreadRows({ adapter, desired });
  assert.equal(result.updated, 25);
  assert.equal(KEY_CHECK_CHUNK, 10);
  assert.deepEqual(state.keyReads.map((rows) => rows.length), [10, 10, 5]);
  assert.deepEqual(state.keyReads.flat(), keys.map((_, i) => i + 2));
  // An adapter without readKeys (the in-memory sheets) skips the check.
  const plain = memorySheet({ rows: [row("10:a")], keys: false });
  const res = await drainThreadRows({
    adapter: plain.adapter, desired: [row("10:a", { count: "2" })],
  });
  assert.equal(res.updated, 1);
});

test("drainThreadRows: a Purpose typed after the read survives the update", async () => {
  const { adapter, state } = memorySheet({
    rows: [row("10:a")],
    onKeys: (s) => { s.rows[0][13] = "typed after the read"; },
  });
  const result = await drainThreadRows({ adapter, desired: [row("10:a", { count: "2" })] });
  assert.equal(result.updated, 1);
  assert.equal(state.rows[0][7], "2");
  assert.equal(state.rows[0][13], "typed after the read");
  assert.deepEqual(state.updateOptions, [{ writePurpose: false }]);
});

test("drainThreadRows: a row with a cell over 50,000 characters is skipped and counted",
  async () => {
    const big = row("10:big");
    big[12] = "x".repeat(MAX_CELL_CHARS + 1);
    const fits = row("10:fits");
    fits[12] = "x".repeat(MAX_CELL_CHARS);
    assert.equal(rowFitsSheet(fits), true);
    assert.equal(rowFitsSheet(big), false);
    const { adapter, state } = memorySheet();
    const result = await drainThreadRows({ adapter, desired: [row("10:a"), big, fits] });
    assert.equal(result.oversized, 1);
    assert.equal(result.appended, 2);
    assert.equal(result.remaining, 0);
    assert.equal(result.failed, 0);
    assert.deepEqual(state.rows.map((r) => r[0]), ["10:a", "10:fits"]);
  });

// ---- the manual CLI: a dry run unless --write, and a write holds the drain's lease ----

const cliPayload = JSON.stringify({
  spreadsheetId: SPREADSHEET_ID, tab: SHEET_TAB, headers: THREAD_HEADERS, rows: [row("10:a")],
});

async function runCliWith(argv, { lease = { close: async () => {} }, env = {} } = {}) {
  const { adapter, state } = memorySheet();
  const seen = { out: [], err: [], leases: [], tokens: 0 };
  const code = await runCli(argv, {
    env,
    readText: () => cliPayload,
    getToken: async () => {
      seen.tokens += 1;
      return "tok";
    },
    makeAdapter: () => adapter,
    takeLease: async (path) => {
      seen.leases.push(path);
      return lease;
    },
    out: (m) => seen.out.push(m),
    err: (m) => seen.err.push(m),
  });
  return { code, state, seen };
}

test("CLI: reads and plans only unless --write; --write holds the drain's lease", async () => {
  const dry = await runCliWith(["rows.json"]);
  assert.equal(dry.code, 0);
  assert.deepEqual(dry.state.writes, []);
  assert.deepEqual(dry.seen.leases, []);
  assert.equal(JSON.parse(dry.seen.out[0]).dryRun, true);
  assert.equal(JSON.parse(dry.seen.out[0]).remaining, 1);
  const write = await runCliWith(["rows.json", "--write"]);
  assert.equal(write.code, 0);
  assert.deepEqual(write.state.writes, ["append"]);
  assert.deepEqual(write.seen.leases, [DEFAULT_LOCK]);
  const custom = await runCliWith(["rows.json", "--write"], {
    env: { OPERATOR_SHEETS_LOCK: "/fixture/drain.lock" },
  });
  assert.deepEqual(custom.seen.leases, ["/fixture/drain.lock"]);
  const both = await runCliWith(["rows.json", "--write", "--dry-run"]);
  assert.deepEqual(both.state.writes, []); // --dry-run wins
  assert.deepEqual(both.seen.leases, []);
  const held = await runCliWith(["rows.json", "--write"], { lease: null });
  assert.equal(held.code, 3);
  assert.equal(held.seen.tokens, 0); // refused before any Google call
  assert.deepEqual(held.state.writes, []);
  assert.match(held.seen.err.join("\n"), /operator-sheets-drain\.timer/);
  const usage = await runCliWith([]);
  assert.equal(usage.code, 2);
  assert.equal(usage.seen.err[0], USAGE);
  assert.match(USAGE, /--write/);
});

test("CLI: --write shares the drain's real lock file and releases it after", async () => {
  const { acquireLease } = await import("./simple_tools/operator_sheets_drain.mjs");
  const dir = await mkdtemp(join(tmpdir(), "thread-cli-lease-"));
  const lock = join(dir, "drain.lock");
  const quiet = { out: () => {}, err: () => {}, readText: () => cliPayload };
  const holder = await acquireLease(lock); // a drain run holds it
  let holderOpen = true;
  try {
    const refused = await runCli(["rows.json", "--write"], {
      ...quiet, env: { OPERATOR_SHEETS_LOCK: lock },
      getToken: async () => { throw new Error("no Google call while the drain writes"); },
    });
    assert.equal(refused, 3);
    const inodeBefore = await stat(lock);
    await holder.close();
    holderOpen = false;
    const { adapter, state } = memorySheet();
    const done = await runCli(["rows.json", "--write"], {
      ...quiet, env: { OPERATOR_SHEETS_LOCK: lock },
      getToken: async () => "tok", makeAdapter: () => adapter,
    });
    assert.equal(done, 0);
    assert.deepEqual(state.writes, ["append"]);
    const inodeAfter = await stat(lock);
    assert.equal(inodeAfter.dev, inodeBefore.dev);
    assert.equal(inodeAfter.ino, inodeBefore.ino);
    assert.equal((await readFile(lock, "utf8")).trim(), "flock-v1");
    const next = await acquireLease(lock);
    assert.ok(next); // the persistent inode is released for the next drain run
    await next.close();
  } finally {
    if (holderOpen) await holder.close().catch(() => {});
    await rm(dir, { recursive: true, force: true });
  }
});

// ---- the upsert plan and the payload contract ----

test("planUpserts: Updated At ignored, a human Purpose kept, duplicates reported", () => {
  const existing = [
    { rowNumber: 2, cells: row("10:a", { stamp: "old" }) },
    { rowNumber: 3, cells: row("10:b", { purpose: "typed by a human" }) },
    { rowNumber: 4, cells: row("10:a") },
  ];
  const plan = planUpserts(existing, [
    row("10:a", { stamp: "new" }),
    row("10:b", { count: "5" }),
    row("10:c"),
  ]);
  assert.equal(plan.unchanged, 1);
  assert.deepEqual(plan.duplicateRows, [4]);
  assert.equal(plan.updates.length, 1);
  assert.equal(plan.updates[0][0], 3);
  assert.equal(plan.updates[0][1][13], "typed by a human");
  assert.deepEqual(plan.appends.map((r) => r[0]), ["10:c"]);
});

test("validatePayload: refuses anything but rows of 15 string cells with a key", () => {
  validatePayload({ rows: [row("10:a")] });
  const bad = [
    null,
    [],
    { rows: "no" },
    { rows: [row("10:a").slice(1)] },
    { rows: [[...row("10:a").slice(0, 14), 7]] },
    { rows: [["", ...row("10:a").slice(1)]] },
  ];
  for (const payload of bad) {
    assert.throws(() => validatePayload(payload), /refusing to drain/);
  }
});
