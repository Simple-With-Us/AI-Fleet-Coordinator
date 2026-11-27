// The service-only deployment canary on the real runtime, through the real Worker entry:  authenticated initialize,
// tools/list and a fixed read-only tools/call with a bearer that belongs to no seat, plus every refusal.
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { testEnv, wranglerConfig } from "./helpers.mjs";

const HOST = "https://agents.simplewithus.com";
const dir = mkdtempSync(path.join(tmpdir(), "service-canary-"));
const out = path.join(dir, "worker.mjs");
await build({ entryPoints: ["src/index.js"], bundle: true, format: "esm", platform: "browser", conditions: ["workerd", "worker", "browser"], external: ["cloudflare:workers", "node:*"], outfile: out, logLevel: "error" });
const script = readFileSync(out, "utf8");
const w = wranglerConfig();
const hashOf = (token) => createHash("sha256").update(token).digest("hex");
const newToken = () => `scn_${randomBytes(32).toString("base64url")}`;
const outbound = { count: 0 };

function start(digest, ratelimits = {}) {
  return new Miniflare(convertV4MiniflareOptions({
    upstream: HOST,
    workers: [{
      name: "canary",
      modules: true,
      script,
      compatibilityDate: w.compatibility_date,
      compatibilityFlags: w.compatibility_flags,
      kvNamespaces: ["OAUTH_KV"],
      ratelimits,
      durableObjects: { SEAT_GATE: { className: "SeatGate", useSQLite: true } },
      bindings: testEnv({ HOSTED_SEATS: "JET,GROK-WEB,CLAUDE", INFISICAL_MODE: "off", ZULIP_CLAUDE_API_KEY: "z".repeat(32), ...(digest === undefined ? {} : { SERVICE_CANARY_BEARER_SHA256: digest }) }),
      outboundService: () => { outbound.count += 1; return new Response("absent", { status: 404 }); },
    }],
  }));
}

let id = 0;
async function post(mf, token, frame, headers = {}, raw = null) {
  return mf.dispatchFetch(`${HOST}/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "CF-Connecting-IP": "203.0.113.9", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers },
    body: raw ?? JSON.stringify({ jsonrpc: "2.0", ...frame }),
  });
}
const rpc = (mf, token, method, params = {}, headers) => post(mf, token, { id: ++id, method, params }, headers);
const message = (text) => {
  const line = text.split("\n").find((l) => l.startsWith("data:"));
  return JSON.parse(line ? line.slice(5) : text);
};
const INIT = { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "1" } };
const V = { "MCP-Protocol-Version": "2025-06-18" };

let passed = 0;
async function step(name, fn) {
  try { await fn(); passed += 1; console.log(`ok - ${name}`); } catch (error) { console.log(`not ok - ${name}`); throw error; }
}

try {
  await step("unauthenticated initialize still answers the OAuth 401 with resource metadata", async () => {
    const token = newToken();
    const mf = start(hashOf(token));
    try {
      const res = await rpc(mf, null, "initialize", INIT);
      assert.equal(res.status, 401);
      assert.match(res.headers.get("www-authenticate") ?? "", /resource_metadata/);
    } finally { await mf.dispose(); }
  });

  await step("the canary bearer initializes, lists exactly one read-only tool and calls it", async () => {
    const token = newToken();
    const mf = start(hashOf(token));
    try {
      const init = await rpc(mf, token, "initialize", INIT);
      assert.equal(init.status, 200);
      const result = message(await init.text()).result;
      assert.ok(result.protocolVersion);
      assert.equal(result.serverInfo.name, "ST-MCP-CANARY");
      assert.deepEqual(Object.keys(result.capabilities), ["tools"]);
      assert.equal((await post(mf, token, { method: "notifications/initialized" }, V)).status, 202);
      const list = message(await (await rpc(mf, token, "tools/list", {}, V)).text()).result.tools;
      assert.deepEqual(list.map((t) => t.name), ["deploy_canary"]);
      assert.equal(list[0].annotations.readOnlyHint, true);
      assert.deepEqual(list[0].inputSchema.properties, {});
      const call = message(await (await rpc(mf, token, "tools/call", { name: "deploy_canary", arguments: {} }, V)).text());
      assert.equal(call.result.isError, undefined);
      assert.deepEqual(JSON.parse(call.result.content[0].text), { ok: true, canary: "service" });
      for (const args of [{ unexpected: true }, [], null, "value"]) {
        const refused = message(await (await rpc(mf, token, "tools/call", { name: "deploy_canary", arguments: args }, V)).text());
        assert.ok(refused.error || refused.result?.isError);
      }
      assert.equal(outbound.count, 0, "nothing left the Worker");
    } finally { await mf.dispose(); }
  });

  await step("no seat tool is reachable: identities, guide and any other name are Unknown tool", async () => {
    const token = newToken();
    const mf = start(hashOf(token));
    try {
      for (const name of ["identities", "guide", "zulip_send", "recall_contribute", "whoami"]) {
        const res = await rpc(mf, token, "tools/call", { name, arguments: {} }, V);
        const body = message(await res.text());
        assert.ok(body.error || body.result?.isError, name);
        assert.match(JSON.stringify(body), /Unknown tool/, name);
      }
    } finally { await mf.dispose(); }
  });

  await step("wrong, malformed, short and unconfigured bearers are the same generic 401", async () => {
    const token = newToken();
    const cases = [
      [hashOf(token), newToken()],
      [hashOf(token), "scn_short"],
      [hashOf(token), `scn_${"!".repeat(40)}`],
      [undefined, token],
      ["", token],
      ["nothex".repeat(11).slice(0, 64), token],
      [hashOf(token).toUpperCase().slice(0, 63), token],
    ];
    for (const [digest, presented] of cases) {
      const mf = start(digest);
      try {
        const res = await rpc(mf, presented, "initialize", INIT);
        assert.equal(res.status, 401, `${digest}`);
        assert.match(res.headers.get("www-authenticate") ?? "", /invalid_token/);
        assert.doesNotMatch(await res.text(), /canary|scn_|sha/i);
      } finally { await mf.dispose(); }
    }
  });

  await step("a seat bearer is not a canary bearer and the canary bearer is not a seat bearer", async () => {
    const token = newToken();
    const mf = start(hashOf(token));
    try {
      const sbt = `sbt_claude_${randomBytes(32).toString("base64url")}`;
      assert.equal((await rpc(mf, sbt, "initialize", INIT)).status, 401);
      // Presented as another seat's prefix, the canary secret is just an unknown seat bearer.
      assert.equal((await rpc(mf, token.replace(/^scn_/, "sbt_claude_"), "initialize", INIT)).status, 401);
    } finally { await mf.dispose(); }
  });

  await step("an existing seat bearer still works beside the canary (no regression)", async () => {
    const canary = newToken();
    const mf = start(hashOf(canary));
    try {
      const seatToken = `sbt_claude_${randomBytes(32).toString("base64url")}`;
      const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "canary");
      await ns.get(ns.idFromName("CLAUDE")).gbBearerMint({ hash: hashOf(seatToken), by: "test", expiresAt: Date.now() + 600_000, mcpAccept: true });
      const res = await rpc(mf, seatToken, "tools/call", { name: "identities", arguments: {} }, V);
      assert.equal(res.status, 200);
      assert.equal(message(await res.text()).result.isError, undefined);
    } finally { await mf.dispose(); }
  });

  await step("the Host and Origin checks and the body bound still apply to the canary", async () => {
    const token = newToken();
    const mf = start(hashOf(token));
    try {
      assert.equal((await rpc(mf, token, "tools/list", {}, { ...V, Origin: "https://evil.example" })).status, 403);
      const big = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "deploy_canary", arguments: { pad: "x".repeat(8000) } } });
      assert.equal((await post(mf, token, null, V, big)).status, 413);
    } finally { await mf.dispose(); }
  });

  await step("the canary bearer shares the per-credential MCP rate limit, so a runaway loop is bounded", async () => {
    const token = newToken();
    const mf = start(hashOf(token), { MCP_RATE_LIMITER: { namespace_id: "1", simple: { limit: 3, period: 60 } } });
    try {
      const statuses = [];
      for (let i = 0; i < 5; i++) statuses.push((await rpc(mf, token, "tools/list", {}, V)).status);
      assert.deepEqual(statuses.slice(0, 3), [200, 200, 200]);
      assert.ok(statuses.slice(3).every((status) => status === 429), statuses.join(","));
    } finally { await mf.dispose(); }
  });

  console.log(`passed ${passed}`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
