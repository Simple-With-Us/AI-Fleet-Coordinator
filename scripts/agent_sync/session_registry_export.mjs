// The live Google drain for the Operator's session registry:
// the registry store (read by session_registry.py, read-only) -> the "Sessions" tab of
// the Operator Thread Registry workbook (the same workbook whose "Zulip Threads" tab
// thread_registry_export.mjs drains).
//
// This file is the ONLY Google writer for the session registry, mirroring
// thread_registry_export.mjs's discipline:  validate the header before any write, upsert
// by the first column (Record ID), stop at the first failed write with the rows so far
// left written, and let the next drain reconcile by key.  The Python side
// (session_registry.py) owns the semantics — projection, extraction, the upsert plan —
// and `python3 -m agent_sync.session_registry export-rows` emits the desired rows this
// drain consumes, so the plan tested in Python is the plan executed here.
//
// Auth:  the existing fleet Sheets service account, reused for the Operator registries
// (owner ruling, 2026-11-03: no dedicated SA).  Its key JSON comes
// from OPERATOR_GOOGLE_SHEETS_SA in the environment, else Infisical Fleet-OPS prod
// /operator-thread-registry (key OPERATOR_GOOGLE_SHEETS_SA) through fleet-secrets — the
// same credential seam as the thread registry's drain;  one seam serves both tabs of
// the workbook.  The workbook must be shared (Editor) with that service account's
// client_email or every call 403s and the drain writes nothing.  Credentials are never
// written to disk or printed.  No silent fallback to any other identity.
//
// Usage:
//   python3 -m agent_sync.session_registry export-rows --out /path/session-rows.json
//   node session_registry_export.mjs /path/session-rows.json          # drain once
//   node session_registry_export.mjs /path/session-rows.json --dry-run  # read + plan only

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import crypto from "node:crypto";

export const SPREADSHEET_ID = "1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE";
export const SHEET_TAB = "Sessions";
export const SESSION_HEADERS = [
  "Record ID", "Letter", "Generation", "Seat", "Platform", "Verified Host",
  "Profile Scope", "Native Session ID", "Title", "Thread Link", "Status",
  "Wake Capability", "Registered At (UTC)", "Last Seen At (UTC)", "Closed At (UTC)",
  "Event Count", "Linear Issues", "Registry Updated At (UTC)",
];
const COL_KEY = 0;
const COL_UPDATED = 17;
const LAST_COL = "R";
const PAGE_SIZE = 500;

function base64url(input) {
  return Buffer.from(input).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function loadServiceAccount() {
  let raw = process.env.OPERATOR_GOOGLE_SHEETS_SA || "";
  if (!raw) {
    const bin = process.env.FLEET_SECRETS || join(homedir(), ".local", "bin", "fleet-secrets");
    try {
      raw = execFileSync(bin, ["get", "OPERATOR_GOOGLE_SHEETS_SA", "--path", "/operator-thread-registry"], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).replace(/\n$/, "");
    } catch {
      raw = "";
    }
  }
  if (!raw) {
    throw new Error(
      "Operator session registry service account missing: set OPERATOR_GOOGLE_SHEETS_SA or add it to Infisical Fleet-OPS prod /operator-thread-registry"
    );
  }
  return JSON.parse(raw);
}

async function getGoogleAccessToken() {
  const sa = loadServiceAccount();
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
  const data = await res.json();
  if (!res.ok) throw new Error(`Google OAuth error: ${JSON.stringify(data)}`);
  return data.access_token;
}

// The Sheets adapter with the surface session_registry.py documents:  validateHeaders(),
// readRows() -> [{rowNumber, cells}], appendRow(cells), updateRow(rowNumber, cells).
export function makeSheetsAdapter(token, spreadsheetId = SPREADSHEET_ID, tab = SHEET_TAB) {
  const auth = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const base = `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}`;
  async function call(path, init = {}) {
    const res = await fetch(`${base}${path}`, { ...init, headers: { ...auth, ...(init.headers || {}) } });
    if (!res.ok) throw new Error(`Sheets API ${init.method || "GET"} ${path} -> ${res.status}: ${JSON.stringify(await res.json())}`);
    return res.json();
  }
  return {
    async validateHeaders() {
      const data = await call(`/values/${encodeURIComponent(tab + "!A1:" + LAST_COL + "1")}`);
      return (data.values && data.values[0]) || [];
    },
    async readRows() {
      const rows = [];
      let startRow = 2;
      for (;;) {
        const data = await call(`/values/${encodeURIComponent(`${tab}!A${startRow}:${LAST_COL}${startRow + PAGE_SIZE - 1}`)}`);
        const values = data.values || [];
        values.forEach((cells, i) => rows.push({ rowNumber: startRow + i, cells }));
        if (values.length < PAGE_SIZE) return rows;
        startRow += PAGE_SIZE;
      }
    },
    async appendRow(cells) {
      const data = await call(
        `/values/${encodeURIComponent(tab + "!A:" + LAST_COL)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`,
        { method: "POST", body: JSON.stringify({ values: [cells] }) }
      );
      const match = /!A(\d+):/.exec(data.updates?.updatedRange || "");
      return match ? Number(match[1]) : null;
    },
    async updateRow(rowNumber, cells) {
      await call(`/values/${encodeURIComponent(`${tab}!A${rowNumber}:${LAST_COL}${rowNumber}`)}?valueInputOption=RAW`, {
        method: "PUT",
        body: JSON.stringify({ values: [cells] }),
      });
    },
  };
}

function cellsMatch(existing, desired) {
  const padded = existing.concat(Array(Math.max(0, desired.length - existing.length)).fill(""));
  for (let i = 0; i < desired.length; i++) {
    if (i === COL_UPDATED) continue;
    if (padded[i] !== desired[i]) return false;
  }
  return true;
}

// The upsert plan, kept in lockstep with session_registry.py's plan_upserts:  first row
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
    else updates.push([current.rowNumber, cells]);
  }
  return { updates, appends, unchanged, duplicateRows };
}

export async function drainSessionRows({ adapter, desired, dryRun = false, log = () => {} }) {
  const headers = await adapter.validateHeaders();
  if (JSON.stringify(headers) !== JSON.stringify(SESSION_HEADERS)) {
    throw new Error(`sheet header mismatch: expected ${JSON.stringify(SESSION_HEADERS)}, found ${JSON.stringify(headers)}`);
  }
  const plan = planUpserts(await adapter.readRows(), desired);
  const result = { updated: 0, appended: 0, unchanged: plan.unchanged, duplicateRows: plan.duplicateRows, failed: 0, partial: false };
  if (dryRun) return result;
  try {
    for (const [rowNumber, cells] of plan.updates) {
      await adapter.updateRow(rowNumber, cells);
      result.updated += 1;
    }
    for (const cells of plan.appends) {
      await adapter.appendRow(cells);
      result.appended += 1;
    }
  } catch (err) {
    result.failed = 1;
    result.partial = true;
    result.error = err.message;
    log(`session registry drain stopped: ${err.message}`);
  }
  return result;
}

async function main() {
  const args = process.argv.slice(2);
  const dryRun = args.includes("--dry-run");
  const file = args.find((a) => !a.startsWith("--"));
  if (!file) {
    console.error("usage: node session_registry_export.mjs <session-rows.json> [--dry-run]");
    process.exit(2);
  }
  const payload = JSON.parse(readFileSync(file, "utf8"));
  if (payload.spreadsheetId !== SPREADSHEET_ID || payload.tab !== SHEET_TAB) {
    throw new Error("session-rows payload names a different workbook or tab; refusing to drain");
  }
  const token = await getGoogleAccessToken();
  const adapter = makeSheetsAdapter(token);
  const result = await drainSessionRows({ adapter, desired: payload.rows, dryRun, log: (m) => console.error(m) });
  console.log(JSON.stringify(result));
  process.exit(result.failed ? 1 : 0);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(err.message);
    process.exit(1);
  });
}
