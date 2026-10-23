// The MA seat - Muse Assist, the seat Jay calls Rob (owner request, Oct 23
// 2026:  "please make a seat for Rob on the admin page").  ROB and ROB-MUSE
// are published aliases of MA (identity-aliases.js, fleet-apps.json), and the
// admin label is the name Jay asked for:  Rob.  Code-only scaffolding:  the
// seat joins KNOWN_SEATS with its own secret names and label, so its exact
// callback pair - captured from the first refused authorize attempt, never
// guessed - can be added to SEATS at deploy.  Until the muse-assist-bot key
// and email are provisioned and the pair is configured, the seat cannot be
// hosted, armed or bound.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { KNOWN_SEATS, SEAT_LABELS, SEAT_SECRETS, WAKE_SECRETS, loadConfig } from "../src/config.js";
import { allowedIdentities, toolListFor } from "../src/delegated-identities.js";
import { seatTagFor } from "../src/contract.js";
import { ROOT, testEnv, wranglerConfig } from "./helpers.mjs";

test("MA is a known seat labeled Rob with its own secret names and full tools", () => {
  assert.ok(KNOWN_SEATS.includes("MA"));
  assert.deepEqual(SEAT_SECRETS.MA, { key: "ZULIP_KEY_MA", email: "ZULIP_EMAIL_MA" });
  assert.equal(SEAT_LABELS.MA, "Rob");
  assert.ok(!("MA" in WAKE_SECRETS), "no wake route for MA");
  assert.deepEqual([...allowedIdentities("MA")], ["MA"], "an MA grant acts only as MA");
  assert.ok(toolListFor("MA").list.length > 0, "MA gets the full hosted tool set");
  // muse-assist-bot signs as MA through the existing email-tag override.
  assert.equal(seatTagFor({ is_bot: true, email: "muse-assist-bot@simplewithus.zulipchat.com", full_name: "Muse Assist" }), "MA");
});

test("MA is not hosted yet, and hosting it without its email var fails closed", () => {
  const prod = loadConfig(wranglerConfig().vars);
  assert.ok(!prod.hostedSeats.includes("MA"), "HOSTED_SEATS gains MA only with its captured pair at deploy");
  assert.throws(() => loadConfig(testEnv({ HOSTED_SEATS: "JET,MA" })), /ZULIP_EMAIL_MA/, "an unprovisioned email var refuses config load rather than serving a wrong binding");
  const cfg = loadConfig(testEnv({ HOSTED_SEATS: "JET,MA", ZULIP_EMAIL_MA: "muse-assist-bot@simplewithus.zulipchat.com" }));
  assert.equal(cfg.botEmails.MA, "muse-assist-bot@simplewithus.zulipchat.com");
});

test("the key installer reads MA's existing Fleet-OPS entry instead of duplicating it", () => {
  const installer = readFileSync(path.join(ROOT, "install_seat_key.py"), "utf8");
  const flat = installer.replace(/\s+/g, " ");
  assert.match(flat, /FLEET_OPS_PROJECT = "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5"/);
  assert.match(flat, /"MA": \( FLEET_OPS_PROJECT, "MA_ZULIP_EMAIL", "MA_ZULIP_KEY", "ZULIP_KEY_MA", "ZULIP_EMAIL_MA", \)/);
});
