import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import {
  registryIngestArgs, summarizeIngest, summarizeIngestFailure, ingestFailureDetail, buildMarkdownMirror,
  MIRROR_MD_PATH, FLEET_HOME, REGISTRY_INGEST_SOURCE,
  readIngestKnobs, ingestFailureOutcome, fleetKnob, ingestEnabled, ingestTimeoutMs, INGEST_ENABLED_KNOB, INGEST_TIMEOUT_KNOB
} from "./fleet-apps-sheet-sync.mjs";

const read = (rel) => readFileSync(new URL(`../${rel}`, import.meta.url), "utf8");

// The `recall` CLI is a local Mac tool, so it is not on PATH in CI.  The tests that need it
// skip themselves there;  the tests that do not need it run everywhere.
const RECALL = spawnSync("which", ["recall"], { encoding: "utf8" }).status === 0;

test("the Fleet RAG ingest names a SOURCE, never a file path", () => {
  const args = registryIngestArgs();
  assert.deepEqual(args, ["ingest", "--source", REGISTRY_INGEST_SOURCE]);
  assert.equal(REGISTRY_INGEST_SOURCE, "doc");
  // The old call passed the mirror's path to a source-based CLI.  Its argparse rejects any
  // positional argument, so that call always exited 2 and this step was a silent no-op.
  const pathish = args.filter((a) => a.includes("/") || a.endsWith(".md"));
  assert.deepEqual(pathish, [], "no path is passed to a source-based CLI");
});

test("the mirror is a top-level ~/apps markdown file, which is what the doc source ingests", () => {
  // The doc source owns APPS_DIR (= HOME / "apps") and globs its top-level *.md files
  // (fleet_rag/sources.py:  live += sorted(apps.glob("*.md"))).  The mirror has to live there,
  // and under that name, or the `doc` source never carries it and the ingest above is a no-op.
  // These assertions are HOME-independent on purpose:  the mirror is pinned to the fleet account's
  // tree, so the CI runner's own HOME is not the reference and the test says nothing about it.
  const parent = MIRROR_MD_PATH.slice(0, MIRROR_MD_PATH.lastIndexOf("/"));
  assert.equal(parent.split("/").pop(), "apps", "the mirror lives in the apps dir the doc source globs");
  assert.equal(MIRROR_MD_PATH.split("/").length, parent.split("/").length + 1, "top level of ~/apps");
  assert.ok(MIRROR_MD_PATH.endsWith(".md"));
  assert.equal(MIRROR_MD_PATH.split("/").pop(), "FLEET-APPS-REGISTRY.md");

  const sources = read("scripts/fleet_rag/sources.py");
  assert.match(sources, /APPS_DIR = HOME \/ "apps"/, "the doc source's apps dir is ~/apps");
  assert.match(sources, /apps\.glob\("\*\.md"\)/, "the doc source still ingests top-level ~/apps/*.md");

  // The source also drops any file over its own size cap, so a mirror that grows past the cap
  // would stop being ingested and this step would go silent again.  Assert the cap holds.
  const cap = Number((sources.match(/DOC_MAX_BYTES = ([\d_]+) \* ([\d_]+)/) || [])
    .slice(1)
    .reduce((a, b) => Number(String(a).replace(/_/g, "")) * Number(String(b).replace(/_/g, "")), 1));
  assert.ok(cap > 0, "the doc source's size cap is readable");
  if (existsSync(MIRROR_MD_PATH)) {
    assert.ok(
      statSync(MIRROR_MD_PATH).size <= cap,
      `the mirror is under the doc source's ${cap}-byte cap`
    );
  }
});

test("the mirror body carries no timestamp, so the ingest's hash skip can match", () => {
  // The ingest skips a document whose content hash is unchanged.  A "Last Synced" stamp in the
  // mirror body changed that hash on every run, so the skip never matched and every sheet sync
  // re-embedded the mirror even when nothing about the registry had changed.
  const tabs = {
    registry: [["A"], ["b"], []],
    testflight: [["A"], []], icons: [[], [], [], ["A"], []], fleetApps: [["A"], []], seats: [["A"], []],
  };
  const first = buildMarkdownMirror({ sections: [] }, tabs);
  const second = buildMarkdownMirror({ sections: [] }, tabs);
  assert.equal(first, second, "two mirrors of the same data are byte-identical");
  assert.doesNotMatch(first, /^\*\*Last Synced:\*\*/m, "the mirror body has no volatile timestamp");
  assert.doesNotMatch(first, /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/, "the mirror body has no ISO timestamp");
});

test("the ingest child's home is the one the mirror is written into", () => {
  // The write target is pinned to the fleet account's apps dir, but sources.py resolves APPS_DIR
  // from the INGEST CHILD's home (Path.home() reads the inherited $HOME).  If the child keeps an
  // ambient HOME -- sudo -H, a wrapper, a CI job -- the two sides disagree and the mirror is
  // written outside the tree the doc source globs, which is the silent no-op this PR fixes.
  // So the ingest call pins HOME to the fleet home; assert the pairing holds by construction.
  assert.equal(
    MIRROR_MD_PATH,
    `${FLEET_HOME}/apps/FLEET-APPS-REGISTRY.md`,
    "the mirror lands in the fleet home's apps dir"
  );
  assert.notEqual(FLEET_HOME, "", "the fleet home is a real path, not the empty ambient $HOME");
  assert.ok(FLEET_HOME.startsWith("/"), "the fleet home is absolute");

  // And the child really is spawned with that HOME, rather than only exporting the constant.
  const source = read("scripts/fleet-apps-sheet-sync.mjs");
  assert.match(source, /env: \{ \.\.\.process\.env, HOME: FLEET_HOME \}/,
    "the ingest child inherits the pinned fleet home");
});

test("the ingest's skip flag and timeout are fleet knobs, not ambient env vars", () => {
  // AGENTS.md:  Infisical is the sole source of truth for the Mac's tunable knobs, and a CLI's
  // caps and flags come through scripts/fleet_knobs (one fetch, last-known-good, never a plain
  // environment read).  So both controls are registered there and read through its CLI.
  const source = read("scripts/fleet-apps-sheet-sync.mjs");
  assert.doesNotMatch(source, /process\.env\.FLEET_SHEET_INGEST/,
    "neither ingest control is read from the environment");
  assert.match(source, /from fleet_knobs import knobs/,
    "the knobs are read through the fleet_knobs CLI");

  const registry = read("scripts/fleet_knobs/registry.py");
  for (const name of [INGEST_ENABLED_KNOB, INGEST_TIMEOUT_KNOB]) {
    assert.ok(registry.includes(`"${name}"`), `${name} is registered in fleet_knobs`);
    assert.ok(read("INFISICAL.md").includes(`| \`${name}\` |`),
      `${name} is in the INFISICAL.md inventory`);
  }

  // A non-secret read never fails the job:  with no `infisical` CLI on PATH (as in CI) the
  // registry default answers rather than the sync dying.
  const settings = readIngestKnobs((_bin, args, options) => {
    assert.equal(options.env.HOME, FLEET_HOME);
    assert.ok(args.includes(INGEST_ENABLED_KNOB) && args.includes(INGEST_TIMEOUT_KNOB));
    return JSON.stringify({ [INGEST_ENABLED_KNOB]: false, [INGEST_TIMEOUT_KNOB]: 60000 });
  });
  assert.equal(settings[INGEST_ENABLED_KNOB], "false");
  assert.equal(settings[INGEST_TIMEOUT_KNOB], "60000");
  assert.equal(fleetKnob("NOT_A_KNOB"), undefined);
});

test("summarizeIngest reads the ingest report and never fakes a success", () => {
  const report = JSON.stringify({
    per_source: { doc: { docs_seen: 142, docs_changed: 1, chunks_new: 37 } }
  });
  assert.equal(summarizeIngest(report), "142 docs, 1 changed, 37 chunks written");
  // A report without the doc source must NOT read as a successful "done":  the old wording made a
  // malformed report look like a clean run, which is the opposite of what a summary is for.
  assert.equal(summarizeIngest(JSON.stringify({ per_source: {} })), "report has no doc source");
  assert.equal(summarizeIngest(JSON.stringify({})), "report has no doc source");
  // Garbage or empty stdout must not throw.
  assert.equal(summarizeIngest(""), "no output");
  assert.equal(summarizeIngest("not json at all"), "not json at all");
  // A count that is not a number is not rendered as one.
  assert.equal(
    summarizeIngest(JSON.stringify({ per_source: { doc: { docs_seen: "many" } } })),
    "no counts in the report"
  );
});

test("a failed ingest reports the tool's own stderr, not just the exit", () => {
  // The CLI's stderr is the only thing that says WHY an ingest failed.  A note that carries only
  // "Command failed" leaves the operator with nothing to act on, which is what a reviewer on this
  // PR caught:  the old code discarded stderr and printed a generic message.
  const err = Object.assign(new Error("Command failed: recall ingest --source doc"), {
    stderr: "ingest lock is held by pid 69239\nwaiting for the ingest lock\n",
  });
  const detail = ingestFailureDetail(err);
  assert.match(detail, /ingest lock is held by pid 69239/);
  assert.match(detail, /waiting for the ingest lock/);
  // A failure with no stderr still says something rather than nothing.
  assert.equal(ingestFailureDetail(Object.assign(new Error("boom"), { stderr: "" })), "boom");
  assert.equal(ingestFailureDetail({}), "no output");
  // Only the tail is kept, so a chatty tool cannot flood the operator's log.
  const chatty = Object.assign(new Error("boom"), { stderr: "a\nb\nc\nd\n" });
  assert.equal(ingestFailureDetail(chatty), "b | c | d");
});

test("the real recall CLI rejects a file path and accepts a source", { skip: !RECALL }, () => {
  // Proof of the contract this fix relies on:  the old argv is an argparse error, and the new
  // argv is the documented form.  Both are answered by argparse before any ingest work starts.
  const oldStyle = spawnSync("recall", ["ingest", MIRROR_MD_PATH], { encoding: "utf8" });
  assert.notEqual(oldStyle.status, 0, "the old file-path call must not succeed");
  assert.match(oldStyle.stderr, /unrecognized arguments/);

  const help = spawnSync("recall", ["ingest", "--help"], { encoding: "utf8" });
  assert.equal(help.status, 0);
  assert.match(help.stdout, /--source/, "the CLI documents --source");
  // A positional argument would appear in the usage line as a bare word.
  const usage = help.stdout.split("\n").filter((l) => /usage: recall ingest|^\s+\[/.test(l)).join(" ");
  const bare = usage
    .replace(/^usage:\s*recall\s+ingest\s*/, "")
    .split(/\s+/)
    .find((t) => t && !t.startsWith("[") && !t.startsWith("-") && !t.endsWith("]") && !t.startsWith("]"));
  assert.equal(bare, undefined, `the ingest command must take no positional argument (saw ${bare})`);
});


test("failed JSON reports retain source stderr alongside counts", () => {
  const detail = summarizeIngestFailure({ stdout: JSON.stringify({ per_source: { doc: { docs_seen: 3 } } }), stderr: "[doc] ERROR synthetic source failure" });
  assert.match(detail, /3 docs/);
  assert.match(detail, /synthetic source failure/);
});


test("ingest process timeouts have an unknown side-effect outcome", () => {
  assert.equal(ingestFailureOutcome({ code: "ETIMEDOUT" }), "outcome_unknown");
  assert.equal(ingestFailureOutcome({ status: 1 }), "failed");
  assert.equal(ingestFailureOutcome({ signal: "SIGTERM" }), "outcome_unknown");
});


test("unavailable knob reader reports registered-default fallback", () => {
  const saved = console.warn;
  const warnings = [];
  console.warn = (message) => warnings.push(message);
  try {
    const settings = readIngestKnobs(() => { throw new Error("synthetic"); });
    assert.equal(settings[INGEST_ENABLED_KNOB], "true");
    assert.equal(settings[INGEST_TIMEOUT_KNOB], "5400000");
    assert.match(warnings.join(" "), /registered defaults/);
  } finally { console.warn = saved; }
});
