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

/** A client_id that names a URL (any scheme), which the library would fetch as CIMD. */
export function isUrlShapedClientId(clientId) {
  return typeof clientId === "string" && /^[a-z][a-z0-9+.-]*:/i.test(clientId);
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

  // A URL-shaped client_id must be an allowlisted CIMD id of the same seat,
  // or the library would fetch an attacker's document.
  if (isUrlShapedClientId(clientId)) {
    if (config.cimdOwner.get(clientId) !== seat) return refuse("cimd_client_not_allowlisted");
  }

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
  if (isUrlShapedClientId(authRequest.clientId) && config.cimdOwner.get(authRequest.clientId) !== seat) return null;
  if (authRequest.codeChallengeMethod !== "S256" || !authRequest.codeChallenge) return null;
  if (authRequest.resource !== config.resource) return null;
  if ((authRequest.scope ?? []).some((s) => !config.scopes.includes(s))) return null;
  return seat;
}

/**
 * Gate a token-endpoint form before the library sees it.  Refuses a URL-shaped
 * client_id that is not allowlisted (no CIMD fetch for strangers), and drops
 * an empty `client_secret=` so a public client that sends a blank secret
 * field authenticates as `none` (the library fixed this in 1.2.2, #405;  we
 * pin 1.1.0).  Returns `{ ok, reason?, form? }` where `form` is the
 * URLSearchParams to forward when it changed.
 */
export function gateTokenForm(form, basicClientId, config) {
  const clientIds = form.getAll("client_id");
  if (clientIds.length > 1) return { ok: false, reason: "repeated_client_id" };
  const clientId = basicClientId ?? clientIds[0] ?? "";
  if (isUrlShapedClientId(clientId) && !config.cimdOwner.has(clientId)) {
    return { ok: false, reason: "cimd_client_not_allowlisted", clientId };
  }
  const secrets = form.getAll("client_secret");
  if (secrets.length === 1 && secrets[0] === "") {
    const next = new URLSearchParams(form);
    next.delete("client_secret");
    return { ok: true, form: next };
  }
  return { ok: true };
}

/** The client_id from a Basic Authorization header, or undefined. */
export function basicClientId(header) {
  if (!header || !/^basic\s+/i.test(header)) return undefined;
  try {
    const decoded = atob(header.replace(/^basic\s+/i, "").trim());
    const idx = decoded.indexOf(":");
    if (idx < 0) return undefined;
    return decodeURIComponent(decoded.slice(0, idx).replace(/\+/g, " "));
  } catch {
    return undefined;
  }
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
