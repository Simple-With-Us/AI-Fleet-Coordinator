// The constants this module ports from scripts/linear-board/board, checked
// against that file so the two cannot drift apart silently.  They live in two
// languages and share no code, so this is the only thing that would notice a
// one-sided edit:  a new team key in the CLI and not here would send a seat's
// issue to a team Linear does not have.
//
// Read-only:  it parses the CLI's source, it never runs it.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  BOARD_SLUG_TEAM,
  FLAT_LABELS,
  LINEAR_SCOPE,
  LINEAR_SECRETS,
  STATUS_TO_STATE,
  TEAM_KEYS,
  teamForSlug,
  routeApp,
  appNameOk,
  seatTagForSeat,
} from "../src/linear.js";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BOARD = readFileSync(path.join(ROOT, "../linear-board/board"), "utf8");

/** The Python tuple or dict literal assigned to `NAME`, parsed into JS values. */
function fromBoard(name) {
  const start = BOARD.search(new RegExp(`^${name}\\s*=\\s*`, "m"));
  assert.notEqual(start, -1, `${name} is gone from the board CLI`);
  const open = BOARD.indexOf("(", start);
  const brace = BOARD.indexOf("{", start);
  const isDict = brace !== -1 && (open === -1 || brace < open);
  const opener = isDict ? "{" : "(";
  const closer = isDict ? "}" : ")";
  const from = isDict ? brace : open;
  let depth = 0;
  let inString = null;
  for (let i = from; i < BOARD.length; i++) {
    const ch = BOARD[i];
    if (inString) {
      if (ch === inString && BOARD[i - 1] !== "\\") inString = null;
      continue;
    }
    if (ch === '"') inString = ch;
    else if (ch === opener) depth++;
    else if (ch === closer && --depth === 0) return BOARD.slice(from, i + 1);
  }
  throw new Error(`${name} is not a closed literal`);
}

/** A Python tuple of bare words or string literals. */
function tupleFromBoard(name) {
  const literal = fromBoard(name);
  // Double quotes only:  "Won't Fix" holds an apostrophe, and a both-quotes
  // pattern would cut it in half.
  return [...literal.slice(1, -1).matchAll(/"([^"]*)"/g)].map((m) => m[1]);
}

/** A flat one-level Python dict of string to string. */
function dictFromBoard(name) {
  const literal = fromBoard(name);
  const out = {};
  for (const m of literal.slice(1, -1).matchAll(/"([^"]*)"\s*:\s*"([^"]*)"/g)) out[m[1]] = m[2];
  return out;
}

test("the ported scope string is the CLI's, character for character", () => {
  // Asking Linear for a token with a different string revokes every token the
  // app holds (design section 2), so this one must never drift.
  assert.equal(LINEAR_SCOPE, /SCOPE\s*=\s*"([^"]+)"/.exec(BOARD)[1]);
});

test("the ported team keys are the CLI's, including the CodeCaps and FleetLink teams", () => {
  assert.deepEqual([...TEAM_KEYS], tupleFromBoard("TEAM_KEYS"));
  assert.ok(TEAM_KEYS.includes("CC"), "the CodeCaps team key is missing");
  assert.ok(TEAM_KEYS.includes("FL"), "the FleetLink team key is missing");
});

test("the ported board slug routing is the CLI's table", () => {
  const board = dictFromBoard("BOARD_SLUG_TEAM");
  assert.deepEqual(Object.fromEntries(Object.entries(BOARD_SLUG_TEAM).sort()), Object.fromEntries(Object.entries(board).sort()));
});

test("the ported status mapping is the CLI's, and duplicate is still not a state", () => {
  assert.deepEqual({ ...STATUS_TO_STATE }, dictFromBoard("STATUS_TO_STATE"));
  // Duplicate is a relation to the kept issue, never a workflow state write.
  assert.equal(STATUS_TO_STATE.duplicate, undefined);
  assert.equal(STATUS_TO_STATE.open, "Todo");
});

test("the ported flat label whitelist is the CLI's, so no seat can forge a claim", () => {
  assert.deepEqual([...FLAT_LABELS], tupleFromBoard("FLAT_LABELS"));
  for (const prefix of ["claimed:", "seat:"]) {
    assert.ok(!FLAT_LABELS.some((l) => l.startsWith(prefix)), `${prefix} must never be nameable here`);
  }
});

test("routing agrees with the CLI on the slugs the fleet actually uses", () => {
  assert.equal(teamForSlug("ai-fleet-coordinator"), "AFC");
  assert.equal(teamForSlug("fleetlink"), "FL");
  assert.equal(teamForSlug("fleetlink-legacy"), "FL");
  assert.equal(teamForSlug("codecaps"), "CC");
  assert.equal(teamForSlug("fleet-infra"), "OPS", "the default app routes to OPS");
  // A bare team key works, case-insensitively, as it does in the CLI.
  assert.equal(teamForSlug("CC"), "CC");
  assert.equal(teamForSlug("cc"), "CC");
  // A slug nothing matches files in the fallback team, still traceable.
  assert.deepEqual(routeApp("some-app-nobody-made"), { team: "AFC", slug: "some-app-nobody-made" });
  assert.deepEqual(routeApp(""), { team: "OPS", slug: null }, "no app at all is the default app");
});

test("a seat's accepted app names are its own, and no two seats share one", () => {
  assert.ok(appNameOk("Grok Web", "GROK-WEB"));
  assert.ok(appNameOk("Grok (Web/iOS)", "GROK-WEB"), "an alias the owner may have used still works");
  assert.ok(appNameOk("GROK-WEB", "GROK-WEB"), "the seat tag itself is always accepted");
  assert.ok(appNameOk("echo (instinct)", "ECHO"), "the name check ignores case");
  assert.ok(!appNameOk("Wise (Instinct)", "GROK-WEB"), "another seat's app must never pass");
  assert.ok(!appNameOk("Jay", "GROK-WEB"), "a person is never an accepted app name");

  // A shared name would let two seats' credentials swap unseen, which is the
  // whole point of the viewer check (board test_accepted_app_names_belong_to_one_seat).
  const names = new Map();
  for (const seat of Object.keys(LINEAR_SECRETS)) {
    for (const name of [seat, ...(seat === "GROK-WEB" ? ["Grok Web", "Grok (Web/iOS)"] : [])]) {
      const key = name.toLowerCase();
      assert.ok(!names.has(key), `${name} belongs to both ${names.get(key)} and ${seat}`);
      names.set(key, seat);
    }
  }
});

test("a seat tag spelling resolves to one seat, the way the CLI resolves it", () => {
  assert.equal(seatTagForSeat("INSTINCT"), "WISE");   // WISE is the tag;  INSTINCT is retired
  assert.equal(seatTagForSeat("WISE"), "WISE");
  assert.equal(seatTagForSeat("grok-web"), "GROK-WEB");
});

test("every seat with Linear secrets has a hyphen-free secret name", () => {
  // An env var name cannot hold a hyphen, and Infisical stores the pair the
  // same way, so a seat whose name breaks this would silently never configure.
  for (const [seat, names] of Object.entries(LINEAR_SECRETS)) {
    assert.equal(seat, seat.trim(), seat);
    assert.ok(!names.clientId.includes("-"), `${seat} client id variable name holds a hyphen`);
    assert.ok(names.clientId.startsWith("LINEAR_") && names.clientId.endsWith("_CLIENT_ID"), names.clientId);
    assert.ok(names.clientSecret.endsWith("_CLIENT_SECRET"), names.clientSecret);
  }
  assert.equal(LINEAR_SECRETS["GROK-WEB"].clientId, "LINEAR_GROK_WEB_CLIENT_ID");
});
test("the early re-mint age is the CLI's (28 days of a 30-day token)", async () => {
  const { LINEAR_REMINT_AFTER_S } = await import("../src/linear.js");
  assert.equal(LINEAR_REMINT_AFTER_S, 28 * 86_400);
  assert.match(BOARD, /^REMINT_AFTER = 28 \* 86400$/m);
});
test("CLUTCH canonical name and CK compatibility alias route to the live C team", () => {
  for (const app of ["C", "c", "CLUTCH", "clutch", "CK", "ck", "dsh-runtime", "harness"]) {
    assert.deepEqual(routeApp(app), { team: "C", slug: null });
  }
});

test("CODEX has its own Linear pair, app name and deploy gate, shared with no other seat", async () => {
  const L = await import("../src/linear.js");
  assert.deepEqual({ ...L.LINEAR_SECRETS.CODEX }, { clientId: "LINEAR_CODEX_CLIENT_ID", clientSecret: "LINEAR_CODEX_CLIENT_SECRET" });
  assert.deepEqual({ ...L.LINEAR_INFISICAL.CODEX }, { clientId: "LINEAR_CODEX_CLIENT_ID", clientSecret: "LINEAR_CODEX_CLIENT_SECRET" });
  assert.ok(L.LINEAR_SEATS.includes("CODEX"));
  assert.deepEqual([...L.seatAppNames("CODEX")], ["Codex"]);
  assert.ok(L.appNameOk("Codex", "CODEX"));
  for (const other of Object.keys(L.LINEAR_SECRETS).filter((s) => s !== "CODEX")) {
    assert.ok(!L.appNameOk("Codex", other), `${other} must not accept the Codex app`);
    assert.notEqual(L.LINEAR_SECRETS[other].clientId, L.LINEAR_SECRETS.CODEX.clientId);
    assert.notEqual(L.LINEAR_INFISICAL[other].clientId, L.LINEAR_INFISICAL.CODEX.clientId);
  }
  // Same scope string as the CLI:  Linear revokes every app token when a mint asks for another one.
  assert.equal(L.LINEAR_SCOPE, "read,write,app:assignable,app:mentionable");
});

// ---- Linear coverage for the own-app seats (read-only names; activation stays a deploy gate) ----

const HOST_VERIFIED_LINEAR_PREFIXES = Object.freeze({
  JET: "LINEAR_JET", "GROK-WEB": "LINEAR_GROK_WEB", ECHO: "LINEAR_ECHO", WISE: "LINEAR_WISE",
  CURSOR: "LINEAR_CURSOR", CLAUDE: "LINEAR_CLAUDE", CODEX: "LINEAR_CODEX",
  MA: "LINEAR_MA", AG: "LINEAR_AG", CLUTCH: "LINEAR_CLUTCH", FX: "LINEAR_FX", GROK: "LINEAR_GROK",
  MC: "LINEAR_MC", MM: "LINEAR_MM", OPENCODE: "LINEAR_OPENCODE",
  "BF-COMPILER": "LINEAR_BF_COMPILER", "BF-DEPLOYER": "LINEAR_BF_DEPLOYER", "BF-DESIGNER": "LINEAR_BF_DESIGNER",
  "BF-DIRECTOR": "LINEAR_BF_DIRECTOR", "BF-ENGINEER": "LINEAR_BF_ENGINEER", "BF-FIXER": "LINEAR_BF_FIXER",
  "BF-HOUSEKEEPER": "LINEAR_BF_HOUSEKEEPER", "BF-MONITOR": "LINEAR_BF_MONITOR", "BF-ORACLE": "LINEAR_BF_ORACLE",
  "BF-PLUMBER": "LINEAR_BF_PLUMBER", "BF-PUBLISHER": "LINEAR_BF_PUBLISHER", "BF-TRADER": "LINEAR_BF_TRADER",
});

test("the Infisical /linear names are the live LINEAR_<SEAT>_CLIENT_* names for every seat", async () => {
  const L = await import("../src/linear.js");
  for (const [seat, prefix] of Object.entries(HOST_VERIFIED_LINEAR_PREFIXES)) {
    assert.deepEqual({ ...L.LINEAR_INFISICAL[seat] }, { clientId: `${prefix}_CLIENT_ID`, clientSecret: `${prefix}_CLIENT_SECRET` }, seat);
  }
  assert.deepEqual({ ...L.LINEAR_INFISICAL["JET-CHAT"] }, { ...L.LINEAR_INFISICAL.JET }, "JET-CHAT keeps reading Jet's record, never a copy");
  for (const [seat, names] of Object.entries(L.LINEAR_INFISICAL)) {
    assert.ok(!/^[A-Z0-9_]+_LINEAR_CLIENT/.test(names.clientId), `${seat} still uses the reverse <SEAT>_LINEAR_ name`);
  }
});

// MA: its cached token was refused (401) at check time;  BF-ENGINEER and BF-TRADER had no token to read.
const HELD_BACK = Object.freeze([]); // all 20 are on:  a missing cache is a bootstrap state, the hosted client mints from the seat's own pair
test("the 20 own-app seats have prepared names, all 20 are enabled, and none share a pair", async () => {
  const L = await import("../src/linear.js");
  const own = Object.keys(L.OWN_APP_LINEAR_PREFIXES);
  assert.equal(own.length, 20);
  assert.ok(!own.includes("BF-BUILDER") && !Object.hasOwn(L.LINEAR_SECRETS, "BF-BUILDER") && !L.LINEAR_SEATS.includes("BF-BUILDER"));
  for (const seat of own) {
    assert.ok(Object.hasOwn(L.LINEAR_SECRETS, seat) && Object.hasOwn(L.LINEAR_INFISICAL, seat), seat);
    assert.equal(L.LINEAR_SEATS.includes(seat), !HELD_BACK.includes(seat), `${seat}: enabled unless held back`);
    assert.deepEqual({ ...L.LINEAR_SECRETS[seat] }, { ...L.LINEAR_INFISICAL[seat] }, `${seat}: Worker and Infisical names agree`);
    assert.deepEqual({ ...L.linearSecretNames(seat) }, { ...L.LINEAR_SECRETS[seat] });
  }
  assert.deepEqual([...L.LINEAR_OWN_APP_ENABLED].sort(), own.filter((x) => !HELD_BACK.includes(x)).sort(), "all 20 own-app seats are switched on");
  const ids = new Map();
  for (const [seat, names] of Object.entries(L.LINEAR_INFISICAL)) {
    if (seat === "JET-CHAT") continue; // the one deliberate JET/JET-CHAT share, unchanged
    assert.ok(!ids.has(names.clientId), `${seat} shares ${names.clientId} with ${ids.get(names.clientId)}`);
    ids.set(names.clientId, seat);
  }
});

test("a retired or unlisted seat reads no credentials in any Infisical mode; an uninstalled enabled seat stays unconfigured", async () => {
  const L = await import("../src/linear.js");
  const { _resetInfisicalForTests } = await import("../src/infisical.js");
  _resetInfisicalForTests();
  const env = { LINEAR_CLUTCH_CLIENT_ID: "id-clutch", LINEAR_CLUTCH_CLIENT_SECRET: "sec-clutch", LINEAR_BF_COMPILER_CLIENT_ID: "id-bf", LINEAR_BF_COMPILER_CLIENT_SECRET: "sec-bf" };
  for (const seat of [...HELD_BACK, "BF-BUILDER"]) assert.equal(await L.resolveLinearPair(env, seat), null, `${seat} is not enabled`);
  assert.equal(await L.resolveLinearPair({ INFISICAL_MODE: "off" }, "CODEX").then((p) => p.clientId), "", "an enabled seat with nothing installed still yields no client");
});

test("every enabled Linear seat has a grounded app name, so a bare seat tag is never the only viewer proof", async () => {
  const L = await import("../src/linear.js");
  for (const seat of L.LINEAR_SEATS) {
    assert.ok(L.seatAppNames(seat).length > 0, `${seat} is enabled but has no SEAT_APP_NAMES entry`);
    assert.ok(Object.hasOwn(L.LINEAR_SECRETS, seat) && Object.hasOwn(L.LINEAR_INFISICAL, seat), seat);
  }
});

test("own-app viewer names are the live Linear viewer names, accepted by their own seat only", async () => {
  const L = await import("../src/linear.js");
  const live = { AG: "Antigravity (AG)", CLUTCH: "Clutch", FX: "FX.sh", GROK: "Grok (Build)", MC: "Muse Code (MC)", MM: "MiniMax (MM)", OPENCODE: "OpenCode",
    "BF-COMPILER": "BF-Compiler", "BF-DEPLOYER": "BF-Deployer", "BF-DESIGNER": "BF-Designer", "BF-DIRECTOR": "BF-Director", "BF-FIXER": "BF-Fixer",
    "BF-HOUSEKEEPER": "BF-Housekeeper", "BF-MONITOR": "BF-Monitor", "BF-ORACLE": "BF-Oracle", "BF-PLUMBER": "BF-Plumber", "BF-PUBLISHER": "BF-Publisher",
    "BF-ENGINEER": "BF-Engineer", "BF-TRADER": "BF-Trader" };
  for (const [seat, name] of Object.entries(live)) {
    assert.deepEqual([...L.seatAppNames(seat)], [name], seat);
    assert.ok(L.appNameOk(name, seat) && L.appNameOk(name.toUpperCase(), seat), `${seat} accepts its own app`);
    for (const other of Object.keys(L.LINEAR_SECRETS).filter((s) => s !== seat && !(seat === "JET" || s === "JET-CHAT"))) {
      assert.ok(!L.appNameOk(name, other), `${other} must not accept ${seat}'s app name`);
    }
  }
  const all = new Map();
  for (const seat of Object.keys(L.LINEAR_SECRETS)) for (const n of L.seatAppNames(seat)) {
    const k = n.toLowerCase();
    if (all.has(k) && !(["JET", "JET-CHAT"].includes(seat) && ["JET", "JET-CHAT"].includes(all.get(k)))) assert.fail(`${n} belongs to both ${all.get(k)} and ${seat}`);
    all.set(k, seat);
  }
});
