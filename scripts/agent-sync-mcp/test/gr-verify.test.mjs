// /internal/gr-bearer-verify (src/gr-verify.js):  the signed hash-only route
// GR-MCP uses to resolve an admin-minted bearer to its identity.

import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { verifyGrRequest, serveGrBearerVerify, GR_VERIFY_CONTRACT, GR_VERIFY_KEY_VAR } from "../src/gr-verify.js";
import { wakeSignature, WAKE_SIGNATURE_HEADER } from "../src/wake.js";
import { newGbBearer, sha256Hex, GB_BEARER_TTL_MS } from "../src/gb-bearer.js";
import * as S from "../src/seat-state.js";

globalThis.crypto ??= webcrypto;

const KEY = "test-key-" + "k".repeat(40);
const ID = "GB-FIXER";

function memStore() {
  const m = new Map();
  const store = {
    get: async (k) => (m.has(k) ? m.get(k) : undefined),
    put: async (k, v) => void m.set(k, v),
    delete: async (k) => void m.delete(k),
    // The DO store serializes transactions;  the counter needs the same surface.
    transaction: (cb) => cb(store),
  };
  return store;
}

async function signedBody(fields) {
  const bytes = new TextEncoder().encode(JSON.stringify(fields));
  return { bytes, signature: await wakeSignature(KEY, bytes) };
}

function fakeGates(store, state = { paused: false }) {
  return (env, seat) => ({
    gbBearerCheck: (hash) => S.gbBearerCheck(store, hash),
    gbBearerStatus: () => S.gbBearerStatus(store),
    gbBearerMismatch: (identity, configured) => S.gbBearerMismatch(store, identity, configured),
    gbBearerMismatches: () => S.gbBearerMismatches(store),
    getState: async () => state,
    audit: async () => {},
  });
}

test("a correctly signed query passes the checker", async () => {
  const { bytes, signature } = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash: "a".repeat(64), sent_at: Math.floor(Date.now() / 1000) });
  const checked = await verifyGrRequest({ bytes, signature, key: KEY });
  assert.equal(checked.ok, true);
  assert.equal(checked.query.identity, ID);
});

test("bad signature, stale, future, and malformed bodies refuse with fixed codes", async () => {
  const nowS = Math.floor(Date.now() / 1000);
  const good = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash: "a".repeat(64), sent_at: nowS });
  assert.equal((await verifyGrRequest({ bytes: good.bytes, signature: "b".repeat(64), key: KEY })).reason, "bad_signature");
  assert.equal((await verifyGrRequest({ bytes: good.bytes, signature: good.signature, key: "other-key-" + "x".repeat(40) })).reason, "bad_signature");
  const stale = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash: "a".repeat(64), sent_at: nowS - 400 });
  assert.equal((await verifyGrRequest({ bytes: stale.bytes, signature: stale.signature, key: KEY })).reason, "stale");
  const future = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash: "a".repeat(64), sent_at: nowS + 400 });
  assert.equal((await verifyGrRequest({ bytes: future.bytes, signature: future.signature, key: KEY })).reason, "future");
  const badId = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: "NO-SUCH-SEAT", hash: "a".repeat(64), sent_at: nowS });
  assert.equal((await verifyGrRequest({ bytes: badId.bytes, signature: badId.signature, key: KEY })).reason, "bad_identity");
  const badHash = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash: "not-hex", sent_at: nowS });
  assert.equal((await verifyGrRequest({ bytes: badHash.bytes, signature: badHash.signature, key: KEY })).reason, "bad_hash");
  assert.equal((await verifyGrRequest({ bytes: new Uint8Array(0), signature: good.signature, key: KEY })).reason, "empty_body");
  assert.equal((await verifyGrRequest({ bytes: good.bytes, signature: good.signature, key: "short" })).reason, "not_configured");
});

test("the route resolves a live bearer, and refuses none/mismatch/expired/paused", async () => {
  const { token, hash } = await newGbBearer(ID);
  const store = memStore();
  await S.gbBearerMint(store, { hash, by: "fixture@example.test", expiresAt: Date.now() + GB_BEARER_TTL_MS });

  const call = async (h, gates = fakeGates(store), options = {}) => {
    const { bytes, signature } = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash: h, sent_at: Math.floor(Date.now() / 1000) });
    const req = new Request("https://x.test/internal/gr-bearer-verify", {
      method: "POST",
      headers: { "Content-Type": "application/json", [WAKE_SIGNATURE_HEADER]: signature },
      body: bytes,
    });
    const resp = await serveGrBearerVerify(req, {}, gates, { resolveKey: async () => KEY, ...options });
    return { status: resp.status, body: await resp.json() };
  };

  const live = await call(hash);
  assert.equal(live.status, 200);
  assert.deepEqual({ ok: live.body.ok, identity: live.body.identity }, { ok: true, identity: ID });
  assert.ok(live.body.expires_at > Date.now());

  assert.equal((await call("0".repeat(64))).body.reason, "mismatch");

  // The wrong guess counted against the claimed identity (counts only).
  let counted = await S.gbBearerMismatches(store);
  assert.equal(counted[ID]?.count, 1);
  assert.equal((await call("1".repeat(64))).body.reason, "mismatch");
  counted = await S.gbBearerMismatches(store);
  assert.equal(counted[ID]?.count, 2);
  assert.ok(!("hash" in counted[ID]) && !("token" in counted[ID]));

  const unknown = memStore();
  assert.equal((await call(hash, fakeGates(unknown))).body.reason, "none");
  // "none" is not a wrong secret:  it does not count.
  assert.deepEqual(await S.gbBearerMismatches(unknown), {});

  const expiredStore = memStore();
  await S.gbBearerMint(expiredStore, { hash, by: "t", expiresAt: Date.now() - 1000 });
  assert.equal((await call(hash, fakeGates(expiredStore))).body.reason, "expired");
  // An expired-but-matching bearer is not a guess either.
  assert.deepEqual(await S.gbBearerMismatches(expiredStore), {});

  assert.equal((await call(hash, fakeGates(store, { paused: true }))).body.reason, "paused");

  // A wake-only seat's bearer stays minted but verifies refused:  GR-MCP maps
  // any ok:false to 401, so the seat keeps no git tools while it is events-only.
  assert.equal((await call(hash, fakeGates(store), { eventsOnlySeats: [ID] })).body.reason, "events_only");
});

test("a failed refusal audit propagates so the relay fails closed", async () => {
  // Kody 4372746631:  the expired-refusal audit row is required.  When the
  // write fails the route must error (relay fails closed) instead of
  // answering with the audit silently missing.
  const { hash } = await newGbBearer(ID);
  const store = memStore();
  await S.gbBearerMint(store, { hash, by: "t", expiresAt: Date.now() - 1000 });
  const base = fakeGates(store);
  const gates = (env, seat) => ({ ...base(env, seat), audit: async () => { throw new Error("store down"); } });
  const { bytes, signature } = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash, sent_at: Math.floor(Date.now() / 1000) });
  const req = new Request("https://x.test/internal/gr-bearer-verify", {
    method: "POST",
    headers: { "Content-Type": "application/json", [WAKE_SIGNATURE_HEADER]: signature },
    body: bytes,
  });
  await assert.rejects(serveGrBearerVerify(req, {}, gates, { resolveKey: async () => KEY }), /store down/);
});

test("the route never echoes the query or the hash", async () => {
  const { hash } = await newGbBearer(ID);
  const { bytes, signature } = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash, sent_at: Math.floor(Date.now() / 1000) });
  const req = new Request("https://x.test/internal/gr-bearer-verify", {
    method: "POST",
    headers: { [WAKE_SIGNATURE_HEADER]: signature },
    body: bytes,
  });
  const resp = await serveGrBearerVerify(req, {}, fakeGates(memStore()), { resolveKey: async () => KEY });
  const text = await resp.text();
  assert.equal(text.includes(hash), false);
  assert.equal(text.includes(String(bytes)), false);
});

test("seat (sbt_) bearers verify through the same route and row", async () => {
  const { newSeatBearer } = await import("../src/gb-bearer.js");
  const { token, hash } = await newSeatBearer("JET");
  const store = memStore();
  // A seat mint WITHOUT the recorded owner opt-in still verifies for GR-MCP
  // (GR acceptance is GR's policy);  mcp_accept governs only /mcp itself.
  await S.gbBearerMint(store, { hash, by: "fixture@example.test", expiresAt: Date.now() + GB_BEARER_TTL_MS, mcpAccept: false });
  const { bytes, signature } = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: "JET", hash, sent_at: Math.floor(Date.now() / 1000) });
  const req = new Request("https://x.test/internal/gr-bearer-verify", {
    method: "POST",
    headers: { [WAKE_SIGNATURE_HEADER]: signature },
    body: bytes,
  });
  const resp = await serveGrBearerVerify(req, {}, fakeGates(store), { resolveKey: async () => KEY });
  const body = await resp.json();
  assert.deepEqual({ ok: body.ok, identity: body.identity }, { ok: true, identity: "JET" });
  const status = await S.gbBearerStatus(store);
  assert.equal(status.mcp_accept, false);
});

test("the recorded mcp_accept choice survives mint and reads back in status", async () => {
  const { newSeatBearer } = await import("../src/gb-bearer.js");
  const { hash } = await newSeatBearer("ECHO");
  const store = memStore();
  await S.gbBearerMint(store, { hash, by: "fixture@example.test", expiresAt: Date.now() + 1000, mcpAccept: true });
  const status = await S.gbBearerStatus(store);
  assert.equal(status.mcp_accept, true);
  // Rows minted before the flag read back as false, never as undefined-accepts.
  const legacy = memStore();
  await legacy.put("gb_bearer", { hash, created_at: 1, created_by: "t", expires_at: Date.now() + 1000, last_used_at: 0, mint_count: 1 });
  assert.equal((await S.gbBearerStatus(legacy)).mcp_accept, false);
});

test("the route fails closed when the verify key resolves empty (Infisical-only, no binding)", async () => {
  const { hash } = await newGbBearer(ID);
  const { bytes, signature } = await signedBody({ contract: GR_VERIFY_CONTRACT, identity: ID, hash, sent_at: Math.floor(Date.now() / 1000) });
  const req = new Request("https://x.test/internal/gr-bearer-verify", {
    method: "POST",
    headers: { [WAKE_SIGNATURE_HEADER]: signature },
    body: bytes,
  });
  // An env binding must NOT serve the key even when present:  GR_BEARER_VERIFY_KEY
  // lives only in Fleet-OPS /tools, so an empty resolveKey means not_configured.
  const resp = await serveGrBearerVerify(req, { [GR_VERIFY_KEY_VAR]: KEY }, fakeGates(memStore()), { resolveKey: async () => "" });
  assert.equal(resp.status, 404);
  assert.equal((await resp.json()).reason, "not_configured");
});
