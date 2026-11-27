// Tests for the OPS-1037 drain runner's static contract: the workbook profile it
// ships must be the exporter's canonical layout (one copy of the headers), and the
// writer lease must exclude a second live drain while recovering from a stale one.
// Run: node --test scripts/test_operator_sheets_drain.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { chmod, mkdtemp, open, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  drainOutbox, EVENTS_HEADERS, PAYLOAD_HEADERS, projectCells, SESSIONS_HEADERS,
} from "./simple_tools/operator_sheets_export.mjs";
import { OperatorSheetsError, OperatorSheetsGoogle } from "./simple_tools/operator_sheets_google.mjs";
import {
  acquireLease, createSheetsReadGate, drainExitCode, drainOnce, makeRegistryBridge, PACING_DEFAULTS, pacedAdapter,
  REGISTRY_CALL_TIMEOUT_MS, RUN_DEADLINE_MS, runOnce, SESSION_SURFACE, syncThreadsOnce, TABLES,
  THREAD_EXPORT_MAX_BYTES, THREAD_SURFACE, THREAD_SYNC_BUDGET_MS, THREAD_SYNC_MAX_WRITES,
  workbookProfileFromEnv,
} from "./simple_tools/operator_sheets_drain.mjs";
import {
  SHEET_TAB, SPREADSHEET_ID, THREAD_HEADERS,
} from "./simple_tools/thread_registry_export.mjs";

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
    new URL("./simple_tools/server/operator-sheets-drain.service", import.meta.url), "utf8");
  const exec = unit.split("\n").find((line) => line.startsWith("ExecStart="));
  assert.ok(exec, "ExecStart present");
  assert.match(exec, /operator_sheets_drain\.mjs$/);
  assert.doesNotMatch(unit, /--once/);
});

const leaseModuleUrl = new URL("./simple_tools/operator_sheets_drain.mjs", import.meta.url).href;

function startLeaseProcess(lock, mode) {
  const source = `
import { acquireLease } from ${JSON.stringify(leaseModuleUrl)};
const lease = await acquireLease(process.argv[1]);
if (!lease) process.exit(75);
process.stdout.write("locked\\n");
if (process.argv[2] === "exit") process.exit(0);
setInterval(() => {}, 60_000);
`;
  return spawn(process.execPath, ["--input-type=module", "-e", source, lock, mode], {
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function withTimeout(promise, label, timeoutMs = 10_000) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

function waitForLockMessage(child) {
  return withTimeout(new Promise((resolve, reject) => {
    let output = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      output += chunk;
      if (output.includes("locked\n")) resolve();
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (!output.includes("locked\n")) reject(new Error(`lease child exited early: ${code}`));
    });
  }), "lease child lock message");
}

function waitForClose(child) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return withTimeout(new Promise((resolve) => child.once("close", resolve)), "lease child close");
}

test("writer lease: a second process is excluded until the owner exits", { timeout: 20_000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-process-"));
  const lock = join(dir, "drain.lock");
  const holder = startLeaseProcess(lock, "hold");
  try {
    await waitForLockMessage(holder);
    const before = await readFile(lock);
    const inodeBefore = await stat(lock);
    const busy = await acquireLease(lock);
    assert.equal(busy, null);
    assert.deepEqual(await readFile(lock), before);
    const inodeWhileHeld = await stat(lock);
    assert.equal(inodeWhileHeld.dev, inodeBefore.dev);
    assert.equal(inodeWhileHeld.ino, inodeBefore.ino);

    holder.kill("SIGKILL"); // Process death releases the kernel lock without close().
    await waitForClose(holder);
    const released = await acquireLease(lock);
    assert.ok(released);
    await released.close();
    assert.deepEqual(await readFile(lock), before); // Keep the canonical lock inode stable.
    const inodeAfterRelease = await stat(lock);
    assert.equal(inodeAfterRelease.dev, inodeBefore.dev);
    assert.equal(inodeAfterRelease.ino, inodeBefore.ino);
  } finally {
    holder.kill("SIGKILL");
    await waitForClose(holder);
    await rm(dir, { recursive: true, force: true });
  }
});

test("writer lease: only the O_EXCL creator may initialize a new empty inode", { timeout: 20_000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-init-window-"));
  const lock = join(dir, "drain.lock");
  let signalOpened;
  let releaseFirst;
  const opened = new Promise((resolve) => { signalOpened = resolve; });
  const gate = new Promise((resolve) => { releaseFirst = resolve; });
  const first = acquireLease(lock, {
    openFile: async (...args) => {
      const handle = await open(...args);
      signalOpened();
      await gate;
      return handle;
    },
  });
  try {
    await opened;
    await assert.rejects(
      acquireLease(lock),
      /writer lease has ambiguous empty initialization/,
    );
    assert.equal((await stat(lock)).size, 0);
    releaseFirst();
    const firstLease = await first;
    assert.ok(firstLease);
    assert.equal((await readFile(lock, "utf8")).trim(), "flock-v1");
    await firstLease.close();
  } finally {
    releaseFirst();
    await rm(dir, { recursive: true, force: true });
  }
});

test("writer lease: preexisting empty inode is ambiguous and remains untouched", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-empty-existing-"));
  const lock = join(dir, "drain.lock");
  await writeFile(lock, "");
  await chmod(lock, 0o600);
  const before = await stat(lock);
  await assert.rejects(
    acquireLease(lock),
    /writer lease has ambiguous empty initialization/,
  );
  const after = await stat(lock);
  assert.equal(after.dev, before.dev);
  assert.equal(after.ino, before.ino);
  assert.equal(after.size, 0);
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: stale PID-only lock content does not block a free kernel lock", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-stale-"));
  const lock = join(dir, "drain.lock");
  await writeFile(lock, "999999999");
  await chmod(lock, 0o600);
  const original = await readFile(lock);
  const lease = await acquireLease(lock);
  assert.ok(lease);
  assert.notDeepEqual(await readFile(lock), original);
  assert.equal((await readFile(lock, "utf8")).trim(), "flock-v1");
  await lease.close();
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: a live legacy PID-only holder is preserved during rollout", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-legacy-live-"));
  const lock = join(dir, "drain.lock");
  await writeFile(lock, String(process.pid));
  await chmod(lock, 0o600);
  const original = await readFile(lock);
  assert.equal(await acquireLease(lock), null);
  assert.deepEqual(await readFile(lock), original);
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: symlink lock paths fail closed without touching their targets", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-symlink-"));
  const lock = join(dir, "drain.lock");
  const target = join(dir, "target");
  await writeFile(target, "preserve");
  await symlink(target, lock);
  await assert.rejects(acquireLease(lock), { code: "ELOOP" });
  assert.equal(await readFile(target, "utf8"), "preserve");
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: a permissive preexisting lock mode fails closed without repair", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-mode-"));
  const lock = join(dir, "drain.lock");
  await writeFile(lock, "flock-v1");
  await chmod(lock, 0o644);
  await assert.rejects(acquireLease(lock), /writer lease path is not the expected private regular file/);
  assert.equal((await readFile(lock, "utf8")).trim(), "flock-v1");
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: arbitrary file content fails closed without clobbering it", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-content-"));
  const lock = join(dir, "drain.lock");
  await writeFile(lock, "operator data");
  await chmod(lock, 0o600);
  const before = await readFile(lock);
  await assert.rejects(acquireLease(lock), /writer lease has an invalid legacy owner marker/);
  assert.deepEqual(await readFile(lock), before);
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: a lock-helper timeout kills the helper and fails closed", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-timeout-"));
  const lock = join(dir, "drain.lock");
  let killed = false;
  const spawnProcess = (_file, _args, options) => {
    assert.ok(Number.isInteger(options.stdio[3]), "the open lock fd is passed as child fd 3");
    const child = new EventEmitter();
    child.kill = (signal) => {
      assert.equal(signal, "SIGKILL");
      killed = true;
      return true;
    };
    return child;
  };
  await assert.rejects(
    acquireLease(lock, { spawnProcess, timeoutMs: 5 }),
    /writer lease lock helper timed out/,
  );
  assert.equal(killed, true);
  await rm(dir, { recursive: true, force: true });
});

test("writer lease: a lock-helper spawn error fails closed", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-helper-error-"));
  const lock = join(dir, "drain.lock");
  const spawnProcess = () => {
    const child = new EventEmitter();
    child.kill = () => true;
    setImmediate(() => child.emit("error", new Error("synthetic helper failure")));
    return child;
  };
  await assert.rejects(
    acquireLease(lock, { spawnProcess }),
    /writer lease lock helper failed/,
  );
  await rm(dir, { recursive: true, force: true });
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
  // The retry count rides on the result, for the fence detail.
  assert.deepEqual(await paced.appendEvent(["e1"], { validate: false }), { rowNumber: 7, rateLimitRetries: 1 });
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

test("429: stopping during a read back-off is a stop signal, not a failed row", async () => {
  const clock = fakeClock();
  let stop = false;
  const { adapter, calls } = scriptedAdapter({ inspect: [quota(), { sessions: { rowCount: 1 } }] });
  const paced = pacedAdapter(adapter, {
    sleep: async (ms) => { await clock.sleep(ms); stop = true; },
    now: clock.now,
    shouldStop: () => stop,
  });
  await assert.rejects(paced.inspect(), { code: "stopped" });
  assert.deepEqual(calls, ["inspect"]); // no retry after the stop
  assert.deepEqual(clock.sleeps, [1_000]); // bounded stop polling interrupts the 2 s back-off
});

test("429: a read refusal arriving after stop is a stop signal before retry", async () => {
  const clock = fakeClock();
  let stop = false;
  let calls = 0;
  const adapter = {
    inspect: async () => {
      calls += 1;
      stop = true;
      throw quota();
    },
  };
  const paced = pacedAdapter(adapter, {
    sleep: clock.sleep, now: clock.now, shouldStop: () => stop,
    surface: { reads: ["inspect"], writes: [] },
  });
  await assert.rejects(paced.inspect(), { code: "stopped" });
  assert.equal(calls, 1);
  assert.deepEqual(clock.sleeps, []);
});

test("429: exhausted read retries remain an HTTP failure", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ inspect: Array(6).fill(null).map(() => quota()) });
  const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now });
  await assert.rejects(paced.inspect(), { code: "http_error", status: 429 });
  assert.equal(calls.length, 6); // original request plus five retries
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

test("pacing: write spacing stays separate from fetch-level read pacing", async () => {
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
  assert.equal(clock.sleeps.length, 3); // adapter methods do not pace reads; the shared GET gate does
});

test("shared read gate paces a real 250-page drain simulation under the run budget", async () => {
  const clock = fakeClock(0);
  const readTimes = [];
  let writes = 0;
  const sheetIds = { sessions: 11, events: 12 };
  const rawFetch = async (url, init = {}) => {
    const method = init.method ?? "GET";
    const path = decodeURIComponent(new URL(url).pathname);
    if (method === "POST") {
      writes += 1;
      return jsonResponse({ spreadsheetId: "wb_1", updates: {
        updatedRange: "'Events'!A47473:I47473", updatedRows: 1, updatedColumns: 9, updatedCells: 9,
      } });
    }
    readTimes.push(clock.now());
    if (!path.includes("/values/")) return jsonResponse({ sheets: [
      { properties: { sheetId: sheetIds.sessions, title: "Sessions", gridProperties: { rowCount: 2_248, columnCount: 16 } } },
      { properties: { sheetId: sheetIds.events, title: "Events", gridProperties: { rowCount: 47_472, columnCount: 9 } } },
    ] });
    if (path.endsWith("!A1:P1")) return jsonResponse({ values: [SESSIONS_HEADERS] });
    if (path.endsWith("!A1:I1")) return jsonResponse({ values: [EVENTS_HEADERS] });
    return jsonResponse({ values: [] });
  };
  const readGate = createSheetsReadGate({ sleep: clock.sleep, now: clock.now });
  const raw = new OperatorSheetsGoogle({
    spreadsheetId: "wb_1", sheetIds, tables: TABLES, token: async () => "tok",
    fetch: readGate.wrap(rawFetch),
  });
  const adapter = pacedAdapter(raw, { sleep: clock.sleep, now: clock.now });
  const cells = EVENTS_HEADERS.map(() => "");
  cells[0] = "event-1";
  const marked = [];
  const result = await drainOutbox({
    adapter,
    outbox: {
      pending: async () => [{ outbox_id: "outbox-1", kind: "event_append", payload: { cells } }],
      markExported: async (ids) => marked.push(...ids),
    },
  });

  const pages = Math.ceil((2_248 - 1) / 200) + Math.ceil((47_472 - 1) / 200);
  assert.equal(pages, 250);
  assert.equal(readTimes.length, 256);
  assert.ok(readTimes.every((at, i) => i === 0 || at - readTimes[i - 1] >= 1_100));
  let peakPerMinute = 0;
  for (const start of readTimes) {
    peakPerMinute = Math.max(peakPerMinute,
      readTimes.filter((at) => at >= start && at < start + 60_000).length);
  }
  assert.ok(peakPerMinute <= 55);
  assert.equal(clock.t, 255 * 1_100);
  assert.ok(clock.t < RUN_DEADLINE_MS);
  assert.deepEqual(result, { exported: 1, failed: 0 });
  assert.equal(writes, 1);
  assert.deepEqual(marked, ["outbox-1"]);
});

test("read gate stop aborts reconciliation before another GET or any write", async () => {
  const clock = fakeClock();
  let stop = false;
  let calls = 0;
  const sheets = [
    { properties: { sheetId: 11, title: "Sessions", gridProperties: { rowCount: 2, columnCount: 16 } } },
    { properties: { sheetId: 12, title: "Events", gridProperties: { rowCount: 2, columnCount: 9 } } },
  ];
  const rawFetch = async (url, init = {}) => {
    calls += 1;
    if (init.method && init.method !== "GET") throw new Error("unexpected write");
    const path = decodeURIComponent(new URL(url).pathname);
    if (!path.includes("/values/")) {
      if (calls === 4) stop = true;
      return jsonResponse({ sheets });
    }
    if (path.endsWith("!A1:P1")) return jsonResponse({ values: [SESSIONS_HEADERS] });
    if (path.endsWith("!A1:I1")) return jsonResponse({ values: [EVENTS_HEADERS] });
    return jsonResponse({ values: [] });
  };
  const readGate = createSheetsReadGate({ sleep: clock.sleep, now: clock.now });
  const raw = new OperatorSheetsGoogle({
    spreadsheetId: "wb_1", sheetIds: { sessions: 11, events: 12 }, tables: TABLES,
    token: async () => "tok", fetch: readGate.wrap(rawFetch, () => stop),
  });
  const adapter = pacedAdapter(raw, { sleep: clock.sleep, now: clock.now, shouldStop: () => stop });
  const cells = SESSIONS_HEADERS.map(() => "");
  cells[0] = "record-1";
  const marked = [];
  const result = await drainOutbox({
    adapter,
    outbox: {
      pending: async () => [{ outbox_id: "outbox-1", kind: "session_upsert", payload: { cells } }],
      markExported: async (ids) => marked.push(...ids),
    },
    shouldStop: () => stop,
  });
  assert.deepEqual(result, { exported: 0, failed: 0, stopped: true });
  assert.equal(calls, 4); // next GET is refused locally after the stop
  assert.deepEqual(marked, []);
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

test("pacing: shouldStop hands a 429 straight back instead of sleeping for a retry", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ appendEvent: [quota(), { rowNumber: 2 }] });
  const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now, shouldStop: () => true });
  const error = await paced.appendEvent(["e1"]).catch((e) => e);
  assert.equal(error.status, 429);
  assert.equal(calls.length, 1);
  assert.deepEqual(clock.sleeps, []);
});

test("pacing: a stop during the 429 back-off cuts the sleep short and sends no retry", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ appendEvent: [quota(30), { rowNumber: 2 }] });
  let stop = false;
  const sleep = async (ms) => { await clock.sleep(ms); stop = true; }; // SIGTERM arrives mid back-off
  const logs = [];
  const paced = pacedAdapter(adapter, {
    sleep, now: clock.now, shouldStop: () => stop, log: (m) => logs.push(m),
  });
  const error = await paced.appendEvent(["e1"]).catch((e) => e);
  assert.equal(error.status, 429);
  assert.equal(calls.length, 1); // no write after the stop
  assert.deepEqual(clock.sleeps, [1_000]); // one 1 s slice of the 30 s back-off, not all of it
  assert.ok(logs.some((m) => /stop requested during the back-off/.test(m)));
});

test("pacing: with a stop check the back-off sleeps in slices and still retries when no stop comes", async () => {
  const clock = fakeClock();
  const { adapter, calls } = scriptedAdapter({ appendEvent: [quota(3), { rowNumber: 2 }] });
  const paced = pacedAdapter(adapter, { sleep: clock.sleep, now: clock.now, shouldStop: () => false });
  assert.deepEqual(await paced.appendEvent(["e1"]), { rowNumber: 2, rateLimitRetries: 1 });
  assert.equal(calls.length, 2);
  assert.deepEqual(clock.sleeps, [1_000, 1_000, 1_000]);
});

// ---- registry bridge and writer lease ----

test("registry bridge: every docker exec carries a timeout", async () => {
  const seen = [];
  const exec = async (file, args, options) => {
    seen.push({ file, args, options });
    return { stdout: "[]" };
  };
  const bridge = makeRegistryBridge({ container: "c1", db: "/data/x.sqlite3", exec });
  assert.deepEqual(await bridge.outbox.pending(5), []);
  await bridge.outbox.markExported(["a"]);
  await bridge.attempts.recordBatch([{ outboxId: "a", destination: "d", tab: "sessions", kind: "k", outcome: "exported" }]);
  assert.equal(REGISTRY_CALL_TIMEOUT_MS, 120_000);
  assert.equal(seen.length, 3);
  for (const call of seen) {
    assert.equal(call.file, "docker");
    assert.equal(call.options.timeout, 120_000);
    assert.equal(call.options.killSignal, "SIGKILL");
  }
});

// ---- drainOnce: the pass loop, the run deadline and the stop signal ----

const RUN_ENV = {
  OPERATOR_SHEETS_WORKBOOK_ID: "wb_1",
  OPERATOR_SHEETS_SESSIONS_SHEET_ID: "11",
  OPERATOR_SHEETS_EVENTS_SHEET_ID: "12",
};

const sessionCells = (id) => [id, ...Array(SESSIONS_HEADERS.length - 1).fill("")];

// A registry fake: pending() answers the unexported rows in order, like the store.
function fakeRegistry(ids) {
  const state = { marked: [], fence: [] };
  const rows = ids.map((id) => ({ outbox_id: `ob-${id}`, kind: "session_upsert", payload: { cells: sessionCells(id) } }));
  return {
    state,
    bridge: {
      outbox: {
        pending: async (limit) => rows.filter((r) => !state.marked.includes(r.outbox_id)).slice(0, limit),
        markExported: async (ids2) => { state.marked.push(...ids2); },
      },
      attempts: { recordBatch: async (records) => { state.fence.push(...records); } },
    },
  };
}

// A Sheets fake with an empty grid; failures and side effects per record id.
function fakeSheet({ fail = {}, onWrite = () => {} } = {}) {
  const appends = [];
  const adapter = {
    validateHeaders: async () => true,
    inspect: async () => ({ sessions: { rowCount: 1 }, events: { rowCount: 1 } }),
    readRows: async () => [],
    appendSession: async (cells) => {
      appends.push(cells[0]);
      onWrite(cells[0]);
      const queue = fail[cells[0]];
      const next = Array.isArray(queue) ? queue.shift() : undefined;
      if (next instanceof Error) throw next;
      return { rowNumber: appends.length + 1 };
    },
    updateSessionRow: async () => ({ rowNumber: 2 }),
    appendEvent: async () => ({ rowNumber: 2 }),
  };
  return { adapter, appends };
}

test("drainOnce: one export trim runs after the passes and its counts are one log line", async () => {
  const { bridge } = fakeRegistry(["r1"]);
  const trims = [];
  bridge.housekeeping = { trim: async () => { trims.push(1); return { attempts: 3, outbox: 2 }; } };
  const logs = [];
  const { adapter } = fakeSheet();
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, log: (m) => logs.push(m) });
  assert.deepEqual(totals, { exported: 1, failed: 0 });
  assert.equal(trims.length, 1, "the trim runs once, after the passes");
  assert.deepEqual(
    logs.filter((m) => m.includes("trimmed")),
    [JSON.stringify({ trimmed: { attempts: 3, outbox: 2 } })],
  );
});

test("drainOnce: a refused export trim is one fixed line and never fails the run", async () => {
  const { bridge } = fakeRegistry(["r1"]);
  bridge.housekeeping = { trim: async () => { throw new Error("registry unavailable"); } };
  const logs = [];
  const { adapter } = fakeSheet();
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, log: (m) => logs.push(m) });
  assert.deepEqual(totals, { exported: 1, failed: 0 });
  assert.ok(logs.some((m) => m.startsWith("export trim failed")), logs.filter((m) => m.includes("trim")));
});

test("drainOnce: a bridge without the trim verb skips it silently", async () => {
  const { bridge } = fakeRegistry(["r1"]);
  const logs = [];
  const { adapter } = fakeSheet();
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, log: (m) => logs.push(m) });
  assert.deepEqual(totals, { exported: 1, failed: 0 });
  assert.equal(logs.filter((m) => m.includes("trim")).length, 0);
});

test("drainOnce: a failed row ends the run, so an uncertain write is never re-sent by the next pass", async () => {
  const { bridge, state } = fakeRegistry(["r1", "r2", "r3", "r4", "r5", "r6", "r7"]);
  const uncertain = new OperatorSheetsError("transport_or_response_error", { uncertain: true });
  const { adapter, appends } = fakeSheet({ fail: { r6: [uncertain] } });
  const logs = [];
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, log: (m) => logs.push(m) });
  assert.deepEqual(totals, { exported: 5, failed: 1 });
  assert.deepEqual(appends, ["r1", "r2", "r3", "r4", "r5", "r6"]); // r6 sent once; no second pass
  assert.deepEqual(state.marked, ["ob-r1", "ob-r2", "ob-r3", "ob-r4", "ob-r5"]);
  assert.ok(logs.some((m) => /ending the run/.test(m)));
});

test("drainOnce: passes repeat while every row exports, then stop when nothing is left", async () => {
  const { bridge, state } = fakeRegistry(["r1", "r2", "r3", "r4", "r5"]);
  const { adapter, appends } = fakeSheet();
  const passes = [];
  const totals = await drainOnce({
    env: { ...RUN_ENV, OPERATOR_SHEETS_PAGE_LIMIT: "2" }, bridge, adapter,
    log: (m) => { if (m.startsWith("{")) passes.push(JSON.parse(m)); },
  });
  assert.deepEqual(totals, { exported: 5, failed: 0 });
  assert.deepEqual(passes.map((p) => p.exported), [2, 2, 1, 0]);
  assert.equal(appends.length, 5);
  assert.equal(state.marked.length, 5);
});

test("drainOnce: an invalid OPERATOR_SHEETS_PAGE_LIMIT refuses the run before anything drains", async () => {
  // "0" would page zero rows a pass and report success forever; "abc" is NaN,
  // which the registry CLI cannot take. Both must fail at startup instead.
  for (const value of ["0", "abc"]) {
    const { bridge, state } = fakeRegistry(["r1"]);
    const { adapter, appends } = fakeSheet();
    await assert.rejects(
      drainOnce({ env: { ...RUN_ENV, OPERATOR_SHEETS_PAGE_LIMIT: value }, bridge, adapter, log: () => {} }),
      /OPERATOR_SHEETS_PAGE_LIMIT must be an integer between 1 and 10000/,
    );
    assert.deepEqual(appends, [], value);
    assert.deepEqual(state.marked, [], value);
  }
});

test("drainOnce: the run deadline stops between rows and leaves the rest pending", async () => {
  const clock = fakeClock(0);
  const { bridge, state } = fakeRegistry(["r1", "r2", "r3", "r4", "r5"]);
  const { adapter, appends } = fakeSheet({ onWrite: () => { clock.t += 1_000; } });
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, now: clock.now, deadlineMs: 2_500, log: () => {} });
  assert.deepEqual(totals, { exported: 3, failed: 0, stopped: "deadline" });
  assert.deepEqual(appends, ["r1", "r2", "r3"]);
  assert.deepEqual(state.marked, ["ob-r1", "ob-r2", "ob-r3"]); // confirmed rows are still acknowledged
  assert.equal(RUN_DEADLINE_MS, 25 * 60 * 1000);
});

test("drainOnce: a stop signal (SIGTERM) ends the run after the current row", async () => {
  const { bridge, state } = fakeRegistry(["r1", "r2", "r3", "r4"]);
  let writes = 0;
  const { adapter } = fakeSheet({ onWrite: () => { writes += 1; } });
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, shouldStop: () => writes >= 2, log: () => {} });
  assert.deepEqual(totals, { exported: 2, failed: 0, stopped: "signal" });
  assert.deepEqual(state.marked, ["ob-r1", "ob-r2"]);
});

test("drainOnce: write pacing holds across passes", async () => {
  const clock = fakeClock();
  const { bridge } = fakeRegistry(["r1", "r2", "r3"]);
  const { adapter: sheet } = fakeSheet();
  const adapter = pacedAdapter(sheet, { sleep: clock.sleep, now: clock.now });
  const totals = await drainOnce({
    env: { ...RUN_ENV, OPERATOR_SHEETS_PAGE_LIMIT: "1" }, bridge, adapter, now: clock.now, log: () => {},
  });
  assert.deepEqual(totals, { exported: 3, failed: 0 });
  assert.deepEqual(clock.sleeps, [1_100, 1_100]); // one write per pass, still 1.1 s apart
});

test("drainOnce: running out of 429 retries on the first of several rows stops the run, nothing else sent", async () => {
  const clock = fakeClock();
  const { bridge, state } = fakeRegistry(["r1", "r2", "r3"]);
  const { adapter: sheet, appends } = fakeSheet({ fail: { r1: [quota(), quota()] } });
  const adapter = pacedAdapter(sheet, { sleep: clock.sleep, now: clock.now, rateLimitRetries: 1 });
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, now: clock.now, log: () => {} });
  assert.deepEqual(totals, { exported: 0, failed: 1 });
  assert.deepEqual(appends, ["r1", "r1"]); // the first try and one retry; r2 and r3 never sent
  assert.deepEqual(clock.sleeps, [2_000]);
  assert.deepEqual(state.marked, []);
  assert.equal(state.fence.length, 1);
  assert.equal(state.fence[0].outcome, "failed");
});

test("drainOnce: a registry or header failure outside the row loop returns failed totals, not a rejection", async () => {
  const timedOut = Object.assign(new Error("Command failed: docker exec (killed after 120000 ms)"), {
    killed: true, signal: "SIGKILL",
  });
  const cases = [
    ["pending read", (bridge, adapter) => { bridge.outbox.pending = async () => { throw timedOut; }; }],
    ["header check", (bridge, adapter) => {
      adapter.validateHeaders = async () => { throw new OperatorSheetsError("header_mismatch"); };
    }],
  ];
  for (const [label, breakIt] of cases) {
    const { bridge, state } = fakeRegistry(["r1", "r2"]);
    const { adapter, appends } = fakeSheet();
    breakIt(bridge, adapter);
    const logs = [];
    const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, log: (m) => logs.push(m) });
    assert.equal(totals.exported, 0, label);
    assert.equal(totals.failed, 1, label);
    assert.ok(totals.error, label);
    assert.deepEqual(appends, [], label);
    assert.deepEqual(state.marked, [], label); // rows stay pending for the next run
    assert.ok(logs.some((m) => /failed before or outside the row loop/.test(m)), label);
  }
});

test("drainOnce: a failure after earlier passes keeps their totals", async () => {
  const { bridge } = fakeRegistry(["r1", "r2", "r3"]);
  const { adapter } = fakeSheet();
  const pending = bridge.outbox.pending;
  let reads = 0;
  bridge.outbox.pending = async (limit) => {
    reads += 1;
    if (reads === 2) throw new Error("registry call timed out");
    return pending(limit);
  };
  const totals = await drainOnce({
    env: { ...RUN_ENV, OPERATOR_SHEETS_PAGE_LIMIT: "2" }, bridge, adapter, log: () => {},
  });
  assert.deepEqual(totals, { exported: 2, failed: 1, error: "registry call timed out" });
});

test("drainOnce: a write that needed a 429 retry says so in its fence detail", async () => {
  const clock = fakeClock();
  const { bridge, state } = fakeRegistry(["r1", "r2"]);
  const { adapter: sheet } = fakeSheet({ fail: { r1: [quota()] } });
  const adapter = pacedAdapter(sheet, { sleep: clock.sleep, now: clock.now });
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, now: clock.now, log: () => {} });
  assert.deepEqual(totals, { exported: 2, failed: 0 });
  const byId = Object.fromEntries(state.fence.map((r) => [r.outboxId, r]));
  assert.equal(byId["ob-r1"].outcome, "exported");
  assert.match(byId["ob-r1"].detail, /written after 1 HTTP 429 quota refusal/);
  assert.equal(byId["ob-r2"].detail, "");
});

// ---- the Zulip Threads sync (AFC-823): the same run, isolated, bounded, counts only ----

// Text that must never reach a log line: topic and participant text.
const TOPIC_MARKER = "topic-text-never-logged";
const PARTICIPANT_MARKER = "PARTICIPANT_NEVER_LOGGED";

// One projected row, the 15 string cells thread_registry.py's export-rows emits.
const threadRow = (n, { topic = `${TOPIC_MARKER} ${n}`, stamp = "2026-11-17T18:00:00Z" } = {}) => [
  `10:${topic.toLowerCase()}`, "Work-In-Progress", "10", topic, "active", PARTICIPANT_MARKER,
  "1", "1", "2026-11-17T17:00:00Z", "2026-11-17T17:30:00Z", String(1000 + n), "AFC-823", "",
  "", stamp,
];

const threadPayload = (rows, overrides = {}) => ({
  spreadsheetId: SPREADSHEET_ID, tab: SHEET_TAB, headers: [...THREAD_HEADERS], rows, ...overrides,
});

// The registry side: export-rows answers the payload (or throws). The outbox and the
// fence refuse and record any call, so a test can prove the thread phase never used them.
function threadBridge(answer) {
  const state = { exports: 0, touched: [] };
  const refuse = (name) => async () => {
    state.touched.push(name);
    throw new Error(`${name} touched by the thread sync`);
  };
  return {
    state,
    bridge: {
      threads: {
        exportRows: async () => {
          state.exports += 1;
          if (answer instanceof Error) throw answer;
          return structuredClone(answer);
        },
      },
      outbox: { pending: refuse("pending"), markExported: refuse("markExported") },
      attempts: { record: refuse("record"), recordBatch: refuse("recordBatch") },
    },
  };
}

// An in-memory Zulip Threads tab with the thread module's adapter surface. fail holds
// one entry per write, in order: an Error is thrown for that write. Like the real
// adapter, an update never writes the Thread Key and writes Purpose only when asked.
// onKeys runs before each key re-read, as a human editing the tab mid-run would.
function threadSheet({
  headers = THREAD_HEADERS, rows = [], fail = [], onWrite = () => {}, onKeys = () => {},
} = {}) {
  const state = { rows: rows.map((r) => [...r]), writes: [], calls: [] };
  const write = (kind, apply) => async (...args) => {
    state.calls.push(kind);
    state.writes.push(kind);
    onWrite(kind);
    const next = fail.shift();
    if (next instanceof Error) throw next;
    return apply(...args);
  };
  const adapter = {
    validateHeaders: async () => {
      state.calls.push("validateHeaders");
      return [...headers];
    },
    readRows: async () => {
      state.calls.push("readRows");
      return state.rows.map((cells, i) => ({ rowNumber: i + 2, cells: [...cells] }));
    },
    readKeys: async (rowNumbers) => {
      state.calls.push("readKeys");
      onKeys(state);
      return rowNumbers.map((r) => state.rows[r - 2]?.[0] ?? "");
    },
    appendRow: write("append", (cells) => {
      state.rows.push([...cells]);
      return state.rows.length + 1;
    }),
    updateRow: write("update", (rowNumber, cells, options = {}) => {
      const current = state.rows[rowNumber - 2] ?? [];
      const next = [...cells];
      next[0] = current[0] ?? "";
      if (!options.writePurpose) next[13] = current[13] ?? "";
      state.rows[rowNumber - 2] = next;
    }),
  };
  return { state, adapter };
}

const jsonResponse = (body) => new Response(JSON.stringify(body), { status: 200 });

// The workbook metadata a real thread adapter reads first: the tab's grid row count.
const gridAnswer = (rowCount) => jsonResponse({
  sheets: [{ properties: { title: SHEET_TAB, gridProperties: { rowCount } } }],
});
const isMetadata = (url) => !new URL(url).pathname.includes("/values");

// A thread-module-shaped HTTP failure, as makeSheetsAdapter throws it.
const sheetsHttpError = (status, uncertain) => Object.assign(
  new Error(`Sheets API POST /values/${TOPIC_MARKER} -> ${status}`),
  { code: "http_error", status, uncertain, retryAfterSeconds: null },
);

// syncThreadsOnce with a fake clock (pacing included, so nothing sleeps for real).
async function syncThreads({ clock = fakeClock(0), ...options } = {}) {
  const logs = [];
  const result = await syncThreadsOnce({
    env: RUN_ENV, log: (m) => logs.push(m), now: clock.now, pacing: { sleep: clock.sleep },
    readGate: options.readGate ?? createSheetsReadGate({ sleep: clock.sleep, now: clock.now }),
    ...options,
  });
  return { result, logs, clock };
}

const counts = ({ ms, ...rest }) => rest;

test("thread sync: first run appends every row, the same rows again write none", async () => {
  const rows = [threadRow(1), threadRow(2), threadRow(3)];
  const { adapter, state } = threadSheet();
  // No loadModule given: the default import finds thread_registry_export.mjs beside the
  // drain, the layout the installer gives /usr/local/lib/operator-sheets-drain.
  const first = await syncThreads({ bridge: threadBridge(threadPayload(rows)).bridge, adapter });
  assert.deepEqual(counts(first.result), {
    rows: 3, refused: 0, updated: 0, appended: 3, unchanged: 0, duplicates: 0, moved: 0,
    remaining: 0, failed: 0,
  });
  const sheet = JSON.stringify(state.rows);
  // A later export of the same aggregates carries a newer Registry Updated At stamp only.
  const again = rows.map((r) => [...r.slice(0, 14), "2026-11-17T19:00:00Z"]);
  const second = await syncThreads({ bridge: threadBridge(threadPayload(again)).bridge, adapter });
  assert.deepEqual(counts(second.result), {
    rows: 3, refused: 0, updated: 0, appended: 0, unchanged: 3, duplicates: 0, moved: 0,
    remaining: 0, failed: 0,
  });
  assert.equal(JSON.stringify(state.rows), sheet); // the same sheet, byte for byte
  assert.deepEqual(state.writes, ["append", "append", "append"]);
});

test("thread sync: one log line of counts, never topic or header text", async () => {
  const { bridge } = threadBridge(threadPayload([threadRow(1), threadRow(2)]));
  const wrongHeaders = [`Thread Key ${TOPIC_MARKER}`, ...THREAD_HEADERS.slice(1)];
  for (const adapter of [threadSheet().adapter, threadSheet({ headers: wrongHeaders }).adapter]) {
    const { logs } = await syncThreads({ bridge, adapter });
    assert.equal(logs.length, 1);
    const line = JSON.parse(logs[0]);
    assert.deepEqual(Object.keys(line), ["threads"]);
    for (const [key, value] of Object.entries(line.threads)) {
      const fixed = typeof value === "string" && /^[a-z0-9_]+$/.test(value);
      const count = value === null || Number.isSafeInteger(value);
      assert.ok(count || fixed, `${key} is a count or a fixed code`);
    }
    assert.doesNotMatch(logs[0], new RegExp(`${TOPIC_MARKER}|${PARTICIPANT_MARKER}|Work-In`));
  }
});

test("thread sync: a payload for another tab or layout is refused unsent", async () => {
  const good = [threadRow(1)];
  const cases = [
    [null, "payload_invalid"],
    [threadPayload(good, { tab: "Sessions" }), "payload_destination_mismatch"],
    [threadPayload(good, { spreadsheetId: "another_workbook" }), "payload_destination_mismatch"],
    [threadPayload(good, { headers: [...THREAD_HEADERS].reverse() }), "payload_headers_mismatch"],
    [threadPayload([["only-a-key"]]), "payload_invalid"],
    [threadPayload([[...threadRow(1).slice(0, 14), 15]]), "payload_invalid"],
    [threadPayload([["", ...threadRow(1).slice(1)]]), "payload_invalid"],
  ];
  for (const [payload, code] of cases) {
    const { adapter, state } = threadSheet();
    const { result } = await syncThreads({ bridge: threadBridge(payload).bridge, adapter });
    assert.equal(result.failed, 1, code);
    assert.equal(result.error, code);
    assert.deepEqual(state.calls, [], `${code}: no sheet call`);
  }
});

test("thread sync: payload validation runs before destination and header fields are read", async () => {
  const reads = [];
  const malformed = new Proxy({}, {
    get(_target, key) {
      if (key === "then") return undefined;
      reads.push(String(key));
      throw new Error("invalid payload must be rejected before field access");
    },
  });
  const bridge = {
    threads: { exportRows: async () => malformed },
    outbox: {}, attempts: {},
  };
  const { adapter, state } = threadSheet();
  const { result } = await syncThreads({
    bridge, adapter,
    loadModule: async () => ({
      SPREADSHEET_ID: SPREADSHEET_ID,
      SHEET_TAB,
      THREAD_HEADERS,
      validatePayload: (payload) => {
        reads.push("validatePayload");
        assert.equal(payload, malformed);
        throw new Error("synthetic malformed payload");
      },
    }),
  });
  assert.equal(result.error, "payload_invalid");
  assert.deepEqual(reads, ["validatePayload"]);
  assert.deepEqual(state.calls, []);
});

test("thread sync: a registry read failure is one fixed code and touches no sheet", async () => {
  const cases = [
    [Object.assign(new Error(`Command failed: docker exec ${TOPIC_MARKER}`), {
      killed: true, signal: "SIGKILL",
    }), "registry_timeout"],
    [Object.assign(new RangeError(`stdout maxBuffer length exceeded ${TOPIC_MARKER}`), {
      code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER", killed: true, signal: "SIGKILL",
    }), "registry_too_large"],
    [new SyntaxError(`Unexpected token in JSON ${TOPIC_MARKER}`), "registry_invalid_json"],
    [Object.assign(new Error(`Command failed ${TOPIC_MARKER}`), { code: 1 }), "registry_failed"],
  ];
  for (const [error, code] of cases) {
    const { adapter, state } = threadSheet();
    const { result, logs } = await syncThreads({ bridge: threadBridge(error).bridge, adapter });
    assert.deepEqual(counts(result), {
      rows: 0, refused: 0, updated: 0, appended: 0, unchanged: 0, duplicates: 0, moved: 0,
      remaining: null, failed: 1, error: code,
    });
    assert.deepEqual(state.calls, []);
    assert.doesNotMatch(logs.join("\n"), new RegExp(TOPIC_MARKER));
  }
});

test("thread sync: a missing module, token or header fails closed", async () => {
  const payload = threadPayload([threadRow(1)]);
  const missing = await syncThreads({
    bridge: threadBridge(payload).bridge, adapter: threadSheet().adapter,
    loadModule: async () => { throw new Error(`Cannot find module ${TOPIC_MARKER}`); },
  });
  assert.equal(missing.result.error, "module_unavailable");
  const token = await syncThreads({
    bridge: threadBridge(payload).bridge,
    tokenProvider: async () => {
      throw new OperatorSheetsError("service_account_load_or_sign_failed");
    },
  });
  assert.equal(token.result.error, "token_service_account_load_or_sign_failed");
  assert.equal(token.result.rows, 1);
  const wrong = threadSheet({ headers: ["Wrong", ...THREAD_HEADERS.slice(1)] });
  const header = await syncThreads({
    bridge: threadBridge(payload).bridge, adapter: wrong.adapter,
  });
  assert.equal(header.result.error, "sheet_header_mismatch");
  assert.deepEqual(wrong.state.writes, []);
  for (const run of [missing, token, header]) {
    assert.equal(run.result.failed, 1);
    assert.doesNotMatch(run.logs.join("\n"), new RegExp(TOPIC_MARKER));
  }
});

test("thread sync: the write cap ends a run; the next runs finish the rest", async () => {
  const rows = [1, 2, 3, 4, 5].map((n) => threadRow(n));
  const { adapter, state } = threadSheet();
  const run = () => syncThreads({
    bridge: threadBridge(threadPayload(rows)).bridge, adapter, maxWrites: 2,
  });
  const first = (await run()).result;
  assert.equal(first.appended, 2);
  assert.equal(first.remaining, 3);
  assert.equal(first.stopped, "max_writes");
  assert.equal(first.failed, 0);
  assert.equal((await run()).result.remaining, 1);
  const third = (await run()).result;
  assert.equal(third.appended, 1);
  assert.equal(third.remaining, 0);
  assert.equal(third.stopped, undefined);
  const settled = (await run()).result;
  assert.equal(settled.unchanged, 5);
  assert.equal(state.writes.length, 5); // every row written once, none twice
  assert.deepEqual(state.rows.map((r) => r[0]), rows.map((r) => r[0]));
  assert.equal(THREAD_SYNC_MAX_WRITES, 200);
});

test("thread sync: updates count toward the write cap as appends do", async () => {
  const rows = [1, 2, 3, 4, 5].map((n) => threadRow(n));
  // The tab already holds threads 1-3 with an older message count: three updates, then
  // two appends. Most steady-state writes are updates (a Status that ages, a new message).
  const stale = rows.slice(0, 3).map((cells) => [...cells.slice(0, 7), "0", ...cells.slice(8)]);
  const { adapter, state } = threadSheet({ rows: stale });
  const run = async () => (await syncThreads({
    bridge: threadBridge(threadPayload(rows)).bridge, adapter, maxWrites: 2,
  })).result;
  const first = await run();
  assert.deepEqual([first.updated, first.appended, first.remaining], [2, 0, 3]);
  assert.equal(first.stopped, "max_writes");
  const second = await run();
  assert.deepEqual([second.updated, second.appended, second.remaining], [1, 1, 1]);
  assert.equal(second.stopped, "max_writes");
  const third = await run();
  assert.deepEqual([third.updated, third.appended, third.remaining], [0, 1, 0]);
  assert.equal(third.stopped, undefined);
  const settled = await run();
  assert.equal(settled.unchanged, 5);
  assert.deepEqual(state.writes, ["update", "update", "update", "append", "append"]);
  assert.deepEqual(state.rows, rows);
});

test("thread sync: a row sorted away after the read is skipped and counted, then repaired",
  async () => {
    const rows = [threadRow(1), threadRow(2)];
    const stale = rows.map((cells) => [...cells.slice(0, 7), "0", ...cells.slice(8)]);
    stale[0][13] = "typed by a human";
    let sorted = false;
    const { adapter, state } = threadSheet({
      rows: stale,
      onKeys: (s) => {
        if (!sorted) s.rows.reverse(); // a human sorts the tab mid-run
        sorted = true;
      },
    });
    const run = async () => (await syncThreads({
      bridge: threadBridge(threadPayload(rows)).bridge, adapter,
    })).result;
    const first = await run();
    assert.deepEqual([first.updated, first.moved, first.remaining, first.failed], [0, 2, 2, 0]);
    assert.deepEqual(state.writes, []); // nothing written over the other thread's row
    const second = await run();
    assert.deepEqual([second.updated, second.moved, second.remaining], [2, 0, 0]);
    // Each thread updated at its new row, and the human's Purpose still on its own thread.
    assert.deepEqual(state.rows.map((r) => [r[0], r[7], r[13]]), [
      [rows[1][0], "1", ""], [rows[0][0], "1", "typed by a human"],
    ]);
  });

test("thread sync: a row with a cell over 50,000 characters is refused, the rest written",
  async () => {
    const oversized = threadRow(2);
    oversized[12] = "o/r#1, ".repeat(8_000); // a PR list grown past the cell limit
    const rows = [threadRow(1), oversized, threadRow(3)];
    const { adapter, state } = threadSheet();
    const { result, logs } = await syncThreads({
      bridge: threadBridge(threadPayload(rows)).bridge, adapter,
    });
    assert.equal(result.rows, 3);
    assert.equal(result.refused, 1);
    assert.equal(result.appended, 2);
    assert.equal(result.failed, 0);
    assert.deepEqual(state.rows.map((r) => r[0]), [threadRow(1)[0], threadRow(3)[0]]);
    assert.ok(logs[0].length < 1_000);
  });

test("thread sync: the time budget ends a run between writes", async () => {
  const clock = fakeClock(0);
  const { adapter, state } = threadSheet({ onWrite: () => { clock.t += 1_000; } });
  const rows = [1, 2, 3, 4, 5].map((n) => threadRow(n));
  const { result } = await syncThreads({
    clock, bridge: threadBridge(threadPayload(rows)).bridge, adapter, budgetMs: 2_500,
    pacing: { sleep: clock.sleep, writeIntervalMs: 0 },
  });
  assert.equal(result.appended, 3);
  assert.equal(result.remaining, 2);
  assert.equal(result.stopped, "deadline");
  assert.equal(result.ms, 3_000);
  assert.equal(state.writes.length, 3);
  assert.equal(THREAD_SYNC_BUDGET_MS, 4 * 60 * 1000);
  // The worst-case thread phase leaves the Sessions and Events drain most of its run.
  assert.ok(RUN_DEADLINE_MS - THREAD_SYNC_BUDGET_MS - 60_000 >= 20 * 60 * 1000);
});

test("thread sync: a stop ends it between writes; one before it skips it", async () => {
  let writes = 0;
  const { adapter } = threadSheet({ onWrite: () => { writes += 1; } });
  const rows = [threadRow(1), threadRow(2), threadRow(3)];
  const { result } = await syncThreads({
    bridge: threadBridge(threadPayload(rows)).bridge, adapter, shouldStop: () => writes >= 2,
  });
  assert.equal(result.appended, 2);
  assert.equal(result.stopped, "signal");
  assert.equal(result.failed, 0);
  const early = threadBridge(threadPayload(rows));
  const skipped = await syncThreads({ bridge: early.bridge, adapter, shouldStop: () => true });
  assert.equal(skipped.result.stopped, "signal");
  assert.equal(early.state.exports, 0); // not even the registry read
});

test("thread sync: a 429 is retried; an uncertain write stops unretried", async () => {
  const rows = [threadRow(1), threadRow(2), threadRow(3)];
  const quotaRun = threadSheet({ fail: [sheetsHttpError(429, false)] });
  const quota = await syncThreads({
    bridge: threadBridge(threadPayload(rows)).bridge, adapter: quotaRun.adapter,
  });
  assert.equal(quota.result.appended, 3);
  assert.equal(quota.result.failed, 0);
  assert.equal(quotaRun.state.writes.length, 4); // the refused write, its retry, two more
  assert.deepEqual(quota.clock.sleeps.slice(0, 2), [1_000, 1_000]); // the 2 s back-off, sliced
  const uncertainRun = threadSheet({ fail: [undefined, sheetsHttpError(503, true)] });
  const uncertain = await syncThreads({
    bridge: threadBridge(threadPayload(rows)).bridge, adapter: uncertainRun.adapter,
  });
  assert.equal(uncertain.result.appended, 1);
  assert.equal(uncertain.result.failed, 1);
  assert.equal(uncertain.result.error, "sheet_http_503");
  assert.equal(uncertain.result.remaining, 2);
  assert.equal(uncertainRun.state.writes.length, 2); // never re-sent blind
  assert.doesNotMatch(uncertain.logs.join("\n"), new RegExp(TOPIC_MARKER));
});

test("thread sync: a credential-shaped cell keeps its row on the host", async () => {
  const credentialShaped = ["gh", "p_", "Z".repeat(36)].join(""); // built here, never a literal
  const rows = [threadRow(1), threadRow(2, { topic: `leak ${credentialShaped}` }), threadRow(3)];
  const { adapter, state } = threadSheet();
  const { result, logs } = await syncThreads({
    bridge: threadBridge(threadPayload(rows)).bridge, adapter,
  });
  assert.equal(result.rows, 3);
  assert.equal(result.refused, 1);
  assert.equal(result.appended, 2);
  assert.equal(result.failed, 0);
  assert.doesNotMatch(JSON.stringify(state.rows) + logs.join("\n"), /gh.?p_Z/i);
});

test("thread sync: never reads or writes the export outbox or its fence", async () => {
  const { bridge, state } = threadBridge(threadPayload([threadRow(1)]));
  const { result } = await syncThreads({ bridge, adapter: threadSheet().adapter });
  assert.equal(result.appended, 1);
  assert.equal(state.exports, 1);
  assert.deepEqual(state.touched, []);
});

test("thread sync: end to end through the thread module's real adapter", async () => {
  const realFetch = globalThis.fetch;
  const seen = [];
  let refuseNextAppend = true;
  const json = jsonResponse;
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url: String(url), init });
    const path = decodeURIComponent(new URL(url).pathname);
    if (isMetadata(url)) return gridAnswer(1_000);
    if (path.endsWith(":append")) {
      if (refuseNextAppend) {
        refuseNextAppend = false;
        return new Response(`quota ${TOPIC_MARKER}`, {
          status: 429, headers: { "retry-after": "3" },
        });
      }
      return json({ updates: { updatedRange: `'${SHEET_TAB}'!A2:O2` } });
    }
    if (path.endsWith("!A1:O1")) return json({ values: [THREAD_HEADERS] });
    return json({ values: [] });
  };
  try {
    const { result, logs, clock } = await syncThreads({
      bridge: threadBridge(threadPayload([threadRow(1), threadRow(2)])).bridge,
      tokenProvider: async () => "fixture-access-token",
    });
    assert.equal(result.appended, 2);
    assert.equal(result.failed, 0);
    // Three read gaps, the 3 s Retry-After in 1 s slices, then write spacing.
    assert.deepEqual(clock.sleeps,
      [1_000, 100, 1_000, 100, 1_000, 100, 1_000, 1_000, 1_000, 1_100]);
    const workbook = `https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}`;
    for (const { url, init } of seen) {
      assert.ok(url.startsWith(`${workbook}/`) || url.startsWith(`${workbook}?`), url);
      assert.ok(init.signal instanceof AbortSignal, "every call is bounded");
      assert.equal(init.redirect, "error");
    }
    // The header, the grid size, both pages of the 1,000-row grid, then the appends.
    assert.equal(seen.filter(({ url }) => isMetadata(url)).length, 1);
    assert.doesNotMatch(logs.join("\n"), new RegExp(`${TOPIC_MARKER}|fixture-access-token`));
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("thread sync: the time budget ends a slow read between pages, a stop and no failure",
  async () => {
    const realFetch = globalThis.fetch;
    const clock = fakeClock(0);
    const pages = [];
    globalThis.fetch = async (url) => {
      const path = decodeURIComponent(new URL(url).pathname);
      if (isMetadata(url)) return gridAnswer(2_001); // four pages of 500
      if (path.endsWith("!A1:O1")) return jsonResponse({ values: [THREAD_HEADERS] });
      pages.push(path);
      clock.t += THREAD_SYNC_BUDGET_MS; // the first page alone outlasts the budget
      return jsonResponse({ values: Array.from({ length: 500 }, (_, i) => threadRow(i)) });
    };
    try {
      const { result, logs } = await syncThreads({
        clock,
        bridge: threadBridge(threadPayload([threadRow(1)])).bridge,
        tokenProvider: async () => "fixture-access-token",
      });
      assert.equal(pages.length, 1); // stopped before the second page, not after the last
      assert.equal(result.stopped, "deadline");
      assert.equal(result.failed, 0);
      assert.equal(result.error, undefined);
      assert.equal(result.remaining, null); // no plan from a partial read
      assert.equal(result.updated + result.appended, 0);
      assert.equal(logs.length, 1);
    } finally {
      globalThis.fetch = realFetch;
    }
  });

test("thread sync: a SIGTERM during the read stops it between pages the same way", async () => {
  const realFetch = globalThis.fetch;
  let pages = 0;
  globalThis.fetch = async (url) => {
    if (isMetadata(url)) return gridAnswer(2_001);
    if (decodeURIComponent(new URL(url).pathname).endsWith("!A1:O1")) {
      return jsonResponse({ values: [THREAD_HEADERS] });
    }
    pages += 1;
    return jsonResponse({ values: Array.from({ length: 500 }, (_, i) => threadRow(i)) });
  };
  try {
    const { result } = await syncThreads({
      bridge: threadBridge(threadPayload([threadRow(1)])).bridge,
      tokenProvider: async () => "fixture-access-token",
      shouldStop: () => pages >= 1,
    });
    assert.equal(pages, 1);
    assert.equal(result.stopped, "signal");
    assert.equal(result.failed, 0);
    assert.equal(result.error, undefined);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("thread Sheets transport preserves the shared read gate stop as stopped", async () => {
  const realFetch = globalThis.fetch;
  const clock = fakeClock(0);
  let stop = false;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    stop = true; // the next request is blocked by the shared gate
    return jsonResponse({ values: [THREAD_HEADERS] });
  };
  try {
    const readGate = createSheetsReadGate({ sleep: clock.sleep, now: clock.now });
    const { result } = await syncThreads({
      clock,
      bridge: threadBridge(threadPayload([threadRow(1)])).bridge,
      tokenProvider: async () => "fixture-access-token",
      readGate,
      shouldStop: () => stop,
    });
    assert.equal(calls, 1);
    assert.equal(result.stopped, "signal");
    assert.equal(result.failed, 0);
    assert.equal(result.error, undefined);
    assert.equal(result.updated + result.appended, 0);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("registry bridge: thread rows come from the image's export-rows, bounded", async () => {
  const seen = [];
  const exec = async (file, args, options) => {
    seen.push({ file, args, options });
    return { stdout: JSON.stringify({ rows: [] }) };
  };
  const bridge = makeRegistryBridge({ container: "c1", db: "/data/x.sqlite3", exec });
  assert.deepEqual(await bridge.threads.exportRows(), { rows: [] });
  await bridge.outbox.pending(1);
  assert.deepEqual(seen[0].args, [
    "exec", "-w", "/app/scripts", "c1", "python3", "-m", "simple_tools.thread_registry",
    "export-rows",
  ]);
  assert.equal(seen[0].file, "docker");
  assert.equal(seen[0].options.timeout, REGISTRY_CALL_TIMEOUT_MS);
  assert.equal(seen[0].options.killSignal, "SIGKILL");
  assert.equal(seen[0].options.maxBuffer, THREAD_EXPORT_MAX_BYTES);
  assert.equal(THREAD_EXPORT_MAX_BYTES, 64 * 1024 * 1024);
  // The outbox read is unchanged: the registry module, its own buffer.
  assert.ok(seen[1].args.includes("simple_tools.operator_registry"));
  assert.equal(seen[1].options.maxBuffer, 256 * 1024 * 1024);
});

test("pacing surface: Sessions by default; THREAD_SURFACE paces the thread adapter", async () => {
  const { adapter } = scriptedAdapter();
  const sessionNames = [...SESSION_SURFACE.reads, ...SESSION_SURFACE.writes].sort();
  assert.deepEqual(Object.keys(pacedAdapter(adapter)).sort(), sessionNames);
  const clock = fakeClock();
  const { adapter: sheet } = threadSheet();
  const paced = pacedAdapter(sheet, {
    sleep: clock.sleep, now: clock.now, surface: THREAD_SURFACE,
  });
  assert.deepEqual(
    Object.keys(paced).sort(),
    ["appendRow", "readKeys", "readRows", "updateRow", "validateHeaders"]);
  await paced.appendRow(threadRow(1));
  await paced.updateRow(2, threadRow(1));
  assert.deepEqual(clock.sleeps, [1_100]);
  assert.throws(() => pacedAdapter(sheet, { surface: { reads: ["readRows"] } }), /surface/);
});

// ---- runOnce: both exports in one run ----

const sessionsOnly = (bridge, adapter) => (args) => drainOnce({ ...args, bridge, adapter });

test("runOnce: threads first, then Sessions and Events with the deadline left", async () => {
  const clock = fakeClock(0);
  const order = [];
  const thread = threadSheet({ onWrite: () => order.push("thread write") });
  const { bridge: tBridge } = threadBridge(threadPayload([threadRow(1)]));
  const { bridge: sBridge, state } = fakeRegistry(["r1", "r2"]);
  const { adapter: sSheet } = fakeSheet({ onWrite: () => order.push("session write") });
  let deadlineMs = null;
  let threadReadGate = null;
  let sessionReadGate = null;
  const out = await runOnce({
    env: RUN_ENV, log: () => {}, now: clock.now,
    threads: async (args) => {
      threadReadGate = args.readGate;
      const res = await syncThreadsOnce({
        ...args, bridge: tBridge, adapter: thread.adapter, pacing: { sleep: clock.sleep },
      });
      clock.t += 60_000; // the thread phase took a minute
      return res;
    },
    sessions: (args) => {
      deadlineMs = args.deadlineMs;
      sessionReadGate = args.readGate;
      return sessionsOnly(sBridge, sSheet)(args);
    },
  });
  assert.equal(out.threads.appended, 1);
  assert.deepEqual(out.totals, { exported: 2, failed: 0 });
  assert.deepEqual(state.marked, ["ob-r1", "ob-r2"]);
  assert.deepEqual(order, ["thread write", "session write", "session write"]);
  assert.equal(deadlineMs, RUN_DEADLINE_MS - 60_000);
  assert.ok(threadReadGate);
  assert.equal(sessionReadGate, threadReadGate);
});

test("runOnce: a failed or thrown thread sync leaves the drain as it runs alone", async () => {
  const ids = ["r1", "r2", "r3"];
  const alone = fakeRegistry(ids);
  const aloneSheet = fakeSheet();
  const baseline = await drainOnce({
    env: RUN_ENV, bridge: alone.bridge, adapter: aloneSheet.adapter, log: () => {},
  });
  const payload = threadPayload([threadRow(1)]);
  const failures = {
    thrown: async () => { throw new Error(`thread phase blew up ${TOPIC_MARKER}`); },
    module: (args) => syncThreadsOnce({
      ...args, bridge: threadBridge(payload).bridge, adapter: threadSheet().adapter,
      loadModule: async () => { throw new SyntaxError("broken module"); },
    }),
    header: (args) => syncThreadsOnce({
      ...args, bridge: threadBridge(payload).bridge,
      adapter: threadSheet({ headers: ["Wrong"] }).adapter,
    }),
    uncertain: (args) => syncThreadsOnce({
      ...args, bridge: threadBridge(payload).bridge,
      adapter: threadSheet({ fail: [sheetsHttpError(503, true)] }).adapter,
    }),
  };
  for (const [label, threads] of Object.entries(failures)) {
    const { bridge, state } = fakeRegistry(ids);
    const sheet = fakeSheet();
    const logs = [];
    const out = await runOnce({
      env: RUN_ENV, log: (m) => logs.push(m), threads,
      sessions: sessionsOnly(bridge, sheet.adapter),
    });
    assert.deepEqual(out.totals, baseline, label);
    assert.deepEqual(sheet.appends, aloneSheet.appends, label);
    assert.deepEqual(state.marked, alone.state.marked, label);
    assert.equal(out.threads.failed, 1, label);
    assert.ok(/^[a-z0-9_]+$/.test(out.threads.error), label);
    assert.equal(logs.filter((m) => m.startsWith('{"threads"')).length, 1, label);
    assert.doesNotMatch(logs.join("\n"), new RegExp(TOPIC_MARKER), label);
  }
});

test("runOnce: a Sessions and Events failure leaves the thread sync's result intact", async () => {
  const thread = threadSheet();
  const { bridge: sBridge } = fakeRegistry(["r1"]);
  sBridge.outbox.pending = async () => { throw new Error("registry call timed out"); };
  const out = await runOnce({
    env: RUN_ENV, log: () => {},
    threads: (args) => syncThreadsOnce({
      ...args, bridge: threadBridge(threadPayload([threadRow(1), threadRow(2)])).bridge,
      adapter: thread.adapter, pacing: { writeIntervalMs: 0 },
    }),
    sessions: sessionsOnly(sBridge, fakeSheet().adapter),
  });
  assert.equal(out.totals.error, "registry call timed out");
  assert.equal(out.threads.appended, 2);
  assert.equal(out.threads.failed, 0);
  assert.equal(thread.state.rows.length, 2);
});

test("exit status: failed Sessions or Events rows fail the service; thread sync stays separate", async () => {
  const { bridge, state } = fakeRegistry(["r1"]);
  const { adapter } = fakeSheet({
    fail: { r1: [new OperatorSheetsError("transport_or_response_error", { uncertain: true })] },
  });
  const totals = await drainOnce({ env: RUN_ENV, bridge, adapter, log: () => {} });
  assert.deepEqual(totals, { exported: 0, failed: 1 });
  assert.deepEqual(state.marked, []); // the failed row remains pending for reconciliation
  assert.equal(drainExitCode(totals), 1);

  const successfulRegistry = fakeRegistry(["r1"]);
  const successfulSheet = fakeSheet();
  const threadFailure = await runOnce({
    env: RUN_ENV, log: () => {},
    threads: async () => ({ failed: 1, error: "sheet_header_mismatch" }),
    sessions: sessionsOnly(successfulRegistry.bridge, successfulSheet.adapter),
  });
  assert.equal(threadFailure.threads.failed, 1);
  assert.deepEqual(threadFailure.totals, { exported: 1, failed: 0 });
  assert.equal(drainExitCode(threadFailure.totals), 0);
  assert.equal(drainExitCode({ exported: 0, failed: 0, error: "registry timeout" }), 1);
  assert.equal(drainExitCode({ exported: 1, failed: 0 }, 1), 1); // interval runs retain prior failure
});

test("runOnce: SIGTERM in the thread sync stops it; the drain sends nothing", async () => {
  let writes = 0;
  const thread = threadSheet({ onWrite: () => { writes += 1; } });
  const { bridge: sBridge, state } = fakeRegistry(["r1", "r2"]);
  const sSheet = fakeSheet();
  const clock = fakeClock(0);
  const out = await runOnce({
    env: RUN_ENV, log: () => {}, now: clock.now, shouldStop: () => writes >= 1,
    threads: (args) => syncThreadsOnce({
      ...args, bridge: threadBridge(threadPayload([threadRow(1), threadRow(2)])).bridge,
      adapter: thread.adapter, pacing: { sleep: clock.sleep },
    }),
    sessions: sessionsOnly(sBridge, sSheet.adapter),
  });
  assert.equal(out.threads.appended, 1);
  assert.equal(out.threads.stopped, "signal");
  assert.deepEqual(out.totals, { exported: 0, failed: 0, stopped: "signal" });
  assert.deepEqual(sSheet.appends, []);
  assert.deepEqual(state.marked, []);
});

test("main: every run goes through runOnce, one run and one lease for both", async () => {
  const source = await readFile(
    new URL("./simple_tools/operator_sheets_drain.mjs", import.meta.url), "utf8");
  const main = source.slice(source.indexOf("async function main()"));
  assert.match(main, /await runOnce\(/);
  assert.doesNotMatch(main, /await drainOnce\(/);
  assert.ok(main.indexOf("acquireLease(") < main.indexOf("await runOnce("));
});
