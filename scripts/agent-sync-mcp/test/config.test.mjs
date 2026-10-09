import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { loadConfig, PUBLIC_HOST, ISSUER, RESOURCE, REFRESH_TOKEN_TTL_S, ACCESS_TOKEN_TTL_S, GATE_LOG_NAME, KNOWN_SEATS } from "../src/config.js";
import { ROOT, wranglerConfig, testEnv, CHATGPT_REDIRECT } from "./helpers.mjs";

test("wrangler.jsonc vars load, and Access fails closed until the AUD is set", () => {
  const cfg = loadConfig(wranglerConfig().vars);
  assert.equal(cfg.host, "agent-sync.jays.services");
  assert.deepEqual([...cfg.hostedSeats], ["JET", "GROK-WEB"]);
  assert.equal(cfg.redirectOwner.get(CHATGPT_REDIRECT), "JET");
  assert.equal(cfg.access.configured, false, "placeholder AUD must not count as configured");
  assert.equal(loadConfig(testEnv()).access.configured, true);
});

test("Access email list matches the Access policy the runbook creates", () => {
  const cfg = loadConfig(wranglerConfig().vars);
  assert.deepEqual([...cfg.access.ownerEmails], ["mail@jays.services", "jaywedgeworth22@gmail.com"]);
  const infra = readFileSync(path.join(ROOT, "infra_phase0.py"), "utf8");
  for (const email of cfg.access.ownerEmails) assert.ok(infra.includes(`"${email}"`), `${email} missing from infra_phase0.py`);
});

test("issuer, resource and metadata URL are fixed to the D1 hostname", () => {
  assert.equal(PUBLIC_HOST, "agent-sync.jays.services");
  assert.equal(ISSUER, "https://agent-sync.jays.services");
  assert.equal(RESOURCE, "https://agent-sync.jays.services/mcp");
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
    [{ SEATS: seats({ redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] }, { redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] }) }, /two seats/],
    [{ SEATS: { CLAUDE: { redirect_uris: [], cimd_client_ids: [] } } }, /unknown seat/],
    [{ HOSTED_SEATS: "JET,CLAUDE" }, /unknown hosted seat/],
    [{ PUBLIC_HOST: "evil.example" }, /PUBLIC_HOST/],
    [{ SEATS: "{not json" }, /not valid JSON/],
    [{ SEATS: "" }, /not set/],
  ];
  for (const [over, re] of bad) assert.throws(() => loadConfig(testEnv(over)), re, JSON.stringify(over));
});

test("wrangler.jsonc keeps every hostname but the custom domain off", () => {
  const w = wranglerConfig();
  assert.equal(w.workers_dev, false);
  assert.equal(w.preview_urls, false);
  assert.deepEqual(w.routes, [{ pattern: "agent-sync.jays.services", custom_domain: true }]);
  assert.equal(w.account_id, "3a9368057468d0909cafaa85df12d1b7");
  assert.ok(w.compatibility_flags.includes("nodejs_compat"));
  assert.ok(w.compatibility_flags.includes("global_fetch_strictly_public"));
  assert.deepEqual(w.migrations, [{ tag: "v1", new_sqlite_classes: ["SeatGate"] }]);
  assert.equal(w.kv_namespaces[0].binding, "OAUTH_KV");
  assert.equal(w.durable_objects.bindings[0].class_name, "SeatGate");
});

test("package.json pins exact versions (fleet two-week rule, see README)", () => {
  const pkg = JSON.parse(readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const all = { ...pkg.dependencies, ...pkg.devDependencies };
  for (const [name, version] of Object.entries(all)) assert.match(version, /^\d+\.\d+\.\d+$/, `${name} must be pinned exactly`);
  assert.equal(pkg.dependencies["@cloudflare/workers-oauth-provider"], "1.1.0");
  assert.equal(pkg.devDependencies.wrangler, "4.139.0");
});
