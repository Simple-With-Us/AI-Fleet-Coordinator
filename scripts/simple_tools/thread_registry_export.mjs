// The live Google drain for the Operator's chat-thread registry:
// thread-registry.json (written by thread_registry.py as the daemon routes messages)
// -> the "Operator Thread Registry" sheet's "Zulip Threads" tab.
//
// This file is the ONLY Google writer for the thread registry, mirroring
// operator_sheets_export.mjs's discipline:  validate the header before any write, upsert
// by the first column (Thread Key), stop at the first failed write with the rows so far
// left written, and let the next drain reconcile by key.  The Python side
// (thread_registry.py) owns the semantics — aggregation, projection, the upsert plan —
// and `python3 -m simple_tools.thread_registry export-rows` emits the desired rows this
// drain consumes, so the plan tested in Python is the plan executed here.
//
// Scheduled on the server (AFC-823):  the Operator Sheets drain runner
// (operator_sheets_drain.mjs, operator-sheets-drain.service and its timer) imports this
// module and runs drainThreadRows on every run, before the Sessions and Events export,
// with the rows from `docker exec ... thread_registry export-rows`, its own write pacing
// and the service-account key file the installer writes.  The CLI below stays for
// manual runs:  it only reads and plans unless given --write, and a write takes the
// drain's writer lease first, so a manual write never races a scheduled one.
//
// The tab is human-annotated, so the writes are built to survive a human editing it
// while a run is in flight:  the read walks the tab's whole grid (a blank row never ends
// it early), an update re-reads the Thread Key at its row number just before it writes
// and skips a row that moved, and an update never writes the Thread Key or the Purpose
// column (a human's Purpose typed after the read survives).  A row with a cell over
// Google's 50,000-character limit is skipped and counted rather than refused by Google
// on every run.
//
// Auth:  the existing fleet Sheets service account, reused for the Operator registries
// (owner ruling, 2026-11-03: no dedicated SA).  The CLI reads its key JSON
// from GOOGLE_SHEETS_SA in the environment, else Infisical Fleet-OPS prod
// /google-sheets-sa (key GOOGLE_SHEETS_SA) through the shared isolated reader and
// an explicit own OPERATOR_SHEETS_TOKEN_FILE bootstrap.  The drain
// runner reads the same key from /etc/operator-sheets/sa.json, which its installer
// fetches from that Infisical path.  The sheet must be shared (Editor) with that service
// account's client_email or every call 403s and the drain writes nothing.  This module
// never writes a credential to disk or prints one.
//
// Usage:
//   python3 -m simple_tools.thread_registry export-rows --out /path/thread-rows.json
//   node thread_registry_export.mjs /path/thread-rows.json          # read + plan only
//   node thread_registry_export.mjs /path/thread-rows.json --write  # drain once
// --write takes the Operator Sheets drain's writer lease (OPERATOR_SHEETS_LOCK, default
// /tmp/operator-sheets-drain.lock) and refuses while a drain run holds it.

import { readFileSync } from "node:fs";
import { loadRegistryServiceAccount } from "./registry_export_reader.mjs";
import crypto from "node:crypto";
import { scanCellText } from "./operator_sheets_google.mjs";

export const SPREADSHEET_ID = "1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE";
export const SHEET_TAB = "Zulip Threads";
export const THREAD_HEADERS = [
  "Thread Key", "Stream", "Stream ID", "Topic", "Status", "Participants",
  "Participant Count", "Message Count", "First Seen (UTC)", "Last Activity (UTC)",
  "Last Message ID", "Linear Issues", "PR References", "Purpose",
  "Registry Updated At (UTC)",
];
const COL_KEY = 0;
const COL_PURPOSE = 13;
const COL_UPDATED = 14;
const PAGE_SIZE = 500;
// Google refuses (HTTP 400) any write with a cell longer than this.
export const MAX_CELL_CHARS = 50_000;
// Updates re-read their rows' Thread Keys this many at a time, just before writing them.
export const KEY_CHECK_CHUNK = 10;
export const DEFAULT_LOCK = "/tmp/operator-sheets-drain.lock";

// Error diagnostics carry the Google error body, never its secrets:  credential-shaped
// content (bearer/access tokens, JWTs, API keys, private-key blocks, JSON credential
// fields) is redacted before the text is bound to the thrown error, and the whole detail
// is capped so a pathological body cannot flood logs.
const ERROR_DETAIL_CAP = 500;
const REDACT_PATTERNS = [
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "[redacted]"],
  [/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, "[redacted]"],
  [/ya29\.[A-Za-z0-9._-]+/g, "[redacted]"],
  [/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "[redacted]"],
  [/AIza[0-9A-Za-z_-]{20,}/g, "[redacted]"],
  [/("(?:[A-Za-z_]*(?:key|token|secret|assertion|password))[A-Za-z_]*"\s*:\s*")[^"]*(")/gi, "$1[redacted]$2"],
];
export function redactForError(text) {
  let out = String(text ?? "");
  for (const [re, replacement] of REDACT_PATTERNS) out = out.replace(re, replacement);
  return out.length > ERROR_DETAIL_CAP ? `${out.slice(0, ERROR_DETAIL_CAP)}...` : out;
}

function base64url(input) {
  return Buffer.from(input).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function getGoogleAccessToken() {
  const sa = await loadRegistryServiceAccount();
  const now = Math.floor(Date.now() / 1000);
  const signingInput = `${base64url(JSON.stringify({ alg: "RS256", typ: "JWT" }))}.${base64url(
    JSON.stringify({
      iss: sa.client_email,
      scope: "https://www.googleapis.com/auth/spreadsheets",
      aud: "https://oauth2.googleapis.com/token",
      iat: now,
      exp: now + 3600,
    })
  )}`;
  const signer = crypto.createSign("RSA-SHA256");
  signer.update(signingInput);
  signer.end();
  const jwt = `${signingInput}.${signer.sign(sa.private_key, "base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`,
  });
  // Read the body once, as text, so a non-JSON error page still reports its
  // HTTP status instead of dying in res.json() with the status lost.
  const text = await res.text();
  if (!res.ok) throw new Error(`Google OAuth error: HTTP ${res.status}: ${redactForError(text)}`);
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(`Google OAuth error: HTTP ${res.status}: token response was not JSON`);
  }
  // Shape-check before use, the way operator_sheets_google.mjs validates its
  // token response:  an unexpected body must fail here, not downstream as an
  // auth header.  (This drain runs under bare node with no zod available.)
  if (
    !data || typeof data !== "object" ||
    typeof data.access_token !== "string" || !data.access_token ||
    /[\r\n]/.test(data.access_token)
  ) {
    throw new Error("Google OAuth error: token response shape invalid");
  }
  return data.access_token;
}

// Statuses Google answers before applying a request:  a write refused with one of these
// is certain (nothing was written), the same list as operator_sheets_google.mjs.
const REJECTED_STATUSES = [400, 401, 403, 404, 409, 412, 413, 422, 429];
export const SHEETS_CALL_TIMEOUT_MS = 30_000;
// A hard bound on one read of the tab (200 pages of 500 rows).  Past it the read fails
// rather than plan from a partial index, which would append duplicates.
export const MAX_READ_ROWS = 100_000;

// Errors carry the fields the drain runner's pacing reads (operator_sheets_drain.mjs
// pacedAdapter):  code, status, uncertain and a numeric retryAfterSeconds.  The message
// names the method, path and status, and after an HTTP failure the response body's
// detail in redactForError's redacted, capped form only, never a credential.  The drain
// runner logs the fixed code and status, never the message.
function sheetsError(message, {
  code, status = null, uncertain = false, retryAfterSeconds = null,
}) {
  return Object.assign(new Error(message), { code, status, uncertain, retryAfterSeconds });
}

function retryAfter(res) {
  const value = res.headers?.get?.("retry-after") ?? "";
  return /^\d+$/.test(value) ? Math.min(Number(value), 3600) : null;
}

// At most this many bytes of a non-success body are held.  It is far above the
// 500-character detail cap, but a credential that starts inside the visible detail can
// still run past the cut, and a cut credential no longer matches its redaction pattern
// (a JSON field without its closing quote, a key block without its END line, a JWT
// without its third part).  So a body cut at this bound first has any such unterminated
// credential at its end redacted (CUT_REDACT_PATTERNS), and then the usual patterns run.
export const ERROR_BODY_READ_BYTES = 64 * 1024;
const CUT_REDACT_PATTERNS = [
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*$/, "[redacted]"],
  [/("(?:[A-Za-z_]*(?:key|token|secret|assertion|password))[A-Za-z_]*"\s*:\s*")[^"]*$/i,
    "$1[redacted]\""],
  [/eyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*)?$/, "[redacted]"],
];

// A non-success body for an error message:  read up to ERROR_BODY_READ_BYTES, the rest
// cancelled unread, then redacted and capped.  It runs under the caller's timer (the
// body stream shares the call's abort signal).  A response with no body stream (a test
// double) falls back to text(), still redacted and capped.  A body that cannot be read
// gives an empty detail, never a different error.
async function errorDetail(res) {
  try {
    const reader = res.body?.getReader?.();
    if (!reader) return redactForError(await res.text());
    const chunks = [];
    let size = 0;
    let ended = false;
    try {
      while (size < ERROR_BODY_READ_BYTES) {
        const { done, value } = await reader.read();
        if (done) {
          ended = true;
          break;
        }
        chunks.push(value);
        size += value.byteLength;
      }
    } finally {
      await reader.cancel().catch(() => {});
    }
    const cut = !ended || size > ERROR_BODY_READ_BYTES;
    let text = Buffer.concat(chunks).subarray(0, ERROR_BODY_READ_BYTES).toString("utf8");
    if (cut) {
      for (const [re, replacement] of CUT_REDACT_PATTERNS) text = text.replace(re, replacement);
    }
    return redactForError(text);
  } catch {
    return "";
  }
}

// The Sheets adapter with the surface thread_registry.py documents:  validateHeaders(),
// readRows() -> [{rowNumber, cells}], appendRow(cells), updateRow(rowNumber, cells),
// plus readKeys(rowNumbers) -> the Thread Key now at each of those rows.
// Every call is bounded by timeoutMs (the response body included) and never follows a
// redirect.  readRows pages to the tab's grid row count, never stopping at a short page:
// Google leaves trailing blank rows out of a page, so a blank row at a page boundary
// would otherwise hide every row below it and they would be appended again.  shouldStop
// is checked between read pages:  a stop ends the read with an error (code "stopped")
// instead of a partial index.  updateRow writes B:M and O, never the Thread Key and,
// unless writePurpose is set, never the human-owned Purpose (N).
export function makeSheetsAdapter(token, spreadsheetId = SPREADSHEET_ID, tab = SHEET_TAB, {
  fetchImpl = globalThis.fetch, timeoutMs = SHEETS_CALL_TIMEOUT_MS, shouldStop = () => false,
} = {}) {
  const auth = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const base = `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}`;
  async function call(path, init = {}) {
    const method = init.method || "GET";
    const write = method !== "GET";
    const where = `Sheets API ${method} ${path}`;
    // One timer for the whole call, the response body included, like the Sessions
    // transport's requestJSON.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      let res;
      try {
        res = await fetchImpl(`${base}${path}`, {
          ...init,
          headers: { ...auth, ...(init.headers || {}) },
          redirect: "error",
          signal: controller.signal,
        });
      } catch (error) {
        if (!write && error?.code === "stopped" && shouldStop()) {
          throw sheetsError("Sheets read stopped", { code: "stopped" });
        }
        throw sheetsError(`${where} -> no response`, {
          code: "transport_or_response_error", uncertain: write,
        });
      }
      if (!res.ok) {
        // A non-success body can echo request data and credentials:  it is read for
        // diagnosis bounded, under this call's timer, and only its redacted, capped
        // form reaches the message (errorDetail).
        const detail = await errorDetail(res);
        throw sheetsError(`${where} -> ${res.status}${detail ? `: ${detail}` : ""}`, {
          code: "http_error",
          status: res.status,
          uncertain: write && !REJECTED_STATUSES.includes(res.status),
          retryAfterSeconds: retryAfter(res),
        });
      }
      try {
        return await res.json();
      } catch {
        throw sheetsError(`${where} -> unreadable response`, {
          code: "transport_or_response_error", uncertain: write,
        });
      }
    } finally {
      clearTimeout(timer);
    }
  }
  // The tab's physical row count, the header row included, from the workbook metadata.
  async function gridRowCount() {
    const fields = encodeURIComponent("sheets(properties(title,gridProperties(rowCount)))");
    const data = await call(`?fields=${fields}`);
    const matches = (Array.isArray(data?.sheets) ? data.sheets : [])
      .filter((sheet) => sheet?.properties?.title === tab);
    const rowCount = matches.length === 1
      ? matches[0].properties.gridProperties?.rowCount : null;
    if (!Number.isSafeInteger(rowCount) || rowCount < 1) {
      throw sheetsError("Sheets metadata names no single tab with a row count", {
        code: "grid_unknown",
      });
    }
    return rowCount;
  }
  return {
    async validateHeaders() {
      const data = await call(`/values/${encodeURIComponent(quotedTab(tab) + "!A1:O1")}`);
      return (data.values && data.values[0]) || [];
    },
    async readRows() {
      const rowCount = await gridRowCount();
      if (rowCount - 1 > MAX_READ_ROWS) {
        throw sheetsError(`Sheets read passes ${MAX_READ_ROWS} rows`, { code: "read_limit" });
      }
      const rows = [];
      for (let startRow = 2; startRow <= rowCount; startRow += PAGE_SIZE) {
        if (startRow > 2 && shouldStop()) {
          throw sheetsError("Sheets read stopped before its last page", { code: "stopped" });
        }
        const endRow = Math.min(startRow + PAGE_SIZE - 1, rowCount);
        const data = await call(`/values/${encodeURIComponent(`${quotedTab(tab)}!A${startRow}:O${endRow}`)}`);
        const values = data.values || [];
        if (!Array.isArray(values) || values.length > endRow - startRow + 1 ||
            values.some((cells) => !Array.isArray(cells))) {
          throw sheetsError("Sheets read returned an invalid page", {
            code: "invalid_rows_response",
          });
        }
        // A blank row inside a page comes back as []; trailing blank rows are left out.
        values.forEach((cells, i) => rows.push({ rowNumber: startRow + i, cells }));
      }
      return rows;
    },
    async readKeys(rowNumbers) {
      if (!rowNumbers.length) return [];
      const ranges = rowNumbers
        .map((r) => `ranges=${encodeURIComponent(`${quotedTab(tab)}!A${r}`)}`).join("&");
      const data = await call(`/values:batchGet?${ranges}`);
      const found = data?.valueRanges;
      if (!Array.isArray(found) || found.length !== rowNumbers.length) {
        throw sheetsError("Sheets key read returned an invalid answer", {
          code: "invalid_keys_response",
        });
      }
      return found.map((range, i) => {
        // Answered in the asked order; the range names the row it covers.
        const match = /!A(\d+)(?::A(\d+))?$/.exec(String(range?.range ?? ""));
        if (!match || Number(match[1]) !== rowNumbers[i]) {
          throw sheetsError("Sheets key read answered another row", {
            code: "invalid_keys_response",
          });
        }
        return String(range.values?.[0]?.[0] ?? "");
      });
    },
    async appendRow(cells) {
      const data = await call(
        `/values/${encodeURIComponent(quotedTab(tab) + "!A:O")}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`,
        { method: "POST", body: JSON.stringify({ values: [cells] }) }
      );
      const match = /!A(\d+):/.exec(data.updates?.updatedRange || "");
      return match ? Number(match[1]) : null;
    },
    async updateRow(rowNumber, cells, { writePurpose = false } = {}) {
      // The Thread Key is already the row's (that is how the row was chosen), so A is
      // never written: a row that moved under the write then keeps its own key and the
      // next run repairs it.  Purpose (N) is written only when the caller asks.
      const r = rowNumber;
      const q = quotedTab(tab);
      const data = writePurpose
        ? [{ range: `${q}!B${r}:O${r}`, values: [cells.slice(1, 15)] }]
        : [
          { range: `${q}!B${r}:M${r}`, values: [cells.slice(1, COL_PURPOSE)] },
          { range: `${q}!O${r}:O${r}`, values: [[cells[COL_UPDATED]]] },
        ];
      await call("/values:batchUpdate", {
        method: "POST",
        body: JSON.stringify({ valueInputOption: "RAW", data }),
      });
    },
  };
}

// A row Google would refuse:  any cell longer than MAX_CELL_CHARS.
export function rowFitsSheet(cells) {
  return cells.every((cell) => String(cell).length <= MAX_CELL_CHARS);
}

function cellsMatch(existing, desired) {
  const padded = existing.concat(Array(Math.max(0, desired.length - existing.length)).fill(""));
  for (let i = 0; i < desired.length; i++) {
    if (i === COL_UPDATED) continue;
    if (i === COL_PURPOSE && padded[i] && !desired[i]) continue; // a human's Purpose is never erased
    if (padded[i] !== desired[i]) return false;
  }
  return true;
}

function mergedCells(existing, desired) {
  const cells = desired.slice();
  if (existing[COL_PURPOSE] && !cells[COL_PURPOSE]) cells[COL_PURPOSE] = existing[COL_PURPOSE];
  return cells;
}

// The upsert plan, kept in lockstep with thread_registry.py's plan_upserts:  first row
// with a key is the row of record, later duplicates are reported and never touched.
export function planUpserts(existing, desired) {
  const byKey = new Map();
  const duplicateRows = [];
  for (const row of existing) {
    const cells = (row.cells || []).map(String);
    if (!cells.length || !cells[COL_KEY]) continue;
    if (byKey.has(cells[COL_KEY])) duplicateRows.push(row.rowNumber);
    else byKey.set(cells[COL_KEY], row);
  }
  const updates = [];
  const appends = [];
  let unchanged = 0;
  for (const cells of desired) {
    const current = byKey.get(cells[COL_KEY]);
    if (!current) {
      appends.push(cells);
      continue;
    }
    const currentCells = (current.cells || []).map(String);
    if (cellsMatch(currentCells, cells)) unchanged += 1;
    else updates.push([current.rowNumber, mergedCells(currentCells, cells)]);
  }
  return { updates, appends, unchanged, duplicateRows };
}

// shouldStop is checked before every write:  when it answers true the drain ends between
// writes with stopped set, the rows so far written, and the rest left for the next run,
// which plans them again by Thread Key.  remaining counts the planned writes not
// confirmed (a failed write's row and a moved row included).  A failed write records its
// code and status beside the message, so a caller can report it without the message.
//
// Rows with a cell over MAX_CELL_CHARS are left out of the plan and counted (oversized).
// Before each run of KEY_CHECK_CHUNK updates the adapter's readKeys re-reads the Thread
// Key at those row numbers, and an update whose row no longer holds its key (a human
// sorted, inserted or deleted rows since the read) is skipped and counted (moved);  the
// next run re-reads the tab and plans it again.  An adapter without readKeys (the
// in-memory test sheets) skips that check.  An update writes Purpose only when the
// desired row carries one, which the projection never does.
// A tab title quoted for A1 notation:  the Sheets API requires single quotes around
// titles with spaces or punctuation, and a literal quote doubles.
export function quotedTab(tab) {
  return `'${String(tab).replace(/'/g, "''")}'`;
}

export async function drainThreadRows({
  adapter, desired, dryRun = false, log = () => {}, shouldStop = () => false,
}) {
  const headers = await adapter.validateHeaders();
  if (JSON.stringify(headers) !== JSON.stringify(THREAD_HEADERS)) {
    const err = new Error(
      `sheet header mismatch: expected ${JSON.stringify(THREAD_HEADERS)}, ` +
      `found ${JSON.stringify(headers)}`,
    );
    err.code = "header_mismatch";
    throw err;
  }
  const fitting = desired.filter(rowFitsSheet);
  const plan = planUpserts(await adapter.readRows(), fitting);
  const result = {
    updated: 0, appended: 0, unchanged: plan.unchanged, duplicateRows: plan.duplicateRows,
    oversized: desired.length - fitting.length, moved: 0, failed: 0, partial: false,
  };
  const planned = plan.updates.length + plan.appends.length;
  const finish = () => {
    result.remaining = planned - result.updated - result.appended;
    return result;
  };
  if (dryRun) return finish();
  const purposeByKey = new Map(fitting.map((cells) => [cells[COL_KEY], cells[COL_PURPOSE]]));
  const checkKeys = typeof adapter.readKeys === "function";
  try {
    let keys = null;
    for (let i = 0; i < plan.updates.length; i++) {
      const [rowNumber, cells] = plan.updates[i];
      if (shouldStop()) {
        result.stopped = true;
        return finish();
      }
      if (checkKeys && i % KEY_CHECK_CHUNK === 0) {
        const chunk = plan.updates.slice(i, i + KEY_CHECK_CHUNK);
        keys = await adapter.readKeys(chunk.map(([r]) => r));
      }
      if (checkKeys && keys[i % KEY_CHECK_CHUNK] !== cells[COL_KEY]) {
        result.moved += 1;
        continue;
      }
      await adapter.updateRow(rowNumber, cells, {
        writePurpose: Boolean(purposeByKey.get(cells[COL_KEY])),
      });
      result.updated += 1;
    }
    for (const cells of plan.appends) {
      if (shouldStop()) {
        result.stopped = true;
        return finish();
      }
      await adapter.appendRow(cells);
      result.appended += 1;
    }
  } catch (err) {
    if (err?.code === "stopped" && shouldStop()) {
      result.stopped = true;
      return finish();
    }
    result.failed = 1;
    result.partial = true;
    result.code = typeof err?.code === "string" ? err.code : "write_failed";
    result.status = Number.isSafeInteger(err?.status) ? err.status : null;
    result.error = result.code;
    log(`thread registry drain stopped: code=${result.code} status=${result.status ?? "unknown"}`);
  }
  return finish();
}

// The payload contract thread_registry.py's export-rows writes:  an object
// whose rows are arrays of THREAD_HEADERS.length string cells, key first.
// A malformed file aborts here with the exact row, before any Google call,
// instead of producing partial or corrupt sheet writes mid-drain.
export function validatePayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("thread-rows payload is not an object; refusing to drain");
  }
  if (!Array.isArray(payload.rows)) {
    throw new Error("thread-rows payload rows is not an array; refusing to drain");
  }
  payload.rows.forEach((row, i) => {
    if (!Array.isArray(row) || row.length !== THREAD_HEADERS.length) {
      throw new Error(`thread-rows row ${i} is not ${THREAD_HEADERS.length} cells; refusing to drain`);
    }
    if (row.some((cell) => typeof cell !== "string")) {
      throw new Error(`thread-rows row ${i} holds a non-string cell; refusing to drain`);
    }
    if (!row[COL_KEY]) {
      throw new Error(`thread-rows row ${i} has an empty Thread Key; refusing to drain`);
    }
  });
}

export const USAGE =
  "usage: node thread_registry_export.mjs <thread-rows.json> [--write]\n" +
  "  Without --write it reads the tab and prints the plan, and writes nothing.\n" +
  "  --write takes the Operator Sheets drain's writer lease (OPERATOR_SHEETS_LOCK, default " +
  `${DEFAULT_LOCK}) and refuses while a drain run holds it;  on the server, ` +
  "stop operator-sheets-drain.timer and let a running drain finish first.";

// The manual CLI, with its effects injectable for tests.  Answers the exit code:  0 done,
// 1 a failed write, 2 usage, 3 the drain holds the writer lease.  Writing needs --write
// (--dry-run wins over it), and a write holds the drain's lease for its whole run.
export async function runCli(argv, {
  env = process.env,
  readText = (file) => readFileSync(file, "utf8"),
  getToken = getGoogleAccessToken,
  makeAdapter = (token) => makeSheetsAdapter(token),
  takeLease = async (lockPath) => {
    const { acquireLease } = await import("./operator_sheets_drain.mjs");
    return acquireLease(lockPath);
  },
  out = (m) => console.log(m),
  err = (m) => console.error(m),
} = {}) {
  const write = argv.includes("--write") && !argv.includes("--dry-run");
  const file = argv.find((a) => !a.startsWith("--"));
  if (!file) {
    err(USAGE);
    return 2;
  }
  const payload = JSON.parse(readText(file));
  validatePayload(payload);
  if (payload.spreadsheetId !== SPREADSHEET_ID || payload.tab !== SHEET_TAB) {
    throw new Error("thread-rows payload names a different workbook or tab; refusing to drain");
  }
  const lockPath = env.OPERATOR_SHEETS_LOCK || DEFAULT_LOCK;
  const lease = write ? await takeLease(lockPath) : null;
  if (write && lease === null) {
    err("the Operator Sheets drain holds the writer lease; nothing written.  On the server, " +
      "stop operator-sheets-drain.timer, let the running drain finish, then retry.");
    return 3;
  }
  try {
    const token = await getToken();
    const adapter = makeAdapter(token);
    // Credential-shaped cells never reach the sheet on the manual path either:  drop them
    // here the way the scheduled drain does, and count what was refused.
    const clean = payload.rows.filter((row) => row.every((cell) => scanCellText(cell) === null));
    const result = await drainThreadRows({
      adapter, desired: clean, dryRun: !write, log: (m) => err(m),
    });
    result.refused = payload.rows.length - clean.length;
    out(JSON.stringify({ dryRun: !write, ...result }));
    return result.failed ? 1 : 0;
  } finally {
    if (lease) {
      await lease.close().catch(() => {});
    }
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  runCli(process.argv.slice(2)).then((code) => process.exit(code), (err) => {
    console.error(err.message);
    process.exit(1);
  });
}
