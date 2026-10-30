// GR OAuth assertions (src/gr-assert.js):  the Worker-minted, short-lived,
// per-call HMAC token a hosted OAuth seat presents to git-relay in place of a
// bearer.  Pins the token shape, the fail-closed mint rules, the Worker's own
// verification, and the cross-language vector
// git_relay/tests/test_assertion.py checks against the same inputs.

import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { ASSERTION_RE, GR_ASSERT_MAX_TTL_S, GR_ASSERT_TTL_S, mintGrAssertion, parseGrAssertion, verifyGrAssertionToken, sha256HexText, resolveGrAssertionKey, grAssertionMinter, GR_ASSERT_TOKEN_BINDING } from "../src/gr-assert.js";
import { _resetInfisicalForTests } from "../src/infisical.js";

globalThis.crypto ??= webcrypto;

const KEY = "k".repeat(48);
const SEAT = "CODEX";
const BODY = JSON.stringify({ repo: "example/x" });
const CLAIMS = { seat: SEAT, action: "open_pr", scopes: ["zulip:read", "zulip:write"], epoch: 7, approvedAt: 1799999000000, phase: 2, grantRef: "0123456789ab" };

// Pinned cross-language vector:  key "test-assert-key-0123456789abcdef0123",
// seat CODEX, action open_pr, body sha256 of '{"repo":"example/x"}',
// iat 1800000000, exp 1800000090, jti "AbCdEfGhIjKlMnOp".
// git_relay/tests/test_assertion.py parses exactly this token and recomputes
// its signature with the same key.
const VECTOR_KEY = "test-assert-key-0123456789abcdef0123";
const VECTOR_BODY_SHA = "3552dcd2b3e97a13af54e33d560bafd9f79836dfecb0bd03766627077fe1f259";
const VECTOR_TOKEN =
  "gra2_eyJ2IjoiZ3ItYXNzZXJ0LzIiLCJhdWQiOiJnaXQtcmVsYXkiLCJzZWF0IjoiQ09ERVgiLCJpYXQiOjE4MDAwMDAwMDAsImV4cCI6MTgwMDAwMDA5MCwianRpIjoiQWJDZEVmR2hJaktsTW5PcCIsImFjdGlvbiI6Im9wZW5fcHIiLCJib2R5IjoiMzU1MmRjZDJiM2U5N2ExM2FmNTRlMzNkNTYwYmFmZDlmNzk4MzZkZmVjYjBiZDAzNzY2NjI3MDc3ZmUxZjI1OSIsInNjb3BlcyI6WyJ6dWxpcDpyZWFkIiwienVsaXA6d3JpdGUiXSwiZXBvY2giOjcsImFwcHJvdmVkX2F0IjoxNzk5OTk5MDAwMDAwLCJwaGFzZSI6MiwiZ3JhbnRfcmVmIjoiMDEyMzQ1Njc4OWFiIn0" +
  "_f2f6326fe364c4ee341414eb7435cbd7b902ca34b62c8753ba985e20b71dd500";

async function mint(over = {}) {
  const bodySha256 = await sha256HexText(BODY);
  return mintGrAssertion({ key: KEY, ...CLAIMS, bodySha256, ...over });
}

test("the pinned vector is what the relay parses", async () => {
  const token = await mint({ key: VECTOR_KEY, bodySha256: VECTOR_BODY_SHA, now: 1_800_000_000_000, ttlS: 90, jti: "AbCdEfGhIjKlMnOp" });
  assert.equal(token, VECTOR_TOKEN);
  assert.equal(await sha256HexText(BODY), VECTOR_BODY_SHA);
});

test("a minted assertion fits the shape and carries every claim", async () => {
  const bodySha256 = await sha256HexText(BODY);
  const token = await mint({ bodySha256 });
  assert.ok(ASSERTION_RE.test(token), "token matches gra2_ shape");
  const parsed = parseGrAssertion(token);
  assert.ok(parsed);
  assert.equal(parsed.claims.v, "gr-assert/2");
  assert.equal(parsed.claims.aud, "git-relay");
  assert.equal(parsed.claims.seat, SEAT);
  assert.equal(parsed.claims.exp - parsed.claims.iat, GR_ASSERT_TTL_S);
  assert.equal(parsed.claims.action, "open_pr");
  assert.equal(parsed.claims.body, bodySha256);
  assert.deepEqual(parsed.claims.scopes, ["zulip:read", "zulip:write"]);
  assert.equal(parsed.claims.epoch, 7);
  assert.equal(parsed.claims.phase, 2);
  assert.match(parsed.claims.jti, /^[A-Za-z0-9_-]{16}$/);
});

test("two mints never share a jti", async () => {
  const bodySha256 = await sha256HexText(BODY);
  const a = await mint({ bodySha256 });
  const b = await mint({ bodySha256 });
  assert.notEqual(a, b);
  assert.notEqual(parseGrAssertion(a).claims.jti, parseGrAssertion(b).claims.jti);
});

test("minting fails closed on every out-of-contract input", async () => {
  const bodySha256 = await sha256HexText(BODY);
  assert.equal(await mint({ key: "short", bodySha256 }), null);
  assert.equal(await mint({ key: "", bodySha256 }), null);
  assert.equal(await mint({ key: null, bodySha256 }), null);
  assert.equal(await mint({ seat: "NOBODY", bodySha256 }), null);
  assert.equal(await mint({ seat: "codex", bodySha256 }), null); // seats are upper-case
  assert.equal(await mint({ bodySha256, ttlS: 10 }), null);
  assert.equal(await mint({ bodySha256, ttlS: GR_ASSERT_MAX_TTL_S + 1 }), null);
  assert.equal(await mint({ bodySha256, ttlS: 90.5 }), null);
  assert.equal(await mint({ bodySha256, jti: "too-short" }), null);
  assert.equal(await mint({ bodySha256, action: "Open_Pr" }), null);
  assert.equal(await mint({ bodySha256: "not-hex" }), null);
  assert.equal(await mint({ bodySha256, scopes: ["zulip:write", "admin"] }), null);
  assert.equal(await mint({ bodySha256, scopes: "zulip:write" }), null);
  assert.equal(await mint({ bodySha256, epoch: 1.5 }), null);
  assert.equal(await mint({ bodySha256, epoch: "7" }), null);
  assert.equal(await mint({ bodySha256, phase: 1.5 }), null);
  assert.equal(await mint({ bodySha256, approvedAt: Number.NaN }), null);
  assert.equal(await mint({ bodySha256, grantRef: "xyz" }), null);
});

test("the assertion shape never matches the bearer families or an OAuth grant", () => {
  assert.equal(ASSERTION_RE.test("gbb_fixer_" + "a".repeat(43)), false);
  assert.equal(ASSERTION_RE.test("sbt_codex_" + "a".repeat(43)), false);
  assert.equal(ASSERTION_RE.test("gra1_codex_1800000000_1800000090_AbCdEfGhIjKlMnOp_" + "0".repeat(64)), false);
  assert.equal(ASSERTION_RE.test("gra2_not-base64!_" + "0".repeat(64)), false);
});

test("parse refuses a non-canonical or tampered claims payload", async () => {
  const parsed = parseGrAssertion(VECTOR_TOKEN);
  assert.ok(parsed);
  // Re-ordered keys are not the minted form and never parse.
  const reordered = { grant_ref: "0123456789ab", phase: 2, approved_at: 1799999000000, epoch: 7, scopes: ["zulip:read", "zulip:write"], body: VECTOR_BODY_SHA, action: "open_pr", jti: "AbCdEfGhIjKlMnOp", exp: 1800000090, iat: 1800000000, seat: "CODEX", aud: "git-relay", v: "gr-assert/2" };
  const payload = Buffer.from(JSON.stringify(reordered)).toString("base64url");
  assert.equal(parseGrAssertion(`gra2_${payload}_${"0".repeat(64)}`), null);
  for (const junk of ["", "gra2_", "not-a-token", null, 42]) assert.equal(parseGrAssertion(junk), null);
});

test("verify accepts a live, request-bound assertion", async () => {
  const bodySha256 = await sha256HexText(BODY);
  const token = await mint({ bodySha256 });
  const verdict = await verifyGrAssertionToken({ key: KEY, token, action: "open_pr", bodySha256 });
  assert.equal(verdict.ok, true);
  assert.equal(verdict.claims.seat, SEAT);
});

test("verify refuses a wrong key, action, body, expiry and future iat", async () => {
  const bodySha256 = await sha256HexText(BODY);
  const token = await mint({ bodySha256 });
  assert.equal((await verifyGrAssertionToken({ key: "x".repeat(48), token, action: "open_pr", bodySha256 })).reason, "bad_signature");
  assert.equal((await verifyGrAssertionToken({ key: KEY, token, action: "merge_pr", bodySha256 })).reason, "action_mismatch");
  assert.equal((await verifyGrAssertionToken({ key: KEY, token, action: "open_pr", bodySha256: "0".repeat(64) })).reason, "body_mismatch");
  const now = 1_800_000_000_000;
  const old = await mint({ bodySha256, now, ttlS: 90 });
  assert.equal((await verifyGrAssertionToken({ key: KEY, token: old, action: "open_pr", bodySha256, now: now + 91_000 })).reason, "expired");
  const ahead = await mint({ bodySha256, now: now + 120_000, ttlS: 90 });
  assert.equal((await verifyGrAssertionToken({ key: KEY, token: ahead, action: "open_pr", bodySha256, now })).reason, "future");
  assert.equal((await verifyGrAssertionToken({ key: "short", token, action: "open_pr", bodySha256 })).reason, "not_configured");
  assert.equal((await verifyGrAssertionToken({ key: KEY, token: "gra2_bad", action: "open_pr", bodySha256 })).reason, "bad_token");
});

// Isolated custody (fresh-head review):  the sign key resolves ONLY through
// the narrowly scoped GR_ASSERT_INFISICAL_SERVICE_TOKEN binding at the
// dedicated /gr-assert-signing folder - never the generic read-tools token,
// never a Worker binding, never another folder, never an alias binding.
// (/gr-front is NOT signer-isolated:  the broad st-mcp-runtime-read token
// reads it, per the host's live token inventory.)
test("the sign key resolves only through the isolated /gr-assert-signing binding", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, authorization: init?.headers?.Authorization ?? "" });
    if (url.includes("/api/v3/secrets/raw/GR_ASSERTION_SIGN_KEY") && url.includes("secretPath=%2Fgr-assert-signing")) {
      return new Response(JSON.stringify({ secret: { secretValue: "k".repeat(48) } }), { status: 200 });
    }
    return new Response("not found", { status: 404 });
  };
  const base = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_SERVICE_TOKEN: "st-broad" };
  try {
    // No isolated binding:  required mode throws (fail visibly), never a
    // broad-token read.
    _resetInfisicalForTests();
    await assert.rejects(resolveGrAssertionKey({ ...base }), /GR_ASSERT_INFISICAL_SERVICE_TOKEN is not bound/);
    assert.equal(calls.length, 0);
    // With the isolated binding:  exactly one read, of the exact name at the
    // exact folder, bearing the ISOLATED token - the broad token never leaves.
    _resetInfisicalForTests();
    const key = await resolveGrAssertionKey({ ...base, [GR_ASSERT_TOKEN_BINDING]: "st-gr-assert-narrow" });
    assert.equal(key, "k".repeat(48));
    assert.equal(calls.length, 1);
    assert.ok(calls[0].url.includes("secretPath=%2Fgr-assert-signing"));
    assert.equal(calls[0].authorization, "Bearer st-gr-assert-narrow");
    // A Worker binding of the same name is never read.
    _resetInfisicalForTests();
    const before = calls.length;
    await assert.rejects(resolveGrAssertionKey({ ...base, GR_ASSERTION_SIGN_KEY: "z".repeat(48) }), /not bound/);
    assert.equal(calls.length, before);
    // Infisical missing the key:  "" (capability off), mint fails closed.
    _resetInfisicalForTests();
    globalThis.fetch = async () => new Response("not found", { status: 404 });
    assert.equal(await resolveGrAssertionKey({ ...base, [GR_ASSERT_TOKEN_BINDING]: "st-gr-assert-narrow" }), "");
    // Off mode:  no binding read, no fetch, "".
    _resetInfisicalForTests();
    assert.equal(await resolveGrAssertionKey({ INFISICAL_MODE: "off", [GR_ASSERT_TOKEN_BINDING]: "st-gr-assert-narrow", GR_ASSERTION_SIGN_KEY: "z".repeat(48) }), "");
    // No alias:  the retired GR_FRONT_INFISICAL_SERVICE_TOKEN binding name is
    // NOT honored - resolution still fails closed and never fetches.
    _resetInfisicalForTests();
    globalThis.fetch = async (url, init) => { calls.push({ url, authorization: init?.headers?.Authorization ?? "" }); return new Response("not found", { status: 404 }); };
    const aliasBefore = calls.length;
    await assert.rejects(resolveGrAssertionKey({ ...base, GR_FRONT_INFISICAL_SERVICE_TOKEN: "st-gr-front-old" }), /GR_ASSERT_INFISICAL_SERVICE_TOKEN is not bound/);
    assert.equal(calls.length, aliasBefore);
    // No fallback path:  with the isolated binding present, resolution never
    // touches the /gr-front folder at all.
    _resetInfisicalForTests();
    calls.length = 0;
    globalThis.fetch = async (url, init) => {
      calls.push({ url, authorization: init?.headers?.Authorization ?? "" });
      if (url.includes("/api/v3/secrets/raw/GR_ASSERTION_SIGN_KEY") && url.includes("secretPath=%2Fgr-assert-signing")) {
        return new Response(JSON.stringify({ secret: { secretValue: "k".repeat(48) } }), { status: 200 });
      }
      return new Response("not found", { status: 404 });
    };
    assert.equal(await resolveGrAssertionKey({ ...base, [GR_ASSERT_TOKEN_BINDING]: "st-gr-assert-narrow" }), "k".repeat(48));
    assert.ok(calls.length >= 1 && calls.every((c) => !c.url.includes("secretPath=%2Fgr-front")));
  } finally {
    _resetInfisicalForTests();
  }
});

// Lazy signer resolution (host review finding 4428031266):  building the
// wiring must perform NO Infisical read, so a failing isolated token can
// never break tools/list or the non-Git routes;  only the Git call that
// mints fails, and it fails closed (null -> not_configured), never a throw.
test("the minter resolves the signer lazily and fails only the Git call closed", async () => {
  const calls = [];
  const denied = async (url, init) => { calls.push({ url, authorization: init?.headers?.Authorization ?? "" }); return new Response("denied", { status: 403 }); };
  globalThis.fetch = denied;
  const env = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_SERVICE_TOKEN: "st-broad", [GR_ASSERT_TOKEN_BINDING]: "st-gr-assert-narrow" };
  const props = { seat: "CODEX", scopes: ["zulip:read", "zulip:write"], epoch: 7, approvedAt: 1799999000000, phase: 2, grantRef: "0123456789ab" };
  try {
    _resetInfisicalForTests();
    // Wiring construction (what serveMcp does for every request, including
    // tools/list and non-Git calls) performs no fetch at all.
    const minter = grAssertionMinter(env, props);
    assert.equal(calls.length, 0);
    // The Git call itself fails closed on a denied isolated read:  null, no
    // throw, and the broad token is never sent.
    assert.equal(await minter({ action: "open_pr", body: "{}" }), null);
    assert.ok(calls.length >= 1);
    assert.ok(calls.every((c) => c.authorization !== "Bearer st-broad"));
    // A missing binding (required mode throws inside the resolver) is also
    // converted to null by the minter.
    _resetInfisicalForTests();
    calls.length = 0;
    assert.equal(await grAssertionMinter({ INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "ws-test" }, props)({ action: "open_pr", body: "{}" }), null);
    // A working isolated binding mints a gra2_ assertion.
    _resetInfisicalForTests();
    globalThis.fetch = async (url, init) => {
      calls.push({ url, authorization: init?.headers?.Authorization ?? "" });
      if (url.includes("/api/v3/secrets/raw/GR_ASSERTION_SIGN_KEY") && url.includes("secretPath=%2Fgr-assert-signing")) {
        return new Response(JSON.stringify({ secret: { secretValue: "k".repeat(48) } }), { status: 200 });
      }
      return new Response("not found", { status: 404 });
    };
    const token = await grAssertionMinter(env, props)({ action: "open_pr", body: BODY });
    assert.ok(token?.startsWith("gra2_"));
    assert.equal(parseGrAssertion(token).claims.seat, "CODEX");
  } finally {
    _resetInfisicalForTests();
  }
});

test("every known seat mints;  JET included, relay policy decides admission", async () => {
  const bodySha256 = await sha256HexText(BODY);
  for (const seat of ["JET", "JET-CHAT", "GROK-WEB", "ECHO", "WISE", "CURSOR", "CLAUDE", "MA", "CODEX"]) {
    const token = await mint({ seat, bodySha256 });
    assert.ok(token?.startsWith("gra2_"), seat);
    assert.equal(parseGrAssertion(token).claims.seat, seat);
  }
});
