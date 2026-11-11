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
//                only: every read a binding serves is counted in
//                stats.fallbacks, and each name is console.warn'ed once per
//                isolate, so verification can prove the service-token path
//                serves before "required" is turned on.
//                An optional, undeclared name that is in neither Infisical
//                nor a binding is counted apart (stats.absent):  "required"
//                answers it the same way (capability off), so it is not a
//                fallback and never keeps the counter above zero.
//   "required" - Infisical only.  A miss or fetch failure throws
//                InfisicalError; nothing silently falls back.  Steady state.
//
// Rotation semantics: values cache for the INFISICAL_CACHE_TTL_S window (60
// seconds when unset) per (path, name), so an Infisical-side rotation
// propagates within that window with no redeploy.  The isolated mint-key reads
// and failed listings always use 60 seconds.  The token itself is never cached
// here beyond the env binding, never logged.

import { INFISICAL_EXPECT_DEFAULT } from "./infisical-manifest.js";
import { readBodyBytes } from "./wake.js";

// Both Infisical answers are untrusted input:  parsed with these schemas
// before anything reads them (kody 4365623474).  Unknown extra fields pass;
// a missing or mistyped field the Worker relies on does not.
// AFC-617:  a folder listing used to go through a zod schema that copies every row (".passthrough()"), and
// zod runs without its code-generating fast path in workerd, so one refresh of five folders cost about
// 9 ms of Worker CPU before any secret was read, and the first zod parse of an isolate cost more.  These two
// checks accept and refuse exactly what those schemas did and build no copy.  test/cpu-paths.test.mjs holds
// the schemas they replace and compares them on a table of bodies.
const isPlainObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

/** The answer for one secret ({ secret: { secretValue: string } }, extra keys allowed) when it has that shape, else null. */
export function checkedSecret(value) {
  return isPlainObject(value) && isPlainObject(value.secret) && typeof value.secret.secretValue === "string" ? value : null;
}

// A listing is an object whose `secrets` is an array of objects with string `secretKey` and `secretValue`, and
// an optional non-negative safe-integer `totalCount`.
export function checkedListing(value) {
  if (!isPlainObject(value) || !Array.isArray(value.secrets)) return null;
  const { secrets, totalCount } = value;
  if (totalCount !== undefined && !(Number.isSafeInteger(totalCount) && totalCount >= 0)) return null;
  for (const item of secrets) {
    if (!isPlainObject(item) || typeof item.secretKey !== "string" || typeof item.secretValue !== "string") return null;
  }
  return { secrets, totalCount };
}

// Explicit limits before the schemas see an answer (kody 4366922682):  the
// body is read up to a byte cap (a longer one is refused unread), then the
// parsed tree is walked without recursion against a depth and a node cap.
// A listing of one scoped folder is a few KiB;  the caps leave wide room.
export const INFISICAL_LIMITS = Object.freeze({ secretBytes: 256 * 1024, listingBytes: 2 * 1024 * 1024, depth: 32, nodes: 50_000 });

// The same walk as before (depth-first, no recursion), with the depth kept beside the node instead of in a
// fresh pair per node:  a listing is hundreds of nodes and this ran on every refresh (AFC-617).
export function withinShape(value) {
  const nodes = [value];
  const depths = [0];
  let seen = 0;
  while (nodes.length) {
    const node = nodes.pop();
    const depth = depths.pop();
    seen += 1;
    if (seen > INFISICAL_LIMITS.nodes || depth > INFISICAL_LIMITS.depth) return false;
    if (node && typeof node === "object") {
      for (const child of Object.values(node)) {
        nodes.push(child);
        depths.push(depth + 1);
      }
    }
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

// The default window, and the fixed one for a failed listing and for the isolated
// (mint-capable) reads below:  a failure heals and a signing key rotates within a minute.
const CACHE_TTL_MS = 60_000;
// AFC-617:  a refresh costs about 5 subrequests and 8 ms of Worker CPU per isolate, and
// every isolate paid it every minute.  INFISICAL_CACHE_TTL_S (10 to 3600) widens only the
// window for successful values, folder listings and remembered 404s;  unset or invalid
// keeps 60 seconds.  A rotation then reaches an isolate within that window, not within a minute.
const TTL_MIN_S = 10;
const TTL_MAX_S = 3600;
function ttlMs(env) {
  const seconds = Number(env?.INFISICAL_CACHE_TTL_S);
  return Number.isFinite(seconds) && seconds >= TTL_MIN_S && seconds <= TTL_MAX_S ? Math.floor(seconds) * 1000 : CACHE_TTL_MS;
}
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
// Listings in flight, by mode and folder (prefetchFolder shares them).
const folderInflight = new Map();
// A name Infisical just answered 404 for, outside any listing:  until the entry
// ages out, no further per-name subrequest for it (AFC-617).  Same decisions as
// a fresh 404, only without the subrequest.
const missed = new Map();
// One infisical_fallback line per isolate per name and code (AFC-617:  13,000
// lines a day for the same names), and the same for a failed listing per folder
// and code.  stats.fallbacks still counts every read.
const fallbackLogged = new Set();

function logOnce(key, event, fields) {
  if (fallbackLogged.has(key)) return;
  fallbackLogged.add(key);
  logEvent(event, fields);
}

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
  folderInflight.clear();
  missed.clear();
  fallbackLogged.clear();
}

const fresh = (at, ttl = CACHE_TTL_MS) => at !== undefined && Date.now() - at < ttl;

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
  const answer = checkedSecret(await boundedJson(res, INFISICAL_LIMITS.secretBytes));
  if (!answer) throw new InfisicalError(`Infisical's answer for ${name} at ${path} failed schema validation`, { code: "invalid_response" });
  // Trimmed like every legacy binding read (kody 4366922800):  a padded record
  // never reaches Zulip as a wrong key, and whitespace alone is absence.
  const value = answer.secret.secretValue.trim();
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
  if (fresh(folderWarm.get(path), ttlMs(env))) return;
  if (m === "fallback" && fresh(folderFailed.get(path))) return;
  // AFC-617:  requests that find a folder stale together share one refresh, where each used to list it again.
  // A refresh that fails fails every request that waited on it (in required mode), and the next one tries again.
  const key = `${m}\u0001${path}`;
  // A workerd isolate serves many requests, and a refresh started by a request that is then cancelled may
  // never settle.  A waiter therefore shares a refresh only while it is young, gives up on it after
  // INFLIGHT_SHARE_MS, and then lists the folder itself, so a stuck refresh costs one bounded wait.
  const running = folderInflight.get(key);
  if (running && Date.now() - running.at < INFLIGHT_SHARE_MS) {
    let timer;
    const giveUp = new Promise((resolve) => { timer = setTimeout(resolve, INFLIGHT_SHARE_MS - (Date.now() - running.at), STUCK); });
    try {
      const outcome = await Promise.race([running.promise, giveUp]);
      if (outcome !== STUCK) return;
    } finally {
      clearTimeout(timer);
    }
    if (folderInflight.get(key) === running) folderInflight.delete(key);
  }
  const entry = { at: Date.now(), promise: null };
  entry.promise = refreshFolder(env, path, m).finally(() => {
    if (folderInflight.get(key) === entry) folderInflight.delete(key);
  });
  folderInflight.set(key, entry);
  return entry.promise;
}

// How long a request waits on another request's listing before it lists the folder itself.
const INFLIGHT_SHARE_MS = 10_000;
const STUCK = Symbol("stuck");

async function refreshFolder(env, path, m) {
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
    const listing = checkedListing(await boundedJson(res, INFISICAL_LIMITS.listingBytes));
    if (!listing) {
      throw new InfisicalError(`Infisical listing for ${path} failed schema validation`, { code: "invalid_listing" });
    }
    const { secrets: items, totalCount } = listing;
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
    logOnce(`listing\u0001${path}\u0001${errorCode(error)}`, "infisical_listing_failed", { path, code: errorCode(error), status: error?.status ?? null, bindings_serve_s: CACHE_TTL_MS / 1000 });
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
  const ttl = ttlMs(env);
  const hit = cache.get(cacheKey);
  if (hit && Date.now() - hit.at < ttl) {
    stats.cacheHits++;
    return hit.value;
  }
  // A fresh folder listing is authoritative for absence: no subrequest.
  const listed = fresh(folderWarm.get(path), ttl);
  // Fallback mode, folder listing just failed:  no per-name subrequest either.
  const unusable = m === "fallback" && !listed && fresh(folderFailed.get(path));
  try {
    if (listed || fresh(missed.get(cacheKey), ttl)) throw new InfisicalError(`Infisical has no ${name} at ${path}`, { code: "not_found", status: 404 });
    if (unusable) throw new InfisicalError(`Infisical listing for ${path} failed recently`, { code: "infisical_unavailable" });
    let value;
    try {
      value = await fetchRaw(env, path, name);
    } catch (error) {
      if (error instanceof InfisicalError && error.code === "not_found") missed.set(cacheKey, Date.now());
      throw error;
    }
    cache.set(cacheKey, { value, at: Date.now() });
    missed.delete(cacheKey);
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
    logOnce(`${cacheKey}\u0001${errorCode(error)}`, "infisical_fallback", { path, name, binding: envName, served: Boolean(value), code: errorCode(error), status: error?.status ?? null });
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
//   - a 404 is remembered for the TTL, so an absent key costs one subrequest a
//     minute, not one per Git call (AFC-617).  Only a success and a 404 are held:
//     an outage is retried on the next call, as a mint needs.

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
    if (error instanceof InfisicalError && error.code === "not_found") {
      isolatedCache.set(cacheKey, { value: "", at: Date.now() });
      return "";
    }
    if (m === "required") throw error instanceof InfisicalError ? error : new InfisicalError(String(error));
    logOnce(`isolated\u0001${cacheKey}\u0001${errorCode(error)}`, "infisical_isolated_failed", { path, name, binding: tokenBinding, code: errorCode(error), status: error?.status ?? null });
    return "";
  }
}
