// GR-MCP shared bearer verification:  POST /internal/gr-bearer-verify.
//
// git-relay (GR-MCP) runs on another host but honors the same admin-minted
// per-identity bearers as this Worker (owner direction 2026-10-23:  one
// credential per identity, shared between ST-MCP and GR-MCP).  It resolves a
// presented bearer by POSTing here.  The raw bearer never transits:  the body
// carries only the identity the token names and the token's SHA-256 hex, which
// is exactly what the SeatGate stores - the Worker compares hashes and answers.
//
// Auth on the route itself reuses the wake scheme (src/wake.js):  the
// X-Agent-Sync-Signature header is the lowercase hex HMAC-SHA256 of the raw
// body bytes keyed with GR_BEARER_VERIFY_KEY, checked before any JSON parse,
// with the same sent_at freshness window.  A verify call is read-only and
// idempotent, so a replay inside the window costs nothing;  the window keeps
// a captured signature useless after five minutes.
//
// Logging discipline matches serveGbBearerMcp:  fixed reason codes only, the
// body and the hash are never echoed, and only an EXPIRED-but-matching bearer
// writes an audit row (wrong guesses are free to send and would push real
// rows out of the capped log).
//
// Pure module for the checker;  the route handler takes its gate factory so
// node --test can drive it without a Worker runtime.

import { safeEqual } from "./policy.js";
import { readBodyBytes, wakeSignature, WAKE_SIGNATURE_HEADER, WAKE_MAX_AGE_S, WAKE_MAX_SKEW_S, WAKE_MIN_KEY_LEN } from "./wake.js";
import { GB_IDENTITIES } from "./delegated-identities.js";
import { KNOWN_SEATS } from "./config.js";

export const GR_VERIFY_CONTRACT = "gr-bearer-verify/1";
export const GR_VERIFY_PATH = "/internal/gr-bearer-verify";
export const GR_VERIFY_MAX_BODY = 1024;
export const GR_VERIFY_KEY_VAR = "GR_BEARER_VERIFY_KEY";

const HEX64_RE = /^[0-9a-f]{64}$/;
const IDENTITY_RE = /^[A-Z][A-Z0-9-]{0,31}$/;
const isInt = (v) => Number.isSafeInteger(v);
const KNOWN = new Set([...GB_IDENTITIES, ...KNOWN_SEATS]);

function jsonResponse(body, status) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

/**
 * Check one signed verify request.  Returns {ok: true, query: {identity, hash}}
 * or {ok: false, status, reason} with a fixed, log-safe reason.
 */
export async function verifyGrRequest({ bytes, signature, key, now = Date.now() }) {
  if (typeof key !== "string" || key.length < WAKE_MIN_KEY_LEN) return { ok: false, status: 404, reason: "not_configured" };
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return { ok: false, status: 400, reason: "empty_body" };
  if (bytes.byteLength > GR_VERIFY_MAX_BODY) return { ok: false, status: 413, reason: "too_large" };
  const given = typeof signature === "string" ? signature.trim() : "";
  if (!HEX64_RE.test(given)) return { ok: false, status: 401, reason: "bad_signature" };
  const expected = await wakeSignature(key, bytes);
  if (!safeEqual(given, expected)) return { ok: false, status: 401, reason: "bad_signature" };

  let query;
  try {
    query = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return { ok: false, status: 400, reason: "bad_json" };
  }
  if (!query || typeof query !== "object" || Array.isArray(query)) return { ok: false, status: 400, reason: "bad_json" };
  if (query.contract !== GR_VERIFY_CONTRACT) return { ok: false, status: 400, reason: "bad_contract" };
  if (typeof query.identity !== "string" || !IDENTITY_RE.test(query.identity) || !KNOWN.has(query.identity)) {
    return { ok: false, status: 400, reason: "bad_identity" };
  }
  if (typeof query.hash !== "string" || !HEX64_RE.test(query.hash)) return { ok: false, status: 400, reason: "bad_hash" };
  if (!isInt(query.sent_at)) return { ok: false, status: 400, reason: "bad_sent_at" };
  const nowS = Math.floor(now / 1000);
  if (nowS - query.sent_at > WAKE_MAX_AGE_S) return { ok: false, status: 401, reason: "stale" };
  if (query.sent_at - nowS > WAKE_MAX_SKEW_S) return { ok: false, status: 401, reason: "future" };
  return { ok: true, query: { identity: query.identity, hash: query.hash } };
}

/**
 * The route.  Answers 200 with {ok: true, identity, expires_at} for a live
 * bearer, or 200 with {ok: false, reason} for a well-formed query whose
 * bearer fails - the route's own auth is the signature, so the introspection
 * answer is data, not an HTTP status.  Route-level failures (bad signature,
 * stale, malformed) keep their 4xx.  `gates` defaults to the real SeatGate
 * factory and is injectable for tests.
 */
export async function serveGrBearerVerify(request, env, gates) {
  const rawKey = env[GR_VERIFY_KEY_VAR];
  const key = typeof rawKey === "string" ? rawKey.trim() : "";
  if (request.method !== "POST") return jsonResponse({ ok: false, reason: "method_not_allowed" }, 405);
  const declared = Number(request.headers.get("content-length") ?? "0");
  const bytes = Number.isFinite(declared) && declared > GR_VERIFY_MAX_BODY ? null : await readBodyBytes(request, GR_VERIFY_MAX_BODY);
  if (bytes === null) return jsonResponse({ ok: false, reason: "too_large" }, 413);
  const checked = await verifyGrRequest({ bytes, signature: request.headers.get(WAKE_SIGNATURE_HEADER), key });
  if (!checked.ok) return jsonResponse({ ok: false, reason: checked.reason }, checked.status);

  const { identity, hash } = checked.query;
  const identityGate = gates(env, identity);
  const verdict = await identityGate.gbBearerCheck(hash);
  if (!verdict.ok) {
    if (verdict.reason === "expired") {
      try { await identityGate.audit({ event: "gb_bearer_refused", reason: "expired", via: "gr-verify" }); } catch { /* a missing row is a gap, not a hole */ }
    }
    return jsonResponse({ ok: false, reason: verdict.reason }, 200);
  }
  const state = await identityGate.getState();
  if (state.paused) return jsonResponse({ ok: false, reason: "paused" }, 200);
  const status = await identityGate.gbBearerStatus();
  return jsonResponse({ ok: true, identity, expires_at: status.expires_at ?? 0 }, 200);
}
