// Owner ruling 2026-11-06:  "successful reads should leave the audit log."
// A read (HOSTED_READ_TOOLS) that ends ok writes no SeatGate call row and so
// never reaches the isolate audit buffer, arms its timer or flushes it.  A
// read that fails still audits, every write audits, and the Workers Logs
// "tool" line stays for every call.

import test from "node:test";
import assert from "node:assert/strict";
import { IdentityCache } from "../src/identity-cache.js";
import { HostedTools, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS, auditsCall } from "../src/hosted-tools.js";
import { AuditBatcher } from "../src/audit-batch.js";

const KEY = "b".repeat(64);

function spyGate() {
  const gate = {
    audits: [],
    batches: 0,
    async reserve() { return { ok: true, waitMs: 0 }; },
    async roleGet() { return null; },
    async roleSet() {},
    async auditCall(row) { gate.audits.push(row); },
    async auditCallBatch(entries) { gate.batches += 1; gate.audits.push(...entries.map((e) => e.row)); },
  };
  return gate;
}

// A whoami served from a cached identity with a role verdict needs no Zulip.
function cachedTools({ gate, callerGate = gate, audit = null, logs = [], zulip = {} }) {
  const cache = new IdentityCache({ now: () => 0 });
  cache.set(KEY, { seat: "ECHO", via: "sbt-bearer", role: { ok: true, user_id: 42, role: 400 } }, 60_000, 0);
  return new HostedTools({
    seat: "ECHO",
    callerSeat: callerGate === gate ? "ECHO" : "CURSOR",
    callerGate,
    scopes: ["zulip:read", "zulip:write"],
    config: { botEmails: {}, ownerUserId: 999 },
    zulip,
    gate,
    audit,
    identityCache: cache,
    identityKey: KEY,
    identityEntry: cache.get(KEY, 0),
    log: (row) => logs.push(row),
  });
}

test("auditsCall:  only an ok read is quiet;  read errors, unknown names and every write audit", () => {
  for (const name of ["whoami", "zulip_read_topic", "identities", "catalog", "guide", "recall_search", "gr_v1_pr_state"]) {
    assert.ok(HOSTED_READ_TOOLS.has(name), name);
    assert.equal(auditsCall(name, "ok"), false, `${name} ok`);
    assert.equal(auditsCall(name, "error"), true, `${name} error`);
  }
  for (const name of HOSTED_WRITE_TOOLS) {
    assert.equal(auditsCall(name, "ok"), true, `${name} ok`);
    assert.equal(auditsCall(name, "error"), true, `${name} error`);
  }
  assert.equal(auditsCall("unknown_tool", "error"), true);
  assert.equal(auditsCall("not_a_tool", "ok"), true, "an unclassified name counts as a write");
});

test("an ok read writes no row direct to the gate, and still logs its tool line", async () => {
  const gate = spyGate();
  const logs = [];
  const result = await cachedTools({ gate, logs }).call("whoami", {});
  assert.notEqual(result.isError, true);
  assert.deepEqual(gate.audits, []);
  assert.deepEqual(logs.filter((l) => l.event === "tool").map((l) => [l.tool, l.outcome]), [["whoami", "ok"]]);
});

test("an ok read never reaches the isolate audit buffer, so it arms no timer and flushes nothing", async () => {
  const gate = spyGate();
  const scheduled = [];
  const batcher = new AuditBatcher({ schedule: (fn, ms) => { scheduled.push(ms); return 1; }, cancel: () => {} });
  const sink = (key, g, row) => batcher.append(key, g, row, null);
  for (let i = 0; i < 60; i++) await cachedTools({ gate, audit: sink }).call("whoami", {});
  assert.deepEqual(batcher.pending(), {}, "no buffer was even created");
  assert.deepEqual(scheduled, [], "no trailing timer");
  assert.equal(gate.batches, 0, "no flush RPC");
});

test("a delegated ok read writes nothing to either gate", async () => {
  const gate = spyGate();
  const callerGate = spyGate();
  const result = await cachedTools({ gate, callerGate }).call("whoami", {});
  assert.notEqual(result.isError, true);
  assert.deepEqual([gate.audits.length, callerGate.audits.length], [0, 0]);
});

test("a read that fails still audits, through the buffer it flushes at once", async () => {
  const gate = spyGate();
  const batcher = new AuditBatcher({ schedule: () => 1, cancel: () => {} });
  const sink = (key, g, row) => batcher.append(key, g, row, null);
  // No Zulip key installed and no cached identity:  whoami refuses.
  const tools = new HostedTools({ seat: "ECHO", scopes: ["zulip:read"], config: { botEmails: {}, ownerUserId: 999 }, zulip: null, gate, audit: sink, log: () => {} });
  const result = await tools.call("whoami", {});
  assert.equal(result.isError, true);
  assert.equal(gate.batches, 1, "an error row flushes immediately");
  assert.deepEqual(gate.audits.map((r) => [r.tool, r.outcome]), [["whoami", "error"]]);
});

test("a delegated failed read audits on both gates", async () => {
  const gate = spyGate();
  const callerGate = spyGate();
  const tools = new HostedTools({ seat: "ECHO", callerSeat: "CURSOR", callerGate, scopes: ["zulip:read"], config: { botEmails: {}, ownerUserId: 999 }, zulip: null, gate, log: () => {} });
  assert.equal((await tools.call("whoami", {})).isError, true);
  assert.deepEqual([gate.audits.length, callerGate.audits.length], [1, 1]);
});
