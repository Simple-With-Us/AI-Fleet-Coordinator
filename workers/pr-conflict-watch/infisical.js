// Infisical is the only source of secrets for this Worker.
//
// The Worker's only secret binding is a read-only Infisical service token
// (INFISICAL_SERVICE_TOKEN).  Every other secret -- the GitHub webhook
// secret, the GitHub token and the Zulip bot pair -- is read at runtime from
// one Infisical folder, so a secret is rotated in Infisical and the Worker
// picks it up without a redeploy.
//
// This repo is public, so the project id is not committed here.  It is
// discovered from the token itself (GET /api/v2/service-token) and the
// token's own scope and permissions are checked before a single secret is
// read: the token must be read-only and must be scoped to exactly this
// environment and this folder.
//
// There are no npm dependencies in this Worker, so the response shapes are
// validated by hand at the boundary, in the same style as index.js's
// `parseZulipResult`: a plain object, string fields, an array of objects
// with string fields.  An unexpected body is never echoed into the logs.

/** The Infisical origin.  A constant, not a setting: the fleet reads prod. */
export const INFISICAL_BASE_URL = "https://app.infisical.com";

/** The only environment the fleet reads. */
export const INFISICAL_ENVIRONMENT = "prod";

/** The folder inside the project that holds this Worker's secrets. */
export const INFISICAL_SECRET_PATH = "/pr-conflict-watch";

/** Every secret name the Worker needs from the folder above. */
export const REQUIRED_SECRET_NAMES = [
  "GITHUB_WEBHOOK_SECRET",
  "GITHUB_TOKEN",
  "ZULIP_SITE",
  "ZULIP_BOT_EMAIL",
  "ZULIP_BOT_API_KEY",
];

/** How long a resolved config stays in the isolate's memory. */
export const CONFIG_CACHE_TTL_MS = 60_000;

/** The token info response is tiny; anything bigger is not what we expect. */
const TOKEN_INFO_MAX_BYTES = 64 * 1024;

/** The secret listing is small too; five secrets are a few kilobytes. */
const SECRET_LISTING_MAX_BYTES = 256 * 1024;

/**
 * A failure with a fixed code.
 *
 * The code is the whole point of this class: the message carries the code
 * and nothing else, so an error can be logged, answered and counted without
 * ever echoing a body, a token or a secret value.
 */
export class InfisicalError extends Error {
  /**
   * @param {string} code one of the fixed codes below.
   * @param {{status?: number, missing?: string[]}} [detail]
   */
  constructor(code, { status, missing } = {}) {
    super(`infisical: ${code}`);
    this.name = "InfisicalError";
    this.code = code;
    if (status !== undefined) this.status = status;
    if (missing !== undefined) this.missing = missing;
  }
}

/** True for a plain object, false for arrays, null and everything else. */
function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** The one place a failure is logged: names and codes, never values. */
function logFailure(error) {
  const line = { event: "infisical_failed", code: error.code };
  if (error.status !== undefined) line.status = error.status;
  if (error.missing !== undefined) line.missing = error.missing;
  console.error(JSON.stringify(line));
}

/** The error for a response whose status is not a success. */
function errorForStatus(response) {
  const status = Number(response?.status);
  // An opaque redirect: the runtime refused to follow it and did not expose
  // the status.  A redirect is a failure either way, and the token must not
  // travel to another origin, so it is never followed.
  if (!Number.isInteger(status) || status < 100) {
    return new InfisicalError("infisical_status_3xx");
  }
  return new InfisicalError(`infisical_status_${status}`, { status });
}

/**
 * Read a JSON body that is small enough to be the answer we expect.
 *
 * The size is capped two ways: the advertised `content-length` and the bytes
 * that actually arrive.  A body over the cap is invalid, whatever it says
 * it is.
 *
 * @param {Response} response
 * @param {number} maxBytes
 * @returns {Promise<unknown>} the parsed body.
 */
async function readJsonBody(response, maxBytes) {
  const advertised = response.headers.get("content-length");
  if (advertised !== null) {
    const length = Number(advertised);
    if (Number.isFinite(length) && length > maxBytes) {
      throw new InfisicalError("invalid_response");
    }
  }

  let bytes;
  try {
    bytes = await response.arrayBuffer();
  } catch {
    throw new InfisicalError("invalid_response");
  }
  if (bytes.byteLength > maxBytes) throw new InfisicalError("invalid_response");

  let text;
  try {
    text = new TextDecoder().decode(bytes);
  } catch {
    throw new InfisicalError("invalid_response");
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new InfisicalError("invalid_response");
  }
}

/**
 * Fetch the service token's own info.
 *
 * The response carries the project id (as `workspace`, or `projectId` when
 * `workspace` is absent) and, when present, the scopes and the permissions
 * of the token.  The scopes must name this environment and this folder, and
 * the permissions must not include write.
 *
 * @param {{token: string, fetchImpl: Function}} args
 * @returns {Promise<{projectId: string}>}
 */
async function fetchTokenInfo({ token, fetchImpl }) {
  let response;
  try {
    response = await fetchImpl(`${INFISICAL_BASE_URL}/api/v2/service-token`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
      },
      // A redirect is never followed: the token must not leave the origin.
      redirect: "manual",
    });
  } catch {
    throw new InfisicalError("infisical_unreachable");
  }

  if (!response || typeof response !== "object") {
    throw new InfisicalError("invalid_response");
  }
  if (!response.ok) throw errorForStatus(response);

  const json = await readJsonBody(response, TOKEN_INFO_MAX_BYTES);
  if (!isPlainObject(json)) throw new InfisicalError("invalid_response");

  const workspace = typeof json.workspace === "string" ? json.workspace.trim() : "";
  const fallbackProjectId = typeof json.projectId === "string" ? json.projectId.trim() : "";
  const projectId = workspace || fallbackProjectId;
  if (!projectId) throw new InfisicalError("invalid_response");

  if (json.scopes !== undefined) {
    if (!Array.isArray(json.scopes)) throw new InfisicalError("invalid_response");
    for (const scope of json.scopes) {
      if (!isPlainObject(scope)) throw new InfisicalError("invalid_response");
      if (typeof scope.environment !== "string" || typeof scope.secretPath !== "string") {
        throw new InfisicalError("invalid_response");
      }
    }
    // Exact scope:  every scope must be this environment and this folder,
    // so a token that can also read another folder is refused, not used.
    const exact = (scope) =>
      scope.environment.trim() === INFISICAL_ENVIRONMENT &&
      scope.secretPath.trim().replace(/\/+$/, "") === INFISICAL_SECRET_PATH;
    if (json.scopes.length === 0 || !json.scopes.every(exact)) {
      throw new InfisicalError("token_scope_mismatch");
    }
  }

  if (json.permissions !== undefined) {
    if (!Array.isArray(json.permissions)) throw new InfisicalError("invalid_response");
    const writable = json.permissions.some(
      (permission) =>
        typeof permission === "string" && permission.trim().toLowerCase() === "write"
    );
    if (writable) throw new InfisicalError("token_not_read_only");
  }

  return { projectId };
}

/**
 * Fetch the secret listing for the folder.
 *
 * @param {{token: string, fetchImpl: Function, projectId: string}} args
 * @returns {Promise<Array<{secretKey: string, secretValue: string}>>}
 */
async function fetchSecretListing({ token, fetchImpl, projectId }) {
  const url =
    `${INFISICAL_BASE_URL}/api/v3/secrets/raw` +
    `?workspaceId=${encodeURIComponent(projectId)}` +
    `&environment=${INFISICAL_ENVIRONMENT}` +
    `&secretPath=${encodeURIComponent(INFISICAL_SECRET_PATH)}`;

  let response;
  try {
    response = await fetchImpl(url, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
      },
      // A 3xx is a failure, never followed: the token must not leave the
      // origin, and a redirect to another host is not this Worker talking to
      // Infisical any more.
      redirect: "manual",
    });
  } catch {
    throw new InfisicalError("infisical_unreachable");
  }

  if (!response || typeof response !== "object") {
    throw new InfisicalError("invalid_response");
  }
  if (!response.ok) throw errorForStatus(response);

  const json = await readJsonBody(response, SECRET_LISTING_MAX_BYTES);
  if (!isPlainObject(json)) throw new InfisicalError("invalid_response");
  if (!Array.isArray(json.secrets)) throw new InfisicalError("invalid_response");

  const secrets = [];
  for (const entry of json.secrets) {
    if (!isPlainObject(entry)) throw new InfisicalError("invalid_response");
    if (typeof entry.secretKey !== "string" || typeof entry.secretValue !== "string") {
      throw new InfisicalError("invalid_response");
    }
    secrets.push({ secretKey: entry.secretKey, secretValue: entry.secretValue });
  }
  return secrets;
}

// The resolved config, in this isolate's memory only: never written to KV,
// never logged, and dropped after CONFIG_CACHE_TTL_MS.
let configCache = null;

/** Drop the cached config.  For tests only. */
export function _resetConfigCacheForTests() {
  configCache = null;
}

/**
 * Read every secret this Worker needs and return them as one config object.
 *
 * The call is cached for CONFIG_CACHE_TTL_MS so a burst of webhooks does not
 * hammer Infisical.  A failure is never cached: the next request tries again.
 *
 * @param {{token?: string, fetchImpl?: Function, now?: () => number}} [args]
 * @returns {Promise<Record<string, string>>} the resolved secrets.
 * @throws {InfisicalError} with a fixed code.
 */
export async function resolveInfisicalConfig({ token, fetchImpl, now } = {}) {
  const serviceToken = typeof token === "string" ? token.trim() : "";
  const doFetch = fetchImpl ?? globalThis.fetch;
  const clock = now ?? (() => Date.now());

  try {
    if (!serviceToken) throw new InfisicalError("not_configured");

    if (
      configCache &&
      configCache.token === serviceToken &&
      configCache.expiresAt > clock()
    ) {
      return configCache.config;
    }

    const info = await fetchTokenInfo({ token: serviceToken, fetchImpl: doFetch });
    const secrets = await fetchSecretListing({
      token: serviceToken,
      fetchImpl: doFetch,
      projectId: info.projectId,
    });

    const values = new Map();
    for (const secret of secrets) {
      values.set(secret.secretKey, secret.secretValue.trim());
    }

    const missing = REQUIRED_SECRET_NAMES.filter((name) => {
      const value = values.get(name);
      return typeof value !== "string" || value === "";
    });
    if (missing.length > 0) throw new InfisicalError("missing_secret", { missing });

    const config = {};
    for (const name of REQUIRED_SECRET_NAMES) config[name] = values.get(name);

    configCache = {
      token: serviceToken,
      config,
      expiresAt: clock() + CONFIG_CACHE_TTL_MS,
    };
    return config;
  } catch (error) {
    const failure =
      error instanceof InfisicalError ? error : new InfisicalError("infisical_unreachable");
    logFailure(failure);
    throw failure;
  }
}
