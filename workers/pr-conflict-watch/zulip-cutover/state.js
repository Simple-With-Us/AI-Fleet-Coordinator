// Mergeable-state transitions for pr-conflict-watch (testable without Workers runtime).

/** States that warrant a Zulip alert and a KV dedupe key (real merge conflicts only). */
export const ALERT_STATES = new Set(["dirty"]);

/**
 * @param {string | null} prev Stored KV value for this PR, if any.
 * @param {string} mergeableState GitHub pull mergeable_state, or "closed".
 * @returns {"noop" | "clear" | "alert" | "recover"}
 */
export function decideStateTransition(prev, mergeableState) {
  if (mergeableState === "closed") {
    return prev ? "clear" : "noop";
  }
  if (ALERT_STATES.has(mergeableState)) {
    return prev !== mergeableState ? "alert" : "noop";
  }
  if (prev && ALERT_STATES.has(prev)) {
    return "recover";
  }
  return "noop";
}
