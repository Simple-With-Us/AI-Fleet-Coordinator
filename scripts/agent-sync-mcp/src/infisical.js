// Runtime Infisical retrieval for the Worker (AFC-478 follow-through, owner
// 2026-10-23: finish the scoped service-token setup instead of install-time
// copies).  One read-tools service token, bound as INFISICAL_SERVICE_TOKEN,
// reads Fleet-OPS prod folders; references expand client-side
// (expandSecretReferences), so the token must also read every base folder -
// that is why the folders were scoped in at token creation.
//
// Modes (env.INFISICAL_MODE):
//   "off"      - never call Infisical; env bindings only (current behavior).
//   "fallback" - Infisical first, env binding on miss/failure.  TRANSITIONAL
//                only: every fallback is console.warn'ed and counted in
//                fallbackCount, so verification can prove the service-token
//                path serves before "required" is turned on.
//   "required" - Infisical only.  A miss or fetch failure throws
//                InfisicalError; nothing silently falls back.  Steady state.
//
// Rotation semantics: values cache for CACHE_TTL_MS per (path, name), so an
// Infisical-side rotation propagates within a minute with no redeploy.  The
// token itself is never cached here beyond the env binding, never logged.

export class InfisicalError extends Error {
  constructor(message, extra = {}) {
    super(message);
    this.name = "InfisicalError";
    this.code = extra.code ?? "infisical_unavailable";
    this.status = extra.status;
  }
}

const CACHE_TTL_MS = 60_000;
const cache = new Map(); // "path\0name" -> { value, at }
const stats = { fetched: 0, cacheHits: 0, fallbacks: 0 };

export function infisicalStats() {
  return { ...stats };
}

export function _resetInfisicalForTests() {
  cache.clear();
  stats.fetched = 0;
  stats.cacheHits = 0;
  stats.fallbacks = 0;
}

/** Declared-configured secrets:  comma list of `path:name` (e.g.
 * "/wake:WAKE_HMAC_KEY_JET").  A genuine 404 for a DECLARED secret throws
 * even on an optional read - an accidentally deleted configured secret fails
 * visibly instead of silently disabling the capability.  Absence of a name
 * from this list is what marks a capability intentionally absent. */
function expectedSet(env) {
  const raw = typeof env.INFISICAL_EXPECT === "string" ? env.INFISICAL_EXPECT : "";
  return new Set(raw.split(",").map((e) => e.trim()).filter(Boolean));
}

function mode(env) {
  const m = String(env.INFISICAL_MODE ?? "off").trim().toLowerCase();
  return m === "fallback" || m === "required" ? m : "off";
}

async function fetchRaw(env, path, name) {
  const token = typeof env.INFISICAL_SERVICE_TOKEN === "string" ? env.INFISICAL_SERVICE_TOKEN.trim() : "";
  if (!token) throw new InfisicalError("INFISICAL_SERVICE_TOKEN is not bound", { code: "not_configured" });
  const workspace = String(env.INFISICAL_WORKSPACE_ID ?? "").trim();
  if (!workspace) throw new InfisicalError("INFISICAL_WORKSPACE_ID is not bound", { code: "not_configured" });
  const environment = String(env.INFISICAL_ENVIRONMENT ?? "prod").trim() || "prod";
  const base = String(env.INFISICAL_BASE_URL ?? "https://app.infisical.com").replace(/\/+$/, "");
  const qs = new URLSearchParams({ workspaceId: workspace, environment, secretPath: path, expandSecretReferences: "true" });
  const res = await fetch(`${base}/api/v3/secrets/raw/${encodeURIComponent(name)}?${qs}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (res.status === 404) throw new InfisicalError(`Infisical has no ${name} at ${path}`, { code: "not_found", status: 404 });
  if (!res.ok) throw new InfisicalError(`Infisical answered ${res.status} for ${name} at ${path}`, { code: "infisical_unavailable", status: res.status });
  const body = await res.json();
  const value = body?.secret?.secretValue;
  if (typeof value !== "string" || value === "") throw new InfisicalError(`Infisical returned no value for ${name} at ${path}`, { code: "not_found" });
  return value;
}

/**
 * Resolve one secret.  `path` is the Fleet-OPS folder the read-tools token
 * may read ("/tools", "/wake", "/fleetlink", ...).  `envName` is the legacy
 * Worker binding used in "off" mode and as the bounded transitional fallback.
 */
export async function resolveSecret(env, { name, path, envName, optional = false }) {
  const m = mode(env);
  if (m === "off") return typeof env[envName] === "string" ? env[envName].trim() : "";
  const cacheKey = `${path}${name}`;
  const hit = cache.get(cacheKey);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) {
    stats.cacheHits++;
    return hit.value;
  }
  try {
    const value = await fetchRaw(env, path, name);
    cache.set(cacheKey, { value, at: Date.now() });
    stats.fetched++;
    return value;
  } catch (error) {
    // An optional read treats a genuine absence (404) as "capability off" even
    // in required mode:  a seat with no wake key is offered no events, not a
    // 500.  Token/outage/scope failures still throw.  This is not a fallback:
    // the env binding is never read for it.
    if (m === "required") {
      if (optional && error instanceof InfisicalError && error.code === "not_found" && !expectedSet(env).has(`${path}:${name}`)) return "";
      throw error instanceof InfisicalError ? error : new InfisicalError(String(error));
    }
    // Transitional fallback: explicit in the mode, observable in logs + stats.
    stats.fallbacks++;
    console.warn(`infisical fallback to env binding ${envName}: ${error.code ?? error.message}`);
    return typeof env[envName] === "string" ? env[envName].trim() : "";
  }
}
