// DO burn reduction (R2):  the isolate audit buffer (src/audit-batch.js)
// and the HostedTools audit seam it plugs into.  The batcher is pinned on
// its flush triggers, its failure discipline (rows kept, order preserved,
// hard cap with a logged drop) and its per-gate isolation;  the seam is
// pinned on key selection and on the no-sink default staying direct.

import test from "node:test";
import assert from "node:assert/strict";
import { AuditBatcher } from "../src/audit-batch.js";
import { HostedTools } from "../src/hosted-tools.js";

function fakeGate() {
  const gate = {
    batches: [],
    failNext: 0,
    async auditCallBatch(entries) {
      if (gate.failNext > 0) {
        gate.failNext -= 1;
        throw new Error("gate unavailable");
      }
      gate.batches.push(entries.map((e) => ({ ...e })));
      return { written: entries.length };
    },
  };
  return gate;
}

test("rows below both triggers make no RPC; the row trigger flushes one batch in order", async () => {
  let now = 1000;
  const gate = fakeGate();
  const batcher = new AuditBatcher({ flushRows: 3, flushMs: 5000, now: () => now, log: () => {} });
  await batcher.append("ECHO", gate, { tool: "a" });
  await batcher.append("ECHO", gate, { tool: "b" });
  assert.deepEqual(gate.batches, []);
  assert.deepEqual(batcher.pending(), { ECHO: 2 });
  await batcher.append("ECHO", gate, { tool: "c" });
  assert.equal(gate.batches.length, 1);
  assert.deepEqual(gate.batches[0].map((e) => e.row.tool), ["a", "b", "c"]);
  assert.deepEqual(gate.batches[0].map((e) => e.at), [1000, 1000, 1000]);
  assert.deepEqual(batcher.pending(), { ECHO: 0 });
});

test("an error row flushes immediately, carrying the rows buffered ahead of it in order", async () => {
  const gate = fakeGate();
  const batcher = new AuditBatcher({ flushRows: 50, flushMs: 5000, now: () => 0, log: () => {} });
  await batcher.append("ECHO", gate, { tool: "a", outcome: "ok" });
  await batcher.append("ECHO", gate, { tool: "b", outcome: "ok" });
  assert.deepEqual(gate.batches, []);
  await batcher.append("ECHO", gate, { tool: "boom", outcome: "error", error_code: "not_authorized" });
  assert.equal(gate.batches.length, 1);
  assert.deepEqual(gate.batches[0].map((e) => e.row.tool), ["a", "b", "boom"]);
  assert.deepEqual(batcher.pending(), { ECHO: 0 });
});

test("the age trigger flushes a partial batch on the next append", async () => {
  let now = 1000;
  const gate = fakeGate();
  const batcher = new AuditBatcher({ flushRows: 50, flushMs: 5000, now: () => now, log: () => {} });
  await batcher.append("ECHO", gate, { tool: "a" });
  now += 4999;
  await batcher.append("ECHO", gate, { tool: "b" });
  assert.deepEqual(gate.batches, []);
  now += 1;
  await batcher.append("ECHO", gate, { tool: "c" });
  assert.equal(gate.batches.length, 1);
  assert.deepEqual(gate.batches[0].map((e) => e.row.tool), ["a", "b", "c"]);
});

test("buffers are per gate key: one gate's flush never carries another's rows", async () => {
  const gateA = fakeGate();
  const gateB = fakeGate();
  const batcher = new AuditBatcher({ flushRows: 2, flushMs: 5000, now: () => 0, log: () => {} });
  await batcher.append("ECHO", gateA, { tool: "a" });
  await batcher.append("WISE", gateB, { tool: "b" });
  await batcher.append("ECHO", gateA, { tool: "c" });
  assert.deepEqual(gateA.batches.map((b) => b.map((e) => e.row.tool)), [["a", "c"]]);
  assert.deepEqual(gateB.batches, []);
  assert.deepEqual(batcher.pending(), { ECHO: 0, WISE: 1 });
});

test("a failed flush keeps its rows, in order, ahead of newer rows", async () => {
  const gate = fakeGate();
  gate.failNext = 1;
  const logs = [];
  const batcher = new AuditBatcher({ flushRows: 2, flushMs: 5000, now: () => 0, log: (r) => logs.push(r) });
  await batcher.append("ECHO", gate, { tool: "a" });
  await batcher.append("ECHO", gate, { tool: "b" }); // trips the flush, which fails
  assert.deepEqual(gate.batches, []);
  assert.deepEqual(batcher.pending(), { ECHO: 2 });
  assert.ok(logs.some((r) => r.event === "audit_batch_flush_failed"));
  await batcher.append("ECHO", gate, { tool: "c" }); // trips again; now it lands
  assert.deepEqual(gate.batches.map((b) => b.map((e) => e.row.tool)), [["a", "b", "c"]]);
});

test("the hard cap drops the oldest unflushed rows and logs the drop", async () => {
  const gate = fakeGate();
  gate.failNext = 99;
  const logs = [];
  const batcher = new AuditBatcher({ flushRows: 2, flushMs: 60_000, hardCap: 3, now: () => 0, log: (r) => logs.push(r) });
  for (const tool of ["a", "b", "c", "d", "e"]) await batcher.append("ECHO", gate, { tool });
  assert.deepEqual(batcher.pending(), { ECHO: 3 });
  assert.ok(logs.some((r) => r.event === "audit_batch_overflow" && r.dropped >= 1));
  gate.failNext = 0;
  await batcher.append("ECHO", gate, { tool: "f" });
  const landed = gate.batches.flatMap((b) => b.map((e) => e.row.tool));
  assert.deepEqual(landed, ["c", "d", "e", "f"]); // a and b were dropped at the cap, order kept
});

test("a row appended while a flush is in flight lands in the next batch, never lost", async () => {
  const gate = fakeGate();
  let release;
  const held = new Promise((r) => (release = r));
  const inner = gate.auditCallBatch.bind(gate);
  gate.auditCallBatch = async (entries) => {
    await held;
    return inner(entries);
  };
  const batcher = new AuditBatcher({ flushRows: 2, flushMs: 5000, now: () => 0, log: () => {} });
  await batcher.append("ECHO", gate, { tool: "a" });
  const flushing = batcher.append("ECHO", gate, { tool: "b" }); // starts the held flush
  await batcher.append("ECHO", gate, { tool: "c" }); // in flight: buffers fresh
  release();
  await flushing;
  assert.deepEqual(gate.batches.map((b) => b.map((e) => e.row.tool)), [["a", "b"]]);
  assert.deepEqual(batcher.pending(), { ECHO: 1 });
  await batcher.flushAll();
  assert.deepEqual(gate.batches.map((b) => b.map((e) => e.row.tool)), [["a", "b"], ["c"]]);
});

test("HostedTools without a sink writes each row straight to its gate (the standing default)", async () => {
  const calls = [];
  const gate = { auditCall: async (row) => calls.push(["gate", row]) };
  const callerGate = { auditCall: async (row) => calls.push(["caller", row]) };
  const tools = new HostedTools({ seat: "ECHO", callerSeat: "WISE", scopes: [], config: { botEmails: {} }, gate, callerGate });
  await tools.auditRow(gate, { tool: "x" });
  await tools.auditRow(callerGate, { tool: "x" });
  assert.deepEqual(calls.map(([where]) => where), ["gate", "caller"]);
});

test("HostedTools with a sink routes rows by gate: acting seat for its gate, caller seat for the caller gate", async () => {
  const sunk = [];
  const gate = { auditCall: async () => assert.fail("the sink replaces the direct write") };
  const callerGate = { auditCall: async () => assert.fail("the sink replaces the direct write") };
  const tools = new HostedTools({
    seat: "ECHO",
    callerSeat: "WISE",
    scopes: [],
    config: { botEmails: {} },
    gate,
    callerGate,
    audit: (key, g, row) => sunk.push([key, g, row]),
  });
  await tools.auditRow(gate, { tool: "x" });
  await tools.auditRow(callerGate, { tool: "x" });
  assert.deepEqual(sunk.map(([key]) => key), ["ECHO", "WISE"]);
  assert.equal(sunk[0][1], gate);
  assert.equal(sunk[1][1], callerGate);
});
