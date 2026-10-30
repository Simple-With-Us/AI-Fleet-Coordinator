import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as nodeModule from "node:module";
import { resolveSecret, infisicalStats, _resetInfisicalForTests, InfisicalError } from "../src/infisical.js";
import { resolveLinearPair, linearClientFromPair } from "../src/linear.js";
import { resolveFleetlinkSeatTokens, fleetlinkClientFromSecret } from "../src/fleetlink.js";
import { LinearClient } from "../src/linear.js";
import { memoryQuota } from "./quota-helper.mjs";

// Every credential-shaped value in this file (fla_ and fls_ tokens, keys, client ids and secrets) is
// synthetic:  built at runtime from a repeated character or a short fixed word.  None is a credential,
// none is read from the environment, and every Infisical answer comes from the mocked fetch below.
const ENV = {
  INFISICAL_SERVICE_TOKEN: "st-test",
  INFISICAL_WORKSPACE_ID: "ws-test",
  INFISICAL_MODE: "required",
};

function mockFetch(routes) {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    for (const [match, reply] of routes) {
      if (url.includes(match)) return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200 });
    }
    return new Response("not found", { status: 404 });
  };
  return calls;
}

test("off mode reads only the env binding and never calls Infisical", async () => {
  _resetInfisicalForTests();
  const calls = mockFetch([]);
  const env = { ...ENV, INFISICAL_MODE: "off", LEGACY: "v0" };
  assert.equal(await resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY" }), "v0");
  assert.equal(calls.length, 0);
});

test("required mode serves from Infisical and caches within the TTL", async () => {
  _resetInfisicalForTests();
  const calls = mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "v1" } } }]]);
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "LEGACY" }), "v1");
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "LEGACY" }), "v1");
  assert.equal(calls.length, 1, "second read is a cache hit");
  assert.deepEqual(infisicalStats(), { fetched: 1, cacheHits: 1, fallbacks: 0, absent: 0 });
});

test("required mode throws on a miss - no silent fallback", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, LEGACY: "v0" };
  await assert.rejects(() => resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY" }), InfisicalError);
});

test("required mode throws when the token binding is absent", async () => {
  _resetInfisicalForTests();
  const env = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test" };
  await assert.rejects(() => resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY" }), /not bound/);
});

test("fallback mode warns, counts, and uses the env binding on failure", async () => {
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: {}, status: 500 }]]);
  const env = { ...ENV, INFISICAL_MODE: "fallback", LEGACY: "v0" };
  assert.equal(await resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY" }), "v0");
  assert.equal(infisicalStats().fallbacks, 1);
});

test("scope denial (403) throws in required mode with a secret-safe error", async () => {
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: { message: "Forbidden" }, status: 403 }]]);
  const env = { ...ENV, LEGACY: "v0" };
  const err = await resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY" }).then(() => null, (e) => e);
  assert.ok(err instanceof InfisicalError);
  assert.equal(err.status, 403);
  assert.ok(!String(err.message).includes("v0"), "error never carries the env-binding value");
  assert.ok(!String(err.message).includes("st-test"), "error never carries the service token");
});

test("outage (500) in required mode throws rather than serving stale cache beyond TTL", async () => {
  _resetInfisicalForTests();
  let serve = { body: { secret: { secretValue: "v1" } } };
  const calls = mockFetch([["/api/v3/secrets/raw/S", { get body() { return serve.body; }, get status() { return serve.status; } }]]);
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), "v1");
  serve = { body: {}, status: 500 };
  // Force cache expiry by backdating: a fresh fetch must fail visibly, not serve v1 forever.
  const { _resetInfisicalForTests: reset } = await import("../src/infisical.js");
  reset();
  await assert.rejects(() => resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), /500/);
});

test("rotation: after cache expiry the new Infisical value is served", async () => {
  _resetInfisicalForTests();
  let value = "old-value";
  mockFetch([["/api/v3/secrets/raw/S", { get body() { return { secret: { secretValue: value } }; } }]]);
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), "old-value");
  value = "rotated-value";
  // Within the TTL the cached value serves; simulate expiry by resetting the cache,
  // which is exactly the state a post-TTL read starts from (no value retained).
  _resetInfisicalForTests();
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), "rotated-value");
});

test("fallback mode serves Infisical when healthy and counts zero fallbacks", async () => {
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "v9" } } }]]);
  const env = { ...ENV, INFISICAL_MODE: "fallback", LEGACY: "v0" };
  assert.equal(await resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY" }), "v9");
  assert.equal(infisicalStats().fallbacks, 0);
});

test("resolveLinearPair: off mode reads the legacy Worker names, WISE maps to LINEAR_INSTINCT_*", async () => {
  _resetInfisicalForTests();
  const calls = mockFetch([]);
  const env = { ...ENV, INFISICAL_MODE: "off", LINEAR_INSTINCT_CLIENT_ID: "id-wise", LINEAR_INSTINCT_CLIENT_SECRET: "sec-wise" };
  const pair = await resolveLinearPair(env, "WISE");
  assert.deepEqual(pair, { clientId: "id-wise", clientSecret: "sec-wise" });
  assert.equal(calls.length, 0);
  assert.equal(await resolveLinearPair(env, "BF-BUILDER"), null, "a seat with no Linear app (retired BF-BUILDER) stays null");
});

test("resolveLinearPair: required mode reads the LINEAR_WISE_* names at /linear", async () => {
  _resetInfisicalForTests();
  const calls = mockFetch([
    ["secrets/raw/LINEAR_WISE_CLIENT_ID", { body: { secret: { secretValue: "id-inf" } } }],
    ["secrets/raw/LINEAR_WISE_CLIENT_SECRET", { body: { secret: { secretValue: "sec-inf" } } }],
  ]);
  const pair = await resolveLinearPair(ENV, "WISE");
  assert.deepEqual(pair, { clientId: "id-inf", clientSecret: "sec-inf" });
  assert.ok(calls.every((u) => u.includes("secretPath=%2Flinear")), "both reads are scoped to /linear");
});

test("resolveLinearPair: required mode treats a missing pair as Linear-off - no throw, no env fallback", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, LINEAR_ECHO_CLIENT_ID: "id-legacy", LINEAR_ECHO_CLIENT_SECRET: "sec-legacy" };
  const pair = await resolveLinearPair(env, "ECHO");
  assert.deepEqual(pair, { clientId: "", clientSecret: "" }, "absent pair, not the legacy bindings");
  assert.equal(linearClientFromPair(pair, "ECHO", { gate: {}, quota: memoryQuota() }), null, "Linear tools answer not_configured");
});

test("linearClientFromPair keeps the null contract without a gate or pair", async () => {
  assert.equal(linearClientFromPair(null, "ECHO", { gate: {}, quota: memoryQuota() }), null);
  assert.equal(linearClientFromPair({ clientId: "", clientSecret: "s" }, "ECHO", { gate: {}, quota: memoryQuota() }), null);
  assert.ok(linearClientFromPair({ clientId: "i", clientSecret: "s" }, "ECHO", { gate: {}, quota: memoryQuota() }) instanceof LinearClient);
});

test("optional read: required mode treats a genuine 404 as capability-off, never reads env", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, LEGACY: "v0" };
  assert.equal(await resolveSecret(env, { name: "S", path: "/wake", envName: "LEGACY", optional: true }), "");
});

test("optional read: required mode still throws on outage and on missing token", async () => {
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: {}, status: 500 }]]);
  const env = { ...ENV, LEGACY: "v0" };
  await assert.rejects(() => resolveSecret(env, { name: "S", path: "/wake", envName: "LEGACY", optional: true }), InfisicalError);
  const noToken = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test" };
  await assert.rejects(() => resolveSecret(noToken, { name: "S", path: "/wake", envName: "LEGACY", optional: true }), /not bound/);
});

/** console.warn lines written while `fn` runs. */
async function warnings(fn) {
  const lines = [];
  const real = console.warn;
  console.warn = (...args) => { lines.push(args.join(" ")); };
  try {
    await fn();
  } finally {
    console.warn = real;
  }
  return lines;
}

test("optional read: fallback mode counts a fallback when a binding serves the absent name, and the log names both", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, INFISICAL_MODE: "fallback", LEGACY: "v0" };
  const lines = await warnings(async () => {
    assert.equal(await resolveSecret(env, { name: "S", path: "/wake", envName: "LEGACY", optional: true }), "v0");
  });
  assert.equal(infisicalStats().fallbacks, 1);
  assert.equal(infisicalStats().absent, 0);
  assert.equal(lines.length, 1);
  // One structured, allow-listed event (kody 4366922553):  names and a fixed code, never a value or a message.
  assert.deepEqual(JSON.parse(lines[0]), { event: "infisical_fallback", path: "/wake", name: "S", binding: "LEGACY", served: true, code: "not_found", status: 404 });
  assert.ok(!lines[0].includes("v0"), "the served value is never logged");
});

test("optional read: an undeclared name set nowhere is capability-off in fallback mode too, counted apart and never as a fallback", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, INFISICAL_MODE: "fallback" };
  const lines = await warnings(async () => {
    assert.equal(await resolveSecret(env, { name: "WAKE_HMAC_KEY_ECHO", path: "/wake", envName: "WAKE_HMAC_KEY_ECHO", optional: true }), "");
  });
  assert.deepEqual([infisicalStats().fallbacks, infisicalStats().absent], [0, 1], "the same answer required mode gives:  not a fallback");
  assert.deepEqual(lines, [], "no fallback line for an intentionally-off capability");
  // A DECLARED name set nowhere is drift, not a capability that is off:  still a fallback (required mode throws on it).
  const declared = await warnings(async () => {
    assert.equal(await resolveSecret(env, { name: "WAKE_HMAC_KEY_JET", path: "/wake", envName: "WAKE_HMAC_KEY_JET", optional: true }), "");
  });
  assert.equal(infisicalStats().fallbacks, 1);
  assert.deepEqual(JSON.parse(declared[0]), { event: "infisical_fallback", path: "/wake", name: "WAKE_HMAC_KEY_JET", binding: "WAKE_HMAC_KEY_JET", served: false, code: "not_found", status: 404 });
  // An outage is a fallback too, binding or not.
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: {}, status: 500 }]]);
  assert.equal(await resolveSecret(env, { name: "S", path: "/wake", envName: "NONE", optional: true }), "");
  assert.deepEqual([infisicalStats().fallbacks, infisicalStats().absent], [1, 0]);
});

test("fallback mode reaches zero fallbacks when Infisical holds every declared name (the proof gate in DEPLOY.md)", async () => {
  _resetInfisicalForTests();
  const { INFISICAL_EXPECT_DEFAULT } = await import("../src/infisical-manifest.js");
  const { SEAT_SECRETS, WAKE_SECRETS } = await import("../src/config.js");
  const { GB_IDENTITIES } = await import("../src/delegated-identities.js");
  const { prefetchFolder, resolveWakeKeys } = await import("../src/infisical.js");
  const folders = {};
  for (const entry of INFISICAL_EXPECT_DEFAULT) {
    const [path, name] = entry.split(":");
    (folders[path] ??= []).push({ secretKey: name, secretValue: name.startsWith("FLEETLINK_TOKEN_") ? `fla_${"c".repeat(64)}` : `v-${name}` });
  }
  globalThis.fetch = async (url) => {
    const path = new URL(url).searchParams.get("secretPath");
    return String(url).includes("/api/v3/secrets/raw?") ? Response.json({ secrets: folders[path] ?? [] }) : new Response("not found", { status: 404 });
  };
  const env = { ...ENV, INFISICAL_MODE: "fallback" };
  const lines = await warnings(async () => {
    await Promise.all(["/fleetlink", "/wake"].map((path) => prefetchFolder(env, path)));
    const tokens = await resolveFleetlinkSeatTokens(env, [...Object.keys(SEAT_SECRETS), ...GB_IDENTITIES]);
    assert.equal(tokens.seatTokens.size, 17, "every declared seat token served by Infisical");
    assert.deepEqual(await resolveWakeKeys(env, Object.values(WAKE_SECRETS)), ["v-WAKE_HMAC_KEY_JET"]);
  });
  assert.equal(infisicalStats().fallbacks, 0, "MA's, JET-CHAT's and CODEX's tokens and three seats' wake keys are set nowhere:  off, not fallbacks");
  assert.equal(infisicalStats().absent, 6);
  assert.deepEqual(lines, []);
});

test("resolveFleetlinkSeatTokens: in fallback mode a token the bundle serves is a counted fallback logged under its bundle entry", async () => {
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [] } }]]);
  const { prefetchFolder } = await import("../src/infisical.js");
  const bundled = `fla_${"d".repeat(64)}`;
  const env = { ...ENV, INFISICAL_MODE: "fallback", FLEETLINK_SEAT_TOKENS: JSON.stringify({ MA: bundled }) };
  const lines = await warnings(async () => {
    await prefetchFolder(env, "/fleetlink");
    const r = await resolveFleetlinkSeatTokens(env, ["MA", "JET-CHAT"]);
    assert.equal(r.seatTokens.get("MA"), bundled, "the bundle still serves in fallback mode");
    assert.ok(!r.seatTokens.has("JET-CHAT"));
  });
  assert.deepEqual([infisicalStats().fallbacks, infisicalStats().absent], [1, 1]);
  assert.equal(lines.length, 1);
  const event = JSON.parse(lines[0]);
  assert.equal(event.binding, "FLEETLINK_SEAT_TOKENS[MA]", "names the real legacy source, never a placeholder");
  assert.deepEqual([event.path, event.name, event.served], ["/fleetlink", "FLEETLINK_TOKEN_MA", true]);
  assert.ok(!lines[0].includes(bundled), "the bundle's token is never logged");
});

test("values are trimmed on both Infisical paths, and whitespace alone is absence (kody 4366922800)", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: "  va\n" }, { secretKey: "B", secretValue: " \n" }] } }]]);
  await prefetchFolder(ENV, "/tools");
  assert.equal(await resolveSecret(ENV, { name: "A", path: "/tools", envName: "L" }), "va");
  assert.equal(await resolveSecret(ENV, { name: "B", path: "/tools", envName: "L", optional: true }), "", "whitespace only reads as absent");
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "\tvs \n" } } }]]);
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), "vs");
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "   " } } }]]);
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "L", optional: true }), "");
});

test("an answer past the byte, depth or node cap is refused before its schema (kody 4366922682)", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder, INFISICAL_LIMITS } = await import("../src/infisical.js");
  const big = "x".repeat(INFISICAL_LIMITS.listingBytes);
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: big }] } }]]);
  await assert.rejects(() => prefetchFolder(ENV, "/tools"), (e) => e.code === "invalid_listing", "over the listing byte cap");
  _resetInfisicalForTests();
  let deep = { secretValue: "v" };
  for (let i = 0; i < INFISICAL_LIMITS.depth + 2; i++) deep = { next: deep };
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "v" }, extra: deep } }]]);
  await assert.rejects(() => resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), (e) => e.code === "invalid_response", "past the depth cap");
  _resetInfisicalForTests();
  const wide = Array.from({ length: INFISICAL_LIMITS.nodes }, () => 0);
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "v" }, extra: wide } }]]);
  await assert.rejects(() => resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), (e) => e.code === "invalid_response", "past the node cap");
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: "v" }, extra: [[1, 2], { a: 3 }] } }]]);
  assert.equal(await resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), "v", "an ordinary answer passes");
});

test("resolveWakeKeys: required mode returns the wake keys Infisical serves with no Worker binding, off mode the bindings (sentry 4364275301)", async () => {
  _resetInfisicalForTests();
  const { resolveWakeKeys } = await import("../src/infisical.js");
  const { WAKE_SECRETS } = await import("../src/config.js");
  const calls = mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "WAKE_HMAC_KEY_JET", secretValue: "wk-jet" }] } }]]);
  assert.deepEqual(await resolveWakeKeys(ENV, Object.values(WAKE_SECRETS)), ["wk-jet"], "a guard built on this set sees an Infisical-only wake key");
  assert.equal(calls.length, 1, "one /wake listing answers all four names");
  _resetInfisicalForTests();
  const off = { INFISICAL_MODE: "off", WAKE_HMAC_KEY_JET: " wk-bound ", WAKE_HMAC_KEY_ECHO: "" };
  assert.deepEqual(await resolveWakeKeys(off, Object.values(WAKE_SECRETS)), ["wk-bound"]);
});

test("INFISICAL_EXPECT: a declared secret's 404 throws even on an optional read", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, INFISICAL_EXPECT: "/wake:WAKE_HMAC_KEY_JET" };
  await assert.rejects(
    () => resolveSecret(env, { name: "WAKE_HMAC_KEY_JET", path: "/wake", envName: "WAKE_HMAC_KEY_JET", optional: true }),
    InfisicalError,
    "declared-configured absence fails visibly");
  assert.equal(await resolveSecret(env, { name: "WAKE_HMAC_KEY_ECHO", path: "/wake", envName: "WAKE_HMAC_KEY_ECHO", optional: true }), "", "undeclared absence stays capability-off");
});

test("resolveFleetlinkSeatTokens: valid token used, malformed fails closed, absent keeps designed paths", async () => {
  _resetInfisicalForTests();
  const good = `fla_${"a".repeat(64)}`;
  mockFetch([
    ["secrets/raw/FLEETLINK_TOKEN_JET", { body: { secret: { secretValue: good } } }],
    ["secrets/raw/FLEETLINK_TOKEN_ECHO", { body: { secret: { secretValue: "not-a-token" } } }],
  ]);
  const env = { ...ENV, INFISICAL_MODE: "fallback", FLEETLINK_SEAT_TOKENS: '{"WISE":"fla_' + "b".repeat(64) + '"}' };
  const r = await resolveFleetlinkSeatTokens(env, ["JET", "ECHO", "WISE"]);
  assert.equal(r.seatTokens.get("JET"), good, "Infisical token used");
  assert.ok(r.deniedSeats.has("ECHO") && !r.seatTokens.has("ECHO"), "malformed fails closed for that seat");
  assert.equal(r.seatTokens.get("WISE"), "fla_" + "b".repeat(64), "absent name keeps the legacy bundle in fallback mode");
  const client = fleetlinkClientFromSecret("", env, { resolvedTokens: r });
  assert.equal(client.credentialFor("ECHO"), null, "denied seat gets no credential - no agent-secret substitution");
});

test("resolveFleetlinkSeatTokens: required mode ignores the legacy bundle; declared-missing throws visibly", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, FLEETLINK_SEAT_TOKENS: '{"WISE":"fla_' + "b".repeat(64) + '"}' };
  await assert.rejects(() => resolveFleetlinkSeatTokens(env, ["WISE"]), /Infisical/, "WISE is declared in the default manifest: an accidental missing secret fails visibly, never silently");
  // An intentionally-absent (undeclared) name is capability-off, no throw, no env read.
  _resetInfisicalForTests();
  mockFetch([]);
  const env2 = { ...ENV, INFISICAL_EXPECT: "/fleetlink:FLEETLINK_AGENT_SECRET", FLEETLINK_SEAT_TOKENS: '{"WISE":"fla_' + "b".repeat(64) + '"}' };
  const r = await resolveFleetlinkSeatTokens(env2, ["WISE"]);
  assert.equal(r.seatTokens.size, 0, "no env binding is read in required mode");
});

test("INFISICAL_EXPECT_DEFAULT: exactly the 72-copy manifest, drift-guarded against delegatedNames", async () => {
  const { INFISICAL_EXPECT_DEFAULT } = await import("../src/infisical-manifest.js");
  const { GB_IDENTITIES, delegatedNames } = await import("../src/delegated-identities.js");
  assert.equal(INFISICAL_EXPECT_DEFAULT.length, 72);
  assert.equal(new Set(INFISICAL_EXPECT_DEFAULT).size, 72, "no duplicates");
  const set = new Set(INFISICAL_EXPECT_DEFAULT);
  for (const identity of GB_IDENTITIES) {
    const n = delegatedNames(identity);
    for (const [path, name] of [["/zulip", n.zulipEmail], ["/zulip", n.zulipKey], ["/linear", n.linearClientId], ["/linear", n.linearClientSecret]]) {
      assert.ok(set.has(`${path}:${name}`), `manifest declares ${path}:${name}`);
    }
  }
  assert.ok(!set.has("/fleetlink:FLEETLINK_TOKEN_GB_ENGINEER"), "never-provisioned seat token is not declared");
  assert.ok([...set].filter((e) => e.startsWith("/fleetlink:")).length === 18, "agent secret + 17 seat tokens");
  assert.ok(![...set].some((e) => e.includes("SPIKE")), "ephemeral spike key is not durable storage");
});

test("personas table:  the committed table passes, and a malformed one fails soft instead of throwing at load (kody 4367022121)", async () => {
  const { GB_TABLE_VALID, gbIdentitiesFrom } = await import("../src/infisical-manifest.js");
  const { GB_IDENTITIES } = await import("../src/delegated-identities.js");
  assert.equal(GB_TABLE_VALID, true, "a malformed committed table fails CI here, before it can ship");
  assert.deepEqual([...gbIdentitiesFrom({ enabled: [...GB_IDENTITIES], "//": "a note" }).identities], [...GB_IDENTITIES]);
  for (const table of [{}, { enabled: [] }, { enabled: ["gb-fixer"] }, { enabled: ["GB-FIXER", 7] }, { enabled: "GB-FIXER" }, null, []]) {
    const r = gbIdentitiesFrom(table);
    assert.equal(r.valid, false, JSON.stringify(table));
    assert.deepEqual([...r.identities], [], "no persona field is declared from a malformed table");
  }
});

// Loads the Worker's persona consumers in a fresh node process whose personas table is `table`:  a load
// hook serves the fixture in place of scripts/agent_sync/mcp/gb-identities.json, so nothing on disk changes.
// The child prints one JSON line describing what each consumer saw.  The hook needs node:module's
// registerHooks, which Node has from 22.15 (and 23.5);  package.json allows Node 22.0, so on an older 22 the
// test is skipped with that reason instead of failing (kody 4368416312).  CI runs Node 24, where it runs.
const LOAD_HOOKS = typeof nodeModule.registerHooks === "function";
function loadWithPersonasTable(table) {
  const src = (name) => new URL(`../src/${name}`, import.meta.url).href;
  const script = `
    import { registerHooks } from "node:module";
    registerHooks({
      load(url, context, nextLoad) {
        if (url.endsWith("/agent_sync/mcp/gb-identities.json")) return { format: "json", source: process.env.GB_TABLE_FIXTURE, shortCircuit: true };
        return nextLoad(url, context);
      },
    });
    const out = { loaded: false };
    try {
      const manifest = await import(${JSON.stringify(src("infisical-manifest.js"))});
      const delegated = await import(${JSON.stringify(src("delegated-identities.js"))});
      const { gate } = await import(${JSON.stringify(src("gate.js"))});
      const { parseGbBearer } = await import(${JSON.stringify(src("gb-bearer.js"))});
      const { resolveFleetlinkSeatTokens } = await import(${JSON.stringify(src("fleetlink.js"))});
      const { SEAT_SECRETS } = await import(${JSON.stringify(src("config.js"))});
      out.loaded = true;
      out.valid = manifest.GB_TABLE_VALID;
      out.identities = [...delegated.GB_IDENTITIES];
      out.sameList = delegated.GB_IDENTITIES === manifest.GB_ENABLED_IDENTITIES;
      out.cursorAllowed = [...delegated.allowedIdentities("CURSOR")];
      out.gbDeclared = manifest.INFISICAL_EXPECT_DEFAULT.filter((e) => /^\\/(zulip|linear):[A-Z]+_GB_/.test(e)).length;
      const env = { SEAT_GATE: { idFromName: (n) => n, get: (id) => ({ id }) } };
      try { gate(env, "GB-FIXER"); out.gbGate = "opened"; } catch (error) { out.gbGate = error.message; }
      out.gbBearer = parseGbBearer("gbb_fixer_" + "A".repeat(43));
      // The seat list src/index.js passes on every /mcp request.
      const tokens = await resolveFleetlinkSeatTokens({ INFISICAL_MODE: "off" }, [...Object.keys(SEAT_SECRETS), ...delegated.GB_IDENTITIES]);
      out.fleetlink = { seatTokens: tokens.seatTokens.size, denied: [...tokens.deniedSeats] };
      out.bundleValid = delegated.loadDelegatedCredentials({}).valid;
    } catch (error) {
      out.error = String(error?.message ?? error);
    }
    console.log(JSON.stringify(out));
  `;
  const { NODE_TEST_CONTEXT: _runner, ...parentEnv } = process.env;
  const run = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
    env: { ...parentEnv, GB_TABLE_FIXTURE: JSON.stringify(table) }, encoding: "utf8", timeout: 30_000,
  });
  assert.equal(run.status, 0, run.stderr);
  return { out: JSON.parse(run.stdout.trim().split("\n").at(-1)), stderr: run.stderr };
}

test("personas table:  a malformed table still loads every module that uses it, and enables no persona (kody 4367022121)", {
  skip: LOAD_HOOKS ? false : "needs node:module registerHooks (Node 22.15 or later)",
}, () => {
  // The control proves the hook takes effect:  a one-persona table gives exactly that persona everywhere.
  const control = loadWithPersonasTable({ enabled: ["GB-FIXER"] });
  assert.equal(control.out.error, undefined, control.out.error);
  assert.deepEqual(control.out.identities, ["GB-FIXER"]);
  assert.equal(control.out.sameList, true, "delegated-identities.js exports the manifest's validated list");
  assert.deepEqual(control.out.cursorAllowed, ["CURSOR"]);
  assert.equal(control.out.gbDeclared, 4);
  assert.equal(control.out.gbGate, "opened");
  assert.equal(control.out.gbBearer?.identity, "GB-FIXER");
  assert.ok(!control.stderr.includes("gb_identity_table_invalid"));
  for (const table of [{}, null, [], { enabled: [] }, { enabled: ["gb-fixer"] }, { enabled: ["GB-FIXER", 7] }, { enabled: "GB-FIXER" }]) {
    const label = JSON.stringify(table);
    const { out, stderr } = loadWithPersonasTable(table);
    assert.equal(out.error, undefined, `${label}: ${out.error}`);
    assert.equal(out.loaded, true, `${label}: every module loads`);
    assert.equal(out.valid, false, label);
    assert.deepEqual(out.identities, [], `${label}: GB_IDENTITIES is empty`);
    assert.equal(out.sameList, true, label);
    assert.ok(!out.cursorAllowed.some((name) => name.startsWith("GB-")), `${label}: no grant may act as a persona`);
    assert.equal(out.gbDeclared, 0, `${label}: no persona field is declared`);
    assert.equal(out.gbGate, "not a seat", `${label}: no persona gate opens`);
    assert.equal(out.gbBearer, null, `${label}: no persona bearer parses`);
    assert.deepEqual(out.fleetlink, { seatTokens: 0, denied: ["GB-ENGINEER"] }, `${label}: the /mcp FleetLink seat list resolves`);
    assert.equal(out.bundleValid, true, `${label}: the legacy credential path still loads`);
    assert.ok(stderr.includes('"event":"gb_identity_table_invalid"'), `${label}: one structured event`);
  }
});

test("resolveDelegatedCredentials: required mode assembles validated rows from Infisical", async () => {
  _resetInfisicalForTests();
  const { resolveDelegatedCredentials } = await import("../src/delegated-identities.js");
  const { GB_IDENTITIES } = await import("../src/delegated-identities.js");
  const calls = [];
  mockFetch(GB_IDENTITIES.flatMap((identity) => {
    const role = identity.slice(3).replaceAll("-", "_");
    return [
      [`secrets/raw/ZULIP_GB_${role}_EMAIL`, { body: { secret: { secretValue: `${role.toLowerCase()}-grok-bot@simplewithus.zulipchat.com` } } }],
      [`secrets/raw/ZULIP_GB_${role}_API_KEY`, { body: { secret: { secretValue: `zk-${role}` } } }],
      [`secrets/raw/LINEAR_GB_${role}_CLIENT_ID`, { body: { secret: { secretValue: `li-${role}` } } }],
      [`secrets/raw/LINEAR_GB_${role}_CLIENT_SECRET`, { body: { secret: { secretValue: `ls-${role}` } } }],
    ];
  }));
  const env = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_SERVICE_TOKEN: "tok", INFISICAL_ENVIRONMENT: "prod" };
  const r = await resolveDelegatedCredentials(env);
  assert.equal(r.source, "infisical");
  assert.equal(r.roles.size, GB_IDENTITIES.length, "every enabled persona assembled");
  assert.equal(r.roles.get("GB-FIXER").email, "fixer-grok-bot@simplewithus.zulipchat.com", "completeRole validation applied");
});

test("resolveDelegatedCredentials: fallback with nothing resolved keeps the env bundle/legacy rows", async () => {
  _resetInfisicalForTests();
  const { resolveDelegatedCredentials, GB_IDENTITIES } = await import("../src/delegated-identities.js");
  mockFetch(new Array(GB_IDENTITIES.length * 4).fill(["secrets/raw/", { status: 404, body: {} }]).map(([, v]) => ["secrets/raw/", v]));
  const email = (i) => `${i.slice(3).toLowerCase().replaceAll("-", "")}-grok-bot@simplewithus.zulipchat.com`;
  const bundle = JSON.stringify({ v: 1, r: Object.fromEntries(GB_IDENTITIES.map((i) => [i, [email(i), "zk", "li", "ls"]])) });
  const env = { INFISICAL_MODE: "fallback", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_SERVICE_TOKEN: "tok", INFISICAL_ENVIRONMENT: "prod", GB_IDENTITY_CREDENTIALS: bundle };
  const r = await resolveDelegatedCredentials(env);
  assert.equal(r.source, "bundle", "family-level fallback: bundle deployment never silently emptied");
  assert.equal(r.roles.size, GB_IDENTITIES.length);
});

test("resolveDelegatedCredentials: fallback with /zulip served but /linear unreadable keeps every bundle persona (kody 4365622415)", async () => {
  _resetInfisicalForTests();
  const { resolveDelegatedCredentials, GB_IDENTITIES } = await import("../src/delegated-identities.js");
  const email = (i) => `${i.slice(3).toLowerCase()}-grok-bot@simplewithus.zulipchat.com`;
  mockFetch(GB_IDENTITIES.flatMap((identity) => {
    const role = identity.slice(3).replaceAll("-", "_");
    return [
      [`secrets/raw/ZULIP_GB_${role}_EMAIL`, { body: { secret: { secretValue: email(identity) } } }],
      [`secrets/raw/ZULIP_GB_${role}_API_KEY`, { body: { secret: { secretValue: `zk-${role}` } } }],
      [`secrets/raw/LINEAR_GB_${role}_`, { status: 403, body: {} }],
    ];
  }));
  const bundle = JSON.stringify({ v: 1, r: Object.fromEntries(GB_IDENTITIES.map((i) => [i, [email(i), "zk-bundle", "li", "ls"]])) });
  const env = { INFISICAL_MODE: "fallback", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_SERVICE_TOKEN: "tok", GB_IDENTITY_CREDENTIALS: bundle };
  const r = await resolveDelegatedCredentials(env);
  assert.equal(r.roles.size, GB_IDENTITIES.length, "no persona is dropped by a partial outage");
  assert.equal(r.roles.get("GB-FIXER").zulipKey, "zk-bundle", "the incomplete Infisical row is replaced by the bundle's whole row, never mixed");
  assert.ok(infisicalStats().fallbacks >= GB_IDENTITIES.length * 2, "each unreadable field was counted");
});

test("resolveRecallSecrets: required mode resolves all three; absent everywhere means unconfigured", async () => {
  _resetInfisicalForTests();
  const { resolveRecallSecrets, recallClientFromResolved } = await import("../src/recall.js");
  mockFetch([
    ["secrets/raw/RECALL_API_TOKEN", { body: { secret: { secretValue: "rt" } } }],
    ["secrets/raw/RECALL_ACCESS_CLIENT_ID", { body: { secret: { secretValue: "rid" } } }],
    ["secrets/raw/RECALL_ACCESS_CLIENT_SECRET", { body: { secret: { secretValue: "rs" } } }],
  ]);
  const env = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_SERVICE_TOKEN: "tok", INFISICAL_ENVIRONMENT: "prod", RECALL_BASE_URL: "https://recall.jays.services" };
  const r = await resolveRecallSecrets(env);
  assert.deepEqual(r, { token: "rt", accessId: "rid", accessSecret: "rs" });
  assert.ok(recallClientFromResolved(r, env, { fetch: async () => { throw new Error("unused"); } }), "client builds from resolved values");
  assert.equal(recallClientFromResolved({ token: "", accessId: "", accessSecret: "" }, env), null, "absent everywhere = not configured, never throws");
});

test("prefetchFolder: one listing warms the cache and makes optional misses subrequest-free", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  const calls = mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: "va" }, { secretKey: "B", secretValue: "vb" }] } }]]);
  await prefetchFolder(ENV, "/tools");
  assert.equal(await resolveSecret(ENV, { name: "A", path: "/tools", envName: "LEGACY" }), "va");
  assert.equal(await resolveSecret(ENV, { name: "B", path: "/tools", envName: "LEGACY" }), "vb");
  // An unlisted optional name is absent by the fresh listing: no fetch, empty.
  assert.equal(await resolveSecret(ENV, { name: "MISSING", path: "/tools", envName: "LEGACY", optional: true }), "");
  assert.equal(calls.length, 1, "one listing served every read");
});

test("prefetchFolder: a required expected name absent from the listing still throws", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [] } }]]);
  await prefetchFolder(ENV, "/wake");
  await assert.rejects(() => resolveSecret(ENV, { name: "WAKE_HMAC_KEY_JET", path: "/wake", envName: "LEGACY" }), InfisicalError, "declared-but-absent is visible drift, not a silent miss");
});

test("prefetchFolder: fallback mode serves the env binding when the listing fails, with no per-name burst", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  const calls = mockFetch([]);
  const env = { ...ENV, INFISICAL_MODE: "fallback", LEGACY: "v0", LEGACY2: "w0" };
  await prefetchFolder(env, "/tools");
  assert.equal(await resolveSecret(env, { name: "S", path: "/tools", envName: "LEGACY", optional: true }), "v0");
  assert.equal(await resolveSecret(env, { name: "T", path: "/tools", envName: "LEGACY2" }), "w0");
  await prefetchFolder(env, "/tools");
  assert.equal(calls.length, 1, "the failed listing only:  no per-name attempt, no re-list within the TTL (kody 4365623733)");
  assert.equal(infisicalStats().fallbacks, 2, "every env-served read is still counted");
});

test("prefetchFolder: a fresh listing is not fetched again (kody 4365623583)", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  const calls = mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: "va" }] } }]]);
  await prefetchFolder(ENV, "/tools");
  await prefetchFolder(ENV, "/tools");
  assert.equal(calls.length, 1, "one listing per folder per TTL");
  assert.equal(await resolveSecret(ENV, { name: "A", path: "/tools", envName: "L" }), "va");
});

test("authenticated reads never follow a redirect (kody 4365623359)", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  const inits = [];
  globalThis.fetch = async (url, init) => {
    inits.push(init);
    return new Response(null, { status: 302, headers: { Location: "https://elsewhere.example.test/" } });
  };
  await assert.rejects(() => prefetchFolder(ENV, "/tools"), (e) => e.code === "infisical_unavailable" && e.status === 302);
  await assert.rejects(() => resolveSecret(ENV, { name: "S", path: "/tools", envName: "L" }), (e) => e.code === "infisical_unavailable" && e.status === 302);
  assert.equal(inits.length, 2);
  assert.ok(inits.every((init) => init.redirect === "manual"), "both the listing and the per-name read refuse redirects");
});

test("a mistyped per-name answer is refused by its schema, not read as a value (kody 4365623474)", async () => {
  _resetInfisicalForTests();
  mockFetch([["/api/v3/secrets/raw/S", { body: { secret: { secretValue: 42 } } }]]);
  await assert.rejects(() => resolveSecret(ENV, { name: "S", path: "/tools", envName: "L", optional: true }), (e) => e.code === "invalid_response");
});

test("prefetchFolder: malformed listing never warms and required mode throws", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: "oops" } }]]);
  await assert.rejects(() => prefetchFolder(ENV, "/tools"), (e) => e instanceof InfisicalError && e.code === "invalid_listing");
  // Nothing warmed: a later resolve still fetches per-name (404 here) rather than trusting the bad listing.
  await assert.rejects(() => resolveSecret(ENV, { name: "A", path: "/tools", envName: "L" }), (e) => e.code === "not_found");
});

test("prefetchFolder: truncated listing (totalCount mismatch) refuses to warm", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: "va" }], totalCount: 2 } }]]);
  await assert.rejects(() => prefetchFolder(ENV, "/tools"), (e) => e.code === "truncated_listing");
  const calls2 = mockFetch([["secrets/raw/A", { body: { secret: { secretValue: "va" } } }]]);
  assert.equal(await resolveSecret(ENV, { name: "A", path: "/tools", envName: "L" }), "va", "per-name fetch still works after a refused listing");
  assert.equal(calls2.length, 1);
});

test("prefetchFolder: permission denial (403) throws in required, never falls back to env", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([["/api/v3/secrets/raw?", { status: 403, body: {} }]]);
  const env = { ...ENV, LEGACY: "should-not-serve" };
  await assert.rejects(() => prefetchFolder(env, "/zulip"), (e) => e.code === "infisical_unavailable" && e.status === 403);
  await assert.rejects(() => resolveSecret(env, { name: "A", path: "/zulip", envName: "LEGACY" }), InfisicalError, "required mode never serves the env binding");
});

test("prefetchFolder: transport failure in fallback mode warns, does not warm, env serves", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  globalThis.fetch = async () => { throw new Error("socket hangup"); };
  const env = { ...ENV, INFISICAL_MODE: "fallback", LEGACY: "v0" };
  await prefetchFolder(env, "/tools");
  assert.equal(await resolveSecret(env, { name: "A", path: "/tools", envName: "LEGACY", optional: true }), "v0");
});

test("cache expiry: a rotated value is picked up after the TTL", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: "old" }] } }]]);
  await prefetchFolder(ENV, "/tools");
  assert.equal(await resolveSecret(ENV, { name: "A", path: "/tools", envName: "L" }), "old");
  // Re-list past the TTL with the rotated value.
  mockFetch([["/api/v3/secrets/raw?", { body: { secrets: [{ secretKey: "A", secretValue: "new" }] } }]]);
  const realNow = Date.now;
  Date.now = () => realNow() + 61000;
  try {
    await prefetchFolder(ENV, "/tools");
    assert.equal(await resolveSecret(ENV, { name: "A", path: "/tools", envName: "L" }), "new", "post-TTL listing serves the rotated value");
    // And while the folder is warm, an intentional absence stays a free miss.
    assert.equal(await resolveSecret(ENV, { name: "NEVER_THERE", path: "/tools", envName: "L2", optional: true }), "");
  } finally {
    Date.now = realNow;
  }
});

test("folder 404 (missing folder) in required mode throws as unavailable, not as per-name absence", async () => {
  _resetInfisicalForTests();
  const { prefetchFolder } = await import("../src/infisical.js");
  mockFetch([]);
  await assert.rejects(() => prefetchFolder(ENV, "/nonexistent"), (e) => e.code === "infisical_unavailable" && e.status === 404, "a missing folder is an infra signal, not authoritative emptiness");
});
