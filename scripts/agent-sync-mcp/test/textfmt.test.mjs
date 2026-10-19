// The outbound text fixes:  the JS port (src/textfmt.js) against the Python
// suite's own tables (scripts/agent_sync/tests/test_textfmt.py), so the two
// transports post the same text.  Skips itself where python3 is missing.

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fixOutbound, fleetDates, sentenceGap, singleGaps, GAP } from "../src/textfmt.js";
import { ROOT } from "./helpers.mjs";

const SCRIPTS = path.resolve(ROOT, "..");
const dump = spawnSync(
  "python3",
  [
    "-c",
    "import json\nfrom agent_sync.tests import test_textfmt as t\nprint(json.dumps({'converted': t.CONVERTED, 'unchanged': t.UNCHANGED, 'now': t.NOW_ISO, 'single_gapped': t.SINGLE_GAPPED, 'single_unchanged': t.SINGLE_UNCHANGED, 'fleet_dates': t.FLEET_DATES, 'fleet_dates_kept': t.FLEET_DATES_KEPT, 'fleet_offsets': t.FLEET_OFFSET_FORMS}))",
  ],
  { cwd: SCRIPTS, encoding: "utf8", env: { ...process.env, PYTHONPATH: SCRIPTS } },
);
const tables = dump.status === 0 ? JSON.parse(dump.stdout) : null;
const skip = tables ? false : `python3 or the Python suite is unavailable (status ${dump.status})`;
const NOW = tables ? new Date(tables.now) : null;

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

test("the Python tables:  one-space gaps widen, abbreviations and list markers do not", { skip }, () => {
  assert.ok(tables.single_gapped.length >= 5);
  for (const [text, expected] of tables.single_gapped) assert.equal(singleGaps(text)[0], expected, text);
  for (const text of tables.single_unchanged) assert.deepEqual(singleGaps(text), [text, 0], text);
});

test("the Python tables:  ISO dates in prose take the fleet form", { skip }, () => {
  assert.ok(tables.fleet_dates.length >= 2);
  for (const [text, expected] of tables.fleet_dates) assert.equal(fleetDates(text, NOW)[0], expected, text);
  for (const kept of tables.fleet_dates_kept) assert.deepEqual(fleetDates(kept, NOW), [kept, []], kept);
});

test("the Python tables:  a space-form offset is a UTC offset, not a range end", { skip }, () => {
  assert.ok(tables.fleet_offsets.length >= 6);
  for (const [text, expected] of tables.fleet_offsets) {
    assert.equal(fleetDates(text, NOW)[0], expected, text);
    assert.doesNotMatch(fleetDates(text, NOW)[0], /[:+-]\d{2}:\d{2}$/, text);
  }
});

test("fixOutbound:  both fixes, a report, the length cap and idempotence", () => {
  const [fixed, report] = fixOutbound("Merged 2026-10-19. Next.", { now: NOW });
  assert.equal(fixed, `Merged Mon, Oct 19.${GAP}Next.`);
  assert.equal(report.length, 2);
  assert.deepEqual(fixOutbound("A.  B.", { now: NOW }), [`A.${GAP}B.`, []]);

  // Over the cap:  only the length-safe double-space conversion survives.
  const long = "x".repeat(9995) + ". Next";
  assert.deepEqual(fixOutbound(long, { limit: 10000, now: NOW }), [long, []]);

  // Idempotent, so applying it at both the body and the transport layer is safe.
  for (const text of ["Merged 2026-10-19. Next. Dr. Smith went. 1. Item", "One.  Two.   Three."]) {
    const once = fixOutbound(text, { now: NOW })[0];
    assert.equal(fixOutbound(once, { now: NOW })[0], once, text);
  }
});