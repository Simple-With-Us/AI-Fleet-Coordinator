// GR-MCP OAuth identity assertions (gr-assert/2):  a hosted OAuth seat carries
// no bearer, so the Worker - after its own grant checks (seat, epoch, phase,
// grant age, pause) pass in serveMcp - mints a SHORT-LIVED, PER-CALL HMAC
// assertion and the relay authenticates it by asking this Worker back over the
// existing signed internal channel (same scheme as /internal/gr-bearer-verify).
//
//   token   gra2_<base64url JSON claims>_<64 hex HMAC-SHA256>
//   claims  {v:"gr-assert/2", aud:"git-relay", seat, iat, exp, jti, action,
//            body: sha256-hex of the exact relay request body, scopes,
//            epoch, approved_at, phase, grant_ref}
//   key     GR_ASSERTION_SIGN_KEY in the DEDICATED Fleet-OPS folder
//           /gr-assert-signing, read only through the dedicated
//           GR_ASSERT_INFISICAL_SERVICE_TOKEN binding:  no runtime reader
//           outside that binding can reach it (the existing
//           st-mcp-runtime-read token reads /gr-front, so the key does
//           NOT live there).  The relay holds no mint-capable material.
//
// Properties the design relies on:
//   - custody:  mint-capable material lives only in the Worker-only folder;
//     the relay verifies by callback, never by holding a key that can mint.
//   - binding:  one assertion covers exactly one relay call - the action and
//     the SHA-256 of its body are inside the signature;  a captured assertion
//     cannot be re-aimed at another action or payload.
//   - scopes:  the grant's own scopes are inside the signature;  the relay
//     refuses a write action when they lack the write scope, so a read-scope
//     grant's assertion can never authorize a write.
//   - one use:  the jti is claimed durably in the seat's SeatGate on first
//     verification;  a second presentation of the same assertion refuses.
//   - revocation:  the verify callback re-runs the seat's gate check with the
//     mint-time epoch/phase/approval, so an epoch bump or pause refuses
//     immediately - the same per-call revocation the bearer path gets.
//   - freshness:  exp-iat is clamped to GR_ASSERT_MAX_TTL_S at both ends, iat
//     may not be in the future beyond skew, expired refuses.
//
// Failure shape:  fail closed.  No key, a short key, an unknown seat, a bad
// TTL or an out-of-contract field mints nothing and the gr_* tools answer
// not_configured, as before.

import { KNOWN_SEATS, SCOPES } from "./config.js";
import { safeEqual } from "./policy.js";
import { resolveSecretIsolated } from "./infisical.js";

export const GR_ASSERT_CONTRACT = "gr-assert/2";
export const GR_ASSERT_AUD = "git-relay";
export const GR_ASSERT_KEY_VAR = "GR_ASSERTION_SIGN_KEY";
export const GR_ASSERT_KEY_PATH = "/gr-assert-signing";
// The narrowly scoped service-token binding the key resolves through
// (isolated custody:  scoped to /gr-assert-signing only;  host grounds and
// mints it).  No other binding name is honored - there is no alias and no
// fallback to the generic read-tools token.
export const GR_ASSERT_TOKEN_BINDING = "GR_ASSERT_INFISICAL_SERVICE_TOKEN";
export const GR_ASSERT_TTL_S = 90;
export const GR_ASSERT_MIN_TTL_S = 30;
export const GR_ASSERT_MAX_TTL_S = 120;
export const GR_ASSERT_SKEW_S = 60;
export const GR_ASSERT_MIN_KEY_LEN = 32;

export const ASSERTION_RE = /^gra2_([A-Za-z0-9_-]+)_([0-9a-f]{64})$/;
// Unanchored family shape, for response echo guards:  a relay answer containing
// ANY assertion-shaped string is refused, whether or not it is this call's own
// token (request-scoped guards can only name their own mint).
export const ASSERTION_FAMILY_RE = /gra2_[A-Za-z0-9_-]+_[0-9a-f]{64}/;
const SEAT_SET = new Set(KNOWN_SEATS);
const SCOPE_SET = new Set(SCOPES);
const SEAT_RE = /^[A-Z][A-Z0-9-]{0,31}$/;
const JTI_RE = /^[A-Za-z0-9_-]{16}$/;
const ACTION_RE = /^[a-z][a-z0-9_]{1,63}$/;
const HEX64_RE = /^[0-9a-f]{64}$/;
const GRANT_REF_RE = /^[0-9a-f]{12}$/;
const SCOPE_RE = /^[a-z][a-z0-9:_-]{0,63}$/;

function base64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function hmacHex(key, text) {
  const cryptoKey = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The canonical signed text:  the exact base64url claims JSON. */
export function assertionPayload(claims) {
  return base64url(new TextEncoder().encode(JSON.stringify(claims)));
}

/** The claims object, or null when any field is out of contract. */
export function buildClaims({ seat, iat, exp, jti, action, bodySha256, scopes, epoch, approvedAt, phase, grantRef }) {
  if (!SEAT_SET.has(seat) || !SEAT_RE.test(seat)) return null;
  if (!Number.isSafeInteger(iat) || !Number.isSafeInteger(exp) || exp <= iat || exp - iat > GR_ASSERT_MAX_TTL_S) return null;
  if (typeof jti !== "string" || !JTI_RE.test(jti)) return null;
  if (typeof action !== "string" || !ACTION_RE.test(action)) return null;
  if (typeof bodySha256 !== "string" || !HEX64_RE.test(bodySha256)) return null;
  if (!Array.isArray(scopes) || scopes.length > 8 || scopes.some((s) => typeof s !== "string" || !SCOPE_RE.test(s) || !SCOPE_SET.has(s))) return null;
  if (!Number.isSafeInteger(epoch) || !Number.isFinite(approvedAt) || !Number.isSafeInteger(phase)) return null;
  if (typeof grantRef !== "string" || !GRANT_REF_RE.test(grantRef)) return null;
  return { v: GR_ASSERT_CONTRACT, aud: GR_ASSERT_AUD, seat, iat, exp, jti, action, body: bodySha256, scopes, epoch, approved_at: approvedAt, phase, grant_ref: grantRef };
}

/**
 * A fresh per-call assertion, or null when minting is not configured or the
 * inputs are out of contract (fail closed;  callers treat null as "no GR").
 * `jti` is injectable only so tests can pin the cross-language vector.
 */
export async function mintGrAssertion({ key, jti = null, now = Date.now(), ttlS = GR_ASSERT_TTL_S, ...rest }) {
  if (typeof key !== "string" || key.length < GR_ASSERT_MIN_KEY_LEN) return null;
  if (!Number.isSafeInteger(ttlS) || ttlS < GR_ASSERT_MIN_TTL_S || ttlS > GR_ASSERT_MAX_TTL_S) return null;
  const iat = Math.floor(now / 1000);
  const jtiValue = jti === null ? base64url(crypto.getRandomValues(new Uint8Array(12))) : String(jti);
  const claims = buildClaims({ ...rest, iat, exp: iat + ttlS, jti: jtiValue });
  if (!claims) return null;
  const payload = assertionPayload(claims);
  const sig = await hmacHex(key, payload);
  return `gra2_${payload}_${sig}`;
}

/**
 * The signing key, resolved at request time from the DEDICATED Fleet-OPS
 * folder /gr-assert-signing through the ISOLATED
 * GR_ASSERT_INFISICAL_SERVICE_TOKEN binding (src/infisical.js
 * resolveSecretIsolated):  never the generic read-tools token, never a
 * Worker binding, never another folder or key name, and never an alias of
 * the binding.  Isolation is against runtime readers - the broad
 * st-mcp-runtime-read token reads /gr-front, which is exactly why the key
 * lives in its own folder;  administrative control-plane access is a
 * separate plane and is not claimed here.  No binding, "off" mode and
 * every Infisical failure answer "" (or throw in required mode) and the
 * mint fails closed.
 */
export async function resolveGrAssertionKey(env) {
  return String(await resolveSecretIsolated(env, { name: GR_ASSERT_KEY_VAR, path: GR_ASSERT_KEY_PATH, tokenBinding: GR_ASSERT_TOKEN_BINDING }) ?? "").trim();
}

/** The claims of an assertion-shaped token, or null.  Structural checks only;
 * the signature is verified by verifyGrAssertionToken, never assumed here. */
export function parseGrAssertion(token) {
  if (typeof token !== "string") return null;
  const m = ASSERTION_RE.exec(token.trim());
  if (!m) return null;
  let claims;
  try {
    const b64 = m[1].replaceAll("-", "+").replaceAll("_", "/");
    claims = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))));
  } catch {
    return null;
  }
  if (!claims || typeof claims !== "object" || Array.isArray(claims)) return null;
  const rebuilt = buildClaims({
    seat: claims.seat, iat: claims.iat, exp: claims.exp, jti: claims.jti,
    action: claims.action, bodySha256: claims.body, scopes: claims.scopes,
    epoch: claims.epoch, approvedAt: claims.approved_at, phase: claims.phase,
    grantRef: claims.grant_ref,
  });
  if (!rebuilt) return null;
  // Field-for-field:  buildClaims canonicalizes, so a claims object with any
  // extra or different key fails the round trip and never reaches the gate.
  if (JSON.stringify(rebuilt) !== JSON.stringify(claims) || assertionPayload(rebuilt) !== m[1]) return null;
  return { claims: rebuilt, payload: m[1], signature: m[2] };
}

/**
 * The Worker's own verification of a presented assertion (the relay holds no
 * key, so it asks the Worker;  src/gr-assert-verify.js).  Returns
 * { ok: true, claims } or { ok: false, reason } with a fixed, log-safe reason.
 * The one-use jti claim and the live gate check happen in the route, not here.
 */
export async function verifyGrAssertionToken({ key, token, action, bodySha256, now = Date.now() }) {
  if (typeof key !== "string" || key.length < GR_ASSERT_MIN_KEY_LEN) return { ok: false, reason: "not_configured" };
  const parsed = parseGrAssertion(token);
  if (!parsed) return { ok: false, reason: "bad_token" };
  const expected = await hmacHex(key, parsed.payload);
  if (!safeEqual(parsed.signature, expected)) return { ok: false, reason: "bad_signature" };
  const { claims } = parsed;
  if (claims.action !== action) return { ok: false, reason: "action_mismatch" };
  if (claims.body !== bodySha256) return { ok: false, reason: "body_mismatch" };
  const nowS = Math.floor(now / 1000);
  if (nowS >= claims.exp) return { ok: false, reason: "expired" };
  if (nowS - claims.iat > GR_ASSERT_MAX_TTL_S) return { ok: false, reason: "stale" };
  if (claims.iat - nowS > GR_ASSERT_SKEW_S) return { ok: false, reason: "future" };
  return { ok: true, claims };
}

/** sha256 hex of a string, for the relay body binding. */
export async function sha256HexText(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
