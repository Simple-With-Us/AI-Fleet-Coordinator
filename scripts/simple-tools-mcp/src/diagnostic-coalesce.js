// Per-isolate coalescing for high-rate diagnostics rows (AFC-844).
//
// The relay's two check routes (/internal/gr-bearer-verify and
// /internal/gr-assert-verify) can refuse many calls in a row:  a relay whose
// verify key is out of step refuses every check, and anyone can send junk
// signatures.  Each diagnostics write costs one Durable Object transaction and
// one unit of the isolate's shared per-minute diagnosticRow budget
// (src/invocation.js), which wake, token, mcp and provider rows also spend.
// So the first occurrence of a kind+reason+seat writes at once, and later
// ones in the same isolate within the window only add to a pending count.
// The next write of that key after the window carries the pending count with
// it.  The log therefore shows every cause within a minute of its first
// occurrence, while one cause writes at most once a minute per isolate.
//
// Counts are a lower bound:  pending occurrences of a key that never recurs
// in this isolate are lost when the isolate ends.

export const DIAGNOSTIC_COALESCE_WINDOW_MS = 60_000;
const MAX_KEYS = 256;
// The writer's own ceiling for one write's count (src/seat-state.js recordDiagnosticInner):
// a count above it would be refused and the whole row lost, so a flood writes the ceiling.
export const DIAGNOSTIC_MAX_COUNT = 1_000_000;

export class DiagnosticCoalescer {
  constructor({ windowMs = DIAGNOSTIC_COALESCE_WINDOW_MS, now = () => Date.now(), maxKeys = MAX_KEYS } = {}) {
    this.windowMs = windowMs;
    this.now = now;
    this.maxKeys = maxKeys;
    this.keys = new Map();
  }

  /**
   * Account for one occurrence.  Returns the count to write now (the
   * occurrence plus any pending ones), or 0 when it was added to the pending
   * count instead.
   */
  take({ kind, reason, seat = null }) {
    const key = `${kind}\0${reason}\0${seat ?? ""}`;
    const at = this.now();
    const state = this.keys.get(key);
    if (state && at - state.writtenAt < this.windowMs) {
      state.pending += 1;
      return 0;
    }
    const count = Math.min(DIAGNOSTIC_MAX_COUNT, 1 + (state?.pending ?? 0));
    this.keys.delete(key);
    this.keys.set(key, { writtenAt: at, pending: 0 });
    // Oldest key first:  a Map keeps insertion order, and a written key moves to the end.
    while (this.keys.size > this.maxKeys) this.keys.delete(this.keys.keys().next().value);
    return count;
  }

  /**
   * A write that `take` allowed did not land (the isolate's diagnostics budget was
   * spent, or the log failed).  Its occurrences go back to pending and the window
   * reopens, so the next occurrence writes at once and carries them.
   */
  giveBack({ kind, reason, seat = null }, count) {
    const key = `${kind}\0${reason}\0${seat ?? ""}`;
    const state = this.keys.get(key) ?? { writtenAt: -Infinity, pending: 0 };
    state.writtenAt = -Infinity;
    state.pending = Math.min(DIAGNOSTIC_MAX_COUNT, state.pending + count);
    this.keys.set(key, state);
  }
}
