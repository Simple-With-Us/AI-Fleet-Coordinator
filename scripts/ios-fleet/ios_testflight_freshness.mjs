#!/usr/bin/env node
/**
 * ios_testflight_freshness.mjs - the fleet's TestFlight deploy check.
 *
 * Owner request 2026-10-17: "add deploy checks and make them update
 * automatically in future".  For every app in testflight-apps.json it answers
 * one question:  does the newest processed TestFlight build contain the newest
 * main commit that touches the app's native tree?
 *
 *   node ios_testflight_freshness.mjs                  table for every app
 *   node ios_testflight_freshness.mjs --app botfleet   one app (post-merge deploy check)
 *   node ios_testflight_freshness.mjs --repo BotFleet  every app a repo ships
 *   node ios_testflight_freshness.mjs --json           machine readable report
 *   node ios_testflight_freshness.mjs --ship-behind --dry-run   show the catch-up plan
 *   node ios_testflight_freshness.mjs --ship-behind --alert     what the LaunchAgent runs
 *
 * Exit codes:  0 every checked app is current;  1 something is behind (or a
 * catch-up ship is still running or processing);  2 the check itself failed.
 *
 * HOW "SHIPPED" IS DECIDED.  App Store Connect never records a git sha.  The
 * build number is the repo commit count, so a current build carries no cut
 * time and the ship workflow run whose start-to-end window contains the
 * UPLOAD is the run that uploaded it;  that run's head_sha is the shipped
 * source.  A green ship run is not an upload (the gate skips most runs), and
 * the run that really uploaded can end red (BotFleet 1.0.113 came from a run
 * that failed after the upload), so the conclusion is ignored.  Builds
 * uploaded under the retired scheme still carry a 12-digit UTC stamp, and
 * those are correlated by their cut time instead.  Fallbacks, labelled in the
 * output:  the local ship state file ~/.cache/ios-fleet/last-ship-<app>.txt,
 * then upload time against commit time.  Containment uses the compare API:
 * ahead or identical means the shipped sha contains the newest native commit.
 *
 * THE REMEDY (--ship-behind) dispatches the app's OWN ship workflow with
 * `gh workflow run`, never a local xcodebuild, and only when every guard holds:
 * autoRemedy is on and the workflow is active;  the iOS compile check on main
 * is green;  2.5 hours have passed since the app's last upload (the documented
 * fleet gate, enforced here because ContactLogo does not use ship-testflight.sh;
 * Hog Hunter does (HH-157), and its registry paths are the exact files its
 * workflow ships on);  this app has not already been dispatched for this main
 * HEAD (so a ship that keeps failing is tried once, not every two hours);
 * build-window.sh does not say "background";  no fleet ship run is in flight;
 * and at most --max-ships (default 1) per run.  It never passes --force-ship
 * or any gate override, and never runs an App Store GM workflow.
 *
 * ALERTS (--alert) go to Zulip #Work-In-Progress, topic "AFC testflight-freshness",
 * through the simple-chat CLI:  an app still behind six hours after the check
 * first saw it behind (so the catch-up ship has had its chance, or could not
 * run), or the check itself failing.  One digest post per run, at most one
 * post per app per six hours, and never twice for the same commit and reason.
 *
 * Credentials:  this file never reads one.  App Store Connect calls go through
 * the fleet's asc-api.mjs (which reads the ASC_* env or Infisical /asc through fleet-secrets), and
 * its stderr is never echoed, because a crash there can name the key path.
 * GitHub calls go through the gh CLI;  Zulip through simple-chat.
 */
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const EXIT_CURRENT = 0;
export const EXIT_BEHIND = 1;
export const EXIT_FAILED = 2;
export const SENTENCE_GAP = "  ";
export const ALERT_TOPIC = "AFC testflight-freshness";
export const ALERT_CHANNEL = "Work-In-Progress";
const HOUR = 3600e3;
const MINUTE = 60e3;
const NOT_CURRENT = new Set(["behind", "no-build", "expired", "shipping", "processing"]);
const REMEDIABLE = new Set(["behind", "no-build", "expired"]);
const ALERTABLE = new Set(["behind", "no-build", "expired"]);
const IN_FLIGHT = new Set(["queued", "in_progress", "waiting", "requested", "pending"]);
// A ship job times out at 90 minutes (the longest timeout-minutes among the fleet's ship
// workflows), so a run still queued or running after 3 hours is stuck:  it stops counting
// as "shipping" and stops holding back every other app's catch-up.  Registry key:
// inflightMaxHours.
const DEFAULT_INFLIGHT_MAX_HOURS = 3;
// A dispatched catch-up gets this long to archive, upload and process before an alert
// may say it did not land.  Registry key:  remedyLandHours.
const DEFAULT_REMEDY_LAND_HOURS = 3;

/** The longest-running entry, so a message quotes one run's age and link together. */
export function oldestRun(runs) {
  return (runs || []).reduce((a, b) => (b.ageHours > a.ageHours ? b : a));
}

/** Split a workflow's queued or running runs into live ones and stuck ones. */
export function splitInflight(runs, nowMs, maxHours = DEFAULT_INFLIGHT_MAX_HOURS) {
  const live = [];
  const stale = [];
  for (const x of runs || []) {
    if (!IN_FLIGHT.has(x.status)) continue;
    const since = Date.parse(x.created_at || x.run_started_at);
    const ageHours = Number.isNaN(since) ? 0 : round1(Math.max(0, nowMs - since) / HOUR);
    const row = { id: x.id, sha: x.head_sha, status: x.status, event: x.event, url: x.html_url, ageHours };
    (ageHours >= maxHours ? stale : live).push(row);
  }
  return { live, stale };
}

// ---------------------------------------------------------------------------
// Small pure helpers (exported for tests)
// ---------------------------------------------------------------------------

/**
 * "202610171703" -> Date (UTC).  null when it is not a plausible cut time.
 *
 * The build number is the repo commit count (owner 2026-10-18), so a current
 * build carries no cut time and this returns null for it.  It still resolves
 * the 12-digit UTC stamp of a build uploaded under the retired scheme, which
 * is what lets those older builds be correlated by their cut time.
 */
export function buildCutTime(buildNumber, uploadedAt) {
  const m = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})$/.exec(String(buildNumber || ""));
  if (!m) return null;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  if (Number.isNaN(t)) return null;
  if (uploadedAt != null) {
    // A cut time after the upload, or more than 6h before it, is not a cut time.
    if (t > uploadedAt + 5 * MINUTE || t < uploadedAt - 6 * HOUR) return null;
  }
  return t;
}

/** The ship run whose window contains the build's cut time. */
export function correlateRun(runs, cut, uploaded) {
  const anchor = cut ?? uploaded;
  const hits = (runs || []).filter((run) => {
    const start = Date.parse(run.run_started_at || run.created_at);
    const end = Date.parse(run.updated_at || run.run_started_at || run.created_at);
    if (Number.isNaN(start) || Number.isNaN(end)) return false;
    return start - 2 * MINUTE <= anchor && start <= uploaded && uploaded <= end + 20 * MINUTE;
  });
  hits.sort((a, b) => Date.parse(b.run_started_at || b.created_at) - Date.parse(a.run_started_at || a.created_at));
  return hits[0] || null;
}

/** Exact check-run name, or a prefix when the pattern ends in "*". */
export function matchCheckName(pattern, name) {
  if (!pattern || !name) return false;
  if (pattern.endsWith("*")) return name.startsWith(pattern.slice(0, -1));
  return name === pattern;
}

/** The owner's clock:  "Sat, Oct 17, 7:15pm", Central, no zone abbreviation. */
export function ownerTime(value) {
  if (value == null) return "-";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "-";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago", weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true,
  }).formatToParts(d);
  const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
  return `${get("weekday")}, ${get("month")} ${get("day")}, ${get("hour")}:${get("minute")}${get("dayPeriod").toLowerCase()}`;
}

/** Join sentences with the fleet's rendered sentence gap (U+00A0 plus a space). */
export function sentences(...parts) {
  return parts.filter(Boolean).map((s) => String(s).trim()).join(SENTENCE_GAP);
}

export function short(sha) {
  return sha ? String(sha).slice(0, 7) : "-";
}

function round1(n) {
  return Math.round(n * 10) / 10;
}

/** Overall exit code from per-app results and global errors. */
export function exitCodeFor(results, errors = []) {
  if (errors.length || results.some((r) => r.status === "error")) return EXIT_FAILED;
  if (results.some((r) => NOT_CURRENT.has(r.status))) return EXIT_BEHIND;
  return EXIT_CURRENT;
}

export function summarize(results) {
  const s = { current: 0, behind: 0, shipping: 0, processing: 0, "no-build": 0, expired: 0, skipped: 0, error: 0 };
  for (const r of results) s[r.status] = (s[r.status] || 0) + 1;
  return s;
}

async function pool(items, size, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, worker));
  return out;
}

// ---------------------------------------------------------------------------
// I/O layer (replaced by stubs in tests)
// ---------------------------------------------------------------------------

export function resolveFleetFile(name, env = process.env) {
  const candidates = [];
  if (env.IOS_FLEET_DIR) candidates.push(path.join(env.IOS_FLEET_DIR, name));
  candidates.push(path.join(SCRIPT_DIR, name));
  candidates.push(path.join(os.homedir(), "apps", "ios-fleet", name));
  return candidates.find((p) => fs.existsSync(p)) || null;
}

export function stateDir(env = process.env) {
  return env.IOS_FLEET_STATE_DIR || path.join(os.homedir(), ".cache", "ios-fleet");
}

function runCmd(cmd, args, { timeout = 90_000, env = process.env } = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { encoding: "utf8", maxBuffer: 64 << 20, timeout, env }, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
      } else {
        resolve(stdout);
      }
    });
  });
}

/** First stderr line of a failed gh call (gh never prints credentials). */
function ghError(err) {
  const line = String(err.stderr || "").split("\n").map((l) => l.trim()).find(Boolean) || "";
  return (line || `exit ${err.code ?? "?"}`).slice(0, 200);
}

// ---------------------------------------------------------------------------
// App Store Connect calls
//
// Every asc-api.mjs call signs in to Infisical through fleet-secrets (three `get`s, each a
// login plus an export), so one call is a handful of process launches.  On 2026-10-20 the Mac
// was memory-thrashing (load average 100 to 200, kernel_task near 50% system time) and the
// 2:20pm and 4:20pm runs lost every call to the 120 second kill timeout:  Node reports a killed
// child as `code: null`, which the report printed as "asc-api.mjs exit ?" with nothing to say
// the child never finished.  So:  at most ASC_MAX_INFLIGHT children at once (the pool of apps
// plus the unmapped-app lookup used to start five), one retry of a call that was killed, a
// fail-fast after repeated kills (a thrashing host will not recover inside this run), and a
// message that names the signal, the time and the host load.
// ---------------------------------------------------------------------------

export const ASC_TIMEOUT_MS = 120_000;
export const ASC_MAX_INFLIGHT = 2;
export const ASC_TRIP_AFTER = 3;

/** A counting gate:  run(fn) starts fn once fewer than `limit` calls are running. */
export function makeGate(limit) {
  let running = 0;
  const waiting = [];
  const release = () => {
    running--;
    const next = waiting.shift();
    if (next) next();
  };
  return async function run(fn) {
    if (running >= limit) await new Promise((resolve) => waiting.push(resolve));
    running++;
    try {
      return await fn();
    } finally {
      release();
    }
  };
}

export function makeAscGuard({ limit = ASC_MAX_INFLIGHT, tripAfter = ASC_TRIP_AFTER, load = () => os.loadavg()[0] } = {}) {
  return { gate: makeGate(limit), killed: 0, tripAfter, load };
}

/** True when the child never exited on its own:  killed by the timeout or by another signal. */
export function wasKilled(err) {
  return Boolean(err) && (err.killed === true || Boolean(err.signal));
}

/** One line for a failed asc-api call.  Never echoes asc-api stderr beyond an HTTP status. */
export function ascFailureText(err, apiPath, { timeoutMs = ASC_TIMEOUT_MS, load = null } = {}) {
  const endpoint = apiPath.split("?")[0];
  const http = /HTTP (\d{3})/.exec(String(err.stderr || ""));
  let code = "";
  try {
    const j = JSON.parse(String(err.stdout || "").slice(String(err.stdout || "").indexOf("{")));
    code = j?.errors?.[0]?.code ? ` ${j.errors[0].code}` : "";
  } catch { /* not JSON */ }
  if (http) return `App Store Connect ${endpoint}: HTTP ${http[1]}${code}`;
  if (wasKilled(err)) {
    const how = err.signal ? `killed by ${err.signal}` : "killed";
    const lo = load === null ? "" : `, host load ${Math.round(load)}`;
    return `App Store Connect ${endpoint}: asc-api.mjs ${how} after the ${Math.round(timeoutMs / 1000)}s timeout${lo}`;
  }
  return `App Store Connect ${endpoint}: asc-api.mjs exit ${err.code ?? "?"}${code}`;
}

/**
 * GET one App Store Connect path through `run` (which spawns asc-api.mjs and takes
 * {timeout}).  Resolves the parsed JSON.  `guard` comes from makeAscGuard and is shared by
 * every call in the run.
 */
export async function ascRequest(run, apiPath, guard = makeAscGuard(), { timeoutMs = ASC_TIMEOUT_MS } = {}) {
  return guard.gate(async () => {
    let lastErr = null;
    for (let attempt = 1; attempt <= 2; attempt++) {
      if (guard.killed >= guard.tripAfter) {
        throw new Error(`App Store Connect ${apiPath.split("?")[0]}: skipped after ${guard.killed} asc-api.mjs calls were killed by the timeout (host load ${Math.round(guard.load())})`);
      }
      try {
        const out = await run({ timeout: timeoutMs });
        const i = out.indexOf("{");
        if (i < 0) throw new Error("App Store Connect returned no JSON");
        guard.killed = 0;
        return JSON.parse(out.slice(i));
      } catch (err) {
        lastErr = err;
        if (!wasKilled(err)) break;
        guard.killed++;
      }
    }
    throw new Error(ascFailureText(lastErr, apiPath, { timeoutMs, load: guard.load() }));
  });
}

export function makeDeps(env = process.env) {
  const sdir = stateDir(env);
  const ascGuard = makeAscGuard();
  const deps = {
    now: () => new Date(),
    async gh(apiPath) {
      try {
        const out = await runCmd("gh", ["api", "-X", "GET", apiPath], { env });
        return JSON.parse(out);
      } catch (err) {
        const e = new Error(`gh api ${apiPath.split("?")[0]}: ${ghError(err)}`);
        e.notFound = /HTTP 404|Not Found/.test(String(err.stderr || ""));
        throw e;
      }
    },
    async ghDispatch(repoFull, workflow, ref, inputs) {
      const args = ["workflow", "run", workflow, "--repo", repoFull, "--ref", ref];
      for (const [k, v] of Object.entries(inputs || {})) args.push("-f", `${k}=${v}`);
      try {
        await runCmd("gh", args, { env });
      } catch (err) {
        throw new Error(`gh workflow run ${workflow}: ${ghError(err)}`);
      }
    },
    async asc(apiPath) {
      const script = resolveFleetFile("asc-api.mjs", env);
      if (!script) throw new Error("asc-api.mjs not found (set IOS_FLEET_DIR or install ~/apps/ios-fleet)");
      return ascRequest((opts) => runCmd(process.execPath, [script, "GET", apiPath], { env, ...opts }), apiPath, ascGuard);
    },
    async buildWindow(appKey) {
      const script = resolveFleetFile("build-window.sh", env);
      if (!script) return "normal";
      try {
        const out = await runCmd("bash", [script, appKey], { env, timeout: 30_000 });
        return out.trim() === "background" ? "background" : "normal";
      } catch {
        return "normal"; // fail open, like ship-testflight.sh
      }
    },
    readLastShip(appKey) {
      try {
        const [ts, sha] = fs.readFileSync(path.join(sdir, `last-ship-${appKey}.txt`), "utf8").trim().split(/\s+/);
        if (!/^\d+$/.test(ts || "")) return null;
        return { at: new Date(Number(ts) * 1000).toISOString(), sha: sha && sha !== "unknown" ? sha : null };
      } catch {
        return null;
      }
    },
    readState() {
      try {
        return JSON.parse(fs.readFileSync(path.join(sdir, "freshness-state.json"), "utf8"));
      } catch {
        return { schemaVersion: 1, apps: {} };
      }
    },
    writeState(state) {
      writeFileAtomic(path.join(sdir, "freshness-state.json"), JSON.stringify(state, null, 2) + "\n");
    },
    writeReport(report) {
      writeFileAtomic(path.join(sdir, "freshness-last.json"), JSON.stringify(report, null, 2) + "\n");
    },
    lock() {
      fs.mkdirSync(sdir, { recursive: true, mode: 0o700 });
      return acquireLockDir(path.join(sdir, "freshness.lockdir"));
    },
    async post(body) {
      fs.mkdirSync(sdir, { recursive: true, mode: 0o700 });
      const file = path.join(sdir, `freshness-alert-${process.pid}.md`);
      fs.writeFileSync(file, body, { mode: 0o600 });
      try {
        const seat = env.IOS_TF_FRESHNESS_SEAT || "CLAUDE";
        await runCmd("simple-chat", ["post", "--as", seat, "--session", "tfresh", "--channel", ALERT_CHANNEL,
          "--topic", ALERT_TOPIC, "--body-file", file], { env, timeout: 60_000 });
      } finally {
        fs.rmSync(file, { force: true });
      }
    },
  };
  return deps;
}

/**
 * Take the run lock (an atomic mkdir).  Returns a release function, or null when
 * another run holds it.  A lock older than 30 minutes is reclaimed;  when two runs
 * race to reclaim it, the loser sees EEXIST and gets null, never an exception.
 */
export function acquireLockDir(dir, { fsx = fs, nowMs = Date.now() } = {}) {
  const take = () => {
    try {
      fsx.mkdirSync(dir);
      return true;
    } catch (err) {
      if (err && err.code === "EEXIST") return false;
      throw err;
    }
  };
  const release = () => fsx.rmSync(dir, { recursive: true, force: true });
  if (take()) return release;
  let age = 0;
  try { age = nowMs - fsx.statSync(dir).mtimeMs; } catch { /* released meanwhile */ }
  if (age < 30 * MINUTE) return take() ? release : null;
  fsx.rmSync(dir, { recursive: true, force: true });
  return take() ? release : null;
}

function writeFileAtomic(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, text, { mode: 0o600 });
  fs.renameSync(tmp, file);
}

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

function normalizeBuilds(json, platform) {
  const pre = new Map((json.included || []).filter((x) => x.type === "preReleaseVersions").map((x) => [x.id, x.attributes || {}]));
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
      uploadedAt: Number.isNaN(uploadedAt) ? null : uploadedAt,
      processingState: a.processingState || null,
      expired: Boolean(a.expired),
    });
  }
  out.sort((x, y) => (y.uploadedAt || 0) - (x.uploadedAt || 0));
  return out;
}

function buildView(b) {
  if (!b) return null;
  return {
    marketingVersion: b.marketingVersion,
    build: b.build,
    uploadedAt: b.uploadedAt ? new Date(b.uploadedAt).toISOString() : null,
    processingState: b.processingState,
    expired: b.expired,
  };
}

/** Context shared by every app in one run:  caches per repo. */
export function makeContext(deps, registry) {
  const cache = new Map();
  const memo = (k, f) => {
    if (!cache.has(k)) cache.set(k, f());
    return cache.get(k);
  };
  return {
    deps,
    registry,
    gh: (p) => memo(`gh:${p}`, () => deps.gh(p)),
    asc: (p) => memo(`asc:${p}`, () => deps.asc(p)),
  };
}

async function newestNativeCommit(ctx, full, branch, paths) {
  let best = null;
  for (const p of paths) {
    const rows = await ctx.gh(`repos/${full}/commits?sha=${encodeURIComponent(branch)}&path=${encodeURIComponent(p)}&per_page=1`);
    const c = Array.isArray(rows) ? rows[0] : null;
    if (!c) continue;
    const date = Date.parse(c.commit?.committer?.date || c.commit?.author?.date);
    if (!best || date > best.dateMs) {
      best = { sha: c.sha, dateMs: date, date: new Date(date).toISOString(), subject: String(c.commit?.message || "").split("\n")[0].slice(0, 120), path: p };
    }
  }
  return best;
}

async function contains(ctx, full, baseSha, headSha) {
  if (!baseSha || !headSha) return null;
  if (baseSha === headSha) return true;
  try {
    const cmp = await ctx.gh(`repos/${full}/compare/${baseSha}...${headSha}?per_page=1`);
    return cmp.status === "ahead" || cmp.status === "identical";
  } catch (err) {
    if (err.notFound) return null;
    throw err;
  }
}

async function compileState(ctx, app, full, headSha, nativeSha) {
  if (!app.compileCheck) return { state: "unknown", source: "no compile check configured" };
  const tried = new Set();
  for (const [label, sha] of [["main HEAD", headSha], ["newest native commit", nativeSha]]) {
    if (!sha || tried.has(sha)) continue;
    tried.add(sha);
    let runs = [];
    if (app.compileCheck.endsWith("*")) {
      for (let page = 1; page <= 3; page++) {
        const j = await ctx.gh(`repos/${full}/commits/${sha}/check-runs?per_page=100&page=${page}`);
        runs.push(...(j.check_runs || []));
        if ((j.check_runs || []).length < 100) break;
      }
    } else {
      const j = await ctx.gh(`repos/${full}/commits/${sha}/check-runs?per_page=100&check_name=${encodeURIComponent(app.compileCheck)}`);
      runs = j.check_runs || [];
    }
    const hits = runs
      .filter((cr) => matchCheckName(app.compileCheck, cr.name))
      .filter((cr) => cr.conclusion !== "skipped" && cr.conclusion !== "neutral");
    if (!hits.length) continue;
    hits.sort((a, b) => (b.id || 0) - (a.id || 0));
    const cr = hits[0];
    const state = cr.status !== "completed" ? "pending" : cr.conclusion === "success" ? "green" : "red";
    return { state, source: label, sha, name: cr.name, conclusion: cr.conclusion || cr.status, url: cr.html_url || null };
  }
  return { state: "unknown", source: "no matching check run on main HEAD or the newest native commit" };
}

async function resolveShippedSource(ctx, key, app, full, branch, build) {
  const cut = buildCutTime(build.build, build.uploadedAt);
  const from = new Date((cut ?? build.uploadedAt) - 3 * HOUR).toISOString().replace(/\.\d{3}Z$/, "Z");
  const to = new Date(build.uploadedAt + 20 * MINUTE).toISOString().replace(/\.\d{3}Z$/, "Z");
  const workflows = [...new Set([...(app.sourceWorkflows || []), app.ship?.workflow].filter(Boolean))];
  const runs = [];
  for (const wf of workflows) {
    try {
      const j = await ctx.gh(`repos/${full}/actions/workflows/${wf}/runs?branch=${encodeURIComponent(branch)}&per_page=100&created=${from}..${to}`);
      for (const r of j.workflow_runs || []) runs.push({ ...r, _workflow: wf });
    } catch (err) {
      if (!err.notFound) throw err;
    }
  }
  const run = correlateRun(runs, cut, build.uploadedAt);
  if (run) return { sha: run.head_sha, source: "ship-run", runUrl: run.html_url, workflow: run._workflow, event: run.event };
  const last = ctx.deps.readLastShip(key);
  if (last?.sha) {
    const at = Date.parse(last.at);
    if (at >= build.uploadedAt - 30 * MINUTE && at <= build.uploadedAt + 3 * HOUR) {
      return { sha: last.sha, source: "state-file", runUrl: null };
    }
  }
  return { sha: null, source: "time", runUrl: null, cut: cut ?? build.uploadedAt };
}

export async function evaluateApp(ctx, key, app, now) {
  const reg = ctx.registry;
  const r = {
    key, name: app.displayName || key, repo: app.repo, platform: app.platform || null,
    appleId: String(app.appleId || ""), bundleId: app.bundleId || null,
    status: null, reason: null,
  };
  if (app.skip) {
    r.status = "skipped";
    r.reason = app.skip;
    return r;
  }
  try {
    const full = `${reg.owner}/${app.repo}`;
    const branch = app.branch || "main";
    const nowMs = now.getTime();

    const ascJson = await ctx.asc(`/v1/builds?filter[app]=${app.appleId}&sort=-uploadedDate&limit=10` +
      "&fields[builds]=version,uploadedDate,processingState,expired,preReleaseVersion" +
      "&include=preReleaseVersion&fields[preReleaseVersions]=version,platform");
    const builds = normalizeBuilds(ascJson, app.platform);
    const newest = builds[0] || null;
    const valid = builds.find((b) => b.processingState === "VALID" && !b.expired) || null;

    const headRow = await ctx.gh(`repos/${full}/commits/${encodeURIComponent(branch)}`);
    const head = { sha: headRow.sha, date: headRow.commit?.committer?.date || null };
    const native = await newestNativeCommit(ctx, full, branch, app.paths || []);
    if (!native) throw new Error(`no ${branch} commit touches ${(app.paths || []).join(", ") || "(no paths configured)"}`);

    r.mainHead = head;
    r.native = { sha: native.sha, date: native.date, subject: native.subject, path: native.path };
    r.testflight = buildView(valid);
    r.newestUpload = newest && newest !== valid ? buildView(newest) : null;
    r.lastShipState = ctx.deps.readLastShip(key);

    // Rate gate:  measured from App Store Connect, not from a local file that
    // hosted runners never write.
    const gateHours = Number(reg.rateGateHours ?? 2.5);
    const lastUpload = newest?.uploadedAt ?? null;
    const sinceH = lastUpload ? (nowMs - lastUpload) / HOUR : null;
    r.rateGate = {
      hours: gateHours,
      lastUploadAt: lastUpload ? new Date(lastUpload).toISOString() : null,
      hoursSinceUpload: sinceH == null ? null : round1(sinceH),
      open: sinceH == null || sinceH >= gateHours,
      minutesLeft: sinceH == null || sinceH >= gateHours ? 0 : Math.ceil((gateHours - sinceH) * 60),
    };

    // Pipeline:  workflow state and any run in flight.
    if (app.ship?.workflow) {
      let wf = null;
      try {
        wf = await ctx.gh(`repos/${full}/actions/workflows/${app.ship.workflow}`);
      } catch (err) {
        if (!err.notFound) throw err;
      }
      const runsJson = wf
        ? await ctx.gh(`repos/${full}/actions/workflows/${app.ship.workflow}/runs?branch=${encodeURIComponent(branch)}&per_page=20`)
        : { workflow_runs: [] };
      const runs = runsJson.workflow_runs || [];
      const { live: inflight, stale: staleRuns } = splitInflight(runs, nowMs, Number(reg.inflightMaxHours ?? DEFAULT_INFLIGHT_MAX_HOURS));
      const onHead = runs.find((x) => x.head_sha === head.sha && x.status === "completed") || null;
      // Some ship jobs only upload while a repo variable is "true" (ContactLogo, Hog Hunter);
      // with it off, a dispatch is a skipped job that uploads nothing.
      let enabledVar = null;
      if (app.ship.enabledVar) {
        let value = null;
        try {
          value = (await ctx.gh(`repos/${full}/actions/variables/${app.ship.enabledVar}`)).value ?? null;
        } catch (err) {
          if (!err.notFound) throw err;
        }
        enabledVar = { name: app.ship.enabledVar, value };
      }
      r.pipeline = {
        workflow: app.ship.workflow,
        state: wf ? wf.state : "missing",
        url: wf ? wf.html_url : null,
        autoRemedy: app.autoRemedy !== false,
        manualReason: app.manualReason || null,
        inputs: app.ship.inputs || {},
        enabledVar,
        inflight,
        staleRuns,
        lastRunOnHead: onHead ? { conclusion: onHead.conclusion, event: onHead.event, url: onHead.html_url } : null,
      };
    } else {
      r.pipeline = { workflow: null, state: "none", autoRemedy: false, inflight: [], staleRuns: [], manualReason: app.manualReason || "no ship workflow" };
    }

    // Shipped source and containment.
    let isCurrent = false;
    if (valid) {
      const src = await resolveShippedSource(ctx, key, app, full, branch, valid);
      r.shipped = { sha: src.sha, source: src.source, runUrl: src.runUrl || null };
      let c = src.sha ? await contains(ctx, full, native.sha, src.sha) : null;
      if (c == null) {
        // No sha, or the sha is unknown to the repo:  compare times instead.
        const cut = src.cut ?? buildCutTime(valid.build, valid.uploadedAt) ?? valid.uploadedAt;
        c = cut >= native.dateMs;
        r.shipped.source = src.sha ? `${src.source}+time` : "time";
      }
      isCurrent = c;
      r.shipped.isMainHead = src.sha ? src.sha === head.sha : null;
    } else {
      r.shipped = null;
    }

    r.compile = await compileState(ctx, app, full, head.sha, native.sha);

    if (isCurrent) {
      r.status = "current";
      r.gapHours = 0;
    } else {
      r.gapHours = round1(Math.max(0, nowMs - native.dateMs) / HOUR);
      let shipping = false;
      for (const run of r.pipeline.inflight) {
        if (run.sha === head.sha || run.sha === native.sha || (await contains(ctx, full, native.sha, run.sha))) {
          shipping = true;
          break;
        }
      }
      const newestCut = newest ? buildCutTime(newest.build, newest.uploadedAt) ?? newest.uploadedAt : null;
      if (shipping) {
        r.status = "shipping";
        r.reason = "a ship run that contains the newest native commit is in flight";
      } else if (newest && newest.processingState === "PROCESSING" && newestCut >= native.dateMs) {
        r.status = "processing";
        r.reason = `build ${newest.build} is still processing in App Store Connect`;
      } else if (!builds.length) {
        r.status = "no-build";
        r.reason = "no TestFlight build exists on this App Store Connect record";
      } else if (!valid && builds.every((b) => b.expired)) {
        r.status = "expired";
        r.reason = "every TestFlight build has expired";
      } else if (!valid) {
        r.status = "behind";
        r.reason = `no usable build:  newest is ${newest.processingState || "unknown"}`;
      } else {
        r.status = "behind";
        r.reason = `TestFlight ${valid.marketingVersion || valid.build} does not contain ${short(native.sha)} (${native.path})`;
      }
      const stuck = r.pipeline.staleRuns || [];
      if (stuck.length && r.status !== "shipping") {
        const oldest = oldestRun(stuck);
        r.reason += `;  a ship run in flight for ${oldest.ageHours}h looks stuck (${oldest.url})`;
      }
    }
  } catch (err) {
    r.status = "error";
    r.reason = String(err.message || err).slice(0, 300);
  }
  return r;
}

// ---------------------------------------------------------------------------
// Remedy
// ---------------------------------------------------------------------------

export function dispatchCommand(owner, r) {
  const parts = ["gh", "workflow", "run", r.pipeline.workflow, "--repo", `${owner}/${r.repo}`, "--ref", "main"];
  for (const [k, v] of Object.entries(r.pipeline.inputs || {})) parts.push("-f", `${k}=${v}`);
  return parts.join(" ");
}

/**
 * Decide and (unless dryRun) perform catch-up ships.  Mutates each result's
 * `remedy` and, for a real dispatch, state.apps[key].remedy.
 */
export async function runRemedies(results, state, deps, registry, { dryRun = false, maxShips = 1, now, extraInflight = [] }) {
  const nowMs = now.getTime();
  state.apps = state.apps || {};
  const fleetInflight = [...results.filter((r) => r.pipeline?.inflight?.length).map((r) => r.key), ...extraInflight];
  let dispatched = 0;
  const candidates = results.filter((r) => REMEDIABLE.has(r.status)).sort((a, b) => (b.gapHours || 0) - (a.gapHours || 0));
  for (const r of candidates) {
    const app = registry.apps[r.key];
    const s = (state.apps[r.key] = state.apps[r.key] || {});
    const decide = (action, reason) => {
      r.remedy = { action, reason };
      return r.remedy;
    };
    if (app.autoRemedy === false) { decide("blocked", app.manualReason || "autoRemedy is off for this app"); continue; }
    if (!r.pipeline?.workflow) { decide("blocked", "no ship workflow in the registry"); continue; }
    if (r.pipeline.state !== "active") { decide("blocked", `ship workflow ${r.pipeline.workflow} is ${r.pipeline.state}`); continue; }
    if (r.pipeline.enabledVar && r.pipeline.enabledVar.value !== "true") {
      decide("blocked", `repo variable ${r.pipeline.enabledVar.name} is ${r.pipeline.enabledVar.value == null ? "not set" : `"${r.pipeline.enabledVar.value}"`}, so ${r.pipeline.workflow} skips its upload`);
      continue;
    }
    if (r.compile?.state === "pending") { decide("deferred", "compile check on main is still running"); continue; }
    if (r.compile?.state !== "green") { decide("blocked", `compile check on main is ${r.compile?.state || "unknown"}`); continue; }
    if (!r.rateGate.open) { decide("deferred", `rate gate:  last upload ${r.rateGate.hoursSinceUpload}h ago, ${r.rateGate.minutesLeft}m left of ${r.rateGate.hours}h`); continue; }
    if (r.shipped?.isMainHead) { decide("blocked", "main HEAD is already the shipped sha"); continue; }
    if (s.remedy && s.remedy.headSha === r.mainHead.sha) {
      decide("blocked", `already dispatched once for main HEAD ${short(r.mainHead.sha)} at ${ownerTime(s.remedy.at)};  a new main commit re-arms it`);
      continue;
    }
    if ((await deps.buildWindow(r.key)) === "background") { decide("deferred", "build-window.sh prefers another time for this app"); continue; }
    if (fleetInflight.length) { decide("deferred", `a fleet ship is already running (${fleetInflight.join(", ")})`); continue; }
    if (dispatched >= maxShips) { decide("deferred", `one catch-up ship per run (max ${maxShips})`); continue; }
    const cmd = dispatchCommand(registry.owner, r);
    if (dryRun) {
      decide("would-dispatch", cmd);
      dispatched++;
      continue;
    }
    try {
      await deps.ghDispatch(`${registry.owner}/${r.repo}`, r.pipeline.workflow, "main", r.pipeline.inputs);
      decide("dispatched", cmd);
      s.remedy = { at: new Date(nowMs).toISOString(), headSha: r.mainHead.sha, targetSha: r.native.sha, workflow: r.pipeline.workflow };
      fleetInflight.push(r.key);
      dispatched++;
    } catch (err) {
      decide("failed", String(err.message || err).slice(0, 200));
    }
  }
  return dispatched;
}

// ---------------------------------------------------------------------------
// Alerts
// ---------------------------------------------------------------------------

function alertLine(r, s) {
  const tf = r.testflight
    ? `TestFlight ${r.testflight.marketingVersion || "?"} (${r.testflight.build}) is from ${ownerTime(r.testflight.uploadedAt)}`
    : "There is no usable TestFlight build";
  const native = `the newest native commit ${short(r.native?.sha)} landed ${ownerTime(r.native?.date)}, ${r.gapHours}h ago`;
  let why;
  const stuck = r.pipeline?.staleRuns || [];
  if (stuck.length) {
    const oldest = oldestRun(stuck);
    why = `A ship run has been in flight for ${oldest.ageHours}h and looks stuck (${oldest.url})`;
  } else if (s.remedy && s.remedy.targetSha) {
    why = `A catch-up ship was dispatched ${ownerTime(s.remedy.at)} and has not landed`;
  } else if (r.remedy?.reason) {
    why = `Catch-up ${r.remedy.action}:  ${r.remedy.reason}`;
  } else {
    why = "No catch-up ship was attempted";
  }
  return `- **${r.name}** (\`${r.key}\`):  ${sentences(`${tf};  ${native}.`, `${why}.`)}`;
}

/**
 * Decide which alerts to post.  Mutates state (behindSince, alert stamps) and
 * returns { body, apps } or null.
 */
export function planAlerts(results, state, registry, { now, exitCode, errors = [] }) {
  const nowMs = now.getTime();
  const staleH = Number(registry.staleAlertHours ?? 6);
  const landH = Number(registry.remedyLandHours ?? DEFAULT_REMEDY_LAND_HOURS);
  state.apps = state.apps || {};
  const due = [];
  for (const r of results) {
    if (r.status === "skipped" || r.status === "error") continue;
    const s = (state.apps[r.key] = state.apps[r.key] || {});
    if (r.status === "current") {
      delete s.behindSince;
      delete s.alert;
      delete s.remedy;
      continue;
    }
    if (!s.behindSince) s.behindSince = new Date(nowMs).toISOString();
    if (!ALERTABLE.has(r.status)) continue;
    const behindH = (nowMs - Date.parse(s.behindSince)) / HOUR;
    if (behindH < staleH) continue;
    // A catch-up dispatched this run, or still inside its landing window, has not failed
    // yet:  say nothing (and store no alert key) until the window has passed.
    if (s.remedy?.at && nowMs - Date.parse(s.remedy.at) < landH * HOUR) continue;
    const kind = s.remedy ? "remedy-did-not-land" : `remedy-${r.remedy?.action || "none"}:${r.remedy?.reason?.split(":")[0] || ""}`;
    const key = `${r.native?.sha || ""}|${kind}`;
    if (s.alert?.key === key) continue;
    if (s.alert?.at && nowMs - Date.parse(s.alert.at) < staleH * HOUR) continue;
    due.push(r);
    s.alert = { at: new Date(nowMs).toISOString(), key };
  }
  const failed = exitCode === EXIT_FAILED;
  let failLine = null;
  if (failed) {
    const msgs = [...errors, ...results.filter((r) => r.status === "error").map((r) => `${r.key}: ${r.reason}`)];
    const key = createHash("sha256").update(msgs.join("\n")).digest("hex").slice(0, 12);
    const prev = state.checkFailure;
    const capped = prev?.at && nowMs - Date.parse(prev.at) < staleH * HOUR;
    if (!capped) {
      failLine = sentences("**The TestFlight freshness check itself failed.**", `${msgs.slice(0, 5).map((m) => `\`${m.replace(/`/g, "'")}\``).join(";  ")}.`);
      state.checkFailure = { at: new Date(nowMs).toISOString(), key };
    }
  } else {
    delete state.checkFailure;
  }
  if (!due.length && !failLine) return null;
  const lines = [];
  if (due.length) {
    lines.push(sentences(
      `**TestFlight freshness:**  ${due.length} ${due.length === 1 ? "app is" : "apps are"} still behind main after ${staleH} hours.`,
      "Each line says why the automatic catch-up did not fix it.",
    ));
    for (const r of due) lines.push(alertLine(r, state.apps[r.key]));
  }
  if (failLine) lines.push(failLine);
  lines.push(sentences(
    `Checked ${ownerTime(now)}.`,
    "Re-run with `node ~/apps/ios-fleet/ios_testflight_freshness.mjs`, and see docs/ios-testflight-freshness.md in AI-Fleet-Coordinator.",
  ));
  return { body: lines.join("\n") + "\n", apps: due.map((r) => r.key), checkFailed: Boolean(failLine) };
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function shortTime(iso) {
  return iso ? ownerTime(iso).replace(/^\w+, /, "") : "-";
}

export function renderTable(report) {
  const rows = [["APP", "STATUS", "TESTFLIGHT", "UPLOADED", "SHIPPED", "NEWEST NATIVE", "GAP", "COMPILE", "GATE", "NOTE"]];
  for (const r of report.apps) {
    if (r.status === "skipped") {
      rows.push([r.key, "skipped", "-", "-", "-", "-", "-", "-", "-", r.reason]);
      continue;
    }
    if (r.status === "error") {
      rows.push([r.key, "ERROR", "-", "-", "-", "-", "-", "-", "-", r.reason]);
      continue;
    }
    const tf = r.testflight ? `${r.testflight.marketingVersion || "?"} (${r.testflight.build})` : "none";
    const shipped = r.shipped ? `${short(r.shipped.sha)} ${r.shipped.source}` : "-";
    const native = r.native ? `${short(r.native.sha)} ${shortTime(r.native.date)}` : "-";
    const gate = r.rateGate?.open ? "open" : `${r.rateGate?.minutesLeft}m`;
    const notes = [];
    if (r.status !== "current" && r.reason) notes.push(r.reason);
    if (r.pipeline && r.pipeline.state !== "active" && r.pipeline.state !== "none") notes.push(`${r.pipeline.workflow} ${r.pipeline.state}`);
    if (r.remedy) notes.push(`remedy ${r.remedy.action}: ${r.remedy.reason}`);
    rows.push([
      r.key, r.status.toUpperCase() === "CURRENT" ? "current" : r.status.toUpperCase(), tf,
      shortTime(r.testflight?.uploadedAt), shipped, native,
      r.status === "current" ? "-" : `${r.gapHours}h`, r.compile?.state || "-", gate, notes.join(";  "),
    ]);
  }
  const widths = rows[0].map((_, i) => Math.min(i === 9 ? 999 : 34, Math.max(...rows.map((row) => String(row[i]).length))));
  const fmt = (row) => row.map((c, i) => (i === row.length - 1 ? String(c) : String(c).slice(0, widths[i]).padEnd(widths[i]))).join("  ");
  const out = [fmt(rows[0]), fmt(widths.map((w) => "-".repeat(Math.min(w, 12))))];
  for (const row of rows.slice(1)) out.push(fmt(row));
  const s = report.summary;
  out.push("");
  out.push(`Summary:  ${s.current} current, ${s.behind} behind, ${s.shipping} shipping, ${s.processing} processing, ${s["no-build"]} without a build, ${s.expired} expired, ${s.skipped} skipped, ${s.error} errors.  Exit ${report.exitCode}.  Checked ${ownerTime(report.generatedAt)}.`);
  if (report.unmapped?.length) out.push(`Not in testflight-apps.json:  ${report.unmapped.map((u) => `${u.name} (${u.bundleId}, ${u.appleId})`).join(";  ")}.`);
  for (const e of report.errors || []) out.push(`ERROR:  ${e}`);
  if (report.alert) out.push(`Alert ${report.alert.posted ? "posted" : report.alert.dryRun ? "(dry run, not posted)" : "NOT posted"} for:  ${report.alert.apps.join(", ") || "check failure"}.`);
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export function parseArgs(argv) {
  const opts = { apps: null, repos: null, json: false, shipBehind: false, dryRun: false, alert: false, maxShips: 1, registry: null, concurrency: 4, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => {
      if (i + 1 >= argv.length) throw new Error(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--app") opts.apps = (opts.apps || []).concat(val().split(",").filter(Boolean));
    else if (a === "--repo") opts.repos = (opts.repos || []).concat(val().split(",").filter(Boolean));
    else if (a === "--json") opts.json = true;
    else if (a === "--ship-behind") opts.shipBehind = true;
    else if (a === "--dry-run") opts.dryRun = true;
    else if (a === "--alert") opts.alert = true;
    else if (a === "--max-ships") opts.maxShips = Math.max(0, parseInt(val(), 10) || 0);
    else if (a === "--registry") opts.registry = val();
    else if (a === "--concurrency") opts.concurrency = Math.max(1, parseInt(val(), 10) || 1);
    else if (a === "-h" || a === "--help") opts.help = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return opts;
}

export function loadRegistry(file) {
  const reg = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!reg || typeof reg.apps !== "object" || !reg.owner) throw new Error(`${file}: missing owner or apps`);
  // A catch-up ship needs a green compile check on main (the remedy's first guard), so an
  // app that may be remedied automatically must name one.  An app with no iOS compile
  // check on main sets autoRemedy false with a manualReason instead of failing silently.
  const bad = Object.entries(reg.apps)
    .filter(([, a]) => !a.skip && a.autoRemedy !== false && !a.compileCheck)
    .map(([k]) => `${k}: autoRemedy is on but compileCheck is empty`);
  if (bad.length) throw new Error(`${file}: ${bad.join(";  ")}`);
  return reg;
}

export async function runCheck(opts, deps, registry) {
  const now = deps.now();
  const errors = [];
  let keys = Object.keys(registry.apps);
  if (opts.apps) {
    const unknown = opts.apps.filter((k) => !registry.apps[k]);
    if (unknown.length) errors.push(`unknown app key(s):  ${unknown.join(", ")}`);
    keys = keys.filter((k) => opts.apps.includes(k));
  }
  if (opts.repos) {
    const want = new Set(opts.repos.map((x) => x.toLowerCase().replace(/^.*\//, "")));
    keys = keys.filter((k) => want.has(String(registry.apps[k].repo).toLowerCase()));
    if (!keys.length) errors.push(`no registry app ships from ${opts.repos.join(", ")}`);
  }
  const ctx = makeContext(deps, registry);
  const fullRun = !opts.apps && !opts.repos;
  const [results, unmapped] = await Promise.all([
    pool(keys, opts.concurrency || 4, (k) => evaluateApp(ctx, k, registry.apps[k], now)),
    fullRun ? findUnmapped(ctx, registry).catch((err) => { errors.push(String(err.message || err)); return []; }) : Promise.resolve([]),
  ]);
  const ascDown = results.filter((r) => r.status !== "skipped").length > 0 &&
    results.filter((r) => r.status !== "skipped").every((r) => r.status === "error" && /App Store Connect|asc-api/.test(r.reason || ""));
  if (ascDown) errors.push("App Store Connect is unreachable for every app");

  let state = null;
  let unlock = null;
  const mutating = (opts.shipBehind || opts.alert) && !opts.dryRun;
  if (mutating) {
    unlock = deps.lock();
    if (!unlock) errors.push("another freshness run holds the lock (freshness.lockdir)");
  }
  try {
    if (opts.shipBehind || opts.alert) state = deps.readState();
    if (opts.shipBehind && (!mutating || unlock)) {
      // One ship at a time across the fleet, even when --app narrowed the check.
      const extraInflight = fullRun ? [] : await otherAppsInFlight(ctx, registry, keys, now);
      await runRemedies(results, state, deps, registry, { dryRun: opts.dryRun, maxShips: opts.maxShips, now, extraInflight });
    }
    const exitCode = exitCodeFor(results, errors);
    const report = {
      schemaVersion: 1,
      generatedAt: now.toISOString(),
      mode: { shipBehind: opts.shipBehind, dryRun: opts.dryRun, alert: opts.alert, apps: opts.apps, repos: opts.repos },
      exitCode,
      summary: summarize(results),
      apps: results,
      unmapped,
      errors,
    };
    if (opts.alert && (!mutating || unlock)) {
      const plan = planAlerts(results, state, registry, { now, exitCode, errors });
      if (plan) {
        report.alert = { apps: plan.apps, checkFailed: plan.checkFailed, dryRun: opts.dryRun, posted: false, body: plan.body };
        if (!opts.dryRun) {
          try {
            await deps.post(plan.body);
            report.alert.posted = true;
          } catch (err) {
            // A failed post never changes the exit code;  the next run retries
            // because the alert stamps are rolled back.
            report.alert.error = String(err.message || err).slice(0, 200);
            for (const k of plan.apps) if (state.apps[k]) delete state.apps[k].alert;
            if (plan.checkFailed) delete state.checkFailure;
          }
        }
      }
    }
    if (mutating && unlock) {
      state.updatedAt = now.toISOString();
      deps.writeState(state);
    }
    if (fullRun && !opts.dryRun) {
      try { deps.writeReport(report); } catch { /* best effort */ }
    }
    return report;
  } finally {
    if (unlock) unlock();
  }
}

async function otherAppsInFlight(ctx, registry, checkedKeys, now) {
  const others = Object.keys(registry.apps).filter((k) => !checkedKeys.includes(k) && !registry.apps[k].skip && registry.apps[k].ship?.workflow);
  const hits = await pool(others, 4, async (k) => {
    const a = registry.apps[k];
    try {
      const j = await ctx.gh(`repos/${registry.owner}/${a.repo}/actions/workflows/${a.ship.workflow}/runs?branch=${encodeURIComponent(a.branch || "main")}&per_page=20`);
      return splitInflight(j.workflow_runs, now.getTime(), Number(registry.inflightMaxHours ?? DEFAULT_INFLIGHT_MAX_HOURS)).live.length ? k : null;
    } catch {
      return null;
    }
  });
  return hits.filter(Boolean);
}

async function findUnmapped(ctx, registry) {
  const j = await ctx.asc("/v1/apps?fields[apps]=name,bundleId&limit=200");
  const known = new Set(Object.values(registry.apps).map((a) => String(a.appleId)));
  const ignore = new Set((registry.ignoreAppleIds || []).map(String));
  return (j.data || [])
    .filter((a) => !known.has(String(a.id)) && !ignore.has(String(a.id)))
    .filter((a) => !/^(OLD|IGNORE)\b/i.test(a.attributes?.name || ""))
    .map((a) => ({ appleId: String(a.id), bundleId: a.attributes?.bundleId || null, name: a.attributes?.name || null }));
}

const HELP = `usage: ios_testflight_freshness.mjs [--app KEY[,KEY]] [--repo REPO] [--json]
                                   [--ship-behind [--dry-run] [--max-ships N]] [--alert]
                                   [--registry PATH] [--concurrency N]

Exit 0 every checked app is current, 1 something is behind, 2 the check failed.
See docs/ios-testflight-freshness.md in AI-Fleet-Coordinator.`;

export async function main(argv = process.argv.slice(2), deps = makeDeps(), out = process.stdout) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    out.write(`${err.message}\n${HELP}\n`);
    return EXIT_FAILED;
  }
  if (opts.help) {
    out.write(`${HELP}\n`);
    return EXIT_CURRENT;
  }
  let registry;
  try {
    registry = loadRegistry(opts.registry || resolveFleetFile("testflight-apps.json") || path.join(SCRIPT_DIR, "testflight-apps.json"));
  } catch (err) {
    out.write(`ERROR:  cannot read the registry:  ${err.message}\n`);
    return EXIT_FAILED;
  }
  let report;
  try {
    report = await runCheck(opts, deps, registry);
  } catch (err) {
    out.write(`ERROR:  ${err.message || err}\n`);
    return EXIT_FAILED;
  }
  out.write(opts.json ? `${JSON.stringify(report, null, 2)}\n` : `${renderTable(report)}\n`);
  return report.exitCode;
}

if (process.argv[1] && import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href) {
  main().then((code) => process.exit(code), (err) => {
    process.stdout.write(`ERROR:  ${err && err.message ? err.message : err}\n`);
    process.exit(EXIT_FAILED);
  });
}
