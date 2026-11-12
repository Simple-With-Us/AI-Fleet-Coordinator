// DO burn reduction (R4):  the isolate verified-identity cache
// (src/identity-cache.js) and the HostedTools behavior it drives.  The
// cache is pinned on its window discipline (only positives stored,
// expiry is a miss, enrichment never extends the window, eviction by key
// and by seat);  HostedTools is pinned on the cached path costing no
// gate RPCs for a read or a whoami, a write keeping its reserve, and
// refusals evicting.

import test from "node:test";
import assert from "node:assert/strict";
import { IdentityCache, bearerCacheMs } from "../src/identity-cache.js";
import { HostedTools } from "../src/hosted-tools.js";
import { ApiError } from "../src/contract.js";

const KEY = "a".repeat(64);

test("index.js starts every identity entry on the cache's own clock", async () => {
  // Expiry and the `since` race guard both run on IDENTITY_CACHE.now();  an entry
  // started on Date.now() would put them on two clocks (Kody, PR 1441).
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../src/index.js", import.meta.url), "utf8");
  const starts = [...src.matchAll(/const now = ([A-Za-z_.]+)\(\);[^\n]*IDENTITY_CACHE\.set|const now = ([A-Za-z_.]+)\(\);(?:[^\n]*\n){0,6}?[^\n]*IDENTITY_CACHE\.set\(/g)];
  assert.ok(starts.length >= 3, `found ${starts.length} cache writes`);
  for (const m of starts) assert.equal(m[1] ?? m[2], "IDENTITY_CACHE.now", m[0].slice(0, 120));
});

test("a positive answer older than a refusal or seat eviction is not stored, and the maps stay bounded", () => {
  let now = 1000;
  const cache = new IdentityCache({ now: () => now, max: 2 });
  const since = now;
  now += 3;
  cache.evict(KEY);
  cache.set(KEY, { seat: "ECHO", via: "gb-bearer" }, 60_000, now, { since });
  assert.equal(cache.get(KEY), null, "a refusal after the check started wins");
  cache.set(KEY, { seat: "ECHO", via: "gb-bearer" }, 60_000, now, { since: now });
  assert.ok(cache.get(KEY), "a check that started after the refusal stores");
  const other = "b".repeat(64);
  const started = now;
  now += 2;
  cache.evictSeat("ECHO");
  cache.set(other, { seat: "ECHO", via: "gb-bearer" }, 60_000, now, { since: started });
  assert.equal(cache.get(other), null, "a seat eviction after the check started wins too");
  cache.set(other, { seat: "ECHO", via: "gb-bearer" }, 60_000, now);
  assert.ok(cache.get(other), "without since the caller opts out, as before");
  for (const k of ["c", "d", "e", "f"]) cache.evict(k.repeat(64));
  assert.ok(cache.refusedAt.size <= 2, "the refusal map is bounded");
});

test("an entry capped at an expiry and started from the same reading never outlives it", () => {
  let now = 1000;
  const cache = new IdentityCache({ now: () => (now += 1) });
  const read = cache.now();
  const expiresAt = read + 500;
  cache.set(KEY, { seat: "ECHO", via: "gb-bearer" }, bearerCacheMs({ gateIdentityCacheMs: 3_600_000 }, { expires_at: expiresAt }, read), read);
  assert.equal(cache.entries.get(KEY).expiresAt, expiresAt);
});

test("only positive verifications are stored, and a fresh entry is served until its window closes", () => {
  let now = 1000;
  const cache = new IdentityCache({ now: () => now });
  cache.set(KEY, { seat: "ECHO", via: "oauth", epoch: 3 }, 60_000, now);
  assert.equal(cache.get(KEY, now)?.epoch, 3);
  now += 59_999;
  assert.equal(cache.get(KEY, now)?.seat, "ECHO");
  now += 1;
  assert.equal(cache.get(KEY, now), null, "at the window's edge the entry is gone, not stale-served");
  assert.deepEqual(cache.pending(), {});
});

test("a zero or negative window stores nothing (the cache's off switch)", () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "oauth" }, 0, 0);
  assert.equal(cache.get(KEY, 0), null);
  assert.deepEqual(cache.pending(), {});
});

test("update enriches a fresh entry without extending its window", () => {
  let now = 1000;
  const cache = new IdentityCache({ now: () => now });
  cache.set(KEY, { seat: "ECHO", via: "sbt-bearer" }, 60_000, now);
  now += 50_000;
  cache.update(KEY, { role: { ok: true, user_id: 42, role: 400 } }, now);
  assert.deepEqual(cache.get(KEY, now)?.role, { ok: true, user_id: 42, role: 400 });
  now += 10_000;
  assert.equal(cache.get(KEY, now), null, "the role rode the verification's window, not its own");
  cache.update(KEY, { role: { ok: true, user_id: 1, role: 400 } }, now);
  assert.equal(cache.get(KEY, now), null, "update never resurrects an expired entry");
});

test("evict drops one credential; evictSeat drops only that seat's entries", () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set("k1", { seat: "ECHO", via: "oauth" }, 60_000, 0);
  cache.set("k2", { seat: "ECHO", via: "sbt-bearer" }, 60_000, 0);
  cache.set("k3", { seat: "WISE", via: "oauth" }, 60_000, 0);
  cache.evict("k1");
  assert.equal(cache.get("k1", 0), null);
  assert.equal(cache.get("k2", 0)?.seat, "ECHO");
  cache.evictSeat("ECHO");
  assert.deepEqual(cache.pending(), { WISE: 1 });
});

test("the cache is bounded:  past the cap the oldest entries go first", () => {
  const cache = new IdentityCache({ max: 2, now: () => 0 });
  cache.set("k1", { seat: "A", via: "oauth" }, 60_000, 0);
  cache.set("k2", { seat: "B", via: "oauth" }, 60_000, 0);
  cache.set("k3", { seat: "C", via: "oauth" }, 60_000, 0);
  assert.equal(cache.get("k1", 0), null);
  assert.deepEqual(cache.pending(), { B: 1, C: 1 });
});

function countingGate(over = {}) {
  const gate = {
    reserves: [],
    roleGets: 0,
    audits: [],
    async reserve({ kind }) {
      gate.reserves.push(kind);
      return over.reserve ? over.reserve(kind) : { ok: true, waitMs: 0 };
    },
    async roleGet() {
      gate.roleGets += 1;
      return over.roleGet ? over.roleGet() : null;
    },
    async roleSet() {},
    async auditCall(row) { gate.audits.push(row); },
  };
  return gate;
}

function toolsWith({ gate, cache = null, key = null, entry = null }) {
  return new HostedTools({
    seat: "ECHO",
    scopes: ["zulip:read", "zulip:write"],
    config: { botEmails: {}, ownerUserId: 999 },
    zulip: {},
    gate,
    identityCache: cache,
    identityKey: key,
    identityEntry: entry,
  });
}

test("a cached read takes no reserve slot, and a cached whoami takes no roleGet either", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "sbt-bearer", role: { ok: true, user_id: 42, role: 400 } }, 60_000, 0);
  const gate = countingGate();
  const tools = toolsWith({ gate, cache, key: KEY, entry: cache.get(KEY, 0) });
  await tools.reserve("read");
  await tools.reserve("linear_read");
  assert.deepEqual(gate.reserves, [], "reads inside the window reserve nothing");
  const result = await tools.call("whoami", {});
  assert.notEqual(result.isError, true);
  assert.deepEqual(gate.reserves, [], "whoami's read reserve is skipped too");
  assert.equal(gate.roleGets, 0, "the role came from the cached verdict");
  assert.equal(result.structuredContent?.user_id, 42);
  assert.equal(result.structuredContent?.role, 400);
  assert.equal(gate.audits.length, 0, "a successful read writes no audit row (owner 2026-11-06)");
});

test("bearerCacheMs:  the hour window, never past the bearer's own expiry, and nothing without one", () => {
  const config = { gateIdentityCacheMs: 3_600_000 };
  const now = 1_000_000;
  assert.equal(bearerCacheMs(config, { expires_at: now + 90 * 86_400_000 }, now), 3_600_000, "a long-lived bearer gets the full hour");
  assert.equal(bearerCacheMs(config, { expires_at: now + 600_000 }, now), 600_000, "a bearer ten minutes from expiry gets ten minutes");
  assert.equal(bearerCacheMs(config, { expires_at: now - 1 }, now), 0);
  assert.equal(bearerCacheMs(config, {}, now), 0, "no expiry, no cache");
  assert.equal(bearerCacheMs({ gateIdentityCacheMs: 0 }, { expires_at: now + 600_000 }, now), 0, "the off switch still holds");
});

test("a cached write keeps its reserve exactly as an uncached one does", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "oauth" }, 60_000, 0);
  const gate = countingGate();
  const tools = toolsWith({ gate, cache, key: KEY, entry: cache.get(KEY, 0) });
  await tools.reserve("write");
  await tools.reserve("linear_write");
  await tools.reserve("react");
  assert.deepEqual(gate.reserves, ["write", "linear_write", "react"]);
});

test("the first (uncached) call verifies the role live and records it on the entry", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "oauth" }, 60_000, 0);
  const gate = countingGate({ roleGet: () => ({ ok: true, user_id: 42, role: 400, owner: 999 }) });
  const tools = toolsWith({ gate, cache, key: KEY, entry: null });
  await tools.ensureSeat();
  assert.equal(gate.roleGets, 1);
  assert.deepEqual(tools.me, { user_id: 42, role: 400 });
  assert.deepEqual(cache.get(KEY, 0)?.role, { ok: true, user_id: 42, role: 400 }, "later calls in the window skip roleGet");
});

test("a refused role verdict on the live path evicts the cached verification", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "oauth" }, 60_000, 0);
  const gate = countingGate({ roleGet: () => ({ ok: false, user_id: null, role: 200, reason: "the bot is not a member", owner: 999 }) });
  const tools = toolsWith({ gate, cache, key: KEY, entry: cache.get(KEY, 0) });
  await assert.rejects(() => tools.ensureSeat(), /not a member/);
  assert.equal(cache.get(KEY, 0), null, "the next call re-verifies instead of coasting");
});

test("a reserve refusal (a paused seat's write) evicts the cached verification", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "oauth" }, 60_000, 0);
  const gate = countingGate({ reserve: () => ({ ok: false, code: "paused" }) });
  const tools = toolsWith({ gate, cache, key: KEY, entry: cache.get(KEY, 0) });
  await assert.rejects(() => tools.reserve("write"), (error) => error.code === "paused");
  assert.equal(cache.get(KEY, 0), null);
});

test("an entry naming another seat is no entry at all", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "WISE", via: "oauth" }, 60_000, 0);
  const gate = countingGate();
  const tools = toolsWith({ gate, cache, key: KEY, entry: cache.get(KEY, 0) });
  await tools.reserve("read");
  assert.deepEqual(gate.reserves, ["read"], "a foreign seat's verification buys this seat nothing");
});

test("a downstream 429 evicts a cached identity, so the next read sees the cooldown it just stored", async () => {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "sbt-bearer", role: { ok: true, user_id: 42, role: 400 } }, 60_000, 0);
  const noted = [];
  const gate = countingGate();
  gate.noteRateLimited = async (entry) => noted.push(entry);
  const tools = toolsWith({ gate, cache, key: KEY, entry: cache.get(KEY, 0) });
  tools.tool_whoami = async () => { throw new ApiError("slow down", { status: 429, data: { retry_after: 7 } }); };
  const result = await tools.call("whoami", {});
  assert.equal(result.isError, true);
  assert.equal(noted.length, 1);
  assert.equal(cache.get(KEY, 0), null);
  await tools.reserve("read");
  assert.deepEqual(gate.reserves, ["read"], "the next read reserves, and reserve reads the cooldown");
});
