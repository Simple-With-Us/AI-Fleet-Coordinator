// DO burn reduction (R4):  the isolate verified-identity cache
// (src/identity-cache.js) and the HostedTools behavior it drives.  The
// cache is pinned on its window discipline (only positives stored,
// expiry is a miss, enrichment never extends the window, eviction by key
// and by seat);  HostedTools is pinned on the cached path costing no
// gate RPCs for a read or a whoami, a write keeping its reserve, and
// refusals evicting.

import test from "node:test";
import assert from "node:assert/strict";
import { IdentityCache } from "../src/identity-cache.js";
import { HostedTools } from "../src/hosted-tools.js";

const KEY = "a".repeat(64);

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
  assert.equal(gate.audits.length, 1, "the call still writes its audit row");
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
