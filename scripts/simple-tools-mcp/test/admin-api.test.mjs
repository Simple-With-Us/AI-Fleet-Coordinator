import test from "node:test";
import assert from "node:assert/strict";
import { bearerProblem, bearerStatusRows } from "../src/admin-api.js";
import { BEARER_SEATS, seatCredentialRecordNames } from "../src/bearer-identities.js";

const NOW = Date.parse("2026-11-23T06:00:00Z");
const good = { active: true, expired: false, mcp_accept: true, mcp_accept_recorded: true, created_at: "2026-11-07T00:00:00Z", expires_at: "2027-02-05T00:00:00Z" };

test("bearerProblem names the first failure in a fixed order", () => {
  const base = { credentialsLoaded: true, status: good, paused: false, mismatches: null };
  assert.equal(bearerProblem(base, NOW), "ok");
  assert.equal(bearerProblem({ ...base, credentialsLoaded: false }, NOW), "credentials_missing");
  assert.equal(bearerProblem({ ...base, credentialsLoaded: false, credentialsUnavailable: true }, NOW), "credentials_unavailable");
  assert.equal(bearerProblem({ ...base, status: { ...good, active: false } }, NOW), "no_bearer");
  assert.equal(bearerProblem({ ...base, status: { ...good, active: false, expired: true } }, NOW), "bearer_expired");
  assert.equal(bearerProblem({ ...base, paused: true }, NOW), "paused");
  assert.equal(bearerProblem({ ...base, status: { ...good, mcp_accept: false } }, NOW), "mcp_not_accepted");
  assert.equal(bearerProblem({ ...base, status: { ...good, expires_at: "2026-11-30T00:00:00Z" } }, NOW), "expiring_soon");
  assert.equal(bearerProblem({ ...base, status: { ...good, expires_at: Date.parse("2026-11-30T00:00:00Z") } }, NOW), "expiring_soon");
  assert.equal(bearerProblem({ ...base, status: { ...good, expired: false, active: true, expires_at: Date.parse("2026-10-01T00:00:00Z") } }, NOW), "bearer_expired");
  assert.equal(bearerProblem({ ...base, mismatches: { count: 2 } }, NOW), "wrong_secret_attempts");
  assert.equal(bearerProblem({ ...base, statusUnavailable: true }, NOW), "status_unavailable");
});

test("bearerStatusRows covers every identity, isolates failures and carries no secret", async () => {
  const rows = await bearerStatusRows({
    now: NOW,
    gateFor: (id) => ({
      gbBearerStatus: async () => { if (id === "AG") throw new Error("boom"); return good; },
      gbBearerMismatches: async () => ({ [id]: { count: 0 } }),
      getState: async () => ({ paused: id === "MC" }),
    }),
    resolveCredentials: async (id) => (id === "ZC" ? null : { key: "k", email: "e" }),
  });
  assert.equal(rows.length, BEARER_SEATS.length);
  const by = Object.fromEntries(rows.map((r) => [r.identity, r]));
  assert.equal(by.AG.problem, "status_unavailable");
  assert.equal(by.MC.problem, "paused");
  assert.equal(by.ZC.problem, "credentials_missing");
  assert.deepEqual(by.ZC.missingRecords, seatCredentialRecordNames("ZC"));
  assert.equal(by.WISE.problem, "ok");
  assert.ok(!JSON.stringify(rows).includes('"key"'));
});

test("CODEX and MA record names follow hosting", () => {
  assert.deepEqual(seatCredentialRecordNames("MA", { hostedSeats: [] }).names, ["MA_ZULIP_KEY", "MA_ZULIP_EMAIL"]);
  assert.notDeepEqual(seatCredentialRecordNames("MA", { hostedSeats: ["MA"] }).names, ["MA_ZULIP_KEY", "MA_ZULIP_EMAIL"]);
});

// ---- owner action API
import { adminBearerAction, adminApiRequestRefusal } from "../src/admin-api.js";

const ISSUER = "https://agents.example.test";
const MINT_ID = "0123456789ab" + "cdef0123456789abcdef01".slice(0, 20);
function req(body, headers = {}, method = "POST") {
  return new Request(`${ISSUER}/admin/api/bearers/action`, {
    method,
    headers: { "content-type": "application/json", "x-admin-api": "1", ...headers },
    body: method === "POST" ? (typeof body === "string" ? body : JSON.stringify(body)) : undefined,
  });
}
function deps(over = {}) {
  const calls = { mints: [], revokes: 0, evicted: [], audits: [] };
  const status = over.status ?? { active: false, expired: false };
  return {
    calls,
    d: {
      issuer: ISSUER, email: "owner@example.test", now: NOW, ttlMs: 1000,
      identities: { bot: ["GB1"], seat: ["WISE", "ZC"] },
      gateFor: () => ({
        gbBearerStatus: async () => status,
        gbBearerMintChecked: async (a) => { calls.mints.push(a); return over.minted ?? { minted: true }; },
        gbBearerRevoke: async () => { calls.revokes++; },
      }),
      evict: (i) => calls.evicted.push(i),
      newBotBearer: async () => ({ token: "gbb_TEST" }),
      newSeatBearer: async () => ({ token: "sbt_TEST" }),
      sha256Hex: async (t) => `hash:${t}`,
      prepare: async () => {},
      botCredentialsLoaded: async () => true,
      seatCredentials: async (i) => (i === "ZC" ? null : { key: "k" }),
      missingRecords: () => ({ names: ["ZC_ZULIP_KEY"] }),
      ...(over.d ?? {}),
    },
  };
}
const j = async (r) => ({ status: r.status, cache: r.headers.get("cache-control"), body: await r.json() });

test("action API refuses anything but a same-origin-shaped JSON POST", () => {
  assert.equal(adminApiRequestRefusal(req({}, {}, "GET"), ISSUER), "method");
  assert.equal(adminApiRequestRefusal(req({}, { "content-type": "text/plain" }), ISSUER), "content_type");
  assert.equal(adminApiRequestRefusal(req({}, { "x-admin-api": "0" }), ISSUER), "missing_header");
  assert.equal(adminApiRequestRefusal(req({}, { "sec-fetch-site": "cross-site" }), ISSUER), "cross_site");
  assert.equal(adminApiRequestRefusal(req({}, { origin: "https://evil.example" }), ISSUER), "origin");
  assert.equal(adminApiRequestRefusal(req({}, { "sec-fetch-site": "same-origin", origin: ISSUER }), ISSUER), null);
  assert.equal(adminApiRequestRefusal(req({}), ISSUER), null);
});

test("mint returns the token once, no-store, and records the hash with the owner's mcp choice", async () => {
  const { d, calls } = deps();
  const r = await j(await adminBearerAction(req({ action: "mint", identity: "WISE", mint_id: MINT_ID, mcp_accept: false }), d));
  assert.equal(r.status, 200);
  assert.match(r.cache, /no-store/);
  assert.equal(r.body.token, "sbt_TEST");
  assert.equal(r.body.mcpAccept, false);
  assert.equal(calls.mints[0].hash, "hash:sbt_TEST");
  assert.equal(calls.mints[0].by, "owner@example.test");
  assert.equal(calls.mints[0].expiresAt, NOW + 1000);
  assert.deepEqual(calls.evicted, ["WISE"]);
});

test("mint of a live bearer and rotate without one are refused with a hint, nothing minted", async () => {
  const live = { active: true, expired: false, mcp_accept: true, expires_at: NOW + 1e9 };
  const a = deps({ status: live });
  const r1 = await j(await adminBearerAction(req({ action: "mint", identity: "WISE", mint_id: MINT_ID, mcp_accept: true }), a.d));
  assert.equal(r1.status, 409); assert.equal(r1.body.error, "bearer_active"); assert.equal(a.calls.mints.length, 0);
  const b = deps();
  const r2 = await j(await adminBearerAction(req({ action: "rotate", identity: "WISE", mint_id: MINT_ID }), b.d));
  assert.equal(r2.status, 409); assert.equal(r2.body.error, "no_active_bearer"); assert.equal(b.calls.mints.length, 0);
  const expired = deps({ status: { active: true, expired: false, expires_at: NOW - 1 } });
  const r3 = await j(await adminBearerAction(req({ action: "rotate", identity: "WISE", mint_id: MINT_ID }), expired.d));
  assert.equal(r3.body.error, "no_active_bearer");
});

test("rotate keeps the current mcp acceptance unless told otherwise; a seat mint must say it", async () => {
  const live = { active: true, expired: false, mcp_accept: false, expires_at: NOW + 1e9 };
  const a = deps({ status: live });
  const r = await j(await adminBearerAction(req({ action: "rotate", identity: "WISE", mint_id: MINT_ID }), a.d));
  assert.equal(r.body.mcpAccept, false);
  const b = deps();
  const r2 = await j(await adminBearerAction(req({ action: "mint", identity: "WISE", mint_id: MINT_ID }), b.d));
  assert.equal(r2.status, 400); assert.equal(r2.body.error, "mcp_accept_required");
});

test("missing credentials name the record and mint nothing; bad ids and identities are refused", async () => {
  const a = deps();
  const r = await j(await adminBearerAction(req({ action: "mint", identity: "ZC", mint_id: MINT_ID, mcp_accept: true }), a.d));
  assert.equal(r.status, 409); assert.equal(r.body.error, "credentials_missing"); assert.deepEqual(r.body.missingRecords, { names: ["ZC_ZULIP_KEY"] });
  assert.equal(a.calls.mints.length, 0);
  assert.equal((await j(await adminBearerAction(req({ action: "mint", identity: "NOPE", mint_id: MINT_ID }), a.d))).body.error, "unknown_identity");
  assert.equal((await j(await adminBearerAction(req({ action: "mint", identity: "WISE", mint_id: "short", mcp_accept: true }), a.d))).body.error, "bad_mint_id");
  assert.equal((await j(await adminBearerAction(req({ action: "nuke", identity: "WISE" }), a.d))).body.error, "unknown_action");
  assert.equal((await j(await adminBearerAction(req("not json"), a.d))).body.error, "bad_body");
});

test("a completed or refused mint form never shows a token; revoke revokes and evicts", async () => {
  const done = deps({ minted: { minted: false, reason: "completed", result: { mint_count: 3 } } });
  const r = await j(await adminBearerAction(req({ action: "mint", identity: "WISE", mint_id: MINT_ID, mcp_accept: true }), done.d));
  assert.equal(r.body.completed, true); assert.equal(r.body.tokenShown, false); assert.ok(!("token" in r.body));
  const stale = deps({ minted: { minted: false, reason: "stale" } });
  const r2 = await j(await adminBearerAction(req({ action: "mint", identity: "WISE", mint_id: MINT_ID, mcp_accept: true }), stale.d));
  assert.equal(r2.status, 409); assert.ok(!JSON.stringify(r2.body).includes("sbt_"));
  const rv = deps();
  const r3 = await j(await adminBearerAction(req({ action: "revoke", identity: "GB1" }), rv.d));
  assert.equal(r3.body.ok, true); assert.equal(rv.calls.revokes, 1); assert.deepEqual(rv.calls.evicted, ["GB1"]);
});

import { diagnosticSummaryRows } from "../src/admin-api.js";
test("diagnosticSummaryRows keeps names and counts only", () => {
  const rows = diagnosticSummaryRows([{ kind: "mcp", reason: "mcp_http_401", seat: null, count: 288, status: 401, ts: 5, secret: "x", message: "leak" }, null, "junk"]);
  assert.deepEqual(rows[0], { kind: "mcp", reason: "mcp_http_401", seat: null, count: 288, status: 401, ts: 5 });
  assert.ok(!JSON.stringify(rows).includes("leak"));
  assert.deepEqual(diagnosticSummaryRows(undefined), []);
});
