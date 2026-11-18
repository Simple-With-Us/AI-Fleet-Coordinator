// AFC-844:  the relay check routes' refusal rows.  The per-isolate coalescer
// (src/diagnostic-coalesce.js) bounds how often one cause writes, and the real
// diagnostics writer and reader (seat-state.js recordDiagnostic / diagnosticTail,
// worker-diagnostics.js diagnosticRow) must accept the new kinds, the `:` detail
// reasons and a carried count, or index.js would swallow the write and the rows
// would silently never appear.

import test from "node:test";
import assert from "node:assert/strict";
import { DiagnosticCoalescer, DIAGNOSTIC_COALESCE_WINDOW_MS, DIAGNOSTIC_MAX_COUNT } from "../src/diagnostic-coalesce.js";
import { diagnosticRow } from "../src/worker-diagnostics.js";
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

test("a write that did not land gives its occurrences back, and the next one writes at once", () => {
  let now = NOW;
  const c = new DiagnosticCoalescer({ now: () => now });
  const entry = { kind: "gr_bearer", reason: "unavailable" };
  assert.equal(c.take(entry), 1);
  assert.equal(c.take(entry), 0);
  // The first write was dropped (budget spent or log down):  it hands its one back.
  c.giveBack(entry, 1);
  now += 1;
  // Not held for the window:  this write carries the given-back one, the pending one and itself.
  assert.equal(c.take(entry), 3);
  assert.equal(c.take(entry), 0);
  // A give-back for a key the coalescer no longer remembers still counts.
  const fresh = new DiagnosticCoalescer({ now: () => now });
  fresh.giveBack({ kind: "gr_assert", reason: "replayed", seat: "CODEX" }, 2);
  assert.equal(fresh.take({ kind: "gr_assert", reason: "replayed", seat: "CODEX" }), 3);
});

test("a carried count never passes the writer's ceiling, so a flood still writes a row", () => {
  let now = NOW;
  const c = new DiagnosticCoalescer({ now: () => now });
  const entry = { kind: "gr_bearer", reason: "bad_signature" };
  assert.equal(c.take(entry), 1);
  c.giveBack(entry, DIAGNOSTIC_MAX_COUNT + 5);
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
