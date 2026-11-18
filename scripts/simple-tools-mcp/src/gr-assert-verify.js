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
// X-Simple-Tools-Signature header is the lowercase hex HMAC-SHA256 of the raw
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
// a SeatGate audit row (wrong guesses are free to send and would push real
// rows out of that capped log).
//
// Every refusal, and a key that could not be read, also goes to the Worker's
// diagnostics log (AFC-844), through the injected `recordRefusal`:  one row of
// kind `gr_assert`, a fixed reason code, the HTTP status, and the seat only
// once the assertion's own signature has checked out.  That log aggregates by
// kind, reason and seat;  index.js also coalesces each key to one write a
// minute per isolate (src/diagnostic-coalesce.js) and hands it to waitUntil,
// so a flood raises a count, cannot use up the shared diagnostics budget, and
// never delays an answer.  Any seat can read it with worker_diagnostics.
// Without it a refused call left no reason anywhere:  the relay reports every
// refusal as bearer_rejected and records no verifier reason (AFC-694, Nov 18).
// The row's reason is the answer's, with two refinements that the relay never
// sees.  A grant_invalid records its cause:  `grant_invalid:no_live_grant` for
// the provider's grant list, or `grant_invalid:<gate code>` for the SeatGate
// check.  The assertion's own bad_token, bad_signature and not_configured are
// `assertion:<code>`, apart from the route-level codes of the same name.  A
// refusal the relay makes without calling here (its 30 s refusal cache, a
// write by a grant without zulip:write, or a proof mismatch it checks itself)
// leaves no row, and a Worker error is a `provider` row instead.  Recording
// never changes the answer, and a failing log is ignored.

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

/** The diagnostics kind of a refused verify (AFC-844), read with worker_diagnostics. */
export const GR_ASSERT_DIAGNOSTIC_KIND = "gr_assert";
// The diagnostics log's own seat rule (src/seat-state.js SAFE_DIAGNOSTIC_SEAT):  a seat
// that does not fit is recorded as none, so the row is still written.
const DIAGNOSTIC_SEAT_RE = /^[A-Z][A-Z0-9_-]{0,39}$/;
// Reason codes the assertion check shares with the route's own call checks:  the
// diagnostics row prefixes them with `assertion:` so the two stay apart.
const ASSERTION_SHARED_CODES = new Set(["bad_token", "bad_signature", "not_configured"]);

function diagnosticSeat(seat) {
  return typeof seat === "string" && DIAGNOSTIC_SEAT_RE.test(seat) ? seat : null;
}

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
  // A named local, not an inline expression:  git-relay's generic assigned-secret rule
  // reads `token: <16+ chars>` as a literal secret and refuses the whole file (AFC-844).
  const presented = parsed.data.token.trim();
  return {
    ok: true,
    query: { token: presented, action: parsed.data.action, bodySha256: parsed.data.body_sha256 },
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
 * `lookup` (a KV list) behind a cache of positive answers, in the identity
 * cache so an admin eviction of the seat drops them.  A negative answer is never
 * cached:  it stays retryable, and a new grant works at once.  `lookup` returns
 * false, true, or { expiresAt } (the grant's expiry in seconds, when it has
 * one), and a positive entry never outlives that expiry.
 */
export async function cachedGrantExists(cache, { seat, grantRef, ttlMs, lookup }) {
  const key = `grant:${seat}:${grantRef}`;
  if (cache.get(key)) return true;
  const since = cache.now();
  const found = await lookup();
  if (!found) return false;
  let ttl = ttlMs;
  const expiresAt = typeof found === "object" ? found.expiresAt : undefined;
  // One clock reading caps the window at the grant's expiry and starts it.
  const now = cache.now();
  if (typeof expiresAt === "number" && Number.isFinite(expiresAt)) ttl = Math.min(ttl, expiresAt * 1000 - now);
  if (ttl > 0) cache.set(key, { seat, via: "grant" }, ttl, now, { since });
  return true;
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
export async function serveGrAssertVerify(request, env, gates, { resolveKey = resolveGrVerifyKey, resolveSignKey = resolveGrAssertionKey, hostedSeats = [], eventsOnlySeats = [], grantExists = async () => false, recordRefusal = async () => {} } = {}) {
  // Every refusal leaves through here:  the answer is exactly the one the route
  // always gave, and recording it never changes or delays it (index.js hands the
  // write to waitUntil).
  const record = async (detail, status, seat = null) => {
    try {
      await recordRefusal({ kind: GR_ASSERT_DIAGNOSTIC_KIND, reason: detail, seat: diagnosticSeat(seat), status });
    } catch {
      // A diagnostics outage must not turn a refusal into a route error.
    }
  };
  const refuse = async (reason, status, { seat = null, detail = reason } = {}) => {
    await record(detail, status, seat);
    return jsonResponse({ ok: false, reason }, status);
  };
  let key;
  try {
    key = await resolveKey(env);
  } catch (error) {
    // A key that cannot be read stays the route error it always was;  the row says so.
    await record("key_unavailable", 500);
    throw error;
  }
  if (request.method !== "POST") return refuse("method_not_allowed", 405);
  const declared = Number(request.headers.get("content-length") ?? "0");
  const bytes = Number.isFinite(declared) && declared > GR_ASSERT_VERIFY_MAX_BODY ? null : await readBodyBytes(request, GR_ASSERT_VERIFY_MAX_BODY);
  if (bytes === null) return refuse("too_large", 413);
  const checked = await verifyGrAssertRequest({ bytes, signature: request.headers.get(WAKE_SIGNATURE_HEADER), key });
  if (!checked.ok) return refuse(checked.reason, checked.status);

  // The mint key, from the Worker-only folder.  Its absence fails closed:
  // nothing here can mint, and nothing else can verify.
  let signKey;
  try {
    signKey = await resolveSignKey(env);
  } catch (error) {
    await record("sign_key_unavailable", 500);
    throw error;
  }
  const { token, action, bodySha256 } = checked.query;
  // From here on a refusal is about the assertion, not the route's own call.  The
  // three codes the route also uses get an `assertion:` detail, so a scanner's
  // junk route signatures cannot hide a mint-key fault in the same row.
  if (!ASSERTION_RE.test(token)) return refuse("bad_token", 200, { detail: "assertion:bad_token" });
  const verdict = await verifyGrAssertionToken({ key: signKey, token, action, bodySha256 });
  // verifyGrAssertionToken names the seat only past the assertion's own signature.
  if (!verdict.ok) {
    const detail = ASSERTION_SHARED_CODES.has(verdict.reason) ? `assertion:${verdict.reason}` : verdict.reason;
    return refuse(verdict.reason, 200, { seat: verdict.seat, detail });
  }

  const { claims } = verdict;
  if (!hostedSeats.includes(claims.seat)) return refuse("bad_seat", 200, { seat: claims.seat });
  if (eventsOnlySeats.includes(claims.seat)) return refuse("events_only", 200, { seat: claims.seat });
  const seatGate = gates(env, claims.seat);
  // The signed grant_ref must still name a LIVE grant of this seat - checked
  // BEFORE the one-use jti is claimed (kody 4428074149):  a transient
  // grant-list miss refuses as grant_invalid and stays retryable instead of
  // burning the nonce (a retry would read as a replay) and filling the
  // bounded jti store.  A grant deleted or revoked in the provider (admin
  // revoke, the one-grant-per-seat replacement at consent, a
  // refresh-mismatch takedown) or one past its expiresAt refuses here even
  // when the gate row would still pass.  The default refuses (fail closed).
  // KV deletion is eventually consistent - this filters dead rows;  the
  // immediate authoritative revocation is the epoch/pause check inside the
  // claim transaction below.  Residual race, documented:  a grant revoked in
  // the sub-second between this check and the claim is accepted by this
  // call;  every assertion is per-call and expires within 120 seconds, and
  // the next call mints fresh and re-runs both checks.
  if (!(await grantExists(claims.seat, claims.grant_ref))) {
    return refuse("grant_invalid", 200, { seat: claims.seat, detail: "grant_invalid:no_live_grant" });
  }
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
    const detail = gateResult.reason === "grant_invalid" && typeof gateResult.gate === "string"
      ? `grant_invalid:${gateResult.gate}`
      : gateResult.reason;
    return refuse(gateResult.reason, 200, { seat: claims.seat, detail });
  }
  return jsonResponse({ ok: true, seat: claims.seat }, 200);
}
