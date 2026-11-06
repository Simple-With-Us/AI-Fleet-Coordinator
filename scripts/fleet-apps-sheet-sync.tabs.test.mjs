import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  parsePartition, parseServerListener, parseBoardCli, buildFleetAppsTab, buildSeatsTab,
  repoForAppKey, worktreeCell, zulipFileCode, buildMarkdownMirror, validateFacts
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
  assert.ok(teamKeys.includes("AFC") && teamKeys.includes("C"));
  // A value with parentheses must not end the dict early.
  assert.equal(seatAppNames.WISE, "Wise (Instinct)");
  assert.equal(seatAppNames.OPENCODE, "OpenCode");
});

test("Fleet Apps lists every fleet-apps.json app, FleetLink included", () => {
  const rows = buildFleetAppsTab(fleetApps, teamKeys, "NOW");
  const acronyms = rows.slice(1).filter(r => r.length > 1).map(r => r[0]);
  assert.deepEqual(acronyms, fleetApps.apps.map(a => a.acronym));
  assert.ok(acronyms.includes("FL") && acronyms.includes("FLL"));
  const clutch = rows.find(r => r[0] === "C");
  assert.match(clutch[6], /^C  \(linear\.app\/simple-with-us\/team\/C\)/);
  assert.match(clutch[6], /team\/C\)/);
  const fll = rows.find(r => r[0] === "FLL");
  assert.match(fll[6], /No team/);
});

test("a BotFleet seat with a Linear app shows that name, and a persona without one stays an em dash", () => {
  const col = seatsRows[0].indexOf("Linear App");
  assert.ok(col > 0);
  const bf = seatsRows.slice(1).filter(r => r.length > 1 && String(r[0]).startsWith("BF-"));
  assert.ok(bf.length > 0);
  for (const row of bf) {
    assert.equal(row[col], seatAppNames[row[0]], row[0]);
    assert.notEqual(row[col], "—", row[0]);
  }
  assert.equal(seatsRows.find(r => r[0] === "BF-FIXER")[col], "BF-Fixer");
  assert.equal(seatsRows.find(r => r[0] === "GB-HOUSEKEEPER")[col], "—");
});

test("Seats and Bots covers every partition seat and every fleet-apps.json seat", () => {
  const tags = new Set(seatsRows.slice(1).filter(r => r.length > 1).map(r => r[0]));
  for (const s of fleetApps.seats.filter(s => !["GROK-BOT", "GROK-BUILD"].includes(s.tag))) assert.ok(tags.has(s.tag), `missing ${s.tag}`);
  for (const t of Object.keys(partition).filter(t => !["GROK-BOT", "GROK-BUILD"].includes(t))) assert.ok(tags.has(t), `missing ${t}`);
  // Eleven BF seats:  the ten role seats plus BF-DIRECTOR.  Twelve GB personas, GB-ENGINEER included.
  assert.equal([...tags].filter(t => t.startsWith("BF-")).length, 11);
  assert.equal([...tags].filter(t => t.startsWith("GB-")).length, 12);
});

test("one Grok Build row, no generic Grok Bot row, active Kimi and ZCode", () => {
  assert.equal(seatsRows.find(r => r[0] === "GROK")[1], "Grok");
  assert.ok(!seatsRows.some(r => ["GROK-BUILD", "GROK-BOT"].includes(r[0])));
  const status = seatsRows[0].indexOf("Status");
  for (const t of ["KIMI", "ZC"]) assert.equal(seatsRows.find(r => r[0] === t)[status], "active");
  for (const t of ["MONET", "RENOIR", "HARNESS", "DSH"]) assert.match(seatsRows.find(r => r[0] === t)[status], /^Retired/);
  assert.ok(!seatsRows[0].some(h => ["Group", "Listener", "Wake Path"].includes(h)));
  assert.doesNotMatch(JSON.stringify(seatsRows), /Coding seat|Cloud seat|BotFleet handles it natively|none \(no lanes\)/);
});

test("credential cells carry names and paths only", () => {
  for (const r of seatsRows.slice(1)) {
    const cell = String(r[seatsRows[0].indexOf("Zulip Credential (name only)")] || "");
    if (!cell) continue;
    assert.match(cell, /^Infisical /, cell);
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

test("the facts file passes its shape check, and a bad section is refused", () => {
  assert.equal(validateFacts(facts), facts);
  assert.throws(() => validateFacts({ ascRecords: { "1": { status: "x" } } }), /wrong shape/);
  assert.throws(() => validateFacts({ seatPlatfroms: {} }), /unknown section/);
});

test("the Markdown mirror escapes HTML in cell text", () => {
  const tabs = {
    registry: [["A"], ["<img src=x onerror=alert(1)>"], []],
    testflight: [["A"], []], icons: [[], [], [], ["A"], []], fleetApps: [["A"], []], seats: [["A"], []],
  };
  const md = buildMarkdownMirror({ sections: [] }, tabs, "NOW");
  assert.doesNotMatch(md, /<img/);
  assert.match(md, /&lt;img/);
});

test('Grok Web has its own source branch prefix and no cloud-only PR restriction',()=>{
  const row=seatsRows.find(r=>r[0]==='GROK-WEB');assert.equal(row[seatsRows[0].indexOf('Branch Prefix')],'grok-web/');assert.doesNotMatch(row.at(-1),/no Mac lanes|no checkouts|refuses|makes no lanes/);
});

test('source notes and reserved/legacy lane tokens remain visible',()=>{
  for(const tag of ['ECHO','WISE']) assert.equal(seatsRows.find(r=>r[0]===tag).at(-1),fleetApps.seats.find(s=>s.tag===tag).notes);
  const text=seatsRows.map(r=>r.join(' ')).join(' ');assert.match(text,/grok-build\//);assert.match(text,/slugs must not start with web-/);assert.doesNotMatch(text,/makes no Mac lanes|not through a Mac lane/);assert.doesNotMatch(seatsRows.at(-1)[0],/server\/listener.toml/);
});
