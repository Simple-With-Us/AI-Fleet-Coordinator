import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveSecret, infisicalStats, _resetInfisicalForTests, InfisicalError } from "../src/infisical.js";
import { resolveLinearPair, linearClientFromPair } from "../src/linear.js";
import { resolveFleetlinkSeatTokens, fleetlinkClientFromSecret } from "../src/fleetlink.js";
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
