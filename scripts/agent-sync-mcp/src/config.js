// Configuration for the hosted agent-sync MCP Worker (Phase 0 OAuth, Phase 2 tools).
//
// Pure module:  no `cloudflare:` imports, so `node --test` can load it in CI
// without `npm ci`.  Every value comes from wrangler.jsonc `vars`.  A missing
// or malformed value makes `loadConfig` throw, and the Worker then answers 500
// for every request (fail closed) rather than guessing.

import { strictRedirectProblem } from "./policy.js";

// D1:  the only hostname this Worker answers on.  It is a constant, not a var,
// so a config edit cannot widen the Host check.
export const PUBLIC_HOST = "agent-sync.jays.services";
export const ISSUER = `https://${PUBLIC_HOST}`;
export const RESOURCE = `${ISSUER}/mcp`;
export const RESOURCE_METADATA_URL = `${ISSUER}/.well-known/oauth-protected-resource/mcp`;

export const SCOPES = Object.freeze(["zulip:read", "zulip:write"]);

// Seats this Worker can ever serve.  HOSTED_SEATS (a var) narrows it further.
export const KNOWN_SEATS = Object.freeze(["JET", "GROK-WEB"]);

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
export const SEAT_SECRETS = Object.freeze({
  JET: Object.freeze({ key: "ZULIP_KEY_JET", email: "ZULIP_EMAIL_JET" }),
  "GROK-WEB": Object.freeze({ key: "ZULIP_KEY_GROK_WEB", email: "ZULIP_EMAIL_GROK_WEB" }),
});

const BOT_EMAIL_RE = /^[a-z0-9-]+-bot@simplewithus\.zulipchat\.com$/;
const CHANNEL_NAME_RE = /^[a-z0-9][a-z0-9 _-]{0,59}$/;

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
  if (env.PUBLIC_HOST !== undefined && env.PUBLIC_HOST !== PUBLIC_HOST) {
    throw new Error("config: PUBLIC_HOST var does not match the compiled hostname");
  }

  const hostedSeats = parseList(env, "HOSTED_SEATS");
  for (const seat of hostedSeats) {
    if (!KNOWN_SEATS.includes(seat)) throw new Error(`config: unknown hosted seat ${seat}`);
  }

  const seatsRaw = parseJsonVar(env, "SEATS");
  if (typeof seatsRaw !== "object" || Array.isArray(seatsRaw)) throw new Error("config: SEATS must be an object");
  const seats = {};
  const redirectOwner = new Map();
  const cimdOwner = new Map();
  for (const [seat, entry] of Object.entries(seatsRaw)) {
    if (!KNOWN_SEATS.includes(seat)) throw new Error(`config: SEATS names unknown seat ${seat}`);
    const redirectUris = (entry?.redirect_uris ?? []).map(String);
    const cimdClientIds = (entry?.cimd_client_ids ?? []).map(String);
    for (const uri of redirectUris) {
      const problem = strictRedirectProblem(uri);
      if (problem) throw new Error(`config: ${seat} redirect is not strict (${problem})`);
      if (redirectOwner.has(uri)) throw new Error("config: a redirect URI is listed under two seats");
      redirectOwner.set(uri, seat);
    }
    for (const id of cimdClientIds) {
      const problem = strictRedirectProblem(id);
      if (problem) throw new Error(`config: ${seat} CIMD client id is not a strict https URL (${problem})`);
      if (cimdOwner.has(id)) throw new Error("config: a CIMD client id is listed under two seats");
      cimdOwner.set(id, seat);
    }
    seats[seat] = Object.freeze({ redirectUris: Object.freeze(redirectUris), cimdClientIds: Object.freeze(cimdClientIds) });
  }

  // D4:  the channel allowlist, pinned name to stream id.  Tools use the id,
  // never a name lookup, so a renamed or look-alike channel cannot widen it.
  const channelsRaw = parseJsonVar(env, "CHANNELS");
  if (typeof channelsRaw !== "object" || Array.isArray(channelsRaw)) throw new Error("config: CHANNELS must be an object");
  const channels = new Map();
  for (const [name, id] of Object.entries(channelsRaw)) {
    if (!CHANNEL_NAME_RE.test(name)) throw new Error("config: a CHANNELS name is not a plain lowercase channel name");
    if (!Number.isSafeInteger(id) || id <= 0) throw new Error(`config: CHANNELS.${name} must be a stream id`);
    if ([...channels.values()].includes(id)) throw new Error("config: a stream id is listed twice in CHANNELS");
    channels.set(name, id);
  }
  if (channels.size === 0) throw new Error("config: CHANNELS is empty");

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

  const teamDomain = String(env.ACCESS_TEAM_DOMAIN ?? "").trim();
  const accessAud = String(env.ACCESS_AUD ?? "").trim();
  const ownerEmails = parseList(env, "OWNER_EMAILS").map((e) => e.toLowerCase());

  return Object.freeze({
    host: PUBLIC_HOST,
    issuer: ISSUER,
    resource: RESOURCE,
    resourceMetadataUrl: RESOURCE_METADATA_URL,
    scopes: SCOPES,
    hostedSeats: Object.freeze(hostedSeats),
    seats: Object.freeze(seats),
    redirectOwner,
    cimdOwner,
    access: Object.freeze({
      teamDomain,
      aud: accessAud,
      ownerEmails: Object.freeze(ownerEmails),
      // Fail closed:  until the Access app exists and its AUD is in vars,
      // nothing behind Access can be reached.
      configured: /^[a-z0-9-]+\.cloudflareaccess\.com$/.test(teamDomain) && /^[0-9a-f]{64}$/.test(accessAud) && ownerEmails.length > 0,
    }),
    mcpDisabled: String(env.MCP_DISABLED ?? "") === "1",
    channels,
    channelIds: Object.freeze([...channels.values()]),
    ownerUserId,
    ownerClients: Object.freeze(ownerClients),
    botEmails: Object.freeze(botEmails),
  });
}

/** Every allowlisted CIMD client id, across seats. */
export function allCimdClientIds(config) {
  return [...config.cimdOwner.keys()];
}
