// The outbound sentence gap:  the JS port (src/textfmt.js) against the Python
// suite's own tables (scripts/agent_sync/tests/test_textfmt.py), so the two
// transports post the same text.  Skips itself where python3 is missing.

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { sentenceGap, GAP } from "../src/textfmt.js";
import { ROOT } from "./helpers.mjs";

const SCRIPTS = path.resolve(ROOT, "..");
const dump = spawnSync(
  "python3",
  ["-c", "import json\nfrom agent_sync.tests import test_textfmt as t\nprint(json.dumps({'converted': t.CONVERTED, 'unchanged': t.UNCHANGED}))"],
  { cwd: SCRIPTS, encoding: "utf8", env: { ...process.env, PYTHONPATH: SCRIPTS } },
);
const tables = dump.status === 0 ? JSON.parse(dump.stdout) : null;
const skip = tables ? false : `python3 or the Python suite is unavailable (status ${dump.status})`;

test("the Python tables:  every converted row", { skip }, () => {
  assert.ok(tables.converted.length >= 30);
  for (const [name, text, expected] of tables.converted) assert.equal(sentenceGap(text), expected, name);
});

test("the Python tables:  every unchanged row", { skip }, () => {
  assert.ok(tables.unchanged.length >= 30);
  for (const [name, text] of tables.unchanged) assert.equal(sentenceGap(text), text, name);
});

test("closers chain, a brace does not close, idempotent, never longer", () => {
  assert.equal(sentenceGap('Done.)"  Next'), `Done.)"${GAP}Next`);
  assert.equal(sentenceGap("Done.”)**  Next"), `Done.”)**${GAP}Next`);
  assert.equal(sentenceGap("Done.}  Next"), "Done.}  Next");
  for (const text of ["One.  Two.   Three.", "```\nx.  y\n```\nz.  w", "a `b.  c` d.  e"]) {
    const once = sentenceGap(text);
    assert.equal(sentenceGap(once), once);
    assert.ok(once.length <= text.length);
  }
});
