// AFC-844:  the relay check routes' refusal rows.  The per-isolate coalescer
// (src/diagnostic-coalesce.js) bounds how often one cause writes, and the real
// diagnostics writer and reader (seat-state.js recordDiagnostic / diagnosticTail,
// worker-diagnostics.js diagnosticRow) must accept the new kinds, the `:` detail
// reasons and a carried count, or index.js would swallow the write and the rows
// would silently never appear.

import test from "node:test";
import assert from "node:assert/strict";
import { DiagnosticCoalescer, DIAGNOSTIC_COALESCE_WINDOW_MS, DIAGNOSTIC_MAX_COUNT } from "../src/diagnostic-coalesce.js";
import { diagnosticRow, WORKER_DIAGNOSTICS_STORE_ROWS } from "../src/worker-diagnostics.js";
import * as S from "../src/seat-state.js";

const NOW = Date.parse("2026-11-18T20:00:00Z");

test("the first occurrence writes at once;  later ones in the window only count", () => {
  let now = NOW;
  const c = new DiagnosticCoalescer({ now: () => now });
  const entry = { kind: "gr_bearer", reason: "bad_signature", seat: null };
  assert.equal(c.take(entry), 1);
  assert.equal(c.take(entry), 0);
  assert.equal(c.take(entry), 0);
  now += DIAGNOSTIC_COALESCE_WINDOW_MS - 1;
  assert.equal(c.take(entry), 0);
  // Past the window the next write carries the three it absorbed.
  now += 1;
  assert.equal(c.take(entry), 4);
  assert.equal(c.take(entry), 0);
});

test("each kind, reason and seat coalesces on its own", () => {
  const c = new DiagnosticCoalescer({ now: () => NOW });
  assert.equal(c.take({ kind: "gr_bearer", reason: "bad_signature" }), 1);
  assert.equal(c.take({ kind: "gr_assert", reason: "bad_signature" }), 1);
  assert.equal(c.take({ kind: "gr_assert", reason: "assertion:bad_signature" }), 1);
  assert.equal(c.take({ kind: "gr_bearer", reason: "paused", seat: "ECHO" }), 1);
  assert.equal(c.take({ kind: "gr_bearer", reason: "paused", seat: "WISE" }), 1);
  assert.equal(c.take({ kind: "gr_bearer", reason: "paused", seat: "ECHO" }), 0);
});

test("a write that did not land gives its occurrences back, without reopening the window", () => {
  let now = NOW;
  const c = new DiagnosticCoalescer({ now: () => now });
  const entry = { kind: "gr_bearer", reason: "unavailable" };
  assert.equal(c.take(entry), 1);
  // The first write was dropped (budget spent or log down):  it hands its one back.
  c.giveBack(entry, 1);
  // The window stays closed, so an outage does not turn every refusal into a write.
  assert.equal(c.take(entry), 0);
  assert.equal(c.take(entry), 0);
  now += DIAGNOSTIC_COALESCE_WINDOW_MS;
  // The next write carries the given-back one, the two pending ones and itself.
  assert.equal(c.take(entry), 4);
  // A give-back for a key the coalescer no longer remembers still counts, at the next window.
  const fresh = new DiagnosticCoalescer({ now: () => now });
  fresh.giveBack({ kind: "gr_assert", reason: "replayed", seat: "CODEX" }, 2);
  assert.equal(fresh.take({ kind: "gr_assert", reason: "replayed", seat: "CODEX" }), 0);
  now += DIAGNOSTIC_COALESCE_WINDOW_MS;
  assert.equal(fresh.take({ kind: "gr_assert", reason: "replayed", seat: "CODEX" }), 4);
});

test("a carried count never passes the writer's ceiling, so a flood still writes a row", () => {
  let now = NOW;
  const c = new DiagnosticCoalescer({ now: () => now });
  const entry = { kind: "gr_bearer", reason: "bad_signature" };
  assert.equal(c.take(entry), 1);
  c.giveBack(entry, DIAGNOSTIC_MAX_COUNT + 5);
  // The window stays closed;  later occurrences still count, but never past the ceiling.
  for (let i = 0; i < 3; i += 1) assert.equal(c.take(entry), 0);
  now += DIAGNOSTIC_COALESCE_WINDOW_MS;
  assert.equal(c.take(entry), DIAGNOSTIC_MAX_COUNT);
  for (let i = 0; i < 3; i += 1) c.take(entry);
  now += DIAGNOSTIC_COALESCE_WINDOW_MS;
  assert.equal(c.take(entry), 4);
});

test("the coalescer's memory is bounded, oldest key first", () => {
  let now = NOW;
  const c = new DiagnosticCoalescer({ now: () => now, maxKeys: 2 });
  c.take({ kind: "gr_bearer", reason: "a" });
  c.take({ kind: "gr_bearer", reason: "b" });
  c.take({ kind: "gr_bearer", reason: "c" });
  // "a" was dropped, so it writes again at once;  "c" is still in its window.
  assert.equal(c.take({ kind: "gr_bearer", reason: "a" }), 1);
  assert.equal(c.take({ kind: "gr_bearer", reason: "c" }), 0);
});

test("the real writer stores the relay check rows, their `:` reasons and a carried count", async () => {
  const store = S.memoryStore();
  await S.recordDiagnostic(store, { kind: "gr_assert", reason: "grant_invalid:stale_epoch", seat: "CODEX", status: 200 }, NOW);
  await S.recordDiagnostic(store, { kind: "gr_assert", reason: "assertion:bad_signature", seat: null, status: 200 }, NOW);
  await S.recordDiagnostic(store, { kind: "gr_bearer", reason: "bad_signature", seat: null, status: 401, count: 5 }, NOW);
  await S.recordDiagnostic(store, { kind: "gr_bearer", reason: "bad_signature", seat: null, status: 401, count: 2 }, NOW + 1);
  const rows = await S.diagnosticTail(store, 10, NOW + 2);
  const byReason = Object.fromEntries(rows.map((r) => [`${r.kind}/${r.reason}`, r]));
  assert.equal(byReason["gr_bearer/bad_signature"].count, 7);
  assert.equal(byReason["gr_assert/grant_invalid:stale_epoch"].seat, "CODEX");
  assert.equal(byReason["gr_assert/assertion:bad_signature"].count, 1);
  // And worker_diagnostics shows them as they are.
  const shown = rows.map(diagnosticRow);
  assert.ok(shown.some((r) => r.kind === "gr_assert" && r.reason === "grant_invalid:stale_epoch" && r.seat === "CODEX"));
});

test("the writer refuses a count that is not a sane positive integer", async () => {
  const store = S.memoryStore();
  for (const count of [0, -1, 1.5, "3", 2_000_000]) {
    await assert.rejects(S.recordDiagnostic(store, { kind: "gr_bearer", reason: "mismatch", count }, NOW), /invalid diagnostic count/);
  }
});

test("relay check rows keep their own store, so a busy relay cannot push wake rows out", async () => {
  const store = S.memoryStore();
  for (let i = 0; i < 100; i += 1) await S.recordDiagnostic(store, { kind: "wake", reason: `wake_${i}` }, NOW + i);
  // Far more distinct relay causes than its own store keeps (Kody, PR 2331).
  for (let i = 0; i < 300; i += 1) await S.recordDiagnostic(store, { kind: i % 2 ? "gr_bearer" : "gr_assert", reason: `cause_${i}` }, NOW + 1_000 + i);
  const rows = await S.diagnosticTail(store, S.DIAGNOSTIC_TOTAL_CAP, NOW + 2_000);
  const wake = rows.filter((r) => r.kind === "wake");
  const relay = rows.filter((r) => r.kind !== "wake");
  // Every wake row survived;  the relay kept its newest 40.
  assert.equal(wake.length, 100);
  assert.equal(relay.length, S.DIAGNOSTIC_TOTAL_CAP - 100);
  assert.equal(relay[0].reason, "cause_299");
  // Newest first within every kind (the kinds themselves take turns in the tail,
  // so no kind can crowd the others out - Kody, PR 2331 review round 3).
  const lastSeenTs = new Map();
  for (const row of rows) {
    const last = lastSeenTs.get(row.kind);
    if (last !== undefined) assert.ok(last >= row.ts, `${row.kind} out of order`);
    lastSeenTs.set(row.kind, row.ts);
  }
  // And a wake row written later still lands ahead of the older relay rows.
  await S.recordDiagnostic(store, { kind: "wake", reason: "wake_late" }, NOW + 3_000);
  const [newest] = await S.diagnosticTail(store, 1, NOW + 3_001);
  assert.equal(newest.reason, "wake_late");
});

test("worker_diagnostics reads the whole log:  its one read equals both stores' caps", () => {
  assert.equal(WORKER_DIAGNOSTICS_STORE_ROWS, S.DIAGNOSTIC_TOTAL_CAP);
});
