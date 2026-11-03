// The CODEX seat (owner request, Oct 23 2026:  Codex connects through /admin
// arming the same way Jet did).  The seat joins KNOWN_SEATS with its own
// secret names and label, and its exact Codex callback pair - captured from
// the first refused authorize attempt, never guessed - is in SEATS:  the
// shared ChatGPT callback, held by the JET / JET-CHAT / CODEX cohort.  The
// seat was live in production before the committed config recorded it;  the
// Nov 3, 2026 parity change closes that gap.

import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { KNOWN_SEATS, SEAT_LABELS, SEAT_SECRETS, WAKE_SECRETS, loadConfig } from "../src/config.js";
import { allowedIdentities, toolListFor } from "../src/delegated-identities.js";
import { seatTagFor } from "../src/contract.js";
import { ROOT, testEnv, wranglerConfig } from "./helpers.mjs";

// codex-bot's address comes from the fleet registry (the CODEX row of
// fleet-apps.json, the file src/notes.js bundles), so the test carries no
// address of its own.  It has to be a real realm address:  loadConfig refuses
// any ZULIP_EMAIL_<SEAT> outside the realm (BOT_EMAIL_RE in src/config.js).
const CODEX_EMAIL = JSON.parse(readFileSync(path.join(ROOT, "../../fleet-apps.json"), "utf8"))
  .seats.find((s) => s.tag === "CODEX")?.zulipBotEmail;

test("CODEX is a known seat with its own secret names, label and full tools", () => {
  assert.equal(typeof CODEX_EMAIL, "string", "fleet-apps.json carries the CODEX bot address");
  assert.ok(KNOWN_SEATS.includes("CODEX"));
  // The runtime read is codex-bot's canonical Fleet-OPS /zulip record, the
  // same name install_seat_key.py reads (CODEX_ZULIP_KEY), never a copy.
  assert.deepEqual(SEAT_SECRETS.CODEX, { key: "ZULIP_KEY_CODEX", email: "ZULIP_EMAIL_CODEX", infisical: { name: "CODEX_ZULIP_KEY", path: "/zulip" } });
  assert.equal(SEAT_LABELS.CODEX, "Codex");
  assert.ok(!("CODEX" in WAKE_SECRETS), "no wake route for Codex");
  assert.deepEqual([...allowedIdentities("CODEX")], ["CODEX"], "a Codex grant acts only as Codex");
  assert.ok(toolListFor("CODEX").list.length > 0, "Codex gets the full hosted tool set");
  // codex-bot signs as its own seat:  no tag expectation entry is needed.
  assert.equal(seatTagFor({ is_bot: true, email: CODEX_EMAIL, full_name: "Codex" }), "CODEX");
});

test("CODEX is hosted with its captured pair, and hosting it without its email var fails closed", () => {
  const prod = loadConfig(wranglerConfig().vars);
  assert.ok(prod.hostedSeats.includes("CODEX"), "CODEX is hosted:  its captured pair is in the committed SEATS");
  assert.equal(prod.botEmails.CODEX, CODEX_EMAIL, "the committed email var matches the fleet registry address");
  assert.deepEqual([...prod.redirectSeats.get("https://chatgpt.com/connector_platform_oauth_redirect")], ["JET", "JET-CHAT", "CODEX"], "CODEX carries the shared ChatGPT callback as part of its cohort");
  const unprovisioned = testEnv({ HOSTED_SEATS: "JET,CODEX" });
  delete unprovisioned.ZULIP_EMAIL_CODEX;
  assert.throws(() => loadConfig(unprovisioned), /ZULIP_EMAIL_CODEX/, "an unprovisioned email var refuses config load rather than serving a wrong binding");
  const cfg = loadConfig(testEnv({ HOSTED_SEATS: "JET,CODEX", ZULIP_EMAIL_CODEX: CODEX_EMAIL }));
  assert.equal(cfg.botEmails.CODEX, CODEX_EMAIL);
});

test("the key installer reads Codex's existing Fleet-OPS entry instead of duplicating it", () => {
  const installer = readFileSync(path.join(ROOT, "install_seat_key.py"), "utf8");
  const flat = installer.replace(/\s+/g, " ");
  // The Fleet-OPS project id itself stays out of fixtures (fleet-infra mock-data
  // rule);  the test pins the consolidation mapping, not the literal.
  // The project is resolved at call time (None marker in the table);  the
  // behavioral resolution is pinned in the seat_entry test below.
  assert.match(flat, /"CODEX": \( None, "CODEX_ZULIP_EMAIL", "CODEX_ZULIP_KEY", "ZULIP_KEY_CODEX", "ZULIP_EMAIL_CODEX", \)/);
});

test("install_seat_key seat_entry: CODEX and MA resolve the Fleet-OPS project at call time, legacy seats derive theirs (kody 4363264472, 4363264918, 4366278313, 4366278510, 4366279206)", () => {
  const program = [
    "import json, sys",
    "sys.path.insert(0, sys.argv[1])",
    "import install_seat_key as m",
    "print(json.dumps({'codex': m.seat_entry('CODEX'), 'ma': m.seat_entry('MA'), 'cursor': m.seat_entry('CURSOR'), 'afc': m.INFISICAL_PROJECT, 'fleetops': m.FLEET_OPS_PROJECT}))",
  ].join("\n");
  // An empty HOME and a cleared INFISICAL_FLEET_OPS_PROJECT_ID, so the host's
  // own ~/.agent-sync/fleet-ops-project-id or environment cannot stand in for
  // the committed fallback.
  const home = mkdtempSync(path.join(tmpdir(), "codex-seat-home-"));
  const run = (env = {}) =>
    execFileSync("python3", ["-c", program, ROOT], {
      env: { ...process.env, HOME: home, INFISICAL_FLEET_OPS_PROJECT_ID: "", ...env },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  const refusesNonUuid = (bad) => (err) =>
    /\(INFISICAL_FLEET_OPS_PROJECT_ID or ~\/\.agent-sync\/fleet-ops-project-id\) is not a UUID/.test(err.stderr) &&
    !err.stderr.includes(bad);
  try {
    const d = JSON.parse(run());
    // Ordered tuples, names pinned; the project ids themselves come from the
    // module (fleet-infra mock-data rule: no literal ids in fixtures).
    assert.deepEqual(d.codex, [d.fleetops, "CODEX_ZULIP_EMAIL", "CODEX_ZULIP_KEY", "ZULIP_KEY_CODEX", "ZULIP_EMAIL_CODEX"]);
    assert.deepEqual(d.ma, [d.fleetops, "MA_ZULIP_EMAIL", "MA_ZULIP_KEY", "ZULIP_KEY_MA", "ZULIP_EMAIL_MA"]);
    assert.deepEqual(d.cursor, [d.afc, "ZULIP_CURSOR_EMAIL", "ZULIP_CURSOR_API_KEY", "ZULIP_KEY_CURSOR", "ZULIP_EMAIL_CURSOR"]);
    // The environment wins over the committed fallback, for both Fleet-OPS
    // seats;  legacy seats are unaffected.
    const envId = "00000000-0000-4000-8000-000000000000";
    const d2 = JSON.parse(run({ INFISICAL_FLEET_OPS_PROJECT_ID: envId }));
    assert.equal(d2.codex[0], envId, "INFISICAL_FLEET_OPS_PROJECT_ID resolves ahead of the fallback");
    assert.equal(d2.ma[0], envId, "MA follows the same resolution as CODEX");
    assert.equal(d2.cursor[0], d.afc);
    // With the environment unset, the first line of ~/.agent-sync/fleet-ops-project-id.
    const fileId = "11111111-1111-4111-8111-111111111111";
    const idFile = path.join(home, ".agent-sync", "fleet-ops-project-id");
    mkdirSync(path.dirname(idFile));
    writeFileSync(idFile, `${fileId}\n`);
    const d3 = JSON.parse(run());
    assert.equal(d3.codex[0], fileId);
    assert.equal(d3.ma[0], fileId);
    // A value that is set but is not a UUID fails closed, naming both sources
    // and never echoing the value, whichever source holds it.
    assert.throws(() => run({ INFISICAL_FLEET_OPS_PROJECT_ID: "zz-typo-zz" }), refusesNonUuid("zz-typo-zz"));
    writeFileSync(idFile, "yy-typo-yy\n");
    assert.throws(() => run(), refusesNonUuid("yy-typo-yy"));
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("install_seat_key --delete (the kill switch) never resolves the Fleet-OPS project id, so a malformed one cannot block it (kody 4368792106)", () => {
  const home = mkdtempSync(path.join(tmpdir(), "codex-seat-home-"));
  const run = (args) =>
    execFileSync("python3", ["-I", path.join(ROOT, "install_seat_key.py"), ...args], {
      env: { ...process.env, HOME: home, INFISICAL_FLEET_OPS_PROJECT_ID: "zz-typo-zz" },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  try {
    // A dry run (no --apply) writes nothing and calls no wrangler.
    assert.match(run(["CODEX", "--delete"]), /Would remove Worker secret ZULIP_KEY_CODEX\./);
    assert.match(run(["MA", "--delete"]), /Would remove Worker secret ZULIP_KEY_MA\./);
    // The install path, which reads Infisical, still refuses the malformed id
    // before any other check, and never echoes it.
    assert.throws(() => run(["CODEX"]), (err) => /is not a UUID/.test(err.stderr) && !err.stderr.includes("zz-typo-zz"));
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("install_seat_key requires its scoped token and refuses malformed Infisical responses (kody 4363264042)", () => {
  const program = [
    "import json, sys",
    "sys.path.insert(0, sys.argv[1])",
    "import install_seat_key as m",
    "import os",
    "good = [{'secretKey': 'E', 'secretValue': 'e'}, {'secretKey': 'K', 'secretValue': 'k'}]",
    "cases = [",
    "  ['not-a-dict'],                                      # response not an object",
    "  {'secrets': None},                                   # missing secrets list",
    "  {'secrets': ['not-a-dict'] + good},                  # entry not an object",
    "  {'secrets': [{'secretKey': 7, 'secretValue': 'v'}] + good},  # non-string key",
    "  {'secrets': [{'secretKey': 'E', 'secretValue': 3}, good[1]]},  # non-string value",
    "  {'secrets': [{'secretKey': 'E'}, good[1]]},          # missing value reads as empty",
    "  {'secrets': good},                                   # well formed",
    "]",
    "results = []",
    "token_name = m.INFISICAL_TOKEN_ENVS[m.INFISICAL_PROJECT]",
    "m.http_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected request'))",
    "os.environ.pop(token_name, None)",
    "try: m.infisical_pair('E', 'K', m.INFISICAL_PROJECT)",
    "except SystemExit as exc: results.append(['missing', str(exc)])",
    "os.environ[token_name] = 'automation.client-secret'",
    "try: m.infisical_pair('E', 'K', m.INFISICAL_PROJECT)",
    "except SystemExit as exc: results.append(['malformed', str(exc)])",
    "os.environ[token_name] = 'st.seat-key-read-token-fixture'",
    "for secrets in cases:",
    "    m.http_json = lambda *a, **k: secrets",
    "    try:",
    "        results.append(['ok', list(m.infisical_pair('E', 'K', m.INFISICAL_PROJECT))])",
    "    except SystemExit as exc:",
    "        results.append(['refused', str(exc)])",
    "    except Exception as exc:",
    "        results.append(['crashed', type(exc).__name__])",
    "print(json.dumps(results))",
  ].join("\n");
  const out = JSON.parse(execFileSync("python3", ["-c", program, ROOT], {
    env: { ...process.env },
    encoding: "utf8",
  }));
  const list = "install_seat_key: Infisical secrets response is malformed (missing secrets list)";
  const entry = "install_seat_key: Infisical secrets response has a malformed entry - refusing to continue on partial data";
  assert.deepEqual(out.slice(0, 2), [
    ["missing", "install_seat_key: " + "AGENT_SYNC_MCP_SEAT_KEY_AFC_INFISICAL_READ_TOKEN is missing or is not a valid service token"],
    ["malformed", "install_seat_key: " + "AGENT_SYNC_MCP_SEAT_KEY_AFC_INFISICAL_READ_TOKEN is missing or is not a valid service token"],
  ]);
  assert.deepEqual(out.slice(2, 7), [
    ["refused", list],
    ["refused", entry],
    ["refused", entry],
    ["refused", entry],
  ]);
  // A missing secretValue is read as empty (not a KeyError), so the empty
  // email is refused by the missing-secret check rather than a crash.
  assert.equal(out[7][0], "refused");
  assert.match(out[7][1], /E or K is missing or empty in Infisical/);
  // The well-formed response passes every check.
  assert.deepEqual(out[8], ["ok", ["e", "k"]]);
});
