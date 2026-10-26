// Seat bearers (sbt_<SEAT>_...):  an admin-minted bearer for a fleet seat that
// serves /mcp only when the mint recorded the owner's per-seat choice
// (mcp_accept).  Covers the token shape, the mint/check round-trip and the
// acceptance gate.

import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { newSeatBearer, parseSbtBearer, sbtBearerFromRequest, newGbBearer, sha256Hex, GB_BEARER_TTL_MS } from "../src/gb-bearer.js";
import * as S from "../src/seat-state.js";

globalThis.crypto ??= webcrypto;

const NOW = 1_800_000_000_000;
const SEAT = "ECHO";

test("a seat bearer round-trips through newSeatBearer and parseSbtBearer", async () => {
  const b = await newSeatBearer(SEAT);
  assert.match(b.token, /^sbt_echo_[A-Za-z0-9_-]{43}$/);
  const parsed = parseSbtBearer(b.token);
  assert.equal(parsed.identity, SEAT);
  assert.equal(parsed.token, b.token);
  assert.equal(await sha256Hex(parsed.token), b.hash);
  assert.equal(parseSbtBearer("sbt_nobody_" + "a".repeat(43)), null); // not a known seat
  assert.equal(parseSbtBearer("sbt_echo_short"), null); // bad tail
  assert.equal(parseSbtBearer("sbt_ECHO_" + "a".repeat(43)), null); // uppercase seat is malformed
  assert.equal(parseSbtBearer((await newGbBearer("GB-FIXER")).token), null); // gbb_ is not sbt_
  assert.equal(parseSbtBearer("not-a-token"), null);
});

test("sbtBearerFromRequest picks only an sbt_ bearer header", async () => {
  const b = await newSeatBearer(SEAT);
  const req = (h) => new Request("https://x.test/mcp", { headers: h });
  assert.equal(sbtBearerFromRequest(req({ authorization: `Bearer ${b.token}` })), b.token);
  assert.equal(sbtBearerFromRequest(req({ authorization: "Bearer gbb_fixer_abc" })), null);
  assert.equal(sbtBearerFromRequest(req({})), null);
});

test("a seat mint with mcp_accept checks ok and reports the choice; without it the choice is false", async () => {
  const store = S.memoryStore();
  const yes = await newSeatBearer(SEAT);
  await S.gbBearerMint(store, { hash: yes.hash, by: "j", expiresAt: NOW + 1e9, mcpAccept: true }, NOW);
  assert.deepEqual(await S.gbBearerCheck(store, yes.hash, NOW + 5), { ok: true, mcp_accept: true });

  const store2 = S.memoryStore();
  const no = await newSeatBearer(SEAT);
  await S.gbBearerMint(store2, { hash: no.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  assert.deepEqual(await S.gbBearerCheck(store2, no.hash, NOW + 5), { ok: true, mcp_accept: false });
});

test("minting without mcp_accept never serves the consent-bypassing route", async () => {
  // The serveSbtBearerMcp gate is `checked.mcp_accept !== true -> refuse`, so a
  // store whose row has no recorded choice (legacy rows, mints that skipped the
  // checkbox) can only ever produce mcp_accept: false.  This pins the fail-
  // closed shape of the check result itself.
  const store = S.memoryStore();
  const b = await newSeatBearer(SEAT);
  await S.gbBearerMint(store, { hash: b.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  const checked = await S.gbBearerCheck(store, b.hash, NOW + 5);
  assert.ok(checked.mcp_accept !== true);
});

test("a wrong seat bearer refuses exactly like a wrong persona bearer", async () => {
  const store = S.memoryStore();
  const a = await newSeatBearer(SEAT);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: NOW + 1e9, mcpAccept: true }, NOW);
  const wrong = await newSeatBearer(SEAT);
  assert.deepEqual(await S.gbBearerCheck(store, wrong.hash, NOW + 5), { ok: false, reason: "mismatch" });
  await S.gbBearerRevoke(store, { by: "j" }, NOW + 10);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 11), { ok: false, reason: "none" });
});
