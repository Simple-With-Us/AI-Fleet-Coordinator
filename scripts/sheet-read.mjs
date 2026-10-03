#!/usr/bin/env node
/**
 * sheet-read.mjs — read-only dump of the Fleet ASC / TestFlight registry sheet.
 * Spreadsheet: 1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY (strictly private).
 * Auth: service account ~/.secrets/google-sheets-sa.json. Never prints credentials.
 *
 * Usage:
 *   node sheet-read.mjs                 # metadata + all tabs, first 60 rows each
 *   node sheet-read.mjs "<SheetName>"   # one tab
 *   node sheet-read.mjs --meta          # tab names + grid sizes only
 */
import { readFileSync, existsSync } from "node:fs";
import crypto from "node:crypto";

const SPREADSHEET_ID = "1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY";
const SA_KEY_PATH = "/Users/jay/.secrets/google-sheets-sa.json";

function base64url(input) {
  return Buffer.from(input).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function getGoogleAccessToken() {
  if (!existsSync(SA_KEY_PATH)) throw new Error(`missing service account key at ${SA_KEY_PATH}`);
  const sa = JSON.parse(readFileSync(SA_KEY_PATH, "utf8"));
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "RS256", typ: "JWT" };
  const payload = {
    iss: sa.client_email,
    scope: "https://www.googleapis.com/auth/spreadsheets",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600
  };
  const signingInput = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(payload))}`;
  const signer = crypto.createSign("RSA-SHA256");
  signer.update(signingInput);
  signer.end();
  const signature = signer.sign(sa.private_key, "base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${signingInput}.${signature}`
  });
  const data = await res.json();
  if (!res.ok) throw new Error(`Google OAuth error: ${JSON.stringify(data)}`);
  return data.access_token;
}

const token = await getGoogleAccessToken();
const auth = { Authorization: `Bearer ${token}` };

const metaRes = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}`, { headers: auth });
if (!metaRes.ok) {
  console.error(`metadata error ${metaRes.status}: ${JSON.stringify(await metaRes.json())}`);
  process.exit(2);
}
const meta = await metaRes.json();
const tabs = meta.sheets.map(s => s.properties);
console.log(`Spreadsheet: ${meta.properties.title}`);
console.log(`Tabs: ${tabs.length}`);
for (const t of tabs) {
  console.log(`  - "${t.title}"  sheetId=${t.sheetId}  grid=${t.gridProperties ? `${t.gridProperties.rowCount}x${t.gridProperties.columnCount}` : "?"}`);
}

const only = process.argv[2];
if (only === "--meta") process.exit(0);

for (const t of tabs) {
  if (only && only !== "--meta" && t.title !== only) continue;
  const url = `https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}/values/${encodeURIComponent(t.title)}`;
  const res = await fetch(url, { headers: auth });
  if (!res.ok) {
    console.error(`\n[${t.title}] read error ${res.status}: ${JSON.stringify(await res.json())}`);
    continue;
  }
  const { values = [] } = await res.json();
  console.log(`\n===== TAB "${t.title}" — ${values.length} rows with data =====`);
  values.slice(0, 60).forEach((row, i) => {
    const cells = row.map(c => String(c ?? "")).join(" | ");
    console.log(`R${String(i + 1).padStart(3)}: ${cells}`);
  });
  if (values.length > 60) console.log(`... ${values.length - 60} more rows`);
}
