// node --test scripts/ios-fleet/asc_scoped_creds.test.mjs
// No network, no keychain, no fleet-secrets, no legacy key folder.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  API_BASE, ASC_NAMES, AscCredentialError, KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, LEGACY_ASC_ENV,
  PROJECT_ENV, SECRET_PATH, TOKEN_ENV, TOKEN_FILE_ENV, resolveFreshnessAscCredentials,
} from "./asc_scoped_creds.mjs";
import { EXIT_FAILED, main, makeDeps, parseArgs } from "./ios_testflight_freshness.mjs";

const PROJECT = "00000000-0000-4000-8000-000000000001";
const FIXTURE = "st.synthetic-freshness-read-token";
const CREDS = {
  ASC_KEY_ID: "TESTKEYID",
  ASC_ISSUER_ID: "00000000-0000-4000-8000-0000000000aa",
  ASC_KEY_P8: "fixture-p8-not-a-key",
};

function grant(over = {}) {
  return {
    id: "freshness-token",
    name: KEYCHAIN_ACCOUNT,
    workspace: PROJECT,
    permissions: ["read"],
    scopes: [{ environment: "prod", secretPath: SECRET_PATH }],
    expiresAt: "2099-01-01T00:00:00Z",
    ...over,
  };
}

function secretBody(name) {
  return {
    secret: {
      secretKey: name,
      secretValue: CREDS[name],
      secretPath: SECRET_PATH,
      workspace: PROJECT,
      environment: "prod",
      type: "shared",
      secretValueHidden: false,
    },
  };
}

function bindingEnv(extra = {}) {
  return {
    HOME: extra.HOME || os.tmpdir(),
    PATH: "/usr/bin:/bin",
    [PROJECT_ENV]: PROJECT,
    [TOKEN_ENV]: FIXTURE,
    ...extra,
  };
}

function fetchScript(metadata = grant(), secrets = CREDS) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, authorization: init?.headers?.authorization || "" });
    const parsed = new URL(url);
    if (parsed.pathname === "/api/v2/service-token") {
      return new Response(JSON.stringify(metadata), { status: 200 });
    }
    const name = decodeURIComponent(parsed.pathname.split("/").pop());
    if (!secrets[name]) return new Response("{}", { status: 404 });
    const query = parsed.searchParams;
    if (query.get("secretPath") !== SECRET_PATH || query.get("environment") !== "prod") {
      return new Response("{}", { status: 403 });
    }
    if (query.get("expandSecretReferences") !== "false" || query.get("recursive") !== "false") {
      return new Response("{}", { status: 400 });
    }
    return new Response(JSON.stringify(secretBody(name)), { status: 200 });
  };
  return { calls, fetchImpl };
}

function forbidLegacyIo() {
  const hits = [];
  const readFileImpl = (file) => {
    hits.push(["read", String(file)]);
    throw new Error(`unexpected file read ${file}`);
  };
  const execFileImpl = (cmd, args, _opts, cb) => {
    hits.push(["exec", cmd, args]);
    cb(Object.assign(new Error("unexpected command"), { code: "ENOENT" }));
  };
  return { hits, readFileImpl, execFileImpl };
}

test("a valid scoped binding is verified and its values are what asc-api.mjs receives", async () => {
  const { calls, fetchImpl } = fetchScript();
  const { hits, readFileImpl, execFileImpl } = forbidLegacyIo();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "asc-scoped-"));
  const script = path.join(dir, "asc-api.mjs");
  fs.writeFileSync(script, "// fixture\n");
  const env = bindingEnv({ HOME: dir, IOS_FLEET_DIR: dir, [TOKEN_ENV]: FIXTURE });
  const spawned = [];
  const deps = makeDeps(env, {
    fetchImpl,
    readFileImpl,
    execFileImpl,
    runCmd(_cmd, args, opts) {
      spawned.push({ args, env: opts.env });
      return Promise.resolve('{"data":[]}\n');
    },
  });
  const body = await deps.asc("/v1/apps?limit=1");
  assert.deepEqual(body, { data: [] });
  assert.deepEqual(calls.map((call) => new URL(call.url).pathname), [
    "/api/v2/service-token",
    "/api/v3/secrets/raw/ASC_KEY_ID",
    "/api/v3/secrets/raw/ASC_ISSUER_ID",
    "/api/v3/secrets/raw/ASC_KEY_P8",
  ]);
  assert.equal(calls.every((call) => call.url.startsWith(API_BASE)), true);
  assert.equal(calls.every((call) => call.authorization === `Bearer ${FIXTURE}`), true);
  assert.deepEqual(spawned[0].args, [script, "GET", "/v1/apps?limit=1"]);
  for (const name of ASC_NAMES) assert.equal(spawned[0].env[name], CREDS[name]);
  assert.equal(Object.hasOwn(spawned[0].env, TOKEN_ENV), false);
  assert.equal(Object.hasOwn(spawned[0].env, "INFISICAL_TOKEN"), false);
  assert.equal(hits.length, 0);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("a missing binding fails closed before asc-api.mjs and ignores legacy material", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "asc-missing-"));
  const legacyDir = path.join(dir, ".appstoreconnect", "private_keys");
  fs.mkdirSync(legacyDir, { recursive: true });
  const legacyKey = path.join(legacyDir, "AuthKey_TEST.p8");
  fs.writeFileSync(legacyKey, "fixture-legacy-key-material\n");
  const bin = path.join(dir, "bin");
  fs.mkdirSync(bin);
  const marker = path.join(dir, "fleet-secrets-was-run");
  fs.writeFileSync(path.join(bin, "fleet-secrets"), `#!/bin/sh\necho ran > ${JSON.stringify(marker)}\n`, { mode: 0o755 });
  const script = path.join(dir, "asc-api.mjs");
  fs.writeFileSync(script, "// fixture\n");
  const reads = [];
  const commands = [];
  const env = {
    HOME: dir,
    PATH: `${bin}:/usr/bin:/bin`,
    IOS_FLEET_DIR: dir,
    [PROJECT_ENV]: PROJECT,
  };
  const deps = makeDeps(env, {
    fetchImpl() {
      throw new Error("network used without a binding");
    },
    readFileImpl(file) {
      reads.push(String(file));
      throw new Error("file read");
    },
    execFileImpl(cmd, args, _opts, cb) {
      commands.push([cmd, ...(args || [])]);
      const err = new Error("not found");
      err.code = 44;
      cb(err, "", "The specified item could not be found in the keychain.");
    },
    runCmd() {
      throw new Error("asc-api.mjs was spawned");
    },
  });
  const out = { text: "", write(chunk) { this.text += chunk; } };
  const registry = path.join(dir, "apps.json");
  fs.writeFileSync(registry, JSON.stringify({
    owner: "Org",
    apps: { demo: { displayName: "Demo", appleId: "1", repo: "Demo", paths: ["ios"], compileCheck: "xcode", autoRemedy: false } },
  }));
  const code = await main(["--app", "demo", "--registry", registry], deps, out);
  assert.equal(code, EXIT_FAILED);
  assert.match(out.text, new RegExp(KEYCHAIN_ACCOUNT));
  assert.match(out.text, /does not fall back to fleet-secrets \/asc or a legacy key folder/);
  assert.equal(fs.existsSync(marker), false);
  assert.deepEqual(reads, []);
  assert.deepEqual(commands, [[
    "/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
  ]]);
  assert.equal(fs.readFileSync(legacyKey, "utf8"), "fixture-legacy-key-material\n");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("a token whose scope is not exactly prod:/asc read is refused before any secret read", async () => {
  const cases = [
    grant({ permissions: ["read", "write"] }),
    grant({ scopes: [{ environment: "prod", secretPath: "/" }] }),
    grant({ scopes: [{ environment: "dev", secretPath: SECRET_PATH }] }),
    grant({ workspace: "00000000-0000-4000-8000-000000000099" }),
    grant({ expiresAt: "2020-01-01T00:00:00Z" }),
  ];
  for (const metadata of cases) {
    const { calls, fetchImpl } = fetchScript(metadata);
    await assert.rejects(
      () => resolveFreshnessAscCredentials({
        env: bindingEnv(),
        fetchImpl,
        nowMs: Date.parse("2026-11-11T00:00:00Z"),
        readFileImpl() { throw new Error("file read"); },
        execFileImpl() { throw new Error("command"); },
      }),
      (err) => err instanceof AscCredentialError && /does not fall back/.test(err.message),
    );
    assert.deepEqual(calls.map((call) => new URL(call.url).pathname), ["/api/v2/service-token"]);
  }
});

test("the legacy folder and unscoped route stay unused unless the flag or env opts in", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "asc-legacy-opt-"));
  fs.writeFileSync(path.join(dir, "asc-api.mjs"), "// fixture\n");
  const base = { HOME: dir, PATH: "/usr/bin:/bin", IOS_FLEET_DIR: dir };
  const run = (env, options = {}) => {
    const spawned = [];
    let fetched = false;
    const deps = makeDeps(env, {
      ...options,
      fetchImpl() {
        fetched = true;
        throw new Error("scoped fetch");
      },
      execFileImpl() {
        throw new Error("keychain");
      },
      runCmd(_cmd, args, opts) {
        spawned.push({ args, env: opts.env });
        return Promise.resolve('{"data":[]}\n');
      },
    });
    return deps.asc("/v1/apps").then((body) => ({ body, spawned, fetched }));
  };
  await assert.rejects(() => run(base), /does not fall back|keychain/);
  for (const [env, options] of [
    [{ ...base, [LEGACY_ASC_ENV]: "1" }, {}],
    [base, { allowLegacyAsc: true }],
  ]) {
    const result = await run(env, options);
    assert.equal(result.fetched, false);
    assert.equal(result.spawned.length, 1);
    assert.equal(result.spawned[0].args[1], "GET");
    assert.equal(result.spawned[0].env, env);
  }
  assert.equal(parseArgs(["--allow-legacy-asc-creds"]).allowLegacyAsc, true);
  assert.equal(parseArgs([]).allowLegacyAsc, false);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("a group-readable token file is refused and its contents are not sent", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "asc-token-file-"));
  const file = path.join(dir, "token");
  fs.writeFileSync(file, `${FIXTURE}\n`, { mode: 0o644 });
  fs.chmodSync(file, 0o644);
  let fetched = false;
  await assert.rejects(
    () => resolveFreshnessAscCredentials({
      env: bindingEnv({ [TOKEN_ENV]: "", [TOKEN_FILE_ENV]: file, HOME: dir }),
      fetchImpl() {
        fetched = true;
        throw new Error("fetched");
      },
    }),
    /mode 0600 or 0400/,
  );
  assert.equal(fetched, false);
  fs.chmodSync(file, 0o600);
  const { calls, fetchImpl } = fetchScript();
  const creds = await resolveFreshnessAscCredentials({
    env: bindingEnv({ [TOKEN_ENV]: "", [TOKEN_FILE_ENV]: file, HOME: dir }),
    fetchImpl,
    execFileImpl() { throw new Error("keychain used beside a token file"); },
  });
  assert.equal(creds.ASC_KEY_ID, CREDS.ASC_KEY_ID);
  assert.equal(calls[0].authorization === `Bearer ${FIXTURE}`, true);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("the credential module does not spawn fleet-secrets itself", () => {
  const src = fs.readFileSync(new URL("./asc_scoped_creds.mjs", import.meta.url), "utf8");
  assert.equal(src.includes("execFile(") || src.includes("execFileImpl("), true);
  assert.equal(/["']fleet-secrets["']/.test(src), false);
  assert.equal(/private_keys|AuthKey_|\.appstoreconnect/.test(src), false);
});
