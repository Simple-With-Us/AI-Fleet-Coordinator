// GR-MCP OAuth assertion verification:  POST /internal/gr-assert-verify.
//
// git-relay holds no mint-capable key (the GR_ASSERTION_SIGN_KEY lives only in
// the dedicated Fleet-OPS folder /gr-assert-signing, reachable by no runtime
// reader outside the Worker's dedicated binding), so it resolves a presented
// gra2_ assertion by POSTing here.  The relay's request carries the full
// token - the Worker is the ONLY holder of the signing key, so verification
// can only happen here - plus the action and body SHA-256 the relay computed
// from the exact request it is authenticating.
//
// Auth on the route itself reuses the wake scheme exactly as
// /internal/gr-bearer-verify does (src/gr-verify.js):  the
// X-Agent-Sync-Signature header is the lowercase hex HMAC-SHA256 of the raw
// body bytes keyed with GR_BEARER_VERIFY_KEY, checked before any JSON parse,
// with the same sent_at freshness window.
//
// After the signature checks, this route is the authoritative per-call gate:
//   - the assertion's own HMAC, window, action and body binding
//     (src/gr-assert.js verifyGrAssertionToken);
//   - the seat's live gate check with the mint-time epoch/phase/approval, so
//     an epoch bump (revocation) or a fresh grant elsewhere refuses
//     immediately - the same per-call revocation the bearer path gets;
//   - a paused or events-only (wake-only) seat refuses;
//   - the jti is claimed durably in the seat's SeatGate, so an assertion is
//     one-use and a replay refuses.
//
// Logging discipline matches serveGrBearerVerify:  fixed reason codes only,
// the token is never echoed, and only a REPLAYED well-formed assertion writes
// an audit row (wrong guesses are free to send and would push real rows out
// of the capped log).

import { safeEqual } from "./policy.js";
import { readBodyBytes, wakeSignature, WAKE_SIGNATURE_HEADER, WAKE_MAX_AGE_S, WAKE_MAX_SKEW_S, WAKE_MIN_KEY_LEN } from "./wake.js";
import { resolveGrVerifyKey } from "./gr-verify.js";
import { resolveGrAssertionKey, verifyGrAssertionToken, ASSERTION_RE } from "./gr-assert.js";
import { z } from "zod";

export const GR_ASSERT_VERIFY_CONTRACT = "gr-assert-verify/1";
export const GR_ASSERT_VERIFY_PATH = "/internal/gr-assert-verify";
export const GR_ASSERT_VERIFY_MAX_BODY = 4096;

const HEX64_RE = /^[0-9a-f]{64}$/;
const ACTION_RE = /^[a-z][a-z0-9_]{1,63}$/;

// The signed payload's strict shape.  sent_at is an epoch-seconds integer.
const GrAssertVerifySchema = z
  .object({
    contract: z.literal(GR_ASSERT_VERIFY_CONTRACT),
    token: z.string().min(1).max(GR_ASSERT_VERIFY_MAX_BODY),
    action: z.string().regex(ACTION_RE),
    body_sha256: z.string().regex(HEX64_RE),
    sent_at: z.number().int(),
  })
  .strict();

/** First-issue field -> the route's fixed 400 reason (unknown keys -> bad_json). */
const FIELD_REASONS = { contract: "bad_contract", token: "bad_token", action: "bad_action", body_sha256: "bad_body_sha256", sent_at: "bad_sent_at" };

function jsonResponse(body, status) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

/**
 * Check one signed verify request.  Returns { ok: true, query } or
 * { ok: false, status, reason } with a fixed, log-safe reason.
 */
export async function verifyGrAssertRequest({ bytes, signature, key, now = Date.now() }) {
  if (typeof key !== "string" || key.length < WAKE_MIN_KEY_LEN) return { ok: false, status: 404, reason: "not_configured" };
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return { ok: false, status: 400, reason: "empty_body" };
  if (bytes.byteLength > GR_ASSERT_VERIFY_MAX_BODY) return { ok: false, status: 413, reason: "too_large" };
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
  const parsed = GrAssertVerifySchema.safeParse(query);
  if (!parsed.success) {
    const field = parsed.error.issues[0]?.path?.[0];
    return { ok: false, status: 400, reason: FIELD_REASONS[field] ?? "bad_json" };
  }
  const nowS = Math.floor(now / 1000);
  if (nowS - parsed.data.sent_at > WAKE_MAX_AGE_S) return { ok: false, status: 401, reason: "stale" };
  if (parsed.data.sent_at - nowS > WAKE_MAX_SKEW_S) return { ok: false, status: 401, reason: "future" };
  return {
    ok: true,
    query: { token: parsed.data.token.trim(), action: parsed.data.action, bodySha256: parsed.data.body_sha256 },
  };
}

/**
 * Is a provider grant summary live right now?  Grounded in the installed
 * @cloudflare/workers-oauth-provider 1.2.3 source (dist/oauth-provider.js):
 * listUserGrants returns raw KV rows with NO expiry filtering, and
 * GrantSummary.expiresAt is an optional UNIX-SECONDS timestamp - absent when
 * no TTL is configured (the grant then does not expire).  This Worker's
 * provider sets refreshTokenTTL, so live grants carry expiresAt;  a row
 * whose expiresAt is past is a dead grant the KV TTL has not swept yet.
 *
 * Honesty note:  KV deletion is eventually consistent, so a revoked grant's
 * row may still LIST briefly;  this check is a best-effort dead-row filter.
 * The immediate, authoritative revocation signal is the SeatGate epoch/pause
 * check in the claim transaction, which every revocation path performs first.
 */
export function grantSummaryIsLive(grant, now = Date.now()) {
  if (!grant || typeof grant.id !== "string" || grant.id.length === 0) return false;
  if (grant.expiresAt === undefined) return true;
  if (typeof grant.expiresAt !== "number" || !Number.isFinite(grant.expiresAt)) return false;
  return grant.expiresAt > Math.floor(now / 1000);
}

/**
 * The route.  Answers 200 with { ok: true, seat } for a live, unspent,
 * request-bound assertion, or 200 with { ok: false, reason } for a
 * well-formed query whose assertion fails - the route's own auth is the
 * signature, so the introspection answer is data, not an HTTP status.
 * Route-level failures (bad signature, stale, malformed) keep their 4xx.
 * `gates` defaults to the real SeatGate factory and both key resolvers are
 * injectable, so node --test drives this without a Worker runtime.
 */
export async function serveGrAssertVerify(request, env, gates, { resolveKey = resolveGrVerifyKey, resolveSignKey = resolveGrAssertionKey, hostedSeats = [], eventsOnlySeats = [], grantExists = async () => false } = {}) {
  const key = await resolveKey(env);
  if (request.method !== "POST") return jsonResponse({ ok: false, reason: "method_not_allowed" }, 405);
  const declared = Number(request.headers.get("content-length") ?? "0");
  const bytes = Number.isFinite(declared) && declared > GR_ASSERT_VERIFY_MAX_BODY ? null : await readBodyBytes(request, GR_ASSERT_VERIFY_MAX_BODY);
  if (bytes === null) return jsonResponse({ ok: false, reason: "too_large" }, 413);
  const checked = await verifyGrAssertRequest({ bytes, signature: request.headers.get(WAKE_SIGNATURE_HEADER), key });
  if (!checked.ok) return jsonResponse({ ok: false, reason: checked.reason }, checked.status);

  // The mint key, from the Worker-only folder.  Its absence fails closed:
  // nothing here can mint, and nothing else can verify.
  const signKey = await resolveSignKey(env);
  const { token, action, bodySha256 } = checked.query;
  if (!ASSERTION_RE.test(token)) return jsonResponse({ ok: false, reason: "bad_token" }, 200);
  const verdict = await verifyGrAssertionToken({ key: signKey, token, action, bodySha256 });
  if (!verdict.ok) return jsonResponse({ ok: false, reason: verdict.reason }, 200);

  const { claims } = verdict;
  if (!hostedSeats.includes(claims.seat)) return jsonResponse({ ok: false, reason: "bad_seat" }, 200);
  if (eventsOnlySeats.includes(claims.seat)) return jsonResponse({ ok: false, reason: "events_only" }, 200);
  const seatGate = gates(env, claims.seat);
  // Revocation equivalence + one use in ONE Durable Object RPC
  // (src/seat-state.js claimGrAssert):  the gate check and the jti claim are
  // a single storage transaction under the object's input gate, so the two
  // can never be split by a concurrent verify.  A refusal spends nothing -
  // a paused-then-unpaused assertion still verifies once.
  const gateResult = await seatGate.claimGrAssert({ epoch: claims.epoch, approvedAt: claims.approved_at, phase: claims.phase, jti: claims.jti, expiresAt: claims.exp * 1000 });
  if (!gateResult.ok) {
    if (gateResult.reason === "replayed") {
      // The replay refusal audit is required:  if the write fails the route
      // errors and the relay fails closed, so a missing row can never pass
      // silently.  A replay is a well-formed, correctly signed token, so this
      // row is a real security signal, unlike free wrong guesses.
      await seatGate.audit({ event: "gr_assert_refused", reason: "replayed", via: "gr-assert-verify" });
    }
    return jsonResponse({ ok: false, reason: gateResult.reason }, 200);
  }
  // The signed grant_ref must still name a LIVE grant of this seat:  a grant
  // deleted or revoked in the provider (admin revoke, the one-grant-per-seat
  // replacement at consent, a refresh-mismatch takedown) or one past its
  // expiresAt refuses here even when the gate row would still pass.  The
  // default refuses (fail closed).  KV deletion is eventually consistent -
  // this filters dead rows;  the immediate authoritative revocation is the
  // epoch/pause claim above.
  if (!(await grantExists(claims.seat, claims.grant_ref))) {
    return jsonResponse({ ok: false, reason: "grant_invalid" }, 200);
  }
  return jsonResponse({ ok: true, seat: claims.seat }, 200);
}
