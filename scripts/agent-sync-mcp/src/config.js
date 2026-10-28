// Configuration for the hosted ST-MCP Worker (Phase 0 OAuth, Phase 2 tools).
//
// Pure module:  no `cloudflare:` imports, so `node --test` can load it in CI
// without `npm ci`.  Every value comes from wrangler.jsonc `vars`.  A missing
// or malformed value makes `loadConfig` throw, and the Worker then answers 500
// for every request (fail closed) rather than guessing.

import { strictRedirectProblem, redirectUriProblem, isLoopbackRedirect, isCursorExactPair, CURSOR_CIMD_CLIENT } from "./policy.js";
import { DEFAULT_CALLBACK_HOSTS, isHostPattern } from "./events.js";

// D1:  the only hostname this Worker answers on.  It is a constant, not a var,
// so a config edit cannot widen the Host check.
//
// Hard cutover (owner ruling, Oct 22, 2026):  the previous hostname,
// agent-sync.jays.services, is NOT answered, aliased or redirected here.  Any
// request naming it is refused like any other unknown Host (policy.js
// `hostAllowed`, 404 at index.js).  Nothing may reintroduce a second accepted
// host without a new owner decision.
export const PUBLIC_HOST = "agents.simplewithus.com";
export const ISSUER = `https://${PUBLIC_HOST}`;

// The Operator bot's Zulip user id, pinned (owner decision, Oct 22 2026) and the same value as
// scripts/agent_sync/zulip.py OPERATOR_USER_ID.  It is an identity, not a credential, and it is only
// ever compared against the opener of a conversation.  OPERATOR_ZULIP_USER_ID overrides it where the
// deployment pins a different one;  when it is absent this pinned value is the whole truth, so there
// is nothing to fail closed on.  Exported so the Worker and the tests never carry a second copy.
export const PINNED_OPERATOR_USER_ID = 1217148;
export const RESOURCE = `${ISSUER}/mcp`;
export const RESOURCE_METADATA_URL = `${ISSUER}/.well-known/oauth-protected-resource/mcp`;

export const SCOPES = Object.freeze(["zulip:read", "zulip:write"]);

// Seats this Worker can ever serve.  HOSTED_SEATS (a var) narrows it further.
// ECHO and WISE are two identities of one third-party app (Instinct), each
// with its own Zulip bot (instinct-bat-bot and instinct-owl-bot).
// JET-CHAT is Jet's full-access login (owner decision, Oct 23 2026):  the
// original JET connection is wake-only, so the second ChatGPT connection is
// its own seat.  Both seats use the same openai-dot-bot;  the seat's expected
// tag is SEAT_TAG_EXPECTATIONS in src/contract.js.
// MA (Rob's admin-arming seat) landed on main via #894, and CODEX (Codex's
// admin-arming seat, #890) joins beside it;  every seat set stays.
export const KNOWN_SEATS = Object.freeze(["JET", "JET-CHAT", "GROK-WEB", "ECHO", "WISE", "CURSOR", "CLAUDE", "MA", "CODEX"]);

// Seats whose connection serves event notifications only.  tools/list is
// empty and tools/call refuses every name (src/delegated-identities.js);
// events stay (src/index.js).  Turning a seat back on is a deploy step.
// The constant lives in ./seats.js so src/pages.js can classify seats without
// importing this module (which would drag in the zod chain the visual tests
// cannot resolve); re-exported here for the existing consumers.
export { EVENTS_ONLY_SEATS } from "./seats.js";
import { EVENTS_ONLY_SEATS as DEFAULT_EVENTS_ONLY_SEATS } from "./seats.js";

// AFC-331 (docs/protocols/per-call-identity.md section 3):  a grant for one of these seats may act as
// either of them and as every enabled Grok Bot persona (GB_IDENTITIES);  every other grant acts only
// as its own seat.  One shared group, so a GROK-WEB grant and a CURSOR grant see the same set.
export const SHARED_IDENTITY_SEATS = Object.freeze(["CURSOR", "GROK-WEB"]);

// Phase 0 durations.
export const ARM_WINDOW_MS = 10 * 60 * 1000;
export const GRANT_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
export const ACCESS_TOKEN_TTL_S = 60 * 60;
export const REFRESH_TOKEN_TTL_S = 90 * 24 * 60 * 60;

// The refusal log lives in one extra SeatGate instance under this name.  It is
// never a seat:  KNOWN_SEATS cannot contain it.
export const GATE_LOG_NAME = "__refusal-log__";

// Every grant approved from Phase 2 on carries `phase: 2` in its props.  A
// grant without it (a Phase 0 stub grant) is refused on /mcp and on refresh, so
// no stub grant reaches the real tools (spec section 6, Phase 0 exit).
export const PROPS_PHASE = 2;

// Where each seat's bot key and email live.  The key is a Worker secret, the
// email a plain var (spec 3.6).  The seat comes only from the grant's props,
// and only this table turns it into a key, so a JET grant can never read
// GROK-WEB's key.
// key/email:  the legacy Worker binding names (off mode + bounded fallback).
// infisical:  the canonical Fleet-OPS /zulip record, verified live
// 2026-10-23 - the stale ZULIP_KEY_<SEAT>/ZULIP_EMAIL_<SEAT> names matched no
// Infisical secret in either project, so runtime reads use the canonical
// <SEAT>_ZULIP_* names and NO seat-key copies are ever provisioned (owner
// rule:  fix stale names with evidence, never invent absent secrets).
export const SEAT_SECRETS = Object.freeze({
  CURSOR: Object.freeze({ key: "ZULIP_KEY_CURSOR", email: "ZULIP_EMAIL_CURSOR", infisical: Object.freeze({ name: "CURSOR_ZULIP_KEY", path: "/zulip" }) }),
  CLAUDE: Object.freeze({ key: "ZULIP_CLAUDE_API_KEY", email: "ZULIP_CLAUDE_EMAIL", infisical: Object.freeze({ name: "CLAUDE_ZULIP_KEY", path: "/zulip" }) }),
  // CODEX's key already lives in Fleet-OPS /zulip as CODEX_ZULIP_KEY
  // (install_seat_key.py, names verified live 2026-10-23).
  CODEX: Object.freeze({ key: "ZULIP_KEY_CODEX", email: "ZULIP_EMAIL_CODEX", infisical: Object.freeze({ name: "CODEX_ZULIP_KEY", path: "/zulip" }) }),
  // MA's key already lives in Fleet-OPS /zulip as MA_ZULIP_KEY
  // (install_seat_key.py, names verified live 2026-10-23).
  MA: Object.freeze({ key: "ZULIP_KEY_MA", email: "ZULIP_EMAIL_MA", infisical: Object.freeze({ name: "MA_ZULIP_KEY", path: "/zulip" }) }),
  JET: Object.freeze({ key: "ZULIP_KEY_JET", email: "ZULIP_EMAIL_JET", infisical: Object.freeze({ name: "JET_ZULIP_KEY", path: "/zulip" }) }),
  // Own env names (this table never shares one), same openai-dot-bot key
  // value:  install_seat_key.py provisions it from the same Infisical entry,
  // so the runtime read is Jet's canonical record, never a copy.
  "JET-CHAT": Object.freeze({ key: "ZULIP_KEY_JET_CHAT", email: "ZULIP_EMAIL_JET_CHAT", infisical: Object.freeze({ name: "JET_ZULIP_KEY", path: "/zulip" }) }),
  "GROK-WEB": Object.freeze({ key: "ZULIP_KEY_GROK_WEB", email: "ZULIP_EMAIL_GROK_WEB", infisical: Object.freeze({ name: "GROK_WEB_ZULIP_KEY", path: "/zulip" }) }),
  ECHO: Object.freeze({ key: "ZULIP_KEY_ECHO", email: "ZULIP_EMAIL_ECHO", infisical: Object.freeze({ name: "ECHO_ZULIP_KEY", path: "/zulip" }) }),
  WISE: Object.freeze({ key: "ZULIP_KEY_WISE", email: "ZULIP_EMAIL_WISE", infisical: Object.freeze({ name: "WISE_ZULIP_KEY", path: "/zulip" }) }),
});

// The shared HMAC key the server listener signs each seat's wake with
// (POST /internal/wake/<SEAT>, src/wake.js).  Only this table turns a seat
// from the path into a secret name, so a path can never pick another secret.
// A seat whose secret is unset answers 404 there.
export const WAKE_SECRETS = Object.freeze({
  JET: "WAKE_HMAC_KEY_JET",
  "GROK-WEB": "WAKE_HMAC_KEY_GROK_WEB",
  ECHO: "WAKE_HMAC_KEY_ECHO",
  WISE: "WAKE_HMAC_KEY_INSTINCT",
});

// The Operator notes API key (AFC-486, docs/protocols/operator-notes.md):  the Worker signs every notes_* request
// to <LISTENER_API_URL>/notes/v1/ as the principal WORKER with this secret, the Infisical key
// OPERATOR_NOTES_WORKER_KEY (AI Fleet Coordinator, prod, /operator).  One key for every seat:  the seat comes only
// from the grant, and the Operator checks it against [notes_queue] worker_seats.  A Worker secret, never a var.
// Unset (or shorter than 32 characters) means every notes tool answers not_configured (src/notes.js).
export const OPERATOR_NOTES_SECRET = "OPERATOR_NOTES_KEY";

// What /admin and a manual client's name call each seat's app.
export const SEAT_LABELS = Object.freeze({ CLAUDE: "Claude", CURSOR: "Cursor Cloud", JET: "ChatGPT", "JET-CHAT": "ChatGPT", "GROK-WEB": "Grok", ECHO: "Echo", WISE: "Wise", MA: "Rob", CODEX: "Codex" });

const BOT_EMAIL_RE = /^[a-z0-9-]+-bot@simplewithus\.zulipchat\.com$/;

function parseJsonVar(env, name) {
  const raw = env[name];
  if (raw === undefined || raw === null || raw === "") throw new Error(`config: ${name} is not set`);
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(String(raw));
  } catch {
    throw new Error(`config: ${name} is not valid JSON`);
  }
}

function parseList(env, name) {
  const raw = env[name];
  if (Array.isArray(raw)) return raw.map(String);
  if (raw === undefined || raw === null || raw === "") return [];
  const text = String(raw).trim();
  if (text.startsWith("[")) return parseJsonVar(env, name).map(String);
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

/**
 * Build the validated config from `env`.  Throws on anything malformed.
 * The result is frozen, so request code cannot mutate policy.
 */
export function loadConfig(env) {
  const cursorToolBudgets = env.AGENT_SYNC_CURSOR_TOOL_BUDGETS
    ? parseJsonVar(env, "AGENT_SYNC_CURSOR_TOOL_BUDGETS") : {};
  if (!cursorToolBudgets || typeof cursorToolBudgets !== "object" || Array.isArray(cursorToolBudgets)) {
    throw new Error("config: AGENT_SYNC_CURSOR_TOOL_BUDGETS must be an object");
  }
  for (const [kind, policy] of Object.entries(cursorToolBudgets)) {
    if (!["write", "read", "react", "recall", "linear_write", "linear_read", "fleetlink"].includes(kind)
      || !policy || typeof policy !== "object" || Array.isArray(policy)
      || Object.keys(policy).some((key) => !["hour", "day", "spacingMs"].includes(key))
      || !Number.isInteger(policy.hour) || policy.hour < 1 || policy.hour > 100000
      || (policy.day !== undefined && (!Number.isInteger(policy.day) || policy.day < policy.hour || policy.day > 1000000))
      || (policy.spacingMs !== undefined && (!Number.isInteger(policy.spacingMs) || policy.spacingMs < 50 || policy.spacingMs > 6000))
      || (!["linear_write", "linear_read"].includes(kind) && policy.spacingMs !== undefined)) {
      throw new Error("config: invalid CURSOR tool budget policy");
    }
    Object.freeze(policy);
  }
  if (env.PUBLIC_HOST !== undefined && env.PUBLIC_HOST !== PUBLIC_HOST) {
    throw new Error("config: PUBLIC_HOST var does not match the compiled hostname");
  }

  const hostedSeats = parseList(env, "HOSTED_SEATS");
  for (const seat of hostedSeats) {
    if (!KNOWN_SEATS.includes(seat)) throw new Error(`config: unknown hosted seat ${seat}`);
  }

  // The events-only set is a var so the wake-only flip is a one-line reviewed
  // config change at cutover, not a source edit:  absent means the seats.js
  // staging default (no seat), "JET" restricts the old login once the new one
  // is verified live.  Only known seats are ever accepted.
  const eventsOnlyRaw = env.EVENTS_ONLY_SEATS;
  const eventsOnlySeats = (eventsOnlyRaw === undefined || eventsOnlyRaw === null || String(eventsOnlyRaw).trim() === "")
    ? [...DEFAULT_EVENTS_ONLY_SEATS]
    : parseList(env, "EVENTS_ONLY_SEATS");
  for (const seat of eventsOnlySeats) {
    if (!KNOWN_SEATS.includes(seat)) throw new Error(`config: unknown events-only seat ${seat}`);
  }

  const seatsRaw = parseJsonVar(env, "SEATS");
  if (typeof seatsRaw !== "object" || Array.isArray(seatsRaw)) throw new Error("config: SEATS must be an object");
  const seats = {};
  // Redirect URI -> every seat that lists it.  An https redirect belongs to
  // exactly one seat.  A loopback redirect (http://127.0.0.1:<port>/<path>)
  // may be listed under several seats of one native app;  /authorize then
  // picks the one seat that is armed (policy.js resolveArmedSeat).
  const redirectSeats = new Map();
  // Redirect URI -> its seat, for redirects that exactly one seat lists.
  const redirectOwner = new Map();
  const cimdOwner = new Map();
  for (const [seat, entry] of Object.entries(seatsRaw)) {
    if (!KNOWN_SEATS.includes(seat)) throw new Error(`config: SEATS names unknown seat ${seat}`);
    const redirectUris = (entry?.redirect_uris ?? []).map(String);
    const cimdClientIds = (entry?.cimd_client_ids ?? []).map(String);
    for (const uri of redirectUris) {
      const problem = redirectUriProblem(uri);
      if (problem && !(cimdClientIds.includes(CURSOR_CIMD_CLIENT) && isCursorExactPair(CURSOR_CIMD_CLIENT, uri, seat))) {
        throw new Error(`config: ${seat} redirect is not strict (${problem})`);
      }
      const listed = redirectSeats.get(uri) ?? [];
      if (listed.includes(seat)) throw new Error(`config: ${seat} lists a redirect URI twice`);
      if (listed.length > 0 && !isLoopbackRedirect(uri)) throw new Error("config: a redirect URI is listed under two seats (only a 127.0.0.1 loopback redirect may be shared)");
      redirectSeats.set(uri, [...listed, seat]);
    }
    for (const id of cimdClientIds) {
      if (id === CURSOR_CIMD_CLIENT && seat !== "CURSOR") throw new Error("config: Cursor CIMD belongs only to CURSOR");
      const problem = strictRedirectProblem(id);
      if (problem) throw new Error(`config: ${seat} CIMD client id is not a strict https URL (${problem})`);
      if (cimdOwner.has(id)) throw new Error("config: a CIMD client id is listed under two seats");
      cimdOwner.set(id, seat);
    }
    seats[seat] = Object.freeze({ redirectUris: Object.freeze(redirectUris), cimdClientIds: Object.freeze(cimdClientIds) });
  }

  for (const [uri, listed] of redirectSeats) {
    Object.freeze(listed);
    if (listed.length === 1) redirectOwner.set(uri, listed[0]);
  }

  // Channels (owner ruling Sat, Oct 10, 2026:  any public channel the seat's
  // bot is subscribed to;  src/channels.js resolves that set from Zulip).
  // CHANNELS_DENY is the one config lever:  stream ids no seat may use even
  // when they are public.  Optional, empty by default, enforced by id.
  const channelsDeny = [];
  if (env.CHANNELS_DENY !== undefined && env.CHANNELS_DENY !== null && env.CHANNELS_DENY !== "") {
    const denyRaw = parseJsonVar(env, "CHANNELS_DENY");
    if (!Array.isArray(denyRaw)) throw new Error("config: CHANNELS_DENY must be a JSON array of stream ids");
    for (const id of denyRaw) {
      if (!Number.isSafeInteger(id) || id <= 0) throw new Error("config: CHANNELS_DENY entries must be stream ids");
      if (channelsDeny.includes(id)) throw new Error("config: a stream id is listed twice in CHANNELS_DENY");
      channelsDeny.push(id);
    }
  }

  // The owner's pinned Zulip user id and human clients (listener rule):  the
  // `owner` flag on a message is a routing hint, never authority.
  const ownerUserId = Number(env.OWNER_USER_ID);
  if (!Number.isSafeInteger(ownerUserId) || ownerUserId <= 0) throw new Error("config: OWNER_USER_ID must be a Zulip user id");
  const ownerClients = parseList(env, "OWNER_CLIENTS");
  if (ownerClients.length === 0) throw new Error("config: OWNER_CLIENTS is empty");

  // Bot emails for every hosted seat (plain vars;  the keys are secrets).
  const botEmails = {};
  for (const seat of hostedSeats) {
    const email = String(env[SEAT_SECRETS[seat].email] ?? "").trim().toLowerCase();
    if (!BOT_EMAIL_RE.test(email)) throw new Error(`config: ${SEAT_SECRETS[seat].email} must be a bot email in the realm`);
    botEmails[seat] = email;
  }

  // MCP Events callback hosts (src/events.js).  Unset means the compiled
  // default;  a malformed entry fails closed like every other var.
  const eventCallbackHosts = env.EVENT_CALLBACK_HOSTS === undefined || env.EVENT_CALLBACK_HOSTS === "" ? [...DEFAULT_CALLBACK_HOSTS] : parseList(env, "EVENT_CALLBACK_HOSTS").map((h) => h.toLowerCase());
  for (const pattern of eventCallbackHosts) {
    if (!isHostPattern(pattern)) throw new Error("config: EVENT_CALLBACK_HOSTS entries must be host names or *.host names");
  }
  if (eventCallbackHosts.length === 0) throw new Error("config: EVENT_CALLBACK_HOSTS is empty");

  const teamDomain = String(env.ACCESS_TEAM_DOMAIN ?? "").trim();
  const accessAud = String(env.ACCESS_AUD ?? "").trim();
  const ownerEmails = parseList(env, "OWNER_EMAILS").map((e) => e.toLowerCase());

  // Which Access variables are absent or malformed.  Naming them is what lets a
  // fail-closed /authorize or /admin answer say what to provision instead of a
  // bare "not configured".  A placeholder AUD counts as missing.
  const accessMissing = [];
  if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/.test(teamDomain)) accessMissing.push("ACCESS_TEAM_DOMAIN");
  if (!/^[0-9a-f]{64}$/.test(accessAud)) accessMissing.push("ACCESS_AUD");
  if (ownerEmails.length === 0) accessMissing.push("OWNER_EMAILS");

  // Deployment-owned binding for receipt verification.  No inferred mapping or
  // caller-supplied principal; missing entry leaves that identity unconfigured.
  const grTypedPrincipals = env.GR_TYPED_PRINCIPALS ? parseJsonVar(env, "GR_TYPED_PRINCIPALS") : {};
  if (!grTypedPrincipals || typeof grTypedPrincipals !== "object" || Array.isArray(grTypedPrincipals) ||
      Object.entries(grTypedPrincipals).some(([seat, principal]) => !/^[A-Za-z0-9_-]{1,100}$/.test(seat) ||
        typeof principal !== "string" || !/^swu-[a-z0-9-]+\[bot\]$/.test(principal)))
    throw new Error("config: invalid GR_TYPED_PRINCIPALS");

  return Object.freeze({
    host: PUBLIC_HOST,
    // The DM rule (owner decision, Oct 22 2026, hard cutover):  a seat may not open a new direct
    // message.  Mirrors scripts/agent_sync/dm_policy.py rule_enabled();  default on.
    dmRule: !["off", "0", "false", "no", "none", "disabled"].includes(
      String(env.AGENT_SYNC_DM_RULE ?? "").trim().toLowerCase(),
    ),
    dmClarifyTimeoutMinutes: (() => {
      const raw = String(env.AGENT_SYNC_DM_CLARIFY_TIMEOUT_MINUTES ?? "").trim();
      const minutes = raw === "" ? 30 : Number(raw);
      return Number.isFinite(minutes) && minutes >= 0 ? minutes : 30;
    })(),
    operatorUserId: (() => {
      const raw = String(env.OPERATOR_ZULIP_USER_ID ?? "").trim();
      const id = raw === "" ? PINNED_OPERATOR_USER_ID : Number(raw);
      if (!Number.isSafeInteger(id) || id <= 0) {
        throw new Error(`config: OPERATOR_ZULIP_USER_ID must be a positive Zulip user id, not ${raw}`);
      }
      return id;
    })(),
    cursorToolBudgets: Object.freeze(cursorToolBudgets),
    issuer: ISSUER,
    resource: RESOURCE,
    resourceMetadataUrl: RESOURCE_METADATA_URL,
    scopes: SCOPES,
    hostedSeats: Object.freeze(hostedSeats),
    eventsOnlySeats: Object.freeze(eventsOnlySeats),
    seats: Object.freeze(seats),
    redirectSeats,
    redirectOwner,
    cimdOwner,
    access: Object.freeze({
      teamDomain,
      aud: accessAud,
      ownerEmails: Object.freeze(ownerEmails),
      // Fail closed:  until the Access app exists and its AUD is provisioned,
      // nothing behind Access can be reached.
      configured: accessMissing.length === 0,
      missing: Object.freeze(accessMissing),
    }),
    mcpDisabled: String(env.MCP_DISABLED ?? "") === "1",
    channelsDeny: Object.freeze(channelsDeny),
    ownerUserId,
    ownerClients: Object.freeze(ownerClients),
    botEmails: Object.freeze(botEmails),
    eventCallbackHosts: Object.freeze(eventCallbackHosts),
    // AFC-477 spike (temporary):  offer `fleet.spike.ping` and answer
    // POST /internal/spike/<SEAT> only while this is exactly "on".  It is a
    // Worker secret binding, not a var, so a later deploy from main does not
    // turn it off (DEPLOY.md, "AFC-477 second-event spike").
    whatsappEnabled: String(env.WHATSAPP_BRIDGE_ENABLED ?? "").trim() === "on",
    grTypedPrincipals: Object.freeze({...grTypedPrincipals}),
    grTypedEnabled: String(env.GR_TYPED_TOOLS ?? "").trim() === "on",
    // Default off: the gr_wf_ workflow verb tools are neither listed nor callable until the owner sets GR_WORKFLOW_TOOLS=on.
    grWorkflowEnabled: String(env.GR_WORKFLOW_TOOLS ?? "").trim() === "on",
    whatsappWakeEnabled: String(env.WHATSAPP_BRIDGE_WAKE_ENABLED ?? "").trim() === "on",
    spikeExtraEvents: String(env.SPIKE_EXTRA_EVENTS ?? "").trim() === "on",
  });
}

/** Every allowlisted CIMD client id, across seats. */
export function allCimdClientIds(config) {
  return [...config.cimdOwner.keys()];
}
