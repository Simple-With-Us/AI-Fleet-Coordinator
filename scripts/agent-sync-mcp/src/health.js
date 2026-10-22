// AFC-478 follow-up:  the Worker's Linear token health, read by the fleet
// admin Mac app (Muster) through a launchd job that writes a local file
// (scripts/fleet-worker-health.py).  The route is POST /internal/health/linear,
// signed with HEALTH_HMAC_KEY (its own key, never a seat's wake key).  The
// answer holds ages, counts and fixed codes only:  no token, client id,
// secret, fingerprint or upstream message text.

import { safeEqual } from "./policy.js";
import { wakeSignature } from "./wake.js";

export const HEALTH_CONTRACT = "agent-sync-health/1";
export const HEALTH_SIGNATURE_HEADER = "x-agent-sync-health-signature";
export const HEALTH_MAX_BODY = 512;
export const HEALTH_MAX_AGE_S = 5 * 60;
export const HEALTH_MAX_SKEW_S = 60;
export const HEALTH_MIN_KEY_LEN = 32;

// Same numbers as linear.js:  re-mint at day 28 of a 30-day token, and a token
// with under an hour left is treated as unusable.
const REMINT_AFTER_S = 28 * 86_400;
const MIN_LIFE_S = 3600;
const KINDS = new Set(["mint", "timeout", "network", "redirect", "too_large", "rate_limited", "status", "unauthorized", "unexpected"]);
const HEX64_RE = /^[0-9a-f]{64}$/;
const BODY_KEYS = Object.freeze(["contract", "sent_at"]);

export function healthKey(raw) {
  const key = typeof raw === "string" ? raw.trim() : "";
  return key.length >= HEALTH_MIN_KEY_LEN ? key : "";
}

/** Check one health request.  Returns {ok: true} or {ok: false, status, reason}. */
export async function verifyHealth({ bytes, signature, key, now = Date.now() }) {
  if (!healthKey(key)) return { ok: false, status: 404, reason: "not_configured" };
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return { ok: false, status: 400, reason: "empty_body" };
  if (bytes.byteLength > HEALTH_MAX_BODY) return { ok: false, status: 413, reason: "too_large" };
  const given = typeof signature === "string" ? signature.trim() : "";
  if (!HEX64_RE.test(given)) return { ok: false, status: 401, reason: "bad_signature" };
  if (!safeEqual(given, await wakeSignature(healthKey(key), bytes))) return { ok: false, status: 401, reason: "bad_signature" };
  let body;
  try {
    body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return { ok: false, status: 400, reason: "bad_json" };
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return { ok: false, status: 400, reason: "bad_json" };
  if (Object.keys(body).sort().join(",") !== BODY_KEYS.join(",")) return { ok: false, status: 400, reason: "bad_fields" };
  if (body.contract !== HEALTH_CONTRACT) return { ok: false, status: 400, reason: "bad_contract" };
  if (!Number.isSafeInteger(body.sent_at)) return { ok: false, status: 400, reason: "bad_sent_at" };
  const nowS = Math.floor(now / 1000);
  if (nowS - body.sent_at > HEALTH_MAX_AGE_S) return { ok: false, status: 401, reason: "stale" };
  if (body.sent_at - nowS > HEALTH_MAX_SKEW_S) return { ok: false, status: 401, reason: "future" };
  return { ok: true };
}

const days = (seconds) => Math.round((seconds / 86_400) * 100) / 100;
const iso = (s) => (s > 0 ? new Date(s * 1000).toISOString().replace(/\.\d{3}Z$/, "Z") : null);

/**
 * One seat's row.  `row` is SeatGate.linearHealth() ({token, note}), `nowS`
 * is in seconds.  state:
 *   fail      no usable token (none, or under an hour left) and the last mint failed,
 *             or the token is under an hour from expiry
 *   warn      past day 28 with the early mint not landed, the last mint failed while the
 *             old token still works, or credentials are installed and never minted
 *   ok        otherwise
 */
export function classifySeat(seat, row, nowS) {
  const token = row?.token ?? null;
  const note = row?.note ?? {};
  const failKind = KINDS.has(note.last_fail_kind) ? note.last_fail_kind : note.last_fail_kind ? "unexpected" : null;
  const lastFailAt = Number(note.last_fail_at) || 0;
  const lastOkAt = Number(note.last_ok_at) || 0;
  const failedSinceOk = lastFailAt > 0 && lastFailAt >= lastOkAt;
  const out = {
    seat,
    state: "ok",
    minted: Boolean(token),
    minted_age_days: null,
    expires_in_days: null,
    remint_due_in_days: null,
    mint_count: token?.mint_count ?? 0,
    last_mint_ok_at: iso(lastOkAt),
    last_mint_fail_at: iso(lastFailAt),
    last_mint_fail_kind: failKind,
  };
  if (!token) {
    out.state = failedSinceOk ? "fail" : "warn";
    out.note = failedSinceOk ? "no token and the last mint failed" : "credentials installed, never minted";
    return out;
  }
  const age = nowS - token.minted_at;
  const left = token.expires_at - nowS;
  out.minted_age_days = days(age);
  out.expires_in_days = days(left);
  out.remint_due_in_days = days(REMINT_AFTER_S - age);
  if (left <= MIN_LIFE_S) {
    out.state = "fail";
    out.note = "token is expired or about to expire";
  } else if (failedSinceOk) {
    out.state = "warn";
    out.note = "the last mint failed, the cached token still works";
  } else if (age >= REMINT_AFTER_S) {
    out.state = "warn";
    out.note = "past day 28 and the early re-mint has not landed";
  }
  return out;
}

const RANK = { ok: 0, warn: 1, fail: 2 };

/** The whole answer.  `rows` is [{seat, row}], `nowS` in seconds. */
export function healthReport(rows, nowS) {
  const seats = rows.map(({ seat, row }) => classifySeat(seat, row, nowS)).sort((a, b) => a.seat.localeCompare(b.seat));
  const worst = seats.reduce((w, s) => (RANK[s.state] > RANK[w] ? s.state : w), "ok");
  return { contract: HEALTH_CONTRACT, generated_at: iso(nowS), worst, ok: worst === "ok", seats };
}
