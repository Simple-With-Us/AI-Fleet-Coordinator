// The session id a notes write must carry (AFC-361, docs/protocols/resume-wake-safety-design.md section 4), for the
// hosted Worker.  The Python twin is scripts/agent_sync/session_ids.py;  the contract is the `sessionIds` block of
// fleet-apps.json, and scripts/test_session_ids_parity.py keeps these constants equal to it.
//
// A writer with seat S finds its id in this order:  the explicit value (the `session` tool argument, checked against
// ID_PATTERN), the environment variable AGENT_SESSION, S's own variables in ENV_BY_SEAT (a seat with no entry tries
// FALLBACK_ENV), and then an exempt seat writes without one and every other seat is refused.  A Worker has no
// environment a client controls, so there only the explicit argument counts (`env` is for tests and stays unset).
//
// The exemption is decided from the seat the Worker resolved for the call (the grant's identity), after the same
// canonical name rule as stdio, and never from an argument.  No `cloudflare:` imports.

import { canonicalSeatName } from "./identity-aliases.js";

// ---- copies of fleet-apps.json `sessionIds` (keep them equal) ----------------------------------------------------
export const ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$";
export const GENERIC_ENV = "AGENT_SESSION";
export const EXEMPT_SEATS = Object.freeze(["ECHO", "GROK-BOT", "JET", "MA", "WISE"]);
export const EXEMPT_PATTERNS = Object.freeze(["GB-*"]);
export const ENV_BY_SEAT = Object.freeze({
  CLAUDE: Object.freeze(["CLAUDE_CODE_SESSION_ID"]),
  CODEX: Object.freeze(["CODEX_THREAD_ID"]),
  CURSOR: Object.freeze(["CURSOR_CONVERSATION_ID", "CURSOR_AGENT_CHAT_ID"]),
  GROK: Object.freeze(["GROK_SESSION_ID"]),
  MM: Object.freeze(["__MAVIS_RUNTIME_SESSION_ID"]),
  OPENCODE: Object.freeze(["OPENCODE_SESSION_ID"]),
});
export const FALLBACK_ENV = Object.freeze([
  "CLAUDE_CODE_SESSION_ID",
  "CODEX_THREAD_ID",
  "CURSOR_CONVERSATION_ID",
  "CURSOR_AGENT_CHAT_ID",
  "GROK_SESSION_ID",
  "OPENCODE_SESSION_ID",
  "__MAVIS_RUNTIME_SESSION_ID",
]);

const ID_RE = new RegExp(ID_PATTERN);
export const SURFACES = Object.freeze(["tool", "cli", "relay", "hosted"]);

/** A non-exempt writer with no session id.  The message says how to pass one;  it never holds a note body. */
export class SessionRequired extends Error {
  constructor(message) {
    super(message);
    this.name = "SessionRequired";
  }
}

const seatOf = (seat) => canonicalSeatName(seat ?? "");

/** True when this seat has a single thread with the owner and needs no session id (after the canonical name rule). */
export function isExempt(seat) {
  const name = seatOf(seat);
  if (!name) return false;
  if (EXEMPT_SEATS.includes(name)) return true;
  return EXEMPT_PATTERNS.some((pattern) => new RegExp(`^${pattern.split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*")}$`).test(name));
}

/** The value as a session id (trimmed), or a RangeError. */
export function check(value, where = "the session id") {
  const text = typeof value === "string" ? value.trim() : null;
  if (text === null || !ID_RE.test(text)) {
    throw new RangeError(`${where} is not a valid session id:  it must be 4 to 128 characters, start with a letter or digit, and hold only letters, digits, . _ : and -`);
  }
  return text;
}

/** The environment variables that hold this seat's session id, or [] for a seat with none. */
export const ownVariables = (seat) => [...(ENV_BY_SEAT[seatOf(seat)] ?? [])];

function firstSet(names, env) {
  for (const name of names) {
    const value = String(env?.[name] ?? "").trim();
    if (value) return [name, value];
  }
  return null;
}

function fromEnv(seat, env) {
  if (!env) return null;
  const name = seatOf(seat);
  const found = firstSet([GENERIC_ENV], env) ?? firstSet(Object.hasOwn(ENV_BY_SEAT, name) ? ENV_BY_SEAT[name] : FALLBACK_ENV, env);
  return found ? check(found[1], `the ${found[0]} environment variable`) : null;
}

/** The session id for `seat` (steps 1 to 3), or null.  Throws a RangeError for an invalid explicit or env value. */
export function detect(seat, explicit = undefined, env = undefined) {
  if (typeof explicit === "string" && explicit.trim()) return check(explicit, "the session argument");
  if (explicit !== undefined && explicit !== null && typeof explicit !== "string") throw new RangeError("the session argument must be text");
  return fromEnv(seat, env);
}

/** The refusal text for a non-exempt writer with no session id (no note body, no secret). */
export function refusal(seat, surface = "tool") {
  const name = seatOf(seat) || "this writer";
  const own = ownVariables(seat);
  const tail = "A headless resume finds the session from the note's Session line.";
  if (surface === "relay" || surface === "hosted") {
    const where = own.length ? ` (for ${name} it is $${own.join(" or $")} in the agent's own shell)` : "";
    const lead = surface === "relay" ? `this request carries none.  Resend it with` : `none was given.  Pass it in`;
    return `a session id is required to write an Apple Note as ${name} and ${lead} the \`session\` argument of notes_create or notes_update${where}.  ${tail}`;
  }
  const how = surface === "tool" ? "the `session` argument" : "--session ID";
  const mine = own.length ? `;  ${name}'s own variable is ${own.join(" or ")}, and it is empty here` : "";
  return `a session id is required to write an Apple Note as ${name}.  Pass it as ${how}, or set the AGENT_SESSION environment variable${mine}.  ${tail}`;
}

/**
 * The session id for `seat`, null for an exempt seat that has none, or a SessionRequired.  `surface` is "tool", "cli",
 * "relay" or "hosted" (a Worker, which has no environment to set).  A bad explicit value throws a RangeError for
 * everyone;  a bad environment value throws for a non-exempt seat and is passed over for an exempt one.
 */
export function require(seat, explicit = undefined, env = undefined, surface = "tool") {
  if (!SURFACES.includes(surface)) throw new RangeError(`surface must be one of ${SURFACES.join(", ")}`);
  const exempt = isExempt(seat);
  let found = null;
  try {
    found = detect(seat, explicit, env);
  } catch (error) {
    if (!(error instanceof RangeError) || !exempt || (typeof explicit === "string" && explicit.trim())) throw error;
  }
  if (found !== null || exempt) return found;
  throw new SessionRequired(refusal(seat, surface));
}
export const requireSession = require;
