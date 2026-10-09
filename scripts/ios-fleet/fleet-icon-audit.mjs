#!/usr/bin/env node
/**
 * fleet-icon-audit.mjs — the standing check that stops fleet icons from rotting.
 *
 * WHY THIS EXISTS
 * Owner, 2026-10-02: "so many apps have outdated icons or no icon at all ... it drives
 * me crazy and never seems to get better."  Nothing was ever checking, so drift was
 * invisible and unrepeatable-to-catch.  This makes drift a hard, reportable failure.
 *
 * WHY THE REPO IS THE ONLY SOURCE
 * The App Store Connect API exposes NO app icon asset (appInfos relationships are
 * ageRatingDeclaration / appInfoLocalizations / categories only — verified 2026-10-02).
 * And for a TestFlight build the icon the tester sees is baked into the uploaded
 * BINARY, not read from App Store Connect.  So the repo's asset catalog is the truth,
 * and the repo is what we audit.
 *
 * SCAN HYGIENE
 * Vendor checkouts live inside repo working trees (for example ContactLogo/build/
 * derivedData/SourcePackages/checkouts/sentry-cocoa/Samples/...).  A naive recursive
 * glob surfaces ~10 phantom AppIcon sets from Sentry's sample apps.  We prune those.
 *
 * BASELINE
 * icon-lock.json records the approved hash per app.  First run creates it (baseline).
 * Later runs report DRIFT when the shipped icon hash changes without the lock being
 * deliberately re-approved with --approve.
 *
 * Usage:
 *   node fleet-icon-audit.mjs              # audit, write report, exit 1 on errors
 *   node fleet-icon-audit.mjs --json       # print the report JSON to stdout too
 *   node fleet-icon-audit.mjs --approve    # accept current state as the new baseline
 *   node fleet-icon-audit.mjs --no-lanes   # skip lane-worktree comparison (faster)
 */

import { readFileSync, writeFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, basename, extname } from "node:path";
import { execFileSync } from "node:child_process";
import crypto from "node:crypto";

const IOS_FLEET = "/Users/jay/apps/ios-fleet";
const APPS_JSON = join(IOS_FLEET, "apps.json");
const LOCK_PATH = join(IOS_FLEET, "icon-lock.json");
const REPORT_PATH = join(IOS_FLEET, "artifacts", "icon-audit.json");

const args = process.argv.slice(2);
const APPROVE = args.includes("--approve");
const JSON_OUT = args.includes("--json");
const NO_LANES = args.includes("--no-lanes");

/** Directories that never contain first-party fleet icon assets. */
const PRUNE_DIRS = new Set([
  "build", "DerivedData", "derivedData", ".build", "Pods", "node_modules", ".git",
  "SourcePackages", "checkouts", "Carthage", "vendor", "artifacts", ".buildkite",
  "dist", "out", "coverage", ".swiftpm", "Packages", "Target", ".venv", "__pycache__"
]);

/** appKey -> canonical repo root. Resolved from disk, not trusted from config. */
const REPO_CANDIDATES = {
  socratic: ["Code/Socratic-Trade", "Code/Socratic.Trade"],
  congress: ["Code/Congress.Trade"],
  usage: ["Code/Usage-Monitor"],
  "usage-local": ["Code/Usage-Monitor"],
  dealdex: ["Code/DealDex"],
  autorotate: ["Code/Autorotate"],
  "autorotate-mac": ["Code/Autorotate"],
  contactlogo: ["Code/ContactLogo"],
  "contactlogo-mac": ["Code/ContactLogo"],
  botfleet: ["Code/BotFleet"],
  "botfleet-mac": ["Code/BotFleet"],
  codecaps: ["Code/CodeCaps"],
  "codecaps-mac": ["Code/CodeCaps"],
  hoghunter: ["Code/HogHunter"],
  "hoghunter-mac": ["Code/HogHunter"],
  clutch: ["Code/Clutch"],
  fleetlink: ["Code/FleetLink"]
};

const MAC_KEYS = new Set(["autorotate-mac", "contactlogo-mac", "botfleet-mac", "codecaps-mac", "hoghunter-mac"]);

function shortHash(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex").slice(0, 12);
}

function safeStat(p) {
  try { return statSync(p); } catch { return null; }
}

/** Recursively find *.appiconset dirs, pruning vendor/build trees. */
function findIconSets(root, maxDepth = 9) {
  const found = [];
  function walk(dir, depth) {
    if (depth > maxDepth) return;
    let entries;
    try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (!e.isDirectory()) continue;
      const name = e.name;
      if (PRUNE_DIRS.has(name)) continue;
      // Hidden dirs hold agent worktrees and caches (e.g. .claude/worktrees/<name>/<repo>),
      // which duplicate the canonical tree and double-count the same icon.
      if (name.startsWith(".")) continue;
      const full = join(dir, name);
      if (name.endsWith(".appiconset")) { found.push(full); continue; }
      walk(full, depth + 1);
    }
  }
  walk(root, 0);
  return found;
}

/** Parse Contents.json; tolerate the two legal shapes (dict-of-images, list-of-images). */
function readContents(iconSetDir) {
  const p = join(iconSetDir, "Contents.json");
  if (!existsSync(p)) return null;
  let raw;
  try { raw = JSON.parse(readFileSync(p, "utf8")); } catch { return null; }
  const images = Array.isArray(raw.images) ? raw.images : Object.values(raw.images || {});
  const referenced = [];
  for (const img of images) {
    const f = img && img.filename;
    if (typeof f === "string" && f.trim()) referenced.push(f.trim());
  }
  const present = new Set();
  for (const e of readdirSync(iconSetDir, { withFileTypes: true })) {
    if (e.isFile() && extname(e.name).toLowerCase() === ".png") present.add(e.name);
  }
  const missing = referenced.filter(f => !present.has(f));
  const orphans = [...present].filter(f => !referenced.includes(f));
  return { referenced, present: [...present], missing, orphans };
}

/** The 1024 marketing/master icon inside a set, if the set declares one. */
function pickMaster(iconSetDir) {
  const c = readContents(iconSetDir);
  if (!c) return null;
  const p = join(iconSetDir, "Contents.json");
  const raw = JSON.parse(readFileSync(p, "utf8"));
  const images = Array.isArray(raw.images) ? raw.images : Object.values(raw.images || {});
  let best = null;
  for (const img of images) {
    const f = img && img.filename;
    if (typeof f !== "string" || !f.trim()) continue;
    const idiom = img.idiom || "";
    const size = String(img.size || "");
    // Xcode omits `scale` entirely for single-size universal slots (e.g. HogHunter's
    // {idiom: universal, platform: ios, size: 1024x1024}).  Treat absent as 1x, or the
    // real 1024 silently goes unexamined.
    const scale = img.scale === undefined || img.scale === null ? "1x" : img.scale;
    if (/1024/.test(size) && scale === "1x") {
      if (!best) best = img;
      if (idiom === "ios-marketing") return img;
    }
  }
  return best;
}

/**
 * Is this an iOS set or a macOS set?
 * macOS App Store icons legitimately carry an alpha channel (rounded-rect, vibrancy),
 * so the ITMS-90717 alpha rule must only ever be applied to the iOS 1024 marketing icon.
 */
function setPlatform(iconSetDir, entry) {
  const p = entry?.platform;
  if (typeof p === "string" && p.length) return p.toLowerCase();
  const id = entry?.idiom || "";
  if (id.startsWith("mac")) return "macos";
  if (id.startsWith("ios")) return "ios";
  if (/macos|mac[-_ ]?(os|app)/i.test(iconSetDir)) return "macos";
  if (/\bios\b|iphone|ipad/i.test(iconSetDir)) return "ios";
  return "unknown";
}

/**
 * Does the PNG carry an alpha channel, and are any pixels ACTUALLY transparent?
 *
 * Reading the colour-type byte is only a hint. A PNG can be RGBA (colour type 6) and
 * still be 100% opaque — Autorotate's iOS 1024 is exactly that. Reporting that as
 * "Apple rejects this" is a false positive, and a guard that cries wolf on a healthy
 * file is worse than no guard because people learn to ignore it.
 *
 * So: colour type decides WHETHER an alpha channel exists; Pillow decides whether any
 * pixel is actually see-through, and that is what sets the severity. If Pillow is
 * unavailable we say "unverified" instead of guessing.
 */
function alphaProfile(path) {
  let colourType = null;
  try {
    const buf = readFileSync(path);
    if (buf.slice(0, 8).toString("binary") === "\x89PNG\r\n\x1a\n") colourType = buf[25];
  } catch { /* unreadable */ }
  const hasChannel = colourType === 4 || colourType === 6;
  if (!hasChannel) return { hasChannel: false, transparentPct: 0, verified: true };

  let transparentPct = null;
  let verified = false;
  try {
    const py = `from PIL import Image;im=Image.open(${JSON.stringify(path)});` +
      `a=im.convert("RGBA").getchannel("A");h=a.histogram();` +
      `t=sum(h[:255]);print(f"{100.0*t/(im.width*im.height):.4f}")`;
    const out = execFileSync("python3", ["-c", py], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    transparentPct = parseFloat(out);
    verified = Number.isFinite(transparentPct);
  } catch { /* Pillow missing — stay honest about it */ }

  return { hasChannel, transparentPct, verified };
}

function gitInfo(repoRoot) {
  const inside = safeStat(join(repoRoot, ".git"));
  if (!inside) return null;
  try {
    const branch = execFileSync("git", ["-C", repoRoot, "rev-parse", "--abbrev-ref", "HEAD"], { encoding: "utf8" }).trim();
    const commit = execFileSync("git", ["-C", repoRoot, "rev-parse", "--short", "HEAD"], { encoding: "utf8" }).trim();
    const lastCommit = execFileSync("git", ["-C", repoRoot, "log", "-1", "--format=%ad", "--date=short"], { encoding: "utf8" }).trim();
    return { branch, commit, lastCommit };
  } catch { return null; }
}

/** Hash the tracked icon blobs on a given git ref, so lanes can be compared to main. */
function iconHashesAtRef(repoRoot, ref) {
  try {
    const files = execFileSync(
      "git",
      ["-C", repoRoot, "ls-tree", "-r", "--format=%(objectname) %(path)", ref],
      { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }
    );
  } catch {
    try {
      const files = execFileSync("git", ["-C", repoRoot, "ls-tree", "-r", ref], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
      return parseLsTree(files);
    } catch { return {}; }
  }
  return parseLsTree(files);
}

function parseLsTree(out) {
  const map = {};
  for (const line of out.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    const sp = t.indexOf(" ");
    if (sp === -1) continue;
    const rest = t.slice(sp + 1);
    // path is last tab-separated token
    const tab = rest.indexOf("\t");
    const path = (tab === -1 ? rest : rest.slice(tab + 1)).replace(/^\"|"$/g, "");
    if (path.includes(".appiconset/") && /\.png$/i.test(path)) {
      map[path] = rest.slice(0, tab === -1 ? rest.length : tab);
    }
  }
  return map;
}

function listLaneWorktrees(appKey, canonicalRoot) {
  // Lanes live at ~/apps/lanes/<Repo>/<seat>-<slug> (layout v2, docs/protocols/lane-map.md), or still in an older
  // place (~/apps/lanes/<prefix>/..., a flat ~/apps/<prefix>-<seat>).  Guessing folder names misses every nested
  // lane, so ask git for the canonical tree's worktrees and keep the ones under ~/apps.
  const lanes = [];
  let listing = "";
  try {
    listing = execFileSync("git", ["-C", canonicalRoot, "worktree", "list", "--porcelain"], { encoding: "utf8" });
  } catch { return lanes; }
  const canonicalIconPaths = iconHashesAtRef(canonicalRoot, "HEAD");
  const canonPaths = Object.keys(canonicalIconPaths);
  if (canonPaths.length === 0) return lanes;
  const worktrees = listing.split("\n").filter(l => l.startsWith("worktree ")).map(l => l.slice("worktree ".length));
  for (const p of worktrees) {
    if (!p.startsWith("/Users/jay/apps/")) continue;
    if (p === canonicalRoot) continue;
    if (!existsSync(join(p, ".git"))) continue;
    let headHashes;
    try { headHashes = iconHashesAtRef(p, "HEAD"); } catch { continue; }
    const headPaths = Object.keys(headHashes);
    if (headPaths.length === 0) continue;
    const shared = headPaths.filter(x => canonPaths.includes(x));
    if (shared.length === 0) continue;
    const drifted = shared.filter(x => headHashes[x] !== canonicalIconPaths[x]);
    if (drifted.length > 0) {
      let branch = "?", merged = "no";
      try { branch = execFileSync("git", ["-C", p, "rev-parse", "--abbrev-ref", "HEAD"], { encoding: "utf8" }).trim(); } catch {}
      try {
        const r = execFileSync("git", ["-C", canonicalRoot, "branch", "--contains", branch], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
        if (r.trim().length > 0) merged = "yes";
      } catch {}
      lanes.push({ lane: p, branch, mergedIntoCanonical: merged, driftedFiles: drifted });
    }
  }
  return lanes;
}

// ---------------------------------------------------------------------------

const findings = [];
const add = (level, appKey, code, message, detail) =>
  findings.push({ level, appKey, code, message, detail: detail || null });

const appsData = JSON.parse(readFileSync(APPS_JSON, "utf8"));
const lock = existsSync(LOCK_PATH) ? JSON.parse(readFileSync(LOCK_PATH, "utf8")) : { approved: {}, generatedAt: null };

const perApp = {};
const appsRoot = "/Users/jay";

for (const [key, conf] of Object.entries(appsData.apps)) {
  const candidates = REPO_CANDIDATES[key] || [];
  const repoRoot = candidates.map(c => join(appsRoot, c)).find(p => existsSync(p)) || null;

  if (!repoRoot) {
    add("error", key, "REPO_MISSING", `No canonical repo found for '${key}'`, `tried: ${candidates.join(", ")}`);
    perApp[key] = { repoRoot: null, sets: [], error: "repo not found" };
    continue;
  }

  const sets = findIconSets(repoRoot);
  const setReports = [];
  let approvedHash = null;
  let masterPath = null;

  for (const setDir of sets) {
    const rel = setDir.replace(`${repoRoot}/`, "");
    const contents = readContents(setDir);
    const masterEntry = pickMaster(setDir);
    const master = masterEntry ? String(masterEntry.filename).trim() : null;
    const masterFile = master ? join(setDir, master) : null;
    const platform = setPlatform(setDir, masterEntry);

    let alpha = null;
    if (masterFile && existsSync(masterFile)) alpha = alphaProfile(masterFile);
    const genuinelyTransparent = alpha && alpha.transparentPct !== null && alpha.transparentPct > 0.01;
    const unverified = alpha && alpha.hasChannel && !alpha.verified;

    let hash = null;
    if (masterFile && existsSync(masterFile)) {
      hash = shortHash(readFileSync(masterFile));
      if (!approvedHash && platform !== "macos") { approvedHash = hash; masterPath = masterFile.replace(`${repoRoot}/`, ""); }
    }

    setReports.push({
      path: rel,
      platform,
      declaredSlots: contents ? contents.referenced.length : 0,
      pngFilesOnDisk: contents ? contents.present.length : 0,
      referencedButMissing: contents ? contents.missing : ["<Contents.json unreadable>"],
      orphanFiles: contents ? contents.orphans : [],
      masterFile: master || null,
      masterHash: hash,
      masterHasAlphaChannel: alpha ? alpha.hasChannel : null,
      masterTransparentPct: alpha ? alpha.transparentPct : null,
      masterAlphaVerified: alpha ? alpha.verified : null
    });

    if (!contents) {
      add("error", key, "CONTENTS_UNREADABLE", `Cannot read Contents.json in ${rel}`, "App build will fail or ship no icon");
      continue;
    }
    if (contents.missing.length > 0) {
      add("error", key, "ICON_FILE_MISSING", `${rel} references ${contents.missing.length} icon file(s) that are not on disk`, contents.missing.join(", "));
    }
    if (contents.referenced.length === 0 && contents.present.length === 0) {
      add("warn", key, "ICON_SET_EMPTY", `${rel} declares no icon files at all`, null);
    }
    // Severity is driven by ACTUAL transparency, not by the presence of an alpha channel.
    // Only the iOS 1024 marketing icon is submitted to App Store Connect; macOS app
    // icons are REQUIRED to carry alpha (rounded rect + vibrancy).
    if (platform === "ios") {
      if (genuinelyTransparent) {
        // Report only.  Do NOT recommend a mechanical flatten: see rules ICO-6/ICO-7/ICO-8.
        // The correct fix is the correct ART in the slot, supplied by the owner or the
        // seat the owner assigned — and menu bar art must never be flattened into an
        // app icon slot.  An audit that tells a seat to synthesise art is how icons
        // drift, which is the exact problem this audit exists to stop.
        add("error", key, "ICON_TRANSPARENT", `${rel} iOS 1024 marketing icon is ${alpha.transparentPct}% transparent`, "Apple rejects this as ITMS-90717. REPORT ONLY — do not auto-flatten and do not substitute other art. See rules ICO-6 (menu bar art is not app art) and ICO-7 (owner or assigned seat supplies art).");
      } else if (unverified) {
        add("warn", key, "ICON_ALPHA_UNVERIFIED", `${rel} carries an alpha channel but Pillow was unavailable to measure it`, "Install Pillow to make this check conclusive");
      } else if (alpha && alpha.hasChannel) {
        add("warn", key, "ICON_ALPHA_CHANNEL", `${rel} iOS icon has an alpha channel that is fully opaque (0% transparent)`, "Harmless today; it is still a valid app icon. No action needed unless the owner asks.");
      }
    }
  }

  if (sets.length === 0) {
    add("error", key, "NO_ICON_SET", `No AppIcon.appiconset found in ${repoRoot}`, null);
  }

  if (MAC_KEYS.has(key) && sets.length === 0) {
    add("error", key, "MAC_ICON_MISSING", "macOS target has no AppIcon set anywhere in the repo", repoRoot);
  }

  // Baseline comparison
  const prev = lock.approved?.[key];
  let driftStatus = "NEW";
  if (prev && approvedHash) driftStatus = prev === approvedHash ? "MATCH" : "DRIFT";
  else if (prev && !approvedHash) driftStatus = "MISSING";
  if (prev && driftStatus === "DRIFT" && !APPROVE) {
    add("warn", key, "ICON_DRIFT", `Approved icon hash ${prev} no longer matches shipped ${approvedHash}`, `master: ${masterPath}`);
  }

  let lanes = [];
  if (!NO_LANES) {
    try { lanes = listLaneWorktrees(key, repoRoot); } catch {}
    for (const l of lanes) {
      add("warn", key, "LANE_ICON_DRIFT", `Lane ${l.branch} carries a different icon than canonical ${repoRoot}`, `${l.driftedFiles.length} file(s): ${l.driftedFiles.slice(0, 3).join(", ")}${l.driftedFiles.length > 3 ? " ..." : ""} (merged: ${l.mergedIntoCanonical})`);
    }
  }

  perApp[key] = {
    repoRoot: repoRoot.replace(`${appsRoot}/`, ""),
    git: gitInfo(repoRoot),
    setCount: sets.length,
    approvedHash: prev || null,
    shippedHash: approvedHash,
    driftStatus,
    master: masterPath,
    sets: setReports,
    lanes
  };
}

const counts = findings.reduce((a, f) => { a[f.level] = (a[f.level] || 0) + 1; return a; }, {});
const report = {
  generatedAt: new Date().toISOString(),
  appsAudited: Object.keys(perApp).length,
  counts,
  findings,
  apps: perApp
};

writeFileSync(REPORT_PATH, JSON.stringify(report, null, 2));

if (APPROVE) {
  const approved = {};
  for (const [key, v] of Object.entries(perApp)) {
    if (v.shippedHash) approved[key] = v.shippedHash;
  }
  writeFileSync(LOCK_PATH, JSON.stringify({ generatedAt: new Date().toISOString(), approved }, null, 2));
  console.log(`✅ Baseline approved: ${Object.keys(approved).length} app icon hash(es) recorded in ${LOCK_PATH}`);
}

// ---- human summary ----
console.log(`\nFleet icon audit — ${report.appsAudited} apps, ${findings.length} finding(s)`);
console.log(`  errors: ${counts.error || 0}   warnings: ${counts.warn || 0}`);
console.log("");
for (const [key, v] of Object.entries(perApp)) {
  const status = v.sets?.length ? (v.driftStatus === "MATCH" ? "OK " : v.driftStatus) : "FAIL";
  console.log(`  [${status.padEnd(7)}] ${key.padEnd(18)} sets=${v.setCount ?? 0}  ${v.repoRoot || "(repo missing)"}`);
}
if (findings.length) {
  console.log("\nFindings:");
  for (const f of findings) {
    console.log(`  ${f.level.toUpperCase().padEnd(5)} ${f.appKey.padEnd(16)} ${f.code}: ${f.message}${f.detail ? `\n        → ${f.detail}` : ""}`);
  }
}
console.log(`\nReport: ${REPORT_PATH}`);

if (JSON_OUT) console.log(JSON.stringify(report, null, 2));

process.exit((counts.error || 0) > 0 ? 1 : 0);
