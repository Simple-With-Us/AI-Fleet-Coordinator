// The Linear token health route (src/health.js), no network, no Linear.
import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import { classifyPath } from "../src/policy.js";
import { wakeSignature } from "../src/wake.js";
import { HEALTH_CONTRACT, classifySeat, healthReport, healthKey, verifyHealth } from "../src/health.js";
import { memoryQuota } from "./quota-helper.mjs";

const KEY = "k".repeat(40);
const SECRET = ["lin", "oauth", "secret", "value"].join("_");
const NOW_S = 1_800_000_000;
const DAY = 86_400;
const token = (ageDays, lifeDays = 30 - ageDays) => ({ minted_at: NOW_S - ageDays * DAY, expires_at: NOW_S + lifeDays * DAY, mint_count: 2 });
const row = (t, note = {}) => ({ token: t, note: { last_ok_at: 0, last_fail_at: 0, last_fail_kind: null, ...note } });

test("classifyPath routes the health path and nothing near it", () => {
  assert.equal(classifyPath("/internal/health/linear"), "linear_health");
  assert.equal(classifyPath("/internal/health/linear/"), null);
  assert.equal(classifyPath("/internal/health"), null);
});

test("a fresh token is ok and shows the day-28 countdown", () => {
  const s = classifySeat("ECHO", row(token(3), { last_ok_at: NOW_S - 3 * DAY }), NOW_S);
  assert.equal(s.state, "ok");
  assert.equal(s.minted_age_days, 3);
  assert.equal(s.remint_due_in_days, 25);
  assert.equal(s.expires_in_days, 27);
});

test("past day 28 without a re-mint warns", () => {
  assert.equal(classifySeat("ECHO", row(token(29)), NOW_S).state, "warn");
});

test("a failed early mint with a working token warns", () => {
  const s = classifySeat("GB-FIXER", row(token(28.5), { last_ok_at: NOW_S - 28 * DAY, last_fail_at: NOW_S - 100, last_fail_kind: "mint" }), NOW_S);
  assert.equal(s.state, "warn");
  assert.equal(s.last_mint_fail_kind, "mint");
});

test("an expired token fails, and so does no token after a failed mint", () => {
  assert.equal(classifySeat("ECHO", row(token(30, 0)), NOW_S).state, "fail");
  assert.equal(classifySeat("ECHO", row(null, { last_fail_at: NOW_S - 5, last_fail_kind: "network" }), NOW_S).state, "fail");
});

test("credentials never minted warn, a recovered failure is ok", () => {
  assert.equal(classifySeat("ECHO", row(null), NOW_S).state, "warn");
  const s = classifySeat("ECHO", row(token(1), { last_fail_at: NOW_S - 2 * DAY, last_fail_kind: "mint", last_ok_at: NOW_S - DAY }), NOW_S);
  assert.equal(s.state, "ok");
});

test("an unknown failure kind is reduced to a fixed code", () => {
  const s = classifySeat("ECHO", row(token(1), { last_fail_at: NOW_S, last_fail_kind: "secret-looking-text" }), NOW_S);
  assert.equal(s.last_mint_fail_kind, "unexpected");
});

test("the report sorts seats, takes the worst state, and carries no token fields", () => {
  const r = healthReport([{ seat: "GB-FIXER", row: row(token(29)) }, { seat: "ECHO", row: row(token(1)) }], NOW_S);
  assert.deepEqual(r.seats.map((s) => s.seat), ["ECHO", "GB-FIXER"]);
  assert.equal(r.worst, "warn");
  assert.equal(r.ok, false);
  const text = JSON.stringify(r);
  for (const word of ["access_token", "client_id", "client_secret", "cred_fp"]) assert.ok(!text.includes(word));
});

test("seat-state records the mint outcome and linearHealth carries no token value", async () => {
  const store = S.memoryStore();
  await S.linearTokenSet(store, { access_token: SECRET, minted_at: NOW_S, expires_at: NOW_S + 30 * DAY, mint_count: 1, cred_fp: "abc" });
  await S.linearMintNote(store, { ok: true, at: NOW_S * 1000 });
  await S.linearMintNote(store, { ok: false, kind: "rate_limited", at: (NOW_S + 5) * 1000 });
  const h = await S.linearHealth(store);
  assert.deepEqual(h.note, { last_ok_at: NOW_S, last_fail_at: NOW_S + 5, last_fail_kind: "rate_limited" });
  assert.ok(!JSON.stringify(h).includes(SECRET));
  assert.ok(!JSON.stringify(h).includes("abc"));
});

async function signed(body, key = KEY) {
  const bytes = new TextEncoder().encode(JSON.stringify(body));
  return { bytes, signature: await wakeSignature(key, bytes) };
}

test("verifyHealth accepts a signed fresh request and refuses the rest", async () => {
  const good = await signed({ contract: HEALTH_CONTRACT, sent_at: NOW_S });
  const now = NOW_S * 1000;
  assert.equal((await verifyHealth({ ...good, key: KEY, now })).ok, true);
  assert.equal((await verifyHealth({ ...good, key: "", now })).status, 404);
  assert.equal((await verifyHealth({ ...good, key: "short", now })).status, 404);
  assert.equal((await verifyHealth({ ...good, key: "z".repeat(40), now })).status, 401);
  assert.equal((await verifyHealth({ ...good, signature: "nothex", key: KEY, now })).status, 401);
  const stale = await signed({ contract: HEALTH_CONTRACT, sent_at: NOW_S - 600 });
  assert.equal((await verifyHealth({ ...stale, key: KEY, now })).reason, "stale");
  const future = await signed({ contract: HEALTH_CONTRACT, sent_at: NOW_S + 600 });
  assert.equal((await verifyHealth({ ...future, key: KEY, now })).reason, "future");
  const extra = await signed({ contract: HEALTH_CONTRACT, sent_at: NOW_S, seat: "ECHO" });
  assert.equal((await verifyHealth({ ...extra, key: KEY, now })).reason, "bad_fields");
  const wrong = await signed({ contract: "other/1", sent_at: NOW_S });
  assert.equal((await verifyHealth({ ...wrong, key: KEY, now })).reason, "bad_contract");
});

test("healthKey needs 32 characters", () => {
  assert.equal(healthKey(undefined), "");
  assert.equal(healthKey("a".repeat(31)), "");
  assert.equal(healthKey(` ${"a".repeat(32)} `), "a".repeat(32));
});

test("a failed and a good mint each leave a note through the real LinearClient", async () => {
  const { LinearClient } = await import("../src/linear.js");
  const store = S.memoryStore();
  const clock = { t: NOW_S * 1000 };
  const gate = {
    linearTokenGet: () => S.linearTokenGet(store),
    linearTokenSet: (e) => S.linearTokenSet(store, e),
    linearMintNote: (e) => S.linearMintNote(store, e),
    idemBegin: (o) => S.idemBegin(store, o, clock.t),
    idemDrop: (o) => S.idemDrop(store, o, clock.t),
  };
  let ok = false;
  const fetchFn = async () => new Response(JSON.stringify(ok ? { access_token: "tok_x", expires_in: 2592000 } : { error: "invalid_client" }), { status: ok ? 200 : 401 });
  const client = new LinearClient({ quota: memoryQuota(), seat: "ECHO", clientId: "id", clientSecret: "sec", gate, fetch: fetchFn, now: () => clock.t, log: () => {} });
  await assert.rejects(() => client.token());
  let h = await S.linearHealth(store);
  assert.equal(h.note.last_fail_kind, "mint");
  assert.equal(h.token, null);
  ok = true;
  clock.t += 1000;
  await client.token();
  h = await S.linearHealth(store);
  assert.equal(h.note.last_ok_at, NOW_S + 1);
  assert.equal(h.token.mint_count, 1);
  assert.ok(!JSON.stringify(h).includes("tok_x"));
});
