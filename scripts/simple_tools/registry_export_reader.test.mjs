import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { mkdtemp, mkdir, writeFile, readFile, rm, chmod, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { randomBytes, randomUUID } from "node:crypto";
import { loadRegistryServiceAccount } from "./registry_export_reader.mjs";

const SA = { client_email: "synthetic@example.invalid", private_key: "synthetic-private-key" };
const fixedFailure = /Registry Google service account unavailable/;

async function fixture(t, program, { installed = false } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "registry-reader-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const scripts = installed ? undefined : join(directory, "scripts");
  const modules = installed ? join(directory, "operator-sheets-drain") : join(scripts, "simple_tools");
  await mkdir(modules, { recursive: true });
  await writeFile(join(modules, "registry_export_reader.mjs"), await readFile(new URL("./registry_export_reader.mjs", import.meta.url)));
  const worker = installed ? join(modules, "fleet_infisical.py") : join(scripts, "fleet_infisical.py");
  if (program) await writeFile(worker, program(directory));
  const adapter = await import(pathToFileURL(join(modules, "registry_export_reader.mjs")));
  const tokenFile = join(directory, "own-bootstrap");
  const token = "st." + randomBytes(32).toString("hex") + ".synthetic";
  await writeFile(tokenFile, token, { mode: 0o600 });
  return {
    directory, scripts, modules, worker, adapter, token,
    env: { HOME: directory, OPERATOR_SHEETS_TOKEN_FILE: tokenFile, INFISICAL_FLEET_OPS_PROJECT_ID: randomUUID() },
  };
}

test("flat installed adapter resolves its private colocated worker without a shared parent helper", async (t) => {
  const f = await fixture(t, (directory) => {
    const reply = { schema: 1, ok: true, values: { GOOGLE_SHEETS_SA: JSON.stringify(SA) } };
    const marker = join(directory, "worker-path");
    return `import sys\nfrom pathlib import Path\nsys.stdin.buffer.read()\nPath(${JSON.stringify(marker)}).write_text(__file__)\nsys.stdout.write(${JSON.stringify(JSON.stringify(reply))})\n`;
  }, { installed: true });
  assert.deepEqual(await f.adapter.loadRegistryServiceAccount({ env: f.env }), SA);
  assert.equal(await readFile(join(f.directory, "worker-path"), "utf8"), f.worker);
});

test("flat installed six-file closure imports the drain and thread exporter and runs its private worker", async (t) => {
  const f = await fixture(t, (directory) => {
    const reply = { schema: 1, ok: true, values: { GOOGLE_SHEETS_SA: JSON.stringify(SA) } };
    return `import sys\nfrom pathlib import Path\nsys.stdin.buffer.read()\nPath(${JSON.stringify(join(directory, "worker-path"))}).write_text(__file__)\nsys.stdout.write(${JSON.stringify(JSON.stringify(reply))})\n`;
  }, { installed: true });
  const lib = f.modules;
  for (const name of [
    "operator_sheets_export.mjs",
    "operator_sheets_google.mjs",
    "operator_sheets_drain.mjs",
    "thread_registry_export.mjs",
  ]) {
    await writeFile(join(lib, name), await readFile(new URL(`./${name}`, import.meta.url)));
  }
  await import(pathToFileURL(join(lib, "operator_sheets_drain.mjs")));
  const threadModule = await import(pathToFileURL(join(lib, "thread_registry_export.mjs")));
  assert.equal(threadModule.THREAD_HEADERS[0], "Thread Key");
  assert.deepEqual(await f.adapter.loadRegistryServiceAccount({ env: f.env }), SA);
  assert.equal(await readFile(join(f.directory, "worker-path"), "utf8"), f.worker);
});

test("a present unsafe colocated worker refuses instead of falling back to the repo helper", async (t) => {
  const f = await fixture(t, (directory) => {
    const reply = { schema: 1, ok: true, values: { GOOGLE_SHEETS_SA: JSON.stringify(SA) } };
    return `import sys\nfrom pathlib import Path\nsys.stdin.buffer.read()\nPath(${JSON.stringify(join(directory, "worker-path"))}).write_text(__file__)\nsys.stdout.write(${JSON.stringify(JSON.stringify(reply))})\n`;
  });
  await symlink(f.worker, join(f.modules, "fleet_infisical.py"));
  await assert.rejects(f.adapter.loadRegistryServiceAccount({ env: f.env }), fixedFailure);
  await assert.rejects(readFile(join(f.directory, "worker-path")), { code: "ENOENT" });
});

test("synced SA transport keeps precedence and malformed JSON never falls back or reflects content", async () => {
  assert.deepEqual(await loadRegistryServiceAccount({ env: { GOOGLE_SHEETS_SA: JSON.stringify(SA) } }), SA);
  await assert.rejects(loadRegistryServiceAccount({ env: { GOOGLE_SHEETS_SA: "private-fixture{" } }), (error) => {
    assert.match(error.message, fixedFailure);
    assert.doesNotMatch(error.message, /private-fixture/);
    return true;
  });
});

test("missing explicit own bootstrap does not use ambient auth or an external executable", async () => {
  await assert.rejects(loadRegistryServiceAccount({ env: {
    HOME: tmpdir(), INFISICAL_TOKEN: "synthetic-ambient", FLEET_SECRETS: "/must-not-run",
  } }), fixedFailure);
});

test("copied adapter and actual isolated canonical worker verify provider scope, expiry and strict rows", async (t) => {
  const f = await fixture(t);
  const calls = [];
  let expiry = "2099-01-01T00:00:00Z";
  let hidden = false;
  const server = createServer((request, response) => {
    calls.push(request.url);
    assert.equal(request.headers.authorization, "Bearer " + f.token);
    const metadata = request.url.endsWith("/service-token");
    const payload = metadata ? {
      workspace: f.env.INFISICAL_FLEET_OPS_PROJECT_ID, permissions: ["read"],
      scopes: [{ environment: "prod", secretPath: "/google-sheets-sa" }], expiresAt: expiry,
    } : { secret: {
      secretKey: "GOOGLE_SHEETS_SA", secretValue: JSON.stringify(SA), type: "shared",
      workspace: f.env.INFISICAL_FLEET_OPS_PROJECT_ID, environment: "prod",
      secretPath: "/google-sheets-sa", secretValueHidden: hidden,
    } };
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify(payload));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => server.close(resolve)));
  let canonical = await readFile(new URL("../fleet_infisical.py", import.meta.url), "utf8");
  // Only the copied test artifact changes its provider constant. Production has no API override.
  canonical = canonical.replace('API_BASE = "https://app.infisical.com/api"', `API_BASE = "http://127.0.0.1:${server.address().port}/api"`);
  // A child or auth-environment dependency inside the actual worker fails this copied smoke.
  canonical = canonical.replace('if __name__ == "__main__":', `if __name__ == "__main__":
    assert set(os.environ) <= {"PATH", "LC_CTYPE"}
    def forbidden_child(*args, **kwargs):
        raise AssertionError("grandchild forbidden")
    subprocess.Popen = forbidden_child`);
  await writeFile(f.worker, canonical);
  // The thread exporter's existing scanner remains in the copied import closure.
  await mkdir(join(f.directory, "scripts", "agent_sync"));
  for (const relative of ["simple_tools/thread_registry_export.mjs", "simple_tools/operator_sheets_google.mjs", "agent_sync/session_registry_export.mjs"]) {
    await writeFile(join(f.directory, "scripts", relative), await readFile(new URL("../" + relative, import.meta.url)));
  }
  const threadExporter = await import(pathToFileURL(join(f.directory, "scripts", "simple_tools", "thread_registry_export.mjs")));
  const sessionExporter = await import(pathToFileURL(join(f.directory, "scripts", "agent_sync", "session_registry_export.mjs")));
  assert.equal(threadExporter.THREAD_HEADERS[0], "Thread Key");
  assert.equal(sessionExporter.SESSION_HEADERS[0], "Record ID");
  f.env.INFISICAL_TOKEN = "must-not-flow";
  f.env.FLEET_SECRETS_API = "http://127.0.0.1:1/api";
  f.env.PYTHONPATH = "/must-not-load";
  assert.deepEqual(await f.adapter.loadRegistryServiceAccount({ env: f.env }), SA);
  assert.equal(calls.length, 2);
  assert.match(calls[1], /raw\/GOOGLE_SHEETS_SA\?/);
  expiry = null;
  await assert.rejects(f.adapter.loadRegistryServiceAccount({ env: f.env }), fixedFailure);
  assert.equal(calls.length, 3, "null expiry refuses before the named secret");
  expiry = "2099-01-01T00:00:00Z";
  hidden = true;
  await assert.rejects(f.adapter.loadRegistryServiceAccount({ env: f.env }), fixedFailure);
  await chmod(f.env.OPERATOR_SHEETS_TOKEN_FILE, 0o644);
  const before = calls.length;
  await assert.rejects(f.adapter.loadRegistryServiceAccount({ env: f.env }), fixedFailure);
  assert.equal(calls.length, before, "unsafe bootstrap refuses before provider access");
});

test("worker reply must pair exact schema and requested value with exit zero", async (t) => {
  for (const [reply, code] of [
    [{ schema: 1, ok: true, values: { GOOGLE_SHEETS_SA: JSON.stringify(SA) } }, 4],
    [{ schema: true, ok: true, values: { GOOGLE_SHEETS_SA: JSON.stringify(SA) } }, 0],
    [{ schema: 1, ok: true, values: { OTHER: JSON.stringify(SA) } }, 0],
    [{ schema: 1, ok: true, values: { GOOGLE_SHEETS_SA: JSON.stringify(SA) }, extra: true }, 0],
    [{ schema: 1, ok: false, reason: "private-fixture" }, 4],
  ]) {
    const f = await fixture(t, () => `import sys\nsys.stdin.buffer.read()\nsys.stdout.write(${JSON.stringify(JSON.stringify(reply))})\nsys.exit(${code})\n`);
    await assert.rejects(f.adapter.loadRegistryServiceAccount({ env: f.env }), (error) => {
      assert.match(error.message, fixedFailure);
      assert.doesNotMatch(error.message, /private-fixture/);
      return true;
    });
  }
});

test("abort and output overflow kill and observe closure of the one direct child", async (t) => {
  for (const overflow of [false, true]) {
    const f = await fixture(t, (directory) => `import os,sys,time\nfrom pathlib import Path\nPath(${JSON.stringify(join(directory, "pid"))}).write_text(str(os.getpid()))\nsys.stdin.buffer.read()\n${overflow ? 'sys.stdout.buffer.write(b"x" * 70000);sys.stdout.buffer.flush()' : ""}\ntime.sleep(60)\n`);
    const controller = new AbortController();
    const started = performance.now();
    const outcome = f.adapter.loadRegistryServiceAccount({ env: f.env, signal: controller.signal });
    // Attach the rejection handler before waiting for the synthetic worker's readiness.
    const refused = assert.rejects(outcome, fixedFailure);
    let pid;
    for (let tries = 0; tries < 100; tries++) {
      try { pid = Number(await readFile(join(f.directory, "pid"), "utf8")); break; } catch {}
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    assert.ok(pid, "synthetic child started");
    if (!overflow) controller.abort();
    await refused;
    assert.ok(performance.now() - started < 3000);
    assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
  }
});

test("the actual thirty-second parent budget reaps a worker that never replies", { timeout: 35_000 }, async (t) => {
  const f = await fixture(t, (directory) => `import os,sys,time\nfrom pathlib import Path\nPath(${JSON.stringify(join(directory, "pid"))}).write_text(str(os.getpid()))\nsys.stdin.buffer.read()\ntime.sleep(60)\n`);
  const started = performance.now();
  await assert.rejects(f.adapter.loadRegistryServiceAccount({ env: f.env }), fixedFailure);
  const elapsed = performance.now() - started;
  assert.ok(elapsed >= 28_500 && elapsed < 31_000, `elapsed ${elapsed}`);
  const pid = Number(await readFile(join(f.directory, "pid"), "utf8"));
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
});
