#!/usr/bin/env node
/**
 * fleet-apps-sheet-sync.mjs  (REG, the registry half of the Fleet Reference Sheet)
 * Regenerates the registry tabs of the Fleet Reference Sheet:
 *   1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY
 * The ops tabs (START HERE, Mac and server inventory, web tabs) come from Fleet-OPS
 * scripts/fleet-sheet/fleet-ops-sheet-sync.mjs.  Run this one first, then that one.
 *
 * RUN  from an AI-Fleet-Coordinator lane at origin/main (~/apps/lane new AFC <slug>):
 *   node scripts/fleet-apps-sheet-sync.mjs --dry-run   # builds every tab, prints live vs new row counts, writes nothing
 *   node scripts/fleet-apps-sheet-sync.mjs             # backs up the workbook, then rewrites the tabs below
 * ~/apps/fleet-apps-sheet-sync.mjs is a guarded shim that runs ~/Code/AI-Fleet-Coordinator's copy only when that
 * tree is clean and at origin/main; ios-fleet/publish-ios-versions.sh calls the shim.
 *
 * TABS
 *   RULES & STANDARDS  — generated from scripts/ios-fleet/sheet-rules.json in this repo.
 *   App Store Registry — one row per app in ~/apps/ios-fleet/apps.json, reconciled against live ASC.
 *   TestFlight Builds  — latest uploaded build per app, expiry, public beta link.
 *   Icon Audit         — output of fleet-icon-audit.mjs; the icon health check.
 *   Fleet Apps         — every fleet app in fleet-apps.json, with its repo, lanes and Linear team.
 *   Seats and Bots     — every seat and bot:  fleet-apps.json seats joined with the listener partition, the
 *                        server listener config and the Linear seat apps.  Credential NAMES only, never values.
 *
 * DESIGN RULES (learned the hard way — do not undo)
 *   • The sync OWNS its tabs and rewrites them wholesale:  it clears each tab before writing it, so a tab that
 *     shrinks leaves no stale tail.  Anything hand-typed into a generated tab is destroyed on the next run.
 *   • Every tab is built before the first write, and the run aborts when App Store Connect cannot be read, so a
 *     failed query can never rewrite the registry as "not in ASC".
 *   • apps.json is intent; App Store Connect is fact. The registry tab shows both and
 *     reconciles them, so a stale apps.json surfaces as a FLAG instead of silently
 *     pointing agents at a retired record.
 *   • ASC apps on the account that are NOT in apps.json get their own section. That is
 *     how the retired "IGNORE old ST" record and stray duplicates stay visible.
 *   • Facts no registry file carries live in scripts/fleet-apps-sheet-facts.json, never in the sheet.
 *
 * Auth: Google service account ~/.secrets/google-sheets-sa.json; ASC via ios-fleet/asc-api.mjs.
 * Neither prints credentials.
 */

import { readFileSync, writeFileSync, existsSync, realpathSync, mkdirSync } from "node:fs";
import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import { pathToFileURL, fileURLToPath } from "node:url";

const SPREADSHEET_ID = "1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY";
const IOS_FLEET = "/Users/jay/apps/ios-fleet";
const SA_KEY_PATH = "/Users/jay/.secrets/google-sheets-sa.json";
const APPS_JSON_PATH = `${IOS_FLEET}/apps.json`;
const ICON_REPORT_PATH = `${IOS_FLEET}/artifacts/icon-audit.json`;
const ASC_API_SCRIPT = `${IOS_FLEET}/asc-api.mjs`;
// Tracked sources, read from the checkout this script runs in.
const repoPath = (rel) => fileURLToPath(new URL(`../${rel}`, import.meta.url));
const RULES_PATH = repoPath("scripts/ios-fleet/sheet-rules.json");
const FACTS_PATH = repoPath("scripts/fleet-apps-sheet-facts.json");
const FLEET_APPS_PATH = repoPath("fleet-apps.json");
const PARTITION_PATH = repoPath("docs/protocols/agent-sync-partition.toml");
const SERVER_LISTENER_PATH = repoPath("scripts/agent_sync/server/listener.toml");
const BOARD_CLI_PATH = repoPath("scripts/linear-board/board");
const BACKUP_DIR = "/Users/jay/apps/fleet-ops-sheet/backups";
const LAST_RUN_PATH = "/Users/jay/apps/fleet-ops-sheet/last-run-registry.json";
const MIRROR_MD_PATH = "/Users/jay/apps/FLEET-APPS-REGISTRY.md";

const TAB_RULES = "RULES & STANDARDS";
const TAB_REGISTRY = "App Store Registry";
const TAB_TESTFLIGHT = "TestFlight Builds";
const TAB_ICONS = "Icon Audit";
const TAB_FLEET_APPS = "Fleet Apps";
const TAB_SEATS = "Seats and Bots";

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
  // Fail closed:  a failed query would otherwise rewrite every registry row as "(not in ASC)".
  const appsRes = queryASC("/v1/apps?limit=50");
  const bundleRes = queryASC("/v1/bundleIds?limit=200");
  if (!Array.isArray(appsRes?.data) || appsRes.data.length === 0) {
    throw new Error("App Store Connect /v1/apps returned nothing; refusing to rewrite the registry tabs");
  }
  if (!Array.isArray(bundleRes?.data)) {
    throw new Error("App Store Connect /v1/bundleIds failed; refusing to rewrite the registry tabs");
  }
  const apps = appsRes.data;
  const bundleIds = bundleRes.data;
  const ascAppMap = new Map();
  for (const a of apps) ascAppMap.set(a.attributes?.bundleId, a);
  const ascBundleMap = new Map();
  for (const b of bundleIds) ascBundleMap.set(b.attributes?.identifier, b);
  return { apps, ascAppMap, ascBundleMap };
}

// Memoized:  the registry, TestFlight and unregistered sections all ask for the same app's builds.
const buildsCache = new Map();
function collectBuilds(appleId) {
  if (buildsCache.has(appleId)) return buildsCache.get(appleId);
  const builds = queryASC(`/v1/apps/${appleId}/builds?limit=5&fields[builds]=version,expired,uploadedDate,minOsVersion`)?.data || [];
  const pre = queryASC(`/v1/apps/${appleId}/preReleaseVersions?limit=5&fields[preReleaseVersions]=version,platform`)?.data || [];
  const out = { builds, pre };
  buildsCache.set(appleId, out);
  return out;
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
  rows.push(["Generated by fleet-apps-sheet-sync.mjs from AI-Fleet-Coordinator scripts/ios-fleet/sheet-rules.json.  Edit that file by pull request, then re-run the sync."]);
  rows.push([]);

  for (const section of rules.sections) {
    rows.push([section.heading]);
    rows.push(["ID", "Rule", "Detail"]);
    for (const r of section.rules) rows.push([r.id, r.rule, r.detail]);
    rows.push([]);
  }
  return rows;
}

// Where an apps.json app's code lives, from the lane map (docs/protocols/lane-map.md), not apps.json's
// worktreeHint:  lanes v2 and the Oct 10 lane removals left most of those hints pointing at folders that are gone.
// An app key matches a fleet-apps.json row when, with any "-mac" / "-local" suffix dropped, it equals the row's
// worktreePrefix or starts its codeDir.
export function repoForAppKey(key, fleetApps) {
  const base = String(key).toLowerCase().replace(/-(mac|macos|local)$/, "");
  return (fleetApps?.apps || []).find(a =>
    String(a.worktreePrefix || "").toLowerCase() === base ||
    String(a.codeDir || "").toLowerCase().startsWith(base)) || null;
}

export function worktreeCell(key, conf, fleetApps) {
  const app = repoForAppKey(key, fleetApps);
  if (app) return `~/Code/${app.codeDir} (lanes: ~/apps/lanes/${app.codeDir}/<seat>-<slug>)`;
  return conf.worktreeHint ? `${conf.worktreeHint} (apps.json hint, unverified)` : "";
}

function buildRegistryTab(appsData, asc, iconReport, nowIso, fleetApps = null, facts = {}) {
  const teamId = appsData.teamId || "CC8UTF7ATG";
  const appNotes = facts.appNotes || {};
  const ascNotes = facts.ascRecords || {};
  const rows = [[
    "App Key", "Display Name (apps.json)", "Live ASC Name", "Platform", "Bundle ID",
    "Apple ID", "Registered?", "Latest Build", "Released To App Store?",
    "Icon Status", "Icon Detail", "Release Update XML / Feed", "TestFlight Beta Link", "FLAG", "Repo And Lanes", "Notes"
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
      conf.updateFeedUrl || "—",
      conf.publicBetaLink || "—",
      flags.join("; "),
      worktreeCell(key, conf, fleetApps),
      [appNotes[key], conf.notes].filter(Boolean).join("  ")
    ]);
  }

  // Retired / orphan ASC records — the section that keeps "IGNORE old ST" visible.
  rows.push([]);
  rows.push(["ASC RECORDS NOT IN apps.json — retired, successor (in progress) or unexplained.  The apps.json ship tooling does not upload to these."]);
  rows.push(["Apple ID", "ASC Name", "Bundle ID", "SKU", "Latest Build", "Status", "Action"]);
  for (const a of asc.apps) {
    if (claimed.has(a.id)) continue;
    const at = a.attributes || {};
    const { builds } = collectBuilds(a.id);
    const latest = builds.length ? builds[0].attributes.version : "(no builds)";
    const retired = /^ignore|^retired/i.test(at.name || "");
    const known = ascNotes[a.id];
    rows.push([
      a.id,
      at.name || "",
      at.bundleId || "",
      at.sku || "",
      latest,
      known ? known.status : retired ? "Retired" : "Unexplained",
      known ? known.action
        : retired ? "Retired by owner — safe to remove (see RULES ST-R5/ST-R6)" : "UNEXPLAINED — decide: adopt into apps.json or retire"
    ]);
  }

  rows.push([]);
  rows.push([`Last synced: ${nowIso}  •  team ${teamId}`]);
  return rows;
}

function buildTestFlightTab(appsData, asc, nowIso) {
  const rows = [[
    "App Key", "Bundle ID", "TestFlight Version", "Latest Build", "Uploaded",
    "Expired?", "TestFlight Beta Link", "Last Build Age (days)", "Notes"
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
      conf.publicBetaLink || "—",
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
    rows.push(["No audit report found.  Run, from an AI-Fleet-Coordinator lane:  node scripts/ios-fleet/fleet-icon-audit.mjs"]);
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
  rows.push(["RE-APPROVE AFTER AN INTENTIONAL ICON CHANGE, from an AI-Fleet-Coordinator lane:  node scripts/ios-fleet/fleet-icon-audit.mjs --approve, then land icon-lock.json by pull request"]);
  rows.push([`Last synced: ${nowIso}`]);
  return rows;
}

// ---------------------------------------------------------------- fleet apps + seats

// Tiny readers for the three non-JSON sources.  Each is a regex over a file this repo owns, guarded by tests,
// so the sheet needs no TOML or Python dependency.
export function parsePartition(text) {
  const out = {};
  const body = String(text).split(/^\[seats\]\s*$/m)[1] || "";
  for (const m of body.matchAll(/^([A-Z][A-Z0-9-]*)\s*=\s*"(mac|server|none)"/gm)) out[m[1]] = m[2];
  return out;
}

export function parseServerListener(text) {
  const out = {};
  const parts = String(text).split(/^\[seat\.([A-Z0-9-]+)\]\s*$/m);
  for (let i = 1; i < parts.length; i += 2) {
    const tag = parts[i];
    const body = parts[i + 1].split(/^\[/m)[0];
    const val = (k) => (body.match(new RegExp(`^${k}\\s*=\\s*"([^"]*)"`, "m")) || [])[1];
    const enabled = (body.match(/^enabled\s*=\s*(true|false)/m) || [])[1];
    const routine = body.match(/^routine\s*=\s*\{([^}]*)\}/m);
    const rv = (k) => routine ? (routine[1].match(new RegExp(`${k}\\s*=\\s*"([^"]*)"`)) || [])[1] : undefined;
    const credComment = (body.match(/^creds\s*=\s*"env"\s*#\s*(.*)$/m) || [])[1] || "";
    out[tag] = {
      wake: val("wake"),
      enabled: enabled === undefined ? true : enabled === "true",
      routineUrlEnv: rv("url_env"),
      routineKeyEnv: rv("key_env"),
      routineAuth: rv("auth"),
      credEnvNames: (credComment.match(/ZULIP_[A-Z0-9_]+/g) || []),
    };
  }
  return out;
}

export function parseBoardCli(text) {
  const src = String(text);
  // A dict literal ends at the first line that starts with "}" (values such as "Echo (Instinct)" hold
  // parentheses, so the closing bracket cannot be found by a lazy match); the team tuple is one line.
  const names = (src.match(/^SEAT_APP_NAMES\s*=\s*\{([\s\S]*?)^\}/m) || [])[1] || "";
  const teams = (src.match(/^TEAM_KEYS\s*=\s*\(([^)]*)\)/m) || [])[1] || "";
  const seatAppNames = {};
  for (const m of names.matchAll(/"([A-Z][A-Z0-9-]*)"\s*:\s*"([^"]+)"/g)) seatAppNames[m[1]] = m[2];
  const teamKeys = [...teams.matchAll(/"([A-Z]+)"/g)].map(m => m[1]);
  return { seatAppNames, teamKeys };
}

export function buildFleetAppsTab(fleetApps, teamKeys, nowIso) {
  const rows = [["Acronym", "App", "Repo", "Kind", "Code Dir", "Lanes", "Linear Team", "Status", "Notes"]];
  const teams = new Set(teamKeys || []);
  for (const a of fleetApps.apps || []) {
    rows.push([
      a.acronym,
      a.displayName || a.repo,
      `${fleetApps.owner || "Simple-With-Us"}/${a.repo}`,
      a.kind || "",
      `~/Code/${a.codeDir}`,
      `~/apps/lanes/${a.codeDir}/<seat>-<slug>`,
      teams.has(a.acronym) ? `${a.acronym}  (linear.app/simple-with-us/team/${a.acronym})` : "No team yet:  file under AFC",
      a.retired ? `Retired${a.retiredDate ? ` ${a.retiredDate}` : ""}` : "active",
      a.notes || "",
    ]);
  }
  rows.push([]);
  rows.push([`Generated by fleet-apps-sheet-sync.mjs from AI-Fleet-Coordinator fleet-apps.json (apps) and the board CLI's Linear team keys.  Last synced: ${nowIso}`]);
  return rows;
}

const ZULIP_DOMAIN = "simplewithus.zulipchat.com";
// File code rule (docs/protocols/zulip-fleet-guide.md, Who's Here):  split the tag on hyphens; parts of two
// letters or fewer stay uppercase, longer parts are Title Case.
export function zulipFileCode(tag) {
  return String(tag).split("-").map(p => p.length <= 2 ? p : p[0] + p.slice(1).toLowerCase()).join("-");
}

export function buildSeatsTab({ fleetApps, partition, serverSeats, seatAppNames, facts }, nowIso) {
  const f = facts || {};
  const platforms = f.seatPlatforms || {};
  const hosted = f.hostedSeats || {};
  const macWake = f.macWake || {};
  const unheld = f.unheldWake || {};
  const keysMissing = f.linearKeysMissing || {};
  const rows = [["Seat", "Display Name", "Group", "Platform", "Status", "Branch Prefix", "Zulip Bot",
                 "Zulip Credential (name only)", "Listener", "Wake Path", "Linear App", "Notes"]];
  const zulipEnvNames = (tag) => {
    const code = tag.replace(/-/g, "_");
    return `ZULIP_${code}_EMAIL, ZULIP_${code}_API_KEY`;
  };
  const wakeFor = (tag, instance) => {
    if (instance === "mac") return macWake[tag] || macWake["*"] || "Mac listener";
    if (instance === "server") {
      const s = serverSeats[tag] || {};
      const off = s.enabled === false ? "  (disabled in the server config)" : "";
      if (s.wake === "http") {
        const auth = s.routineAuth === "hmac-sha256" ? "HMAC-signed" : s.routineAuth === "bearer" ? "bearer" : s.routineAuth || "";
        return `Server listener:  ${auth} POST to ${s.routineUrlEnv || "?"} (key ${s.routineKeyEnv || "?"})${off}`;
      }
      return `Server listener:  ${s.wake === "inbox" ? "inbox capture, no wake adapter yet" : s.wake || "not configured"}${off}`;
    }
    if (instance === "none") return tag.startsWith("BF-") ? "BotFleet handles it natively (no listener)" : "No listener";
    return unheld[tag] || "No listener";
  };
  const credFor = (tag, instance, fileCode) => {
    const parts = [];
    if (fileCode) parts.push(`~/.secrets/Zulip/${fileCode}-zuliprc`);
    if (instance === "server") parts.push(`Infisical AI Fleet Coordinator, prod, /zulip:  ${zulipEnvNames(tag)}`);
    return parts.join(";  ");
  };
  const linearFor = (tag) => {
    const app = seatAppNames[tag];
    if (!app) return "Shared Fleet app";
    return keysMissing[tag] ? `${app}  (${keysMissing[tag]})` : app;
  };

  const seen = new Set();
  const active = [], retired = [];
  for (const s of fleetApps.seats || []) {
    seen.add(s.tag);
    const instance = partition[s.tag];
    if (s.retired) {
      // KIMI predates retiredDate and carries its date in its Notes name.
      const date = s.retiredDate || (String(s.notesName || "").match(/retired (\d{4}-\d{2}-\d{2})/) || [])[1];
      retired.push([s.tag, s.notesName || s.tag, "Retired seat", platforms[s.tag] || "",
        `Retired${date ? ` ${date}` : ""}`, (s.branchPrefixes || []).join(", "), "", "", "—", "—", "—",
        s.retiredReason || (s.aliasOf ? `Alias of ${s.aliasOf}.` : "")]);
      continue;
    }
    active.push([s.tag, s.notesName || s.tag, instance === "server" ? "Cloud seat" : "Coding seat", platforms[s.tag] || "",
      "active", (s.branchPrefixes || []).join(", ") || "none (no lanes)", s.zulipBotEmail || "",
      credFor(s.tag, instance, s.zulipFileCode), instance || "not in partition", wakeFor(s.tag, instance),
      linearFor(s.tag), s.notes || ""]);
  }
  // Seats the listener partition knows that fleet-apps.json does not:  the hosted seats, Grok Bot personas
  // and BotFleet role bots.
  const extra = Object.keys(partition).filter(t => !seen.has(t) && t !== "GROK-BUILD");
  const order = (t) => t.startsWith("GB-") ? 2 : t.startsWith("BF-") ? 3 : 1;
  extra.sort((a, b) => order(a) - order(b) || a.localeCompare(b));
  for (const tag of extra) {
    const instance = partition[tag];
    const h = hosted[tag] || {};
    let group = "Cloud seat", name = h.notesName || tag, email = h.zulipBotEmail || "", file = "";
    let platform = platforms[tag] || "";
    if (tag.startsWith("GB-")) {
      const role = tag.slice(3).toLowerCase();
      group = "Grok Bot persona"; platform = "Grok Bot routine";
      name = `GB-${role[0].toUpperCase()}${role.slice(1)}`;
      email = `${role}-grok-bot@${ZULIP_DOMAIN}`;
    } else if (tag.startsWith("BF-")) {
      const role = tag.slice(3).toLowerCase();
      group = "BotFleet bot"; platform = "BotFleet";
      name = `BF-${role[0].toUpperCase()}${role.slice(1)}`;
      email = `bf-${role}-bot@${ZULIP_DOMAIN}`;
      file = zulipFileCode(tag);
    }
    active.push([tag, name, group, platform, "active", "none (no lanes)", email, credFor(tag, instance, file),
      instance, wakeFor(tag, instance), tag.startsWith("BF-") || tag.startsWith("GB-") ? "—" : linearFor(tag), h.notes || ""]);
  }
  rows.push(...active, ...retired);
  rows.push([]);
  rows.push(["GROK followed by BUILD in any spelling (GROK-BUILD, Grok (Build), grok_build) is an alias of GROK, and everything is signed GROK.  The display name is plain Grok (owner Oct 10, AFC #457)."]);
  rows.push([`Generated by fleet-apps-sheet-sync.mjs from AI-Fleet-Coordinator fleet-apps.json (seats), docs/protocols/agent-sync-partition.toml, scripts/agent_sync/server/listener.toml, scripts/linear-board/board (Linear seat apps) and scripts/fleet-apps-sheet-facts.json.  Credential names and paths only, never values.  Last synced: ${nowIso}`]);
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

// Clear, then write.  values.update touches only the range it is given, so without the clear a tab that
// shrinks keeps its old tail (the RULES tab kept stale rows that way until 2026-10-11).  Only tabs this
// script owns are ever passed here.
async function writeTab(title, rows) {
  if (rows.length === 0) throw new Error(`refusing to write an empty ${title} tab`);
  await sheetsApi(`/values/${encodeURIComponent(`${title}!A1:Z2000`)}:clear`, { method: "POST", body: JSON.stringify({}) });
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
  [TAB_REGISTRY]: [100, 140, 150, 72, 200, 96, 108, 100, 148, 96, 180, 240, 240, 200, 240, 300],
  [TAB_TESTFLIGHT]: [100, 200, 108, 100, 108, 72, 240, 108, 220],
  [TAB_ICONS]: [100, 88, 200, 88, 140, 120, 120, 88, 300],
  [TAB_FLEET_APPS]: [72, 160, 240, 80, 200, 260, 240, 80, 320],
  [TAB_SEATS]: [120, 140, 120, 180, 110, 120, 260, 300, 80, 300, 160, 320]
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

// ---------------------------------------------------------------- Markdown mirror (Fleet RAG)

function toMarkdownTable(headers, rows) {
  if (!headers || headers.length === 0) return "";
  const escapeCell = (c) => String(c ?? "").replace(/\r?\n/g, "<br>").replace(/\|/g, "\\|");
  const headerLine = `| ${headers.map(escapeCell).join(" | ")} |`;
  const separatorLine = `| ${headers.map(() => "---").join(" | ")} |`;
  const dataLines = rows.map(r => `| ${headers.map((_, i) => escapeCell(r[i] ?? "")).join(" | ")} |`);
  return [headerLine, separatorLine, ...dataLines].join("\n");
}

// A table body ends at its first blank row; footers and second sections follow it.
const tableBody = (rows, from = 1) => {
  const out = [];
  for (const r of rows.slice(from)) { if (!r.length) break; out.push(r); }
  return out;
};

export function buildMarkdownMirror(rules, tabs, nowIso) {
  const lines = [
    "# Fleet Apps & App Store Connect Registry",
    "",
    `> Generated by \`fleet-apps-sheet-sync.mjs\` (AI-Fleet-Coordinator) from the private Fleet Reference Sheet \`${SPREADSHEET_ID}\` sources and the App Store Connect API.  Do not edit by hand.`,
    "",
    `**Last Synced:** ${nowIso}`,
    "",
    "---",
    "",
    "## 1. App Store Registry",
    "",
    toMarkdownTable(tabs.registry[0], tableBody(tabs.registry)),
    "",
    "## 2. TestFlight Builds",
    "",
    toMarkdownTable(tabs.testflight[0], tableBody(tabs.testflight)),
    "",
    "## 3. Icon Health Audit",
    "",
    toMarkdownTable(tabs.icons[3] || [], tableBody(tabs.icons, 4)),
    "",
    "## 4. Fleet Apps",
    "",
    toMarkdownTable(tabs.fleetApps[0], tableBody(tabs.fleetApps)),
    "",
    "## 5. Seats and Bots",
    "",
    toMarkdownTable(tabs.seats[0], tableBody(tabs.seats)),
    "",
    "## 6. Standing Rules & Standards",
    ""
  ];
  for (const sec of rules.sections || []) {
    lines.push(`### ${sec.heading}`);
    lines.push("");
    for (const r of sec.rules || []) lines.push(`- **${r.id} (${r.rule})**: ${r.detail}`);
    lines.push("");
  }
  return lines.join("\n");
}

function exportMarkdownMirror(markdown) {
  writeFileSync(MIRROR_MD_PATH, markdown, "utf8");
  console.log(`   Exported Markdown mirror: ${MIRROR_MD_PATH}`);
  try {
    const out = execFileSync("recall", ["ingest", MIRROR_MD_PATH], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    console.log(`   Fleet RAG ingested: ${out.trim().split("\n").pop()}`);
  } catch (err) {
    console.warn(`   Note: Fleet RAG ingest skipped (${err.message.split("\n")[0]})`);
  }
}

// ---------------------------------------------------------------- main

async function readLiveCounts(titles) {
  const out = {};
  for (const t of titles) {
    try {
      const v = await sheetsApi(`/values/${encodeURIComponent(t)}`);
      out[t] = (v.values || []).length;
    } catch { out[t] = "(no tab)"; }
  }
  return out;
}

async function backupWorkbook(meta, stamp) {
  const tabs = {};
  for (const s of meta.sheets) {
    const t = s.properties.title;
    const v = await sheetsApi(`/values/${encodeURIComponent(t)}`);
    tabs[t] = v.values || [];
  }
  mkdirSync(BACKUP_DIR, { recursive: true });
  const path = `${BACKUP_DIR}/fleet-sheet-${stamp}-before-registry.json`;
  writeFileSync(path, JSON.stringify({ spreadsheetId: SPREADSHEET_ID, takenAt: new Date().toISOString(), tabs }, null, 1));
  return path;
}

async function main() {
  const DRY = process.argv.includes("--dry-run");
  // --no-asc:  for when App Store Connect cannot be read (for example its key file is missing).  The two
  // ASC tabs are then neither cleared nor written, and the Markdown mirror is left as it is; the other four
  // tabs still sync.  Without the flag an ASC failure aborts the whole run.
  const NO_ASC = process.argv.includes("--no-asc");
  console.log(`Registry tabs -> workbook ${SPREADSHEET_ID}${DRY ? "  (DRY RUN: nothing is written)" : ""}${NO_ASC ? "  (--no-asc: App Store Registry and TestFlight Builds left as they are)" : ""}`);

  console.log("1. Reading sources...");
  const appsData = JSON.parse(readFileSync(APPS_JSON_PATH, "utf8"));
  const rules = JSON.parse(readFileSync(RULES_PATH, "utf8"));
  const facts = JSON.parse(readFileSync(FACTS_PATH, "utf8"));
  const fleetApps = JSON.parse(readFileSync(FLEET_APPS_PATH, "utf8"));
  const partition = parsePartition(readFileSync(PARTITION_PATH, "utf8"));
  const serverSeats = parseServerListener(readFileSync(SERVER_LISTENER_PATH, "utf8"));
  const { seatAppNames, teamKeys } = parseBoardCli(readFileSync(BOARD_CLI_PATH, "utf8"));
  const iconReport = loadIconReport();
  const nowIso = new Date().toISOString().replace("T", " ").substring(0, 19) + " UTC";
  if (!Object.keys(partition).length) throw new Error(`no seats parsed from ${PARTITION_PATH}`);
  if (!teamKeys.length || !Object.keys(seatAppNames).length) throw new Error(`no Linear teams or seat apps parsed from ${BOARD_CLI_PATH}`);

  let asc = null;
  if (NO_ASC) {
    console.log("2. Skipping App Store Connect (--no-asc).");
  } else {
    console.log("2. Querying App Store Connect...");
    asc = collectASC();
    console.log(`   ${asc.apps.length} ASC app records, ${asc.ascBundleMap.size} bundle IDs.`);
  }

  console.log("3. Building every tab before any write...");
  const tabs = {
    rules: buildRulesTab(rules),
    registry: asc ? buildRegistryTab(appsData, asc, iconReport, nowIso, fleetApps, facts) : null,
    testflight: asc ? buildTestFlightTab(appsData, asc, nowIso) : null,
    icons: buildIconTab(iconReport, nowIso),
    fleetApps: buildFleetAppsTab(fleetApps, teamKeys, nowIso),
    seats: buildSeatsTab({ fleetApps, partition, serverSeats, seatAppNames, facts }, nowIso),
  };
  const plan = [
    // [key, title, header row indexes, column count]
    ["rules", TAB_RULES, [0], 3],
    ["registry", TAB_REGISTRY, [0], 16],
    ["testflight", TAB_TESTFLIGHT, [0], 9],
    ["icons", TAB_ICONS, [3], 9],
    ["fleetApps", TAB_FLEET_APPS, [0], 9],
    ["seats", TAB_SEATS, [0], 12],
  ].filter(([key]) => tabs[key] !== null);
  const appCount = Object.keys(appsData.apps || {}).length;
  if (tabs.registry && tabs.registry.length < appCount + 1) throw new Error("registry tab built short; refusing to write");
  if (tabs.fleetApps.length < (fleetApps.apps || []).length + 1) throw new Error("Fleet Apps tab built short; refusing to write");

  console.log("4. Minting Google access token...");
  token = await getGoogleAccessToken();
  const live = await readLiveCounts(plan.map(p => p[1]));
  for (const [key, title] of plan) {
    console.log(`   ${title.padEnd(22)} live ${String(live[title]).padStart(9)}  ->  new ${String(tabs[key].length).padStart(4)}`);
  }
  if (DRY) {
    console.log("\nDry run:  no tab created, cleared or written; no backup, mirror or last-run file.");
    return;
  }

  console.log("5. Backing up the workbook...");
  let meta = await sheetsApi("?fields=sheets.properties");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  console.log(`   ${await backupWorkbook(meta, stamp)}`);

  console.log("6. Writing tabs (each cleared first)...");
  const written = [];
  for (const [key, title, headerRows, cols] of plan) {
    const t = await ensureTab(meta, title);
    await writeTab(title, tabs[key]);
    await formatTab(t.sheetId, headerRows, cols, TAB_COLUMN_WIDTHS[title], tabs[key].length);
    written.push({ tab: title, rows: tabs[key].length, created: t.created });
    console.log(`   ${title.padEnd(22)} ${tabs[key].length} rows${t.created ? "  (tab created)" : ""}`);
  }

  if (asc) {
    console.log("7. Exporting Markdown mirror for Fleet RAG...");
    exportMarkdownMirror(buildMarkdownMirror(rules, tabs, nowIso));
  } else {
    console.log("7. Markdown mirror left as it is (--no-asc).");
  }

  writeFileSync(LAST_RUN_PATH, JSON.stringify({ ranAt: new Date().toISOString(), noAsc: NO_ASC, tabs: written }, null, 2));

  console.log(`\n✅ Sheet regenerated: https://docs.google.com/spreadsheets/d/${SPREADSHEET_ID}/edit`);
  const errCount = (iconReport?.counts?.error || 0);
  if (errCount) console.log(`   ⚠️  Icon audit reports ${errCount} error(s) — see the ${TAB_ICONS} tab.`);
}

if (isScriptEntryPoint()) {
  main().catch(err => {
    console.error("Sheet sync failed:", err.message);
    process.exit(1);
  });
}
