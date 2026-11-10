// Service-token metadata and returned provenance must be verified before CI
// credentials are exported.  Run: node --test scripts/infisical-fetch-expand.node-test.mjs
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  isUnresolvedReference,
  requireServiceToken,
  run,
  MetadataVerificationError,
} from "./infisical-fetch.mjs";

const REFERENCE = "${prod.global-api-keys.SENTINEL_KEY_ref_3c9e}";
const RESOLVED = "sentinel-resolved-value-5d1a";
const PROJECT = "00000000-0000-4000-8000-000000000042";

/** Stub both metadata and the list call; never contacts Infisical. */
function stubFetch(vaultSecrets, listUrls = [], { metadataPatch = {}, rawStatus = 200, metadataFailure = false } = {}) {
  return async (url) => {
    const u = String(url);
    if (u.endsWith("/api/v2/service-token")) {
      if (metadataFailure) throw new Error("fixture metadata refusal");
      const metadata = {
        id: "synthetic-token-id",
        name: "synthetic-ci-reader",
        projectId: PROJECT,
        workspace: PROJECT,
        scopes: [{ environment: "prod", secretPath: "/" }],
        permissions: ["read"],
        expiresAt: "2099-01-01T00:00:00Z",
        ...metadataPatch,
      };
      return new Response(JSON.stringify(metadata), { status: 200 });
    }
    if (u.includes("/api/v3/secrets/raw")) {
      listUrls.push(u);
      return new Response(JSON.stringify({
        secrets: Object.entries(vaultSecrets).map(([secretKey, secretValue]) => ({
          secretKey,
          secretValue,
          workspace: PROJECT,
          environment: "prod",
          secretPath: "/",
          type: "shared",
        })),
      }), { status: rawStatus });
    }
    throw new Error(`stubFetch: unexpected URL ${u}`);
  };
}

function baseEnv(githubEnv, names) {
  return {
    INFISICAL_NAMES: names,
    INFISICAL_PROJECT_ID: PROJECT,
    INFISICAL_TOKEN: "st.test-service-token-fixture-value",
    GITHUB_ENV: githubEnv,
  };
}

test("metadata is verified first and the secrets list disables expansion, recursion, and imports", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ops818-fetch-"));
  try {
    const listUrls = [];
    await run({ env: baseEnv(join(dir, "env"), "FOO"), fetchImpl: stubFetch({ FOO: RESOLVED }, listUrls), log: () => {} });
    assert.equal(listUrls.length, 1);
    const query = new URL(listUrls[0]).searchParams;
    assert.equal(query.get("expandSecretReferences"), "false");
    assert.equal(query.get("recursive"), "false");
    assert.equal(query.get("include_imports"), "false");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("missing and malformed service tokens fail before any request", async () => {
  for (const token of [undefined, "not-a-token", "st.short"]) {
    let requests = 0;
    await assert.rejects(
      run({ env: { INFISICAL_PROJECT_ID: "proj", INFISICAL_TOKEN: token }, fetchImpl: async () => { requests += 1; } }),
      /INFISICAL_TOKEN/,
    );
    assert.equal(requests, 0);
  }
});

test("the service token validator accepts only the documented token shape", () => {
  assert.equal(requireServiceToken("st.test-service-token-fixture-value"), "st.test-service-token-fixture-value");
  assert.throws(() => requireServiceToken("st.short"), /INFISICAL_TOKEN/);
});

test("a value that is still a bare reference fails closed and names the key only", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ops818-fetch-"));
  try {
    const envFile = join(dir, "env");
    const lines = [];
    await assert.rejects(
      run({
        env: baseEnv(envFile, "GOOD BAD"),
        fetchImpl: stubFetch({ GOOD: RESOLVED, BAD: REFERENCE }),
        log: (l) => lines.push(l),
      }),
      (err) => {
        assert.match(err.message, /BAD/);
        assert.ok(!err.message.includes("SENTINEL_KEY_ref_3c9e"), "error must not echo the reference");
        return true;
      },
    );
    const written = existsSync(envFile) ? readFileSync(envFile, "utf8") : "";
    assert.ok(!written.includes("SENTINEL_KEY_ref_3c9e"), "reference must never reach GITHUB_ENV");
    assert.ok(lines.every((l) => !l.includes("SENTINEL_KEY_ref_3c9e")), "reference must never reach the log");
    assert.ok(lines.some((l) => l.startsWith("::error::BAD ")), "the key is named in an ::error:: line");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a resolved value is written normally", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ops818-fetch-"));
  try {
    const envFile = join(dir, "env");
    await run({ env: baseEnv(envFile, "FOO"), fetchImpl: stubFetch({ FOO: RESOLVED }), log: () => {} });
    assert.ok(readFileSync(envFile, "utf8").includes(RESOLVED));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("metadata project, path, permission, and expiry mismatches fail closed before GH fallback", async () => {
  const dir = mkdtempSync(join(tmpdir(), "metadata-fail-"));
  try {
    const invalidMetadata = [
      { projectId: "wrong-project" },
      { projectId: null },
      { scopes: [{ environment: "prod", secretPath: "/other" }] },
      { permissions: ["read", "write"] },
      { expiresAt: "2000-01-01T00:00:00Z" },
      { expiresAt: "2099-01-01T00:00:00" },
      { expiresAt: "invalid" },
      { expiresAt: 123 },
      { workspace: "wrong-project" },
    ];
    for (let index = 0; index < invalidMetadata.length; index += 1) {
      const envFile = join(dir, `env-${index}`);
      await assert.rejects(
        run({
          env: { ...baseEnv(envFile, "FOO"), GH_FALLBACK_FOO: RESOLVED },
          fetchImpl: stubFetch({ FOO: RESOLVED }, [], { metadataPatch: invalidMetadata[index] }),
          log: () => {},
        }),
        (error) => error instanceof MetadataVerificationError,
      );
      assert.equal(existsSync(envFile), false);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("workspace-only grants allow null or absent expiry while contradictory projectId stays fatal", async () => {
  const dir = mkdtempSync(join(tmpdir(), "metadata-compatible-"));
  try {
    for (const expiresAt of [null, undefined]) {
      const envFile = join(dir, expiresAt === null ? "null-expiry" : "absent-expiry");
      const listUrls = [];
      await run({
        env: baseEnv(envFile, "FOO"),
        fetchImpl: stubFetch({ FOO: RESOLVED }, listUrls, { metadataPatch: { projectId: undefined, expiresAt } }),
        log: () => {},
      });
      assert.equal(listUrls.length, 1);
      assert.ok(readFileSync(envFile, "utf8").includes(RESOLVED));
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("metadata endpoint failure is fatal, while an ordinary raw-list outage may use GH fallback", async () => {
  const dir = mkdtempSync(join(tmpdir(), "metadata-outage-"));
  try {
    const envFile = join(dir, "env");
    await assert.rejects(
      run({
        env: { ...baseEnv(envFile, "FOO"), GH_FALLBACK_FOO: RESOLVED },
        fetchImpl: stubFetch({}, [], { metadataFailure: true }),
        log: () => {},
      }),
      (error) => error instanceof MetadataVerificationError,
    );
    assert.equal(existsSync(envFile), false);

    await run({
      env: { ...baseEnv(envFile, "FOO"), GH_FALLBACK_FOO: RESOLVED },
      fetchImpl: stubFetch({}, [], { rawStatus: 503 }),
      log: () => {},
    });
    assert.ok(readFileSync(envFile, "utf8").includes(RESOLVED));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("record provenance mismatch fails closed rather than exporting the value", async () => {
  const dir = mkdtempSync(join(tmpdir(), "provenance-fail-"));
  try {
    const envFile = join(dir, "env");
    const fetchImpl = async (url) => {
      if (String(url).endsWith("/api/v2/service-token")) {
        return new Response(JSON.stringify({
          id: "synthetic-token-id", name: "synthetic-ci-reader", projectId: PROJECT, workspace: PROJECT,
          scopes: [{ environment: "prod", secretPath: "/" }], permissions: ["read"], expiresAt: "2099-01-01T00:00:00Z",
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ secrets: [{ secretKey: "FOO", secretValue: RESOLVED,
        workspace: "wrong-project", environment: "prod", secretPath: "/", type: "shared" }] }), { status: 200 });
    };
    await assert.rejects(
      run({ env: { ...baseEnv(envFile, "FOO"), GH_FALLBACK_FOO: "" }, fetchImpl, log: () => {} }),
      /unverified records/,
    );
    assert.equal(existsSync(envFile), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("record provenance fields may be absent when token metadata proves the exact scope", async () => {
  const dir = mkdtempSync(join(tmpdir(), "provenance-optional-"));
  try {
    const envFile = join(dir, "env");
    const fetchImpl = async (url) => {
      if (String(url).endsWith("/api/v2/service-token")) {
        return new Response(JSON.stringify({
          id: "synthetic-token-id", name: "synthetic-ci-reader", projectId: PROJECT, workspace: PROJECT,
          scopes: [{ environment: "prod", secretPath: "/" }], permissions: ["read"], expiresAt: "2099-01-01T00:00:00Z",
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ secrets: [{ secretKey: "FOO", secretValue: RESOLVED, type: "shared" }] }),
        { status: 200 });
    };
    await run({ env: baseEnv(envFile, "FOO"), fetchImpl, log: () => {} });
    assert.ok(readFileSync(envFile, "utf8").includes(RESOLVED));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("isUnresolvedReference catches unresolved references anywhere in a value", () => {
  assert.equal(isUnresolvedReference("${prod.global-api-keys.NAME}"), true);
  assert.equal(isUnresolvedReference("  ${prod.a.B}\n"), true);
  assert.equal(isUnresolvedReference("prefix-${prod.a.B}"), true);
  assert.equal(isUnresolvedReference("${prod.a.B}-suffix"), true);
  assert.equal(isUnresolvedReference("plain"), false);
  assert.equal(isUnresolvedReference(""), false);
  assert.equal(isUnresolvedReference(undefined), false);
});
