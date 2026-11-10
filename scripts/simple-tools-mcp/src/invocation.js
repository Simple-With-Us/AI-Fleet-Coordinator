// The per-invocation budget (src/budget-guard.mjs) for this Worker.  One budget
// per request, carried by AsyncLocalStorage so the choke points that spend it
// (gate stubs, the OAuth grant listing, global fetch, diagnostics) need no new
// parameter through the tool code.  Code running outside fetch (the Durable
// Object, node tests) has no invocation and spends nothing.
//
// These caps stop a code bug, not traffic:  the Rate Limiting bindings bound
// traffic.  A BudgetExceeded inside a tool becomes that tool's error;  anywhere
// else the entry point answers 503 (src/index.js).

import { AsyncLocalStorage } from "node:async_hooks";
import { BudgetExceeded, createBudget } from "./budget-guard.mjs";

export const INVOCATION_CAPS = Object.freeze({
  // The Free plan's external subrequest limit.  Over it the platform throws anyway.
  fetch: 50,
  // GET /admin is the largest honest fan-out, at 131 gate calls.  With the fetch cap
  // this stays inside wrangler.jsonc's 200 subrequests, so a runaway meets this
  // budget's log line and 503 before the platform's own error, whether or not a gate
  // call counts as a subrequest.  test/invocation.test.mjs pins both sides.
  doRpc: 150,
});

// Per isolate per minute.  A diagnostic or refusal row is a Durable Object write
// an anonymous caller can trigger.  A live-grant listing on the OAuth Git path is
// one KV list on a cache miss, and KV allows 1,000 lists a day on Free.
export const MINUTE_CAPS = Object.freeze({ diagnosticRow: 60, kvList: 30 });

const budget = createBudget({
  worker: "simple-tools-mcp",
  perInvocation: INVOCATION_CAPS,
  perMinute: MINUTE_CAPS,
});
const storage = new AsyncLocalStorage();

export function runInvocation(fn) {
  return storage.run(budget.invocation(), fn);
}

/** Spend one `kind`, or throw BudgetExceeded.  A no-op outside an invocation. */
export function spend(kind) {
  storage.getStore()?.spend(kind);
}

/** May this isolate write one more anonymous-triggered diagnostic row?  Never throws. */
export function diagnosticRowAllowed() {
  try {
    (storage.getStore() ?? budget.invocation()).spend("diagnosticRow");
    return true;
  } catch (error) {
    if (error instanceof BudgetExceeded) return false;
    throw error;
  }
}

const limitLoggedMinute = new Map();

/** One line per event and binding per minute, so a quiet event never hides a runaway's. */
export function logLimited(event, binding) {
  const minute = Math.floor(Date.now() / 60_000);
  const key = binding ? `${event}:${binding}` : event;
  if (limitLoggedMinute.get(key) === minute) return;
  limitLoggedMinute.set(key, minute);
  console.log(JSON.stringify({ event, binding }));
}

/**
 * A Rate Limiting binding's answer, read from `env` by name so the log can say which
 * guard is not running.  A missing or failing limiter never takes the MCP down with
 * it:  test/invocation.test.mjs pins the bindings in wrangler.jsonc, and the account
 * watchdog is the stop if a guard is ever absent.
 */
export async function withinLimit(env, binding, key) {
  const limiter = env[binding];
  if (!limiter) {
    logLimited("rate_limiter_missing", binding);
    return true;
  }
  try {
    return (await limiter.limit({ key })).success;
  } catch {
    logLimited("rate_limiter_unavailable", binding);
    return true;
  }
}

/** A stub whose every method call spends one doRpc.  Counts calls, changes nothing else. */
export function countedStub(stub) {
  if (!storage.getStore()) return stub;
  return new Proxy(stub, {
    get(target, property) {
      const value = target[property];
      if (typeof value !== "function" || property === "then") return value;
      return (...args) => {
        spend("doRpc");
        // Reflect:  on a real stub `value.apply` would itself be an RPC property.
        return Reflect.apply(value, target, args);
      };
    },
  });
}

/** A fetch that spends one `fetch` per call.  A refusal is a rejected promise, as a failed fetch is. */
export function countedFetch(realFetch, spendFetch = () => spend("fetch")) {
  return (input, init) => {
    try {
      spendFetch();
    } catch (error) {
      return Promise.reject(error);
    }
    return realFetch.call(globalThis, input, init);
  };
}
