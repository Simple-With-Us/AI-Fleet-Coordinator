// The SeatGate Durable Object stub for a name.  A pure module (no `cloudflare:` imports), so the node tests can
// import it;  src/index.js uses it for every gate it opens.
import { GATE_LOG_NAME } from "./config.js";
import { BEARER_SEATS } from "./bearer-identities.js";
import { GB_IDENTITIES } from "./delegated-identities.js";

/**
 * The SeatGate of a seat, an enabled Grok Bot persona, or the refusal log.  GB_IDENTITIES is the one list of
 * personas (delegated-identities.js, scripts/agent_sync/mcp/gb-identities.json):  a persona has its own gate for
 * its spacing, idempotency rows and budget, so createIdentity must be able to open it.  Any other name throws.
 */
export function gate(env, name) {
  if (name !== GATE_LOG_NAME && !BEARER_SEATS.includes(name) && !GB_IDENTITIES.includes(name)) throw new Error("not a seat");
  return env.SEAT_GATE.get(env.SEAT_GATE.idFromName(name));
}

// ---- shared provider quota objects
//
// A provider quota is not a seat.  Its bucket lives in a SeatGate instance of the same class under a reserved
// name, `providerquota:<provider>:<identity>`, opened ONLY through this factory (gate() above still refuses the
// name, so no seat, persona or tool path can reach it).  The name is built from a validated provider and a
// validated identity, never from caller text, and only the two quota methods are ever called on it.
export const PROVIDER_QUOTA_PREFIX = "providerquota:";
const QUOTA_PROVIDERS = Object.freeze(["linear"]);
const QUOTA_IDENTITY = /^(?:unknown|[0-9a-f]{64})$/;

export function providerQuotaGate(env, provider, identity) {
  if (!QUOTA_PROVIDERS.includes(provider) || typeof identity !== "string" || !QUOTA_IDENTITY.test(identity)) throw new Error("not a provider quota");
  return env.SEAT_GATE.get(env.SEAT_GATE.idFromName(`${PROVIDER_QUOTA_PREFIX}${provider}:${identity}`));
}

// Linear's documented limit is 5,000 requests an hour per user or app user, plus a complexity budget that
// request counts do not bound (https://linear.app/developers/rate-limiting).  4,000 is margin under the request
// limit, not a complexity guarantee;  Linear's own 429 and the shared cooldown remain the backstop for that.
export const LINEAR_REQUESTS_PER_HOUR = 4000;
// The unknown bucket serves only a PROVING request: the first whoami for an access token, the one that says which
// user the token acts as.  That proof is stored per token fingerprint in the seat's own Durable Object (linear.js),
// so it happens for a new token, after proof expiry (at most 24 hours), or after a 401, not once per request or isolate, and
// ordinary traffic never touches this bucket.  Each proving request is also debited to the user it proves, so the
// user's bucket counts every request spent as that user.  The only slack is those proving requests (a token's
// proof is repeated only by requests racing at the moment it is minted), all of them inside this one guard.
export const LINEAR_UNKNOWN_REQUESTS_PER_HOUR = 300;
const limitFrom = (value, fallback) => {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 && n <= 1_000_000 ? n : fallback;
};

/** The quota interface LinearClient requires:  reserve({identity}) before every GraphQL fetch, noteRateLimited after a 429. */
export function linearQuotaFromEnv(env) {
  const perHour = limitFrom(env?.LINEAR_REQUESTS_PER_HOUR, LINEAR_REQUESTS_PER_HOUR);
  const unknownPerHour = limitFrom(env?.LINEAR_UNKNOWN_REQUESTS_PER_HOUR, LINEAR_UNKNOWN_REQUESTS_PER_HOUR);
  const open = (identity) => providerQuotaGate(env, "linear", identity ?? "unknown");
  return Object.freeze({
    reserve: ({ identity = null } = {}) => open(identity).providerQuotaReserve({ limit: identity ? perHour : unknownPerHour, n: 1 }),
    // A request that already went out and proved whose it was:  recorded in that identity's bucket, over the limit if need be.
    debit: ({ identity }) => open(identity).providerQuotaReserve({ limit: perHour, n: 1, force: true }),
    noteRateLimited: ({ identity = null, retryAfterS = null } = {}) => open(identity).providerQuotaNoteRateLimited({ retryAfterS }),
  });
}
