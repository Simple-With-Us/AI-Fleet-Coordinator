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
