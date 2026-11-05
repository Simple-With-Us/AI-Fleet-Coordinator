// The worker isolate's verified-identity cache (R4, DO burn reduction).
//
// The gate's per-request identity verification - the OAuth grant `check`
// in serveMcp, `gbBearerCheck` on the bearer routes, and the role verdict
// behind `ensureSeat` - is a Durable Object RPC on every POST, and the
// fleet's clients are chatty one-shot processes.  This cache lets ONE
// isolate reuse a POSITIVE verification for a bounded window (default one
// hour, GATE_IDENTITY_CACHE_MS;  the owner approved up to a day and ruled
// the revocation latency acceptable on 2026-11-03, because cutting a seat
// off is never done by tweaking one gate setting).  Entries are keyed by
// the SHA-256 of the presented credential - never the credential itself -
// and nothing about an entry is ever logged.
//
// HONEST ACCOUNTING (the trade, stated in full).  Inside an entry's
// window, for a call this isolate serves from the cache:
//   - NOT re-verified:  the grant's epoch, phase and age (OAuth), the
//     bearer's expiry, rotation and mcp_accept flag (bearer routes), the
//     pause flag, and the seat's Zulip role verdict.  A pause, an epoch
//     bump, a bearer rotation or revocation, or a role refusal recorded
//     in the gate takes up to the window to bite IN THIS ISOLATE (each
//     isolate converges on its own schedule).
//   - NOT taken for reads:  the reserve slot - so per-seat read spacing,
//     read budgets and the read cooldown are not enforced in the window.
//   - STILL enforced on every call:  the credential's own authenticity
//     (a different bearer hashes to a different key and misses;  the
//     OAuth library still validates every access token), scopes,
//     argument validation, secret scanning, channel allowlists, and
//     every downstream API's own auth.  WRITES keep their full gate
//     semantics:  the reserve slot (which still refuses a paused seat,
//     so in the window only reads slip a pause, never writes) and the
//     idempotency pair hit the Durable Object exactly as before, as do
//     the Linear provider-quota reserves and the gr-assert claims.
//   - The token exchange still re-checks the grant live on every code
//     exchange and refresh;  only the per-POST verification is cached.
// Eviction is immediate in this isolate when:  the gate refuses the
// credential on a live verification (check, gbBearerCheck, ensureSeat),
// a reserve refuses (pause, budget, spacing), an admin action changes
// the seat's state through this isolate (arm, disarm, pause, unpause,
// revoke, bearer mint or revoke), or the system pauses a seat for a
// refresh-token mismatch.  Only positive verifications are ever stored;
// a refusal never creates or refreshes an entry.  Events-only seats are
// never stored at all:  their calls are refused downstream regardless,
// so caching their verification would buy nothing and would only rent
// out that seat class's instant revocation.

export const IDENTITY_CACHE_MAX = 512;

export class IdentityCache {
  constructor({ max = IDENTITY_CACHE_MAX, now = Date.now } = {}) {
    this.max = max;
    this.now = now;
    // key (SHA-256 of the credential) -> entry (see set()).
    this.entries = new Map();
  }

  /** The fresh entry for `key`, or null.  An expired entry is deleted. */
  get(key, now = this.now()) {
    if (!key) return null;
    const entry = this.entries.get(key);
    if (!entry) return null;
    if (now >= entry.expiresAt) {
      this.entries.delete(key);
      return null;
    }
    return entry;
  }

  /**
   * Store a positive verification.  `verified` carries what the gate
   * proved:  { seat, via, epoch?, approvedAt?, phase?, mcp_accept?,
   * role? }.  The window runs from this call;  re-setting a key restarts
   * its window (a fresh live verification is exactly that).
   */
  set(key, verified, ttlMs, now = this.now()) {
    if (!key || !verified?.seat || !(ttlMs > 0)) return;
    this.entries.delete(key);
    this.entries.set(key, { ...verified, role: verified.role ?? null, verifiedAt: now, expiresAt: now + ttlMs });
    while (this.entries.size > this.max) {
      this.entries.delete(this.entries.keys().next().value);
    }
  }

  /**
   * Enrich a fresh entry in place (the role verdict ensureSeat proves
   * after the route's verification).  Never extends the window:  the
   * entry still expires when the verification it hangs off does.
   */
  update(key, patch, now = this.now()) {
    const entry = this.get(key, now);
    if (entry) Object.assign(entry, patch);
  }

  /** Drop one credential's entry (a refusal for that credential). */
  evict(key) {
    if (key) this.entries.delete(key);
  }

  /** Drop every entry for one seat (an admin or system state change). */
  evictSeat(seat) {
    for (const [key, entry] of this.entries) {
      if (entry.seat === seat) this.entries.delete(key);
    }
  }

  /** Live entry counts by seat, for tests and diagnostics. */
  pending() {
    const out = {};
    for (const entry of this.entries.values()) out[entry.seat] = (out[entry.seat] ?? 0) + 1;
    return out;
  }
}
