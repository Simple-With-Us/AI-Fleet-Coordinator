// The listener's wake into a hosted seat:  POST /internal/wake/<SEAT> with the
// `agent-sync-wake/1` body (scripts/agent_sync/adapters.py routine_body), sent
// by the server listener's `http` wake adapter with `auth = "hmac-sha256"`.
//
// The header is X-Agent-Sync-Signature:  the lowercase hex HMAC-SHA256 of the
// raw body bytes, keyed with the UTF-8 bytes of the shared key (the listener's
// <SEAT>_ROUTINE_KEY, this Worker's WAKE_HMAC_KEY_<SEAT>), with no prefix.
// The signature is checked over the raw bytes before any JSON parse.  A body
// whose `sent_at` is more than five minutes old or one minute in the future is
// refused, so the wake_id dedupe in SeatGate only has to remember a few minutes.
// `stream_id` (the trigger's Zulip stream id, null for a DM) is optional here
// so an older listener's body still parses;  events.js then refuses a channel
// wake that lacks it.
// `held_back` is optional too:  the listener's summary of wakes it held back
// for its budget, `{count, items}`, with at most 20 items of metadata and no
// message text.  When present it is checked strictly (heldBackProblem), and a
// malformed one is 400 `bad_held_back`.  Keys an item does not know are
// ignored, never forwarded:  events.js copies only the fields it knows.
//
// Pure module:  WebCrypto only, so `node --test` loads it.

import { safeEqual } from "./policy.js";

export const WAKE_CONTRACT = "agent-sync-wake/1";
export const WAKE_SIGNATURE_HEADER = "x-agent-sync-signature";
export const WAKE_MAX_BODY = 64 * 1024;
export const WAKE_MAX_AGE_S = 5 * 60;
export const WAKE_MAX_SKEW_S = 60;
// The shortest key the Worker accepts.  `openssl rand -hex 32` gives 64.
export const WAKE_MIN_KEY_LEN = 32;
// `held_back` limits:  at most 20 items, and every string in an item at most
// 200 UTF-16 code units (the listener sends at most 100 characters of a name
// and drops a link longer than 200 rather than cut it).
export const HELD_BACK_MAX_ITEMS = 20;
export const HELD_BACK_MAX_STRING = 200;

const HEX64_RE = /^[0-9a-f]{64}$/;
// A held-back reason is a budget's name (wakes_per_hour, usd_per_day, ...) or overflow.
const REASON_RE = /^[a-z][a-z0-9_]{0,63}$/;
const WAKE_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const PATH_RE = /^\/internal\/wake\/([A-Z][A-Z0-9-]{0,31})$/;

/** The seat named by a wake path, or null. */
export function wakePathSeat(pathname) {
  const m = PATH_RE.exec(pathname);
  return m ? m[1] : null;
}

/** The raw body bytes, or null past `max`.  The signature covers these exact bytes. */
export async function readBodyBytes(request, max = WAKE_MAX_BODY) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return all;
}

function hex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Lowercase hex HMAC-SHA256 of `bytes` keyed with the UTF-8 bytes of `key`. */
export async function wakeSignature(key, bytes) {
  const cryptoKey = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", cryptoKey, bytes));
}

const isInt = (v) => Number.isSafeInteger(v);
const isStrOrNull = (v) => v === null || typeof v === "string";
const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isShortStr = (v) => typeof v === "string" && v.length <= HELD_BACK_MAX_STRING;
const isShortStrOrNull = (v) => v === null || isShortStr(v);

/**
 * Is one held-back item well formed?  Every known field is required with its
 * exact type:  message_id a positive integer, dm a boolean, channel and topic
 * a short string or null, stream_id a positive integer or null, sender and
 * link short strings, reason a slug and at whole seconds.  A DM names no
 * stream, so a DM item with a channel, topic or stream id is refused, the same
 * rule as `dm` and `reply_to` at the top level.
 */
function heldBackItemOk(item) {
  if (!isObject(item)) return false;
  if (!(isInt(item.message_id) && item.message_id > 0)) return false;
  if (typeof item.dm !== "boolean") return false;
  if (!isShortStrOrNull(item.channel) || !isShortStrOrNull(item.topic)) return false;
  if (!(item.stream_id === null || (isInt(item.stream_id) && item.stream_id > 0))) return false;
  if (!isShortStr(item.sender_full_name) || !isShortStr(item.zulip_link)) return false;
  if (typeof item.reason !== "string" || !REASON_RE.test(item.reason)) return false;
  if (!(isInt(item.at) && item.at >= 0)) return false;
  if (item.dm && (item.channel !== null || item.topic !== null || item.stream_id !== null)) return false;
  return true;
}

/**
 * True when `held_back` is present and malformed.  Absent or null is fine.
 * Present, it is `{count, items}`:  count an integer of at least 0 and at
 * least the number of items (the listener may hold back more than it lists),
 * items an array of at most HELD_BACK_MAX_ITEMS well-formed items.
 */
export function heldBackProblem(heldBack) {
  if (heldBack === undefined || heldBack === null) return false;
  if (!isObject(heldBack)) return true;
  const { count, items } = heldBack;
  if (!(isInt(count) && count >= 0)) return true;
  if (!Array.isArray(items) || items.length > HELD_BACK_MAX_ITEMS || items.length > count) return true;
  return !items.every(heldBackItemOk);
}

/**
 * Check one wake request.  `bytes` is the raw body (at most WAKE_MAX_BODY),
 * `signature` the header value, `key` the seat's shared key, `seat` the seat
 * from the path.  Returns {ok: true, wake} or {ok: false, status, reason}.
 * The reason is a fixed code, safe to log;  nothing from the body is echoed.
 */
export async function verifyWake({ bytes, signature, key, seat, now = Date.now() }) {
  if (typeof key !== "string" || key.length < WAKE_MIN_KEY_LEN) return { ok: false, status: 404, reason: "not_configured" };
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return { ok: false, status: 400, reason: "empty_body" };
  if (bytes.byteLength > WAKE_MAX_BODY) return { ok: false, status: 413, reason: "too_large" };
  const given = typeof signature === "string" ? signature.trim() : "";
  if (!HEX64_RE.test(given)) return { ok: false, status: 401, reason: "bad_signature" };
  const expected = await wakeSignature(key, bytes);
  if (!safeEqual(given, expected)) return { ok: false, status: 401, reason: "bad_signature" };

  let wake;
  try {
    wake = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return { ok: false, status: 400, reason: "bad_json" };
  }
  if (!wake || typeof wake !== "object" || Array.isArray(wake)) return { ok: false, status: 400, reason: "bad_json" };
  if (wake.contract !== WAKE_CONTRACT) return { ok: false, status: 400, reason: "bad_contract" };
  if (wake.seat !== seat) return { ok: false, status: 400, reason: "seat_mismatch" };
  if (typeof wake.wake_id !== "string" || !WAKE_ID_RE.test(wake.wake_id)) return { ok: false, status: 400, reason: "bad_wake_id" };
  if (!isInt(wake.sent_at)) return { ok: false, status: 400, reason: "bad_sent_at" };
  const nowS = Math.floor(now / 1000);
  if (nowS - wake.sent_at > WAKE_MAX_AGE_S) return { ok: false, status: 401, reason: "stale" };
  if (wake.sent_at - nowS > WAKE_MAX_SKEW_S) return { ok: false, status: 401, reason: "future" };
  if (!isInt(wake.message_id) || wake.message_id <= 0) return { ok: false, status: 400, reason: "bad_message_id" };
  if (typeof wake.dm !== "boolean") return { ok: false, status: 400, reason: "bad_dm" };
  if (!isStrOrNull(wake.channel) || !isStrOrNull(wake.topic)) return { ok: false, status: 400, reason: "bad_where" };
  if (wake.stream_id !== undefined && wake.stream_id !== null && !(isInt(wake.stream_id) && wake.stream_id > 0)) {
    return { ok: false, status: 400, reason: "bad_stream_id" };
  }
  if (typeof wake.excerpt !== "string" || typeof wake.zulip_link !== "string") return { ok: false, status: 400, reason: "bad_fields" };
  const replyTo = wake.reply_to;
  if (!replyTo || typeof replyTo !== "object" || (replyTo.type !== "stream" && replyTo.type !== "direct")) {
    return { ok: false, status: 400, reason: "bad_reply_to" };
  }
  // A DM is a DM in both places, or the channel rule (events.js) and the payload could disagree.
  if ((replyTo.type === "direct") !== wake.dm) return { ok: false, status: 400, reason: "bad_reply_to" };
  if (heldBackProblem(wake.held_back)) return { ok: false, status: 400, reason: "bad_held_back" };
  return { ok: true, wake };
}
