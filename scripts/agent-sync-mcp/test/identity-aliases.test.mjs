// The Worker's alias table must equal what the stdio server resolves.  It cannot read identity.py or fleet-apps.json
// at run time, so this test is the staleness check:  it fails when either changes without a copy into
// src/identity-aliases.js.
import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { GROK_ALIAS_PATTERN, SEAT_NAME_ALIASES, canonicalSeatName } from "../src/identity-aliases.js";
import { canonicalIdentity } from "../src/delegated-identities.js";
import { z } from "zod";
import { ROOT } from "./helpers.mjs";

// Only the fields this test reads, and nothing else about the file's shape is assumed.
const FleetApps = z.looseObject({
  seats: z.array(z.looseObject({ aliasPattern: z.string().optional() })).default([]),
  apps: z.array(z.looseObject({ aliasPattern: z.string().optional() })).default([]),
});

const SCRIPTS = path.resolve(ROOT, "..");

test("the alias table equals identity.py's SEAT_NAME_ALIASES and GROK_ALIAS_PATTERN", () => {
  const out = execFileSync("python3", ["-c", "import json; from agent_sync import identity as I; print(json.dumps({'a': I.SEAT_NAME_ALIASES, 'p': I.GROK_ALIAS_PATTERN}))"], {
    cwd: SCRIPTS, env: { ...process.env, PYTHONPATH: SCRIPTS }, encoding: "utf8",
  });
  const python = JSON.parse(out);
  assert.deepEqual({ ...SEAT_NAME_ALIASES }, python.a, "re-copy SEAT_NAME_ALIASES from identity.py into src/identity-aliases.js");
  assert.equal(GROK_ALIAS_PATTERN, python.p);
});

test("the GROK alias pattern equals the GROK row's aliasPattern in fleet-apps.json", () => {
  const apps = FleetApps.parse(JSON.parse(readFileSync(path.join(SCRIPTS, "..", "fleet-apps.json"), "utf8")));
  const rows = [...apps.seats, ...apps.apps].filter((row) => row.aliasPattern);
  assert.ok(rows.length > 0, "fleet-apps.json carries the alias pattern");
  for (const row of rows) assert.equal(row.aliasPattern, GROK_ALIAS_PATTERN, "re-copy the pattern into src/identity-aliases.js");
});

test("published aliases and every GROK-BUILD spelling resolve to their seat, before the allowed-set check", () => {
  assert.equal(canonicalIdentity("instinct"), "WISE");
  assert.equal(canonicalIdentity(" Muse_Assist "), "MA");
  assert.equal(canonicalIdentity("rob-muse"), "MA");
  for (const spelling of ["GROK-BUILD", "grok_build", "Grok (Build)", "grokbuild"]) assert.equal(canonicalSeatName(spelling), "GROK", spelling);
  assert.equal(canonicalSeatName("GROKBUILDER"), "GROKBUILDER", "it never matches a longer word");
  assert.equal(canonicalIdentity("not a name!"), null);
});
