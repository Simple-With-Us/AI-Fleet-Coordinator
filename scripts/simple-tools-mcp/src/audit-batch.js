// The worker isolate's per-call audit buffer (R2, DO burn reduction).
//
// HostedTools writes one audit row per write and per failed call (spec 3.9;
// a successful read writes none and never reaches append(), owner ruling
// 2026-11-06), so a buffered successful row is always a write's.  The row is
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
// while a flush of the same invocation is in flight queues a follow-up
// flush behind it and the append does not resolve until that follow-up has
// run, so the write-through holds then too.  Errors are rare, so the
// write-through costs almost nothing;  the loss window above therefore
// covers successful-call rows only.
//
// WORKERS REQUEST CONTEXTS (AFC-617).  The buffer outlives any one request,
// but a timer, a Durable Object stub and a promise belong to the request that
// made them:  a timer dies with its response unless waitUntil holds it, a stub
// from another request throws, and awaiting another request's promise hangs if
// that request is cancelled.  So each append names its invocation (`scope`),
// and a flush uses only that invocation's stub, runs under its waitUntil, and
// is awaited only by that invocation.  A second invocation that needs a flush
// runs its own beside the first;  batches land in arrival order at the gate,
// and every row carries its own time.  The Workers limit on waitUntil is 30
// seconds, so AUDIT_FLUSH_MS must stay well under it.

export const AUDIT_FLUSH_ROWS = 50;
export const AUDIT_FLUSH_MS = 5000;
export const AUDIT_HARD_CAP = 500;

export class AuditBatcher {
  constructor({ flushRows = AUDIT_FLUSH_ROWS, flushMs = AUDIT_FLUSH_MS, hardCap = AUDIT_HARD_CAP, retryBackoffMs = 0, now = Date.now, log = (row) => console.log(JSON.stringify(row)), schedule = (fn, ms) => { const t = setTimeout(fn, ms); t?.unref?.(); return t; }, cancel = (handle) => clearTimeout(handle) } = {}) {
    this.flushRows = flushRows;
    this.flushMs = flushMs;
    this.hardCap = hardCap;
    // After a failed flush, how long triggers other than the trailing timer
    // hold off, so a gate outage costs one failed RPC per interval, not per call.
    this.retryBackoffMs = retryBackoffMs;
    this.now = now;
    this.log = log;
    this.schedule = schedule;
    this.cancel = cancel;
    // key (the gate's Durable Object name) ->
    // { gate, scope, rows, firstAt, retryAt, flight, queued, timer }
    this.buffers = new Map();
  }

  /** Arm the trailing timer that makes the age bound real for an idle gate. */
  _armTimer(key, buf, gate, scope, delayMs = null) {
    if (buf.rows.length === 0) return;
    const at = this.now();
    // A timer whose invocation died never fires;  past its time, stand a new one.
    if (buf.timer && at <= buf.timer.dueAt + this.flushMs) return;
    const delay = delayMs ?? Math.max(0, buf.firstAt + this.flushMs - at);
    const timer = { scope, dueAt: at + delay, handle: null, release: null };
    const fire = () => {
      if (buf.timer === timer) buf.timer = null;
      return this.flush(key, { gate, scope });
    };
    if (scope?.waitUntil) {
      scope.waitUntil(new Promise((resolve) => {
        timer.release = resolve;
        timer.handle = this.schedule(() => resolve(fire()), delay);
      }));
    } else {
      timer.handle = this.schedule(() => { void fire(); }, delay);
    }
    buf.timer = timer;
  }

  _disarmTimer(buf, scope) {
    // Only the invocation that armed it may cancel it.  Any other leaves it
    // standing, to carry later rows or to fire and find nothing to flush.
    const timer = buf.timer;
    if (!timer || timer.scope !== scope) return;
    buf.timer = null;
    this.cancel(timer.handle);
    timer.release?.();
  }

  /**
   * Buffer one row for `gate` under `key`, for the invocation `scope`
   * ({ waitUntil }, or null outside a Worker request).  Resolves immediately
   * unless this append trips a flush trigger:  an error row, or any tripped
   * flush with no waitUntil to carry it, resolves after the batch RPC settles.
   * Never throws:  a flush failure keeps the rows.
   */
  async append(key, gate, row, scope = null) {
    let buf = this.buffers.get(key);
    if (!buf) {
      buf = { gate, scope, rows: [], firstAt: 0, retryAt: 0, flight: null, queued: false, timer: null };
      this.buffers.set(key, buf);
    }
    buf.gate = gate;
    buf.scope = scope;
    const at = this.now();
    if (buf.rows.length === 0) buf.firstAt = at;
    buf.rows.push({ row, at });
    const isError = row?.outcome === "error";
    const tripped = isError || buf.rows.length >= this.flushRows || at - buf.firstAt >= this.flushMs;
    if (tripped && at >= buf.retryAt) {
      const run = this.flush(key, { gate, scope });
      if (isError || !scope?.waitUntil) await run;
    } else {
      // Holding rows through a backoff, or below the triggers:  the cap binds here too.
      this._enforceCap(key, buf);
      this._armTimer(key, buf, gate, scope);
    }
  }

  _enforceCap(key, buf) {
    if (buf.rows.length <= this.hardCap) return;
    const dropped = buf.rows.length - this.hardCap;
    buf.rows.splice(0, dropped);
    buf.firstAt = buf.rows[0]?.at ?? 0;
    this.log({ event: "audit_batch_overflow", key, dropped });
  }

  /**
   * Flush one gate's buffered rows, if any.  Safe to call at any time.  While
   * a flush of the SAME invocation is in flight, this queues a follow-up behind
   * it and returns that flush's chain, so an awaiter (an error append above
   * all) resolves only after the follow-up has run.  A flush of another
   * invocation is never joined:  this one runs on its own.
   */
  async flush(key, { gate, scope } = {}) {
    const buf = this.buffers.get(key);
    if (!buf || buf.rows.length === 0) return;
    const owner = scope === undefined ? buf.scope : scope;
    const stub = gate ?? buf.gate;
    if (buf.flight && buf.flight.scope === owner) {
      buf.queued = true;
      return buf.flight.chain;
    }
    const flight = { scope: owner, chain: null };
    buf.flight = flight;
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
            await stub.auditCallBatch(entries);
          } catch {
            // The batch did not land:  merge it back AHEAD of anything
            // appended meanwhile (order preserved), bound the retained tail
            // at the hard cap with the drop logged, and stop - the trailing
            // timer retries on the flush cadence.
            buf.rows = entries.concat(buf.rows);
            buf.firstAt = entries[0]?.at ?? this.now();
            buf.retryAt = this.now() + this.retryBackoffMs;
            this._enforceCap(key, buf);
            this.log({ event: "audit_batch_flush_failed", key, rows: entries.length });
            this._armTimer(key, buf, stub, owner, this.flushMs);
            return;
          }
          if (flight === buf.flight && buf.queued) {
            buf.queued = false;
            continue;
          }
          if (buf.rows.length < this.flushRows) break;
        }
        if (buf.rows.length === 0) this._disarmTimer(buf, owner);
        else this._armTimer(key, buf, stub, owner);
      } finally {
        if (buf.flight === flight) buf.flight = null;
      }
    })();
    flight.chain = run;
    // Under waitUntil, so a client hanging up cannot cancel the RPC mid-flight.
    owner?.waitUntil?.(run);
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
