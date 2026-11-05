// Loads the Muster release credentials in CI, each from its own Infisical folder through its
// own scoped, read-only service token, and hands the later steps file paths rather than
// values.  Names only ever reach the log;  every value that does reach $GITHUB_ENV is masked
// first, line by line.  See muster/docs/AUTO-UPDATE.md, Secrets.
//
// A source whose token or project id is absent is reported by name, so the workflow can build
// and verify without it and say what is missing.  A token that IS present and fails its own
// scope check, or a folder that answers with an unresolved reference, fails the job:  a broken
// credential must never look like a missing one.
//
// The fetch, the scope check and the masking are scripts/infisical-fetch.mjs, unchanged.
import { chmodSync, mkdirSync, writeFileSync, appendFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import {
  isUnresolvedReference,
  listSecretsRaw,
  maskValue,
  writeGithubEnvValue,
} from "../../scripts/infisical-fetch.mjs";

// The AI Fleet Coordinator project id is public in INFISICAL.md.  Fleet-OPS's is not, so it
// comes from the INFISICAL_FLEET_OPS_PROJECT_ID repository secret.
export const AFC_PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5";

export const SOURCES = [
  {
    token: "INFISICAL_MAC_CERTS_READ_TOKEN",
    projectFrom: "INFISICAL_FLEET_OPS_PROJECT_ID",
    path: "/mac-certs",
    names: ["MAC_CERT_P12_BASE64", "MAC_CERT_PASSWORD"],
  },
  {
    token: "INFISICAL_ASC_READ_TOKEN",
    projectFrom: "INFISICAL_FLEET_OPS_PROJECT_ID",
    path: "/asc",
    names: ["ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_P8"],
  },
  {
    token: "INFISICAL_MUSTER_SPARKLE_READ_TOKEN",
    project: AFC_PROJECT_ID,
    path: "/muster-sparkle",
    names: ["MUSTER_SPARKLE_ED_PRIVATE_KEY"],
  },
];

// Values that go to a 0600 file, and the environment name that carries the file's path.
const FILES = {
  MAC_CERT_P12_BASE64: { file: "developer-id.p12", pathName: "MUSTER_CERT_P12_PATH", base64: true },
  ASC_KEY_P8: { file: "asc-key.p8", pathName: "ASC_KEY_PATH" },
  MUSTER_SPARKLE_ED_PRIVATE_KEY: { file: "sparkle-ed25519.key", pathName: "MUSTER_SPARKLE_KEY_PATH" },
};

function writePrivate(directory, file, data) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const path = join(directory, file);
  writeFileSync(path, data, { mode: 0o600 });
  chmodSync(path, 0o600);
  return path;
}

/** Loads every source it can.  Returns the names that are missing, in order. */
export async function load({ env = process.env, fetchImpl = fetch, log = (line) => console.log(line) } = {}) {
  const directory = join(env.RUNNER_TEMP || env.TMPDIR || "/tmp", "muster-release");
  const missing = [];
  for (const source of SOURCES) {
    const token = String(env[source.token] ?? "").trim();
    const projectId = source.project ?? String(env[source.projectFrom] ?? "").trim();
    const absent = [];
    if (!token) absent.push(source.token);
    if (!projectId) absent.push(source.projectFrom);
    if (absent.length > 0) {
      for (const name of absent) if (!missing.includes(name)) missing.push(name);
      log(`${source.path}: not read, because ${absent.join(" and ")} is not set.`);
      continue;
    }
    const values = await listSecretsRaw({
      siteUrl: env.INFISICAL_SITE_URL || "https://app.infisical.com",
      token,
      projectId,
      environment: "prod",
      secretPath: source.path,
      fetchImpl,
    });
    for (const name of source.names) {
      const value = values.get(name) || "";
      if (!value) {
        missing.push(name);
        log(`${source.path}: ${name} is empty or absent.`);
        continue;
      }
      if (isUnresolvedReference(value)) {
        throw new Error(`${name} in ${source.path} is an unresolved Infisical reference.`);
      }
      const target = FILES[name];
      if (target) {
        const data = target.base64 ? Buffer.from(value.replace(/\s+/g, ""), "base64") : value.endsWith("\n") ? value : `${value}\n`;
        if (target.base64 && data.length === 0) {
          throw new Error(`${name} in ${source.path} is not base64.`);
        }
        const path = writePrivate(directory, target.file, data);
        writeGithubEnvValue(env.GITHUB_ENV, target.pathName, path);
      } else {
        maskValue(value, log);
        writeGithubEnvValue(env.GITHUB_ENV, name, value);
      }
      log(`${source.path}: ${name} loaded.`);
    }
  }
  return missing;
}

export async function run({ env = process.env, fetchImpl = fetch, log = (line) => console.log(line) } = {}) {
  const missing = await load({ env, fetchImpl, log });
  if (env.GITHUB_OUTPUT) appendFileSync(env.GITHUB_OUTPUT, `missing=${missing.join(" ")}\n`);
  log(missing.length ? `Missing: ${missing.join(", ")}.` : "Every release credential loaded.");
}

const isMain = process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url;
if (isMain) {
  run().catch((err) => {
    console.error(err instanceof Error ? err.message : String(err));
    process.exitCode = 1;
  });
}
