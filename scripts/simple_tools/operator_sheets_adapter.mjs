// operator_sheets_adapter: the AFC-428 Google Sheets adapter for the Operator outbox drain.
//
// The adapter writes ONLY the Sessions and Events tabs of the fleet reference workbook
// (spreadsheet 1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY;  override
// OPERATOR_SHEETS_SPREADSHEET_ID only to point a test drain at a scratch copy).  It never
// touches Reference.  Before any write, validateHeaders() verifies the workbook IS that
// workbook:  the Reference (2033000101), Sessions (2033000102) and Events (2033000103) tabs
// must exist with exactly those sheet ids - a pointed-at wrong workbook fails closed.
//
// CREDENTIAL CUSTODY.  The service-account JSON comes ONLY from Infisical Fleet-OPS
// /google-sheets-sa through fleet-secrets, with a scrubbed child environment (PATH alone):
// Infisical is the sole secret truth (no ambient GOOGLE_SHEETS_SA fallback, and the child
// never inherits this process's environment).  The JSON and the token are never printed,
// written to disk, or included in an error message.  This adapter is NOT activated anywhere
// yet:  it is source for review.
//
// WRITE CONFIRMATION.  Every write requires the API's own confirmation, strictly parsed:
// an append must return updates.updatedRange naming the written row in the target tab (plus
// updatedCells), an update must echo exactly the requested range and cell count.  A 2xx with
// malformed or empty JSON is a FAILURE, never a success and never an empty table:  reads
// require the echoed range to match the request before a missing `values` may mean empty.
//
// TOKEN LIFETIME.  The bearer token is cached only until 60s before its API-reported expiry;
// a 401 clears the cache and retries the request exactly once with a fresh token (a 401 is
// rejected before any write executes, so the retry cannot double-apply).  No other failure
// is retried:  a 5xx or a timeout leaves the write unconfirmed and the outbox row pending.
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import crypto from "node:crypto";
import { SESSIONS_HEADERS, EVENTS_HEADERS } from "./operator_sheets_export.mjs";

export const DEFAULT_SPREADSHEET_ID = "1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY";
const TABS = Object.freeze({ sessions: "Sessions", events: "Events" });
const HEADERS = Object.freeze({ sessions: SESSIONS_HEADERS, events: EVENTS_HEADERS });
// The live tab identity of the fleet reference workbook (Reference/Sessions/Events), as
// recorded when Codex added the tabs on 2026-10-20.  validateHeaders checks these ids so a
// mis-pointed adapter fails closed instead of writing some other workbook.
const EXPECTED_SHEET_IDS = Object.freeze({
  Reference: 2033000101,
  Sessions: 2033000102,
  Events: 2033000103,
});

// The scrubbed environment for the fleet-secrets child:  enough to exec, nothing ambient.
const CHILD_ENV = Object.freeze({ PATH: process.env.PATH || "/usr/local/bin:/usr/bin:/bin" });

function base64url(input) {
  return Buffer.from(input).toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function columnLetter(count) {
  let n = count;
  let out = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

// Infisical custody only:  fleet-secrets get, scrubbed environment, no env fallback.
function loadServiceAccount(env, execImpl) {
  const bin = env.FLEET_SECRETS || join(homedir(), ".local", "bin", "fleet-secrets");
  let raw = "";
  try {
    raw = execImpl(bin, ["get", "GOOGLE_SHEETS_SA", "--path", "/google-sheets-sa"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      env: { ...CHILD_ENV },
    }).replace(/\n$/, "");
  } catch {
    raw = "";
  }
  if (!raw) {
    throw new Error("Google service account missing in Infisical /google-sheets-sa "
      + "(fleet-secrets get GOOGLE_SHEETS_SA --path /google-sheets-sa)");
  }
  return JSON.parse(raw);
}

export async function getGoogleAccessToken(sa, fetchImpl) {
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "RS256", typ: "JWT" };
  const payload = {
    iss: sa.client_email,
    scope: "https://www.googleapis.com/auth/spreadsheets",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600,
  };
  const signingInput = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(payload))}`;
  const signer = crypto.createSign("RSA-SHA256");
  signer.update(signingInput);
  signer.end();
  const signature = signer.sign(sa.private_key, "base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const res = await fetchImpl("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${signingInput}.${signature}`,
  });
  const data = await res.json().catch(() => null);
  const token = data && typeof data.access_token === "string" ? data.access_token.trim() : "";
  // Trimmed once at extraction:  the validated value is the value the Bearer header gets.
  if (!res.ok || !token)
    throw new Error(`Google OAuth error ${res.status}`);
  // expires_in is seconds and must be a finite positive TTL:  an unknown TTL is a
  // failure, never an assumed 3600 that could over-cache a shorter-lived token, and
  // the cache never exceeds an hour.  Refresh 60s early so --loop never rides a dying token.
  if (!Number.isFinite(data?.expires_in) || data.expires_in <= 0)
    throw new Error("Google OAuth error: malformed expires_in");
  const expiresIn = Math.min(data.expires_in, 3600);
  return { token, expiresAt: now + expiresIn - 60 };
}

export function createAdapter(deps = {}) {
  const env = deps.env ?? process.env;
  const execImpl = deps.execFileSyncImpl ?? execFileSync;
  const fetchImpl = deps.fetchImpl ?? fetch;
  const nowImpl = deps.nowImpl ?? (() => Math.floor(Date.now() / 1000));
  const spreadsheetId = deps.spreadsheetId
    ?? env.OPERATOR_SHEETS_SPREADSHEET_ID
    ?? DEFAULT_SPREADSHEET_ID;
  // tokenProvider is injectable for tests;  the real one follows Infisical custody above.
  // It returns { token, expiresAt } (a bare string is treated as never-expiring, tests only).
  const tokenProvider = deps.tokenProvider
    ?? (async () => getGoogleAccessToken(loadServiceAccount(env, execImpl), fetchImpl));
  let cached = null;
  const auth = async () => {
    if (!cached || cached.expiresAt <= nowImpl()) {
      const fresh = await tokenProvider();
      cached = typeof fresh === "string"
        ? { token: fresh, expiresAt: Number.MAX_SAFE_INTEGER }
        : fresh;
      if (typeof cached?.token !== "string" || !cached.token)
        throw new Error("token provider returned no access token");
    }
    return { Authorization: `Bearer ${cached.token}` };
  };

  const api = async (method, pathSpec, body) => {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const res = await fetchImpl(
        `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}${pathSpec}`, {
          method,
          headers: { ...(await auth()), "Content-Type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
      if (res.status === 401 && attempt === 0) {
        cached = null; // expired early or revoked:  refresh once and retry (see header)
        continue;
      }
      let data;
      try {
        data = await res.json();
      } catch {
        // A 2xx we cannot parse is NOT a success and NOT an empty result.
        throw new Error(`sheets ${method} ${pathSpec}: malformed JSON (status ${res.status})`);
      }
      if (!res.ok) {
        // Surface the API's message, never the request headers (which carry the token).
        const message = data?.error?.message ?? `status ${res.status}`;
        throw new Error(`sheets ${method} ${pathSpec}: ${message}`);
      }
      if (data === null || typeof data !== "object" || Array.isArray(data))
        throw new Error(`sheets ${method} ${pathSpec}: unexpected response shape`);
      return data;
    }
    throw new Error("unreachable");
  };

  const tabFor = (table) => {
    const tab = TABS[table];
    if (!tab) throw new Error(`unknown table ${table}: the adapter serves sessions and events`);
    return tab;
  };

  // The range the API echoes must name the tab we asked (quoted or bare).
  const requireEchoedRange = (data, tab, what) => {
    const range = typeof data?.range === "string" ? data.range : "";
    const bare = range.replace(/^'([^']*)'!/, "$1!");
    if (!bare.startsWith(`${tab}!`))
      throw new Error(`${what}: response range ${JSON.stringify(data?.range)} is not ${tab}`);
    return bare;
  };

  return {
    spreadsheetId,
    async inspect() {
      const meta = await api("GET", "");
      if (typeof meta?.properties?.title !== "string" || !Array.isArray(meta?.sheets))
        throw new Error("inspect: malformed spreadsheet metadata");
      return {
        title: meta.properties.title,
        tabs: meta.sheets.map((s) => ({
          title: s.properties?.title,
          sheetId: s.properties?.sheetId,
        })),
      };
    },

    async validateHeaders() {
      // Workbook identity first:  the three tabs with their recorded sheet ids.
      const info = await this.inspect();
      for (const [tab, id] of Object.entries(EXPECTED_SHEET_IDS)) {
        const found = info.tabs.find((t) => t.title === tab);
        if (!found || found.sheetId !== id)
          throw new Error(`workbook is not the fleet reference: tab ${tab} `
            + `sheetId ${found?.sheetId}, expected ${id}`);
      }
      for (const [table, expected] of Object.entries(HEADERS)) {
        const tab = TABS[table];
        const range = `${encodeURIComponent(tab)}!A1:${columnLetter(expected.length)}1`;
        const data = await api("GET", `/values/${range}`);
        requireEchoedRange(data, tab, "validateHeaders");
        if (!Array.isArray(data.values))
          throw new Error(`${tab} header read returned no values row`);
        const actual = (data.values[0] ?? []).map((c) => String(c ?? ""));
        if (JSON.stringify(actual) !== JSON.stringify(expected)) {
          throw new Error(`${tab} header drift: sheet has ${JSON.stringify(actual)}, `
            + `exporter writes ${JSON.stringify(expected)}`);
        }
      }
    },

    // Pages carry 1-based sheet row numbers ({ rowNumber, cells }) so the Sessions upsert
    // can target updateSessionRow (adapter ask (a)).
    async readRows(table, { startRow = 2, limit = 200 } = {}) {
      const tab = tabFor(table);
      const width = columnLetter(HEADERS[table].length);
      const range = `${encodeURIComponent(tab)}!A${startRow}:${width}${startRow + limit - 1}`;
      const data = await api("GET", `/values/${range}`);
      requireEchoedRange(data, tab, "readRows");
      // The API omits `values` for an empty range;  only the echoed range makes that
      // a proven-empty page rather than a malformed read.
      const values = data.values ?? [];
      if (!Array.isArray(values) || values.some((row) => !Array.isArray(row)))
        throw new Error("readRows: values is not an array of rows");
      return {
        rows: values.map((cells, i) => ({
          rowNumber: startRow + i,
          cells: cells.map((c) => String(c ?? "")),
        })),
      };
    },

    async appendSession(cells) {
      return appendRow("sessions", cells);
    },
    async appendEvent(cells) {
      return appendRow("events", cells);
    },
    async updateSessionRow(rowNumber, cells) {
      if (!Number.isInteger(rowNumber) || rowNumber < 2)
        throw new Error(`updateSessionRow needs a data row number, got ${rowNumber}`);
      const width = columnLetter(SESSIONS_HEADERS.length);
      const wanted = `${TABS.sessions}!A${rowNumber}:${width}${rowNumber}`;
      const data = await api("PUT",
        `/values/${encodeURIComponent(TABS.sessions)}!A${rowNumber}:${width}${rowNumber}`
        + "?valueInputOption=RAW",
        { majorDimension: "ROWS", values: [cells] });
      const echoed = requireEchoedRange(data, TABS.sessions, "updateSessionRow");
      // The write is confirmed only when the API echoes exactly the requested range and
      // the full cell count;  anything less means the drain must NOT acknowledge.
      if (echoed !== wanted || data.updatedCells !== cells.length)
        throw new Error(`updateSessionRow: unconfirmed write (echoed ${echoed}, `
          + `updatedCells ${data.updatedCells})`);
      return rowNumber;
    },
  };

  async function appendRow(table, cells) {
    const tab = tabFor(table);
    const data = await api(
      "POST",
      `/values/${encodeURIComponent(tab)}!A1:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`,
      { majorDimension: "ROWS", values: [cells] },
    );
    const updated = data?.updates?.updatedRange;
    if (typeof updated !== "string")
      throw new Error(`append to ${tab}: no updatedRange - the write is unconfirmed`);
    const bare = updated.replace(/^'([^']*)'!/, "$1!");
    const match = new RegExp(`^${tab}!A([0-9]+):[A-Z]+[0-9]+$`).exec(bare);
    if (!match || !Number.isInteger(data.updates.updatedCells) || data.updates.updatedCells < 1)
      throw new Error(`append to ${tab}: unconfirmed write (updatedRange `
        + `${JSON.stringify(updated)})`);
    return Number(match[1]);
  }
}

export default { createAdapter };
