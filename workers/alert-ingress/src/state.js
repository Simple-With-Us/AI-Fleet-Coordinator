// Shared alert ingress: state transitions for one alertable thing.
//
// The core rule this file exists to enforce: an inbound event is not a message.
// A message is only warranted when the *state* changes.  A monitor that flaps
// Triggered -> Recovered -> Triggered ten times in an hour delivers ten events
// and one state change, and the channel should show one.
//
// Pure functions only, so every rule below is unit-testable without a Workers
// runtime.  See state.test.js.

/** Minimum time an incident must stay open before it is worth telling anyone. */
export const DEFAULT_MIN_OPEN_MS = 10 * 60 * 1000;
/** Production-down is real fast; it gets a much shorter wait (owner 2026-10-16). */
export const PROD_DOWN_MIN_OPEN_MS = 5 * 60 * 1000;

const SEVERITY_RANK = { INFO: 0, MODERATE: 1, SEVERE: 2 };
const SEVERITIES = new Set(Object.keys(SEVERITY_RANK));

/**
 * A single alertable thing's current record.
 * @typedef {object} AlertState
 * @property {"ok"|"pending"|"alerting"} phase
 * @property {string} severity   SEVERE | MODERATE | INFO
 * @property {number} since      epoch ms the current phase began
 * @property {number} updated    epoch ms of the last event
 * @property {string} text       one-line summary for the standing status
 * @property {string} link       monitor / incident link
 * @property {boolean} [prodDown] counts as production down (shorter wait)
 */

/** @returns {AlertState} */
export function initialState() {
  return { phase: "ok", severity: "INFO", since: 0, updated: 0, text: "", link: "" };
}

/**
 * Decide what an inbound event should cause, given the stored state.
 *
 * Returns the action plus the state to persist.  Actions:
 *   discard     no observable change; do not post, do not touch the sweep
 *   hold        an incident opened but is not yet old enough to announce
 *   announce    an incident is now old enough (or severe enough) to announce
 *   update      an already-announced incident changed severity or detail
 *   resolve     an announced incident closed
 *   refresh     nothing changed; only the standing status needs rewriting
 *
 * @param {AlertState|null} prev
 * @param {{severity?:string, firing?:boolean, prodDown?:boolean, text?:string, link?:string}} event
 * @param {number} now epoch ms
 * @param {{minOpenMs?:number}} [opts]
 */
export function decide(prev, event, now, opts = {}) {
  const state = prev || initialState();
  const minOpenMs = opts.minOpenMs ?? DEFAULT_MIN_OPEN_MS;
  const firing = event.firing !== false;
  // A fresh incident whose vendor names no severity opens SEVERE, not at the
  // closed state's INFO:  Better Stack never sends one and Datadog often does
  // not, so a site that went down read INFO.  Over-severing a new alert is
  // recoverable and under-severing one is not (the Sentry rule, everywhere).
  // An open incident keeps its own severity when an update names none.
  const severity = normalizeSeverity(
    event.severity,
    firing && state.phase === "ok" ? "SEVERE" : state.severity,
  );
  const prodDown = event.prodDown === true;
  const threshold = prodDown ? Math.min(minOpenMs, PROD_DOWN_MIN_OPEN_MS) : minOpenMs;
  const text = event.text ?? state.text;
  const link = event.link ?? state.link;

  // --- closing -----------------------------------------------------------
  if (!firing) {
    // Auto-resolved before it was ever announced: the pair is noise.  The
    // guide forbids posting an open -> resolved pair that nobody ever saw.
    if (state.phase === "pending") {
      return { action: "discard", state: initialState() };
    }
    if (state.phase === "alerting") {
      return {
        action: "resolve",
        state: { ...state, phase: "ok", severity: "INFO", since: now, updated: now, text: "", link: "", prodDown: false },
      };
    }
    // Already closed: a stale prodDown flag must not survive recovery either.
    return { action: "refresh", state: state.prodDown ? { ...state, prodDown: false } : state };
  }

  // --- opening -----------------------------------------------------------
  if (state.phase === "ok") {
    return {
      action: "hold",
      state: { phase: "pending", severity, since: now, updated: now, text, link, prodDown },
    };
  }

  // Already open: a repeat delivery of the same condition is a refresh, not an
  // announcement.  This is the line that turns a flapping monitor from twenty
  // messages into one.
  const escalated = SEVERITY_RANK[severity] > SEVERITY_RANK[state.severity];
  const deescalated = SEVERITY_RANK[severity] < SEVERITY_RANK[state.severity];
  const changed = text !== state.text || link !== state.link;

  if (state.phase === "pending") {
    if (escalated || deescalated || changed) {
      return {
        action: "hold",
        state: { ...state, severity, updated: now, text, link, prodDown },
      };
    }
    return { action: "refresh", state: { ...state, updated: now } };
  }

  // phase === "alerting": only a real change earns a new message.
  if (escalated) {
    return {
      action: "update",
      state: { ...state, severity, updated: now, text, link },
    };
  }
  if (deescalated || changed) {
    return { action: "refresh", state: { ...state, severity, updated: now, text, link } };
  }
  return { action: "refresh", state: { ...state, updated: now } };
}

/**
 * The sweep promotes incidents that have been open long enough to matter.
 * Run on a schedule; this is what turns a held incident into a real message.
 *
 * @param {AlertState|null} prev
 * @param {number} now
 * @param {{minOpenMs?:number}} [opts]
 */
export function sweep(prev, now, opts = {}) {
  const state = prev || initialState();
  if (state.phase !== "pending") return null;
  const minOpenMs = opts.minOpenMs ?? DEFAULT_MIN_OPEN_MS;
  const threshold = state.prodDown
    ? Math.min(minOpenMs, PROD_DOWN_MIN_OPEN_MS)
    : minOpenMs;
  if (now - state.since < threshold) return null;
  return {
    action: "announce",
    state: { ...state, phase: "alerting", updated: now },
  };
}

/** @param {unknown} value @param {string} fallback */
export function normalizeSeverity(value, fallback = "INFO") {
  if (typeof value !== "string") return fallback;
  const up = value.trim().toUpperCase();
  if (SEVERITIES.has(up)) return up;
  // Common vendor vocabularies.
  if (["CRIT", "CRITICAL", "FATAL", "P1", "ERROR", "ALERT"].includes(up)) return "SEVERE";
  if (["WARN", "WARNING", "MAJOR", "P2"].includes(up)) return "MODERATE";
  if (["INFO", "OK", "NOTICE", "P3", "RECOVERED"].includes(up)) return "INFO";
  return fallback;
}

/** One line per the fleet alerts format: `SEVERITY  .  service  .  what  .  link`. */
export function formatEventLine({ severity, service, what, link }) {
  const parts = [severity, service, what];
  if (link) parts.push(link);
  return parts.filter(Boolean).join("  \u2022  ");
}