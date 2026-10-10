#!/usr/bin/env node
/**
 * fleet-apps-sheet-sync.mjs
 * Regenerates the Fleet App Store + TestFlight registry Google Sheet:
 *   1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY  ("Fleet Apps Inventory")
 *
 * TABS
 *   RULES & STANDARDS  — the rules/standards/policies block, generated from
 *                        sheet-rules.json so it can never be clobbered by a sync.
 *   App Store Registry — one row per fleet app, reconciled against live ASC.
 *   TestFlight Builds  — latest uploaded build per app, expiry, beta groups.
 *   Icon Audit         — output of fleet-icon-audit.mjs; the icon health check.
 *   Legacy …           — pre-2026-10-02 hand-written rows, preserved untouched.
 *
 * DESIGN RULES (learned the hard way — do not undo)
 *   • The sync OWNS its tabs and rewrites them wholesale. Anything hand-typed into a
 *     generated tab is destroyed on the next run. That is exactly why the rules block
 *     is generated from a file instead of being pasted into the sheet by hand.
 *   • apps.json is intent; App Store Connect is fact. The registry tab shows both and
 *     reconciles them, so a stale apps.json surfaces as a FLAG instead of silently
 *     pointing agents at a retired record.
 *   • ASC apps on the account that are NOT in apps.json get their own section. That is
 *     how the retired "IGNORE old ST" record and stray duplicates stay visible.
 *
 * Auth: Google service account ~/.secrets/google-sheets-sa.json; ASC via ios-fleet/asc-api.mjs.
 * Neither prints credentials.
 */

import { readFileSync, writeFileSync, existsSync, realpathSync } from "node:fs";
import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";

const SPREADSHEET_ID = "1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY";
const IOS_FLEET = "/Users/jay/apps/ios-fleet";
const SA_KEY_PATH = "/Users/jay/.secrets/google-sheets-sa.json";
const APPS_JSON_PATH = `${IOS_FLEET}/apps.json`;
const RULES_PATH = `${IOS_FLEET}/sheet-rules.json`;
const ICON_REPORT_PATH = `${IOS_FLEET}/artifacts/icon-audit.json`;
const ASC_API_SCRIPT = `${IOS_FLEET}/asc-api.mjs`;

const TAB_RULES = "RULES & STANDARDS";
const TAB_REGISTRY = "App Store Registry";
const TAB_TESTFLIGHT = "TestFlight Builds";
const TAB_ICONS = "Icon Audit";
const LEGACY_TAB = "Legacy pre-2026-10-02";

// ---------------------------------------------------------------- auth

function base64url(input) {
  return Buffer.from(input).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function getGoogleAccessToken() {
  if (!existsSync(SA_KEY_PATH)) throw new Error(`Google service account key not found at ${SA_KEY_PATH}`);
  const sa = JSON.parse(readFileSync(SA_KEY_PATH, "utf8"));
  const now = Math.floor(Date.now() / 1000);
  const signingInput = `${base64url(JSON.stringify({ alg: "RS256", typ: "JWT" }))}.${base64url(JSON.stringify({
    iss: sa.client_email,
    scope: "https://www.googleapis.com/auth/spreadsheets",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600
  }))}`;
  const signer = crypto.createSign("RSA-SHA256");
  signer.update(signingInput);
  signer.end();
  const jwt = `${signingInput}.${signer.sign(sa.private_key, "base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`
  });
  const data = await res.json();
  if (!res.ok) throw new Error(`Google OAuth error: ${JSON.stringify(data)}`);
  return data.access_token;
}

let token;
async function sheetsApi(path, init = {}) {
  const res = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${SPREADSHEET_ID}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(init.headers || {}) }
  });
  if (!res.ok) throw new Error(`Sheets API ${init.method || "GET"} ${path} -> ${res.status}: ${JSON.stringify(await res.json())}`);
  return res.json();
}

function queryASC(endpoint) {
  try {
    const raw = execFileSync("node", [ASC_API_SCRIPT, "GET", endpoint], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    // asc-api.mjs prints an "HTTP 200" status line before the JSON body.
    const jsonStart = raw.indexOf("{");
    return JSON.parse(raw.slice(jsonStart));
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- collect

function collectASC() {
  const apps = queryASC("/v1/apps?limit=50")?.data || [];
  const bundleIds = queryASC("/v1/bundleIds?limit=200")?.data || [];
  const ascAppMap = new Map();
  for (const a of apps) ascAppMap.set(a.attributes?.bundleId, a);
  const ascBundleMap = new Map();
  for (const b of bundleIds) ascBundleMap.set(b.attributes?.identifier, b);
  return { apps, ascAppMap, ascBundleMap };
}

function collectBuilds(appleId) {
  const builds = queryASC(`/v1/apps/${appleId}/builds?limit=5&fields[builds]=version,expired,uploadedDate,minOsVersion`)?.data || [];
  const pre = queryASC(`/v1/apps/${appleId}/preReleaseVersions?limit=5&fields[preReleaseVersions]=version,platform`)?.data || [];
  return { builds, pre };
}

function loadIconReport() {
  if (!existsSync(ICON_REPORT_PATH)) return null;
  try { return JSON.parse(readFileSync(ICON_REPORT_PATH, "utf8")); } catch { return null; }
}

function iconStatusFor(iconReport, key) {
  if (!iconReport || !iconReport.apps || !iconReport.apps[key]) return { verdict: "NOT AUDITED", detail: "" };
  const app = iconReport.apps[key];
  const errs = (iconReport.findings || []).filter(f => f.appKey === key && f.level === "error");
  const warns = (iconReport.findings || []).filter(f => f.appKey === key && f.level === "warn");
  let verdict = "OK";
  if (!app.repoRoot || (app.sets || []).length === 0) verdict = "NO ICON SET";
  else if (errs.length) verdict = "ERROR";
  else if (warns.length) verdict = "WARN";
  const bits = [];
  if (errs.length) bits.push(...errs.map(f => f.code));
  else if (warns.length) bits.push(...warns.map(f => f.code));
  return {
    verdict,
    detail: bits.join(", "),
    drift: app.driftStatus || "n/a",
    master: app.master || "",
    shipped: app.shippedHash || "",
    approved: app.approvedHash || ""
  };
}

// ---------------------------------------------------------------- tab builders

function buildRulesTab(rules) {
  const rows = [];
  rows.push([rules.title]);
  rows.push([rules.subtitle]);
  rows.push([rules.updatedNote]);
  rows.push([]);
  rows.push(["Generated by fleet-apps-sheet-sync.mjs from ios-fleet/sheet-rules.json. Edit that file, then re-run the sync."]);
  rows.push([]);

  for (const section of rules.sections) {
    rows.push([section.heading]);
    rows.push(["ID", "Rule", "Detail"]);
    for (const r of section.rules) rows.push([r.id, r.rule, r.detail]);
    rows.push([]);
  }
  return rows;
}

function buildRegistryTab(appsData, asc, iconReport, nowIso) {
  const teamId = appsData.teamId || "CC8UTF7ATG";
  const rows = [[
    "App Key", "Display Name (apps.json)", "Live ASC Name", "Platform", "Bundle ID",
    "Apple ID", "Registered?", "Latest Build", "Released To App Store?",
    "Icon Status", "Icon Detail", "FLAG", "Worktree", "Notes"
  ]];

  const claimed = new Set();
  const allFindings = (iconReport?.findings || []);
  const allErrors = new Set(allFindings.filter(f => f.level === "error").map(f => f.appKey));
  const laneDrift = new Set(allFindings.filter(f => f.code === "LANE_ICON_DRIFT").map(f => f.appKey));

  for (const [key, conf] of Object.entries(appsData.apps)) {
    const bundleId = conf.bundleId;
    const ascApp = asc.ascAppMap.get(bundleId) || null;
    const ascBundle = asc.ascBundleMap.get(bundleId) || null;
    if (ascApp) claimed.add(ascApp.id);

    const icon = iconStatusFor(iconReport, key);
    const flags = [];

    if (!ascApp) flags.push("NO ASC APP for this bundle ID");
    if (conf.appleId && ascApp && String(conf.appleId) !== ascApp.id) {
      flags.push(`appleId mismatch: apps.json=${conf.appleId} ASC=${ascApp.id}`);
    }
    if (/^ignore|^retired/i.test(ascApp?.attributes?.name || "")) flags.push("apps.json points at a RETIRED record");
    if (allErrors.has(key)) flags.push("icon error");
    if (laneDrift.has(key)) flags.push("lane icon drift");

    let latest = "";
    if (ascApp) {
      const { builds } = collectBuilds(ascApp.id);
      if (builds.length) {
        const b = builds[0].attributes || {};
        latest = `${b.version}${b.expired ? " (EXPIRED)" : ""}`;
      }
    }

    rows.push([
      key,
      conf.displayName || key,
      ascApp ? ascApp.attributes.name : "(not in ASC)",
      conf.platform || "iOS",
      bundleId || "",
      String(ascApp ? ascApp.id : (conf.appleId || "—")),
      ascBundle ? `Yes (${ascBundle.id})` : "No",
      latest,
      "No — TestFlight only",
      icon.verdict,
      icon.detail,
      flags.join("; "),
      conf.worktreeHint || "",
      conf.notes || ""
    ]);
  }

  // Retired / orphan ASC records — the section that keeps "IGNORE old ST" visible.
  rows.push([]);
  rows.push(["RETIRED / UNREGISTERED ASC RECORDS — not in apps.json. Do not upload builds to these."]);
  rows.push(["Apple ID", "ASC Name", "Bundle ID", "SKU", "Latest Build", "Action"]);
  for (const a of asc.apps) {
    if (claimed.has(a.id)) continue;
    const at = a.attributes || {};
    const { builds } = collectBuilds(a.id);
    const latest = builds.length ? builds[0].attributes.version : "(no builds)";
    const retired = /^ignore|^retired/i.test(at.name || "");
    rows.push([
      a.id,
      at.name || "",
      at.bundleId || "",
      at.sku || "",
      latest,
      retired ? "Retired by owner — safe to remove (see RULES ST-R5/ST-R6)" : "UNEXPLAINED — decide: adopt into apps.json or retire"
    ]);
  }

  rows.push([]);
  rows.push([`Last synced: ${nowIso}  •  team ${teamId}`]);
  return rows;
}

function buildTestFlightTab(appsData, asc, nowIso) {
  const rows = [[
    "App Key", "Bundle ID", "TestFlight Version", "Latest Build", "Uploaded",
    "Expired?", "External Testing?", "Last Build Age (days)", "Notes"
  ]];

  for (const [key, conf] of Object.entries(appsData.apps)) {
    const ascApp = asc.ascAppMap.get(conf.bundleId);
    if (!ascApp) {
      rows.push([key, conf.bundleId || "", "—", "—", "—", "—", "—", "", "No ASC app record for this bundle ID"]);
      continue;
    }
    const { builds, pre } = collectBuilds(ascApp.id);
    const b = builds[0]?.attributes;
    const p = pre[0]?.attributes;
    if (!b) {
      rows.push([key, conf.bundleId, p ? p.version : "—", "(none uploaded)", "—", "—", "—", "", "ASC app exists but no build has ever been uploaded"]);
      continue;
    }
    const uploaded = b.uploadedDate || "";
    const ageDays = uploaded ? Math.floor((Date.now() - new Date(uploaded).getTime()) / 86400000) : "";
    const flags = [];
    if (b.expired) flags.push("build expired — testers cannot install");
    if (typeof ageDays === "number" && ageDays > 90) flags.push("no new build in 90+ days");
    rows.push([
      key,
      conf.bundleId,
      p ? p.version : "—",
      b.version,
      uploaded ? uploaded.slice(0, 10) : "—",
      b.expired ? "YES" : "no",
      "",
      ageDays,
      flags.join("; ")
    ]);
  }

  rows.push([]);
  rows.push([`Last synced: ${nowIso}. Release mode for every fleet app: MANUAL (automatic release disabled by owner 2026-10-02 — see RULES REL-1).`]);
  return rows;
}

function buildIconTab(iconReport, nowIso) {
  const rows = [["Fleet icon audit — the standing check that stops icons rotting. See RULES section 5 (ICO-1..ICO-5)."]];
  if (!iconReport) {
    rows.push([]);
    rows.push(["No audit report found. Run: node /Users/jay/apps/ios-fleet/fleet-icon-audit.mjs"]);
    return rows;
  }
  rows.push([`Generated: ${iconReport.generatedAt}  •  apps audited: ${iconReport.appsAudited}  •  errors: ${iconReport.counts.error || 0}  •  warnings: ${iconReport.counts.warn || 0}`]);
  rows.push([]);
  rows.push(["App Key", "Verdict", "Repo", "Icon Sets", "Master", "Shipped Hash", "Approved Hash", "Drift", "Findings"]);
  for (const [key, app] of Object.entries(iconReport.apps || {})) {
    const errs = (iconReport.findings || []).filter(f => f.appKey === key && f.level === "error");
    const warns = (iconReport.findings || []).filter(f => f.appKey === key && f.level === "warn");
    const verdict = !app.repoRoot || (app.sets || []).length === 0 ? "NO ICON SET" : errs.length ? "ERROR" : warns.length ? "WARN" : "OK";
    rows.push([
      key,
      verdict,
      app.repoRoot || "(missing)",
      app.setCount ?? 0,
      app.master || "",
      app.shippedHash || "",
      app.approvedHash || "",
      app.driftStatus || "n/a",
      [...errs, ...warns].map(f => `${f.level === "error" ? "E" : "W"} ${f.code}: ${f.message}`).join("\n")
    ]);
  }
  rows.push([]);
  rows.push(["RE-APPROVE AFTER AN INTENTIONAL ICON CHANGE: node /Users/jay/apps/ios-fleet/fleet-icon-audit.mjs --approve"]);
  rows.push([`Last synced: ${nowIso}`]);
  return rows;
}

// ---------------------------------------------------------------- sheet plumbing

async function ensureTab(meta, title) {
  const existing = meta.sheets.find(s => s.properties.title === title);
  if (existing) return { sheetId: existing.properties.sheetId, created: false };
  const res = await sheetsApi(":batchUpdate", {
    method: "POST",
    // Omit sheetId entirely — the API rejects -1 and picks an unused id for us.
    body: JSON.stringify({ requests: [{ addSheet: { properties: { title } } }] })
  });
  const newId = res.replies[0].addSheet.properties.sheetId;
  meta.sheets.push({ properties: { sheetId: newId, title } });
  return { sheetId: newId, created: true };
}

async function writeTab(title, rows) {
  if (rows.length === 0) return;
  const range = `${title}!A1:Z${rows.length}`;
  await sheetsApi(`/values/${encodeURIComponent(range)}?valueInputOption=USER_ENTERED`, {
    method: "PUT",
    body: JSON.stringify({ range, majorDimension: "ROWS", values: rows })
  });
}

const DEFAULT_COL_WIDTH_PX = 132;
const MAX_COL_WIDTH_PX = 320;
const FORMAT_ROW_COUNT = 1000;

/** Pixel widths per generated tab (long-text columns get more room; all capped at MAX_COL_WIDTH_PX). */
const TAB_COLUMN_WIDTHS = {
  // Column A carries title, subtitle, section headings, and other single-cell rows.
  [TAB_RULES]: [320, 220, 320],
  [TAB_REGISTRY]: [100, 140, 150, 72, 200, 96, 108, 100, 148, 96, 180, 200, 140, 240],
  [TAB_TESTFLIGHT]: [100, 200, 108, 100, 108, 72, 120, 108, 220],
  [TAB_ICONS]: [100, 88, 200, 88, 140, 120, 120, 88, 300]
};

function columnWidthPx(columnWidths, index) {
  const w = columnWidths?.[index];
  const px = typeof w === "number" && w > 0 ? w : DEFAULT_COL_WIDTH_PX;
  return Math.min(px, MAX_COL_WIDTH_PX);
}

/**
 * Build Sheets batchUpdate requests for tab chrome (headers, wrap, column widths, freeze).
 * Exported for unit tests — does not call the API.
 */
export function buildFormatTabRequests(sheetId, headerRows, colCount, columnWidths, rowCount) {
  const wrapEndRow = Math.min(FORMAT_ROW_COUNT, Math.max(1, rowCount));
  const requests = [];
  for (const hr of headerRows) {
    requests.push({
      repeatCell: {
        range: { sheetId, startRowIndex: hr, endRowIndex: hr + 1, startColumnIndex: 0, endColumnIndex: colCount },
        cell: { userEnteredFormat: { textFormat: { bold: true }, backgroundColor: { red: 0.88, green: 0.9, blue: 0.94 } } },
        fields: "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor"
      }
    });
  }
  requests.push({
    repeatCell: {
      range: {
        sheetId,
        startRowIndex: 0,
        endRowIndex: wrapEndRow,
        startColumnIndex: 0,
        endColumnIndex: colCount
      },
      cell: {
        userEnteredFormat: {
          wrapStrategy: "WRAP",
          verticalAlignment: "TOP"
        }
      },
      fields: "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"
    }
  });
  for (let i = 0; i < colCount; i++) {
    requests.push({
      updateDimensionProperties: {
        range: { sheetId, dimension: "COLUMNS", startIndex: i, endIndex: i + 1 },
        properties: { pixelSize: columnWidthPx(columnWidths, i) },
        fields: "pixelSize"
      }
    });
  }
  requests.push({
    updateSheetProperties: {
      properties: { sheetId, gridProperties: { frozenRowCount: headerRows.length ? Math.max(...headerRows) + 1 : 0 } },
      fields: "gridProperties.frozenRowCount"
    }
  });
  // Clear the old tail so a shrunken table doesn't leave orphan rows behind.
  requests.push({
    updateSheetProperties: {
      properties: { sheetId, gridProperties: { rowCount: FORMAT_ROW_COUNT } },
      fields: "gridProperties.rowCount"
    }
  });
  return requests;
}

async function formatTab(sheetId, headerRows, colCount, columnWidths, rowCount) {
  const requests = buildFormatTabRequests(sheetId, headerRows, colCount, columnWidths, rowCount);
  await sheetsApi(":batchUpdate", { method: "POST", body: JSON.stringify({ requests }) });
}

/** True when this module is the process entry (direct node invocation or import.meta.main). */
export function isScriptEntryPoint(entryPath = process.argv[1]) {
  if (import.meta.main) return true;
  if (!entryPath) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(entryPath)).href;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- main

async function main() {
  console.log("1. Reading registry + rules...");
  const appsData = JSON.parse(readFileSync(APPS_JSON_PATH, "utf8"));
  const rules = JSON.parse(readFileSync(RULES_PATH, "utf8"));
  const iconReport = loadIconReport();
  const nowIso = new Date().toISOString().replace("T", " ").substring(0, 19) + " UTC";

  console.log("2. Minting Google access token...");
  token = await getGoogleAccessToken();

  console.log("3. Inspecting spreadsheet...");
  let meta = await sheetsApi("?fields=sheets.properties");

  // Preserve the legacy hand-written tab under a clear name, once.
  const old = meta.sheets.find(s => s.properties.title === "Untitled");
  if (old) {
    await sheetsApi(":batchUpdate", {
      method: "POST",
      body: JSON.stringify({ requests: [{ updateSheetProperties: { properties: { sheetId: old.properties.sheetId, title: LEGACY_TAB }, fields: "title" } }] })
    });
    console.log(`   Renamed legacy tab "Untitled" -> "${LEGACY_TAB}" (preserved, not rewritten).`);
    meta = await sheetsApi("?fields=sheets.properties");
  }

  console.log("4. Querying App Store Connect...");
  const asc = collectASC();
  console.log(`   ${asc.apps.length} ASC app records, ${asc.ascBundleMap.size} bundle IDs.`);

  console.log("5. Ensuring tabs...");
  const tabRules = await ensureTab(meta, TAB_RULES);
  const tabRegistry = await ensureTab(meta, TAB_REGISTRY);
  const tabTestflight = await ensureTab(meta, TAB_TESTFLIGHT);
  const tabIcons = await ensureTab(meta, TAB_ICONS);
  console.log(`   rules=${tabRules.created ? "created" : "exists"} registry=${tabRegistry.created ? "created" : "exists"} testflight=${tabTestflight.created ? "created" : "exists"} icons=${tabIcons.created ? "created" : "exists"}`);

  console.log("6. Writing tabs...");
  const rulesRows = buildRulesTab(rules);
  await writeTab(TAB_RULES, rulesRows);
  const registryRows = buildRegistryTab(appsData, asc, iconReport, nowIso);
  await writeTab(TAB_REGISTRY, registryRows);
  const testFlightRows = buildTestFlightTab(appsData, asc, nowIso);
  await writeTab(TAB_TESTFLIGHT, testFlightRows);
  const iconRows = buildIconTab(iconReport, nowIso);
  await writeTab(TAB_ICONS, iconRows);

  console.log("7. Formatting...");
  await formatTab(tabRules.sheetId, [0], 3, TAB_COLUMN_WIDTHS[TAB_RULES], rulesRows.length);
  await formatTab(tabRegistry.sheetId, [0], 14, TAB_COLUMN_WIDTHS[TAB_REGISTRY], registryRows.length);
  await formatTab(tabTestflight.sheetId, [0], 9, TAB_COLUMN_WIDTHS[TAB_TESTFLIGHT], testFlightRows.length);
  await formatTab(tabIcons.sheetId, [3], 9, TAB_COLUMN_WIDTHS[TAB_ICONS], iconRows.length);

  console.log(`\n✅ Sheet regenerated: https://docs.google.com/spreadsheets/d/${SPREADSHEET_ID}/edit`);
  console.log(`   Tabs: ${TAB_RULES} | ${TAB_REGISTRY} | ${TAB_TESTFLIGHT} | ${TAB_ICONS} | ${LEGACY_TAB}`);
  const errCount = (iconReport?.counts?.error || 0);
  if (errCount) console.log(`   ⚠️  Icon audit reports ${errCount} error(s) — see the ${TAB_ICONS} tab.`);
}

if (isScriptEntryPoint()) {
  main().catch(err => {
    console.error("Sheet sync failed:", err.message);
    process.exit(1);
  });
}
