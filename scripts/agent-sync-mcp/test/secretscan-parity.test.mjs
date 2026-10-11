// The Worker's secret scanner (src/contract.js scanSecret) against the Python one (scripts/agent_sync/secretscan.py),
// over the Python suite's own samples (scripts/agent_sync/tests/test_secretscan_fleet.py):  every fleet token kind
// must be refused here with the same kind name, and ordinary text that only looks a little like a token must pass.
// Same shape as textfmt.test.mjs.  Skips itself where python3 is missing.
//
// Grok Bot's cloud routines post through this Worker, and they are the ones that hold the read-only Linear, GitHub
// and fleet-loops-data tokens, so a kind that only the stdio CLI knew would leave the hosted path open.

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { scanSecret } from "../src/contract.js";
import { ROOT } from "./helpers.mjs";

const SCRIPTS = path.resolve(ROOT, "..");
const PROGRAM = [
  "import json",
  "from agent_sync.secretscan import scan",
  "from agent_sync.tests import test_secretscan_fleet as t",
  "samples = [[kind, text, scan(text)] for kind, text in t.samples()]",
  "clean = [[text, scan(text)] for text in t.clean_texts()]",
  "print(json.dumps({'samples': samples, 'clean': clean}))",
].join("\n");
const dump = spawnSync("python3", ["-c", PROGRAM], {
  cwd: SCRIPTS,
  encoding: "utf8",
  env: { ...process.env, PYTHONPATH: SCRIPTS },
});
const tables = dump.status === 0 ? JSON.parse(dump.stdout) : null;
const skip = tables ? false : `python3 or the Python suite is unavailable (status ${dump.status})`;

test("every fleet token kind the Python scanner knows is refused here, with the same kind", { skip }, () => {
  assert.ok(tables.samples.length >= 20);
  const kinds = new Set();
  for (const [kind, text, python] of tables.samples) {
    assert.equal(python, kind, "the Python scanner names the sample's kind");
    assert.equal(scanSecret(text), kind, `Worker: ${kind} in ${JSON.stringify(text.slice(0, 14))}`);
    kinds.add(kind);
  }
  for (const kind of ["Linear API key", "Linear client secret", "Sentry token", "xAI key", "Infisical service token",
    "Infisical credential", "access token (JWT)", "Cloudflare token", "Coolify token", "assigned credential"]) {
    assert.ok(kinds.has(kind), `the samples cover ${kind}`);
  }
});

test("ordinary text that only looks like a token passes here as it does in Python", { skip }, () => {
  assert.ok(tables.clean.length >= 25);
  for (const [text, python] of tables.clean) {
    assert.equal(python, null);
    assert.equal(scanSecret(text), null, `Worker flagged ${JSON.stringify(text.slice(0, 30))}`);
  }
});

test("a token inside prose, markdown and a code block is still refused", () => {
  const token = ["lin_", "api_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"].join("");
  for (const text of [`see ${token} for it`, `\`${token}\``, `\`\`\`\nexport K=${token}\n\`\`\``, `(${token})`, `> ${token}`, `a\n\n${token}\nb`]) {
    assert.equal(scanSecret(text), "Linear API key", JSON.stringify(text.slice(0, 20)));
  }
});

test("long adversarial inputs do not blow up", () => {
  const nasty = ["linear ".repeat(3000), "cloudflare " + "a".repeat(60000), "infisical " + "_".repeat(60000),
    "st." + "a".repeat(60000), "1|" + "a".repeat(60000), "eyJ" + "a".repeat(60000), "A".repeat(100000),
    "FOO_".repeat(20000) + "TOKEN =", "sntrys_" + "=".repeat(60000)];
  const start = performance.now();
  for (const text of nasty) scanSecret(text);
  assert.ok(performance.now() - start < 5000, "under five seconds for all of them");
});
