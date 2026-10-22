// Hosted agent-sync MCP Worker (docs/protocols/agent-sync-mcp.md section 3):
// OAuth 2.1 with CIMD for ChatGPT and Grok, hand-registered public PKCE
// clients bound to one seat each (manual forms, the Instinct app's loopback
// callback), arming, a consent page behind Cloudflare
// Access, a per-seat epoch and pause switch (Phase 0), and the seven agent-sync
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
  SHARED_IDENTITY_SEATS,
  SEAT_LABELS,
  WAKE_SECRETS,
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
import { consentPage, noConnectionPage, errorPage, adminPage, htmlResponse, sentences, withReferrerPolicy } from "./pages.js";
import { serveMcpRequest } from "./mcp.js";
import { HostedTools } from "./hosted-tools.js";
import { IdentityRouter, loadDelegatedCredentials } from "./delegated-identities.js";
import { gate } from "./gate.js";
import { ToolError } from "./contract.js";
import { recallClientFromEnv } from "./recall.js";
import { fleetlinkClientFromEnv } from "./fleetlink.js";
import { LinearClient, linearClientFromEnv, LINEAR_SEATS, linearSecretNames } from "./linear.js";
import { ZulipClient } from "./zulip.js";
import { listenerClientFromEnv } from "./listener.js";
import { ConsentForm, AdminForm, CONSENT_ARRAY_KEYS, parseForm } from "./forms.js";
import { SeatEvents, deliverWake, wakeReply, WAKE_BUDGET_MS, EVENT_NAME } from "./events.js";
import { verifyWake, wakePathSeat, readBodyBytes, WAKE_SIGNATURE_HEADER, WAKE_MAX_BODY } from "./wake.js";
import { verifyHealth, healthKey, healthReport, HEALTH_SIGNATURE_HEADER, HEALTH_MAX_BODY } from "./health.js";
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
    // Null when any recall secret is missing:  the recall tools then answer
    // not_configured and every other tool works as before (spec 1.1).
    recall: recallClientFromEnv(env),
    // Null when this seat has no Linear app credentials, or when the seat is
    // not on LINEAR_SEATS:  the Linear tools then answer not_configured and
    // every other tool works as before (AFC-289).
    linear: linearClientFromEnv(env, seat, { gate: seatGate }),
    linearWrites: String(env.LINEAR_TOOLS_WRITES ?? "") === "1",
    // Null without FLEETLINK_AGENT_SECRET:  the FleetLink tools then answer
    // not_configured and every other tool works as before (AFC-328).
    fleetlink: fleetlinkClientFromEnv(env),
    // Null unless LISTENER_API_URL and this seat's WAKE_HMAC_KEY_<SEAT> are set:  the listener_*
    // tools then answer not_configured and every other tool works as before (AFC-330).
    listener: listenerClientFromEnv(env, seat),
  });
  // AFC-331:  every grant is routed by identity.  A CURSOR or GROK-WEB grant may act as either seat and as
  // every enabled Grok Bot persona;  any other grant only as its own seat (src/delegated-identities.js).
  const sharedGroup = SHARED_IDENTITY_SEATS.includes(seat);
  const delegatedCredentials = sharedGroup ? loadDelegatedCredentials(env) : null;
  const seatKeyOf = (identity) => (typeof env[SEAT_SECRETS[identity]?.key] === "string" ? env[SEAT_SECRETS[identity].key].trim() : "");
  const routedTools = new IdentityRouter({
    callerTools: tools,
    credentials: delegatedCredentials,
    identityStatus: (identity) => ({
      zulip: seatKeyOf(identity) && config.botEmails[identity] ? "configured" : "missing",
      linear: [linearSecretNames(identity)?.clientId, linearSecretNames(identity)?.clientSecret]
        .every((name) => name && typeof env[name] === "string" && env[name].trim()) ? "configured" : "missing",
    }),
    createIdentity: !sharedGroup ? null : async (identity) => {
      // Each identity has its own SeatGate:  its spacing, idempotency rows, role cache and budget.  The
      // grant's gate is charged too (HostedTools.reserve), so another identity never multiplies it.
      const identityGate = gate(env, identity);
      const identityState = await identityGate.getState();
      if (identityState.paused) throw new ToolError("paused", `The ${identity} identity is paused.`);
      const common = {
        callerSeat: seat,
        callerGate: seatGate,
        scopes,
        clientId: typeof auth.clientId === "string" ? auth.clientId : "",
        grantRef: tools.grantRef,
        paused: tools.paused,
        gate: identityGate,
        cf: tools.cf,
        recall: tools.recall,
        fleetlink: tools.fleetlink,
        linearWrites: tools.linearWrites,
      };
      const roleCredentials = delegatedCredentials.roles.get(identity);
      if (roleCredentials) {
        // A Grok Bot persona:  its row of the encrypted GB_IDENTITY_CREDENTIALS bundle.
        const roleKey = roleCredentials.zulipKey;
        const roleEmail = roleCredentials.email;
        return new HostedTools({
          ...common,
          seat: identity,
          delegatedLinear: true,
          config: { ...config, botEmails: { ...config.botEmails, [identity]: roleEmail } },
          zulip: new ZulipClient({ email: roleEmail, key: roleKey }),
          key: roleKey,
          linear: new LinearClient({
            seat: identity,
            clientId: roleCredentials.linearClientId,
            clientSecret: roleCredentials.linearClientSecret,
            gate: identityGate,
            fetch: globalThis.fetch.bind(globalThis),
          }),
        });
      }
      if (!SHARED_IDENTITY_SEATS.includes(identity)) throw new ToolError("not_configured", `Credentials are missing for ${identity}.`);
      // A peer seat of the shared group:  its own SEAT_SECRETS key and its own Linear app, never the grant's.
      const peerKey = seatKeyOf(identity);
      const peerEmail = config.botEmails[identity];
      if (!peerKey || !peerEmail) throw new ToolError("not_configured", `Credentials are missing for ${identity}.`);
      return new HostedTools({
        ...common,
        seat: identity,
        config,
        zulip: new ZulipClient({ email: peerEmail, key: peerKey }),
        key: peerKey,
        linear: linearClientFromEnv(env, identity, { gate: identityGate }),
        listener: listenerClientFromEnv(env, identity),
      });
    },
  });
  const authInfo = {
    token: auth.token,
    clientId: auth.clientId,
    scopes,
    ...(Number.isFinite(auth.expiresAt) ? { expiresAt: auth.expiresAt } : {}),
    extra: { seat },
  };
  // Events only for a seat the listener can wake (its WAKE_HMAC_KEY_<SEAT> is
  // set):  any other seat would list an event that never fires.
  const wakeSecret = env[WAKE_SECRETS[seat]];
  const events = typeof wakeSecret === "string" && wakeSecret.trim() ? new SeatEvents({
    seat,
    scopes,
    epoch: props.epoch,
    approvedAt: props.approved_at,
    paused: result.code === "paused",
    gate: seatGate,
    config,
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
  const rawKey = env[WAKE_SECRETS[seat]];
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
 * SPIKE_HMAC_KEY equal to the seat's wake key, which must never sign a spike.
 * Then the signature, freshness and shape, then SeatGate.spikeClaim (dedupe,
 * rate limit, pause), then delivery inside this request.  The answer holds
 * counts only (spikeReply).
 */
async function serveSpike(request, env, ctx, config) {
  const started = Date.now();
  if (!config.spikeExtraEvents) return notFound();
  const seat = spikePathSeat(new URL(request.url).pathname);
  if (!seat || !config.hostedSeats.includes(seat) || !Object.hasOwn(WAKE_SECRETS, seat)) return notFound();
  const wakeKey = typeof env[WAKE_SECRETS[seat]] === "string" ? env[WAKE_SECRETS[seat]].trim() : "";
  const key = spikeKey(env.SPIKE_HMAC_KEY);
  if (!wakeKey || !key) return notFound();
  if (key === wakeKey) {
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
  const wakeKeys = Object.values(WAKE_SECRETS).map((name) => (typeof env[name] === "string" ? env[name].trim() : ""));
  if (wakeKeys.includes(key)) {
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
  for (const seat of LINEAR_SEATS) {
    const names = linearSecretNames(seat);
    if (names && config.hostedSeats.includes(seat) && [names.clientId, names.clientSecret].every((n) => typeof env[n] === "string" && env[n].trim() !== "")) identities.push(seat);
  }
  const delegated = loadDelegatedCredentials(env);
  for (const identity of delegated.roles.keys()) identities.push(identity);
  const rows = await Promise.all(identities.map(async (seat) => {
    try {
      return { seat, row: await gate(env, seat).linearHealth() };
    } catch {
      return { seat, row: { token: null, note: { last_fail_at: 1, last_ok_at: 0, last_fail_kind: "unexpected" } } };
    }
  }));
  return jsonResponse({ ...healthReport(rows, Math.floor(Date.now() / 1000)), bundle: delegated.valid ? "ok" : "invalid" }, 200);
}

/**
 * The seat's allowed channels for a wake (src/channels.js), read with the
 * seat's own key after the same member-only role check the tools make.  Null
 * (every channel wake refused) when no key is installed or the bot is
 * refused;  a Zulip failure throws, which deliverWake answers with a retry.
 */
async function wakeChannels(env, config, seat, seatGate) {
  const key = typeof env[SEAT_SECRETS[seat].key] === "string" ? env[SEAT_SECRETS[seat].key].trim() : "";
  if (!key) return null;
  const tools = new HostedTools({ seat, scopes: [], config, zulip: new ZulipClient({ email: config.botEmails[seat], key }), key, gate: seatGate });
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
  // Arming first, so an unarmed request never makes the library fetch a CIMD
  // document.  It also picks the seat:  a loopback redirect may be listed under
  // several seats of one app, and exactly one of them must be armed.  The
  // choice runs over every listed seat before the client binding is checked,
  // so "both armed" is refused even for a client bound to one of them.
  const pick = resolveArmedSeat(pre.seats, await armedSeats(env, pre.seats));
  if (!pick.ok) {
    await logRefusal(env, { where: "authorize", reason: pick.reason, clientId: pre.clientId, redirectUri: pre.redirectUri, by: email });
    if (pick.reason === "not_armed") return noConnectionPage();
    return errorPage("More Than One Seat Armed", `These seats share this app's callback:  ${pre.seats.join(", ")}.  More than one is armed, so the connection cannot tell which one you meant.  Disarm all but one in Agent-Sync Admin, then start the connection again.`, 409);
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
    return errorPage("Request Refused", `This client is not bound to ${seat}.  Create a manual client for ${seat} in Agent-Sync Admin and use its client ID.`, 400);
  }

  const consent = await oauth.beginConsent(authRequest);
  const grants = await listAllGrants(oauth, seat);
  return consentPage(
    {
      clientName: client.clientName ?? client.clientId,
      clientDomain: cimdHostname(client.clientId),
      clientId: client.clientId,
      redirectHost: new URL(authRequest.redirectUri).host,
      loopback: isLoopbackRedirect(authRequest.redirectUri),
      scopes: [...config.scopes],
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

  const scopes = [...new Set(parsed.data.scope ?? [])].filter((s) => config.scopes.includes(s));
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
    if (reason === "not_armed") return errorPage("Arming Window Closed", "The 10-minute window closed or was already used.  Arm the seat again in Agent-Sync Admin.", 409);
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
  create_manual_client: "Manual client created.  Its client ID is in the table below.",
  sync_manual_client: "Manual client redirect URIs synced from SEATS.",
};

async function serveAdmin(request, env, config, oauth, email) {
  const path = new URL(request.url).pathname;
  if (path === "/admin" && request.method === "GET") {
    const csrf = newCsrf();
    const done = new URL(request.url).searchParams.get("done");
    const generatedAt = Date.now();
    const seats = [];
    const audits = [];
    const calls = [];
    const statusErrors = [];
    const configured = (name) => typeof env[name] === "string" && env[name].trim() !== "";
    const recallConfigured = recallClientFromEnv(env) !== null;
    // Collect seats independently so a diagnostic read failure is visible on
    // the page rather than hiding every other seat behind an admin 500.
    await Promise.all(config.hostedSeats.map(async (seat) => {
      const g = gate(env, seat);
      const pair = linearSecretNames(seat);
      const facts = {
        seat, keyInstalled: configured(SEAT_SECRETS[seat].key), grants: [],
        linearEnabled: LINEAR_SEATS.includes(seat),
        linearConfigured: Boolean(pair && configured(pair.clientId) && configured(pair.clientSecret)),
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
    return adminPage(
      {
        email,
        seats,
        clients: clients.filter((c) => !isUrlShapedClientId(c.clientId)).map((c) => ({ ...c, seat: manualClientSeat(c) })),
        refusals,
        tokenRefusals,
        audits: audits.slice(0, 40),
        calls: calls.slice(0, 40),
        statusCalls: calls,
        statusErrors,
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
    if (!parsed.ok) return errorPage("Request Refused", "That form was not valid.  Reload Agent-Sync Admin and try again.", 400);
    if (!safeEqual(parsed.data.csrf, readCookie(request, CSRF_COOKIE))) return errorPage("Request Refused", "The admin form expired.  Reload Agent-Sync Admin and try again.", 403);
    const { action, seat = "" } = parsed.data;
    const needsSeat = ["arm", "disarm", "pause", "unpause", "revoke", "create_manual_client"].includes(action);
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
    const response = await fetchProvider(request, env, ctx);
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
