// Request gates for the hosted agent-sync MCP Worker.
//
// Pure module:  no `cloudflare:` imports, no I/O.  Every check here runs before
// the OAuth library sees the request, so nothing here may fetch anything.
// Spec:  docs/protocols/agent-sync-mcp.md sections 3.2 to 3.4.

const MAX_PARAM_LENGTH = 512;

/**
 * Why `value` is not a strict https URL, or "" when it is.  Strict means:
 * https, no userinfo, no explicit port, no query, no fragment, no encoded
 * slash or backslash, and canonical serialization (what `URL` prints back is
 * the input, byte for byte).  Used for redirect URIs and CIMD client ids.
 */
export function strictRedirectProblem(value) {
  if (typeof value !== "string" || value.length === 0) return "empty";
  if (value.length > MAX_PARAM_LENGTH) return "too long";
  if (/[\u0000-\u0020\u007f-\uffff]/.test(value)) return "control, space or non-ASCII character";
  if (value.includes("#")) return "fragment";
  if (value.includes("?")) return "query";
  if (value.includes("\\")) return "backslash";
  if (/%2f|%5c|%2e/i.test(value)) return "encoded slash, backslash or dot";
  let url;
  try {
    url = new URL(value);
  } catch {
    return "not a URL";
  }
  if (url.protocol !== "https:") return "not https";
  if (url.username || url.password || value.includes("@")) return "userinfo";
  if (url.port !== "") return "port";
  if (url.href !== value) return "not canonical";
  return "";
}

/**
 * Host check (spec 3.2):  only the D1 hostname, on the default port.  Both the
 * URL the runtime built and the raw Host header must name it.
 */
export function hostAllowed(request, host) {
  let url;
  try {
    url = new URL(request.url);
  } catch {
    return false;
  }
  if (url.hostname !== host || url.port !== "" || url.protocol !== "https:") return false;
  const header = request.headers.get("host");
  if (header !== null && header.toLowerCase() !== host) return false;
  return true;
}

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/**
 * Display predicate:  a client_id that names a URL in any way the library or a
 * browser might read as one.  Broader than the library's own test on purpose
 * (it parses after WHATWG URL normalization, which drops leading and trailing
 * spaces and removes tabs and newlines).  Used to filter /admin and to decide
 * whether the consent page shows a verified domain.  It is NOT the gate:  see
 * `clientIdVerdict`.
 */
export function isUrlShapedClientId(clientId) {
  if (typeof clientId !== "string") return false;
  if (SCHEME.test(clientId) || SCHEME.test(clientId.replace(/[\u0000-\u0020]/g, ""))) return true;
  try {
    new URL(clientId);
    return true;
  } catch {
    return false;
  }
}

/** Hostname of an https CIMD client id, or "" for anything else. */
export function cimdHostname(clientId) {
  try {
    const url = new URL(clientId);
    return url.protocol === "https:" ? url.hostname : "";
  } catch {
    return "";
  }
}

// What the OAuth library generates for a hand-registered client:  16 characters
// of [A-Za-z0-9_-].  Anything outside this shape that is not an exact
// allowlisted CIMD id never reaches the library, so no parser difference
// between this file and the library can turn an id into a metadata fetch.
const HAND_CLIENT_ID = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * The gate for every client_id seen before the library runs (spec 3.3):
 *   - an exact allowlisted CIMD id            -> { ok: true, seat }
 *   - the shape of a hand-registered id       -> { ok: true, seat: "" }
 *   - anything else, URL-shaped or not        -> { ok: false, reason }
 * An empty id passes (the library answers "client id required" and fetches nothing).
 */
export function clientIdVerdict(clientId, config) {
  if (clientId === "" || clientId === undefined || clientId === null) return { ok: true, seat: "" };
  if (typeof clientId !== "string") return { ok: false, reason: "client_id_shape" };
  const owner = config.cimdOwner.get(clientId);
  if (owner) return { ok: true, seat: owner };
  if (isUrlShapedClientId(clientId)) return { ok: false, reason: "cimd_client_not_allowlisted" };
  if (HAND_CLIENT_ID.test(clientId)) return { ok: true, seat: "" };
  return { ok: false, reason: "client_id_shape" };
}

/** Which route a path belongs to, or null for "404 before anything runs". */
export function classifyPath(pathname) {
  switch (pathname) {
    case "/mcp":
      return "mcp";
    case "/oauth/token":
      return "token";
    case "/authorize":
      return "authorize";
    case "/.well-known/oauth-authorization-server":
    case "/.well-known/oauth-protected-resource":
    case "/.well-known/oauth-protected-resource/mcp":
      return "metadata";
    case "/health":
      return "health";
    case "/admin":
      return "admin";
    default:
      if (pathname.startsWith("/admin/")) return "admin";
      return null;
  }
}

function single(params, name) {
  const all = params.getAll(name);
  if (all.length > 1) return { error: `repeated ${name}` };
  return { value: all.length === 1 ? all[0] : undefined };
}

/**
 * Gate an /authorize request on its raw query, before `parseAuthRequest`
 * (which is what fetches a CIMD document).  Returns
 * `{ ok: true, seat, clientId, redirectUri, scopes }` or
 * `{ ok: false, reason, clientId, redirectUri }`.  The reason is a short slug
 * that is safe to log;  the raw ids are returned for the refusal log only.
 */
export function preGateAuthorize(params, config) {
  const fields = {};
  for (const name of ["response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "resource", "scope", "state"]) {
    const got = single(params, name);
    if (got.error) return { ok: false, reason: got.error.replace(" ", "_") };
    fields[name] = got.value;
  }
  const clientId = fields.client_id ?? "";
  const redirectUri = fields.redirect_uri ?? "";
  const refuse = (reason) => ({ ok: false, reason, clientId, redirectUri });

  if (!clientId || clientId.length > MAX_PARAM_LENGTH) return refuse("client_id_missing_or_long");
  if (fields.response_type !== "code") return refuse("response_type_not_code");

  // Redirect allowlist:  exact string equality, then the strict parse as a
  // second line of defense in case the allowlist ever holds a loose value.
  const seat = config.redirectOwner.get(redirectUri);
  if (!seat) return refuse("redirect_not_allowlisted");
  if (strictRedirectProblem(redirectUri)) return refuse("redirect_not_strict");
  if (!config.hostedSeats.includes(seat)) return refuse("seat_not_hosted");

  // A client_id is an allowlisted CIMD id of the same seat, or the shape of a
  // hand-registered id.  Anything else could make the library fetch a document.
  const verdict = clientIdVerdict(clientId, config);
  if (!verdict.ok) return refuse(verdict.reason);
  if (verdict.seat && verdict.seat !== seat) return refuse("cimd_client_not_allowlisted");

  // PKCE S256 for every client, confidential ones included.
  if (fields.code_challenge_method !== "S256") return refuse("pkce_method_not_s256");
  if (!/^[A-Za-z0-9_-]{43,128}$/.test(fields.code_challenge ?? "")) return refuse("pkce_challenge_missing");

  // RFC 8707:  the token must be for this server's /mcp and nothing else.
  if (fields.resource !== config.resource) return refuse("resource_mismatch");

  const scopes = (fields.scope ?? "").split(" ").filter(Boolean);
  if (scopes.some((s) => !config.scopes.includes(s))) return refuse("scope_not_supported");

  return { ok: true, seat, clientId, redirectUri, scopes };
}

/**
 * Re-check a request the library parsed (or restored from its consent
 * transaction) against the same allowlist.  `authRequest` is the library's
 * AuthRequest.  Returns the seat whose redirect family it belongs to, or null.
 */
export function postGateAuthRequest(authRequest, config) {
  if (!authRequest || typeof authRequest !== "object") return null;
  const seat = config.redirectOwner.get(authRequest.redirectUri);
  if (!seat || strictRedirectProblem(authRequest.redirectUri)) return null;
  if (!config.hostedSeats.includes(seat)) return null;
  const verdict = clientIdVerdict(authRequest.clientId, config);
  if (!verdict.ok || (verdict.seat && verdict.seat !== seat)) return null;
  if (authRequest.codeChallengeMethod !== "S256" || !authRequest.codeChallenge) return null;
  if (authRequest.resource !== config.resource) return null;
  if ((authRequest.scope ?? []).some((s) => !config.scopes.includes(s))) return null;
  return seat;
}

/**
 * The library's own test for a form body (workers-oauth-provider
 * parseTokenEndpointRequest):  the media type, trimmed and lower-cased.  The
 * gate uses the same expression, so a Content-Type the library would accept
 * (a leading U+00A0 or U+FEFF, for example) can never skip the gate.
 */
export function isFormContentType(contentType) {
  return String(contentType ?? "").split(";")[0].trim().toLowerCase() === "application/x-www-form-urlencoded";
}

// A Basic header the gate will forward:  one space, then plain base64.  The
// library splits the scheme on a space or a tab only, while JS `\s` also
// matches U+00A0, U+3000, U+FEFF, VT and FF.  Parsing such a header here would
// disagree with the library about whether the request is Basic at all, so
// anything else is refused instead of interpreted.
const STRICT_BASIC = /^basic [A-Za-z0-9+/]+={0,2}$/i;

/** The client_id inside a strict Basic header, "" when absent, or null when the header is refused. */
export function basicClientId(header) {
  if (header === null || header === undefined || header === "") return "";
  if (typeof header !== "string" || !STRICT_BASIC.test(header)) return null;
  try {
    const decoded = atob(header.slice(6));
    const idx = decoded.indexOf(":");
    if (idx < 0) return null;
    return decodeURIComponent(decoded.slice(0, idx).replace(/\+/g, " "));
  } catch {
    return null;
  }
}

/**
 * Gate a token-endpoint form before the library sees it.  Every candidate
 * client id (the Basic one and each form one) must pass `clientIdVerdict`, so
 * no unlisted URL reaches the CIMD fetch whichever field the library reads.
 * An empty `client_secret=` is dropped so a public client that sends a blank
 * secret field authenticates as `none` (fixed upstream in 1.2.2, #405;  we pin
 * 1.1.0).  `authorization` is the raw Authorization header.  Returns
 * `{ ok: true, form }` where `form` is the URLSearchParams to forward (the
 * caller forwards this, never the original body), or `{ ok: false, reason }`.
 */
export function gateTokenForm(form, authorization, config) {
  const basicId = basicClientId(authorization);
  if (basicId === null) return { ok: false, reason: "authorization_not_basic" };
  const formIds = form.getAll("client_id");
  if (formIds.length > 1) return { ok: false, reason: "repeated_client_id" };
  for (const candidate of [basicId, ...formIds]) {
    const verdict = clientIdVerdict(candidate, config);
    if (!verdict.ok) return { ok: false, reason: verdict.reason, clientId: candidate };
  }
  const next = new URLSearchParams(form);
  const secrets = next.getAll("client_secret");
  if (secrets.length === 1 && secrets[0] === "") next.delete("client_secret");
  return { ok: true, form: next };
}

/**
 * Read at most `max` bytes of a request body as UTF-8 (BOM kept, like the form
 * parser).  Returns null when the body is larger, so a chunked upload cannot
 * fill memory before the size check.
 */
export async function readLimited(request, max) {
  if (!request.body) return "";
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
  return new TextDecoder("utf-8", { ignoreBOM: true }).decode(all);
}

/** Truncate and de-control a value for the refusal log.  Never used on secrets. */
export function logSafe(value, max = 300) {
  if (value === undefined || value === null) return "";
  return String(value).replace(/[\u0000-\u001f\u007f\u2028\u2029]/g, "?").slice(0, max);
}

/**
 * Same-origin check for state-changing POSTs (consent and /admin).  Requires
 * an Origin equal to the issuer, and `Sec-Fetch-Site: same-origin` when the
 * browser sends it (`requireFetchSite` makes it mandatory, for /admin).
 */
export function sameOriginPost(request, issuer, { requireFetchSite = false } = {}) {
  if (request.method !== "POST") return false;
  if (request.headers.get("origin") !== issuer) return false;
  const site = request.headers.get("sec-fetch-site");
  if (site === null) return !requireFetchSite;
  return site === "same-origin";
}

/** Constant-time string compare for CSRF tokens. */
export function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length || a.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
