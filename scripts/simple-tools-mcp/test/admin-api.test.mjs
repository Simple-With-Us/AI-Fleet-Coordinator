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
