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
// Resource identifiers are deployment configuration, never source constants: the
// workbook profile resolves from the environment and fails closed when missing,
// so rotating the workbook is a config change, not a code change.

import { execFile } from "node:child_process";
import { open, readFile, unlink } from "node:fs/promises";
import { promisify } from "node:util";

import { drainOutbox, EVENTS_HEADERS, SESSIONS_HEADERS } from "./operator_sheets_export.mjs";
import {
  createServiceAccountTokenProvider,
  OperatorSheetsGoogle,
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

export function makeRegistryBridge({ container, db, exec = run, timeoutMs = REGISTRY_CALL_TIMEOUT_MS }) {
  const call = async (args) => {
    const { stdout } = await exec(
      "docker",
      ["exec", "-w", "/app/scripts", container, "python3", "-m",
        "agent_sync.operator_registry", ...args],
      { maxBuffer: 256 * 1024 * 1024, timeout: timeoutMs, killSignal: "SIGKILL" },
    );
    return JSON.parse(stdout);
  };
  return {
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
  ...overrides
} = {}) {
  if (typeof sleep !== "function" || typeof now !== "function" ||
      (shouldStop !== null && typeof shouldStop !== "function")) {
    throw new Error("pacing sleep, now and shouldStop must be functions");
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
  return {
    inspect: read("inspect"),
    validateHeaders: read("validateHeaders"),
    readRows: read("readRows"),
    appendSession: write("appendSession"),
    updateSessionRow: write("updateSessionRow"),
    appendEvent: write("appendEvent"),
  };
}

// --- writer lease: one drain at a time, cross-process ----------------------------

export async function acquireLease(lockPath) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const handle = await open(lockPath, "wx");
      await handle.writeFile(String(process.pid));
      return handle;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
      let pid = null;
      try {
        pid = Number((await readFile(lockPath, "utf8")).trim());
      } catch {
        pid = null; // unreadable: the lock is stale
      }
      let holderAlive = false;
      if (Number.isSafeInteger(pid) && pid > 0) {
        try {
          process.kill(pid, 0);
          holderAlive = true;
        } catch (probe) {
          // ESRCH (no such process) is the only answer that proves the holder is gone.
          // EPERM means it exists under another user; any other error proves nothing,
          // so the lock is kept rather than risk two drains writing at once.
          holderAlive = probe?.code !== "ESRCH";
        }
      }
      if (holderAlive) return null;
      await unlink(lockPath).catch(() => {});
    }
  }
  return null;
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
  // One paced adapter for the whole run, so the write spacing holds across passes.
  const sheets = adapter ?? pacedAdapter(new OperatorSheetsGoogle({
    spreadsheetId: profile.spreadsheetId,
    sheetIds: profile.sheetIds,
    tables: TABLES,
    token: createServiceAccountTokenProvider({
      loadServiceAccount: () => loadServiceAccount(env),
    }),
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
      const totals = await drainOnce({ shouldStop: () => stopRequested });
      console.log(JSON.stringify({ done: true, ...totals }));
      // A pass that failed outside the row loop (a wedged registry call, a header
      // mismatch) is an operational failure: report it to systemd as one.
      if (totals.error) process.exitCode = 1;
      if (!intervalSec || stopRequested) break;
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, intervalSec * 1000);
        wake = () => { clearTimeout(timer); resolve(); };
      });
      if (stopRequested) break;
    }
  } finally {
    await lease.close().catch(() => {});
    await unlink(lockPath).catch(() => {});
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(`operator-sheets-drain: ${err.message}`);
    process.exitCode = 1;
  });
}
