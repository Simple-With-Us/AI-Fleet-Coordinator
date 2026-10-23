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
  assert.match(installer, /FLEET_OPS_PROJECT = "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5"/);
  assert.match(installer, /"CODEX": \(FLEET_OPS_PROJECT, "CODEX_ZULIP_EMAIL", "CODEX_ZULIP_KEY", "ZULIP_KEY_CODEX", "ZULIP_EMAIL_CODEX"\)/);
});
