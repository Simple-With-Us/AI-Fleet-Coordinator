// Jet's additive webhook wake (owner ask 2026-11-04:  "jet needs to wake also
// his own way"):  POST /internal/webhook-wake/<SEAT> sends one `fleet.webhook`
// event to that seat's fleet.webhook subscription.  This is a second TRIGGER
// into the same MCP Events delivery plane, on the whatsapp.message pattern:
// its own event name and its own signing domain, no Zulip carrier, and no
// Zulip-shaped payload is ever fabricated.  The event itself is in
// src/events.js (webhookEventDefinition), offered only while the deploy's
// webhook wake is enabled for the seat.
//
// The route answers 404 unless the seat's WAKE_WEBHOOK_KEY_<SEAT> binding is
// set and the seat is hosted with a wake key (only such a seat is offered
// events at all).  Its gate is its own HMAC:  the header
// X-Agent-Sync-Webhook-Signature is the lowercase hex HMAC-SHA256 of the raw
// body bytes, keyed with the UTF-8 bytes of WAKE_WEBHOOK_KEY_<SEAT>, a Worker
// secret that is never WAKE_HMAC_KEY_<SEAT>:  the Worker refuses to fire when
// the two are equal.  The body is exactly
//   {"contract": "agent-sync-webhook-wake/1", "seat": "<SEAT>", "wake_id": "<id>",
//    "sent_at": <unix seconds>}
// checked over the raw bytes before any JSON parse, with sent_at no more than
// five minutes old or one minute in the future.  The caller is any fleet
// process holding the key - the contract is generic on purpose:  no received
// fact yet names who fires it.  The event carries {id, fired_at} and nothing
// else:  a pure doorbell, like the spike.  It takes NO caller content - a note
// field would arrive next to the model labeled "data", and a label is not
// isolation, so there is no note at all.
//
// Dedupe, budget and custody are the CANONICAL ones, shared with the listener
// wake (seat-state.js):  one wake_seen ledger for both paths, so the same
// wake_id can never cause an event twice across paths, and one hourly
// envelope (the seat's existing wake budget), so this ingress can never
// double it.  Delivery ends the claim by custody:
//   - a receiver answered 2xx               -> "delivered"   (settled, spent)
//   - authoritative rejection / 410 / lapsed -> "rejected"/"gone"/"not_live"
//   - no live subscription                   -> "no_subscriber"
//   - NOTHING was dispatched (no attempt)     -> "retry": the claim is released
//     and the caller may send the SAME wake again - no remote effect exists.
//   - anything uncertain (timeout, 5xx, network after a dispatch, an
//     unexpected error)                       -> "uncertain": the wake is
//     TOMBSTONED, not re-armed.  Callback acceptance is unknown, and firing
//     again - same id or new - could repeat a turn.  The same id stays spent
//     until an operator reconciles custody.
// The event id derives from the wake id exactly as the listener's does
// (eventIdFor(wake_id, sub id)):  the same wake names the same event id on
// either path.  That is not a claim that a receiver dedupes across distinct
// event names - only that this Worker never derives two ids for one wake.
//
// Pure module apart from the fetches it is handed:  `node --test` loads it.

import { safeEqual } from "./policy.js";
import { wakeSignature } from "./wake.js";
import { WEBHOOK_EVENT_NAME, subEventName, deliverEvent, eventIdFor } from "./events.js";
import { WAKE_RATE_LIMIT, WAKE_RATE_WINDOW_MS } from "./seat-state.js";

export const WEBHOOK_WAKE_CONTRACT = "agent-sync-webhook-wake/1";
export const WEBHOOK_WAKE_SIGNATURE_HEADER = "x-agent-sync-webhook-signature";
export const WEBHOOK_WAKE_MAX_BODY = 1024;
export const WEBHOOK_WAKE_MAX_AGE_S = 5 * 60;
export const WEBHOOK_WAKE_MAX_SKEW_S = 60;
// The shortest key the Worker accepts.  `openssl rand -hex 32` gives 64.
export const WEBHOOK_WAKE_MIN_KEY_LEN = 32;
// The webhook path draws on the seat's ONE hourly wake envelope, shared with
// the listener wake and charged inside seat-state's canonical eventClaimWake
// for BOTH paths:  at most this many wakes an hour across both paths.
export const WEBHOOK_WAKE_RATE_LIMIT = WAKE_RATE_LIMIT;
export const WEBHOOK_WAKE_RATE_WINDOW_MS = WAKE_RATE_WINDOW_MS;

const HEX64_RE = /^[0-9a-f]{64}$/;
const WAKE_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
// The fleet secret-scan rule:  caller-controlled text is scanned before it is
// persisted or relayed - wake_id lands in the wake_tomb ledger and in the
// delivered event's data.id.  On a hit the wake is refused and only the seat
// and the refusal code are logged, never the suspected value.
const WAKE_SECRET_RE = /(?:ghp_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,}|xoxb-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16})/;
const PATH_RE = /^\/internal\/webhook-wake\/([A-Z][A-Z0-9-]{0,31})$/;
const BODY_KEYS = "contract,seat,sent_at,wake_id";

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

/** The seat named by a webhook-wake path, or null. */
export function webhookWakePathSeat(pathname) {
  const m = PATH_RE.exec(pathname);
  return m ? m[1] : null;
}

/**
 * The route's admission decision, pure and separately tested (index.js itself
 * is not importable from node):  the route is a 404 ("not_admitted") unless
 * the deploy's flag is on, the path names a hosted seat with BOTH a wake
 * secret binding and a webhook key binding (WEBHOOK_WAKE_SECRETS holds JET
 * only - that IS the JET-only enforcement, matching the event offer), the
 * seat's wake key resolves, and the webhook key is set.  A webhook key equal
 * to ANY seat's wake key is "key_reused":  the route refuses to fire, the
 * signing domains stay separate.  Returns null when admitted, else the reason.
 */
export function webhookWakeAdmission({ enabled, seat, hostedSeats, bound, wakeKey, key, wakeKeys }) {
  if (!enabled) return "not_admitted";
  if (!seat || !bound || !hostedSeats.includes(seat)) return "not_admitted";
  if (!wakeKey || !key) return "not_admitted";
  if (wakeKeys.includes(key)) return "key_reused";
  return null;
}

/** The key, trimmed, when it is long enough to use, else "". */
export function webhookWakeKey(raw) {
  const key = typeof raw === "string" ? raw.trim() : "";
  return key.length >= WEBHOOK_WAKE_MIN_KEY_LEN ? key : "";
}

/**
 * Check one webhook-wake request.  `bytes` is the raw body (at most
 * WEBHOOK_WAKE_MAX_BODY), `signature` the header value, `key` the seat's
 * webhook key, `seat` the seat from the path.  Returns {ok: true, wake} or
 * {ok: false, status, reason}.  The reason is a fixed code, safe to log;
 * nothing from the body is echoed.
 */
export async function verifyWebhookWake({ bytes, signature, key, seat, now = Date.now() }) {
  if (!webhookWakeKey(key)) return { ok: false, status: 404, reason: "not_configured" };
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return { ok: false, status: 400, reason: "empty_body" };
  if (bytes.byteLength > WEBHOOK_WAKE_MAX_BODY) return { ok: false, status: 413, reason: "too_large" };
  const given = typeof signature === "string" ? signature.trim() : "";
  if (!HEX64_RE.test(given)) return { ok: false, status: 401, reason: "bad_signature" };
  if (!safeEqual(given, await wakeSignature(webhookWakeKey(key), bytes))) return { ok: false, status: 401, reason: "bad_signature" };

  let wake;
  try {
    wake = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return { ok: false, status: 400, reason: "bad_json" };
  }
  if (!wake || typeof wake !== "object" || Array.isArray(wake)) return { ok: false, status: 400, reason: "bad_json" };
  if (Object.keys(wake).sort().join(",") !== BODY_KEYS) return { ok: false, status: 400, reason: "bad_fields" };
  if (wake.contract !== WEBHOOK_WAKE_CONTRACT) return { ok: false, status: 400, reason: "bad_contract" };
  if (wake.seat !== seat) return { ok: false, status: 400, reason: "seat_mismatch" };
  if (typeof wake.wake_id !== "string" || !WAKE_ID_RE.test(wake.wake_id)) return { ok: false, status: 400, reason: "bad_wake_id" };
  if (!Number.isSafeInteger(wake.sent_at)) return { ok: false, status: 400, reason: "bad_sent_at" };
  const nowS = Math.floor(now / 1000);
  if (nowS - wake.sent_at > WEBHOOK_WAKE_MAX_AGE_S) return { ok: false, status: 401, reason: "stale" };
  if (wake.sent_at - nowS > WEBHOOK_WAKE_MAX_SKEW_S) return { ok: false, status: 401, reason: "future" };
  if (WAKE_SECRET_RE.test(JSON.stringify(wake))) {
    console.log(JSON.stringify({ event: "webhook_wake", seat, verdict: "secret_in_wake_id" }));
    return { ok: false, status: 400, reason: "secret_in_wake_id" };
  }
  return { ok: true, wake };
}

/** The event `data`:  the wake's id and when it was fired.  Nothing else, ever. */
export function webhookWakeData(wake) {
  return { id: wake.wake_id, fired_at: iso(wake.sent_at * 1000) };
}

/**
 * Deliver one claimed webhook wake to the seat's `fleet.webhook` subscriptions
 * (`claim` from SeatGate.webhookWakeClaim, filtered again here so a
 * zulip.mention or whatsapp.message subscription can never receive it), then
 * end the claim with SeatGate.eventSettleWake by the custody contract in the
 * header:  uncertain NEVER re-arms.  Same delivery as a wake (events.js
 * deliverEvent):  Standard Webhooks signed, the access re-check before the
 * attempt, exactly ONE attempt (backoff: []) - an internal retry could repeat
 * an uncertain POST the tombstone cannot undo.  The claim is marked
 * dispatched before the first POST, so a crash after dispatch fences the id
 * against a blind lease-expiry reclaim.  Never throws:  anything unexpected
 * settles "uncertain" (a dispatch may have happened).  Returns
 * {outcome, subscribers, delivered}.
 */
export async function deliverWebhookWake({ gate, wake, claim, fetchFn, now = Date.now, sleep, deadline = Infinity }) {
  const mine = claim.subs.filter((sub) => subEventName(sub) === WEBHOOK_EVENT_NAME);
  // The UNKNOWN fence BEFORE the first dispatch:  the claim is marked
  // dispatched, so if the Worker dies after a POST but before the settle, the
  // expired lease is never blindly reclaimed (seat-state eventClaimWake).  If
  // the mark did not land, nothing is dispatched and the claim is released -
  // provably no remote effect, so the SAME wake may be sent again.
  if (mine.length > 0) {
    let marked;
    try {
      marked = await gate.eventMarkDispatched({ wakeId: wake.wake_id, token: claim.token });
    } catch {
      marked = { ok: false };
    }
    if (!marked.ok) {
      try {
        await gate.eventSettleWake({ wakeId: wake.wake_id, token: claim.token, outcome: "retry" });
      } catch {
        console.log(JSON.stringify({ event: "webhook_wake_settle_failed" }));
      }
      return { outcome: "retry", subscribers: mine.length, delivered: 0 };
    }
  }
  const data = webhookWakeData(wake);
  let results;
  try {
    results = await Promise.all(
      mine.map(async (sub) => {
        // The listener's own derivation:  one wake id names one event id.
        const event = { eventId: await eventIdFor(wake.wake_id, sub.id), name: WEBHOOK_EVENT_NAME, timestamp: data.fired_at, data, cursor: null };
        let result;
        try {
          // backoff: [] - exactly ONE attempt.  deliverEvent's internal retry
          // could repeat an uncertain POST (timeout, 5xx) that the tombstone
          // cannot undo, so this path never retries inside delivery:  one
          // attempt, then custody classification.
          result = await deliverEvent(fetchFn, sub, event, { now, deadline, backoff: [], isLive: () => gate.eventSubLive({ subId: sub.id }), ...(sleep ? { sleep } : {}) });
        } catch {
          result = { outcome: "failed", status: null, attempts: 1 };
        }
        try {
          await gate.eventDeliveryResult({ subId: sub.id, outcome: result.outcome, status: result.status, attempts: result.attempts, reason: result.reason, wakeRef: `webhook:${wake.wake_id.slice(0, 8)}`, messageId: null });
        } catch {
          console.log(JSON.stringify({ event: "webhook_wake_result_record_failed" }));
        }
        return result;
      }),
    );
  } catch {
    results = [{ outcome: "failed", attempts: 1 }];
  }
  const delivered = results.filter((r) => r.outcome === "delivered").length;
  // Custody:  a 2xx settles;  a provable zero-dispatch releases for the SAME
  // wake;  anything else with a dispatch behind it is uncertain and tombs.
  let outcome;
  if (mine.length === 0) outcome = "no_subscriber";
  else if (delivered > 0) outcome = "delivered";
  else if (results.every((r) => (r.attempts ?? 0) === 0)) {
    // Nothing was dispatched to anyone:  no remote effect exists.  Dead
    // subscriptions settle;  anything else releases the claim so the caller
    // may send the SAME wake again.
    outcome = results.every((r) => r.outcome === "not_live") ? "not_live" : "retry";
  } else if (results.every((r) => ["rejected", "gone", "not_live"].includes(r.outcome))) {
    // Every dispatch met an authoritative answer (4xx / 410 / lapsed sub).
    outcome = ["rejected", "gone", "not_live"].find((o) => results.some((r) => r.outcome === o));
  } else {
    // A dispatch happened and its fate is unknown (timeout, 5xx, network).
    outcome = "uncertain";
  }
  try {
    await gate.eventSettleWake({ wakeId: wake.wake_id, token: claim.token, outcome, delivered });
  } catch {
    console.log(JSON.stringify({ event: "webhook_wake_settle_failed" }));
  }
  return { outcome, subscribers: mine.length, delivered };
}

/**
 * The answer for one webhook wake:  {status, body, retryAfterS}.  No secrets,
 * no callback URLs, only counts.
 *   duplicate     200 {ok: true, duplicate: true}
 *   in_flight     503 {ok: false, in_flight: true}, with Retry-After
 *   rate_limited  429 {ok: false, rate_limited: true}, with Retry-After
 *   paused        202 {ok: true, paused: true, subscribers: 0}
 *   retry         503 {ok: false, retry: true, ...}:  nothing was dispatched;
 *                 send the SAME wake again
 *   any other     202 {ok: true, subscribers, delivered, outcome}, outcome one
 *                 of delivered / uncertain / rejected / gone / not_live /
 *                 no_subscriber.  "uncertain" means the wake is tombstoned:
 *                 custody is held, do NOT fire it again.
 */
export function webhookWakeReply(result) {
  if (result.verdict === "duplicate") return { status: 200, body: { ok: true, duplicate: true } };
  if (result.verdict === "in_flight") return { status: 503, body: { ok: false, in_flight: true }, retryAfterS: Math.max(1, result.retryAfterS ?? 1) };
  if (result.verdict === "rate_limited") return { status: 429, body: { ok: false, rate_limited: true }, retryAfterS: Math.max(1, result.retryAfterS ?? 1) };
  if (result.verdict === "paused") return { status: 202, body: { ok: true, paused: true, subscribers: 0 } };
  if (result.outcome === "retry") return { status: 503, body: { ok: false, retry: true, subscribers: result.subscribers, delivered: 0 } };
  return { status: 202, body: { ok: true, subscribers: result.subscribers, delivered: result.delivered, outcome: result.outcome } };
}
