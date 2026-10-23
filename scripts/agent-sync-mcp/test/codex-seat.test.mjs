// The CODEX seat (owner request, Oct 23 2026:  Codex connects through /admin
// arming the same way Jet did).  Code-only scaffolding:  the seat joins
// KNOWN_SEATS with its own secret names and label, so its exact Codex
// callback pair - captured from the first refused authorize attempt, never
// guessed - can be added to SEATS at deploy.  Until the codex-bot key and
// email are provisioned and the pair is configured, the seat cannot be
// hosted, armed or bound.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { KNOWN_SEATS, SEAT_LABELS, SEAT_SECRETS, WAKE_SECRETS, loadConfig } from "../src/config.js";
import { allowedIdentities, toolListFor } from "../src/delegated-identities.js";
import { seatTagFor } from "../src/contract.js";
import { ROOT, testEnv, wranglerConfig } from "./helpers.mjs";

test("CODEX is a known seat with its own secret names, label and full tools", () => {
  assert.ok(KNOWN_SEATS.includes("CODEX"));
  assert.deepEqual(SEAT_SECRETS.CODEX, { key: "ZULIP_KEY_CODEX", email: "ZULIP_EMAIL_CODEX" });
  assert.equal(SEAT_LABELS.CODEX, "Codex");
  assert.ok(!("CODEX" in WAKE_SECRETS), "no wake route for Codex");
  assert.deepEqual([...allowedIdentities("CODEX")], ["CODEX"], "a Codex grant acts only as Codex");
  assert.ok(toolListFor("CODEX").list.length > 0, "Codex gets the full hosted tool set");
  // codex-bot signs as its own seat:  no tag expectation entry is needed.
  assert.equal(seatTagFor({ is_bot: true, email: "codex-bot@simplewithus.zulipchat.com", full_name: "Codex" }), "CODEX");
});

test("CODEX is not hosted yet, and hosting it without its email var fails closed", () => {
  const prod = loadConfig(wranglerConfig().vars);
  assert.ok(!prod.hostedSeats.includes("CODEX"), "HOSTED_SEATS gains CODEX only with its captured pair at deploy");
  assert.throws(() => loadConfig(testEnv({ HOSTED_SEATS: "JET,CODEX" })), /ZULIP_EMAIL_CODEX/, "an unprovisioned email var refuses config load rather than serving a wrong binding");
  const cfg = loadConfig(testEnv({ HOSTED_SEATS: "JET,CODEX", ZULIP_EMAIL_CODEX: "codex-bot@simplewithus.zulipchat.com" }));
  assert.equal(cfg.botEmails.CODEX, "codex-bot@simplewithus.zulipchat.com");
});

test("the key installer reads Codex's existing Fleet-OPS entry instead of duplicating it", () => {
  const installer = readFileSync(path.join(ROOT, "install_seat_key.py"), "utf8");
  const flat = installer.replace(/\s+/g, " ");
  // The Fleet-OPS project id itself stays out of fixtures (fleet-infra mock-data
  // rule);  the test pins the consolidation mapping, not the literal.
  // The project is resolved at call time (None marker in the table);  the
  // behavioral resolution is pinned in the seat_entry test below.
  assert.match(flat, /"CODEX": \( None, .*"CODEX_ZULIP_EMAIL", "CODEX_ZULIP_KEY", "ZULIP_KEY_CODEX", "ZULIP_EMAIL_CODEX", \)/);
});

test("install_seat_key seat_entry: CODEX 5-tuple and legacy derivation are behavioral, project id resolvable (kody 4363264472 + 4363264918)", () => {
  const program = [
    "import json, sys",
    "sys.path.insert(0, sys.argv[1])",
    "import install_seat_key as m",
    "print(json.dumps({'codex': m.seat_entry('CODEX'), 'cursor': m.seat_entry('CURSOR'), 'afc': m.INFISICAL_PROJECT, 'fleetops': m.FLEET_OPS_PROJECT}))",
  ].join("\n");
  const run = (env = {}) =>
    JSON.parse(execFileSync("python3", ["-c", program, ROOT], { env: { ...process.env, ...env }, encoding: "utf8" }));
  const d = run();
  // Ordered tuples, names pinned; the project ids themselves come from the
  // module (fleet-infra mock-data rule: no literal ids in fixtures).
  assert.deepEqual(d.codex, [d.fleetops, "CODEX_ZULIP_EMAIL", "CODEX_ZULIP_KEY", "ZULIP_KEY_CODEX", "ZULIP_EMAIL_CODEX"]);
  assert.deepEqual(d.cursor, [d.afc, "ZULIP_CURSOR_EMAIL", "ZULIP_CURSOR_API_KEY", "ZULIP_KEY_CURSOR", "ZULIP_EMAIL_CURSOR"]);
  // Env resolution wins over the committed fallback; legacy seats unaffected.
  const mockId = "00000000-0000-4000-8000-000000000000";
  const d2 = run({ INFISICAL_FLEET_OPS_PROJECT_ID: mockId });
  assert.equal(d2.codex[0], mockId, "INFISICAL_FLEET_OPS_PROJECT_ID resolves ahead of the fallback");
  assert.equal(d2.cursor[0], d.afc);
});

test("install_seat_key refuses malformed Infisical responses instead of reading partial data (kody 4363264042)", () => {
  const program = [
    "import json, sys",
    "sys.path.insert(0, sys.argv[1])",
    "import install_seat_key as m",
    "cases = [",
    "  {'login': ['not-a-dict']},                       # login not an object",
    "  {'login': {'accessToken': 7}},                   # token not a string",
    "  {'login': {'accessToken': 't'}, 'secrets': None},  # missing secrets list",
    "  {'login': {'accessToken': 't'}, 'secrets': [{'secretKey': 'A', 'secretValue': 3}]},  # non-string value",
    "  {'login': {'accessToken': 't'}, 'secrets': [{'secretKey': 'A'}]},  # missing value is ok (None)",
    "]",
    "results = []",
    "for c in cases:",
    "    calls = iter([c['login'], {'secrets': c.get('secrets')}])",
    "    m.http_json = lambda *a, **k: next(calls)",
    "    try:",
    "        m.infisical_pair('p', 'E', 'K')",
    "        results.append('no-fail')",
    "    except SystemExit:",
    "        results.append('refused')",
    "print(json.dumps(results))",
  ].join("\n");
  const out = JSON.parse(execFileSync("python3", ["-c", program, ROOT], {
    env: { ...process.env, INFISICAL_AUTOMATION_CLIENT_ID: "x", INFISICAL_AUTOMATION_CLIENT_SECRET: "y" },
    encoding: "utf8",
  }));
  // Every malformed shape refused; only the missing-value case (None -> "") proceeds
  // far enough to fail on the missing E/K names instead of a shape error - still refused.
  assert.deepEqual(out, ["refused", "refused", "refused", "refused", "refused"]);
});
