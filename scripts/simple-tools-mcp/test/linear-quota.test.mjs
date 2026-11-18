// The Linear request meter:  one charge per GraphQL fetch, before the fetch, against the Linear user or app
// user that Linear's own whoami named.  Plain `node --test`, no dependencies.

import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import { gate as openSeatGate, providerQuotaGate, linearQuotaFromEnv, LINEAR_REQUESTS_PER_HOUR } from "../src/gate.js";
import { LinearClient, LinearError, mapLinearError, Q_WHOAMI, Q_TEAMS, LINEAR_IDENTITY_REPROVE_MS } from "../src/linear.js";
import { FakeLinear, GRAPHQL_URL } from "./fake-linear.mjs";
import { memoryQuota } from "./quota-helper.mjs";

let serial = 0; // the identity cache is per isolate and keyed by seat and client id, so each client gets its own id
const NOW = Date.parse("2026-10-09T10:00:00Z");

function seatGate(clock) {
  const store = S.memoryStore();
  return {
    idemBegin: (o) => S.idemBegin(store, o, clock.now),
    idemSet: (o) => S.idemSet(store, o, clock.now),
    idemDrop: (o) => S.idemDrop(store, o, clock.now),
    linearTokenGet: () => S.linearTokenGet(store),
    linearTokenSet: (e) => S.linearTokenSet(store, e),
    linearMintNote: (e) => S.linearMintNote?.(store, e),
    linearIdentityGet: () => S.linearIdentityGet(store),
    linearIdentitySet: (e) => S.linearIdentitySet(store, e),
    linearIdentityClear: () => S.linearIdentityClear(store),
    store,
  };
}

function build({ seat = "JET", viewerId = "app-user-1", quota, fake = null, clock = { now: NOW }, gate = null } = {}) {
  const linear = fake ?? new FakeLinear({ viewer: { id: viewerId, name: "Jet", displayName: "Jet", app: true } });
  const q = quota ?? memoryQuota({ now: () => clock.now });
  const client = new LinearClient({ seat, clientId: `client-${seat}-${++serial}`, clientSecret: "s3cret", gate: gate ?? seatGate(clock), quota: q, fetch: linear.fetch, now: () => clock.now });
  return { linear, quota: q, client, clock };
}
const graphqlRequests = (fake) => fake.requests.filter((r) => r.url === GRAPHQL_URL);

test("quota: a client cannot be built without a quota", () => {
  assert.throws(() => new LinearClient({ seat: "JET", clientId: "c", clientSecret: "s", gate: {}, fetch() {} }), /requires a quota/);
});

test("quota: a denied reserve sends nothing to Linear", async () => {
  const deny = { reserve: async () => ({ ok: false, code: "budget_exhausted", resetAt: "2026-10-09T11:00:00Z" }), debit: async () => ({ ok: true }), calls: [] };
  const { client, linear } = build({ quota: deny });
  await assert.rejects(client.call(Q_TEAMS, { first: 1 }), (e) => e.kind === "budget_exhausted" && e.resetAt === "2026-10-09T11:00:00Z");
  assert.equal(graphqlRequests(linear).length, 0);
  const mapped = mapLinearError(new (await import("../src/linear.js")).LinearError("budget_exhausted", { resetAt: "2026-10-09T11:00:00Z" }));
  assert.equal(mapped.code, "budget_exhausted");
  assert.equal(mapped.retryable, true);
  assert.equal(mapped.resetAt, "2026-10-09T11:00:00Z");
});

test("quota: a throwing quota fails closed before the fetch", async () => {
  const { client, linear } = build({ quota: { reserve: async () => { throw new Error("do down"); }, debit: async () => ({ ok: true }) } });
  await assert.rejects(client.call(Q_TEAMS, { first: 1 }), (e) => e.kind === "quota");
  assert.equal(graphqlRequests(linear).length, 0);
});

test("quota: the first request is a whoami on the unknown bucket, then every request is charged to the proven identity", async () => {
  const { client, quota, linear } = build();
  await client.call(Q_TEAMS, { first: 1 });
  await client.call(Q_TEAMS, { first: 1 });
  assert.equal(quota.calls[0], "unknown");
  assert.equal(quota.calls.length, 3, "one whoami plus two queries");
  assert.match(quota.calls[1], /^[0-9a-f]{64}$/);
  assert.equal(quota.calls[1], quota.calls[2]);
  assert.equal(graphqlRequests(linear).length, 3);
  assert.deepEqual([...quota.stores.keys()].sort(), ["unknown", quota.calls[1]].sort());
});

test("quota: every charge happens before its fetch", async () => {
  const order = [];
  const fake = new FakeLinear({ viewer: { id: "app-user-1", name: "Jet", displayName: "Jet", app: true } });
  const quota = memoryQuota();
  const reserve = quota.reserve;
  quota.reserve = async (o) => { order.push("reserve"); return reserve(o); };
  const inner = fake.fetch;
  fake.fetch = async (url, init) => { if (url === GRAPHQL_URL) order.push("fetch"); return inner(url, init); };
  const { client } = build({ fake, quota });
  await client.call(Q_TEAMS, { first: 1 });
  assert.deepEqual(order, ["reserve", "fetch", "reserve", "fetch"]);
});

test("quota: two seats on one app user share one bucket, distinct app users do not", async () => {
  const stores = new Map();
  const one = (seat, viewerId) => build({ seat, viewerId, quota: memoryQuota({ perHour: 3, stores }) });
  const a = one("JET", "shared-app-user");
  const b = one("JET-CHAT", "shared-app-user");
  const c = one("CODEX", "other-app-user");
  const results = [];
  for (const { client } of [a, b, a, b]) results.push(await client.call(Q_TEAMS, { first: 1 }).then(() => "ok", (e) => e.kind));
  // each cold client's whoami is also debited to the shared user, so three slots serve only the first client's whoami and query
  // and the second client's whoami, which is admitted by the unknown bucket and recorded over the line
  assert.deepEqual(results, ["ok", "budget_exhausted", "budget_exhausted", "budget_exhausted"]);
  await c.client.call(Q_TEAMS, { first: 1 });
  assert.equal(stores.size, 3, "unknown, shared and other");
});

test("quota: concurrent cold calls resolve identity once and never overspend", async () => {
  const stores = new Map();
  const { client, linear } = build({ quota: memoryQuota({ perHour: 6, stores }) });
  const outcomes = await Promise.all(Array.from({ length: 20 }, () => client.call(Q_TEAMS, { first: 1 }).then(() => "ok", (e) => e.kind)));
  assert.equal(outcomes.filter((o) => o === "ok").length, 5, "six slots, one spent by the whoami that proved the user");
  assert.equal(outcomes.filter((o) => o === "budget_exhausted").length, 15);
  assert.equal(graphqlRequests(linear).filter((r) => String(r.body?.query).includes("BoardWhoami")).length, 1);
  assert.equal(graphqlRequests(linear).length, 6, "never more actual requests than the limit");
});

test("quota: an unresolved identity refuses, with no per-seat fallback and no query sent", async () => {
  const fake = new FakeLinear({ viewer: { id: "", name: "Jet", displayName: "Jet", app: true } });
  const { client, quota } = build({ fake });
  await assert.rejects(client.call(Q_TEAMS, { first: 1 }), (e) => e.kind === "quota");
  assert.equal(graphqlRequests(fake).length, 1, "only the whoami went out");
  assert.deepEqual([...new Set(quota.calls)], ["unknown"]);
});

test("quota: a 401 retry is charged as its own request", async () => {
  const clock = { now: NOW };
  const { client, quota, linear } = build({ clock });
  await client.call(Q_TEAMS, { first: 1 });
  const row = await client.gate.linearTokenGet();
  await client.gate.linearTokenSet({ ...row, minted_at: clock.now / 1000 - 25 * 3600, expires_at: clock.now / 1000 + 30 * 86_400 });
  const before = quota.calls.length;
  linear.unauthorized = 1;
  await client.call(Q_TEAMS, { first: 1 });
  assert.equal(quota.calls.length - before, 3, "the 401 attempt, the whoami that proves the user again, and the retry");
});

test("quota: a 429 starts a shared cooldown that later reserves honor", async () => {
  const clock = { now: NOW };
  const { client, linear, quota } = build({ clock });
  await client.call(Q_TEAMS, { first: 1 });
  linear.failNext(() => true, () => linear.json(429, {}, { "retry-after": "30" }));
  await assert.rejects(client.call(Q_TEAMS, { first: 1 }), (e) => e.kind === "rate_limited");
  const before = graphqlRequests(linear).length;
  await assert.rejects(client.call(Q_TEAMS, { first: 1 }), (e) => e.kind === "rate_limited" && e.retryAfterS >= 1);
  assert.equal(graphqlRequests(linear).length, before, "the cooldown blocks the fetch");
  clock.now += 31_000;
  await client.call(Q_TEAMS, { first: 1 });
  assert.ok(quota.calls.length >= 4);
});

test("seat-state: provider quota is an atomic, minute-bucketed hourly window", async () => {
  const store = S.memoryStore();
  const t0 = NOW;
  for (let i = 0; i < 5; i++) assert.equal((await S.providerQuotaReserve(store, { limit: 5 }, t0)).ok, true);
  const denied = await S.providerQuotaReserve(store, { limit: 5 }, t0 + 1000);
  assert.equal(denied.ok, false);
  assert.equal(denied.code, "budget_exhausted");
  assert.ok(Date.parse(denied.resetAt) > t0 && Date.parse(denied.resetAt) <= t0 + 3_660_000);
  assert.equal((await S.providerQuotaReserve(store, { limit: 5 }, t0 + 3_661_000)).ok, true, "the window frees after an hour");
  assert.ok((await store.get("quota_buckets")).length <= 61);
  await assert.rejects(S.providerQuotaReserve(store, { limit: 0 }, t0));
  await assert.rejects(S.providerQuotaReserve(store, { limit: 5, n: 0 }, t0));
  await assert.rejects(S.providerQuotaReserve(store, { limit: 5_000_000 }, t0));
  const parallel = await Promise.all(Array.from({ length: 12 }, () => S.providerQuotaReserve(S.memoryStore(), { limit: 1 }, t0)));
  assert.equal(parallel.every((r) => r.ok), true, "separate stores never share a count");
});

test("gate: quota objects open only through the quota factory, with validated names", () => {
  const names = [];
  const env = { SEAT_GATE: { idFromName: (n) => { names.push(n); return n; }, get: (id) => ({ id }) } };
  const hex = "a".repeat(64);
  assert.equal(providerQuotaGate(env, "linear", hex).id, `providerquota:linear:${hex}`);
  assert.equal(providerQuotaGate(env, "linear", "unknown").id, "providerquota:linear:unknown");
  for (const bad of ["JET", "../x", hex.toUpperCase(), "a".repeat(63), "unknown2", "", null]) assert.throws(() => providerQuotaGate(env, "linear", bad), /not a provider quota/);
  assert.throws(() => providerQuotaGate(env, "zulip", hex), /not a provider quota/);
  assert.throws(() => openSeatGate(env, `providerquota:linear:${hex}`), /not a seat/);
  assert.throws(() => openSeatGate(env, "providerquota:linear:unknown"), /not a seat/);
  assert.equal(names.length, 2);
});

test("gate: the Linear quota defaults to 4000 an hour, 300 unknown, and ignores bad overrides", async () => {
  const calls = [];
  const env = { SEAT_GATE: { idFromName: (n) => n, get: (id) => ({ providerQuotaReserve: async (o) => { calls.push([id, o]); return { ok: true }; } }) } };
  const quota = linearQuotaFromEnv(env);
  await quota.reserve({ identity: null });
  await quota.reserve({ identity: "b".repeat(64) });
  assert.deepEqual(calls.map(([id, o]) => [id, o.limit, o.n]), [["providerquota:linear:unknown", 300, 1], [`providerquota:linear:${"b".repeat(64)}`, LINEAR_REQUESTS_PER_HOUR, 1]]);
  calls.length = 0;
  const custom = linearQuotaFromEnv({ ...env, LINEAR_REQUESTS_PER_HOUR: "4500", LINEAR_UNKNOWN_REQUESTS_PER_HOUR: "nope" });
  await custom.reserve({ identity: "c".repeat(64) });
  await custom.reserve({ identity: null });
  assert.deepEqual(calls.map(([, o]) => o.limit), [4500, 300]);
  const bogus = linearQuotaFromEnv({ ...env, LINEAR_REQUESTS_PER_HOUR: "99999999" });
  calls.length = 0;
  await bogus.reserve({ identity: "d".repeat(64) });
  assert.equal(calls[0][1].limit, 4000);
});

const usedOf = async (store) => (await store.get("quota_buckets") ?? []).reduce((n, [, c]) => n + c, 0);

test("quota: every actual request lands in the proven identity's bucket, bootstrap included", async () => {
  const { client, quota, linear } = build();
  await client.call(Q_TEAMS, { first: 1 });
  await client.call(Q_TEAMS, { first: 1 });
  const id = quota.calls[1];
  assert.equal(graphqlRequests(linear).length, 3);
  assert.equal(await usedOf(quota.stores.get(id)), 3, "the whoami was debited to the identity it proved");
  assert.equal(await usedOf(quota.stores.get("unknown")), 1, "and also holds the bootstrap guard");
});

test("quota: a full identity bucket plus a cold client overshoots by the whoami only, and then refuses", async () => {
  const stores = new Map();
  const warm = build({ viewerId: "busy-user", quota: memoryQuota({ perHour: 4, stores }) });
  const cold = build({ viewerId: "busy-user", quota: memoryQuota({ perHour: 4, stores }) });
  const first = await warm.client.call(Q_TEAMS, { first: 1 }).then(() => "ok", (e) => e.kind); // whoami debit + query = 2
  assert.equal(first, "ok");
  await warm.client.call(Q_TEAMS, { first: 1 });
  const id = warm.quota.calls[1];
  assert.equal(await usedOf(stores.get(id)), 3);
  await warm.client.call(Q_TEAMS, { first: 1 });
  assert.equal(await usedOf(stores.get(id)), 4);
  const after = await cold.client.call(Q_TEAMS, { first: 1 }).then(() => "ok", (e) => e.kind);
  assert.equal(after, "budget_exhausted", "the cold client's whoami was admitted by the unknown bucket, its query was not");
  assert.equal(await usedOf(stores.get(id)), 5, "overshoot is exactly the one whoami");
  assert.equal(graphqlRequests(cold.linear).length, 1);
});

test("quota: a 401 forgets the identity so the next request proves it again", async () => {
  const clock = { now: NOW };
  const { client, quota, linear } = build({ clock });
  await client.call(Q_TEAMS, { first: 1 });
  const row = await client.gate.linearTokenGet();
  await client.gate.linearTokenSet({ ...row, minted_at: clock.now / 1000 - 25 * 3600, expires_at: clock.now / 1000 + 30 * 86_400 });
  linear.unauthorized = 1;
  const before = graphqlRequests(linear).length;
  await client.call(Q_TEAMS, { first: 1 });
  const sent = graphqlRequests(linear).slice(before);
  assert.deepEqual(sent.map((r) => (String(r.body?.query).includes("BoardWhoami") ? "whoami" : "teams")), ["teams", "whoami", "teams"], "401, then a whoami proves the user again, then the retry");
  assert.equal(quota.calls.length, 2 + 3, "every one of the three sends was charged");
});

test("quota: a failed debit fails closed", async () => {
  const base = memoryQuota();
  const { client } = build({ quota: { ...base, debit: async () => { throw new Error("do down"); } } });
  await assert.rejects(client.call(Q_WHOAMI, {}), (e) => e instanceof LinearError && e.kind === "quota");
});

test("seat-state: a forced debit records past the limit and the next reserve refuses", async () => {
  const store = S.memoryStore();
  assert.equal((await S.providerQuotaReserve(store, { limit: 1 }, NOW)).ok, true);
  assert.equal((await S.providerQuotaReserve(store, { limit: 1, force: true }, NOW)).ok, true);
  assert.equal(await usedOf(store), 2);
  assert.equal((await S.providerQuotaReserve(store, { limit: 1 }, NOW + 1000)).ok, false);
  await S.providerQuotaNoteRateLimited(store, { retryAfterS: 30 }, NOW);
  assert.equal((await S.providerQuotaReserve(store, { limit: 100, force: true }, NOW + 1000)).ok, true, "a debit is recorded even in a cooldown");
});

test("audit: every production construction of a Linear client passes a quota", async () => {
  const { readdirSync, readFileSync } = await import("node:fs");
  const dir = new URL("../src/", import.meta.url);
  const found = [];
  for (const name of readdirSync(dir).filter((f) => f.endsWith(".js"))) {
    if (name === "linear.js") continue;
    const text = readFileSync(new URL(name, dir), "utf8");
    for (const match of text.matchAll(/(?:new LinearClient|linearClientFromPair|linearClientFromEnv)\(/g)) {
      // the call's argument list, up to the line that closes it
      const rest = text.slice(match.index);
      const call = rest.slice(0, rest.search(/\n\s*\}\),?\n|\);\n|\),\n/) + 3);
      found.push({ name, call });
    }
  }
  assert.ok(found.some((f) => f.name === "index.js") && found.some((f) => f.name === "linear-wake.js"), "the audit sees the Worker and the wake map");
  for (const { name, call } of found) assert.match(call, /quota/, `${name}: ${call.slice(0, 80)}`);
});

test("quota: the proof is stored with the token, so a new client or isolate of the seat needs no proving request", async () => {
  const clock = { now: NOW };
  const gate = seatGate(clock);
  const stores = new Map();
  const fake = new FakeLinear({ viewer: { id: "app-user-1", name: "Jet", displayName: "Jet", app: true } });
  const first = build({ fake, gate, clock, quota: memoryQuota({ now: () => clock.now, stores }) });
  await first.client.call(Q_TEAMS, { first: 1 });
  const unknownUsed = await usedOf(stores.get("unknown"));
  const second = build({ fake, gate, clock, quota: memoryQuota({ now: () => clock.now, stores }) });
  await second.client.call(Q_TEAMS, { first: 1 });
  await second.client.call(Q_WHOAMI, {});
  assert.equal(second.quota.calls.every((c) => c !== "unknown"), true, "a second client charged only the proven user");
  assert.equal(await usedOf(stores.get("unknown")), unknownUsed, "the unknown bucket was not touched again");
  const row = await gate.linearIdentityGet();
  assert.match(row.fp, /^[0-9a-f]{32}$/);
  assert.ok(!JSON.stringify(row).includes("lin_") && !JSON.stringify(row).includes((await gate.linearTokenGet()).access_token), "the proof holds a fingerprint and a hash, never the token");
});

test("quota: many whoami checks with one bound token are charged to the proven user only", async () => {
  const stores = new Map();
  const { client, quota } = build({ quota: memoryQuota({ stores }) });
  for (let i = 0; i < 6; i++) await client.call(Q_WHOAMI, {});
  assert.equal(await usedOf(stores.get("unknown")), 1, "only the proving request");
  assert.equal(quota.calls.filter((c) => c === "unknown").length, 1);
  assert.equal(await usedOf(stores.get(quota.calls[1])), 6, "all six requests are on the proven user");
});

test("quota: a different token never reuses the identity proven for another", async () => {
  const clock = { now: NOW };
  const gate = seatGate(clock);
  const { client, quota } = build({ gate, clock });
  await client.call(Q_TEAMS, { first: 1 });
  const row = await gate.linearTokenGet();
  await gate.linearTokenSet({ ...row, access_token: `${row.access_token}-rotated` });
  const before = quota.calls.length;
  await client.call(Q_TEAMS, { first: 1 }).catch(() => {});
  assert.equal(quota.calls[before], "unknown", "the rotated token proves itself first");
});

test("quota: a proof lasts at most a day and never past the token's expiry", async () => {
  const clock = { now: NOW };
  const gate = seatGate(clock);
  const { client, quota } = build({ gate, clock });
  await client.call(Q_TEAMS, { first: 1 });
  const proof = await gate.linearIdentityGet();
  const token = await gate.linearTokenGet();
  assert.ok(proof.valid_until <= NOW + LINEAR_IDENTITY_REPROVE_MS);
  assert.ok(proof.valid_until <= token.expires_at * 1000);
  clock.now += LINEAR_IDENTITY_REPROVE_MS + 1000;
  const before = quota.calls.length;
  await client.call(Q_TEAMS, { first: 1 });
  assert.equal(quota.calls[before], "unknown", "an expired proof is proven again");
  // a token that expires sooner shortens the proof
  const short = seatGate(clock);
  const b = build({ gate: short, clock, fake: new FakeLinear({ viewer: { id: "u", name: "Jet", displayName: "Jet", app: true }, expiresIn: 4 * 3600 }) });
  await b.client.call(Q_TEAMS, { first: 1 });
  const p2 = await short.linearIdentityGet();
  assert.ok(p2.valid_until <= clock.now + 4 * 3600 * 1000 + 1000);
});

test("quota: a bound token that answers as another user is refused, not debited, and its proof is dropped", async () => {
  const stores = new Map();
  const fake = new FakeLinear({ viewer: { id: "user-a", name: "Jet", displayName: "Jet", app: true } });
  const gate = seatGate({ now: NOW });
  const { client, quota } = build({ fake, gate, quota: memoryQuota({ stores }) });
  await client.call(Q_TEAMS, { first: 1 });
  const oldId = quota.calls[1];
  const usedBefore = new Map([...stores].map(([k, v]) => [k, v]));
  fake.viewer = { ...fake.viewer, id: "user-b" };
  await assert.rejects(client.call(Q_WHOAMI, {}), (e) => e.kind === "identity_changed");
  assert.deepEqual([...stores.keys()].sort(), [...usedBefore.keys()].sort(), "no bucket was created for the new user");
  assert.equal(await gate.linearIdentityGet(), null, "the proof is gone");
  assert.equal(mapLinearError(new LinearError("identity_changed")).code, "not_authorized");
  assert.ok(oldId);
});

test("seat-state: the identity proof validates and clears", async () => {
  const store = S.memoryStore();
  const fp = "a".repeat(32);
  const identity = "b".repeat(64);
  assert.equal(await S.linearIdentityGet(store), null);
  await S.linearIdentitySet(store, { fp, identity, proven_at: 1, valid_until: 2 });
  assert.deepEqual(await S.linearIdentityGet(store), { fp, identity, proven_at: 1, valid_until: 2 });
  await assert.rejects(S.linearIdentitySet(store, { fp: "short", identity, proven_at: 1, valid_until: 2 }));
  await assert.rejects(S.linearIdentitySet(store, { fp, identity: "nothex", proven_at: 1, valid_until: 2 }));
  await S.linearIdentityClear(store);
  assert.equal(await S.linearIdentityGet(store), null);
});


test("quota: optional factories return null for a missing or incomplete meter", async () => {
  const { linearClientFromPair, linearClientFromEnv } = await import("../src/linear.js");
  for (const quota of [undefined, null, {}, { reserve: async () => ({ ok: true }) }]) {
    const options = { gate: {}, fetch() {}, quota };
    assert.equal(linearClientFromPair({ clientId: "id", clientSecret: "secret" }, "JET", options), null);
    assert.equal(linearClientFromEnv({ LINEAR_JET_CLIENT_ID: "id", LINEAR_JET_CLIENT_SECRET: "secret" }, "JET", options), null);
  }
});

test("quota: malformed provider identity refuses before choosing or debiting an actor", async () => {
  for (const payload of [null, {}, { viewer: { id: 42 }, organization: { urlKey: "simple-with-us" } }, { viewer: { id: "user" }, organization: { urlKey: {} } }, { viewer: { id: "u".repeat(257) }, organization: { urlKey: "simple-with-us" } }]) {
    const { client, linear, quota } = build();
    linear.failNext(entry => entry.url === GRAPHQL_URL, () => linear.json(200, { data: payload }));
    await assert.rejects(client.call(Q_TEAMS, { first: 1 }), e => e.kind === "quota");
    assert.equal(graphqlRequests(linear).length, 1);
    assert.deepEqual([...quota.stores.keys()], ["unknown"]);
    assert.equal(await client.gate.linearIdentityGet(), null);
  }
});
