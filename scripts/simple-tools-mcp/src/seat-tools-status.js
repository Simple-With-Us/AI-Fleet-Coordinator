// Per-seat, per-tool readiness for Muster's alarms (names and states only, never a value).
//
// Each tool state mirrors what the hosted runtime itself resolves for that seat (src/index.js, the
// seat wiring), so a tool reads "missing" exactly when the seat's tool would answer not_configured.
// A read that throws is "unavailable", never "missing": an Infisical outage must not look like a
// deleted credential.  States:  ok | missing | unavailable | not_applicable.
// The tool list is derived from the same tables the runtime uses, not a hand-picked list.
import { LISTENER_READ_SECRETS, WAKE_SECRETS } from "./config.js";

export const SEAT_TOOLS = Object.freeze(["zulip", "linear", "wake", "fleetlink", "recall", "relay", "notes"]);
export const LISTENER_KEY_MIN = 32;
export const NOTES_KEY_MIN = 32;

/** The seat's wake or read-only listener key secret name, or null (same rule as eavesdropper.js). */
export function listenerNameFor(seat) {
  if (Object.hasOwn(LISTENER_READ_SECRETS, seat)) return LISTENER_READ_SECRETS[seat];
  return Object.hasOwn(WAKE_SECRETS, seat) ? WAKE_SECRETS[seat] : null;
}

const UNAVAILABLE = Symbol("unavailable");
/** Run a read; a throw becomes the unavailable marker. */
export async function settle(fn) {
  try { return await fn(); } catch { return UNAVAILABLE; }
}
export const isUnavailable = (v) => v === UNAVAILABLE;

const state = (v, ok) => (v === UNAVAILABLE ? "unavailable" : ok ? "ok" : "missing");

/**
 * facts (each already read, or UNAVAILABLE):
 *   zulipKey, zulipEmail (strings), linearEnabled (bool), linearPair ({clientId, clientSecret}|null),
 *   wakeKey (string), fleetlinkSecret (string), fleetlinkSeatToken (bool), fleetlinkDenied (bool),
 *   recallConfigured (bool), relaySecrets ({accessId, accessSecret}), relayUpstream (bool),
 *   eventsOnly (bool), notesKey (string), listenerUrlOk (bool: LISTENER_API_URL is a valid https origin).
 * relayUpstream must already be the validated origin check (grUpstreamUrl), not raw truthiness.
 */
export function seatToolStates(seat, f) {
  const tools = [];
  const add = (tool, st) => tools.push({ tool, state: st });
  add("zulip", f.zulipKey === UNAVAILABLE ? "unavailable" : state(f.zulipKey, Boolean(f.zulipKey) && Boolean(f.zulipEmail)));
  if (!f.linearEnabled) add("linear", "not_applicable");
  else add("linear", state(f.linearPair, Boolean(f.linearPair && f.linearPair.clientId && f.linearPair.clientSecret)));
  if (!listenerNameFor(seat)) add("wake", "not_applicable");
  else add("wake", state(f.wakeKey, Boolean(f.listenerUrlOk) && typeof f.wakeKey === "string" && f.wakeKey.trim().length >= LISTENER_KEY_MIN));
  if (f.fleetlinkSecret === UNAVAILABLE || f.fleetlinkSeatToken === UNAVAILABLE) add("fleetlink", "unavailable");
  else if (f.fleetlinkDenied) add("fleetlink", "missing");
  else add("fleetlink", Boolean(f.fleetlinkSeatToken) || Boolean(f.fleetlinkSecret) ? "ok" : "missing");
  add("recall", state(f.recallConfigured, f.recallConfigured === true));
  if (f.eventsOnly) add("relay", "not_applicable");
  else {
    const s = f.relaySecrets;
    add("relay", state(s, Boolean(f.relayUpstream) && Boolean(s && s.accessId && s.accessSecret)));
  }
  add("notes", state(f.notesKey, Boolean(f.listenerUrlOk) && typeof f.notesKey === "string" && f.notesKey.trim().length >= NOTES_KEY_MIN));
  const bad = tools.filter((t) => t.state === "missing" || t.state === "unavailable");
  return { seat, tools, problem: bad.length ? bad.map((t) => `${t.tool}:${t.state}`).join(",") : "ok" };
}
