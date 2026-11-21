import assert from "node:assert/strict";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import os from "node:os";
import { join } from "node:path";
import test from "node:test";
import { loadServiceAccount } from "./fleet-apps-sheet-sync.mjs";

const fixtureCredential = "st.test-only-grok-key";
const project = randomUUID();
const serviceAccount = '{"client_email":"synthetic@example.invalid"}';
let server;
let api;
let requests;
let oldEnv;
let oldCwd;
let hostile;

test.before(async () => {
  requests = [];
  server = createServer((req, res) => {
    const send = (status, body) => {
      const raw = Buffer.from(JSON.stringify(body));
      res.writeHead(status, { "content-type": "application/json", "content-length": raw.length });
      res.end(raw);
    };
    if (req.url === "/api/v2/service-token") {
      if (req.headers.authorization !== `Bearer ${fixtureCredential}`) return send(401, {});
      return send(200, {
        workspace: project,
        permissions: ["read"],
        scopes: [{ environment: "prod", secretPath: "/google-sheets-sa" }],
        expiresAt: "2099-01-01T00:00:00Z",
      });
    }
    const requestUrl = new URL(req.url, "http://127.0.0.1");
    const name = decodeURIComponent(requestUrl.pathname.split("/").at(-1));
    requests.push(`${name}:${requestUrl.searchParams.get("secretPath")}`);
    if (req.headers.authorization !== `Bearer ${fixtureCredential}` || name !== "GOOGLE_SHEETS_SA") {
      return send(404, {});
    }
    return send(200, {
      secret: {
        secretKey: name,
        secretValue: serviceAccount,
        secretValueHidden: false,
        type: "shared",
        workspace: project,
        environment: "prod",
        secretPath: "/google-sheets-sa",
      },
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  api = `http://127.0.0.1:${server.address().port}/api`;
  oldEnv = { ...process.env };
  oldCwd = process.cwd();
  hostile = mkdtempSync(join(os.tmpdir(), "sheet-loader-hostile-"));
  writeFileSync(join(hostile, "fleet_infisical.py"), "raise AssertionError('foreign client')\n");
  writeFileSync(join(hostile, "fleet_infisical_cli.py"), "raise AssertionError('foreign CLI')\n");
  process.chdir(hostile);
  delete process.env.GOOGLE_SHEETS_SA;
  Object.assign(process.env, {
    INFISICAL_TOKEN: fixtureCredential,
    INFISICAL_FLEET_OPS_PROJECT_ID: project,
    INFISICAL_ENVIRONMENT: "prod",
    INFISICAL_READ_PATHS: "/google-sheets-sa",
    FLEET_SECRETS_API: api,
    PYTHONPATH: hostile,
  });
});

test.after(async () => {
  process.chdir(oldCwd);
  for (const name of Object.keys(process.env)) {
    if (!(name in oldEnv)) delete process.env[name];
  }
  Object.assign(process.env, oldEnv);
  rmSync(hostile, { recursive: true, force: true });
  await new Promise((resolve) => server.close(resolve));
});

function runLoader(env) {
  const entry = new URL("./fleet-apps-sheet-sync.mjs", import.meta.url).href;
  const source = `const { loadServiceAccount } = await import(${JSON.stringify(entry)}); try { console.log(JSON.stringify(loadServiceAccount())); } catch (error) { console.log(JSON.stringify({ error: error.message })); }`;
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["--input-type=module", "-e", source], {
      cwd: hostile,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk) => (stdout += chunk));
    child.stderr.setEncoding("utf8").on("data", (chunk) => (stderr += chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) reject(new Error(`sheet loader child failed (${code}): ${stderr}`));
      else resolve(JSON.parse(stdout.trim()));
    });
  });
}

test("the sheet loader runs the isolated canonical CLI from the source path", async () => {
  const childEnv = { ...process.env };
  delete childEnv.GOOGLE_SHEETS_SA;
  const account = await runLoader(childEnv);
  assert.deepEqual(account, { client_email: "synthetic@example.invalid" });
  assert.deepEqual(requests, ["GOOGLE_SHEETS_SA:/google-sheets-sa"]);
});

test("an explicit environment value still wins without a provider call", () => {
  process.env.GOOGLE_SHEETS_SA = '{"client_email":"environment@example.invalid"}';
  const before = requests.length;
  assert.deepEqual(loadServiceAccount(() => assert.fail("fallback should not run")), {
    client_email: "environment@example.invalid",
  });
  assert.equal(requests.length, before);
  delete process.env.GOOGLE_SHEETS_SA;
});

test("missing or invalid service-token bindings return no credential value", async () => {
  for (const value of [undefined, "st.invalid-test-binding"]) {
    const childEnv = { ...process.env };
    if (value === undefined) delete childEnv.INFISICAL_TOKEN;
    else childEnv.INFISICAL_TOKEN = value;
    const result = await runLoader(childEnv);
    assert.match(result.error, /Google service account missing/);
    assert.equal(JSON.stringify(result).includes("invalid-test-binding"), false);
  }
  assert.equal(requests.length, 1);
});
