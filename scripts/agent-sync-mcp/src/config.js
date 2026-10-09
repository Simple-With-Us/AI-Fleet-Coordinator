// Configuration for the hosted agent-sync MCP Worker (Phase 0 stub).
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
  });
}

/** Every allowlisted CIMD client id, across seats. */
export function allCimdClientIds(config) {
  return [...config.cimdOwner.keys()];
}
