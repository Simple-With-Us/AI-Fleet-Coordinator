import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import {
  registryIngestArgs, summarizeIngest, MIRROR_MD_PATH, REGISTRY_INGEST_SOURCE
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
  const appsDir = `${homedir()}/apps`;
  assert.equal(MIRROR_MD_PATH, `${appsDir}/FLEET-APPS-REGISTRY.md`);
  assert.equal(MIRROR_MD_PATH.split("/").length, appsDir.split("/").length + 1, "top level of ~/apps");
  assert.ok(MIRROR_MD_PATH.endsWith(".md"));

  const sources = read("scripts/fleet_rag/sources.py");
  assert.match(sources, /APPS_DIR = HOME \/ "apps"/, "the doc source's apps dir is ~/apps");
  assert.match(sources, /apps\.glob\("\*\.md"\)/, "the doc source still ingests top-level ~/apps/*.md");
});

test("summarizeIngest reads the ingest report and degrades safely", () => {
  const report = JSON.stringify({
    per_source: { doc: { docs_seen: 142, docs_changed: 1, chunks_new: 37 } }
  });
  assert.equal(summarizeIngest(report), "142 docs, 1 changed, 37 chunks written");
  // A report without the doc source (a different source ran) still reads as a summary.
  assert.equal(summarizeIngest(JSON.stringify({ per_source: {} })), "done");
  // Garbage or empty stdout must not throw.
  assert.equal(summarizeIngest(""), "done");
  assert.equal(summarizeIngest("not json at all"), "not json at all");
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
