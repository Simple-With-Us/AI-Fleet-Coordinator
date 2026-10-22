// Per-bot MCP bearers (src/gb-bearer.js):  token shape, hash-only storage, rotate, expiry, revoke, the
// admin reveal page, and the guarantee that a bearer connection can act only as its own persona.

import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import {
  parseGbBearer, newGbBearer, sha256Hex, gbBearerFromRequest, roleOf, GB_BEARER_TTL_MS,
} from "../src/gb-bearer.js";
import { IdentityRouter, GB_IDENTITIES, allowedIdentities } from "../src/delegated-identities.js";
import { gbBearerRevealPage, adminPage } from "../src/pages.js";
import { AdminForm } from "../src/forms.js";
import { okResult } from "../src/contract.js";

const NOW = 1_800_000_000_000;
const ID = GB_IDENTITIES[0];

test("a new bearer names its persona, is 256 bits, and parses back", async () => {
  const { token, hash } = await newGbBearer(ID);
  assert.match(token, new RegExp(`^gbb_${roleOf(ID)}_[A-Za-z0-9_-]{43}$`));
  assert.equal(hash, await sha256Hex(token));
  assert.equal(hash.length, 64);
  const parsed = parseGbBearer(token);
  assert.equal(parsed.identity, ID);
  const again = await newGbBearer(ID);
  assert.notEqual(again.token, token);
});

test("every GB persona can mint, and nothing else can", async () => {
  for (const identity of GB_IDENTITIES) assert.equal(parseGbBearer((await newGbBearer(identity)).token).identity, identity);
  await assert.rejects(() => newGbBearer("ECHO"));
  assert.equal(parseGbBearer("gbb_echo_" + "a".repeat(43)), null);
  assert.equal(parseGbBearer("gbb_fixer_short"), null);
  assert.equal(parseGbBearer(`gbb_fixer_${"a".repeat(44)}`), null);
  assert.equal(parseGbBearer("seat:grant:secret"), null);
  assert.equal(parseGbBearer(undefined), null);
});

test("only a gbb_ bearer header is picked up", () => {
  const req = (value) => new Request("https://x.test/mcp", { headers: value ? { authorization: value } : {} });
  assert.equal(gbBearerFromRequest(req("Bearer gbb_fixer_abc")), "gbb_fixer_abc");
  assert.equal(gbBearerFromRequest(req("bearer gbb_fixer_abc")), "gbb_fixer_abc");
  assert.equal(gbBearerFromRequest(req("Bearer ECHO:grant:secret")), null);
  assert.equal(gbBearerFromRequest(req("Basic gbb_fixer_abc")), null);
  assert.equal(gbBearerFromRequest(req(null)), null);
});

test("minting stores the hash only, and the status and audit never show it", async () => {
  const store = S.memoryStore();
  const { token, hash } = await newGbBearer(ID);
  await S.gbBearerMint(store, { hash, by: "mail@example.test", expiresAt: NOW + GB_BEARER_TTL_MS }, NOW);
  const status = await S.gbBearerStatus(store, NOW + 1000);
  assert.equal(status.active, true);
  assert.equal(status.mint_count, 1);
  assert.equal(status.expires_at, NOW + GB_BEARER_TTL_MS);
  const seen = JSON.stringify([status, await store.get("audit")]);
  assert.ok(!seen.includes(hash));
  assert.ok(!seen.includes(token));
  assert.ok(!JSON.stringify([...store.entries?.() ?? []]).includes(token));
});

test("check accepts the hash, refuses a wrong one, and rotating replaces the old bearer at once", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 5), { ok: true });
  const b = await newGbBearer(ID);
  assert.deepEqual(await S.gbBearerCheck(store, b.hash, NOW + 5), { ok: false, reason: "mismatch" });
  await S.gbBearerMint(store, { hash: b.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 10);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 20), { ok: false, reason: "mismatch" });
  assert.deepEqual(await S.gbBearerCheck(store, b.hash, NOW + 20), { ok: true });
  assert.equal((await S.gbBearerStatus(store, NOW + 20)).mint_count, 2);
});

test("an expired bearer and a revoked bearer both refuse", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW), { ok: false, reason: "none" });
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: NOW + 1000 }, NOW);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 1000), { ok: false, reason: "expired" });
  assert.equal((await S.gbBearerStatus(store, NOW + 1000)).expired, true);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 2000);
  assert.deepEqual(await S.gbBearerRevoke(store, { by: "j" }, NOW + 3000), { revoked: true });
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 3001), { ok: false, reason: "none" });
  assert.deepEqual(await S.gbBearerStatus(store, NOW + 3001), { active: false });
});

test("last_used is written at most once an hour", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  await S.gbBearerMint(store, { hash: a.hash, by: "j", expiresAt: NOW + 1e10 }, NOW);
  await S.gbBearerCheck(store, a.hash, NOW + 10);
  const first = (await S.gbBearerStatus(store, NOW + 10)).last_used_at;
  await S.gbBearerCheck(store, a.hash, NOW + 60_000);
  assert.equal((await S.gbBearerStatus(store, NOW + 60_000)).last_used_at, first);
  await S.gbBearerCheck(store, a.hash, NOW + 3_700_000);
  assert.equal((await S.gbBearerStatus(store, NOW + 3_700_000)).last_used_at, NOW + 3_700_000);
});

test("every Grok Bot identity round-trips through newGbBearer and parseGbBearer", async () => {
  for (const identity of GB_IDENTITIES) {
    const { token } = await newGbBearer(identity);
    assert.equal(parseGbBearer(token)?.identity, identity, identity);
  }
});

class Stub {
  constructor(seat) {
    this.seat = seat; this.paused = false; this.calls = []; this.audits = [];
    this.grantRef = "abcdef012345"; this.clientId = "gb-bearer"; this.zulip = {}; this.linear = null; this.recall = null;
    this.gate = { auditCall: async (row) => { this.audits.push(row); } };
  }
  async call(name, args) { this.calls.push({ name, args }); return okResult("{}", { seat: this.seat }); }
  requireScope() {}
  async reserve() {}
  async safely(fn) { return fn(); }
  scrub(text) { return text; }
}

test("a bearer connection can act only as its own persona", async () => {
  assert.deepEqual([...allowedIdentities(ID)], [ID]);
  const caller = new Stub(ID);
  const router = new IdentityRouter({ callerTools: caller, credentials: null, createIdentity: null });
  const other = GB_IDENTITIES[1];
  const res = await router.call("zulip_topics", { identity: other });
  assert.equal(res.isError, true);
  assert.equal(caller.calls.length, 0);
  assert.ok(!JSON.stringify(res).includes("ECHO"));
  const echo = await router.call("zulip_topics", { identity: "ECHO" });
  assert.equal(echo.isError, true);
  assert.equal(caller.calls.length, 0);
  const own = await router.call("zulip_topics", { identity: ID });
  assert.notEqual(own.isError, true);
  assert.equal(caller.calls.length, 1);
});

test("the reveal page shows the token once, escaped, and is not cacheable", async () => {
  const res = gbBearerRevealPage({ identity: ID, token: "gbb_fixer_<script>x", expiresAt: NOW });
  const html = await res.text();
  assert.match(html, /only time it is shown/);
  assert.ok(html.includes("gbb_fixer_&#60;script&#62;x"));
  assert.ok(!html.includes("<script>x"));
  assert.match(res.headers.get("cache-control"), /no-store/);
});

test("the admin form accepts the bearer actions with a GB identity only", () => {
  const csrf = "a".repeat(64);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: ID }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "revoke_gb_bearer", identity: ID }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: "ECHO" }).success, false);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: "GB-FIXER; DROP" }).success, false);
});

test("the admin section lists status and buttons and never a token or hash", async () => {
  const html = await adminPage({
    email: "mail@example.test", seats: [], clients: [], refusals: [], audits: [], csrf: "b".repeat(64), generatedAt: NOW,
    gbBearers: [
      { identity: ID, credentials: true, status: { active: true, created_at: NOW, expires_at: NOW + 1e9, last_used_at: 0 } },
      { identity: GB_IDENTITIES[1], credentials: false, status: { active: false } },
    ],
  }, new Headers()).text();
  assert.match(html, /Grok Bot Bearers/);
  assert.match(html, /Rotate \(Mint New\)/);
  assert.match(html, /No credentials loaded/);
  assert.ok(!/gbb_/.test(html));
});
