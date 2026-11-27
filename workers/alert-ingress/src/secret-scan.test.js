// The ingress's credential scanner against the fleet's Python one
// (scripts/simple_tools/secretscan.py), over the Python suite's own samples
// (scripts/simple_tools/tests/test_secretscan_fleet.py):  every fleet token
// kind must be refused here with the same kind name, and ordinary text that
// only looks a little like a token must pass.
//
// The ingress keeps its own copy of the list because it ships without a
// bundler and cannot import the MCP Worker's contract without pulling that
// worker's tool schemas into the bundle.  This test is what keeps the copy
// honest:  it fails the moment the three lists drift apart.
//
// Locally it skips itself where python3 is missing or too old for the Python suite.
// Under CI (CI is set) nothing is skipped:  a missing or broken Python, or output that
// is not the expected JSON, fails the first parity test with the reason, so the drift
// check cannot go quiet.  A dump that parses badly fails everywhere, never skips.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { scanForCredential } from "./secret-scan.js";
import { scanSecret as scanMcp } from "../../../scripts/simple-tools-mcp/src/contract.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../../..");
const SCRIPTS = path.join(ROOT, "scripts");

const PROGRAM = [
  "import json",
  "from simple_tools.secretscan import scan",
  "from simple_tools.tests import test_secretscan_fleet as t",
  "samples = [[kind, text, scan(text)] for kind, text in t.samples()]",
  "clean = [[text, scan(text)] for text in t.clean_texts() if text not in t.source_expressions()]",
  "source = t.source_expressions()",
  "print(json.dumps({'samples': samples, 'clean': clean, 'source': source}))",
].join("\n");
// The dump is read inside a try:  a throw at the top level of a test file takes every test
// in it down with an unreadable stack, and a bad dump must name itself instead.
function loadTables() {
  const dump = spawnSync("python3", ["-c", PROGRAM], {
    cwd: SCRIPTS,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: SCRIPTS },
  });
  if (dump.status !== 0) {
    const why = dump.error
      ? `python3 could not run (${dump.error.code ?? "error"})`
      : `python3 or the Python suite is unavailable (status ${dump.status})`;
    return { tables: null, why, unavailable: true };
  }
  return parseDump(dump.stdout);
}
// Never throws:  an unusable dump is a result that fails the first parity test.
function parseDump(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    if (!parsed || !Array.isArray(parsed.samples) || !Array.isArray(parsed.clean) || !Array.isArray(parsed.source)) {
      return { tables: null, why: "the Python dump is not the expected shape", unavailable: false };
    }
    return { tables: parsed, why: "", unavailable: false };
  } catch {
    return { tables: null, why: "the Python dump is not valid JSON", unavailable: false };
  }
}
const loaded = loadTables();
const tables = loaded.tables;
// Skipping is only for a developer machine without a usable Python.  CI never skips.
const skip = !tables && loaded.unavailable && !process.env.CI ? loaded.why : false;
const needTables = () => assert.ok(tables, `the Python scanner tables did not load: ${loaded.why}`);

test("every fleet token kind the Python scanner knows is refused here, with the same kind", { skip }, () => {
  needTables();
  assert.ok(tables.samples.length >= 20);
  const kinds = new Set();
  for (const [kind, text, python] of tables.samples) {
    assert.equal(python, kind, "the Python scanner names the sample's kind");
    assert.equal(
      scanForCredential(text),
      kind,
      `ingress: ${kind} in ${JSON.stringify(text.slice(0, 14))}`,
    );
    // The third copy:  the hosted MCP Worker's.  All three must name the same kind.
    assert.equal(
      scanMcp(text),
      kind,
      `MCP Worker: ${kind} in ${JSON.stringify(text.slice(0, 14))}`,
    );
    kinds.add(kind);
  }
  for (const kind of [
    "Linear API key",
    "Linear client secret",
    "Sentry token",
    "xAI key",
    "Infisical service token",
    "Infisical credential",
    "access token (JWT)",
    "Cloudflare token",
    "Coolify token",
    "assigned credential",
  ]) {
    assert.ok(kinds.has(kind), `the samples cover ${kind}`);
  }
});

test("ordinary text that only looks like a token passes here as it does in Python", { skip }, () => {
  needTables();
  assert.ok(tables.clean.length >= 25);
  for (const [text, python] of tables.clean) {
    assert.equal(python, null);
    assert.equal(scanMcp(text), null, `MCP Worker flagged ${JSON.stringify(text.slice(0, 30))}`);
    assert.equal(
      scanForCredential(text),
      null,
      `ingress flagged ${JSON.stringify(text.slice(0, 30))}`,
    );
  }
});

test("a token inside prose, markdown and a code block is still refused", () => {
  const sample = "lin_" + "api_" + "Ab12".repeat(8); // built at run time and low-entropy:  gitleaks runs on this repo
  for (const text of [
    `see ${sample} for it`,
    `\`${sample}\``,
    `\`\`\`\nexport K=${sample}\n\`\`\``,
    `(${sample})`,
    `> ${sample}`,
    `a\n\n${sample}\nb`,
  ]) {
    assert.equal(scanForCredential(text), "Linear API key", JSON.stringify(text.slice(0, 20)));
  }
});

test("the answer names the kind and never the value", () => {
  const sample = "lin_" + "api_" + "Ab12".repeat(8);
  const kind = scanForCredential(`token is ${sample}`);
  assert.equal(kind, "Linear API key");
  assert.equal(kind.includes(sample), false);
});

test("long adversarial inputs do not blow up", () => {
  const nasty = [
    "linear ".repeat(3000),
    "cloudflare " + "a".repeat(60000),
    "infisical " + "_".repeat(60000),
    "st." + "a".repeat(60000),
    "1|" + "a".repeat(60000),
    "eyJ" + "a".repeat(60000),
    "A".repeat(100000),
    "FOO_".repeat(20000) + "TOKEN =",
    "sntrys_" + "=".repeat(60000),
  ];
  const start = performance.now();
  for (const text of nasty) scanForCredential(text);
  assert.ok(performance.now() - start < 5000, "under five seconds for all of them");
});

// Alert redaction is deliberately stricter than source-publication precision:
// preserve masking of environment references in alerts, rather than weakening it
// merely to make a source-code corpus pass this unrelated redaction consumer.
test("source assignment expressions stay conservatively masked in alert ingress", { skip }, () => {
  needTables();
  assert.ok(tables.source.length >= 3);
  for (const text of tables.source) assert.ok(scanForCredential(text));
});

// An unusable dump is a failure, not a skip:  only a missing Python may skip, and never under CI.
test("a Python dump that is not JSON or not the expected shape is a failure, not a skip", () => {
  for (const [stdout, why] of [["not json", "not valid JSON"], ["", "not valid JSON"], ["{}", "not the expected shape"], ["null", "not the expected shape"]]) {
    const result = parseDump(stdout);
    assert.equal(result.tables, null);
    assert.ok(result.why.includes(why), result.why);
    assert.equal(result.unavailable, false, "only a missing Python may skip");
  }
  assert.ok(parseDump('{"samples":[],"clean":[],"source":[]}').tables);
});
