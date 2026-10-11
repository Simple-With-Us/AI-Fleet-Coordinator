import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  parsePartition, parseServerListener, parseBoardCli, buildFleetAppsTab, buildSeatsTab,
  repoForAppKey, worktreeCell, zulipFileCode, buildMarkdownMirror
} from "./fleet-apps-sheet-sync.mjs";

const read = (rel) => readFileSync(new URL(`../${rel}`, import.meta.url), "utf8");
const fleetApps = JSON.parse(read("fleet-apps.json"));
const facts = JSON.parse(read("scripts/fleet-apps-sheet-facts.json"));
const partition = parsePartition(read("docs/protocols/agent-sync-partition.toml"));
const serverSeats = parseServerListener(read("scripts/agent_sync/server/listener.toml"));
const { seatAppNames, teamKeys } = parseBoardCli(read("scripts/linear-board/board"));
const seatsRows = buildSeatsTab({ fleetApps, partition, serverSeats, seatAppNames, facts }, "NOW");

test("the three readers find their data in the real files", () => {
  assert.equal(partition.CLAUDE, "mac");
  assert.equal(partition.JET, "server");
  assert.equal(partition["BF-PLUMBER"], "none");
  assert.equal(serverSeats.JET.wake, "http");
  assert.equal(serverSeats.JET.routineUrlEnv, "JET_ROUTINE_URL");
  assert.equal(serverSeats["GB-FIXER"].routineKeyEnv, "ZULIP_ALERT_GB_FIXER_KEY");
  assert.ok(teamKeys.includes("AFC") && teamKeys.includes("CK"));
  // A value with parentheses must not end the dict early.
  assert.equal(seatAppNames.INSTINCT, "Wise (Instinct)");
  assert.equal(seatAppNames.OPENCODE, "OpenCode");
});

test("Fleet Apps lists every fleet-apps.json app, FleetLink included", () => {
  const rows = buildFleetAppsTab(fleetApps, teamKeys, "NOW");
  const acronyms = rows.slice(1).filter(r => r.length > 1).map(r => r[0]);
  assert.deepEqual(acronyms, fleetApps.apps.map(a => a.acronym));
  assert.ok(acronyms.includes("FL") && acronyms.includes("FLL"));
  const ck = rows.find(r => r[0] === "CK");
  assert.match(ck[6], /^CK /);
  const fll = rows.find(r => r[0] === "FLL");
  assert.match(fll[6], /No team/);
});

test("Seats and Bots covers every partition seat and every fleet-apps.json seat", () => {
  const tags = new Set(seatsRows.slice(1).filter(r => r.length > 1).map(r => r[0]));
  for (const s of fleetApps.seats) assert.ok(tags.has(s.tag), `missing ${s.tag}`);
  for (const t of Object.keys(partition)) assert.ok(tags.has(t), `missing ${t}`);
  assert.equal([...tags].filter(t => t.startsWith("BF-")).length, 10);
  assert.equal([...tags].filter(t => t.startsWith("GB-")).length, 11);
});

test("Grok shows as plain Grok, GROK-BUILD as a retired alias, retired seats as retired", () => {
  const grok = seatsRows.find(r => r[0] === "GROK");
  assert.equal(grok[1], "Grok");
  const alias = seatsRows.find(r => r[0] === "GROK-BUILD");
  assert.match(alias[4], /^Retired 2026-10-08/);
  for (const t of ["MONET", "RENOIR", "HARNESS", "DSH", "KIMI"]) {
    assert.match(seatsRows.find(r => r[0] === t)[4], /^Retired/, t);
  }
  assert.match(seatsRows.find(r => r[0] === "KIMI")[4], /2026-08-21/);
});

test("credential cells carry names and paths only", () => {
  for (const r of seatsRows.slice(1)) {
    const cell = String(r[7] || "");
    if (!cell) continue;
    assert.match(cell, /^(~\/\.secrets\/Zulip\/[A-Za-z-]+-zuliprc|Infisical )/, cell);
    assert.doesNotMatch(cell, /=/, "no KEY=value");
  }
  assert.equal(zulipFileCode("BF-COMPILER"), "BF-Compiler");
  assert.equal(zulipFileCode("CLAUDE"), "Claude");
});

test("app keys map to their repo through the lane map", () => {
  assert.equal(repoForAppKey("socratic", fleetApps).acronym, "ST");
  assert.equal(repoForAppKey("botfleet-mac", fleetApps).acronym, "BF");
  assert.equal(repoForAppKey("usage-local", fleetApps).acronym, "UM");
  assert.equal(repoForAppKey("fleetlink", fleetApps).acronym, "FL");
  assert.match(worktreeCell("clutch", { worktreeHint: "~/apps/clutch-<seat>-ios" }, fleetApps), /^~\/Code\/Clutch /);
  assert.match(worktreeCell("nope", { worktreeHint: "~/x" }, fleetApps), /unverified/);
});

test("the Markdown mirror stops each table at its first blank row", () => {
  const tabs = {
    registry: [["A", "B"], ["1", "2"], [], ["ASC RECORDS NOT IN apps.json"]],
    testflight: [["A"], ["x"], [], ["Last synced"]],
    icons: [["t"], ["g"], [], ["A"], ["k"], [], ["RE-APPROVE"]],
    fleetApps: [["Acronym"], ["ST"], []],
    seats: [["Seat"], ["CLAUDE"], []],
  };
  const md = buildMarkdownMirror({ sections: [] }, tabs, "NOW");
  assert.doesNotMatch(md, /ASC RECORDS NOT IN/);
  assert.doesNotMatch(md, /RE-APPROVE/);
  assert.match(md, /\| ST \|/);
});
