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
//     bearer's rotation and mcp_accept flag (bearer routes), the
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
//     every downstream API's own auth.  Zulip, Linear, FleetLink and
//     recall WRITES keep their full gate semantics:  the reserve slot
//     (which still refuses a paused seat) and the idempotency pair hit
//     the Durable Object exactly as before, as do the Linear
//     provider-quota reserves and the gr-assert claims (so OAuth gr_*
//     calls check a pause live).  Hosted gr_* writes on a BEARER
//     connection take no reserve:  like direct Git calls, they can slip a
//     pause for up to the Git verify windows (owner ruling 2026-11-06).
//   - The token exchange still re-checks the grant live on every code
//     exchange and refresh;  only the per-POST verification is cached.
//   - A bearer route's entry never outlives the bearer's own expiry
//     (bearerCacheMs below).
//   - Git callbacks use their own windows and keys (AFC-617), and
//     GATE_IDENTITY_CACHE_MS never turns a bearer verify cache on.
//     /internal/gr-bearer-verify reuses a positive answer for
//     GR_VERIFY_CACHE_MS (src/config.js:  an hour by default, at most an
//     hour, 0 turns it off, never past the bearer's expiry), so a pause or
//     revocation can reach a bearer's Git calls, WRITES included, up to an
//     hour late in an isolate (owner ruling 2026-11-06:  a checked token may
//     work for an hour, because a seat is never cut off through the tools).
//     The OAuth Git verify reuses a live-grant listing for up to
//     GRANT_EXISTS_CACHE_MS (src/index.js, an hour, never past the grant's
//     expiry), while its epoch and pause check stays live on every call
//     (claimGrAssert), because that call must spend the one-use jti anyway.
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

/**
 * A bearer route's window:  the identity cache window, but never past the
 * bearer's own expiry (`checked.expires_at`, ms, from gbBearerCheck).  An
 * answer with no usable expiry, or a bearer about to expire, gives 0, so
 * nothing is stored and the next call re-verifies live.
 */
export function bearerCacheMs(config, checked, now = Date.now()) {
  const expiresAt = Number(checked?.expires_at);
  if (!Number.isFinite(expiresAt) || expiresAt <= 0) return 0;
  return Math.max(0, Math.min(config.gateIdentityCacheMs, expiresAt - now));
}

export class IdentityCache {
  constructor({ max = IDENTITY_CACHE_MAX, now = Date.now } = {}) {
    this.max = max;
    this.now = now;
    // key (SHA-256 of the credential) -> entry (see set()).
    this.entries = new Map();
    // key -> when this isolate last saw a refusal or eviction for it, and
    // seat -> when it last evicted the whole seat.  set() with `since`
    // consults them, so a slow positive verification that STARTED before a
    // refusal can never write over it.  Both maps are bounded like entries.
    this.refusedAt = new Map();
    this.seatEvictedAt = new Map();
  }

  /** Record a refusal time in a bounded map (oldest out first). */
  remember(map, key, now) {
    map.delete(key);
    map.set(key, now);
    while (map.size > this.max) map.delete(map.keys().next().value);
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
   * role? }.  The window runs from `now`;  re-setting a key restarts its
   * window (a fresh live verification is exactly that).  A caller that
   * computed `ttlMs` from a clock reading passes that same reading as
   * `now`, so the entry cannot end later than the limit it was capped at.
   * `since` is when the caller's live verification started (this cache's
   * clock):  when a refusal or eviction for the key or its seat landed
   * after it, the positive answer is older than the refusal and is not
   * stored.  (One in the same millisecond is taken as earlier:  an admin
   * eviction followed at once by a fresh live check must still cache.)
   */
  set(key, verified, ttlMs, now = this.now(), { since } = {}) {
    if (!key || !verified?.seat || !(ttlMs > 0)) return;
    if (Number.isFinite(since)) {
      if ((this.refusedAt.get(key) ?? -Infinity) > since) return;
      if ((this.seatEvictedAt.get(verified.seat) ?? -Infinity) > since) return;
    }
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
  evict(key, now = this.now()) {
    if (!key) return;
    this.entries.delete(key);
    this.remember(this.refusedAt, key, now);
  }

  /** Drop every entry for one seat (an admin or system state change). */
  evictSeat(seat, now = this.now()) {
    if (seat) this.remember(this.seatEvictedAt, seat, now);
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
