/**
 * Read-only App Store Connect credentials for the TestFlight freshness check.
 *
 * The binding is an Infisical service token whose own metadata is exactly
 * Fleet-OPS prod /asc with permission "read".  A missing binding, a token that
 * is not that grant, or a row that does not prove that folder fails closed.
 * This module never calls fleet-secrets, never reads a legacy key folder,
 * and never uses a machine identity or universal-auth.
 *
 * Homes for the one token, with no fall-through after a chosen home fails:
 *   IOS_TF_FRESHNESS_INFISICAL_TOKEN
 *   IOS_TF_FRESHNESS_INFISICAL_TOKEN_FILE   (mode 0600 or 0400)
 *   login keychain  service infisical-service-token
 *                   account ios-testflight-freshness-ro
 *
 * The project id is INFISICAL_FLEET_OPS_PROJECT_ID, else the first line of
 * ~/.simple-tools/fleet-ops-project-id.  It is a UUID, not a credential.
 */
import { execFile as execFileCb } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const TOKEN_ENV = "IOS_TF_FRESHNESS_INFISICAL_TOKEN";
export const TOKEN_FILE_ENV = "IOS_TF_FRESHNESS_INFISICAL_TOKEN_FILE";
export const LEGACY_ASC_ENV = "IOS_TF_ALLOW_LEGACY_ASC_CREDS";
export const PROJECT_ENV = "INFISICAL_FLEET_OPS_PROJECT_ID";
export const PROJECT_FILE = ".simple-tools/fleet-ops-project-id";
export const KEYCHAIN_SERVICE = "infisical-service-token";
export const KEYCHAIN_ACCOUNT = "ios-testflight-freshness-ro";
export const SECRET_PATH = "/asc";
export const ENVIRONMENT = "prod";
export const API_BASE = "https://app.infisical.com/api";
export const ASC_NAMES = Object.freeze(["ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_P8"]);
export const SECURITY_BIN = "/usr/bin/security";
const SECURITY_ITEM_NOT_FOUND = 44;
const SECURITY_INTERACTION_NOT_ALLOWED = 36;
const MAX_METADATA_BYTES = 128 * 1024;
const MAX_SECRET_BYTES = 1024 * 1024;
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const UNRESOLVED_REFERENCE = /\$\{[^}]+\}/;
const ENV_SLUGS = ["INFISICAL_ENVIRONMENT", "INFISICAL_ENV"];
const CHILD_STRIP = Object.freeze([
  TOKEN_ENV,
  TOKEN_FILE_ENV,
  "INFISICAL_TOKEN",
  "INFISICAL_WRITE_TOKEN",
  "INFISICAL_AI_FLEET_COORDINATOR_TOKEN",
  "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
  "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
  "INFISICAL_AUTOMATION_CLIENT_ID",
  "INFISICAL_AUTOMATION_CLIENT_SECRET",
  "INFISICAL_CLIENT_ID",
  "INFISICAL_CLIENT_SECRET",
  "ASC_KEY_PATH",
  "ASC_PRIVATE_KEY_PATH",
  "APP_STORE_CONNECT_API_KEY_KEY_FILEPATH",
]);

const NO_FALLBACK = "This check does not fall back to fleet-secrets /asc or a legacy key folder.";

export class AscCredentialError extends Error {
  constructor(message) {
    super(message);
    this.name = "AscCredentialError";
  }
}

export function legacyAscAllowed(env = process.env, flag = false) {
  if (flag === true) return true;
  const raw = String(env?.[LEGACY_ASC_ENV] ?? "").trim().toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes";
}

function refuse(message) {
  throw new AscCredentialError(message);
}

function checkServiceToken(token) {
  if (
    typeof token !== "string"
    || !token.startsWith("st.")
    || token.length < 20
    || token.length > 8192
    || /[\s\0]/.test(token)
  ) {
    refuse(`App Store Connect credentials: the bound value is not a scoped Infisical service token. ${NO_FALLBACK}`);
  }
  return token;
}

function refuseEnvironment(env) {
  for (const name of ENV_SLUGS) {
    const slug = String(env?.[name] ?? "").trim().toLowerCase();
    if (!slug || slug === ENVIRONMENT) continue;
    const shown = slug === "dev" || slug === "staging" ? slug : "a non-prod value";
    refuse(`App Store Connect credentials: refusing Infisical environment ${shown} from ${name}. Prod is the only environment.`);
  }
}

function loadProjectId(env, home, readFileImpl) {
  let value = String(env?.[PROJECT_ENV] ?? "").trim();
  if (!value) {
    try {
      const text = readFileImpl(path.join(home, PROJECT_FILE), "utf8");
      value = String(text ?? "").split(/\r?\n/, 1)[0].trim();
    } catch {
      value = "";
    }
  }
  if (!value) {
    refuse(
      "App Store Connect credentials: the Fleet-OPS Infisical project id is not set. "
      + `Set ${PROJECT_ENV} or put the UUID in ~/${PROJECT_FILE}.`,
    );
  }
  if (!UUID_RE.test(value)) {
    refuse(
      `App Store Connect credentials: the Fleet-OPS Infisical project id (${PROJECT_ENV} or ~/${PROJECT_FILE}) is not a UUID.`,
    );
  }
  return value;
}

function readTokenFile(file, statImpl, readFileImpl) {
  let mode;
  try {
    mode = statImpl(file).mode;
  } catch {
    refuse(`App Store Connect credentials: the token file named by ${TOKEN_FILE_ENV} cannot be read. ${NO_FALLBACK}`);
  }
  if ((mode & 0o777) !== 0o600 && (mode & 0o777) !== 0o400) {
    refuse(`App Store Connect credentials: the token file named by ${TOKEN_FILE_ENV} must be mode 0600 or 0400. ${NO_FALLBACK}`);
  }
  let text;
  try {
    text = readFileImpl(file, "utf8");
  } catch {
    refuse(`App Store Connect credentials: the token file named by ${TOKEN_FILE_ENV} cannot be read. ${NO_FALLBACK}`);
  }
  return checkServiceToken(String(text ?? "").trim());
}

function readKeychain(execFileImpl) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (status, token = "") => {
      if (settled) return;
      settled = true;
      resolve({ status, token });
    };
    try {
      execFileImpl(SECURITY_BIN, [
        "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
      ], { encoding: "utf8", timeout: 15_000, maxBuffer: 1024 * 1024 }, (err, stdout, stderr) => {
        // stderr is classified here and never leaves this callback.
        const detail = String(stderr ?? "").toLowerCase();
        if (err && (err.code === "ENOENT" || err.code === "EACCES")) {
          finish("token-unreadable");
          return;
        }
        const code = err ? (typeof err.code === "number" ? err.code : 1) : 0;
        if (code === 0) {
          const token = String(stdout ?? "").trim();
          finish(token ? "ok" : "no-token", token);
          return;
        }
        if (code === SECURITY_ITEM_NOT_FOUND || detail.includes("could not be found")) {
          finish("no-token");
          return;
        }
        if (code === SECURITY_INTERACTION_NOT_ALLOWED || detail.includes("interaction is not allowed")) {
          finish("keychain-locked");
          return;
        }
        finish("token-unreadable");
      });
    } catch {
      finish("token-unreadable");
    }
  });
}

function missingBinding(why) {
  return (
    `App Store Connect credentials: ${why} `
    + `Set ${TOKEN_ENV}, or a 0600 file in ${TOKEN_FILE_ENV}, or the login-keychain item `
    + `${KEYCHAIN_SERVICE} / ${KEYCHAIN_ACCOUNT}. ${NO_FALLBACK}`
  );
}

function keychainRefusal(status) {
  switch (status) {
    case "no-token":
      return missingBinding(`no scoped read-only service token for ${KEYCHAIN_ACCOUNT}.`);
    case "keychain-locked":
      return (
        `App Store Connect credentials: the login keychain refused ${KEYCHAIN_ACCOUNT} `
        + `(locked, or this process is outside the GUI session). ${NO_FALLBACK}`
      );
    case "token-unreadable":
      return (
        `App Store Connect credentials: the login-keychain item ${KEYCHAIN_ACCOUNT} could not be read. ${NO_FALLBACK}`
      );
    case "ok":
      return "";
    default: {
      const neverStatus = status;
      return missingBinding(`the keychain binding ${String(neverStatus)} could not be used.`);
    }
  }
}

async function loadToken(env, statImpl, readFileImpl, execFileImpl) {
  const fromEnv = String(env?.[TOKEN_ENV] ?? "").trim();
  if (fromEnv) return checkServiceToken(fromEnv);
  const file = String(env?.[TOKEN_FILE_ENV] ?? "").trim();
  if (file) return readTokenFile(file, statImpl, readFileImpl);
  const found = await readKeychain(execFileImpl);
  if (found.status !== "ok") refuse(keychainRefusal(found.status));
  return checkServiceToken(found.token);
}

async function readLimited(response, limit, stage) {
  if (!response || typeof response.text !== "function") {
    refuse(`App Store Connect credentials: Infisical ${stage} response was invalid.`);
  }
  let text;
  try {
    text = await response.text();
  } catch {
    refuse(`App Store Connect credentials: Infisical ${stage} response was invalid.`);
  }
  if (Buffer.byteLength(text) > limit) {
    refuse(`App Store Connect credentials: Infisical ${stage} response exceeded the size limit.`);
  }
  try {
    return JSON.parse(text);
  } catch {
    refuse(`App Store Connect credentials: Infisical ${stage} response was invalid.`);
  }
}

async function requestJson(fetchImpl, url, token, stage, limit) {
  let response;
  try {
    response = await fetchImpl(url, {
      method: "GET",
      headers: { authorization: `Bearer ${token}`, accept: "application/json" },
      redirect: "error",
    });
  } catch {
    refuse(`App Store Connect credentials: Infisical ${stage} request failed. ${NO_FALLBACK}`);
  }
  if (!response || response.ok !== true) {
    const status = Number(response?.status);
    const http = Number.isInteger(status) ? `HTTP ${status}` : "no response";
    refuse(`App Store Connect credentials: Infisical ${stage} request failed: ${http}. ${NO_FALLBACK}`);
  }
  return readLimited(response, limit, stage);
}

function scopeRefusal() {
  return (
    `App Store Connect credentials: the service token for ${KEYCHAIN_ACCOUNT} is not a read-only grant `
    + `for exactly ${ENVIRONMENT}:${SECRET_PATH}. ${NO_FALLBACK}`
  );
}

function validateMetadata(metadata, projectId, nowMs) {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) refuse(scopeRefusal());
  if (metadata.workspace !== projectId) refuse(scopeRefusal());
  if ("projectId" in metadata && metadata.projectId !== projectId) refuse(scopeRefusal());
  const permissions = metadata.permissions;
  if (!Array.isArray(permissions) || permissions.length !== 1 || permissions[0] !== "read") refuse(scopeRefusal());
  const scopes = metadata.scopes;
  if (!Array.isArray(scopes) || scopes.length !== 1) refuse(scopeRefusal());
  const scope = scopes[0];
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) refuse(scopeRefusal());
  const keys = Object.keys(scope).sort();
  if (keys.join(",") !== "environment,secretPath") refuse(scopeRefusal());
  if (scope.environment !== ENVIRONMENT || scope.secretPath !== SECRET_PATH) refuse(scopeRefusal());
  if (metadata.expiresAt == null) return;
  if (typeof metadata.expiresAt !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(metadata.expiresAt)) {
    refuse(`App Store Connect credentials: the service token for ${KEYCHAIN_ACCOUNT} has an invalid expiry. ${NO_FALLBACK}`);
  }
  const expiry = Date.parse(metadata.expiresAt);
  if (!Number.isFinite(expiry) || expiry <= nowMs) {
    refuse(`App Store Connect credentials: the service token for ${KEYCHAIN_ACCOUNT} is expired. ${NO_FALLBACK}`);
  }
}

function secretQuery(projectId) {
  return new URLSearchParams({
    workspaceId: projectId,
    environment: ENVIRONMENT,
    secretPath: SECRET_PATH,
    viewSecretValue: "true",
    expandSecretReferences: "false",
    recursive: "false",
    include_imports: "false",
  }).toString();
}

function readSecretRow(payload, name, projectId) {
  const secret = payload && typeof payload === "object" ? payload.secret : null;
  if (!secret || typeof secret !== "object" || Array.isArray(secret)) {
    refuse(`App Store Connect credentials: prod:${SECRET_PATH} returned an unverified row for ${name}.`);
  }
  const checks = [
    ["secretKey", name],
    ["secretPath", SECRET_PATH],
    ["workspace", projectId],
    ["environment", ENVIRONMENT],
    ["type", "shared"],
  ];
  for (const [field, want] of checks) {
    if (secret[field] !== want) {
      refuse(`App Store Connect credentials: prod:${SECRET_PATH} returned an unverified row for ${name}.`);
    }
  }
  if (!("secretValueHidden" in secret) || secret.secretValueHidden !== false) {
    refuse(`App Store Connect credentials: prod:${SECRET_PATH} returned an unverified row for ${name}.`);
  }
  const value = secret.secretValue;
  if (typeof value !== "string" || !value) {
    refuse(`App Store Connect credentials: prod:${SECRET_PATH} has no usable ${name}.`);
  }
  if (UNRESOLVED_REFERENCE.test(value)) {
    refuse(`App Store Connect credentials: ${name} at prod:${SECRET_PATH} is an unresolved Infisical reference.`);
  }
  return value;
}

/**
 * The three ASC values, after the token's own grant matches prod:/asc read.
 * `options` replaces network and filesystem seams in tests.  Values stay in the
 * returned object and are never included in an error.
 */
export async function resolveFreshnessAscCredentials(options = {}) {
  const env = options.env ?? process.env;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const readFileImpl = options.readFileImpl ?? fs.readFileSync;
  const statImpl = options.statImpl ?? fs.statSync;
  const execFileImpl = options.execFileImpl ?? execFileCb;
  const home = options.home ?? env.HOME ?? os.homedir();
  const nowMs = options.nowMs ?? Date.now();
  refuseEnvironment(env);
  const token = await loadToken(env, statImpl, readFileImpl, execFileImpl);
  const projectId = loadProjectId(env, home, readFileImpl);
  const metadata = await requestJson(
    fetchImpl,
    `${API_BASE}/v2/service-token`,
    token,
    "service-token verification",
    MAX_METADATA_BYTES,
  );
  validateMetadata(metadata, projectId, nowMs);
  const query = secretQuery(projectId);
  const creds = {};
  for (const name of ASC_NAMES) {
    const payload = await requestJson(
      fetchImpl,
      `${API_BASE}/v3/secrets/raw/${encodeURIComponent(name)}?${query}`,
      token,
      `secret read ${name}`,
      MAX_SECRET_BYTES,
    );
    creds[name] = readSecretRow(payload, name, projectId);
  }
  return creds;
}

/** Child environment for asc-api.mjs: the three verified values, and no service token. */
export function ascChildEnv(parent, creds) {
  const next = { ...parent };
  for (const name of CHILD_STRIP) delete next[name];
  for (const name of ASC_NAMES) {
    const value = creds?.[name];
    if (typeof value !== "string" || !value) {
      refuse(`App Store Connect credentials: prod:${SECRET_PATH} has no usable ${name}.`);
    }
    next[name] = value;
  }
  return next;
}
