// /internal/gr-assert-verify (src/gr-assert-verify.js):  the signed route
// git-relay calls to verify a Worker-minted per-call assertion - the relay
// holds no mint-capable key, so the HMAC, the live gate check and the one-use
// jti claim all happen here.

import { cachedGrantExists } from "../src/gr-assert-verify.js";
import { IdentityCache } from "../src/identity-cache.js";
import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { serveGrAssertVerify, verifyGrAssertRequest, grantSummaryIsLive, GR_ASSERT_VERIFY_CONTRACT } from "../src/gr-assert-verify.js";
import { mintGrAssertion, sha256HexText } from "../src/gr-assert.js";
import { wakeSignature, WAKE_SIGNATURE_HEADER } from "../src/wake.js";
import * as S from "../src/seat-state.js";

globalThis.crypto ??= webcrypto;

const KEY = "test-key-" + "k".repeat(40);
const SIGN_KEY = "s".repeat(48);
const BODY = JSON.stringify({ repo: "example/x" });
const CLAIMS = { seat: "CODEX", action: "open_pr", scopes: ["zulip:read", "zulip:write"], epoch: 7, approvedAt: Date.now(), phase: 2, grantRef: "0123456789ab" };

function memStore() {
  const m = new Map();
  return {
    get: async (k) => (m.has(k) ? m.get(k) : undefined),
    put: async (k, v) => void m.set(k, v),
    delete: async (k) => void m.delete(k),
    transaction: (cb) => cb({ get: async (k) => (m.has(k) ? m.get(k) : undefined), put: async (k, v) => void m.set(k, v) }),
  };
}

async function mint(over = {}) {
  return mintGrAssertion({ key: SIGN_KEY, ...CLAIMS, bodySha256: await sha256HexText(BODY), ...over });
}

async function signedBody(fields) {
  const bytes = new TextEncoder().encode(JSON.stringify(fields));
  return { bytes, signature: await wakeSignature(KEY, bytes) };
}

async function bodyFor(token, over = {}) {
  return signedBody({
    contract: GR_ASSERT_VERIFY_CONTRACT,
    token,
    action: "open_pr",
    body_sha256: await sha256HexText(BODY),
    sent_at: Math.floor(Date.now() / 1000),
    ...over,
  });
}

// The real claimGrAssert runs the real gate check, so the store carries the
// seat's epoch:  CLAIMS.epoch is 7.
async function gateStore() {
  const store = memStore();
  await store.put("epoch", 7);
  return store;
}

function fakeGates(store, { audits = [] } = {}) {
  return (env, seat) => ({
    claimGrAssert: (options) => S.claimGrAssert(store, options),
    audit: async (row) => void audits.push({ seat, ...row }),
  });
}

async function call(token, { store, audits = [], diagnostics = [], recordRefusal, resolveKey, resolveSignKey, hostedSeats = ["CODEX"], eventsOnlySeats = [], signKey = SIGN_KEY, key = KEY, method = "POST", body, grantExists = async () => true } = {}) {
  const gateStore0 = store ?? await gateStore();
  const { bytes, signature } = body ?? await bodyFor(token);
  const req = new Request("https://worker.example/internal/gr-assert-verify", {
    method,
    headers: { "Content-Type": "application/json", [WAKE_SIGNATURE_HEADER]: signature, "Content-Length": String(bytes.byteLength) },
    ...(method === "GET" ? {} : { body: bytes }),
  });
  const gates = fakeGates(gateStore0, { audits });
  return serveGrAssertVerify(req, {}, gates, {
    resolveKey: resolveKey ?? (async () => key),
    resolveSignKey: resolveSignKey ?? (async () => signKey),
    hostedSeats,
    eventsOnlySeats,
    grantExists,
    recordRefusal: recordRefusal ?? (async (row) => void diagnostics.push(row)),
  });
}

test("a live, unspent, request-bound assertion verifies and names its seat", async () => {
  const resp = await call(await mint());
  assert.equal(resp.status, 200);
  const answer = await resp.json();
  assert.deepEqual(answer, { ok: true, seat: "CODEX" });
});

test("an assertion is one-use:  the replay refuses and writes the audit row", async () => {
  const store = await gateStore();
  const audits = [];
  const token = await mint();
  const first = await call(token, { store, audits });
  assert.equal((await first.json()).ok, true);
  const second = await call(token, { store, audits });
  const answer = await second.json();
  assert.equal(answer.ok, false);
  assert.equal(answer.reason, "replayed");
  assert.deepEqual(audits, [{ seat: "CODEX", event: "gr_assert_refused", reason: "replayed", via: "gr-assert-verify" }]);
  // And a third presentation still refuses - the claim is durable.
  const third = await call(token, { store, audits });
  assert.equal((await third.json()).reason, "replayed");
});

test("a mint for call A cannot be presented for call B (action and body bind)", async () => {
  const token = await mint();
  const wrongAction = await call(token, { body: await bodyFor(token, { action: "merge_pr" }) });
  assert.equal((await wrongAction.json()).reason, "action_mismatch");
  const wrongBody = await call(token, { body: await bodyFor(token, { body_sha256: "0".repeat(64) }) });
  assert.equal((await wrongBody.json()).reason, "body_mismatch");
});

test("the route signature gates everything:  bad sig, stale, future, malformed", async () => {
  const token = await mint();
  const good = await bodyFor(token);
  const badSig = await call(token, { body: { bytes: good.bytes, signature: "b".repeat(64) } });
  assert.equal(badSig.status, 401);
  assert.equal((await badSig.json()).reason, "bad_signature");
  const stale = await call(token, { body: await bodyFor(token, { sent_at: Math.floor(Date.now() / 1000) - 400 }) });
  assert.equal(stale.status, 401);
  assert.equal((await stale.json()).reason, "stale");
  const future = await call(token, { body: await bodyFor(token, { sent_at: Math.floor(Date.now() / 1000) + 400 }) });
  assert.equal((await future.json()).reason, "future");
  const badContract = await call(token, { body: await bodyFor(token, { contract: "other/9" }) });
  assert.equal(badContract.status, 400);
  assert.equal((await badContract.json()).reason, "bad_contract");
  const get = await call(token, { method: "GET" });
  assert.equal(get.status, 405);
});

test("route auth fails closed without the channel key", async () => {
  const resp = await call(await mint(), { key: "" });
  assert.equal(resp.status, 404);
  assert.equal((await resp.json()).reason, "not_configured");
});

test("a missing mint key, a wrong mint key and a non-assertion token refuse", async () => {
  const token = await mint();
  assert.equal((await (await call(token, { signKey: "" })).json()).reason, "not_configured");
  assert.equal((await (await call(token, { signKey: "z".repeat(48) })).json()).reason, "bad_signature");
  assert.equal((await (await call("sbt_codex_" + "a".repeat(43))).json()).reason, "bad_token");
});

test("an expired assertion refuses;  its jti was never claimable", async () => {
  const token = await mint({ now: Date.now() - 300_000, ttlS: 90 });
  assert.equal((await (await call(token)).json()).reason, "expired");
});

test("revocation equivalence:  an epoch bump refuses as grant_invalid", async () => {
  const token = await mint();
  const store = await gateStore();
  await store.put("epoch", 8); // the admin revoke path bumps the epoch first
  assert.equal((await (await call(token, { store })).json()).reason, "grant_invalid");
});

test("a paused seat refuses as paused without spending the jti", async () => {
  const token = await mint();
  const store = await gateStore();
  await store.put("paused", true);
  assert.equal((await (await call(token, { store })).json()).reason, "paused");
  // Unpaused, the same assertion verifies once:  the refusal spent nothing.
  await store.put("paused", false);
  assert.equal((await (await call(token, { store })).json()).ok, true);
});

test("a deleted provider grant refuses even when the gate row would pass", async () => {
  const token = await mint();
  assert.equal((await (await call(token, { grantExists: async () => false })).json()).reason, "grant_invalid");
  // The check receives the seat and the signed grant_ref.
  const seen = [];
  await call(await mint(), { grantExists: async (seat, grantRef) => { seen.push([seat, grantRef]); return true; } });
  assert.deepEqual(seen, [["CODEX", "0123456789ab"]]);
});

test("a transient grant-list miss does not burn the one-use jti (kody 4428074149)", async () => {
  const token = await mint();
  const store = await gateStore();
  let live = false;
  const grantExists = async () => live;
  // The miss refuses as grant_invalid and spends NOTHING:  when the grant
  // list answers again, the same assertion verifies - it is not a replay.
  assert.equal((await (await call(token, { store, grantExists })).json()).reason, "grant_invalid");
  live = true;
  assert.equal((await (await call(token, { store, grantExists })).json()).ok, true);
  // The spend still happened exactly once:  a third presentation is a replay.
  assert.equal((await (await call(token, { store, grantExists })).json()).reason, "replayed");
});

test("an events-only seat and an unlisted seat refuse", async () => {
  const token = await mint();
  assert.equal((await (await call(token, { eventsOnlySeats: ["CODEX"] })).json()).reason, "events_only");
  assert.equal((await (await call(token, { hostedSeats: ["ECHO"] })).json()).reason, "bad_seat");
});

// AFC-844:  every refusal is one diagnostics row, so a bearer_rejected the relay reports
// has a readable cause.  The seat is named only past the assertion's own signature;
// before it, nothing in the token can be trusted.
test("every refusal records one gr_assert diagnostics row with its reason, status and trusted seat", async () => {
  const row = (reason, status, seat = null) => ({ kind: "gr_assert", reason, seat, status });
  const cases = [];
  const run = async (label, token, options, answer, expected) => {
    const diagnostics = [];
    const resp = await call(token, { ...options, diagnostics });
    const body = await resp.json();
    assert.equal(body.reason, answer, `${label}:  the relay-facing reason is unchanged`);
    assert.equal(body.ok, false, label);
    assert.deepEqual(diagnostics, [expected], label);
    cases.push(label);
  };
  const token = await mint();
  const good = await bodyFor(token);
  // Before the assertion's signature:  no seat is named.
  await run("route signature", token, { body: { bytes: good.bytes, signature: "b".repeat(64) } }, "bad_signature", row("bad_signature", 401));
  await run("stale route call", token, { body: await bodyFor(token, { sent_at: Math.floor(Date.now() / 1000) - 400 }) }, "stale", row("stale", 401));
  await run("bad contract", token, { body: await bodyFor(token, { contract: "other/9" }) }, "bad_contract", row("bad_contract", 400));
  await run("GET", token, { method: "GET" }, "method_not_allowed", row("method_not_allowed", 405));
  await run("oversized", token, { body: await bodyFor("x".repeat(5000)) }, "too_large", row("too_large", 413));
  await run("no channel key", token, { key: "" }, "not_configured", row("not_configured", 404));
  // The assertion's own checks share three codes with the route's:  their rows say
  // `assertion:`, so junk route signatures cannot hide a mint-key fault.
  await run("not an assertion", "sbt_codex_" + "a".repeat(43), {}, "bad_token", row("assertion:bad_token", 200));
  await run("unreadable assertion", "gra2_bad_" + "0".repeat(64), {}, "bad_token", row("assertion:bad_token", 200));
  await run("wrong mint key", token, { signKey: "z".repeat(48) }, "bad_signature", row("assertion:bad_signature", 200));
  await run("no mint key", token, { signKey: "" }, "not_configured", row("assertion:not_configured", 200));
  // Past it:  the Worker's own claims name the seat.
  await run("wrong action", token, { body: await bodyFor(token, { action: "merge_pr" }) }, "action_mismatch", row("action_mismatch", 200, "CODEX"));
  await run("wrong body", token, { body: await bodyFor(token, { body_sha256: "0".repeat(64) }) }, "body_mismatch", row("body_mismatch", 200, "CODEX"));
  await run("expired", await mint({ now: Date.now() - 300_000, ttlS: 90 }), {}, "expired", row("expired", 200, "CODEX"));
  // The assertion's own window, apart from the route's call window (`stale` above).
  await run("future", await mint({ now: Date.now() + 120_000 }), {}, "future", row("assertion:future", 200, "CODEX"));
  await run("unlisted seat", token, { hostedSeats: ["ECHO"] }, "bad_seat", row("bad_seat", 200, "CODEX"));
  await run("events only", token, { eventsOnlySeats: ["CODEX"] }, "events_only", row("events_only", 200, "CODEX"));
  // grant_invalid keeps its relay-facing reason and records which check refused.
  await run("no live grant", token, { grantExists: async () => false }, "grant_invalid", row("grant_invalid:no_live_grant", 200, "CODEX"));
  const bumped = await gateStore();
  await bumped.put("epoch", 8);
  await run("epoch bump", token, { store: bumped }, "grant_invalid", row("grant_invalid:stale_epoch", 200, "CODEX"));
  const paused = await gateStore();
  await paused.put("paused", true);
  await run("paused", token, { store: paused }, "paused", row("paused", 200, "CODEX"));
  assert.equal(cases.length, 19);
});

test("a key that cannot be read stays the route error it was, and leaves a row", async () => {
  const token = await mint();
  const outage = async () => { throw new Error("infisical down"); };
  const diagnostics = [];
  await assert.rejects(call(token, { diagnostics, resolveKey: outage }), /infisical down/);
  assert.deepEqual(diagnostics, [{ kind: "gr_assert", reason: "key_unavailable", seat: null, status: 500 }]);
  const signDiagnostics = [];
  await assert.rejects(call(token, { diagnostics: signDiagnostics, resolveSignKey: outage }), /infisical down/);
  assert.deepEqual(signDiagnostics, [{ kind: "gr_assert", reason: "sign_key_unavailable", seat: null, status: 500 }]);
});

test("a replay keeps its audit row and also records its diagnostics row", async () => {
  const store = await gateStore();
  const audits = [];
  const diagnostics = [];
  const token = await mint();
  assert.equal((await (await call(token, { store, audits, diagnostics })).json()).ok, true);
  assert.deepEqual(diagnostics, [], "a verified assertion records nothing");
  assert.equal((await (await call(token, { store, audits, diagnostics })).json()).reason, "replayed");
  assert.deepEqual(audits, [{ seat: "CODEX", event: "gr_assert_refused", reason: "replayed", via: "gr-assert-verify" }]);
  assert.deepEqual(diagnostics, [{ kind: "gr_assert", reason: "replayed", seat: "CODEX", status: 200 }]);
});

test("a failing diagnostics log never changes the answer", async () => {
  const recordRefusal = async () => { throw new Error("diagnostics storage down"); };
  const token = await mint();
  const refused = await call(token, { recordRefusal, grantExists: async () => false });
  assert.equal(refused.status, 200);
  assert.deepEqual(await refused.json(), { ok: false, reason: "grant_invalid" });
  const badSig = await call(token, { recordRefusal, body: { bytes: (await bodyFor(token)).bytes, signature: "b".repeat(64) } });
  assert.equal(badSig.status, 401);
  assert.deepEqual(await badSig.json(), { ok: false, reason: "bad_signature" });
  assert.deepEqual(await (await call(token, { recordRefusal })).json(), { ok: true, seat: "CODEX" });
});

// grantSummaryIsLive pins the provider's actual representation
// (@cloudflare/workers-oauth-provider 1.2.3):  listUserGrants returns raw KV
// rows unfiltered;  expiresAt is UNIX SECONDS, optional, absent = no expiry.
test("a grant summary is live only when unexpired (Unix-seconds expiresAt)", () => {
  const nowS = Math.floor(Date.now() / 1000);
  assert.equal(grantSummaryIsLive({ id: "g1" }), true); // no TTL configured: never expires
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: nowS + 3600 }), true);
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: nowS - 1 }), false); // dead row awaiting KV sweep
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: nowS }), false); // boundary: expired AT now (library: <= invalid)
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: Number.NaN }), false);
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: "soon" }), false);
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: null }), false);
  assert.equal(grantSummaryIsLive({ expiresAt: nowS + 3600 }), false); // no id, nothing to match
  assert.equal(grantSummaryIsLive(null), false);
  // Units hazard, documented:  a millisecond value in this seconds field
  // compares as far-future (the library itself reads seconds, so the check
  // mirrors it exactly);  the provider only ever writes seconds.
  assert.equal(grantSummaryIsLive({ id: "g1", expiresAt: (nowS - 3600) * 1000 }), true);
});

test("the checker validates the signed payload shape with fixed reasons", async () => {
  const nowS = Math.floor(Date.now() / 1000);
  const mk = (fields) => signedBody(fields);
  const badAction = await mk({ contract: GR_ASSERT_VERIFY_CONTRACT, token: "gra2_x", action: "NO", body_sha256: "0".repeat(64), sent_at: nowS });
  assert.equal((await verifyGrAssertRequest({ bytes: badAction.bytes, signature: badAction.signature, key: KEY })).reason, "bad_action");
  const extra = await mk({ contract: GR_ASSERT_VERIFY_CONTRACT, token: "gra2_x", action: "open_pr", body_sha256: "0".repeat(64), sent_at: nowS, sneaky: 1 });
  assert.equal((await verifyGrAssertRequest({ bytes: extra.bytes, signature: extra.signature, key: KEY })).reason, "bad_json");
});

test("cachedGrantExists:  one listing per window, no negative caching, and an admin eviction reaches it", async () => {
  const cache = new IdentityCache();
  let listings = 0;
  let live = false;
  const check = () => cachedGrantExists(cache, { seat: "CODEX", grantRef: "abc", ttlMs: 300_000, lookup: async () => { listings += 1; return live; } });
  assert.equal(await check(), false);
  assert.equal(await check(), false);
  assert.equal(listings, 2, "a miss stays retryable");
  live = true;
  assert.equal(await check(), true);
  assert.equal(await check(), true);
  assert.equal(listings, 3);
  cache.evictSeat("CODEX");
  assert.equal(await check(), true);
  assert.equal(listings, 4);
  const off = new IdentityCache();
  let offListings = 0;
  const offCheck = () => cachedGrantExists(off, { seat: "CODEX", grantRef: "abc", ttlMs: 0, lookup: async () => { offListings += 1; return true; } });
  await offCheck();
  await offCheck();
  assert.equal(offListings, 2, "GATE_IDENTITY_CACHE_MS=0 turns it off");
});

test("cachedGrantExists:  an hour window that never outlives the grant's own expiry", async () => {
  let now = 10_000_000;
  const cache = new IdentityCache({ now: () => now });
  let listings = 0;
  let expiresAt;
  const check = () => cachedGrantExists(cache, { seat: "CODEX", grantRef: "g", ttlMs: 3_600_000, lookup: async () => { listings += 1; return { expiresAt }; } });
  // A grant ten minutes from expiry (seconds):  cached for ten minutes, not the hour.
  expiresAt = Math.floor(now / 1000) + 600;
  assert.equal(await check(), true);
  assert.equal(cache.get("grant:CODEX:g").expiresAt - now, 600_000);
  now += 600_000;
  assert.equal(await check(), true);
  assert.equal(listings, 2, "past the grant's expiry the entry is gone and the listing runs again");
  // A grant with no expiry gets the full hour.
  cache.evictSeat("CODEX");
  expiresAt = undefined;
  await check();
  assert.equal(cache.get("grant:CODEX:g").expiresAt - now, 3_600_000);
  // An already-expired grant answers true from the listing but is not stored.
  cache.evictSeat("CODEX");
  expiresAt = Math.floor(now / 1000) - 1;
  await check();
  assert.equal(cache.get("grant:CODEX:g"), null);
});
