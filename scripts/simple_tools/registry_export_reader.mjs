// One credential seam for both registry exporters. Only the Python child opens the bootstrap.
import { spawn } from "node:child_process";
import { lstatSync } from "node:fs";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const WORKER = fileURLToPath(new URL("../fleet_infisical.py", import.meta.url));
const BUDGET_MS = 30_000;
const CLEANUP_MS = 1_000;
const MAX_OUTPUT = 64 * 1024;
const FAILURE = "Registry Google service account unavailable; check GOOGLE_SHEETS_SA or the own OPERATOR_SHEETS_TOKEN_FILE binding";
const fail = () => new Error(FAILURE);

function serviceAccount(raw) {
  if (typeof raw !== "string" || !raw || Buffer.byteLength(raw) > MAX_OUTPUT) throw fail();
  let value;
  try { value = JSON.parse(raw); } catch { throw fail(); }
  if (!value || Array.isArray(value) || typeof value !== "object" ||
      typeof value.client_email !== "string" || !value.client_email ||
      typeof value.private_key !== "string" || !value.private_key) throw fail();
  return value;
}

export async function loadRegistryServiceAccount({ env = process.env, signal } = {}) {
  // A supplied, synced SA keeps its existing precedence; a malformed one never falls back.
  if (env.GOOGLE_SHEETS_SA) return serviceAccount(env.GOOGLE_SHEETS_SA);
  const started = performance.now();
  if (typeof env.HOME !== "string" || !isAbsolute(env.HOME) ||
      typeof env.OPERATOR_SHEETS_TOKEN_FILE !== "string" ||
      !isAbsolute(env.OPERATOR_SHEETS_TOKEN_FILE) || signal?.aborted) throw fail();
  const metadata = { HOME: env.HOME, INFISICAL_ENVIRONMENT: "prod" };
  for (const key of ["INFISICAL_FLEET_OPS_PROJECT_ID", "INFISICAL_ENVIRONMENT", "INFISICAL_ENV"]) {
    if (key in env) {
      if (typeof env[key] !== "string") throw fail();
      metadata[key] = env[key];
    }
  }
  const request = Buffer.from(JSON.stringify({
    schema: 1, operation: "file-named-fetch", project_slug: "fleet-ops",
    path: "/google-sheets-sa", env: metadata, token_file: env.OPERATOR_SHEETS_TOKEN_FILE,
    names: ["GOOGLE_SHEETS_SA"], timeout: 29, max_output_bytes: MAX_OUTPUT,
  }));
  if (request.length > 128 * 1024) throw fail();
  try {
    const info = lstatSync(WORKER);
    if (!info.isFile() || info.isSymbolicLink()) throw fail();
  } catch { throw fail(); }
  const raw = await new Promise((resolve, reject) => {
    let child;
    let output = [];
    let bytes = 0;
    let failed = false;
    let settled = false;
    let operationTimer;
    let cleanupTimer;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(operationTimer);
      clearTimeout(cleanupTimer);
      signal?.removeEventListener("abort", stop);
      output = [];
      request.fill(0);
      if (error) reject(fail()); else resolve(value);
    };
    const stop = () => {
      if (failed || settled) return;
      failed = true;
      output = [];
      if (child) {
        child.stdin.destroy();
        try { child.kill("SIGKILL"); } catch { /* A closed child is handled by close. */ }
      }
      const remaining = Math.max(1, Math.min(CLEANUP_MS, BUDGET_MS - (performance.now() - started)));
      cleanupTimer = setTimeout(() => {
        // A fixed failure acknowledges that the OS did not confirm closure in the budget.
        child?.stdout.destroy();
        finish(true);
      }, remaining);
    };
    try {
      child = spawn("/usr/bin/python3", ["-I", "-B", WORKER, "--bounded-read-worker"], {
        shell: false, env: { PATH: "/usr/bin:/bin" }, stdio: ["pipe", "pipe", "ignore"],
      });
      const remaining = BUDGET_MS - CLEANUP_MS - (performance.now() - started);
      if (remaining <= 0) stop();
      else operationTimer = setTimeout(stop, remaining);
      signal?.addEventListener("abort", stop, { once: true });
      if (signal?.aborted) stop();
      child.on("error", stop);
      child.stdin.on("error", stop);
      child.stdout.on("error", stop);
      child.stdout.on("data", (chunk) => {
        if (failed) return;
        bytes += chunk.length;
        if (bytes > MAX_OUTPUT) stop(); else output.push(chunk);
      });
      child.on("close", (code, exitSignal) => {
        if (failed || exitSignal || performance.now() - started >= BUDGET_MS) return finish(true);
        let reply;
        try { reply = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(output))); } catch { return finish(true); }
        if (code !== 0 || !reply || Array.isArray(reply) ||
            Object.keys(reply).sort().join() !== "ok,schema,values" || reply.schema !== 1 || reply.ok !== true ||
            !reply.values || Array.isArray(reply.values) || typeof reply.values !== "object" ||
            Object.keys(reply.values).join() !== "GOOGLE_SHEETS_SA" ||
            typeof reply.values.GOOGLE_SHEETS_SA !== "string" || !reply.values.GOOGLE_SHEETS_SA) return finish(true);
        finish(false, reply.values.GOOGLE_SHEETS_SA);
      });
      if (!failed) child.stdin.end(request);
    } catch {
      if (child) stop(); else finish(true);
    }
  });
  const value = serviceAccount(raw);
  if (performance.now() - started >= BUDGET_MS) throw fail();
  return value;
}
