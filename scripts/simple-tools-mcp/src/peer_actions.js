// Peer-Actions Registry for hosted ST-MCP (AFC-745).
// Appends one row through the Google Sheets API using GOOGLE_SHEETS_SA from
// Infisical Fleet-OPS /google-sheets-sa (read with INFISICAL_SHEETS_SA_TOKEN only).  Pure module — fetch and WebCrypto only.

import PEER_ACTIONS_JSON from "../../simple_tools/mcp/peer-actions-tools.json" with { type: "json" };
import { ToolError, okResult, scanSecret } from "./contract.js";
import { canonicalSeatName } from "./identity-aliases.js";
import { InfisicalError, resolveSecretIsolated } from "./infisical.js";

export const PEER_ACTIONS_TOOLS = Object.freeze(PEER_ACTIONS_JSON.tools);
export const PEER_ACTIONS_WRITE_TOOLS = Object.freeze(new Set(["peer_actions_log"]));

const TAB = "Peer-Actions Registry";
const APPEND_RANGE = "'Peer-Actions Registry'!A:F";
const DEFAULT_SPREADSHEET_ID = "1ZlHD3kJOcOPnVdkMYbHt_vfLbHKzsI7O-MpsM7Cq_NY";
const TEXT_MAX = 4000;
const SEAT_RE = /^[A-Z0-9][A-Z0-9_-]{0,31}$/;
const MAX_ATTEMPTS = 5;

function cleanLine(name, value, max = TEXT_MAX) {
  if (typeof value !== "string") throw new ToolError("invalid_argument", `${name} must be a string`);
  const text = value.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!text) throw new ToolError("invalid_argument", `${name} is required`);
  if (text.length > max) throw new ToolError("invalid_argument", `${name} must be at most ${max} characters`);
  if (text.includes("\n")) throw new ToolError("invalid_argument", `${name} must be a single line`);
  if (scanSecret(text)) throw new ToolError("refused_secret", `${name} contains credential-shaped text`);
  return text;
}

function cleanSeat(name, value) {
  const text = canonicalSeatName(cleanLine(name, value, 32));
  if (!SEAT_RE.test(text)) throw new ToolError("invalid_argument", `${name} must be a fleet seat tag`);
  return text;
}

function centralTimestamp(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")} ${get("dayPeriod")} CT`;
}

function b64url(bytes) {
  const bin = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
  let out = "";
  const view = bin instanceof Uint8Array ? bin : new Uint8Array(bin);
  for (const b of view) out += String.fromCharCode(b);
  return btoa(out).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function pemToPkcs8(pem) {
  const body = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const raw = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
  return raw.buffer;
}

async function googleAccessToken(sa, fetchImpl) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const payload = b64url(
    JSON.stringify({
      iss: sa.client_email,
      scope: "https://www.googleapis.com/auth/spreadsheets",
      aud: "https://oauth2.googleapis.com/token",
      iat: now,
      exp: now + 3600,
    }),
  );
  const input = `${header}.${payload}`;
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToPkcs8(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(input));
  const assertion = `${input}.${b64url(new Uint8Array(sig))}`;
  const res = await fetchImpl("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=${encodeURIComponent(assertion)}`,
    redirect: "manual",
  });
  const data = await res.json();
  if (!res.ok || typeof data.access_token !== "string" || !data.access_token) {
    throw new ToolError("sheets_auth_failed", "Google OAuth refused the service account");
  }
  return data.access_token;
}

// GOOGLE_SHEETS_SA comes from Infisical only (owner rule, Nov 12 2026:  no Worker secret holds a real
// secret).  It is read from Fleet-OPS /google-sheets-sa through its own narrowly scoped service token,
// bound as SHEETS_SA_TOKEN_BINDING, never through the broad read-tools INFISICAL_SERVICE_TOKEN (not scoped
// to that folder:  Infisical answers 403) and never from a GOOGLE_SHEETS_SA Worker binding.
export const SHEETS_SA_TOKEN_BINDING = "INFISICAL_SHEETS_SA_TOKEN";
const SHEETS_SA_PATH = "/google-sheets-sa";

async function loadServiceAccount(env) {
  // Fallback and off modes answer "" for an unbound token, a scope refusal or a 404;  required mode
  // throws InfisicalError.  Name every such gap not_configured instead of "not valid JSON".
  let raw;
  try {
    raw = await resolveSecretIsolated(env, { name: "GOOGLE_SHEETS_SA", path: SHEETS_SA_PATH, tokenBinding: SHEETS_SA_TOKEN_BINDING });
  } catch (error) {
    if (!(error instanceof InfisicalError)) throw error;
    if (error.code === "not_found" || error.code === "not_configured" || error.status === 401 || error.status === 403) {
      throw new ToolError("not_configured", `GOOGLE_SHEETS_SA is unavailable: ${SHEETS_SA_TOKEN_BINDING} could not read Infisical ${SHEETS_SA_PATH} (${error.code})`);
    }
    throw new ToolError("unavailable", "could not read GOOGLE_SHEETS_SA from Infisical", { retryable: true });
  }
  if (!String(raw ?? "").trim()) {
    throw new ToolError(
      "not_configured",
      `GOOGLE_SHEETS_SA is unavailable: ${SHEETS_SA_TOKEN_BINDING} is not bound or cannot read Infisical ${SHEETS_SA_PATH}`,
    );
  }
  return parseServiceAccount(raw);
}

// Parse and narrow GOOGLE_SHEETS_SA:  null, arrays, and objects without a usable client_email and
// private_key answer invalid_secret here instead of failing later inside the JWT signer.
function parseServiceAccount(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ToolError("invalid_secret", "GOOGLE_SHEETS_SA is not valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new ToolError("invalid_secret", "GOOGLE_SHEETS_SA is not a JSON object");
  }
  for (const field of ["client_email", "private_key"]) {
    if (typeof parsed[field] !== "string" || !parsed[field].trim()) {
      throw new ToolError("invalid_secret", `GOOGLE_SHEETS_SA has no ${field}`);
    }
  }
  return parsed;
}

function retryAfterMs(headers, attempt) {
  const raw = headers.get("retry-after");
  if (raw && /^\d+$/.test(raw)) return Math.min(Number(raw), 120) * 1000;
  return Math.min(2 ** attempt, 32) * 1000;
}

async function appendRow(env, row, fetchImpl) {
  const spreadsheetId = String(env.PEER_ACTIONS_SPREADSHEET_ID ?? DEFAULT_SPREADSHEET_ID).trim();
  if (!/^[A-Za-z0-9_-]+$/.test(spreadsheetId)) throw new ToolError("invalid_argument", "PEER_ACTIONS_SPREADSHEET_ID is invalid");
  const sa = await loadServiceAccount(env);
  const token = await googleAccessToken(sa, fetchImpl);
  const url =
    `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}/values/` +
    `${encodeURIComponent(APPEND_RANGE)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`;
  const body = JSON.stringify({ values: [row] });
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const res = await fetchImpl(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body,
      redirect: "manual",
    });
    if (res.status === 429 && attempt < MAX_ATTEMPTS) {
      await new Promise((r) => setTimeout(r, retryAfterMs(res.headers, attempt)));
      continue;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new ToolError(
        "sheets_write_failed",
        `Google Sheets answered HTTP ${res.status}`,
        { retryable: res.status === 429 || res.status >= 500 },
      );
    }
    return {
      spreadsheet_id: spreadsheetId,
      tab: TAB,
      row,
      updated_range: data?.updates?.updatedRange ?? null,
      updated_rows: data?.updates?.updatedRows ?? null,
    };
  }
  throw new ToolError("sheets_write_failed", "Google Sheets quota retries exhausted", { retryable: true });
}

export function buildPeerActionsRow(args, seat) {
  const acting = args.acting_seat ? cleanSeat("acting_seat", args.acting_seat) : cleanSeat("identity", seat);
  const row = [
    centralTimestamp(),
    acting,
    cleanSeat("requesting_seat", args.requesting_seat),
    cleanLine("request", args.request),
    cleanLine("action_taken", args.action_taken),
    cleanLine("result", args.result),
  ];
  return { row, dry_run: Boolean(args.dry_run) };
}

export const PEER_ACTIONS_METHODS = {
  async tool_peer_actions_log(args, audit) {
    const { row, dry_run } = buildPeerActionsRow(args, this.seat);
    const env = this.workerEnv;
    if (!env) throw new ToolError("not_configured", "peer-actions logging is not bound on this Worker");
    const structured = {
      dry_run,
      spreadsheet_id: String(env.PEER_ACTIONS_SPREADSHEET_ID ?? DEFAULT_SPREADSHEET_ID).trim(),
      tab: TAB,
      row,
    };
    if (dry_run) return okResult(JSON.stringify(structured), structured);
    await this.reserve("write");
    const fetchImpl = globalThis.fetch.bind(globalThis);
    const result = await appendRow(env, row, fetchImpl);
    Object.assign(structured, { dry_run: false, ...result });
    audit.row_len = row.join("").length;
    return okResult(JSON.stringify(structured), structured);
  },
};
