// Operator registry -> Google Sheets drain runner (OPS-1037).
//
// The production caller for drainOutbox: it reads the registry outbox through the
// registry CLI inside the eavesdropper container (docker exec), writes to the SWU
// Operator workbook through OperatorSheetsGoogle configured with that workbook's
// real profile (tabs "Sessions" / "Events", the 10-column Sessions layout), records
// every attempt in the store-side fence (export_attempts), and marks rows exported
// only after write + fence confirm. See docs/protocols/operator-sheets-export.md.
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

import { drainOutbox } from "./operator_sheets_export.mjs";
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

export const SESSIONS_HEADERS = [
  "Record ID", "Label", "Seat", "Platform", "Host", "Native Session ID",
  "Title", "Project", "Thread Link", "Apple Note Link",
];
export const EVENTS_HEADERS = [
  "Event ID", "Recorded At", "Session Record ID", "Label", "Event",
  "Actor", "Outcome", "Source", "Details",
];
export const TABLES = {
  sessions: { title: "Sessions", headers: SESSIONS_HEADERS },
  events: { title: "Events", headers: EVENTS_HEADERS },
};
const TAB_TITLES = { sessions: "Sessions", events: "Events" };
const LAYOUT = { sessions: SESSIONS_HEADERS, events: EVENTS_HEADERS };

// --- registry bridge: the store's own CLI, inside the container -----------------

export function makeRegistryBridge({ container, db }) {
  const call = async (args) => {
    const { stdout } = await run(
      "docker",
      ["exec", "-w", "/app/scripts", container, "python3", "-m",
        "agent_sync.operator_registry", ...args],
      { maxBuffer: 256 * 1024 * 1024 },
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

// --- writer lease: one drain at a time, cross-process ----------------------------

export async function acquireLease(lockPath) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const handle = await open(lockPath, "wx");
      await handle.writeFile(String(process.pid));
      return handle;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
      let holderAlive = false;
      try {
        const pid = Number((await readFile(lockPath, "utf8")).trim());
        if (Number.isSafeInteger(pid) && pid > 0) {
          process.kill(pid, 0);
          holderAlive = true;
        }
      } catch {
        holderAlive = false; // unreadable or dead holder: the lock is stale
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

export async function drainOnce({ env = process.env, log = (m) => console.log(m) } = {}) {
  const bridge = makeRegistryBridge({
    container: env.OPERATOR_REGISTRY_CONTAINER || "eavesdropper",
    db: env.OPERATOR_REGISTRY_DB || "/data/operator/registry.sqlite3",
  });
  const limit = Number(env.OPERATOR_SHEETS_PAGE_LIMIT || 1000);
  const profile = workbookProfileFromEnv(env);
  const adapter = new OperatorSheetsGoogle({
    spreadsheetId: profile.spreadsheetId,
    sheetIds: profile.sheetIds,
    tables: TABLES,
    token: createServiceAccountTokenProvider({
      loadServiceAccount: () => loadServiceAccount(env),
    }),
  });
  let totals = { exported: 0, failed: 0 };
  // Passes continue while a pass still exports rows: the backfill is several
  // thousand rows and one pass is capped. A pass that exports nothing ends the
  // run (remaining rows are failures or refusals, visible in the fence).
  for (let pass = 0; pass < 100; pass += 1) {
    const res = await drainOutbox({
      outbox: bridge.outbox,
      attempts: bridge.attempts,
      destination: profile.spreadsheetId,
      layout: LAYOUT,
      adapter,
      limit,
      log,
    });
    totals = {
      exported: totals.exported + res.exported,
      failed: totals.failed + res.failed,
    };
    log(JSON.stringify({ pass, ...res }));
    if (res.exported === 0) break;
  }
  return totals;
}

async function main() {
  const args = process.argv.slice(2);
  const intervalArg = args.indexOf("--interval");
  const intervalSec = intervalArg >= 0 ? Number(args[intervalArg + 1]) : 0;
  const lockPath = process.env.OPERATOR_SHEETS_LOCK || "/tmp/operator-sheets-drain.lock";
  const lease = await acquireLease(lockPath);
  if (lease === null) {
    console.log("another drain holds the writer lease; exiting");
    return;
  }
  try {
    for (;;) {
      const totals = await drainOnce({});
      console.log(JSON.stringify({ done: true, ...totals }));
      if (!intervalSec) break;
      await new Promise((resolve) => setTimeout(resolve, intervalSec * 1000));
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
