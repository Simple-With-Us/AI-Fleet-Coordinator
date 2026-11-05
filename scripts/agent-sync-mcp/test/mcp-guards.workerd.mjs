// The request guards and the audit buffer on the real runtime (AFC-617).  Node
// cannot prove these:  timers after a response, stubs across requests and the Rate
// Limiting bindings exist only in workerd.
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { testEnv, wranglerConfig } from "./helpers.mjs";

const HOST = "https://agents.simplewithus.com";
const dir = mkdtempSync(path.join(tmpdir(), "mcp-guards-"));
const out = path.join(dir, "worker.mjs");
await build({ entryPoints: ["src/index.js"], bundle: true, format: "esm", platform: "browser", conditions: ["workerd", "worker", "browser"], external: ["cloudflare:workers", "node:*"], outfile: out, logLevel: "error" });
const script = readFileSync(out, "utf8");
const w = wranglerConfig();

const outbound = { count: 0 };
function start(bindings = {}, ratelimits = {}) {
  return new Miniflare(convertV4MiniflareOptions({
    upstream: HOST,
    workers: [{
      name: "guards",
      modules: true,
      script,
      compatibilityDate: w.compatibility_date,
      compatibilityFlags: w.compatibility_flags,
      kvNamespaces: ["OAUTH_KV"],
      durableObjects: { SEAT_GATE: { className: "SeatGate", useSQLite: true } },
      ratelimits,
      bindings: testEnv({ HOSTED_SEATS: "JET,GROK-WEB,CLAUDE", EVENTS_ONLY_SEATS: "JET", ZULIP_CLAUDE_API_KEY: "z".repeat(32), INFISICAL_MODE: "off", ...bindings }),
      outboundService: () => {
        outbound.count += 1;
        return new Response("absent", { status: 404 });
      },
    }],
  }));
}

const tokenFor = () => `sbt_claude_${randomBytes(32).toString("base64url")}`;
const hashOf = (token) => createHash("sha256").update(token).digest("hex");

async function mint(mf) {
  const token = tokenFor();
  const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "guards");
  const gate = ns.get(ns.idFromName("CLAUDE"));
  await gate.gbBearerMint({ hash: hashOf(token), by: "test", expiresAt: Date.now() + 600_000, mcpAccept: true });
  return { token, gate, logGate: ns.get(ns.idFromName("__refusal-log__")) };
}

let id = 0;
async function post(mf, token, frame, headers = {}) {
  return mf.dispatchFetch(`${HOST}/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "CF-Connecting-IP": "203.0.113.9", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers },
    body: JSON.stringify({ jsonrpc: "2.0", ...frame }),
  });
}
const rpc = (mf, token, method, params = {}, headers) => post(mf, token, { id: ++id, method, params }, headers);
const notify = (mf, token) => post(mf, token, { method: "notifications/initialized" });
const sseJson = (text) => JSON.parse(text.split("\n").find((line) => line.startsWith("data:")).slice(5));

let passed = 0;
async function step(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`ok - ${name}`);
  } catch (error) {
    console.log(`not ok - ${name}`);
    throw error;
  }
}

try {
  await step("an audit row lands with no further traffic, flushed by the invocation that buffered it", async () => {
    const mf = start();
    try {
      const { token, gate } = await mint(mf);
      const res = await rpc(mf, token, "tools/call", { name: "identities", arguments: {} });
      assert.equal(res.status, 200);
      assert.equal(sseJson(await res.text()).result.isError, undefined);
      const deadline = Date.now() + 9000;
      let tail = [];
      while (Date.now() < deadline && tail.length === 0) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        tail = await gate.callTail(10);
      }
      assert.equal(tail.length, 1, "the trailing timer must flush after the response");
      assert.equal(tail[0].tool, "identities");
    } finally {
      await mf.dispose();
    }
  });

  await step("rows buffered across requests flush with a live stub, not a dead request's", async () => {
    const mf = start();
    try {
      const { token, gate } = await mint(mf);
      for (let i = 0; i < 3; i++) {
        const res = await rpc(mf, token, "tools/call", { name: "identities", arguments: {} });
        await res.text();
      }
      const deadline = Date.now() + 9000;
      let tail = [];
      while (Date.now() < deadline && tail.length < 3) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        tail = await gate.callTail(10);
      }
      assert.equal(tail.length, 3);
    } finally {
      await mf.dispose();
    }
  });

  await step("lifecycle frames need no secret:  no outbound call in required mode", async () => {
    const mf = start({ INFISICAL_MODE: "required", INFISICAL_SERVICE_TOKEN: "fixture", INFISICAL_WORKSPACE_ID: "fixture", INFISICAL_BASE_URL: "https://infisical-fixture.example" });
    try {
      const { token } = await mint(mf);
      outbound.count = 0;
      const init = await rpc(mf, token, "initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "1" } });
      assert.equal(init.status, 200);
      assert.equal(sseJson(await init.text()).result.serverInfo.name, "ST-MCP");
      assert.equal((await notify(mf, token)).status, 202);
      const ping = await rpc(mf, token, "ping", {}, { "MCP-Protocol-Version": "2025-06-18" });
      assert.deepEqual(sseJson(await ping.text()).result, {});
      const list = await rpc(mf, token, "tools/list", {}, { "MCP-Protocol-Version": "2025-06-18" });
      assert.ok(sseJson(await list.text()).result.tools.some((tool) => tool.name === "identities"));
      assert.equal(outbound.count, 0, "nothing left the Worker");
    } finally {
      await mf.dispose();
    }
  });

  await step("a frame not answered early reaches the SDK on the rebuilt request, Origin check included", async () => {
    const mf = start();
    try {
      const { token } = await mint(mf);
      const res = await rpc(mf, token, "tools/call", { name: "guide", arguments: {} });
      assert.equal(res.status, 200);
      assert.equal(sseJson(await res.text()).result.isError, undefined);
      assert.equal((await rpc(mf, token, "tools/list", {}, { Origin: "https://evil.example" })).status, 403);
    } finally {
      await mf.dispose();
    }
  });

  await step("the /mcp limiter counts notifications and handshakes, refuses with Retry-After, and is per credential", async () => {
    const mf = start({}, { MCP_RATE_LIMITER: { namespace_id: "1", simple: { limit: 3, period: 60 } } });
    try {
      const { token } = await mint(mf);
      const other = await mint(mf); // a re-mint:  `token` is now a wrong bearer for the gate
      const statuses = [];
      for (let i = 0; i < 6; i++) statuses.push((await notify(mf, other.token)).status);
      assert.deepEqual(statuses.slice(0, 3), [202, 202, 202]);
      const limited = await notify(mf, other.token);
      assert.equal(limited.status, 429);
      assert.equal(limited.headers.get("retry-after"), "60");
      assert.equal((await limited.json()).error.code, -32000);
      assert.equal((await notify(mf, token)).status, 401, "another credential has its own count");
      const rows = await other.logGate.diagnosticTail(50);
      assert.equal(JSON.stringify(rows).includes("mcp_http_429"), false, "a limited request writes no row");
    } finally {
      await mf.dispose();
    }
  });

  await step("failed auth:  a repeated wrong bearer costs no gate call, and past the IP limit nothing is written", async () => {
    const mf = start({}, { AUTH_FAIL_RATE_LIMITER: { namespace_id: "2", simple: { limit: 2, period: 60 } } });
    try {
      const { token: good, logGate } = await mint(mf);
      assert.equal((await rpc(mf, good, "ping")).status, 200, "this credential is verified in the isolate");
      const statuses = [];
      for (let i = 0; i < 6; i++) statuses.push((await rpc(mf, tokenFor(), "ping")).status);
      assert.ok(statuses.includes(429), `blocked: ${statuses}`);
      const blocked = await rpc(mf, tokenFor(), "ping");
      assert.equal(blocked.status, 429);
      assert.equal(blocked.headers.get("retry-after"), "60");
      const firstRows = await logGate.diagnosticTail(50);
      for (let i = 0; i < 5; i++) await (await rpc(mf, tokenFor(), "ping")).text();
      assert.deepEqual(await logGate.diagnosticTail(50), firstRows, "a refused request writes no row");
      assert.equal((await rpc(mf, good, "ping")).status, 200, "a verified credential on that IP is not blocked");
    } finally {
      await mf.dispose();
    }
  });

  await step("one client retrying one failing token counts once, and does not lock out other credentials on its IP", async () => {
    const mf = start({}, { AUTH_FAIL_RATE_LIMITER: { namespace_id: "4", simple: { limit: 2, period: 60 } } });
    try {
      const { token: cold, logGate } = await mint(mf);
      const statuses = [];
      for (let i = 0; i < 8; i++) statuses.push((await rpc(mf, "expired-oauth-token", "ping")).status);
      assert.deepEqual(statuses, Array(8).fill(401));
      const rows = (await logGate.diagnosticTail(50)).filter((row) => row.reason === "mcp_http_401");
      assert.equal(rows.reduce((total, row) => total + (row.count ?? 1), 0), 1, "the retries wrote one row");
      assert.equal((await rpc(mf, cold, "ping")).status, 200, "a cold credential on that IP still works");
    } finally {
      await mf.dispose();
    }
  });

  await step("a rate-limited /oauth/token answers 429 and writes no row", async () => {
    const mf = start({}, { TOKEN_RATE_LIMITER: { namespace_id: "3", simple: { limit: 1, period: 60 } } });
    try {
      const { logGate } = await mint(mf);
      const token = () => mf.dispatchFetch(`${HOST}/oauth/token`, { method: "POST", headers: { "Content-Type": "text/plain", "CF-Connecting-IP": "203.0.113.10" }, body: "x=1" });
      assert.equal((await token()).status, 400);
      const before = await logGate.diagnosticTail(50);
      const limited = await token();
      assert.equal(limited.status, 429);
      assert.equal(limited.headers.get("retry-after"), "60");
      assert.deepEqual(await logGate.diagnosticTail(50), before);
    } finally {
      await mf.dispose();
    }
  });

  await step("GUARD_MODE=off stops every route before any work", async () => {
    const mf = start({ GUARD_MODE: "off" });
    try {
      for (const pathname of ["/health", "/mcp", "/oauth/token", "/internal/gr-bearer-verify"]) {
        const res = await mf.dispatchFetch(`${HOST}${pathname}`, { method: pathname === "/health" ? "GET" : "POST" });
        assert.equal(res.status, 503, pathname);
        assert.equal(res.headers.get("retry-after"), "3600", pathname);
      }
    } finally {
      await mf.dispose();
    }
  });

  console.log(`passed ${passed}`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
