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

const HEX64_RE = /^[0-9a-f]{64}$/;
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
  if (typeof wake.excerpt !== "string" || typeof wake.zulip_link !== "string") return { ok: false, status: 400, reason: "bad_fields" };
  const replyTo = wake.reply_to;
  if (!replyTo || typeof replyTo !== "object" || (replyTo.type !== "stream" && replyTo.type !== "direct")) {
    return { ok: false, status: 400, reason: "bad_reply_to" };
  }
  return { ok: true, wake };
}
