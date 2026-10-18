// node --test scripts/ios-fleet/ios_testflight_freshness.test.mjs
// App Store Connect, gh, build-window.sh, the state file and Zulip are all
// stubbed:  nothing here touches the network or ~/.cache.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  EXIT_BEHIND, EXIT_CURRENT, EXIT_FAILED, SENTENCE_GAP,
  acquireLockDir, buildCutTime, oldestRun, correlateRun, dispatchCommand, evaluateApp, exitCodeFor, loadRegistry,
  main, makeContext, matchCheckName, ownerTime, parseArgs, planAlerts, runRemedies, sentences,
} from "./ios_testflight_freshness.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const NOW = new Date("2026-10-18T00:15:00Z"); // Sat, Oct 17, 7:15pm on the owner's clock

// ---------------------------------------------------------------------------
// Fake world
// ---------------------------------------------------------------------------

function registry(apps, extra = {}) {
  return { owner: "Org", rateGateHours: 2.5, staleAlertHours: 6, apps, ...extra };
}

function app(over = {}) {
  return {
    displayName: "Demo", appleId: "111", bundleId: "demo.ios", platform: "IOS", repo: "Demo",
    paths: ["ios"], ship: { workflow: "ios-ship.yml", inputs: {} }, sourceWorkflows: ["ios-ship.yml"],
    compileCheck: "xcodebuild (unsigned)", autoRemedy: true, ...over,
  };
}

function ascBuild(id, build, marketing, uploaded, state = "VALID", expired = false, platform = "IOS") {
  return {
    data: { type: "builds", id: `b${id}`, attributes: { version: build, uploadedDate: uploaded, processingState: state, expired }, relationships: { preReleaseVersion: { data: { id: `p${id}` } } } },
    inc: { type: "preReleaseVersions", id: `p${id}`, attributes: { version: marketing, platform } },
  };
}

/**
 * repos: { Demo: { head, headDate, natives: {path: {sha,date}}, wfState, recentRuns, createdRuns,
 *                  compare: {"a...b": status}, checks: {sha: [check runs]} } }
 * builds: { appleId: [ascBuild(...)] }
 */
function fakeDeps({ repos = {}, builds = {}, window = "normal", lastShip = {}, state = null, failPost = false } = {}) {
  const calls = { gh: [], dispatch: [], posts: [], states: [] };
  const deps = {
    now: () => NOW,
    async gh(p) {
      calls.gh.push(p);
      const m = /^repos\/Org\/([^/]+)\/(.*)$/.exec(p);
      if (!m) throw new Error(`unexpected gh path ${p}`);
      const repo = repos[m[1]];
      if (!repo) throw new Error(`unknown repo ${m[1]}`);
      const rest = m[2];
      let mm;
      if (rest === "commits/main") return { sha: repo.head, commit: { committer: { date: repo.headDate || "2026-10-17T20:00:00Z" } } };
      if ((mm = /^commits\?sha=main&path=([^&]+)&per_page=1$/.exec(rest))) {
        const n = (repo.natives || {})[decodeURIComponent(mm[1])];
        return n ? [{ sha: n.sha, commit: { committer: { date: n.date }, message: `${n.subject || "native change"}\n\nbody` } }] : [];
      }
      if ((mm = /^actions\/workflows\/([^/?]+)$/.exec(rest))) {
        if (repo.wfMissing) { const e = new Error("gh: Not Found (HTTP 404)"); e.notFound = true; throw e; }
        return { state: repo.wfState || "active", html_url: `https://github.com/Org/${m[1]}/actions/workflows/${mm[1]}` };
      }
      if ((mm = /^actions\/workflows\/([^/?]+)\/runs\?(.*)$/.exec(rest))) {
        if (mm[2].includes("created=")) return { workflow_runs: repo.createdRuns || [] };
        return { workflow_runs: repo.recentRuns || [] };
      }
      if ((mm = /^compare\/([0-9a-z]+)\.\.\.([0-9a-z]+)/.exec(rest))) {
        return { status: (repo.compare || {})[`${mm[1]}...${mm[2]}`] || "behind" };
      }
      if ((mm = /^commits\/([0-9a-z]+)\/check-runs/.exec(rest))) {
        return { check_runs: (repo.checks || {})[mm[1]] || [] };
      }
      throw new Error(`unrouted gh path ${p}`);
    },
    async ghDispatch(repoFull, workflow, ref, inputs) { calls.dispatch.push({ repoFull, workflow, ref, inputs }); },
    async asc(p) {
      if (p.startsWith("/v1/apps?")) return { data: [] };
      const id = /filter\[app\]=(\d+)/.exec(p)[1];
      const list = builds[id] || [];
      return { data: list.map((b) => b.data), included: list.map((b) => b.inc) };
    },
    async buildWindow() { return window; },
    readLastShip: (k) => lastShip[k] || null,
    readState: () => state || { schemaVersion: 1, apps: {} },
    writeState: (s) => calls.states.push(JSON.parse(JSON.stringify(s))),
    writeReport() {},
    lock: () => () => {},
    async post(body) { if (failPost) throw new Error("agent-sync exit 1"); calls.posts.push(body); },
  };
  return { deps, calls };
}

function run(sha, started, updated, extra = {}) {
  return { id: Math.floor(Math.random() * 1e9), head_sha: sha, run_started_at: started, created_at: started, updated_at: updated, status: "completed", conclusion: "success", event: "push", html_url: `https://github.com/run/${sha}`, ...extra };
}

// The BotFleet shape from 2026-10-17:  the upload came from a push run that
// ended red, between two quick gate-skip schedule runs.
function botfleetLike(over = {}) {
  return {
    head: "head1", natives: { ios: { sha: "nat1", date: "2026-10-17T17:02:32Z" } },
    createdRuns: [
      run("old1", "2026-10-17T16:29:07Z", "2026-10-17T16:29:55Z", { event: "schedule" }),
      run("ship1", "2026-10-17T17:02:36Z", "2026-10-17T17:10:45Z", { conclusion: "failure" }),
      run("skip1", "2026-10-17T18:51:37Z", "2026-10-17T18:52:26Z", { event: "schedule" }),
    ],
    compare: { "nat1...ship1": "identical" },
    checks: { head1: [{ id: 5, name: "xcodebuild (unsigned)", status: "completed", conclusion: "success", html_url: "u" }] },
    ...over,
  };
}

const BOTFLEET_BUILDS = { 111: [ascBuild(1, "202610171703", "1.0.113", "2026-10-17T10:09:00-07:00"), ascBuild(2, "202610170807", "1.0.112", "2026-10-17T01:12:03-07:00")] };

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

test("buildCutTime reads the UTC cut time and rejects implausible numbers", () => {
  const up = Date.parse("2026-10-17T17:09:00Z");
  assert.equal(new Date(buildCutTime("202610171703", up)).toISOString(), "2026-10-17T17:03:00.000Z");
  assert.equal(buildCutTime("42", up), null);
  assert.equal(buildCutTime("202610181703", up), null, "a cut after the upload is not a cut time");
  assert.equal(buildCutTime("202610100000", up), null, "a cut days before the upload is not a cut time");
});

test("correlateRun picks the run that uploaded, even when it ended red", () => {
  const w = botfleetLike();
  const up = Date.parse("2026-10-17T17:09:00Z");
  const hit = correlateRun(w.createdRuns, buildCutTime("202610171703", up), up);
  assert.equal(hit.head_sha, "ship1");
  assert.equal(correlateRun(w.createdRuns.filter((r) => r.head_sha !== "ship1"), Date.parse("2026-10-17T17:03:00Z"), up), null,
    "a gate-skip run that finished before the cut is not the upload");
});

test("matchCheckName is exact unless the pattern ends in *", () => {
  assert.ok(matchCheckName("test", "test"));
  assert.ok(!matchCheckName("test", "test-reporter"));
  assert.ok(matchCheckName("apple (ContactLogoiOS*", "apple (ContactLogoiOS, generic/platform=iOS Simulator)"));
  assert.ok(!matchCheckName("apple (ContactLogoiOS*", "apple (ContactLogoMac, platform=macOS)"));
});

test("owner clock is 12-hour Central with no zone abbreviation", () => {
  assert.equal(ownerTime(NOW), "Sat, Oct 17, 7:15pm");
  assert.equal(ownerTime("2026-10-17T14:05:00Z"), "Sat, Oct 17, 9:05am");
  assert.doesNotMatch(ownerTime(NOW), /C[DS]?T/);
});

test("sentences use U+00A0 plus a space", () => {
  assert.equal(sentences("One.", "Two."), `One.${SENTENCE_GAP}Two.`);
  assert.ok(SENTENCE_GAP.startsWith(" "));
});

test("exit codes:  0 current, 1 behind or in flight, 2 failed", () => {
  assert.equal(exitCodeFor([{ status: "current" }, { status: "skipped" }]), EXIT_CURRENT);
  for (const s of ["behind", "no-build", "expired", "shipping", "processing"]) assert.equal(exitCodeFor([{ status: "current" }, { status: s }]), EXIT_BEHIND);
  assert.equal(exitCodeFor([{ status: "behind" }, { status: "error" }]), EXIT_FAILED);
  assert.equal(exitCodeFor([{ status: "current" }], ["boom"]), EXIT_FAILED);
});

test("parseArgs", () => {
  const o = parseArgs(["--app", "a,b", "--app", "c", "--ship-behind", "--dry-run", "--max-ships", "2", "--json"]);
  assert.deepEqual(o.apps, ["a", "b", "c"]);
  assert.ok(o.shipBehind && o.dryRun && o.json);
  assert.equal(o.maxShips, 2);
  assert.throws(() => parseArgs(["--force-ship"]), /unknown argument/, "no gate override exists");
});

// ---------------------------------------------------------------------------
// evaluateApp
// ---------------------------------------------------------------------------

async function evalOne(world, builds, appOver = {}, depOver = {}) {
  const reg = registry({ demo: app(appOver) });
  const { deps, calls } = fakeDeps({ repos: { Demo: world }, builds, ...depOver });
  const r = await evaluateApp(makeContext(deps, reg), "demo", reg.apps.demo, NOW);
  return { r, calls };
}

test("current:  the correlated ship run contains the newest native commit", async () => {
  const { r } = await evalOne(botfleetLike(), BOTFLEET_BUILDS);
  assert.equal(r.status, "current", r.reason);
  assert.equal(r.shipped.sha, "ship1");
  assert.equal(r.shipped.source, "ship-run");
  assert.equal(r.testflight.marketingVersion, "1.0.113");
  assert.equal(r.compile.state, "green");
  assert.equal(r.rateGate.open, true);
});

test("behind:  compare says the shipped sha does not contain the native commit", async () => {
  const w = botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T20:15:00Z" } } });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(r.status, "behind");
  assert.equal(r.gapHours, 4);
  assert.match(r.reason, /does not contain nat2/);
});

test("the newest commit across several native paths wins", async () => {
  const w = botfleetLike({ natives: { ios: { sha: "nat1", date: "2026-10-17T17:02:32Z" }, "project.yml": { sha: "yml1", date: "2026-10-17T21:00:00Z" } } });
  const { r } = await evalOne(w, BOTFLEET_BUILDS, { paths: ["ios", "project.yml"] });
  assert.equal(r.native.sha, "yml1");
  assert.equal(r.native.path, "project.yml");
  assert.equal(r.status, "behind");
});

test("time fallback when no run correlates and there is no state file", async () => {
  const w = botfleetLike({ createdRuns: [] });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(r.shipped.source, "time");
  assert.equal(r.status, "current", "cut 17:03 is after the 17:02:32 commit");
  const late = botfleetLike({ createdRuns: [], natives: { ios: { sha: "nat9", date: "2026-10-17T17:05:00Z" } } });
  assert.equal((await evalOne(late, BOTFLEET_BUILDS)).r.status, "behind");
});

test("state-file fallback is used only when it matches the upload", async () => {
  const w = botfleetLike({ createdRuns: [], compare: { "nat1...st1": "ahead" } });
  const near = { demo: { at: "2026-10-17T17:15:00Z", sha: "st1" } };
  assert.equal((await evalOne(w, BOTFLEET_BUILDS, {}, { lastShip: near })).r.shipped.source, "state-file");
  const far = { demo: { at: "2026-10-10T17:15:00Z", sha: "st1" } };
  assert.equal((await evalOne(w, BOTFLEET_BUILDS, {}, { lastShip: far })).r.shipped.source, "time");
});

test("shipping:  a run in flight on main HEAD", async () => {
  const w = botfleetLike({
    natives: { ios: { sha: "nat2", date: "2026-10-17T20:15:00Z" } },
    recentRuns: [run("head1", "2026-10-18T00:10:00Z", "2026-10-18T00:12:00Z", { status: "in_progress", conclusion: null })],
  });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(r.status, "shipping");
  assert.equal(r.pipeline.inflight.length, 1);
});

test("processing:  the newest upload is after the commit but not VALID yet", async () => {
  const builds = { 111: [ascBuild(3, "202610172100", "1.0.114", "2026-10-17T21:06:00Z", "PROCESSING"), ...BOTFLEET_BUILDS[111]] };
  const w = botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T20:15:00Z" } } });
  const { r } = await evalOne(w, builds);
  assert.equal(r.status, "processing");
  assert.equal(r.rateGate.open, true, "3h09m since the 21:06 upload");
});

test("no-build, and a skipped app is not checked", async () => {
  const { r } = await evalOne(botfleetLike(), {});
  assert.equal(r.status, "no-build");
  const reg = registry({ mac: app({ skip: "ships through Sparkle" }) });
  const { deps, calls } = fakeDeps();
  const s = await evaluateApp(makeContext(deps, reg), "mac", reg.apps.mac, NOW);
  assert.equal(s.status, "skipped");
  assert.equal(calls.gh.length, 0);
});

test("compile check falls back to the newest native commit, and pending is not green", async () => {
  const w = botfleetLike({ checks: { nat1: [{ id: 1, name: "xcodebuild (unsigned)", status: "in_progress", conclusion: null }] } });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(r.compile.state, "pending");
  assert.equal(r.compile.source, "newest native commit");
  const none = botfleetLike({ checks: {} });
  assert.equal((await evalOne(none, BOTFLEET_BUILDS)).r.compile.state, "unknown");
});

test("a broken repo is an error for that app, not a crash", async () => {
  const w = botfleetLike({ natives: {} });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(r.status, "error");
  assert.match(r.reason, /no main commit touches ios/);
});

// ---------------------------------------------------------------------------
// Remedy
// ---------------------------------------------------------------------------

function behindResult(key, over = {}) {
  return {
    key, name: key, repo: over.repo || "Demo", status: "behind", gapHours: 10,
    mainHead: { sha: "head1" }, native: { sha: "nat2", date: "2026-10-17T14:00:00Z" },
    shipped: { sha: "ship1", isMainHead: false },
    compile: { state: "green" },
    rateGate: { open: true, hours: 2.5, hoursSinceUpload: 7, minutesLeft: 0 },
    pipeline: { workflow: "ios-ship.yml", state: "active", inputs: {}, inflight: [] },
    ...over,
  };
}

test("remedy dispatches the oldest gap first, one per run, with the workflow's upload input", async () => {
  const reg = registry({ a: app({ repo: "A" }), cl: app({ repo: "ContactLogo", ship: { workflow: "ios-testflight.yml", inputs: { upload: "true" } } }) });
  const results = [
    behindResult("a", { repo: "A", gapHours: 3 }),
    behindResult("cl", { repo: "ContactLogo", gapHours: 30, pipeline: { workflow: "ios-testflight.yml", state: "active", inputs: { upload: "true" }, inflight: [] } }),
  ];
  const state = { apps: {} };
  const { deps, calls } = fakeDeps();
  const n = await runRemedies(results, state, deps, reg, { now: NOW, maxShips: 1 });
  assert.equal(n, 1);
  assert.deepEqual(calls.dispatch, [{ repoFull: "Org/ContactLogo", workflow: "ios-testflight.yml", ref: "main", inputs: { upload: "true" } }]);
  assert.equal(results[1].remedy.action, "dispatched");
  assert.equal(results[0].remedy.action, "deferred");
  assert.match(results[0].remedy.reason, /fleet ship is already running \(cl\)/);
  assert.equal(state.apps.cl.remedy.headSha, "head1");
  assert.equal(dispatchCommand("Org", results[1]), "gh workflow run ios-testflight.yml --repo Org/ContactLogo --ref main -f upload=true");
});

test("remedy guards:  disabled, manual-only, compile red or pending, rate gate, same HEAD, build window, fleet in flight", async () => {
  const reg = registry({ x: app() , m: app({ autoRemedy: false, manualReason: "manual only by design" }) });
  const cases = [
    [behindResult("x", { pipeline: { workflow: "ios-ship.yml", state: "disabled_manually", inputs: {}, inflight: [] } }), {}, "blocked", /disabled_manually/],
    [behindResult("m"), {}, "blocked", /manual only by design/],
    [behindResult("x", { compile: { state: "red" } }), {}, "blocked", /compile check on main is red/],
    [behindResult("x", { compile: { state: "unknown" } }), {}, "blocked", /unknown/],
    [behindResult("x", { compile: { state: "pending" } }), {}, "deferred", /still running/],
    [behindResult("x", { rateGate: { open: false, hours: 2.5, hoursSinceUpload: 1.2, minutesLeft: 78 } }), {}, "deferred", /rate gate:  last upload 1.2h ago, 78m left/],
    [behindResult("x"), { stateApps: { x: { remedy: { at: "2026-10-17T20:00:00Z", headSha: "head1" } } } }, "blocked", /already dispatched once for main HEAD head1/],
    [behindResult("x"), { window: "background" }, "deferred", /build-window/],
    [behindResult("x", { pipeline: { workflow: "ios-ship.yml", state: "active", inputs: {}, inflight: [{ id: 1 }] } }), {}, "deferred", /already running \(x\)/],
  ];
  for (const [r, opt, action, re] of cases) {
    const { deps, calls } = fakeDeps({ window: opt.window || "normal" });
    await runRemedies([r], { apps: opt.stateApps || {} }, deps, reg, { now: NOW, maxShips: 1 });
    assert.equal(r.remedy.action, action, `${r.key}: ${r.remedy.reason}`);
    assert.match(r.remedy.reason, re);
    assert.equal(calls.dispatch.length, 0);
  }
});

test("a new main HEAD re-arms the one-dispatch-per-HEAD guard", async () => {
  const reg = registry({ x: app() });
  const { deps, calls } = fakeDeps();
  const r = behindResult("x", { mainHead: { sha: "head2" } });
  await runRemedies([r], { apps: { x: { remedy: { at: "2026-10-17T20:00:00Z", headSha: "head1" } } } }, deps, reg, { now: NOW });
  assert.equal(r.remedy.action, "dispatched");
  assert.equal(calls.dispatch.length, 1);
});

test("dry run plans the dispatch but sends nothing and records nothing", async () => {
  const reg = registry({ x: app() });
  const { deps, calls } = fakeDeps();
  const state = { apps: {} };
  const r = behindResult("x");
  await runRemedies([r], state, deps, reg, { now: NOW, dryRun: true });
  assert.equal(r.remedy.action, "would-dispatch");
  assert.equal(r.remedy.reason, "gh workflow run ios-ship.yml --repo Org/Demo --ref main");
  assert.equal(calls.dispatch.length, 0);
  assert.equal(state.apps.x.remedy, undefined);
});

// ---------------------------------------------------------------------------
// Alerts
// ---------------------------------------------------------------------------

test("alerts wait six hours, post once per commit and reason, and cap at one per app per six hours", () => {
  const reg = registry({ x: app() });
  const state = { apps: {} };
  const r = behindResult("x", { remedy: { action: "blocked", reason: "ship workflow ios-ship.yml is disabled_manually" }, testflight: { marketingVersion: "1.0.5", build: "202610100000", uploadedAt: "2026-10-10T00:00:00Z" } });
  assert.equal(planAlerts([r], state, reg, { now: NOW, exitCode: 1 }), null, "first sighting starts the clock");
  const later = new Date(NOW.getTime() + 6.5 * 3600e3);
  const plan = planAlerts([r], state, reg, { now: later, exitCode: 1 });
  assert.deepEqual(plan.apps, ["x"]);
  assert.match(plan.body, /still behind main after 6 hours/);
  assert.match(plan.body, /disabled_manually/);
  assert.ok(plan.body.includes(SENTENCE_GAP), "Zulip text carries U+00A0 sentence gaps");
  assert.match(plan.body, /Fri, Oct 9, 7:00pm/, "TestFlight upload time on the owner's clock");
  assert.equal(planAlerts([r], state, reg, { now: new Date(later.getTime() + 7 * 3600e3), exitCode: 1 }), null, "same commit and reason never posts twice");
  const moved = { ...r, native: { sha: "nat3", date: "2026-10-18T05:00:00Z" } };
  assert.equal(planAlerts([moved], state, reg, { now: new Date(later.getTime() + 2 * 3600e3), exitCode: 1 }), null, "a new commit inside six hours is capped");
  assert.deepEqual(planAlerts([moved], state, reg, { now: new Date(later.getTime() + 6.1 * 3600e3), exitCode: 1 }).apps, ["x"]);
});

test("shipping and processing never alert, and current clears the clock", () => {
  const reg = registry({ x: app() });
  const state = { apps: { x: { behindSince: "2026-10-16T00:00:00Z" } } };
  assert.equal(planAlerts([behindResult("x", { status: "shipping" })], state, reg, { now: NOW, exitCode: 1 }), null);
  planAlerts([behindResult("x", { status: "current" })], state, reg, { now: NOW, exitCode: 0 });
  assert.equal(state.apps.x.behindSince, undefined);
});

test("a dispatched catch-up that never landed is named in the alert", () => {
  const reg = registry({ x: app() });
  const state = { apps: { x: { behindSince: "2026-10-17T10:00:00Z", remedy: { at: "2026-10-17T12:00:00Z", headSha: "head1", targetSha: "nat2" } } } };
  const plan = planAlerts([behindResult("x")], state, reg, { now: NOW, exitCode: 1 });
  assert.match(plan.body, /catch-up ship was dispatched Sat, Oct 17, 7:00am and has not landed/);
});

test("a failed check alerts once per six hours", () => {
  const reg = registry({ x: app() });
  const state = { apps: {} };
  const err = [{ key: "x", status: "error", reason: "App Store Connect /v1/builds: HTTP 401" }];
  const plan = planAlerts(err, state, reg, { now: NOW, exitCode: EXIT_FAILED });
  assert.ok(plan.checkFailed);
  assert.match(plan.body, /check itself failed/);
  assert.equal(planAlerts(err, state, reg, { now: new Date(NOW.getTime() + 3600e3), exitCode: EXIT_FAILED }), null);
});

// ---------------------------------------------------------------------------
// End to end through main()
// ---------------------------------------------------------------------------

function sink() {
  let text = "";
  return { write: (s) => { text += s; }, get text() { return text; } };
}

function writeRegistry(reg) {
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || "/tmp", "tf-fresh-"));
  const file = path.join(dir, "reg.json");
  fs.writeFileSync(file, JSON.stringify(reg));
  return file;
}

test("main --json:  one current, one behind, exit 1", async () => {
  const reg = registry({ a: app({ repo: "A", appleId: "111" }), b: app({ repo: "B", appleId: "222" }) });
  const { deps } = fakeDeps({
    repos: { A: botfleetLike(), B: botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T20:15:00Z" } } }) },
    builds: { 111: BOTFLEET_BUILDS[111], 222: BOTFLEET_BUILDS[111] },
  });
  const out = sink();
  const code = await main(["--json", "--registry", writeRegistry(reg)], deps, out);
  assert.equal(code, EXIT_BEHIND);
  const rep = JSON.parse(out.text);
  assert.equal(rep.exitCode, 1);
  assert.deepEqual(rep.apps.map((x) => [x.key, x.status]), [["a", "current"], ["b", "behind"]]);
  assert.equal(rep.summary.current, 1);
});

test("main table, --repo filter, and an unknown --app fails the check", async () => {
  const reg = registry({ a: app({ repo: "A" }), mac: app({ repo: "A", skip: "Sparkle" }) });
  const { deps } = fakeDeps({ repos: { A: botfleetLike() }, builds: BOTFLEET_BUILDS });
  const out = sink();
  assert.equal(await main(["--repo", "Org/A", "--registry", writeRegistry(reg)], deps, out), EXIT_CURRENT);
  assert.match(out.text, /^APP\s+STATUS/m);
  assert.match(out.text, /Summary:  1 current, .* 1 skipped/);
  assert.equal(await main(["--app", "nope", "--registry", writeRegistry(reg)], deps, sink()), EXIT_FAILED);
});

test("main --ship-behind --alert:  dispatches without alerting, then a later run's failed post keeps the exit code and retries", async () => {
  const reg = registry({ b: app({ repo: "B" }) });
  const world = { B: botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T14:15:00Z" } }, createdRuns: [], checks: { head1: [{ id: 1, name: "xcodebuild (unsigned)", status: "completed", conclusion: "success" }] } }) };
  const builds = { 111: [ascBuild(1, "202610100000", "1.0.5", "2026-10-10T00:05:00Z")] };
  const first = fakeDeps({ repos: world, builds, state: { apps: { b: { behindSince: "2026-10-17T00:00:00Z" } } }, failPost: true });
  const out = sink();
  const code = await main(["--ship-behind", "--alert", "--json", "--registry", writeRegistry(reg)], first.deps, out);
  assert.equal(code, EXIT_BEHIND);
  const rep = JSON.parse(out.text);
  assert.equal(rep.apps[0].remedy.action, "dispatched");
  assert.equal(first.calls.dispatch.length, 1);
  assert.equal(rep.alert, undefined, "no did-not-land alert in the run that dispatched");
  const saved = first.calls.states.at(-1);
  assert.equal(saved.apps.b.remedy.headSha, "head1");

  // Four hours later the catch-up has not landed:  the alert is due, the post fails.
  const later = fakeDeps({ repos: world, builds, state: saved, failPost: true });
  later.deps.now = () => new Date(NOW.getTime() + 4 * 3600e3);
  const out2 = sink();
  assert.equal(await main(["--ship-behind", "--alert", "--json", "--registry", writeRegistry(reg)], later.deps, out2), EXIT_BEHIND);
  const rep2 = JSON.parse(out2.text);
  assert.equal(rep2.apps[0].remedy.action, "blocked", "one dispatch per main HEAD");
  assert.equal(later.calls.dispatch.length, 0);
  assert.equal(rep2.alert.posted, false);
  assert.match(rep2.alert.error, /agent-sync/);
  assert.match(rep2.alert.body, /has not landed/);
  assert.equal(later.calls.states.at(-1).apps.b.alert, undefined, "alert stamp rolled back so the next run retries");
});

test("main --app --ship-behind still sees another app's ship in flight", async () => {
  const reg = registry({ b: app({ repo: "B" }), other: app({ repo: "O", appleId: "999" }) });
  const { deps, calls } = fakeDeps({
    repos: {
      B: botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T14:15:00Z" } }, createdRuns: [] }),
      O: { recentRuns: [run("o1", "2026-10-18T00:00:00Z", "2026-10-18T00:05:00Z", { status: "in_progress", conclusion: null })] },
    },
    builds: { 111: [ascBuild(1, "202610100000", "1.0.5", "2026-10-10T00:05:00Z")] },
  });
  const out = sink();
  await main(["--app", "b", "--ship-behind", "--dry-run", "--json", "--registry", writeRegistry(reg)], deps, out);
  const rep = JSON.parse(out.text);
  assert.equal(rep.apps[0].remedy.action, "deferred");
  assert.match(rep.apps[0].remedy.reason, /already running \(other\)/);
  assert.equal(calls.dispatch.length, 0);
});

// ---------------------------------------------------------------------------
// Review fixes (PR 617)
// ---------------------------------------------------------------------------

test("a catch-up dispatched this run, or inside the landing window, is not called a failure", () => {
  const reg = registry({ x: app() });
  const state = { apps: { x: { behindSince: "2026-10-17T10:00:00Z", remedy: { at: NOW.toISOString(), headSha: "head1", targetSha: "nat2" } } } };
  const r = behindResult("x", { remedy: { action: "dispatched", reason: "gh workflow run ios-ship.yml --repo Org/Demo --ref main" } });
  assert.equal(planAlerts([r], state, reg, { now: NOW, exitCode: 1 }), null, "dispatched seconds ago");
  assert.equal(state.apps.x.alert, undefined, "no alert key is stored, so a later real failure still alerts");
  const in2h = new Date(NOW.getTime() + 2 * 3600e3);
  assert.equal(planAlerts([behindResult("x")], state, reg, { now: in2h, exitCode: 1 }), null, "still inside the 3 hour landing window");
  const in4h = new Date(NOW.getTime() + 4 * 3600e3);
  const plan = planAlerts([behindResult("x")], state, reg, { now: in4h, exitCode: 1 });
  assert.deepEqual(plan.apps, ["x"]);
  assert.match(plan.body, /has not landed/);
});

test("an in-flight ship run older than the cap is stale:  not shipping, not blocking, and it alerts", async () => {
  const stuck = run("head1", "2026-10-17T19:00:00Z", "2026-10-17T19:01:00Z", { status: "in_progress", conclusion: null });
  const w = botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T14:15:00Z" } }, recentRuns: [stuck] });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(r.status, "behind", "a run in progress for 5h15m no longer counts as shipping");
  assert.equal(r.pipeline.inflight.length, 0);
  assert.equal(r.pipeline.staleRuns.length, 1);
  assert.match(r.reason, /ship run in flight for 5\.3h/);
  const fresh = run("head1", "2026-10-18T00:00:00Z", "2026-10-18T00:01:00Z", { status: "queued", conclusion: null });
  assert.equal((await evalOne({ ...w, recentRuns: [fresh] }, BOTFLEET_BUILDS)).r.status, "shipping");

  const state = { apps: { x: { behindSince: "2026-10-17T10:00:00Z" } } };
  const plan = planAlerts([{ ...r, key: "x" }], state, registry({ x: app() }), { now: NOW, exitCode: 1 });
  assert.deepEqual(plan.apps, ["x"]);
  assert.match(plan.body, /5\.3h/);

  const reg = registry({ b: app({ repo: "B" }), other: app({ repo: "O", appleId: "999" }) });
  const { deps, calls } = fakeDeps({
    repos: { B: botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T14:15:00Z" } }, createdRuns: [] }), O: { recentRuns: [stuck] } },
    builds: { 111: [ascBuild(1, "202610100000", "1.0.5", "2026-10-10T00:05:00Z")] },
  });
  const out = sink();
  await main(["--app", "b", "--ship-behind", "--dry-run", "--json", "--registry", writeRegistry(reg)], deps, out);
  assert.equal(JSON.parse(out.text).apps[0].remedy.action, "would-dispatch", "another app's stuck run does not block the fleet");
  assert.equal(calls.dispatch.length, 0);
});

test("two stuck runs:  the reason and the alert quote the oldest run's age with that run's link", async () => {
  const newer = run("head1", "2026-10-17T20:00:00Z", "2026-10-17T20:01:00Z", { status: "in_progress", conclusion: null, html_url: "https://github.com/run/newer" });
  const older = run("head0", "2026-10-17T18:00:00Z", "2026-10-17T18:01:00Z", { status: "queued", conclusion: null, html_url: "https://github.com/run/older" });
  const w = botfleetLike({ natives: { ios: { sha: "nat2", date: "2026-10-17T14:15:00Z" } }, recentRuns: [newer, older] });
  const { r } = await evalOne(w, BOTFLEET_BUILDS);
  assert.equal(oldestRun(r.pipeline.staleRuns).url, "https://github.com/run/older");
  assert.match(r.reason, /in flight for 6\.3h looks stuck \(https:\/\/github\.com\/run\/older\)/);
  const state = { apps: { x: { behindSince: "2026-10-17T10:00:00Z" } } };
  const plan = planAlerts([{ ...r, key: "x" }], state, registry({ x: app() }), { now: NOW, exitCode: 1 });
  assert.match(plan.body, /in flight for 6\.3h and looks stuck \(https:\/\/github\.com\/run\/older\)/);
});

test("an app with autoRemedy on must name a compile check:  the registry fails loudly", async () => {
  assert.throws(() => loadRegistry(writeRegistry(registry({ x: app({ compileCheck: null }) }))), /x: autoRemedy is on but compileCheck is empty/);
  loadRegistry(writeRegistry(registry({ x: app({ compileCheck: null, autoRemedy: false, manualReason: "none" }) })));
  loadRegistry(writeRegistry(registry({ x: app({ compileCheck: null, skip: "Sparkle" }) })));
  const out = sink();
  assert.equal(await main(["--registry", writeRegistry(registry({ x: app({ compileCheck: null }) }))], fakeDeps().deps, out), EXIT_FAILED);
  assert.match(out.text, /compileCheck is empty/);
});

test("the run lock:  held, reclaimed when stale, and a lost reclaim race returns null instead of throwing", () => {
  const dir = path.join(fs.mkdtempSync(path.join(process.env.TMPDIR || "/tmp", "tf-lock-")), "freshness.lockdir");
  const release = acquireLockDir(dir);
  assert.equal(typeof release, "function");
  assert.equal(acquireLockDir(dir), null, "a fresh lock is held");
  assert.equal(typeof acquireLockDir(dir, { nowMs: Date.now() + 31 * 60e3 }), "function", "a 31 minute old lock is reclaimed");
  release();

  // Another run re-creates the lock between our rmSync and our mkdirSync.
  const eexist = () => Object.assign(new Error("EEXIST: file already exists"), { code: "EEXIST" });
  const racing = {
    mkdirSync() { throw eexist(); },
    statSync: () => ({ mtimeMs: 0 }),
    rmSync() {},
  };
  assert.equal(acquireLockDir("/x/freshness.lockdir", { fsx: racing, nowMs: 3600e3 }), null);
  const denied = { ...racing, mkdirSync() { throw Object.assign(new Error("EACCES"), { code: "EACCES" }); } };
  assert.throws(() => acquireLockDir("/x/freshness.lockdir", { fsx: denied }), /EACCES/, "a real error is not mistaken for a held lock");
});

// ---------------------------------------------------------------------------
// The tracked registry
// ---------------------------------------------------------------------------

test("testflight-apps.json is complete and carries no gate override", () => {
  const reg = loadRegistry(path.join(HERE, "testflight-apps.json"));
  assert.equal(reg.rateGateHours, 2.5);
  assert.equal(reg.staleAlertHours, 6);
  const ids = new Set();
  for (const [key, a] of Object.entries(reg.apps)) {
    assert.ok(/^\d+$/.test(String(a.appleId)), `${key}: appleId`);
    assert.ok(!ids.has(a.appleId), `${key}: duplicate appleId`);
    ids.add(a.appleId);
    assert.ok(a.repo && Array.isArray(a.paths) && a.paths.length, `${key}: repo and paths`);
    if (a.skip) continue;
    assert.ok(a.ship && /\.ya?ml$/.test(a.ship.workflow), `${key}: ship workflow`);
    assert.ok(!/appstore-gm/.test(a.ship.workflow), `${key}: never the App Store GM workflow`);
    for (const k of Object.keys(a.ship.inputs || {})) assert.doesNotMatch(k, /force|interval|gate/i, `${key}: no gate override input`);
    if (a.autoRemedy !== false) assert.ok(a.compileCheck, `${key}: autoRemedy needs a compile check`);
    assert.ok(["IOS", "MAC_OS"].includes(a.platform), `${key}: platform`);
  }
});
