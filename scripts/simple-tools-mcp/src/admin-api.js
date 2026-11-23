// Read-only owner status API for Muster: one JSON row per bearer identity, the same facts the
// /admin "Per-Identity Bearers" table shows, so per-seat Fleet Tools state is readable without
// the web page.  Auth is the same Cloudflare Access owner check as /admin (serveRequest runs it
// before serveAdmin).  Never returns a bearer, a hash or a secret value: names, booleans, times.
import { ADDITIONAL_BEARER_SEATS, BEARER_SEATS, seatCredentialRecordNames } from "./bearer-identities.js";

const EXPIRY_SOON_MS = 14 * 24 * 60 * 60 * 1000;

/** The first thing wrong for one identity, or "ok".  A pure function of the row's facts. */
export function bearerProblem(row, now) {
  if (row.statusUnavailable) return "status_unavailable";
  if (!row.credentialsLoaded) return "credentials_missing";
  const st = row.status ?? {};
  if (!st.active && !st.expired) return "no_bearer";
  if (st.expired) return "bearer_expired";
  if (row.paused) return "paused";
  if (st.mcp_accept_recorded === false || st.mcp_accept === false) return "mcp_not_accepted";
  const exp = Date.parse(st.expires_at ?? "");
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
      missingRecords: credentialsLoaded ? null : seatCredentialRecordNames(identity),
      status: status.status === "fulfilled" ? status.value : null,
      mismatches: mismatches.status === "fulfilled" ? (mismatches.value?.[identity] ?? null) : null,
      paused: state.status === "fulfilled" ? Boolean(state.value?.paused) : null,
    };
    return { ...row, problem: bearerProblem(row, now) };
  }));
}
