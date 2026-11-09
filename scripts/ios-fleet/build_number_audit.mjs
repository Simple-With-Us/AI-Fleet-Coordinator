#!/usr/bin/env node
/**
 * build_number_audit.mjs — read-only fleet build-number audit (AFC-675).
 *
 * For every app in testflight-apps.json it reads App Store Connect builds and
 * reports drift from the fleet rule:  build numbers are YYYYMMDDHHMM in
 * America/Chicago, shared between iOS and Mac for a release, and monotonically
 * increasing.  Marketing version is 0.1.0 for all apps except Congress.Trade
 * (1.1.2), unless an approved App Store version is higher.
 *
 *   node build_number_audit.mjs              human-readable table
 *   node build_number_audit.mjs --json       machine-readable report
 *   node build_number_audit.mjs --strict     exit 1 when findings exist
 *   node build_number_audit.mjs --app contactlogo,contactlogo-mac
 *   node build_number_audit.mjs --all-history   # every train (noisy)
 *
 * Exit 0 unless --strict and the report has findings.  No App Store Connect writes.
 *
 * Credentials:  same asc-api.mjs path as ios_testflight_freshness.mjs (imported).
 */
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  loadRegistry,
  makeContext,
  makeDeps,
  resolveFleetFile,
  ownerTime,
} from "./ios_testflight_freshness.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

export const BUILD_NUMBER_RE = /^20\d{10}$/;
export const EXIT_OK = 0;
export const EXIT_STRICT = 1;
export const EXIT_FAILED = 2;

/** App Store states that mean a version is approved and may raise the marketing floor. */
export const APPROVED_STORE_STATES = new Set([
  "READY_FOR_SALE",
  "PENDING_APPLE_RELEASE",
  "PROCESSING_FOR_APP_STORE",
]);

/** First moment of 2026-10-29 on the owner's clock (America/Chicago, CDT → UTC). */
export const FLEET_TIMESTAMP_SCHEME_START_MS = Date.parse("2026-10-29T05:00:00.000Z");

const FINDING = {
  INVALID_BUILD_NUMBER: "invalid_build_number",
  NON_MONOTONIC: "non_monotonic_upload",
  IOS_MAC_MISMATCH: "ios_mac_build_mismatch",
  MARKETING_DRIFT: "marketing_version_drift",
};

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

export function parseAuditArgs(argv) {
  const opts = {
    apps: null,
    json: false,
    strict: false,
    allHistory: false,
    registry: null,
    limit: 30,
    help: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => {
      if (i + 1 >= argv.length) throw new Error(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--app") opts.apps = val().split(",").map((s) => s.trim()).filter(Boolean);
    else if (a === "--json") opts.json = true;
    else if (a === "--strict") opts.strict = true;
    else if (a === "--all-history") opts.allHistory = true;
    else if (a === "--registry") opts.registry = val();
    else if (a === "--limit") opts.limit = Math.max(1, parseInt(val(), 10) || 30);
    else if (a === "-h" || a === "--help") opts.help = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return opts;
}

/**
 * Default audit scope: newest build head, monotonicity on the current marketing train
 * since the timestamp scheme start, or the full history when allHistory is set.
 */
export function scopeBuildsForAudit(builds, { allHistory = false, schemeStartMs = FLEET_TIMESTAMP_SCHEME_START_MS } = {}) {
  const sorted = [...(builds || [])].sort((a, b) => (b.uploadedAt || 0) - (a.uploadedAt || 0));
  if (allHistory || !sorted.length) {
    return {
      scope: "all",
      newest: sorted[0] || null,
      currentMarketingVersion: sorted[0]?.marketingVersion ?? null,
      formatBuilds: sorted,
      marketingBuilds: sorted,
      monotonicBuilds: sorted,
      pairBuildsByPlatform: null,
    };
  }

  const newest = sorted[0];
  const currentMarketingVersion = newest.marketingVersion ?? null;
  const train = currentMarketingVersion
    ? sorted.filter((b) => b.marketingVersion === currentMarketingVersion)
    : sorted;
  const monotonicBuilds = train.filter((b) => b.uploadedAt != null && b.uploadedAt >= schemeStartMs);

  return {
    scope: "current",
    newest,
    currentMarketingVersion,
    formatBuilds: newest ? [newest] : [],
    marketingBuilds: newest ? [newest] : [],
    monotonicBuilds,
    pairBuildsByPlatform: "current_train",
  };
}

/** Product group for iOS/Mac pairing (`contactlogo-mac` → `contactlogo`). */
export function productGroupKey(registryKey) {
  return String(registryKey || "").replace(/-mac$/, "");
}

export function expectedMarketingBase(registryKey) {
  const g = productGroupKey(registryKey);
  return g === "congress" ? "1.1.2" : "0.1.0";
}

export function parseVersionParts(v) {
  return String(v || "")
    .trim()
    .split(".")
    .map((x) => {
      const n = parseInt(x, 10);
      return Number.isFinite(n) ? n : 0;
    });
}

export function compareSemver(a, b) {
  const pa = parseVersionParts(a);
  const pb = parseVersionParts(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const da = pa[i] ?? 0;
    const db = pb[i] ?? 0;
    if (da !== db) return da < db ? -1 : 1;
  }
  return 0;
}

export function effectiveExpectedMarketing(registryKey, approvedStoreVersion) {
  const base = expectedMarketingBase(registryKey);
  if (!approvedStoreVersion) return base;
  return compareSemver(approvedStoreVersion, base) > 0 ? approvedStoreVersion : base;
}

export function buildNumberValue(build) {
  const n = BigInt(String(build || "0").replace(/\D/g, "") || "0");
  return n;
}

export function normalizeAscBuilds(json, platform) {
  const pre = new Map(
    (json.included || [])
      .filter((x) => x.type === "preReleaseVersions")
      .map((x) => [x.id, x.attributes || {}]),
  );
  const out = [];
  for (const b of json.data || []) {
    const a = b.attributes || {};
    const rel = b.relationships?.preReleaseVersion?.data;
    const p = rel ? pre.get(rel.id) || {} : {};
    if (platform && p.platform && p.platform !== platform) continue;
    const uploadedAt = Date.parse(a.uploadedDate);
    out.push({
      build: a.version,
      marketingVersion: p.version || null,
      platform: p.platform || platform || null,
      uploadedAt: Number.isNaN(uploadedAt) ? null : uploadedAt,
      processingState: a.processingState || null,
      expired: Boolean(a.expired),
    });
  }
  out.sort((x, y) => (y.uploadedAt || 0) - (x.uploadedAt || 0));
  return out;
}

export function maxApprovedMarketingVersion(appStoreVersionsJson) {
  let best = null;
  for (const row of appStoreVersionsJson?.data || []) {
    const a = row.attributes || {};
    const state = a.appStoreState || "";
    const ver = a.versionString || a.version || null;
    if (!ver || !APPROVED_STORE_STATES.has(state)) continue;
    if (!best || compareSemver(ver, best) > 0) best = ver;
  }
  return best;
}

export function auditBuildFormat(builds, appKey, platform) {
  const findings = [];
  for (const b of builds) {
    const build = String(b.build || "");
    if (!build) continue;
    if (!BUILD_NUMBER_RE.test(build)) {
      findings.push({
        type: FINDING.INVALID_BUILD_NUMBER,
        appKey,
        platform,
        build,
        marketingVersion: b.marketingVersion,
        uploadedAt: b.uploadedAt,
        detail: `build ${build} does not match ^20\\d{10}$`,
      });
    }
  }
  return findings;
}

export function auditMonotonic(builds, appKey, platform) {
  const findings = [];
  const rows = [...builds].filter((b) => b.uploadedAt != null && b.build);
  rows.sort((a, b) => (b.uploadedAt || 0) - (a.uploadedAt || 0));
  for (let i = 0; i < rows.length; i++) {
    const newer = rows[i];
    for (let j = i + 1; j < rows.length; j++) {
      const older = rows[j];
      if (buildNumberValue(newer.build) < buildNumberValue(older.build)) {
        findings.push({
          type: FINDING.NON_MONOTONIC,
          appKey,
          platform,
          newerBuild: newer.build,
          olderBuild: older.build,
          newerUploadedAt: newer.uploadedAt,
          olderUploadedAt: older.uploadedAt,
          detail:
            `newer upload ${ownerTime(newer.uploadedAt)} (build ${newer.build}) ` +
            `is below older upload ${ownerTime(older.uploadedAt)} (build ${older.build})`,
        });
      }
    }
  }
  return findings;
}

export function auditMarketing(builds, appKey, platform, approvedStoreVersion) {
  const expected = effectiveExpectedMarketing(appKey, approvedStoreVersion);
  const findings = [];
  const seen = new Set();
  for (const b of builds) {
    const mv = b.marketingVersion;
    if (!mv || seen.has(mv)) continue;
    seen.add(mv);
    if (compareSemver(mv, expected) !== 0) {
      findings.push({
        type: FINDING.MARKETING_DRIFT,
        appKey,
        platform,
        marketingVersion: mv,
        expected,
        approvedStoreVersion: approvedStoreVersion || null,
        detail: `marketing version ${mv} does not match expected ${expected}`,
      });
    }
  }
  return findings;
}

function newestOnTrain(rows, marketingVersion) {
  const train = (rows || []).filter((b) => b.marketingVersion === marketingVersion);
  train.sort((a, b) => (b.uploadedAt || 0) - (a.uploadedAt || 0));
  return train[0] || null;
}

/**
 * For one product group, compare iOS vs Mac build numbers.  With allHistory, every
 * marketing train is checked;  otherwise only the group's current marketing train.
 */
export function auditIosMacPair(
  groupKey,
  platformBuilds,
  { allHistory = false, schemeStartMs = FLEET_TIMESTAMP_SCHEME_START_MS } = {},
) {
  const findings = [];
  const ios = platformBuilds.IOS || [];
  const mac = platformBuilds.MAC_OS || [];
  if (!ios.length || !mac.length) return findings;

  if (allHistory) {
    const byMarketing = new Map();
    for (const platform of ["IOS", "MAC_OS"]) {
      for (const b of platformBuilds[platform] || []) {
        const mv = b.marketingVersion || "(unknown)";
        if (!byMarketing.has(mv)) byMarketing.set(mv, {});
        const slot = byMarketing.get(mv);
        if (!slot[platform] || (b.uploadedAt || 0) > (slot[platform].uploadedAt || 0)) {
          slot[platform] = b;
        }
      }
    }
    for (const [mv, slot] of byMarketing) {
      const i = slot.IOS;
      const m = slot.MAC_OS;
      if (!i || !m) continue;
      if (String(i.build) !== String(m.build)) {
        findings.push({
          type: FINDING.IOS_MAC_MISMATCH,
          groupKey,
          marketingVersion: mv,
          iosBuild: i.build,
          macBuild: m.build,
          iosUploadedAt: i.uploadedAt,
          macUploadedAt: m.uploadedAt,
          detail: `iOS build ${i.build} and Mac build ${m.build} differ for marketing ${mv}`,
        });
      }
    }
    return findings;
  }

  const combined = [...ios, ...mac].sort((a, b) => (b.uploadedAt || 0) - (a.uploadedAt || 0));
  const currentMv = combined[0]?.marketingVersion;
  if (!currentMv) return findings;

  const i = newestOnTrain(ios, currentMv);
  const m = newestOnTrain(mac, currentMv);
  if (!i || !m) return findings;
  const inStampEra =
    (i.uploadedAt != null && i.uploadedAt >= schemeStartMs) ||
    (m.uploadedAt != null && m.uploadedAt >= schemeStartMs);
  if (!inStampEra) return findings;

  if (String(i.build) !== String(m.build)) {
    findings.push({
      type: FINDING.IOS_MAC_MISMATCH,
      groupKey,
      marketingVersion: currentMv,
      iosBuild: i.build,
      macBuild: m.build,
      iosUploadedAt: i.uploadedAt,
      macUploadedAt: m.uploadedAt,
      detail: `iOS build ${i.build} and Mac build ${m.build} differ for marketing ${currentMv}`,
    });
  }
  return findings;
}

export function auditAppBuilds({
  appKey,
  platform,
  builds,
  approvedStoreVersion,
  allHistory = false,
  schemeStartMs = FLEET_TIMESTAMP_SCHEME_START_MS,
}) {
  const scoped = scopeBuildsForAudit(builds, { allHistory, schemeStartMs });
  return [
    ...auditBuildFormat(scoped.formatBuilds, appKey, platform),
    ...auditMonotonic(scoped.monotonicBuilds, appKey, platform),
    ...auditMarketing(scoped.marketingBuilds, appKey, platform, approvedStoreVersion),
  ];
}

export function exitCodeForReport(report, { strict = false } = {}) {
  if (report.errors?.length) return EXIT_FAILED;
  if (strict && (report.findings?.length || 0) > 0) return EXIT_STRICT;
  return EXIT_OK;
}

export function renderAuditTable(report) {
  const lines = [];
  lines.push(`Fleet build-number audit  (generated ${report.generatedAt})`);
  const scope = report.mode?.allHistory ? "all-history" : "current-only";
  lines.push(`Scope: ${scope};  apps checked: ${report.appsChecked};  findings: ${report.findings.length}`);
  if (!report.findings.length) {
    lines.push("No findings.");
  } else {
    lines.push("");
    lines.push("app".padEnd(18) + "platform".padEnd(10) + "type".padEnd(28) + "detail");
    lines.push("-".repeat(100));
    for (const f of report.findings) {
      const app = (f.appKey || f.groupKey || "-").slice(0, 17);
      const plat = (f.platform || "-").slice(0, 9);
      lines.push(`${app.padEnd(18)}${plat.padEnd(10)}${f.type.padEnd(28)}${f.detail}`);
    }
  }
  for (const e of report.errors || []) lines.push(`ERROR: ${e}`);
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Live ASC orchestration
// ---------------------------------------------------------------------------

const BUILDS_PATH = (appleId, limit) =>
  `/v1/builds?filter[app]=${appleId}&sort=-uploadedDate&limit=${limit}` +
  "&fields[builds]=version,uploadedDate,processingState,expired,preReleaseVersion" +
  "&include=preReleaseVersion&fields[preReleaseVersions]=version,platform";

const STORE_VERSIONS_PATH = (appleId) =>
  `/v1/apps/${appleId}/appStoreVersions?limit=20&fields[appStoreVersions]=versionString,appStoreState`;

async function fetchBuildsForApp(ctx, app, limit) {
  const platform = app.platform || "IOS";
  const json = await ctx.asc(BUILDS_PATH(app.appleId, limit));
  return normalizeAscBuilds(json, platform);
}

async function fetchApprovedVersion(ctx, appleId) {
  try {
    const json = await ctx.asc(STORE_VERSIONS_PATH(appleId));
    return maxApprovedMarketingVersion(json);
  } catch {
    return null;
  }
}

export async function runAudit(opts, deps, registry) {
  const now = deps.now();
  const errors = [];
  let keys = Object.keys(registry.apps);
  if (opts.apps) {
    const unknown = opts.apps.filter((k) => !registry.apps[k]);
    if (unknown.length) errors.push(`unknown app key(s): ${unknown.join(", ")}`);
    keys = keys.filter((k) => opts.apps.includes(k));
  }

  const ctx = makeContext(deps, registry);
  const findings = [];
  const perApp = [];
  const groupBuilds = new Map();

  for (const key of keys) {
    const app = registry.apps[key];
    const row = {
      key,
      name: app.displayName || key,
      platform: app.platform || "IOS",
      appleId: String(app.appleId || ""),
      builds: [],
      approvedStoreVersion: null,
      error: null,
    };
    try {
      const [builds, approved] = await Promise.all([
        fetchBuildsForApp(ctx, app, opts.limit || 30),
        fetchApprovedVersion(ctx, app.appleId),
      ]);
      row.builds = builds;
      row.approvedStoreVersion = approved;
      findings.push(
        ...auditAppBuilds({
          appKey: key,
          platform: row.platform,
          builds,
          approvedStoreVersion: approved,
          allHistory: opts.allHistory,
        }),
      );

      const g = productGroupKey(key);
      if (!groupBuilds.has(g)) groupBuilds.set(g, { IOS: [], MAC_OS: [] });
      const plat = row.platform === "MAC_OS" ? "MAC_OS" : "IOS";
      groupBuilds.get(g)[plat].push(...builds);
    } catch (err) {
      row.error = String(err.message || err);
      errors.push(`${key}: ${row.error}`);
    }
    perApp.push(row);
  }

  for (const [groupKey, platformBuilds] of groupBuilds) {
    findings.push(...auditIosMacPair(groupKey, platformBuilds, { allHistory: opts.allHistory }));
  }

  findings.sort((a, b) => `${a.type}:${a.appKey || a.groupKey}`.localeCompare(`${b.type}:${b.appKey || b.groupKey}`));

  return {
    schemaVersion: 1,
    generatedAt: now.toISOString(),
    mode: { allHistory: Boolean(opts.allHistory) },
    appsChecked: perApp.filter((a) => !a.error).length,
    apps: perApp,
    findings,
    errors,
  };
}

export async function main(argv = process.argv.slice(2), deps = makeDeps(), out = process.stdout) {
  let opts;
  try {
    opts = parseAuditArgs(argv);
  } catch (err) {
    process.stderr.write(`${err.message}\n`);
    return EXIT_FAILED;
  }
  if (opts.help) {
    out.write(
      "Usage: node build_number_audit.mjs [--json] [--strict] [--all-history] [--app keys] [--registry path] [--limit N]\n",
    );
    return EXIT_OK;
  }

  const regPath =
    opts.registry || resolveFleetFile("testflight-apps.json") || path.join(SCRIPT_DIR, "testflight-apps.json");
  let registry;
  try {
    registry = loadRegistry(regPath);
  } catch (err) {
    process.stderr.write(`${err.message}\n`);
    return EXIT_FAILED;
  }

  let report;
  try {
    report = await runAudit(opts, deps, registry);
  } catch (err) {
    process.stderr.write(`${err.message}\n`);
    return EXIT_FAILED;
  }

  const code = exitCodeForReport(report, { strict: opts.strict });
  report.exitCode = code;

  if (opts.json) out.write(`${JSON.stringify(report, null, 2)}\n`);
  else out.write(`${renderAuditTable(report)}\n`);

  return code;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().then((c) => process.exit(c));
}
