// The worker isolate's per-call audit buffer (R2, DO burn reduction).
//
// HostedTools writes one audit row per tool call (spec 3.9), and the row is
// observability, not a security control:  no call is authorized by the audit
// path, and HostedTools already treats a failed audit write as non-fatal
// (its safely() wrapper).  Writing each row as its own Durable Object RPC
// made the audit log a full third of the gate's per-call cost, so rows are
// buffered here, in the isolate, per gate, and flushed to the gate in one
// auditCallBatch RPC - the same write-throttling discipline the codebase
// already keeps for gbBearerMismatch increments in seat-state.js.
//
// HONEST ACCOUNTING (the trade, stated in full):  a buffered successful-call
// row is visible to callTail readers only after its batch flushes - seconds
// late, never immediately.  Successful-call rows still sitting in the
// buffer when the isolate is evicted are LOST:  the loss window is bounded
// by the flush triggers (AUDIT_FLUSH_ROWS rows or AUDIT_FLUSH_MS
// milliseconds per gate, whichever comes first - the age bound is backed
// by a trailing timer per gate, not left to the next append, and a failed
// flush retries on that same cadence) and by AUDIT_HARD_CAP, the most
// rows one gate's buffer can hold while flushes keep failing;  past the
// cap the oldest unflushed rows are dropped and the drop is logged.  A
// failed flush keeps its rows, merged ahead of newer rows so order is
// preserved.  Nothing here delays, authorizes or refuses a tool call.
//
// Error rows do NOT wait:  an error append flushes its gate's buffer
// immediately, carrying the rows buffered ahead of it in order, so the
// admin error view and every errorTail reader stay exactly as current as
// they were when each row wrote through on its own.  An error appended
// while a flush is already in flight queues a follow-up flush behind it
// and the append does not resolve until that follow-up has run, so the
// write-through holds then too.  Errors are rare, so the write-through
// costs almost nothing;  the loss window above therefore covers
// successful-call rows only.

export const AUDIT_FLUSH_ROWS = 50;
export const AUDIT_FLUSH_MS = 5000;
export const AUDIT_HARD_CAP = 500;

export class AuditBatcher {
  constructor({ flushRows = AUDIT_FLUSH_ROWS, flushMs = AUDIT_FLUSH_MS, hardCap = AUDIT_HARD_CAP, now = Date.now, log = (row) => console.log(JSON.stringify(row)), schedule = (fn, ms) => { const t = setTimeout(fn, ms); t?.unref?.(); return t; }, cancel = (handle) => clearTimeout(handle) } = {}) {
    this.flushRows = flushRows;
    this.flushMs = flushMs;
    this.hardCap = hardCap;
    this.now = now;
    this.log = log;
    this.schedule = schedule;
    this.cancel = cancel;
    // key (the gate's Durable Object name) -> { gate, rows, firstAt, flushing, queued, chain, timer }
    this.buffers = new Map();
  }

  /** Arm the trailing timer that makes the age bound real for an idle gate. */
  _armTimer(key, buf, delayMs = null) {
    if (buf.timer !== null || buf.rows.length === 0) return;
    const delay = delayMs ?? Math.max(0, buf.firstAt + this.flushMs - this.now());
    buf.timer = this.schedule(() => {
      buf.timer = null;
      void this.flush(key);
    }, delay);
  }

  _disarmTimer(buf) {
    if (buf.timer !== null) {
      this.cancel(buf.timer);
      buf.timer = null;
    }
  }

  /**
   * Buffer one row for `gate` under `key`.  Resolves immediately unless
   * this append trips a flush trigger, in which case it resolves after the
   * batch RPC settles (the caller already awaits its audit write before
   * answering, so the occasional batched RPC lands on the same await the
   * per-row RPC used to).  Never throws:  a flush failure keeps the rows.
   */
  async append(key, gate, row) {
    let buf = this.buffers.get(key);
    if (!buf) {
      buf = { gate, rows: [], firstAt: 0, flushing: false, queued: false, chain: null, timer: null };
      this.buffers.set(key, buf);
    }
    buf.gate = gate;
    const at = this.now();
    if (buf.rows.length === 0) buf.firstAt = at;
    buf.rows.push({ row, at });
    if (row?.outcome === "error" || buf.rows.length >= this.flushRows || at - buf.firstAt >= this.flushMs) {
      await this.flush(key);
    } else {
      this._armTimer(key, buf);
    }
  }

  /**
   * Flush one gate's buffered rows, if any.  Safe to call at any time:
   * while a flush is in flight, calling again queues a follow-up behind
   * it and returns the in-flight chain, so an awaiter (an error append
   * above all) resolves only after the follow-up has run.
   */
  async flush(key) {
    const buf = this.buffers.get(key);
    if (!buf || buf.rows.length === 0) return;
    if (buf.flushing) {
      buf.queued = true;
      return buf.chain;
    }
    buf.flushing = true;
    const run = (async () => {
      try {
        // Loop while full batches keep accumulating behind an in-flight
        // flush, and once more whenever a follow-up was queued during
        // one;  a partial tail otherwise waits for its own trigger.
        while (buf.rows.length > 0) {
          // Synchronous hand-off:  the batch leaves the live buffer BEFORE
          // the first await, so a row appended while the RPC is in flight
          // lands in the fresh buffer and survives both outcomes.
          const entries = buf.rows;
          buf.rows = [];
          buf.firstAt = 0;
          try {
            await buf.gate.auditCallBatch(entries);
          } catch {
            // The batch did not land:  merge it back AHEAD of anything
            // appended meanwhile (order preserved), bound the retained tail
            // at the hard cap with the drop logged, and stop - the trailing
            // timer retries on the flush cadence.
            buf.rows = entries.concat(buf.rows);
            buf.firstAt = entries[0]?.at ?? this.now();
            if (buf.rows.length > this.hardCap) {
              const dropped = buf.rows.length - this.hardCap;
              buf.rows.splice(0, dropped);
              buf.firstAt = buf.rows[0]?.at ?? 0;
              this.log({ event: "audit_batch_overflow", key, dropped });
            }
            this.log({ event: "audit_batch_flush_failed", key, rows: entries.length });
            this._armTimer(key, buf, this.flushMs);
            return;
          }
          if (buf.queued) {
            buf.queued = false;
            continue;
          }
          if (buf.rows.length < this.flushRows) break;
        }
        if (buf.rows.length === 0) this._disarmTimer(buf);
        else this._armTimer(key, buf);
      } finally {
        buf.flushing = false;
        if (buf.chain === run) buf.chain = null;
      }
    })();
    buf.chain = run;
    return run;
  }

  /** Flush every gate's buffer (tests and shutdown seams). */
  async flushAll() {
    for (const key of [...this.buffers.keys()]) await this.flush(key);
  }

  /** Buffered row counts by gate key, for tests and diagnostics. */
  pending() {
    const out = {};
    for (const [key, buf] of this.buffers) out[key] = buf.rows.length;
    return out;
  }
}
