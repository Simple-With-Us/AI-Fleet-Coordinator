import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { loadConfig, KNOWN_SEATS, SEAT_SECRETS, SEAT_LABELS, WAKE_SECRETS } from "../src/config.js";
import { clientBoundToSeat, preGateAuthorize } from "../src/policy.js";
import { HostedTools, seatProblem } from "../src/hosted-tools.js";
import { LINEAR_SEATS } from "../src/linear.js";
import { ROOT, testEnv, wranglerConfig, authorizeParams, CHATGPT_CLIENT, CHATGPT_REDIRECT } from "./helpers.mjs";

const REDIRECT = "https://claude.ai/api/mcp/auth_callback";
const CLIENT = "AbCdEf1234567890";
const cfg = loadConfig(testEnv({ HOSTED_SEATS: "JET,CLAUDE" }));
const client = { clientId: CLIENT, clientName: "Claude (manual form, CLAUDE)" };

test("hosted CLAUDE has an independent bot mapping and exact hosted callback; CODEX remains unenabled", () => {
  const prod = loadConfig(wranglerConfig().vars);
  assert.ok(prod.hostedSeats.includes("CLAUDE"));
  assert.equal(prod.botEmails.CLAUDE, "claude-bot@simplewithus.zulipchat.com");
  assert.deepEqual(SEAT_SECRETS.CLAUDE, { key: "ZULIP_CLAUDE_API_KEY", email: "ZULIP_CLAUDE_EMAIL" });
  assert.equal(SEAT_LABELS.CLAUDE, "Claude");
  assert.deepEqual([...prod.seats.CLAUDE.redirectUris], [REDIRECT]);
  assert.deepEqual([...prod.seats.CLAUDE.cimdClientIds], []);
  assert.equal(prod.redirectOwner.get(REDIRECT), "CLAUDE");
  assert.equal(prod.redirectOwner.get(CHATGPT_REDIRECT), "JET");
  assert.equal(prod.cimdOwner.get(CHATGPT_CLIENT), "JET");
  assert.equal(prod.redirectOwner.get("https://www.cursor.com/agents/mcp/oauth/callback"), "CURSOR");
  assert.ok(!KNOWN_SEATS.includes("CODEX"));
  assert.ok(!("CLAUDE" in WAKE_SECRETS));
  assert.ok(LINEAR_SEATS.includes("CLAUDE"));
  const installer = readFileSync(path.join(ROOT, "install_seat_key.py"), "utf8");
  assert.match(installer, /"CLAUDE": \("ZULIP_CLAUDE", "ZULIP_CLAUDE_API_KEY", "ZULIP_CLAUDE_EMAIL"\)/);
});

test("CLAUDE public manual client and JET clients cannot cross-bind", () => {
  assert.equal(clientBoundToSeat(client, "CLAUDE", REDIRECT, cfg), true);
  assert.equal(clientBoundToSeat(client, "JET", CHATGPT_REDIRECT, cfg), false);
  const cursor = { clientId: "CuRsOr1234567890", clientName: "Cursor Cloud (manual form, CURSOR)" };
  assert.equal(clientBoundToSeat(cursor, "CLAUDE", REDIRECT, cfg), false);
  assert.equal(clientBoundToSeat(client, "CURSOR", "https://www.cursor.com/agents/mcp/oauth/callback", cfg), false);
  const jet = { clientId: "ZyXwVu9876543210", clientName: "ChatGPT (manual form, JET)" };
  assert.equal(clientBoundToSeat(jet, "CLAUDE", REDIRECT, cfg), false);
  const cimd = { clientId: CHATGPT_CLIENT, clientName: "Claude (manual form, CLAUDE)" };
  assert.equal(clientBoundToSeat(cimd, "CLAUDE", REDIRECT, cfg), false);
  const pre = preGateAuthorize(authorizeParams({ client_id: CLIENT, redirect_uri: REDIRECT }), cfg);
  assert.equal(pre.ok, true);
  assert.equal(pre.seat, "CLAUDE");
  assert.equal(preGateAuthorize(authorizeParams({ client_id: CHATGPT_CLIENT, redirect_uri: REDIRECT }), cfg).ok, false);
});

test("Claude callback lookalikes, native callbacks, unlisted CIMD and missing/plain PKCE fail closed", () => {
  for (const redirect_uri of [REDIRECT + "/", REDIRECT + "?x=1", REDIRECT + "#x", "https://claude.ai.evil.example/api/mcp/auth_callback", "https://claude.ai:444/api/mcp/auth_callback", "http://localhost:3118/callback", "http://127.0.0.1:3118/callback"]) {
    assert.equal(preGateAuthorize(authorizeParams({ client_id: CLIENT, redirect_uri }), cfg).ok, false, redirect_uri);
  }
  for (const over of [{ code_challenge: undefined }, { code_challenge_method: "plain" }, { client_id: "https://claude.ai/unverified/client.json" }]) {
    assert.equal(preGateAuthorize(authorizeParams({ client_id: CLIENT, redirect_uri: REDIRECT, ...over }), cfg).ok, false);
  }
});

test("CLAUDE accepts only its own member bot, never JET or an administrator", () => {
  const expected = { seat: "CLAUDE", email: "claude-bot@simplewithus.zulipchat.com" };
  const me = { email: expected.email, full_name: "Claude", is_bot: true, role: 400, is_admin: false, is_owner: false };
  assert.equal(seatProblem(me, expected), null);
  for (const override of [{ email: "openai-dot-bot@simplewithus.zulipchat.com", full_name: "Jet" }, { role: 200, is_admin: true }, { role: 100, is_owner: true }, { is_bot: false }]) {
    assert.ok(seatProblem({ ...me, ...override }, expected));
  }
});

test("unprovisioned CLAUDE key fails closed for identity and writes", async () => {
  const tools = new HostedTools({
    seat: "CLAUDE", scopes: ["zulip:read", "zulip:write"], clientId: CLIENT,
    grantRef: "abc123", paused: false, config: cfg, zulip: null, key: "",
    gate: { auditCall: async () => {} }, log: () => {},
  });
  for (const [name, args] of [["whoami", {}], ["zulip_post", { topic: "sandbox", text: "probe" }]]) {
    const result = await tools.call(name, args);
    assert.equal(result.isError, true);
    assert.equal(result._meta["agent-sync/error"].code, "not_authorized");
    assert.match(result._meta["agent-sync/error"].message, /No Zulip key is installed for CLAUDE/);
    assert.equal(result.structuredContent, undefined);
  }
});

test("CLAUDE canonical email is required; other seats keep their existing binding names", () => {
  assert.throws(() => loadConfig(testEnv({ HOSTED_SEATS: "CLAUDE", ZULIP_CLAUDE_EMAIL: "", ZULIP_EMAIL_CLAUDE: "claude-bot@simplewithus.zulipchat.com" })), /ZULIP_CLAUDE_EMAIL/);
  assert.deepEqual(Object.fromEntries(Object.entries(SEAT_SECRETS).filter(([seat]) => seat !== "CLAUDE")), {
    CURSOR: { key: "ZULIP_KEY_CURSOR", email: "ZULIP_EMAIL_CURSOR" },
    JET: { key: "ZULIP_KEY_JET", email: "ZULIP_EMAIL_JET" },
    "GROK-WEB": { key: "ZULIP_KEY_GROK_WEB", email: "ZULIP_EMAIL_GROK_WEB" },
    ECHO: { key: "ZULIP_KEY_ECHO", email: "ZULIP_EMAIL_ECHO" },
    WISE: { key: "ZULIP_KEY_WISE", email: "ZULIP_EMAIL_WISE" },
    MA: { key: "ZULIP_KEY_MA", email: "ZULIP_EMAIL_MA" },
  });
});
