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
//                only: every read a binding serves is console.warn'ed and
//                counted in stats.fallbacks, so verification can prove the
//                service-token path serves before "required" is turned on.
//                An optional, undeclared name that is in neither Infisical
//                nor a binding is counted apart (stats.absent):  "required"
//                answers it the same way (capability off), so it is not a
//                fallback and never keeps the counter above zero.
//   "required" - Infisical only.  A miss or fetch failure throws
//                InfisicalError; nothing silently falls back.  Steady state.
//
// Rotation semantics: values cache for CACHE_TTL_MS per (path, name), so an
// Infisical-side rotation propagates within a minute with no redeploy.  The
// token itself is never cached here beyond the env binding, never logged.

import { z } from "zod";
import { INFISICAL_EXPECT_DEFAULT } from "./infisical-manifest.js";
import { readBodyBytes } from "./wake.js";

// Both Infisical answers are untrusted input:  parsed with these schemas
// before anything reads them (kody 4365623474).  Unknown extra fields pass;
// a missing or mistyped field the Worker relies on does not.
const SecretResponse = z.object({ secret: z.object({ secretValue: z.string() }).passthrough() }).passthrough();
const FolderListing = z.object({
  secrets: z.array(z.object({ secretKey: z.string(), secretValue: z.string() }).passthrough()),
  totalCount: z.number().int().nonnegative().optional(),
}).passthrough();

// Explicit limits before the schemas see an answer (kody 4366922682):  the
// body is read up to a byte cap (a longer one is refused unread), then the
// parsed tree is walked without recursion against a depth and a node cap.
// A listing of one scoped folder is a few KiB;  the caps leave wide room.
export const INFISICAL_LIMITS = Object.freeze({ secretBytes: 256 * 1024, listingBytes: 2 * 1024 * 1024, depth: 32, nodes: 50_000 });

function withinShape(value) {
  const stack = [[value, 0]];
  let nodes = 0;
  while (stack.length) {
    const [node, depth] = stack.pop();
    nodes += 1;
    if (nodes > INFISICAL_LIMITS.nodes || depth > INFISICAL_LIMITS.depth) return false;
    if (node && typeof node === "object") for (const child of Object.values(node)) stack.push([child, depth + 1]);
  }
  return true;
}

/** The answer's JSON, or null when it is over the byte cap, not JSON, or
 * past the depth or node cap;  the caller's schema then refuses the null. */
async function boundedJson(res, maxBytes) {
  const declared = Number(res.headers.get("content-length") ?? "");
  if (Number.isFinite(declared) && declared > maxBytes) {
    await res.body?.cancel().catch(() => {});
    return null;
  }
  const bytes = await readBodyBytes(res, maxBytes);
  if (bytes === null) return null;
  let value;
  try {
    value = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
  return withinShape(value) ? value : null;
}

/** One structured, allow-listed log line (kody 4366922553):  names, the mode
 * and a fixed code or status only, never a value and never an error message. */
function logEvent(event, fields) {
  console.warn(JSON.stringify({ event, ...fields }));
}

const errorCode = (error) => (error instanceof InfisicalError ? error.code : "fetch_failed");

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
const stats = { fetched: 0, cacheHits: 0, fallbacks: 0, absent: 0 };
// Folder listings warm the same per-name cache; while a folder's listing is
// fresh it is authoritative for absence too, so optional misses cost no
// subrequest (sentry 4364275308: a cold per-name burst could pass the free
// plan's subrequest budget on shared seats).
const folderWarm = new Map();
// Fallback mode only:  a folder whose listing just failed.  Until the entry
// ages out, reads in it go straight to the env binding (counted) instead of
// one doomed per-name subrequest each (kody 4365623733:  an outage would
// otherwise cost ~90 subrequests per request).
const folderFailed = new Map();

export function infisicalStats() {
  return { ...stats };
}

export function _resetInfisicalForTests() {
  isolatedCache.clear();
  cache.clear();
  stats.fetched = 0;
  stats.cacheHits = 0;
  stats.fallbacks = 0;
  stats.absent = 0;
  folderWarm.clear();
  folderFailed.clear();
}

const fresh = (at) => at !== undefined && Date.now() - at < CACHE_TTL_MS;

/** Declared-configured secrets:  comma list of `path:name` (e.g.
 * "/wake:WAKE_HMAC_KEY_JET").  A genuine 404 for a DECLARED secret throws
 * even on an optional read - an accidentally deleted configured secret fails
 * visibly instead of silently disabling the capability.  Absence of a name
 * from this list is what marks a capability intentionally absent. */
function expectedSet(env) {
  const raw = typeof env.INFISICAL_EXPECT === "string" ? env.INFISICAL_EXPECT.trim() : "";
  if (raw) return new Set(raw.split(",").map((e) => e.trim()).filter(Boolean));
  return new Set(INFISICAL_EXPECT_DEFAULT);
}

/** The configured mode ("off" | "fallback" | "required") for callers that
 * must know whether legacy env bindings are in play. */
export function infisicalMode(env) {
  return mode(env);
}

function mode(env) {
  const m = String(env.INFISICAL_MODE ?? "off").trim().toLowerCase();
  return m === "fallback" || m === "required" ? m : "off";
}

async function fetchRaw(env, path, name, { tokenOverride, tokenBinding = "INFISICAL_SERVICE_TOKEN" } = {}) {
  const token = tokenOverride ?? (typeof env.INFISICAL_SERVICE_TOKEN === "string" ? env.INFISICAL_SERVICE_TOKEN.trim() : "");
  if (!token) throw new InfisicalError(`${tokenBinding} is not bound`, { code: "not_configured" });
  const workspace = String(env.INFISICAL_WORKSPACE_ID ?? "").trim();
  if (!workspace) throw new InfisicalError("INFISICAL_WORKSPACE_ID is not bound", { code: "not_configured" });
  const environment = String(env.INFISICAL_ENVIRONMENT ?? "prod").trim() || "prod";
  const base = String(env.INFISICAL_BASE_URL ?? "https://app.infisical.com").replace(/\/+$/, "");
  const qs = new URLSearchParams({ workspaceId: workspace, environment, secretPath: path, expandSecretReferences: "true" });
  // redirect "manual" (kody 4365623359):  a 3xx is refused by the !res.ok
  // check below, never followed, so the bearer token never reaches another origin.
  const res = await fetch(`${base}/api/v3/secrets/raw/${encodeURIComponent(name)}?${qs}`, {
    headers: { Authorization: `Bearer ${token}` },
    redirect: "manual",
  });
  if (res.status === 404) throw new InfisicalError(`Infisical has no ${name} at ${path}`, { code: "not_found", status: 404 });
  if (!res.ok) throw new InfisicalError(`Infisical answered ${res.status} for ${name} at ${path}`, { code: "infisical_unavailable", status: res.status });
  const parsed = SecretResponse.safeParse(await boundedJson(res, INFISICAL_LIMITS.secretBytes));
  if (!parsed.success) throw new InfisicalError(`Infisical's answer for ${name} at ${path} failed schema validation`, { code: "invalid_response" });
  // Trimmed like every legacy binding read (kody 4366922800):  a padded record
  // never reaches Zulip as a wrong key, and whitespace alone is absence.
  const value = parsed.data.secret.secretValue.trim();
  if (value === "") throw new InfisicalError(`Infisical returned no value for ${name} at ${path}`, { code: "not_found" });
  return value;
}

/** List one folder and warm the cache with every value in it.  While the
 * listing is fresh, resolveSecret treats an unlisted name as absent without
 * another subrequest, and a second call is a no-op (kody 4365623583:  no
 * re-list on every request).  "off" mode is a no-op; a listing failure throws
 * in required mode, and in fallback it only warns and marks the folder failed
 * for the TTL, so its reads use the env binding without a subrequest each.
 */
export async function prefetchFolder(env, path) {
  const m = mode(env);
  if (m === "off") return;
  if (fresh(folderWarm.get(path))) return;
  if (m === "fallback" && fresh(folderFailed.get(path))) return;
  const token = typeof env.INFISICAL_SERVICE_TOKEN === "string" ? env.INFISICAL_SERVICE_TOKEN.trim() : "";
  const workspace = String(env.INFISICAL_WORKSPACE_ID ?? "").trim();
  const environment = String(env.INFISICAL_ENVIRONMENT ?? "prod").trim() || "prod";
  const base = String(env.INFISICAL_BASE_URL ?? "https://app.infisical.com").replace(/\/+$/, "");
  const qs = new URLSearchParams({ workspaceId: workspace, environment, secretPath: path, expandSecretReferences: "true" });
  let res;
  try {
    if (!token || !workspace) throw new InfisicalError("INFISICAL_SERVICE_TOKEN/WORKSPACE_ID not bound", { code: "not_configured" });
    res = await fetch(`${base}/api/v3/secrets/raw?${qs}`, { headers: { Authorization: `Bearer ${token}` }, redirect: "manual" });
    if (!res.ok) throw new InfisicalError(`Infisical answered ${res.status} listing ${path}`, { code: "infisical_unavailable", status: res.status });
    // Authoritative absence requires a COMPLETE, validated listing.  Any
    // shape doubt (no array, non-string fields, pagination markers) refuses
    // to warm:  an empty-looking malformed listing must never deactivate
    // tools or masquerade as drift.
    const parsed = FolderListing.safeParse(await boundedJson(res, INFISICAL_LIMITS.listingBytes));
    if (!parsed.success) {
      throw new InfisicalError(`Infisical listing for ${path} failed schema validation`, { code: "invalid_listing" });
    }
    const { secrets: items, totalCount } = parsed.data;
    if (totalCount !== undefined && totalCount !== items.length) {
      throw new InfisicalError(`Infisical listing for ${path} is truncated (${items.length} of ${totalCount})`, { code: "truncated_listing" });
    }
    const now = Date.now();
    for (const item of items) {
      const value = item.secretValue.trim();
      if (value !== "") cache.set(`${path}${item.secretKey}`, { value, at: now });
    }
    folderWarm.set(path, now);
    folderFailed.delete(path);
    stats.fetched++;
  } catch (error) {
    if (m === "required") throw error instanceof InfisicalError ? error : new InfisicalError(String(error));
    folderFailed.set(path, Date.now());
    logEvent("infisical_listing_failed", { path, code: errorCode(error), status: error?.status ?? null, bindings_serve_s: CACHE_TTL_MS / 1000 });
  }
}

/**
 * Resolve one secret.  `path` is the Fleet-OPS folder the read-tools token
 * may read ("/tools", "/wake", "/fleetlink", ...).  `envName` is the legacy
 * Worker binding used in "off" mode and as the bounded transitional fallback.
 * `legacy`, when given, is that legacy value itself, for a secret whose
 * legacy source is not one binding (a FleetLink seat token's entry in the
 * FLEETLINK_SEAT_TOKENS bundle);  `envName` then only labels it in the log.
 */
export async function resolveSecret(env, { name, path, envName, optional = false, legacy }) {
  const m = mode(env);
  const legacyValue = () => {
    const raw = legacy !== undefined ? legacy : env[envName];
    return typeof raw === "string" ? raw.trim() : "";
  };
  if (m === "off") return legacyValue();
  const cacheKey = `${path}${name}`;
  const hit = cache.get(cacheKey);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) {
    stats.cacheHits++;
    return hit.value;
  }
  // A fresh folder listing is authoritative for absence: no subrequest.
  const listed = fresh(folderWarm.get(path));
  // Fallback mode, folder listing just failed:  no per-name subrequest either.
  const unusable = m === "fallback" && !listed && fresh(folderFailed.get(path));
  try {
    if (listed) throw new InfisicalError(`Infisical has no ${name} at ${path}`, { code: "not_found", status: 404 });
    if (unusable) throw new InfisicalError(`Infisical listing for ${path} failed recently`, { code: "infisical_unavailable" });
    const value = await fetchRaw(env, path, name);
    cache.set(cacheKey, { value, at: Date.now() });
    stats.fetched++;
    return value;
  } catch (error) {
    // An optional read treats a genuine absence (404) as "capability off" even
    // in required mode:  a seat with no wake key is offered no events, not a
    // 500.  Token/outage/scope failures still throw.  This is not a fallback:
    // the env binding is never read for it.
    const capabilityOff = optional && error instanceof InfisicalError && error.code === "not_found" && !expectedSet(env).has(`${path}:${name}`);
    if (m === "required") {
      if (capabilityOff) return "";
      throw error instanceof InfisicalError ? error : new InfisicalError(String(error));
    }
    // Transitional fallback: explicit in the mode, observable in logs + stats.
    const value = legacyValue();
    // Nothing serves it, and "required" would answer the same empty value:
    // an intentionally-off capability, not a fallback.  Counted apart, so a
    // zero fallback counter is reachable and means "required changes nothing".
    if (capabilityOff && !value) {
      stats.absent++;
      return "";
    }
    stats.fallbacks++;
    logEvent("infisical_fallback", { path, name, binding: envName, served: Boolean(value), code: errorCode(error), status: error?.status ?? null });
    return value;
  }
}

/** Every wake key Infisical serves for `names` (in "off" mode, every Worker
 * binding), after one /wake listing.  A key-reuse guard compares against this
 * set:  in "required" mode the wake keys need not be Worker bindings, so an
 * env-only read would see none of them (sentry 4364275301). */
export async function resolveWakeKeys(env, names) {
  await prefetchFolder(env, "/wake");
  const keys = await Promise.all(names.map((name) => resolveSecret(env, { name, path: "/wake", envName: name, optional: true })));
  return keys.map((key) => key.trim()).filter(Boolean);
}
// ---------------------------------------------------------------- isolated custody reads
//
// The read-tools service token (INFISICAL_SERVICE_TOKEN) is deliberately
// broad:  every folder it may read is readable by anything holding it.  A
// MINT-capable secret must not ride it.  resolveSecretIsolated serves one
// secret through a NARROWLY SCOPED, separately bound service token (e.g.
// GR_ASSERT_INFISICAL_SERVICE_TOKEN, scoped to /gr-assert-signing only):
//
//   - provenance is exact:  the value comes from `path` through
//     `tokenBinding`, and from nowhere else;
//   - NO fallback:  the generic INFISICAL_SERVICE_TOKEN is never consulted
//     for this read, no legacy Worker binding is read, and no other folder
//     or key name is tried;
//   - the shared folder cache is bypassed:  a value listed under the broad
//     token (e.g. the /gr-front Access pair) is never reused as this secret,
//     and this secret is never served from a listing it was not read under;
//   - fail closed:  no binding answers "" in off/fallback mode and throws in
//     required mode;  a genuine 404 answers "" (capability off) while an
//     outage or a scope denial throws in required mode, as elsewhere.

const isolatedCache = new Map();

export async function resolveSecretIsolated(env, { name, path, tokenBinding }) {
  const m = mode(env);
  if (m === "off") return "";
  const token = typeof env[tokenBinding] === "string" ? env[tokenBinding].trim() : "";
  if (!token) {
    if (m === "required") throw new InfisicalError(`${tokenBinding} is not bound`, { code: "not_configured" });
    return "";
  }
  const cacheKey = `${tokenBinding}\u0001${path}\u0001${name}`;
  const hit = isolatedCache.get(cacheKey);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) {
    stats.cacheHits++;
    return hit.value;
  }
  try {
    const value = await fetchRaw(env, path, name, { tokenOverride: token, tokenBinding });
    isolatedCache.set(cacheKey, { value, at: Date.now() });
    stats.fetched++;
    return value;
  } catch (error) {
    if (error instanceof InfisicalError && error.code === "not_found") return "";
    if (m === "required") throw error instanceof InfisicalError ? error : new InfisicalError(String(error));
    logEvent("infisical_isolated_failed", { path, name, binding: tokenBinding, code: errorCode(error), status: error?.status ?? null });
    return "";
  }
}
