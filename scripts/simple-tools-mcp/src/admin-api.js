// Read-only owner status API for Muster: one JSON row per bearer identity, the same facts the
// /admin "Per-Identity Bearers" table shows, so per-seat Fleet Tools state is readable without
// the web page.  Auth is the same Cloudflare Access owner check as /admin (serveRequest runs it
// before serveAdmin).  Never returns a bearer, a hash or a secret value: names, booleans, times.
import { ADDITIONAL_BEARER_SEATS, BEARER_SEATS, seatCredentialRecordNames } from "./bearer-identities.js";

const EXPIRY_SOON_MS = 14 * 24 * 60 * 60 * 1000;

/** The first thing wrong for one identity, or "ok".  A pure function of the row's facts. */
export function bearerProblem(row, now) {
  if (row.statusUnavailable) return "status_unavailable";
  if (row.credentialsUnavailable) return "credentials_unavailable";
  if (!row.credentialsLoaded) return "credentials_missing";
  const st = row.status ?? {};
  if (!st.active && !st.expired) return "no_bearer";
  if (st.expired) return "bearer_expired";
  if (row.paused) return "paused";
  if (st.mcp_accept_recorded === false || st.mcp_accept === false) return "mcp_not_accepted";
  // gbBearerStatus reports epoch milliseconds;  accept an ISO string too.
  const exp = typeof st.expires_at === "number" ? st.expires_at : Date.parse(st.expires_at ?? "");
  if (Number.isFinite(exp) && exp <= now) return "bearer_expired";
  if (Number.isFinite(exp) && exp - now < EXPIRY_SOON_MS) return "expiring_soon";
  if (Number(row.mismatches?.count) > 0) return "wrong_secret_attempts";
  return "ok";
}

/** deps: { gateFor(identity), resolveCredentials(identity), now }.  Each read fails on its own. */
export async function bearerStatusRows(deps) {
  const now = deps.now ?? Date.now();
  return Promise.all(BEARER_SEATS.map(async (identity) => {
    const g = deps.gateFor(identity);
    const [status, mismatches, credentials, state] = await Promise.allSettled([
      g.gbBearerStatus(), g.gbBearerMismatches(), deps.resolveCredentials(identity),
      typeof g.getState === "function" ? g.getState() : Promise.resolve({}),
    ]);
    const credentialsLoaded = credentials.status === "fulfilled" && Boolean(credentials.value);
    const row = {
      identity,
      kind: Object.hasOwn(ADDITIONAL_BEARER_SEATS, identity) ? "bearer_only" : "hosted",
      statusUnavailable: status.status === "rejected",
      credentialsLoaded,
      credentialsUnavailable: credentials.status === "rejected",
      missingRecords: credentialsLoaded ? null : seatCredentialRecordNames(identity, deps.config),
      status: status.status === "fulfilled" ? status.value : null,
      mismatches: mismatches.status === "fulfilled" ? (mismatches.value?.[identity] ?? null) : null,
      paused: state.status === "fulfilled" ? Boolean(state.value?.paused) : null,
    };
    return { ...row, problem: bearerProblem(row, now) };
  }));
}

// ---------------------------------------------------------------- owner action API (POST)
//
// Mint, rotate and revoke for one bearer identity, as JSON for Muster.  It runs behind the same Cloudflare
// Access owner check as /admin (the owner's own Access JWT, e.g. from `cloudflared access token`);  a service
// token carries no email and never passes.  The one response that carries a bearer is a successful mint or
// rotate, with `Cache-Control: no-store`;  the Worker keeps only the hash and logs no token.  The mint id is
// the caller's single-use 32-hex form id (12-hex ms timestamp + 20 random hex), so a retry of a finished mint
// is answered from the gate's tombstone without touching the bearer.
const ACTIONS = new Set(["mint", "rotate", "revoke"]);
const MINT_ID_RE = /^[0-9a-f]{32}$/;
const MAX_BODY = 4096;

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store, private", Pragma: "no-cache" },
  });
}

/** CSRF guard for a browser that holds the Access cookie:  JSON content type, a custom header, no cross-site fetch. */
export function adminApiRequestRefusal(request, issuer) {
  if (request.method !== "POST") return "method";
  const ct = String(request.headers.get("content-type") ?? "").toLowerCase();
  if (!/^application\/json(\s*;|$)/.test(ct)) return "content_type";
  if (request.headers.get("x-admin-api") !== "1") return "missing_header";
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "none" && site !== "same-origin") return "cross_site";
  const origin = request.headers.get("origin");
  if (origin !== null && origin !== issuer) return "origin";
  return null;
}

/**
 * deps: { issuer, email, identities: { bot: string[], seat: string[] }, gateFor(identity), evict(identity),
 *         newBotBearer(identity), newSeatBearer(identity), sha256Hex(text), ttlMs, prepare(), botCredentialsLoaded(identity),
 *         seatCredentials(identity), missingRecords(identity), now }
 */
export async function adminBearerAction(request, deps) {
  const refusal = adminApiRequestRefusal(request, deps.issuer);
  if (refusal) return json(refusal === "method" ? 405 : 403, { ok: false, error: "request_refused", reason: refusal });
  let raw;
  try { raw = await request.text(); } catch { return json(400, { ok: false, error: "bad_body" }); }
  if (raw.length > MAX_BODY) return json(400, { ok: false, error: "bad_body" });
  let body;
  try { body = JSON.parse(raw); } catch { return json(400, { ok: false, error: "bad_body" }); }
  if (!body || typeof body !== "object" || Array.isArray(body)) return json(400, { ok: false, error: "bad_body" });
  const { action, identity } = body;
  if (!ACTIONS.has(action)) return json(400, { ok: false, error: "unknown_action" });
  const isBot = deps.identities.bot.includes(identity);
  const isSeat = deps.identities.seat.includes(identity);
  if (typeof identity !== "string" || (!isBot && !isSeat)) return json(400, { ok: false, error: "unknown_identity" });
  const gate = deps.gateFor(identity);
  const now = deps.now ?? Date.now();

  if (action === "revoke") {
    await gate.gbBearerRevoke({ by: deps.email });
    deps.evict(identity);
    return json(200, { ok: true, action, identity });
  }

  const mintId = body.mint_id;
  if (typeof mintId !== "string" || !MINT_ID_RE.test(mintId)) return json(400, { ok: false, error: "bad_mint_id" });
  let status = null;
  try { status = await gate.gbBearerStatus(); } catch { return json(503, { ok: false, error: "status_unavailable" }); }
  const exp = typeof status?.expires_at === "number" ? status.expires_at : Date.parse(status?.expires_at ?? "");
  const hasLive = Boolean(status?.active) && !status?.expired && !(Number.isFinite(exp) && exp <= now);
  if (action === "mint" && hasLive) return json(409, { ok: false, error: "bearer_active", hint: "use rotate to replace a live bearer" });
  if (action === "rotate" && !hasLive) return json(409, { ok: false, error: "no_active_bearer", hint: "use mint" });

  let token, kind, mcpAccept;
  if (isBot) {
    await deps.prepare("bot");
    if (!(await deps.botCredentialsLoaded(identity))) return json(409, { ok: false, error: "credentials_missing", missingRecords: deps.missingRecords(identity) });
    ({ token } = await deps.newBotBearer(identity));
    kind = "bot"; mcpAccept = true;
  } else {
    let credentials = null;
    try { await deps.prepare("seat"); credentials = await deps.seatCredentials(identity); } catch { /* never an env fallback */ }
    if (!credentials) return json(409, { ok: false, error: "credentials_missing", missingRecords: deps.missingRecords(identity) });
    if (typeof body.mcp_accept === "boolean") mcpAccept = body.mcp_accept;
    else if (action === "rotate" && typeof status?.mcp_accept === "boolean") mcpAccept = status.mcp_accept;
    else return json(400, { ok: false, error: "mcp_accept_required" });
    ({ token } = await deps.newSeatBearer(identity));
    kind = "seat";
  }
  const expiresAt = now + deps.ttlMs;
  const minted = await gate.gbBearerMintChecked({ mintId, hash: await deps.sha256Hex(token), by: deps.email, expiresAt, mcpAccept });
  if (!minted.minted && minted.reason === "completed") return json(200, { ok: true, action, identity, completed: true, tokenShown: false, result: minted.result ?? null });
  if (!minted.minted) return json(409, { ok: false, error: "mint_refused", reason: minted.reason });
  deps.evict(identity);
  return json(200, { ok: true, action, identity, kind, token, expiresAt: new Date(expiresAt).toISOString(), mcpAccept });
}
