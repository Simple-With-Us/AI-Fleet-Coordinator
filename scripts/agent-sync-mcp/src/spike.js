// AFC-477 spike (temporary):  can one ChatGPT conversation hold a SECOND MCP
// Events subscription on this Worker and be woken by it, while its
// zulip.mention subscription keeps working?  This module is the firing half:
// POST /internal/spike/<SEAT> sends one `fleet.spike.ping` to that seat's
// spike subscription.  The event itself is in src/events.js
// (spikeEventDefinition), offered only while SPIKE_EXTRA_EVENTS is "on".
// Spec:  docs/protocols/agent-sync-mcp.md section 3.12, item 16.
//
// The route answers 404 unless SPIKE_EXTRA_EVENTS is "on", SPIKE_HMAC_KEY is
// set, and the seat is hosted with a wake key (only such a seat is offered
// events at all).  Its gate is its own HMAC:  the header
// X-Agent-Sync-Spike-Signature is the lowercase hex HMAC-SHA256 of the raw
// body bytes, keyed with the UTF-8 bytes of SPIKE_HMAC_KEY, a Worker secret
// that is never WAKE_HMAC_KEY_<SEAT>:  the Worker refuses to fire when the two
// are equal.  The body is exactly
//   {"contract": "agent-sync-spike/1", "seat": "<SEAT>", "spike_id": "<uuid>", "sent_at": <unix seconds>}
// checked over the raw bytes before any JSON parse, with sent_at no more than
// five minutes old or one minute in the future.  SeatGate.spikeClaim dedupes
// on spike_id for an hour (a replay is a 200 duplicate) and allows at most
// SPIKE_RATE_LIMIT pings an hour (429 after that).  The event carries
// {id, fired_at} and nothing else:  no Zulip data, no message, no content.
//
// Pure module apart from the fetches it is handed:  `node --test` loads it.

import { safeEqual } from "./policy.js";
import { wakeSignature } from "./wake.js";
import { SPIKE_EVENT_NAME, subEventName, deliverEvent, eventIdFor, wakeOutcome } from "./events.js";

export const SPIKE_CONTRACT = "agent-sync-spike/1";
export const SPIKE_SIGNATURE_HEADER = "x-agent-sync-spike-signature";
export const SPIKE_MAX_BODY = 1024;
export const SPIKE_MAX_AGE_S = 5 * 60;
export const SPIKE_MAX_SKEW_S = 60;
// The shortest key the Worker accepts.  `openssl rand -hex 32` gives 64.
export const SPIKE_MIN_KEY_LEN = 32;
// At most this many pings per seat per window.  The dedupe remembers ids for
// the same window, far longer than the sent_at window.
export const SPIKE_RATE_LIMIT = 3;
export const SPIKE_RATE_WINDOW_MS = 60 * 60 * 1000;

const HEX64_RE = /^[0-9a-f]{64}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PATH_RE = /^\/internal\/spike\/([A-Z][A-Z0-9-]{0,31})$/;
const BODY_KEYS = Object.freeze(["contract", "seat", "sent_at", "spike_id"].sort());

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

/** The seat named by a spike path, or null. */
export function spikePathSeat(pathname) {
  const m = PATH_RE.exec(pathname);
  return m ? m[1] : null;
}

/** The key, trimmed, when it is long enough to use, else "". */
export function spikeKey(raw) {
  const key = typeof raw === "string" ? raw.trim() : "";
  return key.length >= SPIKE_MIN_KEY_LEN ? key : "";
}

/**
 * Check one spike request.  `bytes` is the raw body (at most SPIKE_MAX_BODY),
 * `signature` the header value, `key` SPIKE_HMAC_KEY, `seat` the seat from the
 * path.  Returns {ok: true, spike} or {ok: false, status, reason}.  The reason
 * is a fixed code, safe to log;  nothing from the body is echoed.
 */
export async function verifySpike({ bytes, signature, key, seat, now = Date.now() }) {
  if (!spikeKey(key)) return { ok: false, status: 404, reason: "not_configured" };
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return { ok: false, status: 400, reason: "empty_body" };
  if (bytes.byteLength > SPIKE_MAX_BODY) return { ok: false, status: 413, reason: "too_large" };
  const given = typeof signature === "string" ? signature.trim() : "";
  if (!HEX64_RE.test(given)) return { ok: false, status: 401, reason: "bad_signature" };
  if (!safeEqual(given, await wakeSignature(spikeKey(key), bytes))) return { ok: false, status: 401, reason: "bad_signature" };

  let spike;
  try {
    spike = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return { ok: false, status: 400, reason: "bad_json" };
  }
  if (!spike || typeof spike !== "object" || Array.isArray(spike)) return { ok: false, status: 400, reason: "bad_json" };
  if (Object.keys(spike).sort().join(",") !== BODY_KEYS.join(",")) return { ok: false, status: 400, reason: "bad_fields" };
  if (spike.contract !== SPIKE_CONTRACT) return { ok: false, status: 400, reason: "bad_contract" };
  if (spike.seat !== seat) return { ok: false, status: 400, reason: "seat_mismatch" };
  if (typeof spike.spike_id !== "string" || !UUID_RE.test(spike.spike_id)) return { ok: false, status: 400, reason: "bad_spike_id" };
  if (!Number.isSafeInteger(spike.sent_at)) return { ok: false, status: 400, reason: "bad_sent_at" };
  const nowS = Math.floor(now / 1000);
  if (nowS - spike.sent_at > SPIKE_MAX_AGE_S) return { ok: false, status: 401, reason: "stale" };
  if (spike.sent_at - nowS > SPIKE_MAX_SKEW_S) return { ok: false, status: 401, reason: "future" };
  return { ok: true, spike };
}

/** The event `data`:  the ping's id and when it was fired.  Nothing else, ever. */
export function spikeData(spike) {
  return { id: spike.spike_id, fired_at: iso(spike.sent_at * 1000) };
}

/**
 * Deliver one claimed spike to the seat's `fleet.spike.ping` subscriptions
 * (`subs` from SeatGate.spikeClaim, filtered again here so a zulip.mention
 * subscription can never receive it).  Same delivery as a wake (events.js
 * deliverEvent):  Standard Webhooks signed, the access re-check before every
 * attempt, at most two attempts inside `deadline`.  Each result is recorded
 * with SeatGate.eventDeliveryResult (410 drops the subscription).  Never
 * throws.  Returns {outcome, subscribers, delivered}, outcome as wakeOutcome.
 */
export async function deliverSpike({ gate, spike, subs, fetchFn, now = Date.now, sleep, deadline = Infinity }) {
  const mine = subs.filter((sub) => subEventName(sub) === SPIKE_EVENT_NAME);
  const data = spikeData(spike);
  const ref = `spike:${spike.spike_id.slice(0, 8)}`;
  let results;
  try {
    results = await Promise.all(
      mine.map(async (sub) => {
        const event = { eventId: await eventIdFor(`spike:${spike.spike_id}`, sub.id), name: SPIKE_EVENT_NAME, timestamp: data.fired_at, data, cursor: null };
        let result;
        try {
          result = await deliverEvent(fetchFn, sub, event, { now, deadline, isLive: () => gate.eventSubLive({ subId: sub.id }), ...(sleep ? { sleep } : {}) });
        } catch {
          result = { outcome: "failed", status: null, attempts: 0 };
        }
        try {
          await gate.eventDeliveryResult({ subId: sub.id, outcome: result.outcome, status: result.status, attempts: result.attempts, reason: result.reason, wakeRef: ref, messageId: null });
        } catch {
          console.log(JSON.stringify({ event: "spike_result_record_failed" }));
        }
        return { subId: sub.id, ...result };
      }),
    );
  } catch {
    results = [{ outcome: "failed" }];
  }
  return { outcome: wakeOutcome(results), subscribers: mine.length, delivered: results.filter((r) => r.outcome === "delivered").length };
}

/**
 * The answer for one spike:  {status, body, retryAfterS}.  No secrets, no
 * callback URLs, only counts.
 *   duplicate     200 {ok: true, duplicate: true}
 *   rate_limited  429 {ok: false, rate_limited: true}, with Retry-After
 *   paused        202 {ok: true, paused: true, subscribers: 0, delivered: 0}
 *   delivered     202 {ok: true, subscribers, delivered, outcome}  (any other outcome too:
 *                 the id is spent, so a retry is a new ping with a new id)
 */
export function spikeReply(result) {
  if (result.verdict === "duplicate") return { status: 200, body: { ok: true, duplicate: true } };
  if (result.verdict === "rate_limited") return { status: 429, body: { ok: false, rate_limited: true }, retryAfterS: Math.max(1, result.retryAfterS ?? 1) };
  if (result.verdict === "paused") return { status: 202, body: { ok: true, paused: true, subscribers: 0, delivered: 0 } };
  return { status: 202, body: { ok: true, subscribers: result.subscribers, delivered: result.delivered, outcome: result.outcome } };
}
