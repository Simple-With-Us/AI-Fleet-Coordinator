// Hosted agent-sync MCP Worker (docs/protocols/agent-sync-mcp.md section 3):
// OAuth 2.1 with CIMD for ChatGPT and Grok and a hand-registered public PKCE
// client for Grok's manual form, arming, a consent page behind Cloudflare
// Access, a per-seat epoch and pause switch (Phase 0), and the seven agent-sync
// tools from tools.json, each seat posting as its own Zulip bot (Phase 2).
//
// Request order, outermost first:
//   1. Host check (404 for anything but agent-sync.jays.services) and a path
//      allowlist (404 for everything else).  GET /health is a static 200.
//   2. /oauth/token:  rate limit, Content-Type and Authorization checked the
//      way the library reads them, client_id allowlist before any fetch, blank
//      client_secret dropped, and the re-serialized form is what gets forwarded.
//   3. The OAuth library:  metadata, token endpoint, bearer check on /mcp.
//   4. /mcp:  seat, phase, epoch and grant age re-checked in the seat's
//      SeatGate, then the tools run with that seat's key only.
//      /authorize and /admin:  Access JWT re-verified, then our gates.

import { OAuthProvider, OAuthError, AuthorizationError, CimdFetchError } from "@cloudflare/workers-oauth-provider";
import { SeatGate } from "./seat-gate.js";
import {
  loadConfig,
  ISSUER,
  RESOURCE,
  RESOURCE_METADATA_URL,
  SCOPES,
  ACCESS_TOKEN_TTL_S,
  REFRESH_TOKEN_TTL_S,
  GATE_LOG_NAME,
  KNOWN_SEATS,
  PROPS_PHASE,
  SEAT_SECRETS,
} from "./config.js";
import {
  hostAllowed,
  classifyPath,
  preGateAuthorize,
  postGateAuthRequest,
  gateTokenForm,
  isFormContentType,
  readLimited,
  isUrlShapedClientId,
  cimdHostname,
  logSafe,
  sameOriginPost,
  safeEqual,
} from "./policy.js";
import { verifyAccessJwt } from "./access.js";
import { consentPage, noConnectionPage, errorPage, adminPage, htmlResponse, sentences, withReferrerPolicy } from "./pages.js";
import { serveMcpRequest } from "./mcp.js";
import { HostedTools } from "./hosted-tools.js";
import { ZulipClient } from "./zulip.js";
import { ConsentForm, AdminForm, CONSENT_ARRAY_KEYS, parseForm } from "./forms.js";

export { SeatGate };

const MAX_TOKEN_BODY = 16 * 1024;
const CSRF_COOKIE = "__Host-agent-sync-csrf";
const GROK_PLACEHOLDER_REDIRECT = `${ISSUER}/oauth/no-redirect-yet`;

function textResponse(body, status, headers = {}) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", ...headers } });
}

function notFound() {
  return textResponse("Not Found", 404);
}

function oauthJsonError(error, description, status, headers = {}) {
  return new Response(JSON.stringify({ error, error_description: description }), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers },
  });
}

function invalidToken(description) {
  return oauthJsonError("invalid_token", description, 401, {
    "WWW-Authenticate": `Bearer error="invalid_token", error_description="${description}", resource_metadata="${RESOURCE_METADATA_URL}"`,
  });
}

function gate(env, name) {
  if (name !== GATE_LOG_NAME && !KNOWN_SEATS.includes(name)) throw new Error("not a seat");
  return env.SEAT_GATE.get(env.SEAT_GATE.idFromName(name));
}

/**
 * Refusals go to one SeatGate instance.  `by` is the Access email, present only
 * on /authorize rows (those requests passed Access);  token rows are anonymous.
 */
async function logRefusal(env, { where, reason, clientId, redirectUri, by }) {
  const row = { where, reason: logSafe(reason, 80), client_id: logSafe(clientId), redirect_uri: logSafe(redirectUri), ...(by ? { by: logSafe(by, 120) } : {}) };
  console.log(JSON.stringify({ event: "refused", ...row }));
  try {
    await gate(env, GATE_LOG_NAME).logRefusal(row);
  } catch {
    console.log(JSON.stringify({ event: "refusal_log_failed", where }));
  }
}

async function sha256Ref(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(text)));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
}

// ---------------------------------------------------------------- provider

const providers = new WeakMap();

/**
 * One OAuthProvider per `env` object.  1.1.0's tokenExchangeCallback receives
 * no `env` (added in 1.2.0, #337), so the callback closes over the env it was
 * built for.  Workers hand every request in an isolate the same env object,
 * so this builds once per isolate.
 */
function providerFor(env) {
  let provider = providers.get(env);
  if (provider) return provider;
  provider = new OAuthProvider({
    apiRoute: "/mcp",
    apiHandler: { fetch: (request, e, ctx) => serveMcp(request, e, ctx) },
    defaultHandler: { fetch: (request, e, ctx) => serveApp(request, e, ctx) },
    authorizeEndpoint: `${ISSUER}/authorize`,
    tokenEndpoint: `${ISSUER}/oauth/token`,
    // DCR stays off (spec 3.3):  no clientRegistrationEndpoint.
    scopesSupported: [...SCOPES],
    accessTokenTTL: ACCESS_TOKEN_TTL_S,
    // Explicit:  the default is 30 days, and `undefined` would mean no expiry.
    refreshTokenTTL: REFRESH_TOKEN_TTL_S,
    allowImplicitFlow: false,
    allowPlainPKCE: false,
    allowTokenExchangeGrant: false,
    disallowPublicClientRegistration: true,
    clientIdMetadataDocumentEnabled: true,
    resourceMetadata: {
      resource: RESOURCE,
      // Byte-for-byte equal to the metadata `issuer` (the token endpoint's
      // origin), or ChatGPT drops to a per-connector callback.  Pinned by test.
      authorization_servers: [ISSUER],
      scopes_supported: [...SCOPES],
      bearer_methods_supported: ["header"],
      resource_name: "Agent-Sync",
    },
    tokenExchangeCallback: (options) => checkTokenExchange(env, options),
    onError: ({ code, status, internal }) => {
      console.log(JSON.stringify({ event: "oauth_error", code, status, category: internal?.category, reason: internal?.reason }));
    },
  });
  providers.set(env, provider);
  return provider;
}

/** Spec 3.5 step 2:  every code exchange and refresh re-checks the seat. */
async function checkTokenExchange(env, { grantType, props, userId, grantId }) {
  const config = loadConfig(env);
  const seat = props?.seat;
  if (typeof seat !== "string" || seat !== userId || !config.hostedSeats.includes(seat)) {
    throw new OAuthError("invalid_grant", { description: "This grant is not for a hosted seat." });
  }
  const seatGate = gate(env, seat);
  const result = await seatGate.check({ epoch: props.epoch, approvedAt: props.approved_at, phase: props.phase });
  await seatGate.audit({ event: "token_exchange", seat, grant_type: grantType, grant_ref: await sha256Ref(grantId), outcome: result.code });
  if (result.code === "paused") {
    // Not invalid_grant:  that would revoke the grant, and unpausing must work.
    throw new OAuthError("temporarily_unavailable", { description: "This seat is paused.", statusCode: 503, headers: { "Retry-After": "300" } });
  }
  if (result.code !== "ok") {
    // invalid_grant makes the library revoke this grant and its tokens.
    throw new OAuthError("invalid_grant", { description: "This grant is no longer valid." });
  }
}

// ---------------------------------------------------------------- /mcp

async function serveMcp(request, env, ctx) {
  const config = loadConfig(env);
  const props = ctx.props ?? {};
  const auth = ctx.auth ?? {};
  const seat = props.seat;
  if (typeof seat !== "string" || seat !== auth.userId || !config.hostedSeats.includes(seat)) {
    return invalidToken("This token is not for a hosted seat.");
  }
  const seatGate = gate(env, seat);
  const result = await seatGate.check({ epoch: props.epoch, approvedAt: props.approved_at, phase: props.phase });
  if (result.code !== "ok" && result.code !== "paused") return invalidToken("This grant is no longer valid.");

  const scopes = Array.isArray(auth.scope) ? auth.scope.map(String) : [];
  // The key comes from the seat in the verified props, through SEAT_SECRETS only.
  const secretNames = SEAT_SECRETS[seat];
  const key = typeof env[secretNames.key] === "string" ? env[secretNames.key].trim() : "";
  const email = config.botEmails[seat];
  const grantId = typeof auth.token === "string" ? auth.token.split(":")[1] ?? "" : "";
  const tools = new HostedTools({
    seat,
    scopes,
    clientId: typeof auth.clientId === "string" ? auth.clientId : "",
    grantRef: grantId ? await sha256Ref(grantId) : "",
    paused: result.code === "paused",
    config,
    zulip: key ? new ZulipClient({ email, key }) : null,
    key,
    gate: seatGate,
    cf: { asn: request.cf?.asn, country: request.cf?.country },
  });
  const authInfo = {
    token: auth.token,
    clientId: auth.clientId,
    scopes,
    ...(Number.isFinite(auth.expiresAt) ? { expiresAt: auth.expiresAt } : {}),
    extra: { seat },
  };
  const response = await serveMcpRequest(request, tools, authInfo);
  if (response.status === 403 && request.headers.has("origin")) {
    // Records whether any client sends a browser Origin (spec 3.2).
    console.log(JSON.stringify({ event: "mcp_origin_refused", origin_host: logSafe(safeHost(request.headers.get("origin")), 120) }));
  }
  return response;
}

function safeHost(origin) {
  try {
    return new URL(origin).host;
  } catch {
    return "unparseable";
  }
}

// ---------------------------------------------------------------- /authorize and /admin

async function serveApp(request, env, _ctx) {
  const config = loadConfig(env);
  const route = classifyPath(new URL(request.url).pathname);
  if (route !== "authorize" && route !== "admin") return notFound();

  const who = await verifyAccessJwt(request.headers.get("cf-access-jwt-assertion"), config.access);
  if (!who.ok) {
    console.log(JSON.stringify({ event: "access_refused", route, reason: who.reason }));
    return errorPage("Sign-In Required", "This page needs Cloudflare Access sign-in as the fleet owner.", 403);
  }
  const oauth = env.OAUTH_PROVIDER;
  if (route === "authorize") {
    if (request.method === "GET") return authorizeGet(request, env, config, oauth, who.email);
    if (request.method === "POST") return authorizePost(request, env, config, oauth, who.email);
    return textResponse("Method Not Allowed", 405, { Allow: "GET, POST" });
  }
  return serveAdmin(request, env, config, oauth, who.email);
}

async function authorizeGet(request, env, config, oauth, email) {
  const params = new URL(request.url).searchParams;
  const pre = preGateAuthorize(params, config);
  if (!pre.ok) {
    await logRefusal(env, { where: "authorize", ...pre, by: email });
    return errorPage("Request Refused", `This app and redirect are not on the allowlist (${pre.reason}).`, 400);
  }
  // Arming first, so an unarmed request never makes the library fetch a CIMD document.
  const seatState = await gate(env, pre.seat).getState();
  if (!seatState.armed) {
    await logRefusal(env, { where: "authorize", reason: "not_armed", clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    return noConnectionPage();
  }

  let authRequest;
  try {
    authRequest = await oauth.parseAuthRequest(request);
  } catch (error) {
    if (error instanceof AuthorizationError || error instanceof CimdFetchError) {
      await logRefusal(env, { where: "authorize", reason: `library_${error.code ?? error.reason ?? "refused"}`, clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
      return errorPage("Request Refused", "The OAuth library refused this request.", 400);
    }
    throw error;
  }
  const seat = postGateAuthRequest(authRequest, config);
  if (seat !== pre.seat) {
    await logRefusal(env, { where: "authorize", reason: "post_gate", clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    return errorPage("Request Refused", "This request does not match the allowlist.", 400);
  }
  const client = await oauth.lookupClient(authRequest.clientId);
  if (!client) return errorPage("Request Refused", "Unknown client.", 400);

  const consent = await oauth.beginConsent(authRequest);
  const grants = await listAllGrants(oauth, seat);
  return consentPage(
    {
      clientName: client.clientName ?? client.clientId,
      clientDomain: cimdHostname(client.clientId),
      clientId: client.clientId,
      redirectHost: new URL(authRequest.redirectUri).hostname,
      scopes: [...config.scopes],
      seat,
      handle: consent.handle,
      replacing: grants.map((g) => ({ client: g.metadata?.client_name ?? g.clientId, createdAt: toMs(g.createdAt) })),
    },
    consent.headers,
  );
}

function toMs(value) {
  if (!Number.isFinite(value)) return NaN;
  return value < 1e12 ? value * 1000 : value;
}

async function listAllGrants(oauth, seat) {
  const out = [];
  let cursor;
  for (let page = 0; page < 20; page++) {
    const result = await oauth.listUserGrants(seat, cursor ? { cursor } : undefined);
    out.push(...(result.items ?? []));
    if (!result.cursor) break;
    cursor = result.cursor;
  }
  return out;
}

async function authorizePost(request, env, config, oauth, email) {
  if (!sameOriginPost(request, config.issuer)) return errorPage("Request Refused", "Approve or deny from the consent page itself.", 403);
  const parsed = await readForm(request, ConsentForm, CONSENT_ARRAY_KEYS);
  if (!parsed.ok) return errorPage("Request Refused", "That form was not valid.  Go back to the consent page and try again.", 400);
  const { handle, decision, seat: postedSeat = "" } = parsed.data;

  if (decision !== "approve") {
    let denied;
    try {
      denied = await oauth.denyConsent(request, handle);
    } catch (error) {
      if (error instanceof AuthorizationError) return expiredPage();
      throw error;
    }
    const seat = postGateAuthRequest(denied.request, config);
    if (seat) await gate(env, seat).disarm({ by: email, reason: "denied" });
    const headers = withReferrerPolicy(denied.headers);
    if (!headers.has("Location")) headers.set("Location", denied.redirectTo);
    return new Response(null, { status: 302, headers });
  }

  const scopes = [...new Set(parsed.data.scope ?? [])].filter((s) => config.scopes.includes(s));
  if (scopes.length === 0) return errorPage("Pick A Scope", "Approve needs at least one scope.  Go back and tick one.", 400);

  let approved;
  try {
    approved = await oauth.approveConsent(request, handle, { scope: scopes });
  } catch (error) {
    if (error instanceof AuthorizationError) return expiredPage();
    throw error;
  }
  // The stored request, never the form, decides the seat family.
  const seat = postGateAuthRequest(approved.request, config);
  if (!seat || seat !== postedSeat) {
    await logRefusal(env, { where: "consent", reason: "seat_family_mismatch", clientId: approved.request.clientId, redirectUri: approved.request.redirectUri, by: email });
    return errorPage("Request Refused", "That seat cannot be bound to this app.", 400);
  }
  // Resolve the display name first:  it may fetch a CIMD document, and a failure
  // there must not come after the arming window is spent and the epoch bumped.
  // The only awaited external call after `approve` is completeAuthorization.
  let clientName = logSafe(approved.request.clientId, 120);
  try {
    const client = await oauth.lookupClient(approved.request.clientId);
    if (client?.clientName) clientName = logSafe(client.clientName, 120);
  } catch {
    console.log(JSON.stringify({ event: "client_lookup_failed", where: "consent" }));
  }
  const seatGate = gate(env, seat);
  const won = await seatGate.approve({ by: email });
  if (!won.ok) {
    return errorPage("Arming Window Closed", "The 10-minute window closed or was already used.  Arm the seat again in Agent-Sync Admin.", 409);
  }

  const approvedAt = Date.now();
  const { redirectTo } = await oauth.completeAuthorization({
    request: approved.request,
    userId: seat,
    metadata: { seat, client_name: clientName, approved_by: email, approved_at: approvedAt },
    scope: approved.request.scope,
    props: { seat, scopes: approved.request.scope, approved_at: approvedAt, epoch: won.epoch, client_id: approved.request.clientId, phase: PROPS_PHASE },
  });

  // D6:  one grant per seat.  The library only replaces grants of the same
  // client and resource, so every other grant of this seat is revoked here.
  let revoked = 0;
  for (const g of await listAllGrants(oauth, seat)) {
    if (g.metadata?.approved_at !== approvedAt) {
      await oauth.revokeGrant(g.id, seat);
      revoked++;
    }
  }
  await seatGate.audit({ event: "grant_created", seat, by: email, client: clientName, redirect_host: new URL(approved.request.redirectUri).host, scopes: approved.request.scope, epoch: won.epoch, revoked_others: revoked });

  const headers = withReferrerPolicy(approved.headers);
  headers.set("Location", redirectTo);
  return new Response(null, { status: 302, headers });
}

/** Parse a form body against a strict schema.  A body that is not a form at all is a refusal, not a 500. */
async function readForm(request, schema, arrayKeys) {
  let form;
  try {
    form = await request.formData();
  } catch {
    return { ok: false, reason: "form_unreadable" };
  }
  return parseForm(schema, form, arrayKeys);
}

function expiredPage() {
  return errorPage("This Page Expired", "The consent page was already used, expired after 10 minutes, or was opened in another browser.  Start the connection again from the app.", 400);
}

// ---------------------------------------------------------------- /admin

function readCookie(request, name) {
  const header = request.headers.get("cookie") ?? "";
  for (const part of header.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return rest.join("=");
  }
  return "";
}

function newCsrf() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const DONE_NOTICES = {
  arm: "Armed for 10 minutes.  Start the connection from the app now.",
  disarm: "Disarmed.",
  pause: "Paused.  Tools return paused and token refreshes fail until you unpause.",
  unpause: "Unpaused.",
  revoke: "Every grant for that seat is revoked and its epoch is bumped.",
  create_grok_client: "Grok manual client created.  Its client ID is in the table below.",
  update_grok_client: "Grok client redirect URIs synced from SEATS.",
};

async function serveAdmin(request, env, config, oauth, email) {
  const path = new URL(request.url).pathname;
  if (path === "/admin" && request.method === "GET") {
    const csrf = newCsrf();
    const done = new URL(request.url).searchParams.get("done");
    const seats = [];
    const audits = [];
    const calls = [];
    for (const seat of config.hostedSeats) {
      const g = gate(env, seat);
      const st = await g.getState();
      const grants = await listAllGrants(oauth, seat);
      const keyName = SEAT_SECRETS[seat].key;
      seats.push({
        seat,
        ...st,
        keyInstalled: typeof env[keyName] === "string" && env[keyName].trim() !== "",
        role: await g.roleStatus(),
        grants: grants.map((x) => ({ client: x.metadata?.client_name ?? x.clientId, scope: x.scope ?? [], createdAt: toMs(x.createdAt) })),
      });
      for (const row of await g.tail(15)) {
        const { ts, event, ...detail } = row;
        audits.push({ ts, seat, event, detail });
      }
      calls.push(...(await g.callTail(25)));
    }
    audits.sort((a, b) => b.ts - a.ts);
    calls.sort((a, b) => b.ts - a.ts);
    const clients = (await oauth.listClients()).items ?? [];
    const refusals = await gate(env, GATE_LOG_NAME).refusals(30);
    const tokenRefusals = await gate(env, GATE_LOG_NAME).tokenRefusals(15);
    return adminPage(
      {
        email,
        seats,
        clients: clients.filter((c) => !isUrlShapedClientId(c.clientId)),
        refusals,
        tokenRefusals,
        audits: audits.slice(0, 40),
        calls: calls.slice(0, 40),
        csrf,
        notice: DONE_NOTICES[done] ?? "",
        grokRedirectsConfigured: (config.seats["GROK-WEB"]?.redirectUris ?? []).length > 0,
      },
      { "Set-Cookie": `${CSRF_COOKIE}=${csrf}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600` },
    );
  }
  if (path === "/admin/action" && request.method === "POST") {
    if (!sameOriginPost(request, config.issuer, { requireFetchSite: true })) return errorPage("Request Refused", "Admin changes must come from the admin page.", 403);
    const parsed = await readForm(request, AdminForm, []);
    if (!parsed.ok) return errorPage("Request Refused", "That form was not valid.  Reload Agent-Sync Admin and try again.", 400);
    if (!safeEqual(parsed.data.csrf, readCookie(request, CSRF_COOKIE))) return errorPage("Request Refused", "The admin form expired.  Reload Agent-Sync Admin and try again.", 403);
    const { action, seat = "" } = parsed.data;
    const needsSeat = ["arm", "disarm", "pause", "unpause", "revoke"].includes(action);
    if (needsSeat && !config.hostedSeats.includes(seat)) return errorPage("Request Refused", "Unknown seat.", 400);
    switch (action) {
      case "arm":
        await gate(env, seat).arm({ by: email });
        break;
      case "disarm":
        await gate(env, seat).disarm({ by: email, reason: "admin" });
        break;
      case "pause":
        await gate(env, seat).setPaused({ by: email, paused: true });
        break;
      case "unpause":
        await gate(env, seat).setPaused({ by: email, paused: false });
        break;
      case "revoke": {
        // Kill switch step 2:  the epoch bump is instant, then the KV grants go.
        await gate(env, seat).bumpEpoch({ by: email, reason: "admin_revoke" });
        let n = 0;
        for (const g of await listAllGrants(oauth, seat)) {
          await oauth.revokeGrant(g.id, seat);
          n++;
        }
        await gate(env, seat).audit({ event: "revoke_all", by: email, revoked: n });
        break;
      }
      case "create_grok_client": {
        if (!config.hostedSeats.includes("GROK-WEB")) return errorPage("Request Refused", "GROK-WEB is not a hosted seat.", 400);
        const configured = config.seats["GROK-WEB"]?.redirectUris ?? [];
        const client = await oauth.createClient({
          clientName: "Grok (manual form, GROK-WEB)",
          redirectUris: configured.length ? [...configured] : [GROK_PLACEHOLDER_REDIRECT],
          tokenEndpointAuthMethod: "none",
          grantTypes: ["authorization_code", "refresh_token"],
          responseTypes: ["code"],
        });
        await gate(env, "GROK-WEB").audit({ event: "client_created", by: email, client_id: client.clientId, placeholder: configured.length === 0 });
        break;
      }
      case "update_grok_client": {
        const clientId = parsed.data.client_id ?? "";
        const configured = config.seats["GROK-WEB"]?.redirectUris ?? [];
        if (isUrlShapedClientId(clientId) || configured.length === 0) return errorPage("Nothing To Sync", "Add Grok's redirect URI to SEATS in wrangler.jsonc and redeploy first.", 400);
        const updated = await oauth.updateClient(clientId, { redirectUris: [...configured] });
        if (!updated) return errorPage("Request Refused", "Unknown client.", 400);
        await gate(env, "GROK-WEB").audit({ event: "client_redirects_synced", by: email, client_id: clientId });
        break;
      }
      default:
        return errorPage("Request Refused", "Unknown action.", 400);
    }
    return new Response(null, { status: 303, headers: withReferrerPolicy({ Location: `/admin?done=${encodeURIComponent(action)}`, "Cache-Control": "no-store" }) });
  }
  return notFound();
}

// ---------------------------------------------------------------- /oauth/token gate

/**
 * Gate POST /oauth/token before the library can fetch a client metadata
 * document.  Three rules keep this gate and the library reading the same
 * request:  the Content-Type test is the library's own expression, the
 * Authorization header must be strict Basic or is refused, and the request the
 * library receives is rebuilt from the form this gate validated (never the
 * original bytes).  Anything that is not a POST goes straight to the library,
 * which answers 405 before it reads a client id.
 */
async function gateToken(request, env, config) {
  if (request.method !== "POST") return request;
  if (env.TOKEN_RATE_LIMITER) {
    const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
    const { success } = await env.TOKEN_RATE_LIMITER.limit({ key: `token:${ip}` });
    if (!success) return oauthJsonError("temporarily_unavailable", "Too many token requests.", 429, { "Retry-After": "60" });
  }
  if (!isFormContentType(request.headers.get("content-type"))) {
    return oauthJsonError("invalid_request", "Content-Type must be application/x-www-form-urlencoded", 400);
  }
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_TOKEN_BODY) return oauthJsonError("invalid_request", "Request body too large.", 413);
  const text = await readLimited(request, MAX_TOKEN_BODY);
  if (text === null) return oauthJsonError("invalid_request", "Request body too large.", 413);
  const result = gateTokenForm(new URLSearchParams(text), request.headers.get("authorization"), config);
  if (!result.ok) {
    await logRefusal(env, { where: "token", reason: result.reason, clientId: result.clientId, redirectUri: "" });
    return oauthJsonError("invalid_client", "Client not allowed.", 401);
  }
  const headers = new Headers(request.headers);
  headers.delete("content-length");
  headers.set("content-type", "application/x-www-form-urlencoded");
  return { request: new Request(request.url, { method: "POST", headers, body: result.form.toString() }), form: result.form };
}

/**
 * Spec 3.5 step 3:  a refresh that fails because the refresh token is not the
 * grant's current or previous one (a replayed, stolen token) or names another
 * client pauses that seat.  The library's onError gets no seat, so this reads
 * it from the refresh token itself (`seat:grantId:secret`) and the library's
 * error description.  Only an existing grant can produce these two errors.
 */
async function pauseOnRefreshMismatch(env, config, form, response) {
  if (response.status !== 400 || form.get("grant_type") !== "refresh_token") return;
  const seat = String(form.get("refresh_token") ?? "").split(":")[0];
  if (!config.hostedSeats.includes(seat)) return;
  let body;
  try {
    body = await response.clone().json();
  } catch {
    return;
  }
  const reason = { "Invalid refresh token": "refresh_token_mismatch", "Client ID mismatch": "client_mismatch" }[body?.error_description];
  if (body?.error !== "invalid_grant" || !reason) return;
  console.log(JSON.stringify({ event: "alert_refresh_mismatch", seat, reason }));
  try {
    const seatGate = gate(env, seat);
    await seatGate.setPaused({ by: `system:${reason}`, paused: true });
  } catch {
    console.log(JSON.stringify({ event: "pause_failed", seat }));
  }
}

// ---------------------------------------------------------------- entry

export default {
  async fetch(request, env, ctx) {
    let config;
    try {
      config = loadConfig(env);
    } catch (error) {
      console.log(JSON.stringify({ event: "config_error", message: logSafe(error?.message, 200) }));
      return textResponse("Server misconfigured", 500);
    }
    if (!hostAllowed(request, config.host)) return notFound();
    const route = classifyPath(new URL(request.url).pathname);
    if (!route) return notFound();
    if (route === "health") {
      // Static liveness probe for the fleet admin panel:  no seat, config or
      // grant data, and it never reaches the OAuth library.
      if (request.method !== "GET" && request.method !== "HEAD") return textResponse("Method Not Allowed", 405, { Allow: "GET, HEAD" });
      return new Response(request.method === "HEAD" ? null : '{"ok":true}', { status: 200, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
    }
    if (route === "mcp" && config.mcpDisabled) return textResponse("The agent-sync MCP server is turned off.", 503, { "Retry-After": "3600" });
    if (route === "token") {
      const gated = await gateToken(request, env, config);
      if (gated instanceof Response) return gated;
      if (gated instanceof Request) return providerFor(env).fetch(gated, env, ctx);
      const response = await providerFor(env).fetch(gated.request, env, ctx);
      await pauseOnRefreshMismatch(env, config, gated.form, response);
      return response;
    }
    return providerFor(env).fetch(request, env, ctx);
  },
};

// For tests:  the HTML helpers render without the Worker runtime.
export const _internal = { sentences, htmlResponse };
