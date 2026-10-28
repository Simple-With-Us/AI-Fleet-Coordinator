// Hosted ST-MCP Worker (docs/protocols/agent-sync-mcp.md section 3):
// OAuth 2.1 with CIMD for ChatGPT and Grok, hand-registered public PKCE
// clients bound to one seat each (manual forms, the Instinct app's loopback
// callback), arming, a consent page behind Cloudflare
// Access, a per-seat epoch and pause switch (Phase 0), and the seven chat
// tools from tools.json, each seat posting as its own Zulip bot (Phase 2).
//
// Request order, outermost first:
//   1. Host check (404 for anything but agents.simplewithus.com) and a path
//      allowlist (404 for everything else).  GET /health is a static 200.
//   2. /oauth/token:  rate limit, Content-Type and Authorization checked the
//      way the library reads them, client_id allowlist before any fetch, blank
//      client_secret dropped, and the re-serialized form is what gets forwarded.
//   3. The OAuth library:  metadata, token endpoint, bearer check on /mcp.
//   4. /mcp:  seat, phase, epoch and grant age re-checked in the seat's
//      SeatGate, then the tools run with that seat's key only.
//      /authorize and /admin:  Access JWT re-verified, then our gates.
//   5. /internal/wake/<SEAT>:  the server listener's signed wake (src/wake.js),
//      handled before the OAuth library and outside Cloudflare Access.  It
//      fans out an MCP event to that seat's subscriptions (src/events.js).
//   6. /internal/spike/<SEAT> (AFC-477, temporary):  one signed, hand-fired
//      `fleet.spike.ping` to that seat's spike subscription (src/spike.js),
//      a 404 unless SPIKE_EXTRA_EVENTS is "on" and SPIKE_HMAC_KEY is set.

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
  PROPS_PHASE,
  SEAT_SECRETS,
  SEAT_LABELS,
  WAKE_SECRETS,
  KNOWN_SEATS,
} from "./config.js";
import {
  hostAllowed,
  classifyPath,
  preGateAuthorize,
  postGateAuthRequest,
  hostedRedirectSeats,
  resolveArmedSeat,
  clientBoundToSeat,
  manualClientSeat,
  isLoopbackRedirect,
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
import { consentPage, noConnectionPage, errorPage, adminPage, htmlResponse, sentences, withReferrerPolicy, gbBearerMintCompletedPage, gbBearerRevealPage, gbBearerMintRefusedPage } from "./pages.js";
import { gbBearerFromRequest, sbtBearerFromRequest, parseGbBearer, parseSbtBearer, newGbBearer, newSeatBearer, sha256Hex, GB_BEARER_TTL_MS } from "./gb-bearer.js";
import { BEARER_SEATS, ADDITIONAL_BEARER_SEATS, resolveBearerSeatCredentials } from "./bearer-identities.js";
import { serveGrBearerVerify } from "./gr-verify.js";
import { resolveGrSecrets, grClientFromResolved } from "./gr-tools.js";
import { serveMcpRequest } from "./mcp.js";
import { HostedTools } from "./hosted-tools.js";
import { IdentityRouter, resolveDelegatedCredentials, GB_IDENTITIES } from "./delegated-identities.js";
import { gate } from "./gate.js";
import { ToolError } from "./contract.js";
import { recallClientFromResolved, resolveRecallSecrets } from "./recall.js";
import { fleetlinkClientFromSecret, resolveFleetlinkSeatTokens } from "./fleetlink.js";
import { LinearClient, linearClientFromPair, resolveLinearPair, LINEAR_SEATS, linearSecretNames, seatTagForSeat } from "./linear.js";
import { ZulipClient } from "./zulip.js";
import { listenerClientFromEnv } from "./eavesdropper.js";
import { operatorNotesClientFromEnv } from "./notes.js";
import { ConsentForm, AdminForm, CONSENT_ARRAY_KEYS, parseForm } from "./forms.js";
import { SeatEvents, deliverWake, wakeReply, WAKE_BUDGET_MS, EVENT_NAME } from "./events.js";
import { verifyWake, wakePathSeat, readBodyBytes, WAKE_SIGNATURE_HEADER, WAKE_MAX_BODY } from "./wake.js";
import { verifyHealth, healthKey, healthReport, HEALTH_SIGNATURE_HEADER, HEALTH_MAX_BODY } from "./health.js";
import { serveWa } from "./wa/index.js";
import { resolveSecret, resolveWakeKeys, infisicalStats, infisicalMode, prefetchFolder } from "./infisical.js";
import { verifySpike, spikePathSeat, spikeKey, deliverSpike, spikeReply, SPIKE_SIGNATURE_HEADER, SPIKE_MAX_BODY, SPIKE_RATE_LIMIT, SPIKE_RATE_WINDOW_MS } from "./spike.js";

export { SeatGate };

const MAX_TOKEN_BODY = 16 * 1024;
const CSRF_COOKIE = "__Host-agent-sync-csrf";
// A manual client for a seat with no redirect URI yet gets this one, which no
// seat lists, so the app's first attempt is refused and its real callback is
// logged on /admin.
const MANUAL_PLACEHOLDER_REDIRECT = `${ISSUER}/oauth/no-redirect-yet`;

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

/**
 * Refusals go to one SeatGate instance.  `by` is the Access email, present only
 * on /authorize rows (those requests passed Access);  token rows are anonymous.
 */
async function logRefusal(env, { where, reason, clientId, redirectUri, resource, by }) {
  const row = { where, reason: logSafe(reason, 80), client_id: logSafe(clientId), redirect_uri: logSafe(redirectUri), ...(resource ? { resource: logSafe(resource, 200) } : {}), ...(by ? { by: logSafe(by, 120) } : {}) };
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

// Persist only fixed reason codes for pre-tool failures.  This must never
// change the response if diagnostics storage itself is unavailable.
async function recordDiagnostic(env, entry) {
  try { await gate(env, GATE_LOG_NAME).recordDiagnostic(entry); }
  catch { console.log(JSON.stringify({ event: "diagnostic_unavailable", kind: entry.kind })); }
}

// ---------------------------------------------------------------- /mcp

/** Raw request bytes for one /mcp POST:  read with a hard cap BEFORE the MCP
 * SDK parses (kody 4372745946).  A generous quarter megabyte - the largest
 * honest call is a long PR body, and the relay caps its own requests at 64
 * KiB anyway. */
const MCP_MAX_BODY = 262_144;

async function serveMcp(request, env, ctx) {
  if (request.method === "POST") {
    const bytes = await readBodyBytes(request, MCP_MAX_BODY);
    if (bytes === null) return textResponse("Payload Too Large", 413);
    request = new Request(request.url, { method: "POST", headers: request.headers, body: bytes });
  }
  const config = loadConfig(env);
  // One listing per folder beats a cold per-name burst (sentry 4364275308);
  // the listings also make later optional misses subrequest-free.
  await Promise.all(["/zulip", "/linear", "/fleetlink", "/wake", "/tools"].map((path) => prefetchFolder(env, path)));
  const props = ctx.props ?? {};
  const auth = ctx.auth ?? {};
  const seat = props.seat;
  if (typeof seat !== "string" || seat !== auth.userId || !config.hostedSeats.includes(seat)) {
    return invalidToken("This token is not for a hosted seat.");
  }
  const seatGate = gate(env, seat);
  const result = await seatGate.check({ epoch: props.epoch, approvedAt: props.approved_at, phase: props.phase });
  if (result.code !== "ok" && result.code !== "paused") {
    const refused = invalidToken("This grant is no longer valid.");
    try {
      await seatGate.audit({ event: "mcp_refused", reason: result.code, status: 401 });
      refused.headers.set("X-Agent-Sync-Diagnostic", "recorded");
    } catch {
      console.log(JSON.stringify({ event: "diagnostic_unavailable", kind: "mcp_refused", seat }));
    }
    return refused;
  }

  const scopes = Array.isArray(auth.scope) ? auth.scope.map(String) : [];
  // The key comes from the seat in the verified props, through SEAT_SECRETS only.
  // Canonical Fleet-OPS /zulip record (<SEAT>_ZULIP_KEY, verified live); the
  // legacy binding is the bounded transitional fallback only.  Optional (kody
  // 4365622648):  a seat key absent from Infisical leaves that seat's Zulip
  // tools not_configured, as a missing binding did before;  an outage or a
  // scope denial still throws in required mode.
  const seatKeyRead = (names) => resolveSecret(env, { name: names.infisical.name, path: names.infisical.path, envName: names.key, optional: true });
  const secretNames = SEAT_SECRETS[seat];
  const key = await seatKeyRead(secretNames);
  const email = config.botEmails[seat];
  const grantId = typeof auth.token === "string" ? auth.token.split(":")[1] ?? "" : "";
  // Resolve only this verified principal's Linear pair, never unused peers.
  const linearPair = await resolveLinearPair(env, seatTagForSeat(seat));
  // FleetLink's agent secret resolves through /fleetlink (read-tools scope);
  // the folder awaits the population approval, so fallback mode covers the
  // gap observably and required mode throws visibly until it is populated.
  const fleetlinkSecret = await resolveSecret(env, { name: "FLEETLINK_AGENT_SECRET", path: "/fleetlink", envName: "FLEETLINK_AGENT_SECRET", optional: true });
  const fleetlinkTokens = await resolveFleetlinkSeatTokens(env, [seat]);
  const recallSecrets = await resolveRecallSecrets(env);

  // The listener key resolves through Infisical too (optional:  no wake key
  // means no listener tools, never a 500 - src/eavesdropper.js).
  const listenerWakeKey = Object.hasOwn(WAKE_SECRETS, seat)
    ? await resolveSecret(env, { name: WAKE_SECRETS[seat], path: "/wake", envName: WAKE_SECRETS[seat], optional: true })
    : "";
  const tools = new HostedTools({
    seat,
    scopes,
    clientId: typeof auth.clientId === "string" ? auth.clientId : "",
    grantRef: grantId ? await sha256Ref(grantId) : "",
    paused: result.code === "paused",
    config,
    zulip: key ? new ZulipClient({ email, key, ownerUserId: config.ownerUserId }) : null,
    key,
    gate: seatGate,
    whatsappGate: gate(env, "JET"),
    cf: { asn: request.cf?.asn, country: request.cf?.country },
    // Null when any recall secret is missing:  the recall tools then answer
    // not_configured and every other tool works as before (spec 1.1).
    recall: recallClientFromResolved(recallSecrets, env),
    // Null when this seat has no Linear app credentials, or when the seat is
    // not on LINEAR_SEATS:  the Linear tools then answer not_configured and
    // every other tool works as before (AFC-289).
    linear: linearClientFromPair(linearPair, seat, { gate: seatGate }),
    linearWrites: String(env.LINEAR_TOOLS_WRITES ?? "") === "1",
    // Null without FLEETLINK_AGENT_SECRET:  the FleetLink tools then answer
    // not_configured and every other tool works as before (AFC-328).
    fleetlink: fleetlinkClientFromSecret(fleetlinkSecret, env, { resolvedTokens: fleetlinkTokens }),
    // Null unless LISTENER_API_URL and this seat's WAKE_HMAC_KEY_<SEAT> are set:  the listener_*
    // tools then answer not_configured and every other tool works as before (AFC-330).
    listener: listenerClientFromEnv(env, seat, { wakeKey: listenerWakeKey }),
    // Null unless LISTENER_API_URL and OPERATOR_NOTES_KEY are set:  the notes_* tools then answer not_configured
    // and every other tool works as before (AFC-486).  The seat a request names is always this HostedTools' seat.
    notes: operatorNotesClientFromEnv(env),
  });
  // The authenticated grant assigns the acting principal. No per-call peer or
  // persona credential loader is installed, even for legacy shared OAuth seats.
  const routedTools = new IdentityRouter({ callerTools: tools, credentials: null, createIdentity: null });
  const authInfo = {
    token: auth.token,
    clientId: auth.clientId,
    scopes,
    ...(Number.isFinite(auth.expiresAt) ? { expiresAt: auth.expiresAt } : {}),
    extra: { seat },
  };
  // Events only for a seat the listener can wake (its WAKE_HMAC_KEY_<SEAT> is
  // set):  any other seat would list an event that never fires.
  // Resolves through Infisical /wake when INFISICAL_MODE allows; the
  // legacy WAKE_HMAC_KEY_<SEAT> binding is the bounded transitional fallback.
  // A seat with no wake secret name (CURSOR, CLAUDE, MA, JET-CHAT) reads
  // nothing:  no lookup of an undefined name, no counted fallback.
  const wakeSecret = Object.hasOwn(WAKE_SECRETS, seat) ? listenerWakeKey : "";
  const events = typeof wakeSecret === "string" && wakeSecret.trim() ? new SeatEvents({
    seat,
    scopes,
    epoch: props.epoch,
    approvedAt: props.approved_at,
    paused: result.code === "paused",
    gate: seatGate,
    config: { ...config, eventSeat: seat },
    // A `channel` filter must name a channel the seat may use (src/channels.js).
    channels: () => tools.checkedChannelSet(),
    clientId: typeof auth.clientId === "string" ? auth.clientId : "",
    logRefusal: (row) => logRefusal(env, row),
  }) : null;
  const response = await serveMcpRequest(request, routedTools, authInfo, events);
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

// ---------------------------------------------------------------- /mcp with a per-bot bearer

/**
 * A Grok Bot persona's own bearer (src/gb-bearer.js) in place of an OAuth token.  The token names its persona;
 * that persona's SeatGate holds the hash.  The router is built with NO createIdentity, so the connection acts
 * only as that persona (allowedIdentities for a non-shared seat is just itself).  Every refusal is the same
 * generic 401.  Paused personas, an unset credential bundle and an expired bearer all refuse.
 */
async function serveGbBearerMcp(request, env, token) {
  const config = loadConfig(env);
  const refuse = () => invalidToken("This token is not valid.");
  const parsed = parseGbBearer(token);
  if (!parsed) return refuse();
  const { identity } = parsed;
  const identityGate = gate(env, identity);
  const checked = await identityGate.gbBearerCheck(await sha256Hex(parsed.token));
  if (!checked.ok) {
    // Only a token whose hash matched is logged (expired).  A wrong guess is not:  anyone can send one, and the
    // audit log is capped, so guesses would push real mint and revoke rows out.  They do count
    // against the claimed identity's mismatch counter (counts only, /admin-visible).
    if (checked.reason === "expired") {
      try { await identityGate.audit({ event: "gb_bearer_refused", reason: checked.reason }); } catch { /* A missing row is a gap in the log, not a hole. */ }
    } else if (checked.reason === "mismatch") {
      // Static configured set, not roles.keys():  the counter must still
      // work during a delegated-credential outage, when the roles map is
      // incomplete - an attacker's guesses do not pause for our outage.
      try { await identityGate.gbBearerMismatch(identity, new Set(GB_IDENTITIES)); } catch { /* A counter is observability, never the security path. */ }
    }
    return refuse();
  }
  // A valid hash is not consent for hosted tool access.  Legacy missing rows
  // and explicit GR-only rows both refuse before loading any persona bundle.
  if (checked.mcp_accept !== true) return refuse();
  await Promise.all(["/zulip", "/linear"].map((path) => prefetchFolder(env, path)));
  const roles = (await resolveDelegatedCredentials(env)).roles;
  const credentials = roles.get(identity);
  if (!credentials) return refuse();
  const state = await identityGate.getState();
  if (state.paused) return invalidToken("This identity is paused.");
  const scopes = ["zulip:read", "zulip:write"];
  const grantRef = await sha256Ref(`gb-bearer:${identity}`);
  await Promise.all(["/fleetlink", "/tools"].map((path) => prefetchFolder(env, path)));
  const fleetlinkSecret = await resolveSecret(env, { name: "FLEETLINK_AGENT_SECRET", path: "/fleetlink", envName: "FLEETLINK_AGENT_SECRET", optional: true });
  const fleetlinkTokens = await resolveFleetlinkSeatTokens(env, [identity]);
  const recallSecrets = await resolveRecallSecrets(env);
  const grSecrets = await resolveGrSecrets(env);
  const tools = new HostedTools({
    seat: identity,
    callerSeat: identity,
    callerGate: identityGate,
    verifySeat: true,
    scopes,
    clientId: "gb-bearer",
    grantRef,
    paused: false,
    gate: identityGate,
    // The GR front authenticates to the relay with this same bearer:  the
    // relay resolves it to the identity and applies its own allowlist.
    gr: grClientFromResolved(grSecrets, env, { bearer: token }),
    cf: { asn: request.cf?.asn, country: request.cf?.country },
    recall: recallClientFromResolved(recallSecrets, env),
    fleetlink: fleetlinkClientFromSecret(fleetlinkSecret, env, { resolvedTokens: fleetlinkTokens }),
    notes: operatorNotesClientFromEnv(env),
    linearWrites: String(env.LINEAR_TOOLS_WRITES ?? "") === "1",
    delegatedLinear: true,
    config: { ...config, botEmails: { ...config.botEmails, [identity]: credentials.email } },
    zulip: new ZulipClient({ email: credentials.email, key: credentials.zulipKey, ownerUserId: config.ownerUserId }),
    key: credentials.zulipKey,
    linear: new LinearClient({
      seat: identity,
      clientId: credentials.linearClientId,
      clientSecret: credentials.linearClientSecret,
      gate: identityGate,
      fetch: globalThis.fetch.bind(globalThis),
    }),
  });
  const routed = new IdentityRouter({ callerTools: tools, credentials: null, createIdentity: null });
  const authInfo = { token: grantRef, clientId: "gb-bearer", scopes, extra: { seat: identity } };
  return serveMcpRequest(request, routed, authInfo, null);
}

// ---------------------------------------------------------------- /mcp with a seat bearer

/**
 * A seat's own admin-minted bearer (sbt_<seat>_..., src/gb-bearer.js) in place of
 * its OAuth grant.  Accepted ONLY when the mint recorded the owner's per-seat
 * choice (mcp_accept, shown with its disclosure in /admin):  a bearer that
 * bypasses the consent flow is never inferred.  The connection acts only as
 * that seat (no createIdentity, whatever shared group it is in).  Every refusal
 * is the same generic 401.  A paused seat, an unhosted seat, a missing key and
 * an expired bearer all refuse.
 */
async function serveSbtBearerMcp(request, env, token) {
  const config = loadConfig(env);
  const refuse = () => invalidToken("This token is not valid.");
  const parsed = parseSbtBearer(token);
  if (!parsed) return refuse();
  const { identity: seat } = parsed;
  await Promise.all(["/zulip", "/linear"].map((path) => prefetchFolder(env, path)));
  const seatGate = gate(env, seat);
  const checked = await seatGate.gbBearerCheck(await sha256Hex(parsed.token));
  if (!checked.ok) {
    // Same cap discipline as serveGbBearerMcp:  only an expired-but-matching
    // bearer writes a row;  guesses are free to send.  They do count against
    // the seat's mismatch counter (counts only, /admin-visible).
    if (checked.reason === "expired") {
      try { await seatGate.audit({ event: "gb_bearer_refused", reason: checked.reason }); } catch { /* A missing row is a gap in the log, not a hole. */ }
    } else if (checked.reason === "mismatch") {
      // Static seat set for the same outage reason as the persona path.
      try { await seatGate.gbBearerMismatch(seat, new Set(BEARER_SEATS)); } catch { /* A counter is observability, never the security path. */ }
    }
    return refuse();
  }
  const credentials = await resolveBearerSeatCredentials(env, config, seat);
  if (!credentials) return refuse();
  const { key, email } = credentials;
  if (checked.mcp_accept !== true) return refuse();
  const state = await seatGate.getState();
  if (state.paused) return invalidToken("This identity is paused.");
  const scopes = ["zulip:read", "zulip:write"];
  const grantRef = await sha256Ref(`sbt-bearer:${seat}`);
  await Promise.all(["/fleetlink", "/tools"].map((path) => prefetchFolder(env, path)));
  const fleetlinkSecret = await resolveSecret(env, { name: "FLEETLINK_AGENT_SECRET", path: "/fleetlink", envName: "FLEETLINK_AGENT_SECRET", optional: true });
  const fleetlinkTokens = await resolveFleetlinkSeatTokens(env, [seat]);
  const recallSecrets = await resolveRecallSecrets(env);
  // The listener key resolves through Infisical too (optional:  no wake key
  // means no listener tools, never a 500 - src/eavesdropper.js).
  const listenerWakeKey = Object.hasOwn(WAKE_SECRETS, seat)
    ? await resolveSecret(env, { name: WAKE_SECRETS[seat], path: "/wake", envName: WAKE_SECRETS[seat], optional: true })
    : "";
  const grSecrets = await resolveGrSecrets(env);
  const tools = new HostedTools({
    seat,
    callerSeat: seat,
    callerGate: seatGate,
    verifySeat: true,
    scopes,
    clientId: "sbt-bearer",
    grantRef,
    paused: false,
    config: { ...config, botEmails: { ...config.botEmails, [seat]: email } },
    // Same pass-through as the persona path:  the relay authenticates this
    // bearer and enforces the seat's own allowlist.
    gr: grClientFromResolved(grSecrets, env, { bearer: token }),
    zulip: new ZulipClient({ email, key, ownerUserId: config.ownerUserId }),
    key,
    gate: seatGate,
    cf: { asn: request.cf?.asn, country: request.cf?.country },
    recall: recallClientFromResolved(recallSecrets, env),
    linear: linearClientFromPair(await resolveLinearPair(env, seatTagForSeat(seat)), seat, { gate: seatGate }),
    linearWrites: String(env.LINEAR_TOOLS_WRITES ?? "") === "1",
    // Additional identities never fall back to the shared FleetLink credential.
    fleetlink: fleetlinkClientFromSecret(Object.hasOwn(ADDITIONAL_BEARER_SEATS, seat) ? "" : fleetlinkSecret, env, { resolvedTokens: fleetlinkTokens }),
    listener: listenerClientFromEnv(env, seat, { wakeKey: listenerWakeKey }),
    notes: operatorNotesClientFromEnv(env),
  });
  const routed = new IdentityRouter({ callerTools: tools, credentials: null, createIdentity: null });
  const authInfo = { token: grantRef, clientId: "sbt-bearer", scopes, extra: { seat } };
  return serveMcpRequest(request, routed, authInfo, null);
}

// ---------------------------------------------------------------- /internal/wake/<SEAT>

function jsonResponse(body, status, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers } });
}

/**
 * The listener's wake (src/wake.js):  signature, freshness and shape first,
 * then a wake_id claim in the seat's SeatGate, then the signed deliveries
 * inside this request (at most WAKE_BUDGET_MS from its arrival), and an answer
 * that says how they went (events.js wakeReply).  A wake every delivery of
 * which failed in a way worth retrying is forgotten and answered 503, so the
 * listener sends it again.  A settled wake_id is a 200 duplicate, so the
 * listener's retry after a lost response never wakes Jet twice.
 */
async function serveWake(request, env, ctx, config) {
  const started = Date.now();
  const seat = wakePathSeat(new URL(request.url).pathname);
  if (!seat || !config.hostedSeats.includes(seat) || !Object.hasOwn(WAKE_SECRETS, seat)) return notFound();
  const rawKey = await resolveSecret(env, { name: WAKE_SECRETS[seat], path: "/wake", envName: WAKE_SECRETS[seat], optional: true });
  const key = typeof rawKey === "string" ? rawKey.trim() : "";
  if (!key) {
    await recordDiagnostic(env, { kind: "wake", reason: "wake_key_missing", seat, status: 404 });
    return notFound();
  }
  if (request.method !== "POST") {
    await recordDiagnostic(env, { kind: "wake", reason: "method_not_allowed", seat, status: 405 });
    return textResponse("Method Not Allowed", 405, { Allow: "POST" });
  }
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > WAKE_MAX_BODY) {
    await recordDiagnostic(env, { kind: "wake", reason: "payload_too_large", seat, status: 413 });
    return textResponse("Payload Too Large", 413);
  }
  const bytes = await readBodyBytes(request, WAKE_MAX_BODY);
  if (bytes === null) {
    await recordDiagnostic(env, { kind: "wake", reason: "payload_too_large", seat, status: 413 });
    return textResponse("Payload Too Large", 413);
  }
  const checked = await verifyWake({ bytes, signature: request.headers.get(WAKE_SIGNATURE_HEADER), key, seat });
  if (!checked.ok) {
    await recordDiagnostic(env, { kind: "wake", reason: checked.reason, seat, status: checked.status });
    console.log(JSON.stringify({ event: "wake_refused", seat, reason: checked.reason }));
    return checked.status === 404 ? notFound() : textResponse(checked.status === 401 ? "Unauthorized" : "Bad Request", checked.status);
  }
  const wake = checked.wake;
  if (config.mcpDisabled) {
    // The MCP kill switch stops events too.  Not 5xx:  the listener would retry for nothing.
    console.log(JSON.stringify({ event: "wake", seat, verdict: "mcp_disabled", message_id: wake.message_id }));
    return jsonResponse({ ok: true, disabled: true, subscribers: 0 }, 202);
  }
  const seatGate = gate(env, seat);
  const claim = await seatGate.eventClaimWake({ wakeId: wake.wake_id, messageId: wake.message_id, name: EVENT_NAME });
  let result = claim;
  if (claim.verdict === "go") {
    // The answer waits for the deliveries, and waitUntil keeps them (and the
    // settle that ends the claim) running if the listener hangs up first.
    const work = deliverWake({ gate: seatGate, wake, claim, channels: () => wakeChannels(env, config, seat, seatGate), fetchFn: (...a) => fetch(...a), deadline: started + WAKE_BUDGET_MS });
    ctx.waitUntil(work.catch(() => {}));
    result = await work;
  }
  console.log(JSON.stringify({ event: "wake", seat, verdict: claim.verdict, outcome: result.outcome ?? null, subscribers: claim.subs?.length ?? 0, delivered: result.delivered ?? 0, message_id: wake.message_id }));
  const reply = wakeReply(result);
  return jsonResponse(reply.body, reply.status, reply.retryAfterS ? { "Retry-After": String(reply.retryAfterS) } : {});
}

// ---------------------------------------------------------------- /internal/spike/<SEAT> (AFC-477)

/**
 * One hand-fired `fleet.spike.ping` (src/spike.js).  Every way it is not set
 * up is a 404:  SPIKE_EXTRA_EVENTS not "on", no SPIKE_HMAC_KEY, a seat that is
 * not hosted or has no wake key (no events are offered to it), or a
 * SPIKE_HMAC_KEY equal to any seat's wake key, which must never sign a spike.
 * Then the signature, freshness and shape, then SeatGate.spikeClaim (dedupe,
 * rate limit, pause), then delivery inside this request.  The answer holds
 * counts only (spikeReply).
 */
async function serveSpike(request, env, ctx, config) {
  const started = Date.now();
  if (!config.spikeExtraEvents) return notFound();
  const seat = spikePathSeat(new URL(request.url).pathname);
  if (!seat || !config.hostedSeats.includes(seat) || !Object.hasOwn(WAKE_SECRETS, seat)) return notFound();
  const wakeKey = await resolveSecret(env, { name: WAKE_SECRETS[seat], path: "/wake", envName: WAKE_SECRETS[seat], optional: true });
  const key = spikeKey(env.SPIKE_HMAC_KEY);
  if (!wakeKey || !key) return notFound();
  // SPIKE_HMAC_KEY must never equal any seat's wake key, not only this seat's.
  const wakeKeys = await resolveWakeKeys(env, Object.values(WAKE_SECRETS));
  if (wakeKeys.includes(key)) {
    console.log(JSON.stringify({ event: "spike_refused", seat, reason: "key_reused" }));
    return notFound();
  }
  if (request.method !== "POST") return textResponse("Method Not Allowed", 405, { Allow: "POST" });
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > SPIKE_MAX_BODY) return textResponse("Payload Too Large", 413);
  const bytes = await readBodyBytes(request, SPIKE_MAX_BODY);
  if (bytes === null) return textResponse("Payload Too Large", 413);
  const checked = await verifySpike({ bytes, signature: request.headers.get(SPIKE_SIGNATURE_HEADER), key, seat });
  if (!checked.ok) {
    console.log(JSON.stringify({ event: "spike_refused", seat, reason: checked.reason }));
    return checked.status === 404 ? notFound() : textResponse(checked.status === 401 ? "Unauthorized" : checked.status === 413 ? "Payload Too Large" : "Bad Request", checked.status);
  }
  const spike = checked.spike;
  if (config.mcpDisabled) {
    console.log(JSON.stringify({ event: "spike", seat, verdict: "mcp_disabled" }));
    return jsonResponse({ ok: true, disabled: true, subscribers: 0, delivered: 0 }, 202);
  }
  const seatGate = gate(env, seat);
  const claim = await seatGate.spikeClaim({ spikeId: spike.spike_id, max: SPIKE_RATE_LIMIT, windowMs: SPIKE_RATE_WINDOW_MS });
  let result = claim;
  if (claim.verdict === "go") {
    const work = deliverSpike({ gate: seatGate, spike, subs: claim.subs, fetchFn: (...a) => fetch(...a), deadline: started + WAKE_BUDGET_MS });
    ctx.waitUntil(work.catch(() => {}));
    result = { verdict: "go", ...(await work) };
    try {
      await seatGate.audit({ event: "spike_settled", spike_ref: spike.spike_id.slice(0, 8), outcome: result.outcome, delivered: result.delivered });
    } catch {
      console.log(JSON.stringify({ event: "spike_settle_audit_failed" }));
    }
  }
  console.log(JSON.stringify({ event: "spike", seat, verdict: claim.verdict, outcome: result.outcome ?? null, subscribers: result.subscribers ?? 0, delivered: result.delivered ?? 0 }));
  const reply = spikeReply(result);
  return jsonResponse(reply.body, reply.status, reply.retryAfterS ? { "Retry-After": String(reply.retryAfterS) } : {});
}

// ---------------------------------------------------------------- /internal/health/linear

/**
 * The Linear token health of every seat that has Linear credentials (the six
 * LINEAR_SEATS and the twelve Grok Bot personas), for Muster's launchd reader.
 * 404 when HEALTH_HMAC_KEY is unset or equals any seat's wake key, POST only,
 * signed.  The body holds ages, counts and fixed codes:  no token or credential.
 */
async function serveLinearHealth(request, env, config) {
  const key = healthKey(env.HEALTH_HMAC_KEY);
  if (!key) return notFound();
  // Every wake key Infisical serves, as serveSpike reads them (sentry
  // 4364275301):  in required mode the wake bindings may be gone, and an env
  // read would miss a health key equal to a wake key.
  const wakeKeys = await resolveWakeKeys(env, Object.values(WAKE_SECRETS));
  const spikeKeyRaw = typeof env.SPIKE_HMAC_KEY === "string" ? env.SPIKE_HMAC_KEY.trim() : "";
  if (wakeKeys.includes(key) || (spikeKeyRaw && spikeKeyRaw === key)) {
    console.log(JSON.stringify({ event: "health_refused", reason: "key_reused" }));
    return notFound();
  }
  if (request.method !== "POST") return textResponse("Method Not Allowed", 405, { Allow: "POST" });
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > HEALTH_MAX_BODY) return textResponse("Payload Too Large", 413);
  const bytes = await readBodyBytes(request, HEALTH_MAX_BODY);
  if (bytes === null) return textResponse("Payload Too Large", 413);
  const checked = await verifyHealth({ bytes, signature: request.headers.get(HEALTH_SIGNATURE_HEADER), key });
  if (!checked.ok) {
    console.log(JSON.stringify({ event: "health_refused", reason: checked.reason }));
    return checked.status === 404 ? notFound() : textResponse(checked.status === 401 ? "Unauthorized" : checked.status === 413 ? "Payload Too Large" : "Bad Request", checked.status);
  }
  const identities = [];
  const missing = [];
  // /zulip too:  the Grok Bot persona rows below read it (kody 4365622780).
  await Promise.all(["/zulip", "/linear"].map((path) => prefetchFolder(env, path)));
  for (const seat of LINEAR_SEATS) {
    if (!linearSecretNames(seat) || !config.hostedSeats.includes(seat)) continue;
    // Resolved through Infisical (src/linear.js), not env:  in required mode
    // the legacy bindings are absent and an env read would misreport missing.
    const pair = await resolveLinearPair(env, seat);
    if (pair && pair.clientId && pair.clientSecret) identities.push(seat);
    else missing.push(seat);
  }
  const delegated = await resolveDelegatedCredentials(env);
  for (const identity of delegated.roles.keys()) identities.push(identity);
  const rows = await Promise.all(identities.map(async (seat) => {
    try {
      return { seat, row: await gate(env, seat).linearHealth() };
    } catch {
      return { seat, row: { unreadable: true } };
    }
  }));
  for (const seat of missing) rows.push({ seat, row: { missing: true } });
  return jsonResponse({ ...healthReport(rows, Math.floor(Date.now() / 1000)), bundle: delegated.valid ? "ok" : "invalid" }, 200);
}

/**
 * The seat's allowed channels for a wake (src/channels.js), read with the
 * seat's own key after the same member-only role check the tools make.  Null
 * (every channel wake refused) when no key is installed or the bot is
 * refused;  a Zulip failure throws, which deliverWake answers with a retry.
 */
async function wakeChannels(env, config, seat, seatGate) {
  // Optional like every seat-key read (kody 4365622648):  an absent key is "no
  // channels", which the caller already handles;  an outage still throws.
  const key = await resolveSecret(env, { name: SEAT_SECRETS[seat].infisical.name, path: SEAT_SECRETS[seat].infisical.path, envName: SEAT_SECRETS[seat].key, optional: true });
  if (!key) return null;
  const tools = new HostedTools({ seat, scopes: [], config, zulip: new ZulipClient({ email: config.botEmails[seat], key, ownerUserId: config.ownerUserId }), key, gate: seatGate });
  try {
    return await tools.checkedChannelSet();
  } catch (error) {
    if (error instanceof ToolError && error.code === "not_authorized") return null;
    throw error;
  }
}

// ---------------------------------------------------------------- /authorize and /admin

async function serveApp(request, env, _ctx) {
  const config = loadConfig(env);
  const route = classifyPath(new URL(request.url).pathname);
  if (route !== "authorize" && route !== "admin") return notFound();

  const who = await verifyAccessJwt(request.headers.get("cf-access-jwt-assertion"), config.access);
  if (!who.ok) {
    // Name the variable, never its value:  an unprovisioned AUD or team domain
    // is an operator fix, and saying which one turns a mystery 403 into a
    // one-line deploy step.
    const missing = config.access.missing;
    console.log(
      JSON.stringify({ event: "access_refused", route, reason: who.reason, missing }),
    );
    if (who.reason === "access_not_configured" && missing.length > 0) {
      return errorPage(
        "Server misconfigured",
        `Access is not provisioned on this Worker. Missing: ${missing.join(", ")}.`,
        500,
      );
    }
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
  // Arming first, so an unarmed request never makes the library fetch a CIMD
  // document.  It also picks the seat:  a loopback redirect may be listed under
  // several seats of one app, and exactly one of them must be armed.  The
  // choice runs over every listed seat before the client binding is checked,
  // so "both armed" is refused even for a client bound to one of them.
  const pick = resolveArmedSeat(pre.seats, await armedSeats(env, pre.seats));
  if (!pick.ok) {
    await logRefusal(env, { where: "authorize", reason: pick.reason, clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    if (pick.reason === "not_armed") return noConnectionPage();
    return errorPage("More Than One Seat Armed", `These seats share this app's callback:  ${pre.seats.join(", ")}.  More than one is armed, so the connection cannot tell which one you meant.  Disarm all but one in ST-MCP + GR-MCP Admin, then start the connection again.`, 409);
  }
  const seat = pick.seat;
  // An allowlisted CIMD id names its seat;  it must be the armed one.  Checked
  // before the library parses the request, so a mismatch fetches nothing.
  if (pre.cimdSeat && pre.cimdSeat !== seat) {
    await logRefusal(env, { where: "authorize", reason: "client_seat_mismatch", clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    return errorPage("Request Refused", `This app is allowlisted for ${pre.cimdSeat}, but ${seat} is the armed seat.`, 400);
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
  if (!postGateAuthRequest(authRequest, config, seat)) {
    await logRefusal(env, { where: "authorize", reason: "post_gate", clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    return errorPage("Request Refused", "This request does not match the allowlist.", 400);
  }
  const client = await oauth.lookupClient(authRequest.clientId);
  if (!client) return errorPage("Request Refused", "Unknown client.", 400);
  // A hand-registered client tagged for another seat (or untagged, on a shared
  // callback) cannot mint a grant for this one.
  if (!clientBoundToSeat(client, seat, authRequest.redirectUri, config)) {
    await logRefusal(env, { where: "authorize", reason: "client_seat_mismatch", clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    return errorPage("Request Refused", `This client is not bound to ${seat}.  Create a manual client for ${seat} in ST-MCP + GR-MCP Admin and use its client ID.`, 400);
  }

  const consent = await oauth.beginConsent(authRequest);
  const grants = await listAllGrants(oauth, seat);
  return consentPage(
    {
      eventsOnly: config.eventsOnlySeats.includes(seat),
      clientName: client.clientName ?? client.clientId,
      clientDomain: cimdHostname(client.clientId),
      clientId: client.clientId,
      redirectHost: new URL(authRequest.redirectUri).host,
      loopback: isLoopbackRedirect(authRequest.redirectUri),
      // A wake-only seat subscribes to events (zulip:read) and never posts:
      // offering zulip:write would promise a capability the router refuses.
      scopes: config.eventsOnlySeats.includes(seat) ? config.scopes.filter((s) => s !== "zulip:write") : [...config.scopes],
      // Consent copy must disclose the WhatsApp inbox grant exactly when this
      // seat would actually receive it (hosted-tools.js gates it the same way).
      whatsapp: seat === "JET-CHAT" && config.whatsappEnabled,
      seat,
      handle: consent.handle,
      replacing: grants.map((g) => ({ client: g.metadata?.client_name ?? g.clientId, createdAt: toMs(g.createdAt) })),
    },
    consent.headers,
  );
}

/** The seats in `seats` whose arming window is open now. */
async function armedSeats(env, seats) {
  const armed = new Set();
  for (const seat of seats) {
    if ((await gate(env, seat).getState()).armed) armed.add(seat);
  }
  return armed;
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
    // Disarm the seat the page showed;  if the form named none of the listed
    // seats, disarm every listed seat that is armed.
    const listed = hostedRedirectSeats(denied.request, config);
    const targets = listed.includes(postedSeat) ? [postedSeat] : [...(await armedSeats(env, listed))];
    for (const seat of targets) await gate(env, seat).disarm({ by: email, reason: "denied" });
    const headers = withReferrerPolicy(denied.headers);
    if (!headers.has("Location")) headers.set("Location", denied.redirectTo);
    return new Response(null, { status: 302, headers });
  }

  // postedSeat is verified against the armed seat below before anything is minted.
  const offered = config.eventsOnlySeats.includes(postedSeat) ? config.scopes.filter((s) => s !== "zulip:write") : config.scopes;
  const scopes = [...new Set(parsed.data.scope ?? [])].filter((s) => offered.includes(s));
  if (scopes.length === 0) return errorPage("Pick A Scope", "Approve needs at least one scope.  Go back and tick one.", 400);

  let approved;
  try {
    approved = await oauth.approveConsent(request, handle, { scope: scopes });
  } catch (error) {
    if (error instanceof AuthorizationError) return expiredPage();
    throw error;
  }
  // The stored request decides which seats are possible;  the arming state,
  // re-read now, decides which one.  It must be the seat the page showed:  if
  // the window lapsed or another seat sharing this callback was armed since,
  // nothing is minted.
  const seat = postedSeat;
  const listed = hostedRedirectSeats(approved.request, config);
  if (!seat || !postGateAuthRequest(approved.request, config, seat)) {
    await logRefusal(env, { where: "consent", reason: "seat_family_mismatch", clientId: approved.request.clientId, redirectUri: approved.request.redirectUri, by: email });
    return errorPage("Request Refused", "That seat cannot be bound to this app.", 400);
  }
  const pick = resolveArmedSeat(listed, await armedSeats(env, listed));
  if (!pick.ok || pick.seat !== seat) {
    const reason = pick.ok ? "seat_family_mismatch" : pick.reason;
    await logRefusal(env, { where: "consent", reason, clientId: approved.request.clientId, redirectUri: approved.request.redirectUri, by: email });
    if (reason === "not_armed") return errorPage("Arming Window Closed", "The 10-minute window closed or was already used.  Arm the seat again in ST-MCP + GR-MCP Admin.", 409);
    return errorPage("Request Refused", `The armed seat is no longer ${seat}, or more than one seat sharing this app's callback is armed.  Leave exactly one armed and start the connection again.`, 409);
  }
  // Resolve the display name first:  it may fetch a CIMD document, and a failure
  // there must not come after the arming window is spent and the epoch bumped.
  // The only awaited external call after `approve` is completeAuthorization.
  let clientName = logSafe(approved.request.clientId, 120);
  let client = null;
  try {
    client = await oauth.lookupClient(approved.request.clientId);
    if (client?.clientName) clientName = logSafe(client.clientName, 120);
  } catch {
    console.log(JSON.stringify({ event: "client_lookup_failed", where: "consent" }));
  }
  if (!clientBoundToSeat(client ?? { clientId: approved.request.clientId }, seat, approved.request.redirectUri, config)) {
    await logRefusal(env, { where: "consent", reason: "client_seat_mismatch", clientId: approved.request.clientId, redirectUri: approved.request.redirectUri, by: email });
    return errorPage("Request Refused", `This client is not bound to ${seat}.`, 400);
  }
  const seatGate = gate(env, seat);
  const won = await seatGate.approve({ by: email });
  if (!won.ok) {
    return errorPage("Arming Window Closed", "The 10-minute window closed or was already used.  Arm the seat again in ST-MCP + GR-MCP Admin.", 409);
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
  create_manual_client: "Manual client created.  Its client ID is in the table below.",
  sync_manual_client: "Manual client redirect URIs synced from SEATS.",
  revoke_gb_bearer: "Bearer revoked.",
};

async function serveAdmin(request, env, config, oauth, email) {
  const path = new URL(request.url).pathname;
  if (path === "/admin" && request.method === "GET") {
    const csrf = newCsrf();
    // One idempotency key per rendered page for the bearer mint forms:  an
    // accidental resubmit of the same form is refused without changing the
    // bearer or showing it again (src/pages.js gbBearerSection).
    const mintId = Date.now().toString(16).padStart(12, "0") + newCsrf().slice(0, 20);
    const done = new URL(request.url).searchParams.get("done");
    const generatedAt = Date.now();
    const seats = [];
    const audits = [];
    const calls = [];
    const statusErrors = [];
    // Every folder this page reads, listed once before any per-name read.
    await Promise.all(["/zulip", "/linear", "/tools"].map((path) => prefetchFolder(env, path)));
    const recallConfigured = recallClientFromResolved(await resolveRecallSecrets(env), env) !== null;
    // Collect seats independently so a diagnostic read failure is visible on
    // the page rather than hiding every other seat behind an admin 500.
    await Promise.all(config.hostedSeats.map(async (seat) => {
      const g = gate(env, seat);
      const pair = linearSecretNames(seat);
      // Resolved through Infisical (optional reads):  in required mode the
      // legacy bindings are absent, so env-name checks would misreport every
      // seat uninstalled.  A genuine miss reports not-installed, never a 500.
      const resolvedKey = await resolveSecret(env, { name: SEAT_SECRETS[seat].infisical.name, path: SEAT_SECRETS[seat].infisical.path, envName: SEAT_SECRETS[seat].key, optional: true });
      const resolvedPair = pair ? await resolveLinearPair(env, seat) : null;
      const facts = {
        seat, keyInstalled: Boolean(resolvedKey), grants: [],
        linearEnabled: LINEAR_SEATS.includes(seat),
        linearConfigured: Boolean(resolvedPair && resolvedPair.clientId && resolvedPair.clientSecret),
        linearWrites: String(env.LINEAR_TOOLS_WRITES ?? "") === "1",
        recallConfigured,
      };
      const reads = await Promise.allSettled([
        g.getState(), listAllGrants(oauth, seat), g.roleStatus(),
        g.tail(40), g.callTail(100), g.errorTail(50),
      ]);
      const value = (i, fallback) => reads[i].status === "fulfilled" ? reads[i].value : fallback;
      const grants = value(1, []);
      seats.push({
        ...facts, ...value(0, {}), role: value(2, null),
        diagnosticsUnavailable: reads.some((r) => r.status === "rejected"),
        unavailableFields: ["state", "grants", "role", "audits", "calls", "errors"].filter((_, i) => reads[i].status === "rejected"),
        grants: grants.map((x) => ({ client: x.metadata?.client_name ?? x.clientId, scope: x.scope ?? [], createdAt: toMs(x.createdAt) })),
      });
      for (const row of value(3, [])) {
        const { ts, event, ...detail } = row;
        audits.push({ ts, seat, event, detail });
      }
      calls.push(...value(4, []));
      statusErrors.push(...value(5, []));
    }));
    seats.sort((a, b) => config.hostedSeats.indexOf(a.seat) - config.hostedSeats.indexOf(b.seat));
    audits.sort((a, b) => b.ts - a.ts);
    calls.sort((a, b) => b.ts - a.ts);
    const clients = (await oauth.listClients()).items ?? [];
    const logGate = gate(env, GATE_LOG_NAME);
    const globalReads = await Promise.allSettled([
      logGate.refusals(30), logGate.tokenRefusals(30), logGate.diagnosticTail(50),
    ]);
    const globalValue = (i) => globalReads[i].status === "fulfilled" ? globalReads[i].value : [];
    const [refusals, tokenRefusals, runtimeErrors] = [0, 1, 2].map(globalValue);
    const diagnosticsUnavailable = globalReads.some((r) => r.status === "rejected");
    const delegatedRoles = (await resolveDelegatedCredentials(env)).roles;
    const gbBearers = await Promise.all([
      ...GB_IDENTITIES.map(async (identity) => {
        let status = null;
        let mismatches = null;
        let mismatchesUnavailable = false;
        const g = gate(env, identity);
        try { status = await g.gbBearerStatus(); } catch { /* Shown as unknown. */ }
        try { mismatches = (await g.gbBearerMismatches())[identity] ?? null; } catch { mismatchesUnavailable = true; }
        return { identity, kind: "persona", credentials: delegatedRoles.has(identity), status, mismatches, mismatchesUnavailable };
      }),
      // Every seat may hold a bearer too (owner direction 2026-10-23:  all apps in
      // this panel, one credential per identity, honored by ST-MCP and GR-MCP).
      // OAuth seats retain hosting checks; bearer-only bots need their own Zulip bundle.
      // resolved through Infisical (optional read), so a genuine miss reports
      // not-installed, never a 500.
      ...BEARER_SEATS.map(async (identity) => {
        let status = null;
        let mismatches = null;
        let mismatchesUnavailable = false;
        const g = gate(env, identity);
        try { status = await g.gbBearerStatus(); } catch { /* Shown as unknown. */ }
        try { mismatches = (await g.gbBearerMismatches())[identity] ?? null; } catch { mismatchesUnavailable = true; }
        let credentials = null;
        let credentialsUnavailable = false;
        try { credentials = await resolveBearerSeatCredentials(env, config, identity); }
        catch { credentialsUnavailable = true; }
        return { identity, kind: "seat", credentials: Boolean(credentials), credentialsUnavailable, status, mismatches, mismatchesUnavailable };
      }),
    ]);
    // Observability for the Infisical transition (kody 4365623014):  the mode
    // the Worker actually runs in and this isolate's counters, read after
    // this page's own reads so they are included;  rendered by adminPage.
    const infisical = { mode: infisicalMode(env), ...infisicalStats() };
    return adminPage(
      {
        gbBearers,
        mintId,
        email,
        seats,
        clients: clients.filter((c) => !isUrlShapedClientId(c.clientId)).map((c) => ({ ...c, seat: manualClientSeat(c) })),
        refusals,
        tokenRefusals,
        audits: audits.slice(0, 40),
        calls: calls.slice(0, 40),
        statusCalls: calls,
        statusErrors,
        infisical,
        runtimeErrors,
        statusAudits: audits,
        generatedAt,
        diagnosticsUnavailable,
        csrf,
        notice: DONE_NOTICES[done] ?? "",
        manualSeats: config.hostedSeats.map((seat) => {
          const uris = config.seats[seat]?.redirectUris ?? [];
          return { seat, label: SEAT_LABELS[seat] ?? seat, redirectsConfigured: uris.length > 0, shared: uris.some((u) => (config.redirectSeats.get(u) ?? []).length > 1) };
        }),
      },
      { "Set-Cookie": `${CSRF_COOKIE}=${csrf}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600` },
    );
  }
  if (path === "/admin/action" && request.method === "POST") {
    if (!sameOriginPost(request, config.issuer, { requireFetchSite: true })) return errorPage("Request Refused", "Admin changes must come from the admin page.", 403);
    const parsed = await readForm(request, AdminForm, []);
    if (!parsed.ok) return errorPage("Request Refused", "That form was not valid.  Reload ST-MCP + GR-MCP Admin and try again.", 400);
    if (!safeEqual(parsed.data.csrf, readCookie(request, CSRF_COOKIE))) return errorPage("Request Refused", "The admin form expired.  Reload ST-MCP + GR-MCP Admin and try again.", 403);
    const { action, seat = "" } = parsed.data;
    const needsSeat = ["arm", "disarm", "pause", "unpause", "revoke", "create_manual_client"].includes(action);
    const pauseActions = ["pause", "unpause"];
    // Pause is the seat-level kill switch for every mintable bearer identity,
    // including unhosted BEARER_SEATS such as CODEX.  Arm/disarm/revoke stay
    // hosted-only because they own OAuth grants.
    if (needsSeat) {
      const known = pauseActions.includes(action)
        ? (config.hostedSeats.includes(seat) || BEARER_SEATS.includes(seat))
        : config.hostedSeats.includes(seat);
      if (!known) return errorPage("Request Refused", "Unknown seat.", 400);
    }
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
      case "create_manual_client": {
        // A public PKCE client bound to one seat:  its name carries the seat
        // tag (policy.js manualClientSeat) and it gets that seat's redirects.
        // The GROK-WEB name is unchanged from the Grok-only button.
        const configured = config.seats[seat]?.redirectUris ?? [];
        const client = await oauth.createClient({
          clientName: `${SEAT_LABELS[seat] ?? seat} (manual form, ${seat})`,
          redirectUris: configured.length ? [...configured] : [MANUAL_PLACEHOLDER_REDIRECT],
          tokenEndpointAuthMethod: "none",
          grantTypes: ["authorization_code", "refresh_token"],
          responseTypes: ["code"],
        });
        await gate(env, seat).audit({ event: "client_created", by: email, client_id: client.clientId, placeholder: configured.length === 0 });
        break;
      }
      case "sync_manual_client": {
        // The seat comes from the client's own tag, never from the form, so a
        // sync cannot rebind a client to another seat.
        const clientId = parsed.data.client_id ?? "";
        if (isUrlShapedClientId(clientId)) return errorPage("Request Refused", "Unknown client.", 400);
        const client = await oauth.lookupClient(clientId);
        const bound = manualClientSeat(client);
        if (!client || !bound || !config.hostedSeats.includes(bound)) return errorPage("Request Refused", "This client has no hosted seat tag, so it cannot be synced.  Create a new manual client for the seat instead.", 400);
        const configured = config.seats[bound]?.redirectUris ?? [];
        if (configured.length === 0) return errorPage("Nothing To Sync", `Add ${bound}'s redirect URI to SEATS in wrangler.jsonc and redeploy first.`, 400);
        const updated = await oauth.updateClient(clientId, { redirectUris: [...configured] });
        if (!updated) return errorPage("Request Refused", "Unknown client.", 400);
        await gate(env, bound).audit({ event: "client_redirects_synced", by: email, client_id: clientId });
        break;
      }
      case "mint_gb_bearer": {
        const identity = parsed.data.identity ?? "";
        if (!GB_IDENTITIES.includes(identity) && !BEARER_SEATS.includes(identity)) return errorPage("Request Refused", "Unknown identity.", 400);
        const mintId = parsed.data.mint_id ?? "";
        const expiresAt = Date.now() + GB_BEARER_TTL_MS;
        let token, mcpAccept, kind;
        if (GB_IDENTITIES.includes(identity)) {
          await Promise.all(["/zulip", "/linear"].map((path) => prefetchFolder(env, path)));
          if (!(await resolveDelegatedCredentials(env)).roles.has(identity)) return errorPage("Request Refused", "Unknown Grok Bot, or its credentials are not loaded.", 400);
          ({ token } = await newGbBearer(identity));
          // A persona's bearer is its only credential:  accepted on /mcp by design.
          mcpAccept = true; kind = "persona";
        } else {
          await prefetchFolder(env, "/zulip");
          if (!await resolveBearerSeatCredentials(env, config, identity)) {
            return errorPage("Request Refused", "That identity's own credentials are not loaded.", 400);
          }
          ({ token } = await newSeatBearer(identity));
          // The owner's per-seat consent choice travels with the mint and is
          // recorded in the gate row and audit:  /mcp accepts this bearer only
          // when the box was ticked (src/pages.js states what that means).
          mcpAccept = parsed.data.mcp_accept === "1"; kind = "seat";
        }
        // Check-consume-mint is one transaction in the identity's own gate
        // (src/seat-state.js gbBearerMintChecked):  a stale or already-used form
        // refuses WITHOUT touching the bearer, so no refresh, resubmit or
        // days-later replay can rotate it.  The only response that ever carries
        // the token:  no redirect, no log line, nothing stored.
        const minted = await gate(env, identity).gbBearerMintChecked({ mintId, hash: await sha256Hex(token), by: email, expiresAt, mcpAccept });
        if (!minted.minted && minted.reason === "completed") return gbBearerMintCompletedPage({ identity, result: minted.result });
        if (!minted.minted) return gbBearerMintRefusedPage({ identity, reason: minted.reason });
        return gbBearerRevealPage({ identity, token, expiresAt, kind, mcpAccept });
      }
      case "revoke_gb_bearer": {
        const identity = parsed.data.identity ?? "";
        if (!GB_IDENTITIES.includes(identity) && !BEARER_SEATS.includes(identity)) return errorPage("Request Refused", "Unknown identity.", 400);
        await gate(env, identity).gbBearerRevoke({ by: email });
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
    if (!success) {
      await recordDiagnostic(env, { kind: "token", reason: "token_rate_limited", status: 429 });
      return oauthJsonError("temporarily_unavailable", "Too many token requests.", 429, { "Retry-After": "60" });
    }
  }
  if (!isFormContentType(request.headers.get("content-type"))) {
    await recordDiagnostic(env, { kind: "token", reason: "invalid_content_type", status: 400 });
    return oauthJsonError("invalid_request", "Content-Type must be application/x-www-form-urlencoded", 400);
  }
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_TOKEN_BODY) {
    await recordDiagnostic(env, { kind: "token", reason: "payload_too_large", status: 413 });
    return oauthJsonError("invalid_request", "Request body too large.", 413);
  }
  const text = await readLimited(request, MAX_TOKEN_BODY);
  if (text === null) {
    await recordDiagnostic(env, { kind: "token", reason: "payload_too_large", status: 413 });
    return oauthJsonError("invalid_request", "Request body too large.", 413);
  }
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

async function fetchProvider(request, env, ctx) {
  try { return await providerFor(env).fetch(request, env, ctx); }
  catch (error) {
    const route = classifyPath(new URL(request.url).pathname);
    await recordDiagnostic(env, { kind: route === "token" ? "token" : route === "mcp" ? "mcp" : "provider", reason: "internal_error", status: 500 });
    throw error;
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
    // Hard cutover (owner ruling, Oct 22, 2026):  the old agent-sync.jays.services
    // host is not answered, aliased or redirected.  It fails this single-host
    // check exactly like any unknown Host, so a request there gets a clean 404
    // rather than a working fallback.  The route is gone from wrangler.jsonc too.
    if (!hostAllowed(request, config.host)) return notFound();
    const route = classifyPath(new URL(request.url).pathname);
    if (!route) return notFound();
    if (route === "health") {
      // Static liveness probe for the fleet admin panel:  no seat, config or
      // grant data, and it never reaches the OAuth library.
      if (request.method !== "GET" && request.method !== "HEAD") return textResponse("Method Not Allowed", 405, { Allow: "GET, HEAD" });
      return new Response(request.method === "HEAD" ? null : '{"ok":true}', { status: 200, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
    }
    if (route === "mcp" && config.mcpDisabled) {
      await recordDiagnostic(env, { kind: "mcp", reason: "mcp_disabled", status: 503 });
      return textResponse("The ST-MCP server is turned off.", 503, { "Retry-After": "3600" });
    }
    if (route === "wake") return serveWake(request, env, ctx, config);
    if (route === "wa") return serveWa(request, env, config, gate(env, "JET"));
    if (route === "spike") return serveSpike(request, env, ctx, config);
    if (route === "linear_health") return serveLinearHealth(request, env, config);
    if (route === "token") {
      const gated = await gateToken(request, env, config);
      if (gated instanceof Response) return gated;
      if (gated instanceof Request) {
        const response = await fetchProvider(gated, env, ctx);
        if (response.status >= 400) await recordDiagnostic(env, { kind: "token", reason: "method_not_allowed", status: response.status });
        return response;
      }
      const response = await fetchProvider(gated.request, env, ctx);
      if (response.status >= 400) {
        let code = "oauth_error";
        let alreadyRecorded = false;
        try {
          const body = await response.clone().json();
          alreadyRecorded = ["This seat is paused.", "This grant is no longer valid."].includes(body?.error_description);
          if (["invalid_request", "invalid_client", "invalid_grant", "invalid_scope", "unauthorized_client", "unsupported_grant_type", "temporarily_unavailable", "server_error"].includes(body?.error)) code = body.error;
        } catch { /* No raw provider response is persisted. */ }
        if (!alreadyRecorded) await recordDiagnostic(env, { kind: "token", reason: code, status: response.status });
      }
      await pauseOnRefreshMismatch(env, config, gated.form, response);
      return response;
    }
    if (route === "gr_verify") return serveGrBearerVerify(request, env, gate);
    const gbBearer = route === "mcp" ? gbBearerFromRequest(request) : null;
    const sbtBearer = route === "mcp" && !gbBearer ? sbtBearerFromRequest(request) : null;
    const response = gbBearer
      ? await serveGbBearerMcp(request, env, gbBearer)
      : sbtBearer
        ? await serveSbtBearerMcp(request, env, sbtBearer)
        : await fetchProvider(request, env, ctx);
    // An unauthenticated MCP discovery 401 is normal OAuth negotiation.
    // Count only failed requests that actually supplied authorization.
    if (route === "mcp" && response.status >= 400 && request.headers.has("authorization") && response.headers.get("X-Agent-Sync-Diagnostic") !== "recorded") {
      let knownToolFailure = false;
      if (response.status === 400) {
        // Unknown tool calls already have a fixed, safe per-seat error row.
        try { knownToolFailure = /"message"\s*:\s*"Unknown tool"/.test(await response.clone().text()); }
        catch { /* Keep the generic protocol failure when unreadable. */ }
      }
      if (!knownToolFailure) await recordDiagnostic(env, { kind: "mcp", reason: `mcp_http_${response.status}`, status: response.status });
    }
    if (response.headers.has("X-Agent-Sync-Diagnostic")) {
      const headers = new Headers(response.headers);
      headers.delete("X-Agent-Sync-Diagnostic");
      return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
    }
    return response;
  },
};

// For tests:  the HTML helpers render without the Worker runtime.
export const _internal = { sentences, htmlResponse };
