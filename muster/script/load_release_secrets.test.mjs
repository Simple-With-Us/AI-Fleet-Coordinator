// The Muster release credential loader, against a stand-in Infisical.  No network, no real
// token and no value from anywhere real:  every value here is made up for the test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { AFC_PROJECT_ID, load, run } from "./load_release_secrets.mjs";

// A stand-in made at run time, so no project id is committed for the test.
const FLEET_OPS = randomUUID();
// Deliberately low-entropy stand-ins, so no secret scanner mistakes them for real ones.
const TOKENS = {
  INFISICAL_MAC_CERTS_READ_TOKEN: `st.${"a".repeat(24)}`,
  INFISICAL_ASC_READ_TOKEN: `st.${"b".repeat(24)}`,
  INFISICAL_MUSTER_SPARKLE_READ_TOKEN: `st.${"c".repeat(24)}`,
};
const P8 = "FAKE-P8-CONTENT\nSECOND-LINE";
const SEED = "d".repeat(44);
const PASSWORD = "e".repeat(12);
const FOLDERS = {
  [TOKENS.INFISICAL_MAC_CERTS_READ_TOKEN]: { project: FLEET_OPS, path: "/mac-certs" },
  [TOKENS.INFISICAL_ASC_READ_TOKEN]: { project: FLEET_OPS, path: "/asc" },
  [TOKENS.INFISICAL_MUSTER_SPARKLE_READ_TOKEN]: { project: AFC_PROJECT_ID, path: "/muster-sparkle" },
};

function stand_in(values, { scopeFor } = {}) {
  return async (url, options) => {
    const token = options.headers.authorization.replace("Bearer ", "");
    const folder = FOLDERS[token];
    if (url.endsWith("/api/v2/service-token")) {
      const scope = scopeFor ? scopeFor(folder) : folder;
      return new Response(JSON.stringify({
        id: `id-${scope.path}`, name: `muster-${scope.path}`, workspace: scope.project,
        scopes: [{ environment: "prod", secretPath: scope.path }], permissions: ["read"],
      }));
    }
    const secrets = Object.entries(values[folder.path] ?? {}).map(([secretKey, secretValue]) => ({
      type: "shared", secretKey, secretValue, environment: "prod", secretPath: folder.path,
    }));
    return new Response(JSON.stringify({ secrets }));
  };
}

function environment(extra = {}) {
  const dir = mkdtempSync(join(tmpdir(), "muster-release-test-"));
  return { RUNNER_TEMP: dir, GITHUB_ENV: join(dir, "env"), GITHUB_OUTPUT: join(dir, "out"), ...extra };
}

const FULL = {
  "/mac-certs": { MAC_CERT_P12_BASE64: Buffer.from("p12 bytes").toString("base64"), MAC_CERT_PASSWORD: PASSWORD },
  "/asc": { ASC_KEY_ID: "KEYID", ASC_ISSUER_ID: "issuer", ASC_KEY_P8: P8 },
  "/muster-sparkle": { MUSTER_SPARKLE_ED_PRIVATE_KEY: SEED },
};

test("with every token present, keys land in 0600 files and nothing is missing", async () => {
  const env = environment({ ...TOKENS, INFISICAL_FLEET_OPS_PROJECT_ID: FLEET_OPS });
  const lines = [];
  const missing = await load({ env, fetchImpl: stand_in(FULL), log: (line) => lines.push(line) });
  assert.deepEqual(missing, []);
  const exported = readFileSync(env.GITHUB_ENV, "utf8");
  const p12 = join(env.RUNNER_TEMP, "muster-release", "developer-id.p12");
  assert.equal(readFileSync(p12, "utf8"), "p12 bytes");
  assert.equal(statSync(p12).mode & 0o777, 0o600);
  assert.equal(statSync(join(env.RUNNER_TEMP, "muster-release", "asc-key.p8")).mode & 0o777, 0o600);
  assert.match(exported, /MUSTER_CERT_P12_PATH<</);
  assert.match(exported, /ASC_KEY_PATH<</);
  assert.match(exported, /MUSTER_SPARKLE_KEY_PATH<</);
  // The private keys travel as files only:  neither value is in the environment file or the log.
  assert.ok(!exported.includes("FAKE-P8-CONTENT"));
  assert.ok(!exported.includes(SEED));
  assert.ok(!lines.some((line) => line.includes("FAKE-P8-CONTENT") || line.includes(SEED)));
  assert.equal(readFileSync(join(env.RUNNER_TEMP, "muster-release", "sparkle-ed25519.key"), "utf8"), `${SEED}\n`);
  // A value that is exported is masked first.
  assert.ok(lines.includes(`::add-mask::${PASSWORD}`));
});

test("absent tokens are named, and the job is not failed for them", async () => {
  const env = environment({ INFISICAL_MUSTER_SPARKLE_READ_TOKEN: TOKENS.INFISICAL_MUSTER_SPARKLE_READ_TOKEN });
  const lines = [];
  await run({ env, fetchImpl: stand_in(FULL), log: (line) => lines.push(line) });
  const output = readFileSync(env.GITHUB_OUTPUT, "utf8");
  assert.equal(
    output,
    "missing=INFISICAL_MAC_CERTS_READ_TOKEN INFISICAL_FLEET_OPS_PROJECT_ID INFISICAL_ASC_READ_TOKEN\n",
  );
});

test("an empty name in a folder is reported as missing", async () => {
  const env = environment({ ...TOKENS, INFISICAL_FLEET_OPS_PROJECT_ID: FLEET_OPS });
  const partial = { ...FULL, "/muster-sparkle": {} };
  const missing = await load({ env, fetchImpl: stand_in(partial), log: () => {} });
  assert.deepEqual(missing, ["MUSTER_SPARKLE_ED_PRIVATE_KEY"]);
});

test("a present token with the wrong scope fails instead of reading as missing", async () => {
  const env = environment({ ...TOKENS, INFISICAL_FLEET_OPS_PROJECT_ID: FLEET_OPS });
  const wide = stand_in(FULL, { scopeFor: (folder) => ({ ...folder, path: "/" }) });
  await assert.rejects(load({ env, fetchImpl: wide, log: () => {} }));
});

test("an unresolved reference fails the job", async () => {
  const env = environment({ ...TOKENS, INFISICAL_FLEET_OPS_PROJECT_ID: FLEET_OPS });
  const referenced = { ...FULL, "/asc": { ...FULL["/asc"], ASC_KEY_ID: "${prod.asc.ASC_KEY_ID}" } };
  await assert.rejects(load({ env, fetchImpl: stand_in(referenced), log: () => {} }), /unresolved/);
});
