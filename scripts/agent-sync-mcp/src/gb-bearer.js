// Per-bot MCP bearers for the Grok Bot personas.  A bearer lets one GB persona
// reach /mcp without the OAuth consent flow, and ONLY as itself:  the router is
// built with no other identity, so there is nothing to switch to.
//
//   token   gbb_<role>_<43 base64url chars>   (role is the lower-case GB role,
//           for example gbb_fixer_...), 256 random bits.
//   stored  only its SHA-256 (hex) in that persona's own SeatGate, with created,
//           expires and last-used times.  The Worker never keeps a copy it could
//           show again:  /admin shows the plaintext once, on the mint response.
//           Rotating mints a new token and replaces the old one.
//   where   nowhere else.  Not in Infisical, not in a Worker secret, not in logs
//           or the audit rows (which hold who, when and expiry only).

import { GB_IDENTITIES } from "./delegated-identities.js";
import { KNOWN_SEATS } from "./config.js";

export const GB_BEARER_PREFIX = "gbb_";
export const GB_BEARER_TTL_MS = 90 * 24 * 60 * 60 * 1000;

const TOKEN_RE = /^gbb_([a-z][a-z0-9]{1,19})_([A-Za-z0-9_-]{43})$/;
const GB_SET = new Set(GB_IDENTITIES);

/** The identity a token names (for example "GB-FIXER") and the token, or null for a malformed one. */
export function parseGbBearer(raw) {
  if (typeof raw !== "string") return null;
  const m = TOKEN_RE.exec(raw.trim());
  if (!m) return null;
  const identity = `GB-${m[1].toUpperCase()}`;
  return GB_SET.has(identity) ? { identity, token: raw.trim() } : null;
}

/** The bearer in an Authorization header when it has the gbb_ prefix, else null (OAuth tokens never have it). */
export function gbBearerFromRequest(request) {
  const header = request.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(gbb_\S+)$/i.exec(header.trim());
  return m ? m[1] : null;
}

export function roleOf(identity) {
  return String(identity).replace(/^GB-/, "").toLowerCase();
}

function base64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** A fresh token for `identity` and its hash.  The token is returned once, to the caller, and never stored. */
export async function newGbBearer(identity) {
  if (!GB_SET.has(identity)) throw new Error("not a Grok Bot identity");
  const token = `${GB_BEARER_PREFIX}${roleOf(identity)}_${base64url(crypto.getRandomValues(new Uint8Array(32)))}`;
  return { token, hash: await sha256Hex(token) };
}

// ---------------------------------------------------------------- seat bearers (sbt_)
//
// The same arrangement for the OAuth-capable seats (KNOWN_SEATS), minted in the
// same /admin panel:  one credential per identity, honored by ST-MCP and GR-MCP
// (owner direction 2026-10-23).  A seat bearer is `sbt_<seat>_<43 base64url>`
// with the seat lower-cased (JET-CHAT -> sbt_jet-chat_...), 256 random bits;
// only its SHA-256 is stored, in that seat's own SeatGate under the same
// GB_BEARER_KEY row shape, with the same 90-day TTL and rotate-by-remint rule.
//
// Acceptance on /mcp is NOT automatic:  a seat already has its OAuth grant, and
// a bearer that bypasses the consent flow is a per-seat owner choice recorded in
// the admin UI, never inferred.  GR-MCP accepts sbt_ bearers through the verify
// route for any seat whose admin minted one.

export const SBT_BEARER_PREFIX = "sbt_";

const SBT_TOKEN_RE = /^sbt_([a-z][a-z0-9-]{1,19})_([A-Za-z0-9_-]{43})$/;
const SEAT_SET = new Set(KNOWN_SEATS);

/** The seat a token names (for example "JET-CHAT") and the token, or null for a malformed one. */
export function parseSbtBearer(raw) {
  if (typeof raw !== "string") return null;
  const m = SBT_TOKEN_RE.exec(raw.trim());
  if (!m) return null;
  const identity = m[1].toUpperCase();
  return SEAT_SET.has(identity) ? { identity, token: raw.trim() } : null;
}

/** The bearer in an Authorization header when it has the sbt_ prefix, else null. */
export function sbtBearerFromRequest(request) {
  const header = request.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(sbt_\S+)$/i.exec(header.trim());
  return m ? m[1] : null;
}

/** A fresh seat bearer for `identity` and its hash.  The token is returned once, to the caller, and never stored. */
export async function newSeatBearer(identity) {
  if (!SEAT_SET.has(identity)) throw new Error("not a known seat");
  const token = `${SBT_BEARER_PREFIX}${identity.toLowerCase()}_${base64url(crypto.getRandomValues(new Uint8Array(32)))}`;
  return { token, hash: await sha256Hex(token) };
}
