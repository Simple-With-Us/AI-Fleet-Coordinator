// Copied verbatim from Simple-With-Us/BotFleet scripts/infisical-fetch.mjs on
// 2026-09-29, with .github/actions/infisical-secrets, so the Mac release
// workflow reads its signing certificate the way BotFleet's release.yml does.
// Keep the two copies in step until the fleet has a shared action.
//
// CI-side Infisical fetch: scoped service-token bearer, one bulk secrets list, then
// per requested name mask + export into $GITHUB_ENV -- falling back to a
// same-named GH_FALLBACK_<NAME> step env var, warning when a name stays
// empty everywhere, and failing only for a name listed in `required`.
//
// Runs unmodified on GitHub-hosted macOS, Windows and Linux runners: no CLI
// install (ios-ship.yml's `brew install infisical/get-cli/infisical` is
// macOS-only), no dependency (`@infisical/sdk` is in neither package.json
// nor pnpm-lock.yaml) -- just global `fetch`, which every GitHub runner's
// Node ships with.
//
// Never prints a secret value except as the payload of an `::add-mask::`
// workflow command, which is the mechanism GitHub uses to scrub it from the
// rendered log from that point on -- one command per LINE of the value, see
// `maskValue` below, because the runner's parser stops at the first newline.
// A login/list failure never crashes the job -- callers still get their
// GH_FALLBACK_<NAME> values, or the missing-name warning/error below.
import { appendFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

function splitNames(raw) {
  return String(raw ?? "")
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function stripTrailingSlash(url) {
  return String(url ?? "").replace(/\/+$/, "");
}

const SERVICE_TOKEN = /^st\.\S{17,2045}$/;

export function requireServiceToken(value) {
  const token = String(value ?? "").trim();
  if (!SERVICE_TOKEN.test(token)) {
    throw new Error("INFISICAL_TOKEN must be a valid scoped Infisical service token.");
  }
  return token;
}

export class MetadataVerificationError extends Error {}
export class SecretListingError extends Error {
  constructor(message, { recoverable = false } = {}) {
    super(message);
    this.recoverable = recoverable;
  }
}

const PROJECT_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const EXPIRY_TZ = /(?:Z|[+-]\d{2}:\d{2})$/;
const MAX_METADATA_BYTES = 128 * 1024;
const MAX_SECRETS_BYTES = 16 * 1024 * 1024;

function validateSiteUrl(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("INFISICAL_SITE_URL must be a valid HTTPS URL.");
  }
  const localHttp = parsed.protocol === "http:" &&
    (parsed.hostname === "localhost" || parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]");
  if ((parsed.protocol !== "https:" && !localHttp) || !parsed.hostname || parsed.username || parsed.password ||
      parsed.search || parsed.hash || (parsed.pathname !== "/" && parsed.pathname !== "")) {
    throw new Error("INFISICAL_SITE_URL must be a valid HTTPS URL.");
  }
  return parsed.origin;
}

async function readJsonBounded(response, limit, stage) {
  if (!response.body || typeof response.body.getReader !== "function") {
    throw new Error(`Infisical ${stage} response was invalid.`);
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new Error(`Infisical ${stage} response exceeded the size limit.`);
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  try {
    return JSON.parse(Buffer.concat(chunks, size).toString("utf8"));
  } catch {
    throw new Error(`Infisical ${stage} response was invalid.`);
  }
}

async function requestJson({ url, token, fetchImpl, timeoutMs, limit, stage, metadata = false }) {
  let res;
  try {
    res = await fetchImpl(url, {
      method: "GET",
      headers: { authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(timeoutMs),
      redirect: "error",
    });
  } catch {
    if (metadata) throw new MetadataVerificationError(`Infisical ${stage} verification failed.`);
    throw new SecretListingError(`Infisical ${stage} request failed.`, { recoverable: true });
  }
  if (!res.ok) {
    if (metadata) throw new MetadataVerificationError(`Infisical ${stage} verification failed.`);
    const status = Number(res.status);
    throw new SecretListingError(
      `Infisical ${stage} request failed: HTTP ${status}.`,
      { recoverable: status >= 500 },
    );
  }
  try {
    return await readJsonBounded(res, limit, stage);
  } catch (error) {
    if (metadata) throw new MetadataVerificationError(`Infisical ${stage} verification failed.`);
    throw new SecretListingError(error.message, { recoverable: false });
  }
}

export function validateServiceTokenMetadata(metadata, { projectId, environment, secretPath, tokenId }) {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata) ||
      typeof metadata.id !== "string" || !metadata.id ||
      (tokenId && metadata.id !== tokenId) ||
      (metadata._id !== undefined && metadata._id !== metadata.id) ||
      typeof metadata.name !== "string" || !metadata.name ||
      ("projectId" in metadata && metadata.projectId !== projectId) || metadata.workspace !== projectId) {
    throw new MetadataVerificationError("Infisical service-token metadata did not match the requested project.");
  }
  if (!Array.isArray(metadata.scopes) || metadata.scopes.length !== 1 ||
      !metadata.scopes[0] || typeof metadata.scopes[0] !== "object" ||
      Object.keys(metadata.scopes[0]).sort().join(",") !== "environment,secretPath" ||
      metadata.scopes[0].environment !== environment || metadata.scopes[0].secretPath !== secretPath ||
      !Array.isArray(metadata.permissions) || metadata.permissions.length !== 1 ||
      metadata.permissions[0] !== "read") {
    throw new MetadataVerificationError("Infisical service-token metadata did not match the requested scope.");
  }
  if (metadata.expiresAt == null) return;
  if (typeof metadata.expiresAt !== "string" || !EXPIRY_TZ.test(metadata.expiresAt)) {
    throw new MetadataVerificationError("Infisical service-token expiry metadata is invalid.");
  }
  const expiry = Date.parse(metadata.expiresAt);
  if (!Number.isFinite(expiry) || expiry <= Date.now()) {
    throw new MetadataVerificationError("Infisical service-token expiry metadata is invalid.");
  }
}

export async function verifyServiceToken({ siteUrl, token, projectId, environment, secretPath, fetchImpl = fetch, timeoutMs = 30_000 }) {
  siteUrl = validateSiteUrl(stripTrailingSlash(siteUrl));
  token = requireServiceToken(token);
  if (!PROJECT_ID.test(projectId) || environment !== "prod" || !secretPath.startsWith("/") ||
      secretPath.split("/").some((part) => part === "." || part === "..")) {
    throw new MetadataVerificationError("Infisical project, environment, or folder configuration is invalid.");
  }
  const metadata = await requestJson({
    url: `${siteUrl}/api/v2/service-token`, token, fetchImpl, timeoutMs,
    limit: MAX_METADATA_BYTES, stage: "service-token metadata", metadata: true,
  });
  validateServiceTokenMetadata(metadata, { projectId, environment, secretPath });
}

/** GET one exact folder after verifying the service token's own grant. */
export async function listSecretsRaw({ siteUrl, token, projectId, environment, secretPath, fetchImpl = fetch, timeoutMs = 30_000 }) {
  await verifyServiceToken({ siteUrl, token, projectId, environment, secretPath, fetchImpl, timeoutMs });
  siteUrl = validateSiteUrl(stripTrailingSlash(siteUrl));
  token = requireServiceToken(token);
  const qs = new URLSearchParams({
    workspaceId: projectId,
    environment,
    secretPath,
    viewSecretValue: "true",
    expandSecretReferences: "false",
    recursive: "false",
    include_imports: "false",
  });
  const body = await requestJson({
    url: `${siteUrl}/api/v3/secrets/raw?${qs.toString()}`,
    token, fetchImpl, timeoutMs, limit: MAX_SECRETS_BYTES, stage: "secrets list",
  });
  if (!body || typeof body !== "object" || Array.isArray(body) || !Array.isArray(body.secrets) ||
      (body.totalCount !== undefined && body.totalCount !== body.secrets.length)) {
    throw new SecretListingError("Infisical secrets list was incomplete or invalid.");
  }
  const values = new Map();
  for (const s of body.secrets) {
    if (!s || typeof s !== "object" || Array.isArray(s) ||
        s.type !== "shared" || s.secretValueHidden === true ||
        (s.workspace !== undefined && s.workspace !== projectId) ||
        (s.environment !== undefined && s.environment !== environment) ||
        (s.secretPath !== undefined && s.secretPath !== secretPath) ||
        typeof s.secretKey !== "string" || !s.secretKey || typeof s.secretValue !== "string" ||
        values.has(s.secretKey)) {
      throw new SecretListingError("Infisical secrets list contained unverified records.");
    }
    values.set(s.secretKey, s.secretValue);
  }
  return values;
}

/** Registers each LINE of the value as its own mask, and never the joined
 * value.
 *
 * `::add-mask::` is a workflow command the runner parses one LINE at a
 * time: it registers only the text up to the first newline, then echoes the
 * remaining lines of that same write to the job log as ordinary, unmasked
 * output.  So a command carrying a whole multi-line value is not merely
 * useless -- emitting it FIRST is the leak itself: the runner registers line
 * one and prints every line after it into a public log, before the per-line
 * commands that would have hidden them have run.  A wrapped base64 blob --
 * which is exactly what `base64` produces on both macOS and GNU coreutils
 * unless given `-b 0` / `-w 0` -- is the everyday shape of such a value.
 *
 * GitHub's own `secrets.*` machinery registers each line of a multi-line
 * secret as a separate mask; this does the same, so a signing certificate or
 * an ASC key routed through this action is scrubbed as thoroughly as it was
 * when the workflow read it from `secrets.*`.  A single-line value is its own
 * only line, so it still costs exactly one command.
 *
 * The `seen` set collapses a value whose lines repeat, and the `trim()`
 * guard keeps a blank trailing line from registering the empty string (which
 * would mask nothing and warn on some runner versions). */
export function maskValue(value, log) {
  const seen = new Set();
  for (const line of String(value).split(/\r?\n/)) {
    if (!line.trim() || seen.has(line)) continue;
    seen.add(line);
    log(`::add-mask::${line}`);
  }
}

/** Appends one GitHub Actions multiline env-var entry.  The delimiter is
 * random per call so a value that happens to contain a fixed delimiter
 * string can never break the file for a later name. */
export function writeGithubEnvValue(path, name, value, appendFileImpl = appendFileSync) {
  if (!path) return;
  const delimiter = `INFISICAL_${randomUUID().replace(/-/g, "")}`;
  appendFileImpl(path, `${name}<<${delimiter}\n${value}\n${delimiter}\n`);
}

/** A value that is still a bare Infisical reference such as
 * ${prod.some-folder.NAME}: the server did not expand it (target missing, or
 * the reading identity cannot read the referenced folder).  Exporting it would
 * hand a workflow the literal reference string as if it were the credential. */
export const UNRESOLVED_REFERENCE = /\$\{[^}]+\}/;

export function isUnresolvedReference(value) {
  return UNRESOLVED_REFERENCE.test(String(value ?? "").trim());
}

/** Resolves the vault snapshot (empty map when unconfigured or the fetch
 * failed) -- never throws; a failure is reported through `log` and the
 * caller proceeds on GH_FALLBACK_<NAME> alone. */
async function resolveVaultValues({ siteUrl, projectId, token, environment, secretPath, fetchImpl, log }) {
  try {
    return await listSecretsRaw({ siteUrl, token, projectId, environment, secretPath, fetchImpl });
  } catch (err) {
    if (err instanceof MetadataVerificationError) throw err;
    if (err instanceof SecretListingError && !err.recoverable) throw err;
    const reason = err instanceof SecretListingError ? err.message : "request failed";
    log(`::warning::Infisical ${environment} fetch unavailable (${reason}); using GitHub secret fallbacks only.`);
    return new Map();
  }
}

export async function run({ env = process.env, fetchImpl = fetch, log = (line) => console.log(line) } = {}) {
  const names = splitNames(env.INFISICAL_NAMES);
  const required = new Set(splitNames(env.INFISICAL_REQUIRED));
  const siteUrl = validateSiteUrl(stripTrailingSlash(env.INFISICAL_SITE_URL || "https://app.infisical.com"));
  const projectId = env.INFISICAL_PROJECT_ID || "";
  const environment = env.INFISICAL_ENVIRONMENT || "prod";
  const secretPath = env.INFISICAL_SECRET_PATH || "/";
  const token = requireServiceToken(env.INFISICAL_TOKEN);
  const githubEnvPath = env.GITHUB_ENV || "";
  if (!PROJECT_ID.test(projectId) || environment !== "prod" || !secretPath.startsWith("/") ||
      secretPath.split("/").some((part) => part === "." || part === "..")) {
    throw new Error("Infisical project, environment, or folder configuration is invalid.");
  }

  const vaultValues = await resolveVaultValues({ siteUrl, projectId, token, environment, secretPath, fetchImpl, log });

  const missingRequired = [];
  const unresolved = [];
  for (const name of names) {
    let value = vaultValues.get(name) || "";
    if (!value) {
      value = env[`GH_FALLBACK_${name}`] || "";
    }
    if (!value) {
      const message = `${name} is empty after the Infisical ${environment} export and the GitHub secret fallback.`;
      if (required.has(name)) {
        missingRequired.push(name);
        log(`::error::${message}`);
      } else {
        log(`::warning::${message}`);
      }
      continue;
    }
    if (isUnresolvedReference(value)) {
      // Name the KEY only -- never the value, even though it is a reference.
      unresolved.push(name);
      log(`::error::${name} is still an unresolved Infisical secret reference after the ${environment} export; check the target exists and the CI identity can read the referenced folder.`);
      continue;
    }
    maskValue(value, log);
    writeGithubEnvValue(githubEnvPath, name, value);
  }

  if (unresolved.length > 0) {
    throw new Error(`Unresolved Infisical secret reference(s): ${unresolved.join(", ")}`);
  }

  if (missingRequired.length > 0) {
    throw new Error(`Missing required Infisical name(s): ${missingRequired.join(", ")}`);
  }
}

const isMain = process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url;
if (isMain) {
  run().catch((err) => {
    console.error(err instanceof Error ? err.message : String(err));
    process.exitCode = 1;
  });
}
