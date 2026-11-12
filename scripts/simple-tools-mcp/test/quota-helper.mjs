// A provider quota over real seat-state storage, one in-memory store per identity name, as the SeatGate
// instances `providerquota:linear:<identity>` hold it live.
import * as S from "../src/seat-state.js";

export function memoryQuota({ now = Date.now, perHour = 4000, unknownPerHour = 300, stores = new Map() } = {}) {
  const calls = [];
  // A Durable Object's input gate runs one storage method at a time per object;  the in-memory store has no gate,
  // so each identity's reserves are chained here to stand in for it.
  const chains = new Map();
  const gated = (name, fn) => {
    const run = (chains.get(name) ?? Promise.resolve()).then(fn, fn);
    chains.set(name, run.catch(() => {}));
    return run;
  };
  const open = (identity) => {
    const name = identity ?? "unknown";
    if (!stores.has(name)) stores.set(name, S.memoryStore());
    return stores.get(name);
  };
  return {
    stores,
    calls,
    reserve: async ({ identity = null } = {}) => {
      calls.push(identity ?? "unknown");
      return gated(identity ?? "unknown", () => S.providerQuotaReserve(open(identity), { limit: identity ? perHour : unknownPerHour, n: 1 }, now()));
    },
    debit: async ({ identity }) => gated(identity, () => S.providerQuotaReserve(open(identity), { limit: perHour, n: 1, force: true }, now())),
    noteRateLimited: async ({ identity = null, retryAfterS = null } = {}) => gated(identity ?? "unknown", () => S.providerQuotaNoteRateLimited(open(identity), { retryAfterS }, now())),
  };
}
