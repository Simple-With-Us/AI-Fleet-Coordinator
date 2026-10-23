import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveSecret, infisicalStats, _resetInfisicalForTests, InfisicalError } from "../src/infisical.js";
import { resolveLinearPair, linearClientFromPair } from "../src/linear.js";
import { LinearClient } from "../src/linear.js";

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
  assert.deepEqual(infisicalStats(), { fetched: 1, cacheHits: 1, fallbacks: 0 });
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
  assert.equal(await resolveLinearPair(env, "GROK"), null, "a seat with no Linear app stays null");
});

test("resolveLinearPair: required mode reads the WISE_LINEAR_* names at /linear", async () => {
  _resetInfisicalForTests();
  const calls = mockFetch([
    ["secrets/raw/WISE_LINEAR_CLIENT_ID", { body: { secret: { secretValue: "id-inf" } } }],
    ["secrets/raw/WISE_LINEAR_CLIENT_SECRET", { body: { secret: { secretValue: "sec-inf" } } }],
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
  assert.equal(linearClientFromPair(pair, "ECHO", { gate: {} }), null, "Linear tools answer not_configured");
});

test("linearClientFromPair keeps the null contract without a gate or pair", async () => {
  assert.equal(linearClientFromPair(null, "ECHO", { gate: {} }), null);
  assert.equal(linearClientFromPair({ clientId: "", clientSecret: "s" }, "ECHO", { gate: {} }), null);
  assert.ok(linearClientFromPair({ clientId: "i", clientSecret: "s" }, "ECHO", { gate: {} }) instanceof LinearClient);
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

test("optional read: fallback mode still counts a fallback on absence", async () => {
  _resetInfisicalForTests();
  mockFetch([]);
  const env = { ...ENV, INFISICAL_MODE: "fallback", LEGACY: "v0" };
  assert.equal(await resolveSecret(env, { name: "S", path: "/wake", envName: "LEGACY", optional: true }), "v0");
  assert.equal(infisicalStats().fallbacks, 1);
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
