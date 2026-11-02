// The Jet split (owner decision, Oct 23 2026):  the existing JET connection
// is wake-only - no tools, notifications only - and Jet's normal access moves
// to a new JET-CHAT seat on the same openai-dot-bot.  One bot, two seats:
// the tag expectation, not a shared secret name, ties them together.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { z } from "zod";
import path from "node:path";
import { EVENTS_ONLY_SEATS, KNOWN_SEATS, SEAT_LABELS, SEAT_SECRETS, WAKE_SECRETS, loadConfig } from "../src/config.js";
import { SEAT_TAG_EXPECTATIONS } from "../src/contract.js";
import { IdentityRouter, allowedIdentities, toolListFor } from "../src/delegated-identities.js";
import { UnknownTool } from "../src/hosted-tools.js";
import { adminSeatLabel, consentPage } from "../src/pages.js";
import { ROOT, testEnv, wranglerConfig } from "./helpers.mjs";

// The events-only mechanism is pinned with a test-local set:  the production
// constant is the staging state (empty) until the reviewed flip commit turns
// JET wake-only after the JET-CHAT login verifies live (no-gap rollout).
const TEST_EVENTS_ONLY = ["JET"];

test("JET is the wake-only seat;  JET-CHAT is a full seat on the same bot", () => {
  assert.deepEqual([...EVENTS_ONLY_SEATS], [], "staging: no seat is events-only until the post-verification flip commit");
  assert.equal(KNOWN_SEATS.indexOf("JET-CHAT"), KNOWN_SEATS.indexOf("JET") + 1);
  assert.deepEqual(SEAT_SECRETS["JET-CHAT"], { key: "ZULIP_KEY_JET_CHAT", email: "ZULIP_EMAIL_JET_CHAT", infisical: { name: "JET_ZULIP_KEY", path: "/zulip" } });
  assert.notEqual(SEAT_SECRETS["JET-CHAT"].key, SEAT_SECRETS.JET.key, "the table never shares a secret name");
  assert.deepEqual(SEAT_SECRETS["JET-CHAT"].infisical, SEAT_SECRETS.JET.infisical, "one bot key:  the runtime read is Jet's canonical record, never a copy");
  assert.equal(SEAT_LABELS["JET-CHAT"], "ChatGPT");
  assert.deepEqual(SEAT_TAG_EXPECTATIONS, { "JET-CHAT": "JET" });
  assert.ok(!("JET-CHAT" in WAKE_SECRETS), "no wake secret, so /mcp serves the full login no events");
});

test("the wake-only seat advertises no tools;  the full seat advertises them", () => {
  assert.deepEqual(toolListFor("JET", { eventsOnlySeats: TEST_EVENTS_ONLY }).list, []);
  assert.deepEqual(toolListFor("JET", { whatsappEnabled: true, eventsOnlySeats: TEST_EVENTS_ONLY }).list, [], "no flag reopens a wake-only seat");
  assert.ok(toolListFor("JET").list.length > 0, "staging state: JET still advertises tools until the flip commit");
  const names = toolListFor("JET-CHAT").list.map((t) => t.name);
  assert.deepEqual(names.slice(0, 3), ["whoami", "identities", "guide"]);
  assert.ok(names.includes("zulip_post"));
  assert.deepEqual([...allowedIdentities("JET-CHAT")], ["JET-CHAT"], "the new login acts only as itself");
});

class StubTools {
  constructor(seat) {
    this.seat = seat;
    this.audits = [];
    this.grantRef = "abcdef012345";
    this.clientId = "client";
    this.gate = { auditCall: async (row) => { this.audits.push(row); } };
  }
  async call(name) {
    throw new Error(`the wake-only gate must refuse before ${name} reaches the seat's tools`);
  }
  async safely(fn) {
    return fn();
  }
}

test("the wake-only seat refuses every tools/call name, audited, as the unknown-tool protocol error", async () => {
  const caller = new StubTools("JET");
  const r = new IdentityRouter({ callerTools: caller, eventsOnlySeats: TEST_EVENTS_ONLY });
  for (const name of ["zulip_post", "whoami", "identities", "guide", "post"]) {
    await assert.rejects(r.call(name, {}), UnknownTool, name);
  }
  assert.equal(caller.audits.length, 5);
  for (const row of caller.audits) {
    assert.equal(row.seat, "JET");
    assert.equal(row.authenticated_seat, "JET");
    assert.equal(row.error_code, "unknown_tool");
  }
});

test("admin labels tell the two Jet logins apart;  stored identifiers stay canonical", () => {
  assert.equal(adminSeatLabel("JET"), "Jet-Wake");
  assert.equal(adminSeatLabel("JET-CHAT"), "Jet");
  assert.equal(adminSeatLabel("CURSOR"), "Cursor & Grok Bots");
  assert.equal(adminSeatLabel("ECHO"), "ECHO");
});

test("the consent page names the wake seat and states its real capability", async () => {
  const facts = {
    clientName: "ChatGPT",
    clientDomain: "chatgpt.com",
    clientId: "https://chatgpt.com/oauth/client.json",
    redirectHost: "chatgpt.com",
    scopes: ["zulip:read", "zulip:write"],
    handle: "h",
    replacing: [],
  };
  const wake = await (await consentPage({ ...facts, seat: "JET", eventsOnly: true }, {})).text();
  assert.match(wake, /Connect ChatGPT as Jet-Wake\?/);
  assert.match(wake, /name="seat" value="JET"/, "the posted value stays the canonical seat");
  assert.match(wake, /This seat only delivers wake notifications to the app/);
  assert.ok(!wake.includes("This seat reads and posts"));
  const full = await (await consentPage({ ...facts, seat: "JET-CHAT", eventsOnly: false }, {})).text();
  const stagedJet = await (await consentPage({ ...facts, seat: "JET", eventsOnly: false }, {})).text();
  assert.match(stagedJet, /This seat reads and posts/, "staging state: the wake copy is flag-driven, not seat-name-driven");
  assert.match(full, /Connect ChatGPT as Jet\?/);
  assert.match(full, /name="seat" value="JET-CHAT"/);
  assert.match(full, /This seat reads and posts/);
  // The private WhatsApp inbox grant is disclosed exactly when the bridge is
  // on for this seat (kody 4365431558), and Zulip privacy is named as Zulip
  // so the two scopes cannot blur together.
  assert.ok(!wake.includes("WhatsApp"), "the wake-only seat never mentions WhatsApp");
  assert.ok(!full.includes("WhatsApp"), "no WhatsApp sentence while the bridge is off");
  assert.match(full, /Private Zulip channels are never open to it/);
  const wa = await (await consentPage({ ...facts, seat: "JET-CHAT", whatsapp: true, eventsOnly: false }, {})).text();
  assert.match(wa, /read and acknowledge messages in the owner&#39;s private WhatsApp inbox/);
  assert.match(wa, /Private Zulip channels are never open to it/);
});

test("the key installer provisions JET-CHAT from Jet's Infisical entry with the JET tag", () => {
  const installer = readFileSync(path.join(ROOT, "install_seat_key.py"), "utf8");
  assert.match(installer, /"JET-CHAT": \("ZULIP_JET", "ZULIP_KEY_JET_CHAT", "ZULIP_EMAIL_JET_CHAT"\)/);
  assert.match(installer, /EXPECTED_TAGS = \{"JET-CHAT": "JET"\}/);
  // The tag alone never grants the seat:  the installer pins the bot owner
  // (kody 4365430953), failing closed when the pin cannot be resolved.
  assert.match(installer, /str\(me\.get\("bot_owner_id"\)\) == handoff\("ZULIP_OWNER_USER_ID"\)/);
});

test("the events-only flip is a config var:  absent means no restriction, an unknown seat fails closed", () => {
  assert.deepEqual([...loadConfig(wranglerConfig().vars).eventsOnlySeats], ["JET"], "original Jet is wake-only after Jet Chat verified live");
  assert.deepEqual([...loadConfig(testEnv({ EVENTS_ONLY_SEATS: "JET" })).eventsOnlySeats], ["JET"], "the cutover flip is one reviewed var line");
  assert.throws(() => loadConfig(testEnv({ EVENTS_ONLY_SEATS: "NOPE" })), /unknown events-only seat/, "never widen to an unknown seat");
});

test("JET-CHAT is absent by default, and only hosting it unprovisioned fails closed", () => {
  // An absent optional seat loads safely:  the default production config has
  // no JET-CHAT and loads unchanged.
  const prod = loadConfig(wranglerConfig().vars);
  assert.ok(!prod.hostedSeats.includes("JET-CHAT"), "HOSTED_SEATS gains JET-CHAT only with its captured pair at deploy");
  assert.equal(prod.botEmails["JET-CHAT"], undefined);
  // The fail-closed 500 is reserved for explicitly hosting an incompletely
  // provisioned seat.
  assert.throws(() => loadConfig(testEnv({ HOSTED_SEATS: "JET,JET-CHAT" })), /ZULIP_EMAIL_JET_CHAT/, "an unprovisioned email var refuses config load rather than serving a wrong binding");
});

test("JET-CHAT loads with Jet's bot email under its own var name", () => {
  // Synthetic fixture identity (kody 4365848837):  no production address is
  // read out of wrangler.jsonc, and no production literal is pasted (kody
  // 4365545858).  Both bindings get the same obviously-fixture address (the
  // realm domain is required by config validation;  the local part names
  // itself a fixture), so the test still proves one bot serves both seats.
  const jetEmail = "jet-split-fixture-bot@simplewithus.zulipchat.com";
  const cfg = loadConfig(testEnv({ HOSTED_SEATS: "JET,JET-CHAT", ZULIP_EMAIL_JET: jetEmail, ZULIP_EMAIL_JET_CHAT: jetEmail }));
  assert.equal(cfg.botEmails["JET-CHAT"], jetEmail);
  assert.equal(cfg.botEmails.JET, cfg.botEmails["JET-CHAT"], "one bot, two seats");
});

test("the wake-only refusal never carries a secret-shaped tool name (kody 4363581984)", async () => {
  const caller = new StubTools("JET");
  const r = new IdentityRouter({ callerTools: caller, eventsOnlySeats: TEST_EVENTS_ONLY });
  // Split in source per the repo fixture convention: the scanner must see the
  // stripe-shaped value only at runtime, and gitleaks must never see it at rest.
  // The suffix is obviously synthetic (kody 4365432461/4365432924):  the Stripe
  // shape still trips the secret scan, and no reader can mistake it for a live key.
  const secretish = ["sk", "live", "FAKE".repeat(6)].join("_");
  try {
    await r.call(secretish, {});
    assert.fail("must refuse");
  } catch (error) {
    assert.ok(error instanceof UnknownTool);
    assert.equal(error.toolName, "<omitted after secret scan>", "audited name, never the raw one");
    assert.ok(!JSON.stringify(error).includes(secretish) && !String(error.stack).includes(secretish));
  }
});

test("JET-CHAT carries Jet's Linear app under its own env names (kody 4363263223)", async () => {
  const { LINEAR_SECRETS, LINEAR_SEATS, SEAT_APP_NAMES, SEAT_APP_ALIASES, seatAppNames, appNameOk, linearSecretNames } = await import("../src/linear.js");
  assert.ok(LINEAR_SEATS.includes("JET-CHAT"), "the full Jet login is Linear-enabled");
  assert.deepEqual(linearSecretNames("JET-CHAT"), { clientId: "LINEAR_JET_CHAT_CLIENT_ID", clientSecret: "LINEAR_JET_CHAT_CLIENT_SECRET" }, "own env names, never shared with JET");
  assert.notDeepEqual(linearSecretNames("JET-CHAT"), linearSecretNames("JET"));
  assert.equal(SEAT_APP_NAMES["JET-CHAT"], "Jet", "two logins, one Linear app");
  assert.deepEqual([...SEAT_APP_ALIASES["JET-CHAT"]], ["Jet (OpenAI dot)"]);
  assert.deepEqual([...seatAppNames("JET-CHAT")], [...seatAppNames("JET")], "JET-CHAT accepts exactly JET's app names");
  assert.ok(appNameOk("Jet", "JET-CHAT") && appNameOk("jet (openai dot)", "JET-CHAT"), "the viewer check passes for the shared app");
});

test("JET-CHAT's Linear pair resolves in every Infisical mode (#901 merge with #889)", async () => {
  const { LINEAR_SECRETS, LINEAR_INFISICAL, resolveLinearPair } = await import("../src/linear.js");
  const { _resetInfisicalForTests } = await import("../src/infisical.js");
  for (const seat of Object.keys(LINEAR_SECRETS)) assert.ok(LINEAR_INFISICAL[seat], `${seat} has a Fleet-OPS /linear record`);
  assert.deepEqual(LINEAR_INFISICAL["JET-CHAT"], LINEAR_INFISICAL.JET, "one Linear app:  Jet's record, never a copy");
  _resetInfisicalForTests();
  const off = await resolveLinearPair({ LINEAR_JET_CHAT_CLIENT_ID: "id-chat", LINEAR_JET_CHAT_CLIENT_SECRET: "sec-chat" }, "JET-CHAT");
  assert.deepEqual(off, { clientId: "id-chat", clientSecret: "sec-chat" }, "off mode reads JET-CHAT's own Worker names");
});

test("JET-CHAT is session-id exempt like the JET it replaces (kody 4363263820)", async () => {
  const { EXEMPT_SEATS } = await import("../src/session-ids.js");
  assert.ok(EXEMPT_SEATS.includes("JET-CHAT"));
  // Boundary check before the registry is read (kody 4365432623/4365432771):  the
  // consumed section is validated with zod, every element typed.  The full-file
  // contract stays with scripts/test_session_ids_parity.py.
  // The consumed section is fully enumerated and strict (kody 4365849243):
  // an unknown field inside sessionIds aborts the test.  The top level stays
  // loose - other registry sections evolve independently, and the full-file
  // contract stays with scripts/test_session_ids_parity.py.
  const registrySchema = z.object({
    sessionIds: z.object({
      notes: z.string(),
      idPattern: z.string().min(1),
      genericEnv: z.string().min(1),
      exemptSeats: z.array(z.string().min(1)).min(1),
      exemptPatterns: z.array(z.string()),
      envBySeat: z.record(z.array(z.string().min(1))),
      fallbackEnv: z.array(z.string().min(1)),
    }).strict(),
  }).loose();
  const registry = registrySchema.parse(JSON.parse(readFileSync(path.join(ROOT, "../../fleet-apps.json"), "utf8")));
  assert.deepEqual([...EXEMPT_SEATS].sort(), [...registry.sessionIds.exemptSeats].sort(), "code and registry stay aligned");
});
