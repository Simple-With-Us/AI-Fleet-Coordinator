// /internal/gr-assert-verify (src/gr-assert-verify.js):  the signed route
// git-relay calls to verify a Worker-minted per-call assertion - the relay
// holds no mint-capable key, so the HMAC, the live gate check and the one-use
// jti claim all happen here.

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

async function call(token, { store, audits = [], hostedSeats = ["CODEX"], eventsOnlySeats = [], signKey = SIGN_KEY, key = KEY, method = "POST", body, grantExists = async () => true } = {}) {
  const gateStore0 = store ?? await gateStore();
  const { bytes, signature } = body ?? await bodyFor(token);
  const req = new Request("https://worker.example/internal/gr-assert-verify", {
    method,
    headers: { "Content-Type": "application/json", [WAKE_SIGNATURE_HEADER]: signature, "Content-Length": String(bytes.byteLength) },
    ...(method === "GET" ? {} : { body: bytes }),
  });
  const gates = fakeGates(gateStore0, { audits });
  return serveGrAssertVerify(req, {}, gates, {
    resolveKey: async () => key,
    resolveSignKey: async () => signKey,
    hostedSeats,
    eventsOnlySeats,
    grantExists,
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

test("an events-only seat and an unlisted seat refuse", async () => {
  const token = await mint();
  assert.equal((await (await call(token, { eventsOnlySeats: ["CODEX"] })).json()).reason, "events_only");
  assert.equal((await (await call(token, { hostedSeats: ["ECHO"] })).json()).reason, "bad_seat");
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
