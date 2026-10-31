import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { SHARED_CHATGPT_SEATS, loadConfig, PUBLIC_HOST, ISSUER, RESOURCE, REFRESH_TOKEN_TTL_S, ACCESS_TOKEN_TTL_S, GATE_LOG_NAME, KNOWN_SEATS, SEAT_SECRETS, PROPS_PHASE, PINNED_OPERATOR_USER_ID } from "../src/config.js";
import { REALM } from "../src/zulip.js";
import { ROOT, wranglerConfig, testEnv, CHATGPT_REDIRECT } from "./helpers.mjs";

test("wrangler.jsonc vars load, and Access fails closed until the AUD is set", () => {
  const vars = wranglerConfig().vars;
  const cfg = loadConfig(vars);
  assert.equal(cfg.host, "agents.simplewithus.com");
  // Production serves both seats.  JET was held back while openai-dot-bot was a
  // realm administrator (hosted seats accept member (400) only, spec 3.6);  Jay
  // demoted it on Fri, Oct 9, and JET was deployed with its key at about 8:10am
  // (DEPLOY.md, "Re-enable JET").
  // ECHO and WISE (the Instinct app) were added on Fri, Oct 9.
  assert.deepEqual([...cfg.hostedSeats], ["JET", "GROK-WEB", "ECHO", "WISE", "CURSOR", "CLAUDE"]);
  assert.deepEqual([...loadConfig(testEnv()).hostedSeats], ["JET", "GROK-WEB"]);
  // Channels:  any public channel the bot is subscribed to (owner, Sat, Oct 10, 2026), so no allowlist;  the denylist ships empty.
  assert.deepEqual([...cfg.channelsDeny], []);
  assert.ok(!("CHANNELS" in vars), "the old two-channel allowlist is gone");
  assert.equal(cfg.channels, undefined);
  assert.equal(cfg.ownerUserId, 1211974);
  assert.equal(cfg.botEmails["GROK-WEB"], "grok-web-bot@simplewithus.zulipchat.com");
  assert.equal(cfg.redirectOwner.get(CHATGPT_REDIRECT), "JET");
  assert.equal(cfg.botEmails.ECHO, "instinct-bat-bot@simplewithus.zulipchat.com");
  assert.equal(cfg.botEmails.WISE, "instinct-owl-bot@simplewithus.zulipchat.com");
  // The Instinct app's loopback callback is shared by its two seats, and only
  // a loopback redirect may be.
  assert.deepEqual([...cfg.redirectSeats.get("http://127.0.0.1:8737/callback")], ["ECHO", "WISE"]);
  assert.equal(cfg.redirectOwner.has("http://127.0.0.1:8737/callback"), false, "a shared redirect has no single owner");
  // The Access aud is a private Cloudflare resource identifier and is NOT
  // committed:  infra_phase0.py write-config --apply provisions it as an
  // encrypted Worker secret.  The committed vars must therefore fail closed,
  // and say which variable is missing.
  assert.ok(!("ACCESS_AUD" in vars), "the Access aud must never be committed to the repo");
  assert.equal(cfg.access.configured, false);
  assert.deepEqual([...cfg.access.missing], ["ACCESS_AUD"]);
  // Still the only valid shape once provisioned, and still fail closed on a
  // placeholder or an empty value.
  assert.match(testEnv().ACCESS_AUD, /^[0-9a-f]{64}$/);
  assert.equal(loadConfig(testEnv()).access.configured, true);
  assert.equal(loadConfig(testEnv({ ACCESS_AUD: "REPLACE_WITH_ACCESS_AUD" })).access.configured, false, "placeholder AUD fails closed");
  assert.deepEqual([...loadConfig(testEnv({ ACCESS_AUD: "REPLACE_WITH_ACCESS_AUD" })).access.missing], ["ACCESS_AUD"]);
  assert.equal(loadConfig(testEnv({ ACCESS_AUD: "" })).access.configured, false, "empty AUD fails closed");
  assert.equal(loadConfig(testEnv({ ACCESS_TEAM_DOMAIN: "" })).access.configured, false, "empty team domain fails closed");
  assert.equal(loadConfig(testEnv({ OWNER_EMAILS: "" })).access.configured, false, "no owner email fails closed");
});

test("the wrangler file carries no Access aud, and infra_phase0.py provisions one", () => {
  const raw = readFileSync(path.join(ROOT, "wrangler.jsonc"), "utf8");
  assert.ok(!/"ACCESS_AUD"\s*:\s*"[0-9a-f]{64}"/.test(raw), "no aud literal in wrangler.jsonc");
  const infra = readFileSync(path.join(ROOT, "infra_phase0.py"), "utf8");
  // write-config must write live values, not only the original placeholders.
  assert.ok(!/REPLACE_WITH_ACCESS_AUD/.test(infra), "the dead placeholder substitution is gone");
  // The aud is provisioned through put_worker_secret, which also hands wrangler
  // the credentials it authenticates with - so the call carries the Cloudflare
  // handle first, then the name.
  assert.match(
    infra,
    /put_worker_secret\(\s*\w+\s*,\s*"ACCESS_AUD"/,
    "the aud is provisioned as a Worker secret",
  );
  // A child that inherits the environment runs `wrangler secret put` with none
  // of the credentials wrangler reads, which fails for a reason the operator
  // cannot see.  Pin the env and the working directory.
  assert.match(infra, /env=cf\.wrangler_env\(\)/, "wrangler gets the Cloudflare credentials");
  assert.match(infra, /cwd=HERE/, "wrangler runs in the Worker directory");
  // The retired host still has to be cleaned up and checked.
  assert.match(infra, /^RETIRED_HOST = "agent-sync\.jays\.services"$/m);
  assert.match(infra, /^RETIRED_ZONE_NAME = "jays\.services"$/m);
});

test("the Operator user id is pinned, and a malformed override fails closed", () => {
  // Unset:  the pinned fleet identity (the same constant as scripts/agent_sync/zulip.py), never a guess.
  assert.equal(loadConfig(testEnv()).operatorUserId, PINNED_OPERATOR_USER_ID);
  // Set to a real id:  that id is used.
  assert.equal(loadConfig(testEnv({ OPERATOR_ZULIP_USER_ID: "43210" })).operatorUserId, 43210);
  // Set to something that is not a Zulip user id:  refuse rather than act under a wrong identity.
  for (const bad of ["0", "-1", "abc", "1e400"]) {
    assert.throws(
      () => loadConfig(testEnv({ OPERATOR_ZULIP_USER_ID: bad })),
      /OPERATOR_ZULIP_USER_ID/,
      `"${bad}" must fail closed`,
    );
  }
});

test("Access email list is mail@jays.services only, in the Worker and in the Access policy", () => {
  // Spec 3.3.  The identity provider is One-time PIN, so every extra address is an admin credential.
  const cfg = loadConfig(wranglerConfig().vars);
  assert.deepEqual([...cfg.access.ownerEmails], ["mail@jays.services"]);
  const infra = readFileSync(path.join(ROOT, "infra_phase0.py"), "utf8");
  assert.match(infra, /^OWNER_EMAILS = \["mail@jays\.services"\]$/m, "infra_phase0.py must list the same single address");
  assert.ok(!/jaywedgeworth22/.test(infra) && !/jaywedgeworth22/.test(readFileSync(path.join(ROOT, "wrangler.jsonc"), "utf8")));
});

test("GROK-WEB uses Grok's published client metadata document, and both ids pass the strict parse", () => {
  const cfg = loadConfig(wranglerConfig().vars);
  assert.deepEqual([...cfg.seats["GROK-WEB"].cimdClientIds], ["https://grok.com/oauth/mcp-client.json"]);
  assert.deepEqual([...cfg.seats["GROK-WEB"].redirectUris], ["https://grok.com/connectors-oauth-exchange-code/"]);
  assert.equal(cfg.cimdOwner.get("https://grok.com/oauth/mcp-client.json"), "GROK-WEB");
  // console.x.ai is added only for a Business or Enterprise xAI account (spec 4).
  assert.equal(cfg.redirectOwner.has("https://console.x.ai/connectors-oauth-exchange-code/"), false);
});

test("issuer, resource and metadata URL are fixed to the D1 hostname", () => {
  assert.equal(PUBLIC_HOST, "agents.simplewithus.com");
  assert.equal(ISSUER, "https://agents.simplewithus.com");
  assert.equal(RESOURCE, "https://agents.simplewithus.com/mcp");
  assert.ok(!KNOWN_SEATS.includes(GATE_LOG_NAME));
});

test("token lifetimes are explicit integers the library accepts", () => {
  assert.equal(ACCESS_TOKEN_TTL_S, 3600);
  assert.equal(REFRESH_TOKEN_TTL_S, 90 * 86400);
  const src = readFileSync(path.join(ROOT, "src/index.js"), "utf8");
  assert.match(src, /refreshTokenTTL: REFRESH_TOKEN_TTL_S,/);
  // DCR stays off:  no registration endpoint is configured.
  assert.doesNotMatch(src, /clientRegistrationEndpoint:/);
  assert.match(src, /clientIdMetadataDocumentEnabled: true/);
  assert.match(src, /allowPlainPKCE: false/);
});

test("config refuses loose or ambiguous allowlists", () => {
  const seats = (jet, grok = { redirect_uris: [], cimd_client_ids: [] }) => ({ JET: jet, "GROK-WEB": grok });
  const bad = [
    [{ SEATS: seats({ redirect_uris: ["https://chatgpt.com/cb?x=1"], cimd_client_ids: [] }) }, /not strict/],
    [{ SEATS: seats({ redirect_uris: ["http://chatgpt.com/cb"], cimd_client_ids: [] }) }, /not strict/],
    // The loopback exception is 127.0.0.1 with an explicit port, nothing looser.
    [{ SEATS: seats({ redirect_uris: ["http://localhost:8737/cb"], cimd_client_ids: [] }) }, /not strict/],
    [{ SEATS: seats({ redirect_uris: ["http://127.0.0.1/cb"], cimd_client_ids: [] }) }, /not strict/],
    [{ SEATS: seats({ redirect_uris: ["http://[::1]:8737/cb"], cimd_client_ids: [] }) }, /not strict/],
    [{ SEATS: seats({ redirect_uris: ["http://127.0.0.2:8737/cb"], cimd_client_ids: [] }) }, /not strict/],
    [{ SEATS: seats({ redirect_uris: ["http://127.0.0.1:8737/cb?x=1"], cimd_client_ids: [] }) }, /not strict/],
    [{ SEATS: seats({ redirect_uris: ["https://127.0.0.1:8737/cb"], cimd_client_ids: [] }) }, /not strict/],
    // A CIMD client id is never http, loopback or not.
    [{ SEATS: seats({ redirect_uris: [], cimd_client_ids: ["http://127.0.0.1:8737/client.json"] }) }, /not a strict https URL/],
    // Only a loopback redirect may be shared;  a shared https redirect is refused.
    [{ SEATS: seats({ redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] }, { redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] }) }, /two seats/],
    [{ SEATS: seats({ redirect_uris: ["http://127.0.0.1:8737/callback", "http://127.0.0.1:8737/callback"], cimd_client_ids: [] }) }, /twice/],
    [{ SEATS: seats({ redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] }, { redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] }) }, /two seats/],
    [{ SEATS: { GEMINI: { redirect_uris: [], cimd_client_ids: [] } } }, /unknown seat/],
    [{ HOSTED_SEATS: "JET,GEMINI" }, /unknown hosted seat/],
    [{ PUBLIC_HOST: "evil.example" }, /PUBLIC_HOST/],
    [{ SEATS: "{not json" }, /not valid JSON/],
    [{ SEATS: "" }, /not set/],
    [{ CHANNELS_DENY: { "agent-sync": 642232 } }, /JSON array of stream ids/],
    [{ CHANNELS_DENY: ["642232"] }, /must be stream ids/],
    [{ CHANNELS_DENY: [0] }, /must be stream ids/],
    [{ CHANNELS_DENY: [642232, 642232] }, /listed twice/],
    [{ CHANNELS_DENY: "[1," }, /not valid JSON/],
    [{ OWNER_USER_ID: "" }, /OWNER_USER_ID/],
    [{ OWNER_CLIENTS: "" }, /OWNER_CLIENTS/],
    [{ ZULIP_EMAIL_GROK_WEB: "" }, /ZULIP_EMAIL_GROK_WEB/],
    [{ ZULIP_EMAIL_GROK_WEB: "grok-web-bot@evil.example" }, /ZULIP_EMAIL_GROK_WEB/],
    [{ ZULIP_EMAIL_JET: "jay@simplewithus.zulipchat.com" }, /ZULIP_EMAIL_JET/],
  ];
  for (const [over, re] of bad) assert.throws(() => loadConfig(testEnv(over)), re, JSON.stringify(over));
});

test("CHANNELS_DENY is optional:  unset, empty, a JSON string or an array of stream ids", () => {
  const { CHANNELS_DENY: _drop, ...without } = testEnv();
  assert.deepEqual([...loadConfig(without).channelsDeny], []);
  assert.deepEqual([...loadConfig(testEnv({ CHANNELS_DENY: "" })).channelsDeny], []);
  assert.deepEqual([...loadConfig(testEnv({ CHANNELS_DENY: "[700001, 642167]" })).channelsDeny], [700001, 642167]);
  assert.deepEqual([...loadConfig(testEnv({ CHANNELS_DENY: [700001] })).channelsDeny], [700001]);
  assert.ok(Object.isFrozen(loadConfig(testEnv({ CHANNELS_DENY: [700001] })).channelsDeny));
});

test("config accepts the loopback callback, shared by two seats", () => {
  const loop = "http://127.0.0.1:8737/callback";
  const cfg = loadConfig(testEnv({ SEATS: { JET: { redirect_uris: [loop], cimd_client_ids: [] }, ECHO: { redirect_uris: [loop], cimd_client_ids: [] } } }));
  assert.deepEqual([...cfg.redirectSeats.get(loop)], ["JET", "ECHO"]);
  const solo = loadConfig(testEnv({ SEATS: { ECHO: { redirect_uris: [loop], cimd_client_ids: [] }, WISE: { redirect_uris: [], cimd_client_ids: [] } } }));
  assert.equal(solo.redirectOwner.get(loop), "ECHO");
});

test("every known seat has its own key secret and email var, and the realm is compiled in", () => {
  assert.deepEqual(Object.keys(SEAT_SECRETS).sort(), [...KNOWN_SEATS].sort());
  const keys = Object.values(SEAT_SECRETS).map((s) => s.key);
  assert.equal(new Set(keys).size, keys.length);
  const w = wranglerConfig();
  for (const { key } of Object.values(SEAT_SECRETS)) assert.ok(!(key in w.vars), `${key} is a secret, never a var`);
  assert.equal(REALM, "https://simplewithus.zulipchat.com");
  assert.ok(!("REALM" in w.vars) && !("ZULIP_SITE" in w.vars), "a config edit cannot move Zulip egress");
  assert.equal(PROPS_PHASE, 2);
});

test("wrangler.jsonc keeps every hostname but the custom domain off", () => {
  const w = wranglerConfig();
  assert.equal(w.workers_dev, false);
  assert.equal(w.preview_urls, false);
  // Hard cutover (owner ruling Oct 22, 2026):  exactly one custom domain.  The
  // old host is not a route, an alias or a redirect; policy.js refuses it too.
  assert.deepEqual(w.routes, [{ pattern: "agents.simplewithus.com", custom_domain: true }]);
  assert.equal(w.account_id, "3a9368057468d0909cafaa85df12d1b7");
  assert.ok(w.compatibility_flags.includes("nodejs_compat"));
  assert.ok(w.compatibility_flags.includes("global_fetch_strictly_public"));
  assert.deepEqual(w.migrations, [{ tag: "v1", new_sqlite_classes: ["SeatGate"] }]);
  // The automatic per-request logs are off:  nothing proves they omit the Access JWT header or cookies.
  assert.equal(w.observability.enabled, true);
  assert.equal(w.observability.logs.invocation_logs, false);
  assert.equal(w.kv_namespaces[0].binding, "OAUTH_KV");
  assert.equal(w.durable_objects.bindings[0].class_name, "SeatGate");
});

test("package.json pins exact versions (fleet two-week rule, see README)", () => {
  const pkg = JSON.parse(readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const all = { ...pkg.dependencies, ...pkg.devDependencies };
  for (const [name, version] of Object.entries(all)) assert.match(version, /^\d+\.\d+\.\d+$/, `${name} must be pinned exactly`);
});


test("Cursor binds its CIMD client to its exact Cloud callback", () => {
  const cfg = loadConfig(wranglerConfig().vars);
  const callback = "https://www.cursor.com/agents/mcp/oauth/callback";
  assert.deepEqual(cfg.seats.CURSOR.redirectUris, [callback]);
  assert.deepEqual(cfg.seats.CURSOR.cimdClientIds, ["https://cursor.com/oauth/mcp-client.json"]);
  assert.equal(cfg.redirectOwner.get(callback), "CURSOR");
  assert.equal(cfg.botEmails.CURSOR, "cursor-bot@simplewithus.zulipchat.com");
  assert.deepEqual(SEAT_SECRETS.CURSOR, { key: "ZULIP_KEY_CURSOR", email: "ZULIP_EMAIL_CURSOR", infisical: { name: "CURSOR_ZULIP_KEY", path: "/zulip" } });
  for (const wrong of ["https://cursor.com/agents/mcp/oauth/callback", `${callback}/`, "http://localhost:8788/callback"]) {
    assert.equal(cfg.redirectSeats.has(wrong), false);
  }
});


test("Cursor budget overrides reject malformed, unknown and unsafe settings", () => {
  const setting = (value) => testEnv({ AGENT_SYNC_CURSOR_TOOL_BUDGETS: JSON.stringify(value) });
  for (const value of [null, [], { write: { hour: 0 } }, { other: { hour: 50 } },
    { write: { hour: 50, spacingMs: 50 } }, { linear_write: { hour: 50, day: 2 } },
    { linear_read: { hour: 50, spacingMs: 0 } }, { read: { hour: 2, extra: 1 } }]) {
    assert.throws(() => loadConfig(setting(value)), /config:/);
  }
  const policy = { linear_write: { hour: 2000, day: 20000, spacingMs: 100 } };
  const cfg = loadConfig(setting(policy));
  assert.deepEqual(cfg.cursorToolBudgets, policy);
  assert.equal(Object.isFrozen(cfg.cursorToolBudgets.linear_write), true);
});

test("the standard ChatGPT callback may be listed by JET, JET-CHAT and CODEX only, each keeping its own client", async () => {
  const { resolveArmedSeat, clientBoundToSeat, manualClientSeat } = await import("../src/policy.js");
  const entry = { redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] };
  const none = { redirect_uris: [], cimd_client_ids: [] };
  assert.deepEqual([...SHARED_CHATGPT_SEATS], ["JET", "JET-CHAT", "CODEX"]);
  // two and all three of the cohort load, the callback has no single owner, and the order is kept
  const two = loadConfig(testEnv({ SEATS: { JET: entry, "JET-CHAT": entry } }));
  assert.deepEqual([...two.redirectSeats.get(CHATGPT_REDIRECT)], ["JET", "JET-CHAT"]);
  const all = loadConfig(testEnv({ SEATS: { JET: entry, "JET-CHAT": entry, CODEX: entry } }));
  assert.deepEqual([...all.redirectSeats.get(CHATGPT_REDIRECT)], ["JET", "JET-CHAT", "CODEX"]);
  assert.equal(all.redirectOwner.has(CHATGPT_REDIRECT), false);
  // JET-CHAT alone owns it when it is the only one listing it
  assert.equal(loadConfig(testEnv({ SEATS: { JET: none, "JET-CHAT": entry } })).redirectOwner.get(CHATGPT_REDIRECT), "JET-CHAT");
  // any seat outside the cohort sharing it is refused, with the cohort or with a cohort seat first or last
  for (const other of ["GROK-WEB", "ECHO", "WISE", "CURSOR", "CLAUDE", "MA"]) {
    assert.throws(() => loadConfig(testEnv({ SEATS: { JET: entry, "JET-CHAT": entry, [other]: entry } })), /two seats/, other);
    assert.throws(() => loadConfig(testEnv({ SEATS: { [other]: entry, "JET-CHAT": entry } })), /two seats/, other);
  }
  // another https callback is never shared, even inside the cohort
  const other = "https://chatgpt.com/other_redirect";
  assert.throws(() => loadConfig(testEnv({ SEATS: { JET: { redirect_uris: [other], cimd_client_ids: [] }, "JET-CHAT": { redirect_uris: [other], cimd_client_ids: [] } } })), /two seats/);
  // listing it twice under one seat is still refused
  assert.throws(() => loadConfig(testEnv({ SEATS: { JET: { redirect_uris: [CHATGPT_REDIRECT, CHATGPT_REDIRECT], cimd_client_ids: [] } } })), /twice/);
  // a CIMD client id still belongs to exactly one seat
  const cimd = "https://example.test/client.json";
  assert.throws(() => loadConfig(testEnv({ SEATS: { JET: { ...entry, cimd_client_ids: [cimd] }, "JET-CHAT": { ...entry, cimd_client_ids: [cimd] } } })), /CIMD client id is listed under two seats/);
  // arming still picks exactly one listed seat among the three
  const listed = [...all.redirectSeats.get(CHATGPT_REDIRECT)];
  assert.deepEqual(resolveArmedSeat(listed, new Set()), { ok: false, reason: "not_armed" });
  assert.deepEqual(resolveArmedSeat(listed, new Set(["JET-CHAT"])), { ok: true, seat: "JET-CHAT" });
  assert.deepEqual(resolveArmedSeat(listed, new Set(["JET", "JET-CHAT"])), { ok: false, reason: "ambiguous_armed_seats" });
  assert.deepEqual(resolveArmedSeat(listed, new Set(["CODEX", "JET-CHAT", "JET"])), { ok: false, reason: "ambiguous_armed_seats" });
  // a manual client is bound to the seat in its tag, never to another cohort seat, and an untagged one is refused on a shared callback
  const tagged = (seat) => ({ clientId: "synthetic-manual-client", clientName: `ChatGPT (manual form, ${seat})` });
  assert.equal(manualClientSeat(tagged("JET-CHAT")), "JET-CHAT");
  assert.equal(clientBoundToSeat(tagged("JET-CHAT"), "JET-CHAT", CHATGPT_REDIRECT, all), true);
  assert.equal(clientBoundToSeat(tagged("JET-CHAT"), "JET", CHATGPT_REDIRECT, all), false);
  assert.equal(clientBoundToSeat(tagged("JET-CHAT"), "CODEX", CHATGPT_REDIRECT, all), false);
  assert.equal(clientBoundToSeat(tagged("CODEX"), "JET-CHAT", CHATGPT_REDIRECT, all), false);
  assert.equal(clientBoundToSeat({ clientId: "synthetic-manual-client", clientName: "ChatGPT" }, "JET-CHAT", CHATGPT_REDIRECT, all), false);
});

// PUBLIC_HOST is compiled; omit its redundant var to leave a Free-plan slot.
test("compiled hostname is identical with redundant PUBLIC_HOST var absent", () => {
  const withVar = loadConfig({ ...testEnv(), PUBLIC_HOST });
  const env = { ...testEnv() }; delete env.PUBLIC_HOST;
  const withoutVar = loadConfig(env);
  assert.equal(withoutVar.host, withVar.host);
  assert.equal(withoutVar.issuer, withVar.issuer);
  assert.equal(withoutVar.resource, withVar.resource);
  assert.ok(!("PUBLIC_HOST" in wranglerConfig().vars));
});
