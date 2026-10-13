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
  assert.equal(seatTagForSeat("WISE"), "INSTINCT");
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