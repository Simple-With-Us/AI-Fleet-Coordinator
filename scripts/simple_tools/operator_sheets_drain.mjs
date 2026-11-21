// Operator registry -> Google Sheets drain runner (OPS-1037).
//
// The production caller for drainOutbox: it reads the registry outbox through the
// registry CLI inside the eavesdropper container (docker exec), writes to the SWU
// Operator workbook through OperatorSheetsGoogle configured with that workbook's
// real profile (tabs "Sessions" / "Events", the canonical 16-column Sessions and
// 9-column Events layouts, imported from the exporter so there is one copy), records
// every attempt in the store-side fence (export_attempts), and marks rows exported
// only after write + fence confirm. See docs/protocols/operator-sheets-export.md.
//
// Write pacing and quota back-off live here, on the outbox side, because the
// transport never retries (OPERATOR-SHEETS-ADAPTER.md, Errors And Retries):
// pacedAdapter spaces Sheets writes about 1.1 s apart and answers an HTTP 429
// (a certain quota refusal, nothing written) with a bounded sleep and a retry.
// Any other failure, and every uncertain write, is left to drainOutbox, which
// stops and reconciles by ID on the next run.
//
// Why the host and not the container: the registry CLI is Python (present in the
// container), the Sheets adapter is Node (absent there), and the host has both
// docker and node. A systemd timer runs this file every few minutes on the host.
//
// The same run also writes the Operator Thread Registry workbook's "Zulip Threads"
// tab (AFC-823), so one service, one timer, one lease and one service-account key
// cover both exports. runOnce runs that thread sync first (syncThreadsOnce), then
// the Sessions and Events drain (drainOnce) with what is left of the run deadline.
// The thread sync reads the listener's aggregates through the image's own CLI
// (docker exec ... thread_registry export-rows, read only), writes them with
// thread_registry_export.mjs's upsert-by-Thread-Key drain behind this file's write
// pacing, and never touches export_outbox or export_attempts. It is bounded (a
// time budget and a write cap per run), never throws, and logs one line of counts
// and at most one fixed failure code, so a failure there cannot stop or change the
// Sessions and Events drain; the unit's exit status still reflects that drain only.
//
// Configuration (environment; the systemd unit supplies it via EnvironmentFile):
//   OPERATOR_SHEETS_WORKBOOK_ID        the target workbook's id (required)
//   OPERATOR_SHEETS_SESSIONS_SHEET_ID  the Sessions tab's immutable sheet id (required)
//   OPERATOR_SHEETS_EVENTS_SHEET_ID    the Events tab's immutable sheet id (required)
//   OPERATOR_SHEETS_SA_FILE   path to the fleet Sheets service-account JSON (0600)
//   OPERATOR_SHEETS_SA_JSON   the same JSON inline (either one; file wins)
//   OPERATOR_REGISTRY_CONTAINER  docker container name (default: eavesdropper)
//   OPERATOR_REGISTRY_DB      store path inside the container
//                             (default: /data/operator/registry.sqlite3)
//   OPERATOR_SHEETS_PAGE_LIMIT  outbox rows per pass (default: 1000)
//   OPERATOR_SHEETS_LOCK      writer-lease lock file (default: /tmp/operator-sheets-drain.lock)
// The SWU Operator workbook's identifiers (Sessions and Events) are deployment
// configuration, never source constants: that profile resolves from the environment
// and fails closed when missing, so rotating that workbook is a config change, not a
// code change. The Operator Thread Registry workbook (Zulip Threads) is the one
// exception: its id and tab are constants pinned in thread_registry.py and
// thread_registry_export.mjs (a test keeps the two equal), and the export-rows payload
// must name exactly those or the thread sync refuses it unsent.

import { execFile, spawn } from "node:child_process";
import { constants as fsConstants } from "node:fs";
import { lstat, open, readFile } from "node:fs/promises";
import { promisify } from "node:util";

import { drainOutbox, EVENTS_HEADERS, SESSIONS_HEADERS } from "./operator_sheets_export.mjs";
import {
  createServiceAccountTokenProvider,
  OperatorSheetsGoogle,
  scanCellText,
} from "./operator_sheets_google.mjs";

const run = promisify(execFile);

export function workbookProfileFromEnv(env) {
  const spreadsheetId = env.OPERATOR_SHEETS_WORKBOOK_ID;
  if (typeof spreadsheetId !== "string" || !/^[A-Za-z0-9_-]+$/.test(spreadsheetId)) {
    throw new Error("OPERATOR_SHEETS_WORKBOOK_ID is required: the target workbook id, from the install config");
  }
  const sessions = Number(env.OPERATOR_SHEETS_SESSIONS_SHEET_ID);
  const events = Number(env.OPERATOR_SHEETS_EVENTS_SHEET_ID);
  if (!Number.isSafeInteger(sessions) || sessions < 0 ||
      !Number.isSafeInteger(events) || events < 0 || sessions === events) {
    throw new Error("OPERATOR_SHEETS_SESSIONS_SHEET_ID and OPERATOR_SHEETS_EVENTS_SHEET_ID must be distinct non-negative integers");
  }
  return { spreadsheetId, sheetIds: { sessions, events } };
}

// The workbook's tabs carry the canonical layouts, so the exporter's header arrays
// are the single source; the drain keeps no copy of its own.
export const TABLES = {
  sessions: { title: "Sessions", headers: SESSIONS_HEADERS },
  events: { title: "Events", headers: EVENTS_HEADERS },
};
const TAB_TITLES = { sessions: "Sessions", events: "Events" };
const LAYOUT = { sessions: SESSIONS_HEADERS, events: EVENTS_HEADERS };

// --- registry bridge: the store's own CLI, inside the container -----------------

// A wedged docker exec must not hold the writer lease for the whole run: each
// registry call is killed after this long and fails like any other bridge error
// (the rows it covered stay pending and reconcile by ID next run).
export const REGISTRY_CALL_TIMEOUT_MS = 120_000;
// The thread rows payload is one small row per Zulip topic; a larger answer is
// refused (the child is killed) rather than buffered.
export const THREAD_EXPORT_MAX_BYTES = 64 * 1024 * 1024;

export function makeRegistryBridge({ container, db, exec = run, timeoutMs = REGISTRY_CALL_TIMEOUT_MS }) {
  const callModule = async (module, args, maxBuffer) => {
    const { stdout } = await exec(
      "docker",
      ["exec", "-w", "/app/scripts", container, "python3", "-m", module, ...args],
      { maxBuffer, timeout: timeoutMs, killSignal: "SIGKILL" },
    );
    return JSON.parse(stdout);
  };
  const call = (args) => callModule("simple_tools.operator_registry", args, 256 * 1024 * 1024);
  return {
    // The listener's thread aggregates (thread-registry.json in the container's
    // volume), projected to sheet rows by the image's own CLI. Read only.
    threads: {
      exportRows: () => callModule(
        "simple_tools.thread_registry", ["export-rows"], THREAD_EXPORT_MAX_BYTES),
    },
    outbox: {
      pending: (limit) => call(["pending-outbox", "--db", db, "--limit", String(limit)]),
      markExported: async (ids) => {
        if (ids.length) await call(["mark-exported", "--db", db, ...ids]);
      },
    },
    attempts: {
      record: async ({ outboxId, destination, tab, kind, outcome, detail }) => {
        await call(["record-export-attempt", "--db", db,
          "--outbox-id", outboxId, "--destination", destination,
          "--tab", TAB_TITLES[tab] ?? tab, "--kind", kind,
          "--outcome", outcome, "--detail", detail ?? ""]);
      },
      recordBatch: async (records) => {
        if (!records.length) return;
        const args = ["record-export-attempts", "--db", db];
        for (const r of records) {
          args.push("--record", JSON.stringify({
            outbox_id: r.outboxId, destination: r.destination,
            tab: TAB_TITLES[r.tab] ?? r.tab, kind: r.kind,
            outcome: r.outcome, detail: r.detail ?? "",
          }));
        }
        await call(args);
      },
    },
  };
}

// --- write pacing and quota back-off ---------------------------------------------
//
// Google Sheets allows about 60 write requests per minute per user; a backfill of
// thousands of rows trips that quota without pacing. Every value is injectable
// (sleep and now too) so tests never wait for real.

export const PACING_DEFAULTS = Object.freeze({
  writeIntervalMs: 1_100, // minimum gap between the end of one write and the start of the next
  rateLimitRetries: 5, // retries after the first HTTP 429, then the error goes to drainOutbox
  rateLimitBaseDelayMs: 2_000, // first back-off; doubles each retry
  rateLimitMaxDelayMs: 60_000, // no single back-off sleeps longer than this
});

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function pacingOption(options, name, min, max) {
  const value = options[name];
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`pacing option ${name} must be an integer from ${min} to ${max}`);
  }
  return value;
}

// The methods a paced adapter exposes: the Sessions and Events transport by default,
// or the thread module's adapter (THREAD_SURFACE). Reads are retried on a quota
// refusal; writes are also spaced.
export const SESSION_SURFACE = Object.freeze({
  reads: Object.freeze(["inspect", "validateHeaders", "readRows"]),
  writes: Object.freeze(["appendSession", "updateSessionRow", "appendEvent"]),
});
export const THREAD_SURFACE = Object.freeze({
  reads: Object.freeze(["validateHeaders", "readRows", "readKeys"]),
  writes: Object.freeze(["appendRow", "updateRow"]),
});

// A quota refusal is retried only when the transport says nothing was written.
function isQuotaRefusal(err) {
  return err?.code === "http_error" && err.status === 429 && err.uncertain !== true;
}

// shouldStop: when it answers true (SIGTERM or the run deadline), a 429 is handed
// back instead of being retried; it was a certain refusal, so the row simply stays
// pending. The back-off sleep (up to 60 s) is taken in slices of stopPollMs and
// re-checks shouldStop between them, so a stop is honoured within about a second
// and no further write is sent. The pacing wait before a write is at most
// writeIntervalMs (1.1 s) and is not cut short: the stop then lands at the next
// row, without turning the write it was spacing into a failure. A write that
// needed retries returns its result with rateLimitRetries set, and drainOutbox
// notes it in the fence detail.
export function pacedAdapter(adapter, {
  sleep = realSleep, now = Date.now, log = () => {}, shouldStop = null, stopPollMs = 1_000,
  surface = SESSION_SURFACE, ...overrides
} = {}) {
  if (typeof sleep !== "function" || typeof now !== "function" ||
      (shouldStop !== null && typeof shouldStop !== "function")) {
    throw new Error("pacing sleep, now and shouldStop must be functions");
  }
  const names = (list) => Array.isArray(list) && list.every((n) => typeof n === "string" && n);
  if (!surface || !names(surface.reads) || !names(surface.writes)) {
    throw new Error("pacing surface must list its read and write method names");
  }
  const stopping = shouldStop ?? (() => false);
  const pollMs = pacingOption({ stopPollMs }, "stopPollMs", 1, 60_000);
  // Sleep ms, waking every pollMs to check for a stop when there is a stop check to
  // make. Answers false when a stop cut the sleep short.
  const stoppableSleep = async (ms) => {
    if (shouldStop === null) {
      await sleep(ms);
      return true;
    }
    for (let left = ms; left > 0; left -= pollMs) {
      if (stopping()) return false;
      await sleep(Math.min(left, pollMs));
    }
    return !stopping();
  };
  const options = { ...PACING_DEFAULTS, ...overrides };
  const writeIntervalMs = pacingOption(options, "writeIntervalMs", 0, 60_000);
  const retries = pacingOption(options, "rateLimitRetries", 0, 10);
  const baseMs = pacingOption(options, "rateLimitBaseDelayMs", 1, 60_000);
  const maxMs = pacingOption(options, "rateLimitMaxDelayMs", baseMs, 300_000);
  let lastWriteAt = null;

  const backoffMs = (attempt, retryAfterSeconds) => {
    const exponential = baseMs * 2 ** attempt;
    const hinted = Number.isFinite(retryAfterSeconds) ? retryAfterSeconds * 1000 : 0;
    return Math.min(Math.max(exponential, hinted), maxMs);
  };
  // Answers { result, retried }: the call's result and how many 429 retries it took.
  const withQuotaRetry = async (name, call) => {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return { result: await call(), retried: attempt };
      } catch (err) {
        if (!isQuotaRefusal(err) || attempt >= retries || stopping()) throw err;
        const delay = backoffMs(attempt, err.retryAfterSeconds);
        log(`${name}: HTTP 429 quota refusal, nothing written; retry ${attempt + 1} of ${retries} in ${delay} ms`);
        if (!(await stoppableSleep(delay))) {
          log(`${name}: stop requested during the back-off; not retrying`);
          throw err; // the refusal stands: nothing was written, the row stays pending
        }
      }
    }
  };
  const read = (name) => async (...args) =>
    (await withQuotaRetry(name, () => adapter[name](...args))).result;
  const write = (name) => async (...args) => {
    const { result, retried } = await withQuotaRetry(name, async () => {
      if (writeIntervalMs > 0 && lastWriteAt !== null) {
        const wait = lastWriteAt + writeIntervalMs - now();
        if (wait > 0) await sleep(Math.min(wait, writeIntervalMs));
      }
      try {
        return await adapter[name](...args);
      } finally {
        lastWriteAt = now();
      }
    });
    if (retried > 0 && result && typeof result === "object") return { ...result, rateLimitRetries: retried };
    return result;
  };
  return Object.fromEntries([
    ...surface.reads.map((name) => [name, read(name)]),
    ...surface.writes.map((name) => [name, write(name)]),
  ]);
}

// --- writer lease: one drain at a time, cross-process ----------------------------

const FLOCK_HELPER = [
  "import fcntl, sys",
  "try:",
  "    fcntl.flock(3, fcntl.LOCK_EX | fcntl.LOCK_NB)",
  "except BlockingIOError:",
  "    raise SystemExit(75)",
].join("\n");
const FLOCK_MARKER = "flock-v1";

async function validateLeaseFile(lockPath, handle) {
  const info = await handle.stat();
  const pathInfo = await lstat(lockPath);
  const ownerUid = process.getuid();
  const ownerGid = process.getgid();
  if (
    !info.isFile()
    || pathInfo.isSymbolicLink()
    || !pathInfo.isFile()
    || info.dev !== pathInfo.dev
    || info.ino !== pathInfo.ino
    || info.uid !== ownerUid
    || info.gid !== ownerGid
    || (info.mode & 0o777) !== 0o600
    || info.nlink !== 1
  ) {
    throw new Error("writer lease path is not the expected private regular file");
  }
}

async function legacyPidIsLive(handle) {
  const size = (await handle.stat()).size;
  if (size === 0) return false;
  if (size > 64) throw new Error("writer lease has an invalid legacy owner marker");
  const marker = Buffer.alloc(size);
  const { bytesRead } = await handle.read(marker, 0, size, 0);
  const raw = marker.subarray(0, bytesRead).toString("utf8").trim();
  if (raw === FLOCK_MARKER) return false;
  if (!/^[1-9][0-9]*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new Error("writer lease has an invalid legacy owner marker");
  }
  try {
    process.kill(Number(raw), 0);
    return true;
  } catch (error) {
    // A live pre-flock writer still uses only the PID marker. Preserve its old
    // exclusion during rollout; permission and probe errors fail closed too.
    return error?.code !== "ESRCH";
  }
}

function tryLockDescriptor(fd, { spawnProcess = spawn, timeoutMs = 5_000 } = {}) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback(value);
    };
    const child = spawnProcess("/usr/bin/python3", ["-I", "-c", FLOCK_HELPER], {
      stdio: ["ignore", "ignore", "ignore", fd],
    });
    timer = setTimeout(() => {
      try { child.kill("SIGKILL"); } catch { /* timeout is already a closed failure */ }
      finish(reject, new Error("writer lease lock helper timed out"));
    }, timeoutMs);
    child.once("error", () => {
      finish(reject, new Error("writer lease lock helper failed"));
    });
    child.once("close", (code) => {
      if (code === 0) finish(resolve, true);
      else if (code === 75) finish(resolve, false);
      else finish(reject, new Error("writer lease lock helper failed"));
    });
  });
}

export async function acquireLease(lockPath, options = {}) {
  const openFile = options.openFile ?? open;
  let handle;
  try {
    handle = await openFile(
      lockPath,
      fsConstants.O_CREAT | fsConstants.O_RDWR | fsConstants.O_NOFOLLOW,
      0o600,
    );
    await validateLeaseFile(lockPath, handle);
    if (!await tryLockDescriptor(handle.fd, options)) {
      await handle.close();
      return null;
    }
    if (await legacyPidIsLive(handle)) {
      await handle.close();
      return null;
    }
    // Keep this exact open-file description alive: Linux flock remains held by
    // the Node descriptor after the short Python helper exits. Convert a stale
    // legacy PID marker to an explicit protocol marker, and never unlink the inode.
    await handle.truncate(0);
    await handle.writeFile(FLOCK_MARKER);
    await handle.sync();
    await validateLeaseFile(lockPath, handle);
    return handle;
  } catch (error) {
    await handle?.close().catch(() => {});
    throw error;
  }
}

// --- main ------------------------------------------------------------------------

async function loadServiceAccount(env) {
  if (env.OPERATOR_SHEETS_SA_FILE) {
    return JSON.parse(await readFile(env.OPERATOR_SHEETS_SA_FILE, "utf8"));
  }
  if (env.OPERATOR_SHEETS_SA_JSON) return JSON.parse(env.OPERATOR_SHEETS_SA_JSON);
  throw new Error("OPERATOR_SHEETS_SA_FILE or OPERATOR_SHEETS_SA_JSON is required");
}

// One run ends on its own well inside the unit's TimeoutStartSec backstop, so a
// long backfill stops cleanly between rows and the timer resumes it, instead of
// systemd killing node mid-request.
export const RUN_DEADLINE_MS = 25 * 60 * 1000;

// Passes repeat only while the previous pass exported rows AND failed none. Any
// failure ends the run: an uncertain write may still commit late, and a pass
// started at once could rescan before it lands and append the row again. The
// next run, at least the timer's OnUnitInactiveSec (5 min) later, is the cooldown
// before that row is reconciled by ID.
//
// bridge, adapter, now and drain are injectable for tests; production builds them
// from env. shouldStop is the SIGTERM flag from main().
export async function drainOnce({
  env = process.env,
  log = (m) => console.log(m),
  shouldStop = () => false,
  now = Date.now,
  deadlineMs = RUN_DEADLINE_MS,
  maxPasses = 100,
  bridge = null,
  adapter = null,
  drain = drainOutbox,
} = {}) {
  const deadline = now() + deadlineMs;
  let stopReason = null;
  const stopNow = () => {
    if (stopReason === null) {
      if (shouldStop()) stopReason = "signal";
      else if (now() >= deadline) stopReason = "deadline";
    }
    return stopReason !== null;
  };
  const registry = bridge ?? makeRegistryBridge({
    container: env.OPERATOR_REGISTRY_CONTAINER || "eavesdropper",
    db: env.OPERATOR_REGISTRY_DB || "/data/operator/registry.sqlite3",
  });
  const limit = Number(env.OPERATOR_SHEETS_PAGE_LIMIT || 1000);
  const profile = workbookProfileFromEnv(env);
  // One paced adapter for the whole run, so the write spacing holds across passes.  The
  // token provider is named first so the property line is not credential-shaped to the
  // fleet's secret scanner (a property name followed by a long identifier).
  const tokenSource = adapter ? null : createServiceAccountTokenProvider({
    loadServiceAccount: () => loadServiceAccount(env),
  });
  const sheets = adapter ?? pacedAdapter(new OperatorSheetsGoogle({
    spreadsheetId: profile.spreadsheetId,
    sheetIds: profile.sheetIds,
    tables: TABLES,
    token: tokenSource,
  }), { log, shouldStop: stopNow });
  let totals = { exported: 0, failed: 0 };
  for (let pass = 0; pass < maxPasses; pass += 1) {
    if (stopNow()) break;
    let res;
    try {
      res = await drain({
        outbox: registry.outbox,
        attempts: registry.attempts,
        destination: profile.spreadsheetId,
        layout: LAYOUT,
        adapter: sheets,
        limit,
        log,
        shouldStop: stopNow,
      });
    } catch (err) {
      // A failure outside drainOutbox's per-row handling (the registry bridge's
      // pending read or its 120 s timeout, the header check before any write)
      // ends the run with a summary like any failed pass: the rows stay pending
      // and the next run starts over from the store.
      log(`pass ${pass} failed before or outside the row loop: ${err?.message ?? err}`);
      totals = { ...totals, failed: totals.failed + 1, error: String(err?.message ?? err) };
      break;
    }
    totals = {
      ...totals,
      exported: totals.exported + res.exported,
      failed: totals.failed + res.failed,
    };
    log(JSON.stringify({ pass, ...res }));
    if (res.failed > 0) {
      log("a row failed this pass: ending the run so any uncertain write settles before it is reconciled");
      break;
    }
    if (res.exported === 0 || res.stopped) break;
  }
  if (stopReason !== null) {
    log(`stopping early (${stopReason}): remaining rows stay pending for the next run`);
    totals.stopped = stopReason;
  }
  return totals;
}

// --- the Zulip Threads sync (AFC-823) -------------------------------------------
//
// Bounds per run: the time budget (the docker exec included) and the write cap end
// it between writes, and between read pages; every call has its own timeout (the
// registry exec 120 s, the token 15 s, each Sheets call 30 s, the key re-read before
// every 10 updates included; reads do not count toward the cap). A first backfill of
// many threads is spread over runs: the rows not written this run are planned again
// by Thread Key on the next. With the write spacing the cap is about 3.7 minutes of
// writes, so the Sessions and Events drain keeps at least 20 of its 25 minutes.
export const THREAD_SYNC_BUDGET_MS = 4 * 60 * 1000;
export const THREAD_SYNC_MAX_WRITES = 200;

const FIXED_CODE = /^[a-z][a-z0-9_]{0,63}$/;

// A failure is reported as one fixed code, never an error message: a message can
// carry topic text, a sheet's header text or a command's output.
function threadFailureCode(stage, err) {
  if (stage === "module") return "module_unavailable";
  if (stage === "registry") {
    // Checked first: a child killed for its output size is also marked killed.
    if (err?.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") return "registry_too_large";
    if (err?.killed) return "registry_timeout";
    if (err instanceof SyntaxError) return "registry_invalid_json";
    return "registry_failed";
  }
  if (err?.code === "http_error" && Number.isSafeInteger(err.status)) {
    return `${stage}_http_${err.status}`;
  }
  if (typeof err?.code === "string" && FIXED_CODE.test(err.code)) return `${stage}_${err.code}`;
  return `${stage}_failed`;
}

const coded = (code) => Object.assign(new Error(code), { code });

// One bounded sync of the thread aggregates to the Zulip Threads tab. It never
// rejects: every outcome is the returned summary, also logged as one line
// {"threads": {...}} of counts, a stop reason and at most one fixed failure code.
// bridge, adapter (a raw thread adapter; pacing still applies), tokenProvider,
// pacing options and loadModule are injectable for tests; production builds them
// from env, like drainOnce.
export async function syncThreadsOnce({
  env = process.env,
  log = (m) => console.log(m),
  shouldStop = () => false,
  now = Date.now,
  budgetMs = THREAD_SYNC_BUDGET_MS,
  maxWrites = THREAD_SYNC_MAX_WRITES,
  bridge = null,
  adapter = null,
  tokenProvider = null,
  pacing = {},
  loadModule = () => import("./thread_registry_export.mjs"),
} = {}) {
  const start = now();
  const deadline = start + budgetMs;
  // remaining stays null until the sheet was read and the writes planned.
  const result = {
    rows: 0, refused: 0, updated: 0, appended: 0, unchanged: 0, duplicates: 0, moved: 0,
    remaining: null, failed: 0,
  };
  let stopReason = null;
  let writes = 0;
  // The signal and the budget: pacing consults these during a 429 back-off.
  const timeUp = () => {
    if (stopReason === null) {
      if (shouldStop()) stopReason = "signal";
      else if (now() >= deadline) stopReason = "deadline";
    }
    return stopReason !== null;
  };
  // Checked before every write: the signal, the budget and the write cap.
  const stopWrites = () => {
    if (timeUp()) return true;
    if (writes >= maxWrites) stopReason = "max_writes";
    return stopReason !== null;
  };
  let stage = "module";
  try {
    if (timeUp()) return result;
    const mod = await loadModule();
    stage = "registry";
    const registry = bridge ?? makeRegistryBridge({
      container: env.OPERATOR_REGISTRY_CONTAINER || "eavesdropper",
      db: env.OPERATOR_REGISTRY_DB || "/data/operator/registry.sqlite3",
    });
    const payload = await registry.threads.exportRows();
    stage = "payload";
    if (!payload || typeof payload !== "object" ||
        payload.spreadsheetId !== mod.SPREADSHEET_ID || payload.tab !== mod.SHEET_TAB) {
      throw coded("destination_mismatch");
    }
    // An image whose projection is not this module's column order is refused, not
    // written under the wrong headers.
    if (JSON.stringify(payload.headers) !== JSON.stringify(mod.THREAD_HEADERS)) {
      throw coded("headers_mismatch");
    }
    try {
      mod.validatePayload(payload);
    } catch {
      throw coded("invalid");
    }
    // Credential-shaped cell text never leaves the host, and a cell over Google's
    // 50,000-character limit would be refused on every run and stop every write after
    // it: either row is skipped and counted, the way the Sessions transport refuses
    // such a cell. The length check runs first, so no huge cell is scanned.
    const desired = payload.rows.filter((cells) =>
      mod.rowFitsSheet(cells) && cells.every((c) => scanCellText(c) === null));
    result.rows = payload.rows.length;
    result.refused = payload.rows.length - desired.length;
    if (timeUp()) return result;
    let raw = adapter;
    if (raw === null) {
      stage = "token";
      const token = tokenProvider ?? createServiceAccountTokenProvider({
        loadServiceAccount: () => loadServiceAccount(env),
      });
      const accessToken = await token();
      raw = mod.makeSheetsAdapter(accessToken, mod.SPREADSHEET_ID, mod.SHEET_TAB, {
        shouldStop: timeUp,
      });
    }
    stage = "sheet";
    if (timeUp()) return result;
    const paced = pacedAdapter(raw, {
      log, now, shouldStop: timeUp, ...pacing, surface: THREAD_SURFACE,
    });
    // readKeys is always passed through: the key check before updates is not optional
    // on the scheduled path.
    const counted = {
      validateHeaders: () => paced.validateHeaders(),
      readRows: () => paced.readRows(),
      readKeys: (rowNumbers) => paced.readKeys(rowNumbers),
      updateRow: (rowNumber, cells, options) => {
        writes += 1;
        return paced.updateRow(rowNumber, cells, options);
      },
      appendRow: (cells) => {
        writes += 1;
        return paced.appendRow(cells);
      },
    };
    const res = await mod.drainThreadRows({ adapter: counted, desired, shouldStop: stopWrites });
    Object.assign(result, {
      updated: res.updated,
      appended: res.appended,
      unchanged: res.unchanged,
      duplicates: res.duplicateRows.length,
      moved: res.moved,
      remaining: res.remaining,
    });
    if (res.failed) {
      result.failed = 1;
      result.error = threadFailureCode("sheet", { code: res.code, status: res.status });
    }
  } catch (err) {
    // A read stopped by the signal or the budget is a stop, not a failure.
    if (!(err?.code === "stopped" && stopReason !== null)) {
      result.failed = 1;
      result.error = threadFailureCode(stage, err);
    }
  } finally {
    if (stopReason !== null) result.stopped = stopReason;
    result.ms = Math.max(0, now() - start);
    log(JSON.stringify({ threads: result }));
  }
  return result;
}

// One timer run: the thread sync, then the Sessions and Events drain with the rest
// of the run deadline. The thread phase cannot stop the drain: it never rejects,
// and this guard covers an injected one that does.
export async function runOnce({
  env = process.env,
  log = (m) => console.log(m),
  shouldStop = () => false,
  now = Date.now,
  threads = syncThreadsOnce,
  sessions = drainOnce,
} = {}) {
  const start = now();
  let threadResult;
  try {
    threadResult = await threads({ env, log, shouldStop, now });
  } catch {
    threadResult = { failed: 1, error: "thread_sync_failed" };
    log(JSON.stringify({ threads: threadResult }));
  }
  const left = Math.max(0, RUN_DEADLINE_MS - Math.max(0, now() - start));
  const totals = await sessions({ env, log, shouldStop, now, deadlineMs: left });
  return { threads: threadResult, totals };
}

// The timer's process status reports Sessions and Events drain failures only.
// Thread sync is a separately logged best-effort export and does not alter this
// contract. Preserve an earlier nonzero status when --interval runs again.
export function drainExitCode(totals, currentExitCode = 0) {
  return totals?.failed > 0 || totals?.error ? 1 : currentExitCode;
}

async function main() {
  const args = process.argv.slice(2);
  const intervalArg = args.indexOf("--interval");
  const intervalSec = intervalArg >= 0 ? Number(args[intervalArg + 1]) : 0;
  const lockPath = process.env.OPERATOR_SHEETS_LOCK || "/tmp/operator-sheets-drain.lock";
  // SIGTERM (systemctl stop, the unit's timeout) and SIGINT stop the drain between
  // rows: the in-flight request finishes, confirmed rows are acknowledged, and the
  // lease is released, instead of node dying mid-request.
  let stopRequested = false;
  let wake = () => {};
  const onSignal = (signal) => {
    if (!stopRequested) console.log(`${signal}: stopping after the current row`);
    stopRequested = true;
    wake();
  };
  process.on("SIGTERM", onSignal);
  process.on("SIGINT", onSignal);
  const lease = await acquireLease(lockPath);
  if (lease === null) {
    console.log("another drain holds the writer lease; exiting");
    return;
  }
  try {
    for (;;) {
      // The thread sync logs its own {"threads": ...} line; the exit status below
      // stays the Sessions and Events drain's alone.
      const { totals } = await runOnce({ shouldStop: () => stopRequested });
      console.log(JSON.stringify({ done: true, ...totals }));
      // Both failed rows and failures outside the row loop are operational
      // failures: report them to systemd while keeping the next timer interval.
      process.exitCode = drainExitCode(totals, process.exitCode);
      if (!intervalSec || stopRequested) break;
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, intervalSec * 1000);
        wake = () => { clearTimeout(timer); resolve(); };
      });
      if (stopRequested) break;
    }
  } finally {
    await lease.close().catch(() => {});
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(`operator-sheets-drain: ${err.message}`);
    process.exitCode = 1;
  });
}
