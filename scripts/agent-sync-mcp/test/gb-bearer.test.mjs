// Per-bot MCP bearers (src/gb-bearer.js):  token shape, hash-only storage, rotate, expiry, revoke, the
// admin reveal page, and the guarantee that a bearer connection can act only as its own persona.

import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/seat-state.js";
import {
  parseGbBearer, newGbBearer, sha256Hex, gbBearerFromRequest, roleOf, GB_BEARER_TTL_MS,
} from "../src/gb-bearer.js";
import { IdentityRouter, GB_IDENTITIES, allowedIdentities } from "../src/delegated-identities.js";
import { gbBearerMintCompletedPage,  gbBearerRevealPage, gbBearerMintRefusedPage, adminPage } from "../src/pages.js";
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
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 5), { ok: true, mcp_accept: false });
  const b = await newGbBearer(ID);
  assert.deepEqual(await S.gbBearerCheck(store, b.hash, NOW + 5), { ok: false, reason: "mismatch" });
  await S.gbBearerMint(store, { hash: b.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 10);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 20), { ok: false, reason: "mismatch" });
  assert.deepEqual(await S.gbBearerCheck(store, b.hash, NOW + 20), { ok: true, mcp_accept: false });
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

test("the reveal page shows the token once, escaped, single-use form, and is not cacheable", async () => {
  const res = gbBearerRevealPage({ identity: ID, token: "gbb_fixer_<script>x", expiresAt: NOW, kind: "persona", mcpAccept: true });
  const html = await res.text();
  assert.match(html, /only time it is shown/);
  assert.match(html, /resubmit only shows this bearer/);
  assert.ok(html.includes("gbb_fixer_&#60;script&#62;x"));
  assert.ok(!html.includes("<script>x"));
  assert.match(res.headers.get("cache-control"), /no-store/);
});

test("the completed-ack page shows outcome metadata, never a token, and is not cacheable", async () => {
  const res = gbBearerMintCompletedPage({ identity: ID, result: { created_at: NOW, expires_at: NOW + 1e9, mint_count: 3, mcp_accept: false } });
  const html = await res.text();
  assert.match(html, /Already Minted/);
  assert.match(html, /never shown again/);
  assert.match(html, /mint #3/);
  assert.match(html, /GR-MCP only/);
  assert.ok(html.includes("identity&#39;s"), "single-escaped apostrophe");
  assert.ok(!html.includes("&#38;"), "no double-escaping");
  assert.ok(!html.includes('<code id="gb-bearer-token">'), "no token element");
  assert.match(res.headers.get("cache-control"), /no-store/);
  const bare = await gbBearerMintCompletedPage({ identity: ID, result: null }).text();
  assert.match(bare, /Already Minted/);
});

test("the reveal send line matches what the minted bearer can actually do", async () => {
  // Persona:  ST-MCP /mcp and GR-MCP.
  const persona = await gbBearerRevealPage({ identity: ID, token: "t", expiresAt: NOW, kind: "persona", mcpAccept: true }).text();
  assert.ok(persona.includes("ST-MCP&#39;s /mcp URL and to GR-MCP"));
  // Ticked seat:  same reach.
  const ticked = await gbBearerRevealPage({ identity: "JET", token: "t", expiresAt: NOW, kind: "seat", mcpAccept: true }).text();
  assert.ok(ticked.includes("ST-MCP&#39;s /mcp URL and to GR-MCP"));
  // Unticked seat:  GR-MCP ONLY, and the page says /mcp refuses it - it must
  // never imply the bearer works on ST-MCP's /mcp.
  const unticked = await gbBearerRevealPage({ identity: "JET", token: "t", expiresAt: NOW, kind: "seat", mcpAccept: false }).text();
  assert.ok(unticked.includes("to GR-MCP (git-relay) only"));
  assert.ok(unticked.includes("ST-MCP&#39;s /mcp refuses this bearer"));
  assert.ok(!unticked.includes("ST-MCP&#39;s /mcp URL and to GR-MCP"));
});

test("the refused mint page never shows a token and states nothing changed", async () => {
  const res = gbBearerMintRefusedPage({ identity: ID });
  const html = await res.text();
  assert.match(html, /Nothing Was Minted/);
  assert.match(html, /current one is unchanged/);
  assert.ok(!/gbb_|sbt_/.test(html));
  assert.match(res.headers.get("cache-control"), /no-store/);
});

const mintIdAt = (ms, tail = "a".repeat(20)) => ms.toString(16).padStart(12, "0") + tail;

test("checked mint: fresh form mints, resubmit is acked from the stored outcome, stale and future refuse, nothing mutates", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  const ok = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW), hash: a.hash, by: "j", expiresAt: NOW + 1e9, mcpAccept: true }, NOW);
  assert.equal(ok.minted, true);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 5), { ok: true, mcp_accept: true });
  // Same form again - a real HTTP resubmit carries a FRESH hash (the route
  // generates a new raw token per POST):  acked from the stored outcome, and
  // the bearer is untouched.
  const again = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW), hash: (await newGbBearer(ID)).hash, by: "j", expiresAt: NOW + 2e9, mcpAccept: false }, NOW + 1000);
  assert.deepEqual(again, {
    minted: false,
    reason: "completed",
    result: { created_at: NOW, expires_at: NOW + 1e9, mint_count: 1, mcp_accept: true },
  });
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 1005), { ok: true, mcp_accept: true }, "the first bearer survives the replay");
  assert.equal((await S.gbBearerStatus(store, NOW + 1005)).expires_at, NOW + 1e9, "no state mutation on the ack");
  assert.ok(!JSON.stringify(again).includes(a.hash), "the ack carries metadata only");
  // Stale form (older than the TTL): refused, nothing minted.
  const stale = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW - S.GB_MINT_FORM_TTL_MS - 1000, "b".repeat(20)), hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  assert.deepEqual(stale, { minted: false, reason: "stale" });
  // Future form beyond the skew: refused.
  const future = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW + 120_000, "c".repeat(20)), hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  assert.deepEqual(future, { minted: false, reason: "stale" });
  // A fresh deliberate mint still works after refusals.
  const b = await newGbBearer(ID);
  const ok2 = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW + 2000, "d".repeat(20)), hash: b.hash, by: "j", expiresAt: NOW + 3e9, mcpAccept: false }, NOW + 2000);
  assert.equal(ok2.minted, true);
  assert.deepEqual(await S.gbBearerCheck(store, b.hash, NOW + 2005), { ok: true, mcp_accept: false });
  // The refusal was audited, the refused mint left no trace in the bearer row.
  const auditRows = JSON.stringify([...store.map.keys()]);
  assert.ok(auditRows.includes("audit"));
});

test("a resubmitted form acks only /admin-visible metadata, whatever hash the retry carries", async () => {
  // Kody discussion_r4372748176:  the raw token is never stored, so the ack
  // can only confirm the completed outcome - the caller recovers nothing
  // secret and mints nothing new.  An owner who lost the token rotates.
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  const id = mintIdAt(NOW);
  await S.gbBearerMintChecked(store, { mintId: id, hash: a.hash, by: "j", expiresAt: NOW + 1e9, mcpAccept: false }, NOW);
  const replay = await S.gbBearerMintChecked(store, { mintId: id, hash: (await newGbBearer(ID)).hash, by: "j", expiresAt: NOW + 2e9, mcpAccept: true }, NOW + 1000);
  assert.equal(replay.reason, "completed");
  assert.equal(replay.result.mcp_accept, false, "the ORIGINAL mint's acceptance, not the retry's");
  assert.equal(replay.result.mint_count, 1);
  assert.ok(!("minted" in replay) || replay.minted === false);
  assert.deepEqual(await S.gbBearerCheck(store, a.hash, NOW + 1005), { ok: true, mcp_accept: false }, "the original bearer survives with its original acceptance");
  assert.ok(JSON.stringify([...store.map.keys()]).includes("audit"), "the replay was audited");
});

test("replay handling spans the whole validity window: 64+ mints, then the oldest form replays without minting", async () => {
  const store = S.memoryStore();
  const firstId = mintIdAt(NOW, "0".repeat(20));
  for (let i = 0; i < 64; i++) {
    const b = await newGbBearer(ID);
    const r = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW + i * 1000, String(i).padStart(20, "0")), hash: b.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + i * 1000);
    assert.equal(r.minted, true, `mint ${i}`);
  }
  // The 64-record window is full of still-fresh forms: the oldest form's
  // tombstone was NOT evicted, so its replay is acked as completed and
  // absolutely does not mint...
  const replay = await S.gbBearerMintChecked(store, { mintId: firstId, hash: (await newGbBearer(ID)).hash, by: "j", expiresAt: NOW + 2e9 }, NOW + 64_000);
  assert.equal(replay.minted, false);
  assert.equal(replay.reason, "completed");
  assert.equal(replay.result.mint_count, 1, "the ack reports the ORIGINAL mint");
  assert.equal((await S.gbBearerStatus(store, NOW + 64_000)).mint_count, 64, "the replay minted nothing");
  // ... and a 65th fresh form refuses fail-closed instead of evicting it.
  const sixtyFive = await S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW + 64_000, "f".repeat(20)), hash: (await newGbBearer(ID)).hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 64_000);
  assert.deepEqual(sixtyFive, { minted: false, reason: "busy" });
  assert.equal((await S.gbBearerStatus(store, NOW + 64_000)).mint_count, 64, "no 65th mutation");
  // Only records past their validity window evict; the old form then refuses as stale, never mints.
  const later = NOW + S.GB_MINT_FORM_TTL_MS + 1000;
  const b2 = await newGbBearer(ID);
  const ok = await S.gbBearerMintChecked(store, { mintId: mintIdAt(later, "e".repeat(20)), hash: b2.hash, by: "j", expiresAt: later + 1e9 }, later);
  assert.equal(ok.minted, true, "mints resume once the window drains");
  assert.ok(store.map.get("gb_bearer_mint_forms").consumed.length <= 64, "bounded");
  const oldReplay = await S.gbBearerMintChecked(store, { mintId: firstId, hash: b2.hash, by: "j", expiresAt: later + 1e9 }, later + 1000);
  assert.deepEqual(oldReplay, { minted: false, reason: "stale" });
});

test("a future-dated (max-skew) form cannot replay after a consumption-timed tombstone would have dropped", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  // Dated 60s into the future:  the largest accepted clock skew.
  const mintId = mintIdAt(NOW + 60_000, "f".repeat(20));
  const first = await S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  assert.equal(first.minted, true, "a form dated within the future skew is accepted");
  // One hour plus 1ms after CONSUMPTION:  a consumption-timed tombstone would
  // already have dropped, but the form itself stays fresh for ~60s more.  The
  // replay must still not mint - retention follows the form's own validity,
  // and the still-fresh form is acked as completed.
  const replay = await S.gbBearerMintChecked(store, { mintId, hash: (await newGbBearer(ID)).hash, by: "j", expiresAt: NOW + 2e9 }, NOW + S.GB_MINT_FORM_TTL_MS + 1);
  assert.equal(replay.minted, false);
  assert.equal(replay.reason, "completed");
  assert.equal((await S.gbBearerStatus(store, NOW + S.GB_MINT_FORM_TTL_MS + 1)).mint_count, 1, "no second mint");
  // Once the form's own validity ends, the same id refuses as stale, never mints.
  const expired = await S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 3e9 }, NOW + S.GB_MINT_FORM_TTL_MS + 60_001);
  assert.deepEqual(expired, { minted: false, reason: "stale" });
});

test("concurrent identical submissions mint exactly once", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  const b = await newGbBearer(ID);
  const mintId = mintIdAt(NOW);
  const [r1, r2] = await Promise.all([
    S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW),
    S.gbBearerMintChecked(store, { mintId, hash: b.hash, by: "j", expiresAt: NOW + 1e9 }, NOW),
  ]);
  assert.equal([r1.minted, r2.minted].filter(Boolean).length, 1, "exactly one mint landed");
  assert.equal((await S.gbBearerStatus(store, NOW)).mint_count, 1);
});

test("a mid-mint failure rolls back: no bearer, no consumed form, retry succeeds", async () => {
  // A transactional store that really rolls back (the Durable Object's
  // storage.transaction semantics), rigged to fail the consumed-marker write.
  const base = S.memoryStore();
  const store = Object.create(base);
  store.transaction = async (cb) => {
    const snapshot = new Map([...base.map.entries()]);
    try { return await cb(base); }
    catch (e) { base.map.clear(); for (const [k, v] of snapshot) base.map.set(k, v); throw e; }
  };
  const a = await newGbBearer(ID);
  const mintId = mintIdAt(NOW);
  const realPut = base.put.bind(base);
  base.put = async (k, v) => { if (k === "gb_bearer_mint_forms") throw new Error("storage fault"); return realPut(k, v); };
  await assert.rejects(S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW), /storage fault/);
  base.put = realPut;
  assert.equal((await S.gbBearerStatus(store, NOW)).active, false, "no bearer row survived the fault");
  assert.equal(store.map.has("gb_bearer_mint_forms"), false, "no consumed marker survived the fault");
  const retry = await S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 1000);
  assert.equal(retry.minted, true, "the same form is usable after a rolled-back fault");
  // And the whole mint happens inside ONE transaction.
  let transactions = 0;
  const spy = Object.create(base);
  spy.transaction = (cb) => { transactions++; return base.transaction(cb); };
  await S.gbBearerMintChecked(spy, { mintId: mintIdAt(NOW + 5000, "9".repeat(20)), hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 5000);
  assert.equal(transactions, 1, "check-consume-mint is a single transaction");
});

test("the admin form accepts the bearer actions for a persona or a known seat only", () => {
  const csrf = "a".repeat(64);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: ID }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "revoke_gb_bearer", identity: ID }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: "ECHO", mcp_accept: "1" }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: "ECHO" }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: "GB-FIXER; DROP" }).success, false);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: "gb-fixer" }).success, false);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: ID, mint_id: "a".repeat(32) }).success, true);
  assert.equal(AdminForm.safeParse({ csrf, action: "mint_gb_bearer", identity: ID, mint_id: "not-hex" }).success, false);
  // An uppercase unknown name passes the form's shape check;  the admin
  // handler is what refuses an identity that is no persona and no seat.
});

test("the admin section lists status and buttons and never a token or hash", async () => {
  const html = await adminPage({
    email: "mail@example.test", seats: [], clients: [], refusals: [], audits: [], csrf: "b".repeat(64), generatedAt: NOW,
    gbBearers: [
      { identity: ID, credentials: true, status: { active: true, created_at: NOW, expires_at: NOW + 1e9, last_used_at: 0 } },
      { identity: GB_IDENTITIES[1], credentials: false, status: { active: false } },
    ],
  }, new Headers()).text();
  assert.match(html, /Per-Identity Bearers/);
  assert.match(html, /Rotate \(Mint New\)/);
  assert.match(html, /No credentials loaded/);
  assert.ok(!/gbb_/.test(html));
});

test("mint id shape is enforced: malformed ids refuse as invalid before any timestamp math (kody 4372747948)", async () => {
  const store = S.memoryStore();
  for (const mintId of ["zz" + "a".repeat(30), "abcd", 123, null]) {
    const r = await S.gbBearerMintChecked(store, { mintId, hash: "h", by: "j", expiresAt: NOW + 1e9 }, NOW);
    assert.deepEqual(r, { minted: false, reason: "invalid" }, JSON.stringify(mintId));
  }
});

test("an unexpected store failure leaves a gb_bearer_mint_error audit row and still throws (kody 4372747686)", async () => {
  const store = S.memoryStore();
  const failing = { ...store, transaction: async () => { throw new Error("storage on fire"); } };
  await assert.rejects(
    S.gbBearerMintChecked(failing, { mintId: mintIdAt(NOW), hash: "h", by: "j", expiresAt: NOW + 1e9 }, NOW),
    /storage on fire/,
  );
  const rows = await store.get("audit");
  assert.ok(rows.some((r) => r.event === "gb_bearer_mint_error" && r.by === "j"), JSON.stringify(rows));
});

test("every mint attempt writes exactly one outcome audit row from the central finally (kody 4372747686)", async () => {
  const store = S.memoryStore();
  const a = await newGbBearer(ID);
  const mintId = mintIdAt(NOW);
  await S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW);
  await S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 100);
  await S.gbBearerMintChecked(store, { mintId: "not-a-form", hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 200);
  const rows = (await store.get("audit")) ?? [];
  const count = (event) => rows.filter((r) => r.event === event).length;
  assert.equal(count("gb_bearer_minted"), 1, JSON.stringify(rows));
  assert.equal(count("gb_bearer_mint_replay"), 1, JSON.stringify(rows));
  assert.equal(count("gb_bearer_mint_refused"), 1, JSON.stringify(rows));
  assert.equal(count("gb_bearer_mint_error"), 0, JSON.stringify(rows));
  assert.equal(rows.length, 3, "three attempts, three rows, exactly one each");
});

test("an audit-write failure aborts the mint: no bearer, no tombstone, no reveal (kody 4372747686)", async () => {
  // DO-semantic store (snapshot rollback on throw) whose audit write fails
  // ONCE, inside the transaction:  the mint must abort with it.
  const base = S.memoryStore();
  const store = Object.create(base);
  store.transaction = async (cb) => {
    const snapshot = new Map([...base.map.entries()]);
    try { return await cb(base); }
    catch (e) { base.map.clear(); for (const [k, v] of snapshot) base.map.set(k, v); throw e; }
  };
  const realPut = base.put.bind(base);
  let failedOnce = false;
  base.put = async (k, v) => { if (k === "audit" && !failedOnce) { failedOnce = true; throw new Error("audit store down"); } return realPut(k, v); };
  const a = await newGbBearer(ID);
  const mintId = mintIdAt(NOW);
  // The mint throws, so the route never reaches its reveal page.
  await assert.rejects(S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW), /audit store down/);
  base.put = realPut;
  assert.equal((await S.gbBearerStatus(store, NOW)).active, false, "no bearer row survived the audit failure");
  assert.equal(store.map.has("gb_bearer_mint_forms"), false, "no tombstone survived the audit failure");
  // The aborted attempt is recorded exactly once, by the outside error row.
  const rows = (await store.get("audit")) ?? [];
  assert.equal(rows.filter((r) => r.event === "gb_bearer_minted").length, 0, "no false minted row: " + JSON.stringify(rows));
  assert.equal(rows.filter((r) => r.event === "gb_bearer_mint_error").length, 1, JSON.stringify(rows));
  // The same form mints once the audit store recovers.
  const retry = await S.gbBearerMintChecked(store, { mintId, hash: a.hash, by: "j", expiresAt: NOW + 1e9 }, NOW + 1000);
  assert.equal(retry.minted, true, "the form survives an aborted attempt");
});

test("a commit failure after the mint body leaves only the error row, never a false minted row (kody 4372747686)", async () => {
  // A commit failure in a real Durable Object rolls the whole transaction
  // back, including the finally's row;  the fake emulates that.
  const base = S.memoryStore();
  const store = Object.create(base);
  store.transaction = async (cb) => {
    const snapshot = new Map([...base.map.entries()]);
    try { await cb(base); throw new Error("commit lost"); }
    catch (e) { base.map.clear(); for (const [k, v] of snapshot) base.map.set(k, v); throw e; }
  };
  await assert.rejects(
    S.gbBearerMintChecked(store, { mintId: mintIdAt(NOW), hash: "h", by: "j", expiresAt: NOW + 1e9 }, NOW),
    /commit lost/,
  );
  const rows = (await base.get("audit")) ?? [];
  assert.equal(rows.filter((r) => r.event === "gb_bearer_minted").length, 0, "no false minted row: " + JSON.stringify(rows));
  assert.equal(rows.filter((r) => r.event === "gb_bearer_mint_error").length, 1, JSON.stringify(rows));
});

test("gbBearerMismatch counts per configured identity, buckets strangers, and stays bounded", async () => {
  const store = S.memoryStore();
  const configured = new Set(["JET", "ECHO"]);

  await S.gbBearerMismatch(store, "JET", configured, 1000);
  await S.gbBearerMismatch(store, "JET", configured, 2000);
  await S.gbBearerMismatch(store, "ECHO", configured, 3000);
  // An attacker-chosen name lands in the one bucket, never its own key.
  await S.gbBearerMismatch(store, "JET-EVIL", configured, 4000);
  await S.gbBearerMismatch(store, "anything at all", configured, 5000);

  let map = await S.gbBearerMismatches(store);
  assert.deepEqual(Object.keys(map).sort(), ["ECHO", "JET", "unconfigured"]);
  assert.equal(map.JET.count, 2);
  assert.equal(map.JET.last_at, 2000);
  assert.equal(map.ECHO.count, 1);
  assert.equal(map.unconfigured.count, 2);
  for (const row of Object.values(map)) {
    assert.ok(!("hash" in row) && !("token" in row) && !("bearer" in row));
  }

  // A seat dropped from the configuration is evicted on the next write.
  await S.gbBearerMismatch(store, "JET", new Set(["JET"]), 6000);
  map = await S.gbBearerMismatches(store);
  assert.deepEqual(Object.keys(map).sort(), ["JET", "unconfigured"]);
  assert.equal(map.JET.count, 3);
});

test("gbBearerMismatch recovers from a corrupted count and a bad clock", async () => {
  const store = S.memoryStore();
  await store.put("gb_bearer_mismatches", { JET: { count: "not-a-number", last_at: -5 } });
  await S.gbBearerMismatch(store, "JET", new Set(["JET"]), Number.NaN);
  const map = await S.gbBearerMismatches(store);
  assert.equal(map.JET.count, 1);
  assert.ok(Number.isFinite(map.JET.last_at));
});

test("gbBearerMismatches is empty before any mismatch", async () => {
  assert.deepEqual(await S.gbBearerMismatches(S.memoryStore()), {});
});
