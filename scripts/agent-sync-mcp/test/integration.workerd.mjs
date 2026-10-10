// One workerd integration test (spec 6, Worker suite).
// Runs the bundled Worker in Miniflare (from the pinned wrangler) with fake
// Access certificates, ChatGPT's and Grok's real CIMD documents and a fake
// Zulip (test/fake-zulip.mjs) served by a mocked outbound fetch, then walks
// the OAuth flow and the seven tools, ending with a post as GROK-WEB.
//
//   npm ci --ignore-scripts && npm run test:workerd
//
// It needs node_modules, so CI runs only the plain `node --test` suites.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, createHmac, randomBytes } from "node:crypto";
import { Webhook } from "standardwebhooks";
import path from "node:path";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import CONTRACT from "../../agent_sync/mcp/tools.json" with { type: "json" };
import { ROOT, wranglerConfig, testEnv, browserPostOrigin, CHATGPT_CLIENT, CHATGPT_REDIRECT, RESOURCE } from "./helpers.mjs";
import { FakeZulip, REALM, STREAMS, OWNER_ID, zulipShapedKey } from "./fake-zulip.mjs";

const TOOLS = CONTRACT.tools;
// The hosted server lists tools.json, then the hosted-only recall tools (src/recall.js), then the DM tools (src/dm.js).
const HOSTED_NAMES = [...TOOLS.map((t) => t.name), "recall_search", "recall_stats", "recall_contribute", "dm_list", "dm_read", "dm_send"];
const JET_KEY = zulipShapedKey(11);
const zulip = new FakeZulip({ bots: [{ email: "openai-dot-bot@simplewithus.zulipchat.com", full_name: "Jet", key: JET_KEY }] });

const HOST = "https://agent-sync.jays.services";
const TEAM = "silent-frost-37e0.cloudflareaccess.com";
const AUD = "d".repeat(64);
const CHATGPT_CIMD = {
  client_id: CHATGPT_CLIENT,
  client_uri: "https://chatgpt.com/",
  redirect_uris: [CHATGPT_REDIRECT],
  token_endpoint_auth_method: "private_key_jwt",
  token_endpoint_auth_methods_supported: ["none", "private_key_jwt"],
  grant_types: ["authorization_code", "refresh_token"],
  response_types: ["code"],
  client_name: "ChatGPT",
  logo_uri: "https://persistent.oaistatic.com/sonic/misc/openai-logo.png",
  token_endpoint_auth_signing_alg: "RS256",
  jwks_uri: "https://chatgpt.com/oauth/jwks.json",
};

const GROK_CLIENT = "https://grok.com/oauth/mcp-client.json";
const GROK_REDIRECT = "https://grok.com/connectors-oauth-exchange-code/";
// Fetched from the live URL on 2026-10-09.
const GROK_CIMD = {
  client_id: GROK_CLIENT,
  client_name: "Grok",
  client_uri: "https://grok.com",
  logo_uri: "https://grok.com/icon-512x512.png",
  redirect_uris: [GROK_REDIRECT, "https://console.x.ai/connectors-oauth-exchange-code/"],
  grant_types: ["authorization_code", "refresh_token"],
  response_types: ["code"],
  token_endpoint_auth_method: "none",
};

let passed = 0;
async function step(name, fn) {
  try {
    await fn();
    passed++;
    console.log(`ok - ${name}`);
  } catch (error) {
    console.log(`not ok - ${name}`);
    throw error;
  }
}

// ---------------------------------------------------------------- fakes

const b64url = (buf) => Buffer.from(buf).toString("base64url");
const { privateKey, publicKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true,
  ["sign", "verify"],
);
const jwks = { keys: [{ ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "test-kid" }] };
async function accessJwt(email = "mail@jays.services") {
  const now = Math.floor(Date.now() / 1000);
  const head = b64url(JSON.stringify({ alg: "RS256", kid: "test-kid", typ: "JWT" }));
  const body = b64url(JSON.stringify({ iss: `https://${TEAM}`, aud: [AUD], exp: now + 900, iat: now, nbf: now, email, type: "app" }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(`${head}.${body}`));
  return `${head}.${body}.${b64url(new Uint8Array(sig))}`;
}

// A fake ChatGPT MCP Events receiver on chatgpt.com:  it echoes verification
// challenges, records deliveries, and answers each delivery with the next
// status in `callbackStatuses` (200 when empty).  `onDelivery`, when set, runs
// once while the next delivery is still waiting for its answer.
const CALLBACK = "https://chatgpt.com/backend-api/mcp-events/cb_workerd";
const CALLBACK_SECRET = `whsec_${Buffer.alloc(32, 5).toString("base64")}`;
const callbackCalls = [];
const callbackStatuses = [];
let onDelivery = null;
const WAKE_KEY_JET = randomBytes(32).toString("hex");

const outbound = [];
// A countdown of ChatGPT document fetches that fail with 503 before it recovers.
let failChatgptCimd = 0;
async function outboundFetch(request) {
  outbound.push(request.url);
  if (request.url === `https://${TEAM}/cdn-cgi/access/certs`) return Response.json(jwks);
  if (request.url === CHATGPT_CLIENT) {
    if (failChatgptCimd > 0) {
      failChatgptCimd--;
      return new Response("temporarily down", { status: 503 });
    }
    return Response.json(CHATGPT_CIMD);
  }
  if (request.url === GROK_CLIENT) return Response.json(GROK_CIMD);
  if (request.url.startsWith(`${REALM}/`)) return zulip.fetch(request);
  if (request.url.startsWith(CALLBACK)) {
    const raw = await request.text();
    const headers = Object.fromEntries(request.headers);
    const body = JSON.parse(raw);
    callbackCalls.push({ url: request.url, headers, raw, body });
    if (body.type === "verification") return Response.json({ challenge: body.challenge });
    const status = callbackStatuses.length ? callbackStatuses.shift() : 200;
    if (onDelivery) {
      const hook = onDelivery;
      onDelivery = null;
      await hook(body);
    }
    return new Response("", { status });
  }
  return new Response("blocked in test", { status: 599 });
}

// ---------------------------------------------------------------- worker

execFileSync("npx", ["--no-install", "wrangler", "deploy", "--dry-run", "--outdir", "dist"], {
  cwd: ROOT,
  stdio: ["ignore", "ignore", "inherit"],
  env: { ...process.env, WRANGLER_SEND_METRICS: "false" },
});
const w = wranglerConfig();
// Miniflare 5 (pinned by wrangler 4.139.0) takes v4-style options through its
// converter.  `upstream` makes workerd see the production URL and Host header;
// without it, Host is the loopback socket, which the Worker must refuse.
function startWorker(upstream) {
  return new Miniflare(convertV4MiniflareOptions({
    ...(upstream ? { upstream } : {}),
    workers: [
      {
        name: "agent-sync-mcp",
        modules: true,
        scriptPath: path.join(ROOT, "dist/index.js"),
        compatibilityDate: w.compatibility_date,
        compatibilityFlags: w.compatibility_flags,
        kvNamespaces: ["OAUTH_KV"],
        durableObjects: { SEAT_GATE: { className: "SeatGate", useSQLite: true } },
        bindings: testEnv({ ACCESS_AUD: AUD, HOSTED_SEATS: "JET,GROK-WEB,ECHO,INSTINCT", ZULIP_KEY_GROK_WEB: zulip.key, ZULIP_KEY_JET: JET_KEY, WAKE_HMAC_KEY_JET: WAKE_KEY_JET }),
        outboundService: outboundFetch,
      },
    ],
  }));
}

// Host check first, on its own instance (sequential, to keep memory low).
{
  const raw = startWorker(null);
  try {
    await step("a foreign Host header, a foreign URL or workers.dev gets 404", async () => {
      for (const url of [`${HOST}/.well-known/oauth-authorization-server`, `${HOST}/health`, "https://evil.example/mcp", "https://evil.example/health", "https://agent-sync-mcp.example.workers.dev/.well-known/oauth-authorization-server"]) {
        const res = await raw.dispatchFetch(url);
        assert.equal(res.status, 404, url);
        assert.equal(await res.text(), "Not Found");
      }
    });
  } finally {
    await raw.dispose();
  }
}

const mf = startWorker(HOST);

const jar = new Map();
// Referrer-Policy of the last HTML page the "browser" loaded.  It decides the
// Origin header on the next form POST, the way a real browser decides it.
let pagePolicy = null;
function remember(res) {
  if ((res.headers.get("content-type") ?? "").includes("text/html")) pagePolicy = res.headers.get("referrer-policy");
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const [pair] = c.split(";");
    const idx = pair.indexOf("=");
    jar.set(pair.slice(0, idx), pair.slice(idx + 1));
  }
}
const cookieHeader = () => [...jar].map(([k, v]) => `${k}=${v}`).join("; ");

async function browser(pathname, { method = "GET", form, jwt = true, origin = true } = {}) {
  const headers = { Cookie: cookieHeader() };
  if (jwt) headers["Cf-Access-Jwt-Assertion"] = await accessJwt();
  if (method === "POST" && origin) Object.assign(headers, { Origin: browserPostOrigin({ policy: pagePolicy }), "Sec-Fetch-Site": "same-origin" });
  if (form) headers["Content-Type"] = "application/x-www-form-urlencoded";
  const res = await mf.dispatchFetch(`${HOST}${pathname}`, { method, headers, body: form ? new URLSearchParams(form).toString() : undefined, redirect: "manual" });
  remember(res);
  return res;
}

async function adminAction(action, fields = {}) {
  const page = await browser("/admin");
  assert.equal(page.status, 200);
  const csrf = (await page.text()).match(/name="csrf" value="([0-9a-f]{64})"/)[1];
  return browser("/admin/action", { method: "POST", form: { csrf, action, ...fields } });
}

function pkce() {
  const verifier = b64url(randomBytes(32));
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  return { verifier, challenge };
}

function authorizeQuery(challenge, state = "st-1", { clientId = CHATGPT_CLIENT, redirect = CHATGPT_REDIRECT } = {}) {
  return new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirect,
    code_challenge: challenge,
    code_challenge_method: "S256",
    resource: RESOURCE,
    scope: "zulip:read zulip:write",
    state,
  }).toString();
}

async function openConsent(challenge, state, who) {
  const res = await browser(`/authorize?${authorizeQuery(challenge, state, who)}`);
  const html = await res.text();
  assert.equal(res.status, 200, html.slice(0, 300));
  return html.match(/name="handle" value="([^"]+)"/)[1];
}

async function token(form, headers = {}) {
  const res = await mf.dispatchFetch(`${HOST}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", ...headers },
    body: new URLSearchParams(form).toString(),
  });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

let rpcId = 0;
async function mcp(accessToken, method, params = {}) {
  const res = await mf.dispatchFetch(`${HOST}/mcp`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": "2025-06-18",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params }),
  });
  const text = await res.text();
  let json = null;
  if ((res.headers.get("content-type") ?? "").includes("text/event-stream")) {
    const line = text.split("\n").find((l) => l.startsWith("data:"));
    json = line ? JSON.parse(line.slice(5)) : null;
  } else if (text) {
    try {
      json = JSON.parse(text);
    } catch {}
  }
  return { status: res.status, headers: res.headers, json };
}

/** A 2026-07-28 request:  the per-request _meta envelope plus the standard headers. */
async function mcpModern(accessToken, method, params = {}) {
  const envelope = { "io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": { name: "workerd-test", version: "0" } };
  const res = await mf.dispatchFetch(`${HOST}/mcp`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": "2026-07-28",
      "Mcp-Method": method,
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params: { ...params, _meta: envelope } }),
  });
  const text = await res.text();
  const line = text.split("\n").find((l) => l.startsWith("data:"));
  return { status: res.status, json: line ? JSON.parse(line.slice(5)) : text ? JSON.parse(text) : null };
}

/** The listener's wake POST (adapters.py):  sorted compact JSON, hex HMAC of the raw bytes. */
function wakeBytes(overrides = {}) {
  const body = {
    contract: "agent-sync-wake/1", seat: "JET", wake_id: `w-${randomBytes(6).toString("hex")}`, message_id: 9001, trigger_ids: [9001],
    dm: false, channel: "sandbox", topic: "jet hello", stream_id: STREAMS.sandbox, dm_recipient_ids: [], sender_user_id: 1211974, sender_full_name: "Jay",
    is_bot: false, owner: true, excerpt: "<<<BEGIN nonce=n1\n@**Jet** ping\n>>>END nonce=n1",
    zulip_link: `${REALM}/#narrow/channel/642167-sandbox/topic/jet.20hello/near/9001`,
    reply_to: { type: "stream", channel: "sandbox", topic: "jet hello" }, reply_prefix: "[JET·wake] re=9001",
    sent_at: Math.floor(Date.now() / 1000), ...overrides,
  };
  return Buffer.from(JSON.stringify(Object.fromEntries(Object.keys(body).sort().map((k) => [k, body[k]]))));
}
function wakePost(seat, bytes, { key = WAKE_KEY_JET, method = "POST", signature } = {}) {
  return mf.dispatchFetch(`${HOST}/internal/wake/${seat}`, {
    method,
    headers: { "Content-Type": "application/json; charset=utf-8", "X-Agent-Sync-Signature": signature ?? createHmac("sha256", Buffer.from(key, "utf8")).update(bytes).digest("hex"), "Idempotency-Key": "x" },
    body: method === "POST" ? bytes : undefined,
  });
}

const INIT = { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "workerd-test", version: "0" } };
const call = (accessToken, name, args = {}) => mcp(accessToken, "tools/call", { name, arguments: args });
const seatOf = async (accessToken) => (await call(accessToken, "whoami")).json?.result?.structuredContent?.seat;

// ---------------------------------------------------------------- flow

try {
  let issuer;
  await step("metadata:  issuer equals authorization_servers[0] byte for byte", async () => {
    const asm = await (await mf.dispatchFetch(`${HOST}/.well-known/oauth-authorization-server`)).json();
    const prm = await (await mf.dispatchFetch(`${HOST}/.well-known/oauth-protected-resource/mcp`)).json();
    issuer = asm.issuer;
    assert.equal(issuer, HOST);
    assert.equal(prm.authorization_servers[0], asm.issuer);
    assert.equal(prm.resource, RESOURCE);
    assert.equal(asm.authorization_endpoint, `${HOST}/authorize`);
    assert.equal(asm.token_endpoint, `${HOST}/oauth/token`);
    assert.deepEqual(asm.code_challenge_methods_supported, ["S256"]);
    assert.equal(asm.authorization_response_iss_parameter_supported, true);
    assert.equal(asm.client_id_metadata_document_supported, true);
    assert.equal(asm.registration_endpoint, undefined);
  });

  await step("GET /health is a static 200 for the fleet admin panel, and nothing else answers near it", async () => {
    const res = await mf.dispatchFetch(`${HOST}/health`);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true });
    assert.equal((await mf.dispatchFetch(`${HOST}/health`, { method: "HEAD" })).status, 200);
    assert.equal((await mf.dispatchFetch(`${HOST}/health`, { method: "POST", body: "x" })).status, 405);
    for (const p of ["/health/", "/Health", "/healthz"]) {
      const r = await mf.dispatchFetch(`${HOST}${p}`);
      assert.equal(r.status, 404, p);
      await r.arrayBuffer();
    }
  });

  await step("unauthenticated initialize gets 401 with resource_metadata", async () => {
    const r = await mcp(null, "initialize", INIT);
    assert.equal(r.status, 401);
    assert.match(r.headers.get("www-authenticate"), /resource_metadata="https:\/\/agent-sync\.jays\.services\/\.well-known\/oauth-protected-resource\/mcp"/);
  });

  await step("/authorize and /admin without an Access JWT are 403", async () => {
    for (const p of ["/authorize", "/admin"]) {
      const res = await browser(p, { jwt: false });
      assert.equal(res.status, 403, p);
      await res.arrayBuffer();
    }
  });

  await step("unarmed /authorize says no connection expected and fetches nothing", async () => {
    outbound.length = 0;
    const res = await browser(`/authorize?${authorizeQuery(pkce().challenge)}`);
    assert.equal(res.status, 403);
    assert.match(await res.text(), /No Connection Expected/);
    assert.ok(!outbound.includes(CHATGPT_CLIENT), "no CIMD fetch while unarmed");
  });

  await step("unlisted CIMD client_id is refused before any fetch, on /authorize and /oauth/token", async () => {
    outbound.length = 0;
    const evil = "https://evil.example/oauth/client.json";
    const q = authorizeQuery(pkce().challenge).replace(encodeURIComponent(CHATGPT_CLIENT), encodeURIComponent(evil));
    const res = await browser(`/authorize?${q}`);
    assert.equal(res.status, 400);
    await res.arrayBuffer();
    const t = await token({ grant_type: "authorization_code", client_id: evil, code: "x", code_verifier: "y", redirect_uri: CHATGPT_REDIRECT });
    assert.equal(t.status, 401);
    assert.equal(t.body.error, "invalid_client");
    assert.deepEqual(outbound.filter((u) => u.includes("evil.example")), []);
  });

  await step("token gate wiring:  parser-differential tricks reach no fetch, and the refusals are counted apart", async () => {
    outbound.length = 0;
    const evil = "https://evil.example/oauth/client.json";
    const base = { grant_type: "authorization_code", client_id: evil, code: "x", code_verifier: "y", redirect_uri: CHATGPT_REDIRECT };
    // A Content-Type the library trims into a form type (leading U+00A0) must still be gated.
    const nbspType = await token(base, { "Content-Type": "\u00a0application/x-www-form-urlencoded" });
    assert.ok([401, 400].includes(nbspType.status), `nbsp content-type: ${nbspType.status}`);
    // A Basic header the library does not read as Basic must not hide the form client_id.
    const nbspBasic = await token(base, { Authorization: `Basic\u00a0${btoa("x:")}` });
    assert.equal(nbspBasic.status, 401);
    assert.equal(nbspBasic.body.error, "invalid_client");
    // Not a form at all:  refused by the gate itself.
    const json = await token({}, { "Content-Type": "application/json" });
    assert.equal(json.status, 400);
    assert.equal(json.body.error, "invalid_request");
    // A leading-space id.
    const spaced = await token({ ...base, client_id: ` ${evil}` });
    assert.equal(spaced.status, 401);
    assert.deepEqual(outbound.filter((u) => u.includes("evil.example")), [], "no document fetch for any trick");
    const admin = await (await browser("/admin")).text();
    const [authorizePart, tokenPart] = admin.split("Refused Token Requests");
    assert.ok(tokenPart.includes("authorization_not_basic") && tokenPart.includes("cimd_client_not_allowlisted"), "token refusals are in the counted table");
    assert.ok(!authorizePart.includes("authorization_not_basic"), "and not in the authorize log");
  });

  await step("admin POST without CSRF or same-origin is refused", async () => {
    const res = await browser("/admin/action", { method: "POST", form: { action: "arm", seat: "JET", csrf: "0".repeat(64) } });
    assert.equal(res.status, 403);
    await res.arrayBuffer();
    const page = await browser("/admin");
    const csrf = (await page.text()).match(/name="csrf" value="([0-9a-f]{64})"/)[1];
    const cross = await browser("/admin/action", { method: "POST", form: { action: "arm", seat: "JET", csrf }, origin: false });
    assert.equal(cross.status, 403);
    await cross.arrayBuffer();
  });

  const { verifier, challenge } = pkce();
  let code;
  await step("arm JET, consent, and the redirect carries code, state and iss", async () => {
    const armed = await adminAction("arm", { seat: "JET" });
    assert.equal(armed.status, 303);
    const handle = await openConsent(challenge, "st-1");
    // The consent page must not be no-referrer, or a browser's Approve would carry Origin: null.
    assert.equal(pagePolicy, "same-origin");
    assert.equal(browserPostOrigin({ policy: pagePolicy }), HOST);
    const res = await browser("/authorize", { method: "POST", form: { handle, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302, await res.clone().text());
    assert.equal(res.headers.get("referrer-policy"), "same-origin", "the redirect to the client sends no Referer");
    const loc = new URL(res.headers.get("location"));
    assert.equal(`${loc.origin}${loc.pathname}`, CHATGPT_REDIRECT);
    assert.equal(loc.searchParams.get("state"), "st-1");
    assert.equal(loc.searchParams.get("iss"), issuer);
    code = loc.searchParams.get("code");
    assert.ok(code);
  });

  await step("approval consumed the arming window", async () => {
    const st = await (await browser("/admin")).text();
    assert.match(st, /<strong>JET<\/strong><\/td><td>Not armed/);
  });

  let tokens;
  await step("code exchange with PKCE (ChatGPT's CIMD client authenticates as none)", async () => {
    const t = await token({ grant_type: "authorization_code", code, code_verifier: verifier, redirect_uri: CHATGPT_REDIRECT, client_id: CHATGPT_CLIENT, resource: RESOURCE });
    assert.equal(t.status, 200, JSON.stringify(t.body));
    tokens = t.body;
    assert.ok(tokens.access_token && tokens.refresh_token);
    assert.equal(tokens.scope, "zulip:read");
  });

  await step("tools/list is tools.json plus recall plus DMs, and whoami shows JET with a read-only grant", async () => {
    const init = await mcp(tokens.access_token, "initialize", INIT);
    assert.equal(init.status, 200, JSON.stringify(init.json));
    assert.match(init.json.result.instructions, /one bot, JET\./);
    assert.match(init.json.result.instructions, /BEGIN_UNTRUSTED_ZULIP and END_UNTRUSTED_ZULIP/);
    assert.deepEqual(init.json.result.capabilities, { tools: {}, events: {} });
    const list = await mcp(tokens.access_token, "tools/list");
    assert.equal(list.status, 200);
    const tools = list.json.result.tools;
    assert.deepEqual(tools.map((t) => t.name), HOSTED_NAMES);
    for (const t of TOOLS) {
      // Era fields aside, every tool is exactly the stdio server's contract, except that the
      // hosted inbox says it lists DMs (spec 1.2) where the stdio one says they are left out.
      const served = Object.fromEntries(Object.keys(t).map((k) => [k, tools.find((x) => x.name === t.name)[k]]));
      if (t.name === "inbox") {
        assert.match(served.description, /direct messages sent to it/);
        assert.deepEqual({ ...served, description: null }, { ...t, description: null });
      } else assert.deepEqual(served, t, t.name);
    }
    for (const name of ["dm_list", "dm_read", "dm_send"]) assert.ok(tools.find((x) => x.name === name).outputSchema, name);
    assert.match(init.json.result.instructions, /Direct messages \(dm_list, dm_read, dm_send/);
    const who = await call(tokens.access_token, "whoami");
    assert.equal(who.json.result.structuredContent.seat, "JET");
    assert.equal(who.json.result.structuredContent.transport, "hosted");
    assert.equal(who.json.result.structuredContent.email, "openai-dot-bot@simplewithus.zulipchat.com");
  });

  await step("MCP Events:  server/discover advertises events at 2026-07-28, and events/list offers zulip.mention", async () => {
    const discover = await mcpModern(tokens.access_token, "server/discover");
    assert.equal(discover.status, 200, JSON.stringify(discover.json));
    assert.deepEqual(discover.json.result.supportedVersions, ["2026-07-28"]);
    assert.deepEqual(discover.json.result.capabilities, { tools: {}, events: {} });
    const list = await mcpModern(tokens.access_token, "events/list");
    assert.equal(list.status, 200, JSON.stringify(list.json));
    const [ev] = list.json.result.events;
    assert.equal(ev.name, "zulip.mention");
    assert.deepEqual(ev.delivery, ["webhook"]);
    assert.deepEqual(ev.inputSchema.properties.channel.enum, ["agent-sync", "sandbox"]);
  });

  let subId;
  await step("MCP Events:  subscribe verifies the callback with a signed challenge;  a foreign host is -32015 and logged", async () => {
    const params = { name: "zulip.mention", arguments: { channel: "sandbox" }, delivery: { mode: "webhook", url: CALLBACK, secret: CALLBACK_SECRET }, cursor: null };
    const sub = await mcpModern(tokens.access_token, "events/subscribe", params);
    assert.equal(sub.status, 200, JSON.stringify(sub.json));
    subId = sub.json.result.id;
    assert.match(subId, /^sub_[0-9a-f]{32}$/);
    assert.equal(sub.json.result.cursor, null);
    assert.equal(sub.json.result.truncated, false);
    assert.ok(Date.parse(sub.json.result.refreshBefore) > Date.now());
    const verification = callbackCalls.find((c) => c.body.type === "verification");
    assert.ok(verification, "a verification request reached the callback");
    assert.equal(verification.headers["x-mcp-subscription-id"], subId);
    assert.ok(new Webhook(CALLBACK_SECRET).verify(verification.raw, verification.headers), "the challenge is Standard Webhooks signed");
    // A refresh is the same subscription, and is not re-verified.
    const again = await mcpModern(tokens.access_token, "events/subscribe", { ...params, ttlMs: 7_200_000 });
    assert.equal(again.json.result.id, subId);
    assert.equal(callbackCalls.filter((c) => c.body.type === "verification").length, 1);

    const evil = await mcpModern(tokens.access_token, "events/subscribe", { ...params, delivery: { ...params.delivery, url: "https://evil.example.org/cb" } });
    assert.equal(evil.json.error.code, -32015);
    assert.equal(evil.json.error.data.reason, "callback_url_refused");
    const admin = await (await browser("/admin")).text();
    assert.match(admin, /callback_host_not_allowed/);
    assert.match(admin, /evil\.example\.org/);
    assert.ok(!outbound.some((u) => u.startsWith("https://evil.example.org")), "no fetch to a refused host");

    // One live destination:  a second chat is refused and the first stays.
    const second = await mcpModern(tokens.access_token, "events/subscribe", { ...params, delivery: { ...params.delivery, url: `${CALLBACK}_second` } });
    assert.equal(second.json.error.code, -32600, JSON.stringify(second.json));
    assert.match(second.json.error.message, /another chat/);
  });

  await step("wake:  a signed listener POST is 202 and delivers one Standard Webhooks event;  a replay is a duplicate", async () => {
    const before = callbackCalls.length;
    const bytes = wakeBytes();
    const res = await wakePost("JET", bytes);
    assert.equal(res.status, 202, await res.clone().text());
    assert.deepEqual(await res.json(), { ok: true, subscribers: 1, delivered: 1, outcome: "delivered" });
    assert.equal(callbackCalls.length, before + 1, "delivered before the answer, inside the listener's request");
    const delivery = callbackCalls[callbackCalls.length - 1];
    const event = new Webhook(CALLBACK_SECRET).verify(delivery.raw, delivery.headers);
    assert.equal(event.name, "zulip.mention");
    assert.equal(delivery.headers["webhook-id"], event.eventId);
    assert.equal(delivery.headers["x-mcp-subscription-id"], subId);
    assert.equal(event.data.message_id, 9001);
    assert.equal(event.data.owner_hint, true);
    assert.deepEqual(event.data.reply_to, { type: "stream", channel: "sandbox", topic: "jet hello" });
    assert.ok(!delivery.raw.includes(WAKE_KEY_JET) && !delivery.raw.includes(JET_KEY), "no key in the payload");

    // A DM wakes too.  The sandbox filter leaves DMs out, so here it is checked through the
    // answer only (no matching subscription);  the unit suite checks that an unfiltered
    // subscription gets the DM's fenced excerpt.
    const dmBefore = callbackCalls.length;
    const dm = await wakePost("JET", wakeBytes({ dm: true, channel: null, topic: null, stream_id: null, dm_recipient_ids: [1211974], reply_to: { type: "direct", to: [1211974] } }));
    assert.equal(dm.status, 202);
    assert.deepEqual(await dm.json(), { ok: true, subscribers: 1, delivered: 0, outcome: "no_subscriber" });
    assert.equal(callbackCalls.length, dmBefore, "a channel-filtered subscription gets no DM");

    const replay = await wakePost("JET", bytes);
    assert.equal(replay.status, 200);
    assert.deepEqual(await replay.json(), { ok: true, duplicate: true });
    // An agent-sync mention (an allowlisted stream) does not match the sandbox-only subscription.
    const other = await wakePost("JET", wakeBytes({ channel: "agent-sync", stream_id: STREAMS["agent-sync"], reply_to: { type: "stream", channel: "agent-sync", topic: "x" } }));
    assert.equal(other.status, 202);
    assert.deepEqual(await other.json(), { ok: true, subscribers: 1, delivered: 0, outcome: "no_subscriber" });
    assert.equal(callbackCalls.length, before + 1, "no delivery for a non-matching channel");
  });

  await step("wake:  a failed delivery is 503 so the listener retries;  the retry delivers (202);  one more is a duplicate;  a copy mid-delivery is 503 in_flight", async () => {
    const before = callbackCalls.length;
    const bytes = wakeBytes();
    callbackStatuses.push(503, 502);
    let concurrent = null;
    onDelivery = async () => {
      const res = await wakePost("JET", bytes);
      concurrent = { status: res.status, retryAfter: res.headers.get("retry-after"), body: await res.json() };
    };
    let res = await wakePost("JET", bytes);
    assert.equal(res.status, 503, await res.clone().text());
    assert.deepEqual(await res.json(), { ok: false, retry: true, subscribers: 1, delivered: 0 });
    assert.equal(callbackCalls.length, before + 2, "two attempts inside the request");
    assert.deepEqual(concurrent, { status: 503, retryAfter: concurrent.retryAfter, body: { ok: false, in_flight: true } });
    assert.match(concurrent.retryAfter, /^\d+$/);
    // The listener's retry sends the identical bytes.
    res = await wakePost("JET", bytes);
    assert.equal(res.status, 202, await res.clone().text());
    assert.deepEqual(await res.json(), { ok: true, subscribers: 1, delivered: 1, outcome: "delivered" });
    assert.equal(callbackCalls.length, before + 3);
    assert.equal(new Set(callbackCalls.slice(before).map((c) => c.headers["webhook-id"])).size, 1, "one event id across attempts and retries");
    res = await wakePost("JET", bytes);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true, duplicate: true });
    assert.equal(callbackCalls.length, before + 3);
  });

  await step("wake:  a mention in a stream off the allowlist, or with no stream id, is settled with no callback hit", async () => {
    const before = callbackCalls.length;
    for (const overrides of [{ stream_id: STREAMS.other }, { stream_id: undefined }]) {
      const res = await wakePost("JET", wakeBytes(overrides));
      assert.equal(res.status, 202, await res.clone().text());
      assert.deepEqual(await res.json(), { ok: true, subscribers: 1, delivered: 0, outcome: "channel_refused" });
    }
    assert.equal(callbackCalls.length, before, "no callback hit");
    const admin = await (await browser("/admin")).text();
    assert.match(admin, /event_wake_channel_refused/);
    assert.match(admin, new RegExp(`stream_id&#34;:${STREAMS.other}`), "the audit shows the numeric stream id");
  });

  await step("wake:  held_back reaches the callback reduced to the allowlist;  a malformed one is 400 with no callback hit", async () => {
    const before = callbackCalls.length;
    const at = Math.floor(Date.now() / 1000) - 900;
    const kept = {
      message_id: 8001, dm: false, channel: "sandbox", topic: "jet hello", stream_id: STREAMS.sandbox, sender_full_name: "Codex",
      zulip_link: `${REALM}/#narrow/channel/642167-sandbox/topic/jet.20hello/near/8001`, reason: "wakes_per_hour", at,
    };
    const offList = {
      message_id: 8002, dm: false, channel: "secret-plans", topic: "launch", stream_id: STREAMS.other, sender_full_name: "Mallory",
      zulip_link: `${REALM}/#narrow/channel/999001-secret-plans/topic/launch/near/8002`, reason: "per_topic_per_hour", at,
    };
    const res = await wakePost("JET", wakeBytes({ held_back: { count: 5, items: [kept, offList] } }));
    assert.equal(res.status, 202, await res.clone().text());
    assert.deepEqual(await res.json(), { ok: true, subscribers: 1, delivered: 1, outcome: "delivered" });
    assert.equal(callbackCalls.length, before + 1);
    const delivery = callbackCalls[callbackCalls.length - 1];
    const event = new Webhook(CALLBACK_SECRET).verify(delivery.raw, delivery.headers);
    assert.deepEqual(event.data.held_back, { count: 5, items: [kept, { message_id: 8002, dm: false, reason: "per_topic_per_hour", at }] });
    for (const leak of ["secret-plans", "launch", "Mallory"]) assert.ok(!delivery.raw.includes(leak), `${leak} never reaches the callback`);

    const bad = await wakePost("JET", wakeBytes({ held_back: { count: 1, items: [{ ...kept, reason: "Not A Slug" }] } }));
    assert.equal(bad.status, 400);
    assert.equal(callbackCalls.length, before + 1, "a refused body reaches no callback");
  });

  await step("wake:  bad signature, stale, wrong seat, unkeyed seat, wrong method and oversize are refused", async () => {
    const bytes = wakeBytes();
    let res = await wakePost("JET", bytes, { key: "f".repeat(64) });
    assert.equal(res.status, 401);
    await res.arrayBuffer();
    res = await wakePost("JET", wakeBytes({ sent_at: Math.floor(Date.now() / 1000) - 600 }));
    assert.equal(res.status, 401);
    await res.arrayBuffer();
    res = await wakePost("JET", wakeBytes({ seat: "GROK-WEB" }));
    assert.equal(res.status, 400);
    await res.arrayBuffer();
    res = await wakePost("GROK-WEB", wakeBytes({ seat: "GROK-WEB" }));
    assert.equal(res.status, 404, "a seat without a wake key is not there");
    await res.arrayBuffer();
    res = await wakePost("NOPE", bytes);
    assert.equal(res.status, 404);
    await res.arrayBuffer();
    res = await wakePost("JET", bytes, { method: "GET" });
    assert.equal(res.status, 405);
    await res.arrayBuffer();
    res = await wakePost("JET", Buffer.alloc(70 * 1024, 32));
    assert.equal(res.status, 413);
    await res.arrayBuffer();
    // The Worker answers 413 without reading the body, which leaves the test
    // client's pooled socket reset;  one throwaway request absorbs that.
    await mf.dispatchFetch(`${HOST}/health`).then((r) => r.arrayBuffer()).catch(() => {});
  });

  await step("wake:  410 from the callback drops the subscription;  unsubscribe is idempotent", async () => {
    const before = callbackCalls.length;
    callbackStatuses.push(410);
    let res = await wakePost("JET", wakeBytes());
    assert.equal(res.status, 202);
    assert.deepEqual(await res.json(), { ok: true, subscribers: 1, delivered: 0, outcome: "gone" });
    assert.equal(callbackCalls.length, before + 1, "the 410 delivery");
    res = await wakePost("JET", wakeBytes());
    assert.deepEqual(await res.json(), { ok: true, subscribers: 0, delivered: 0, outcome: "no_subscriber" });
    const unsub = await mcpModern(tokens.access_token, "events/unsubscribe", { name: "zulip.mention", arguments: { channel: "sandbox" }, delivery: { mode: "webhook", url: CALLBACK } });
    assert.equal(unsub.status, 200, JSON.stringify(unsub.json));
    assert.deepEqual(Object.keys(unsub.json.result).filter((k) => k !== "_meta" && k !== "resultType"), []);
  });

  await step("wake:  a pause or an unsubscribe while the first attempt is out stops the retry (202 not_live, one callback hit)", async () => {
    const params = { name: "zulip.mention", arguments: { channel: "sandbox" }, delivery: { mode: "webhook", url: CALLBACK, secret: CALLBACK_SECRET }, cursor: null };
    const sub = await mcpModern(tokens.access_token, "events/subscribe", params);
    assert.equal(sub.status, 200, JSON.stringify(sub.json));
    const flips = {
      pause: async () => assert.equal((await adminAction("pause", { seat: "JET" })).status, 303),
      unsubscribe: async () => {
        const unsub = await mcpModern(tokens.access_token, "events/unsubscribe", { name: "zulip.mention", arguments: { channel: "sandbox" }, delivery: { mode: "webhook", url: CALLBACK } });
        assert.equal(unsub.status, 200, JSON.stringify(unsub.json));
      },
    };
    for (const [name, flip] of Object.entries(flips)) {
      const before = callbackCalls.length;
      callbackStatuses.push(503);
      onDelivery = flip;
      const res = await wakePost("JET", wakeBytes());
      assert.equal(res.status, 202, `${name}:  ${await res.clone().text()}`);
      assert.deepEqual(await res.json(), { ok: true, subscribers: 1, delivered: 0, outcome: "not_live" }, name);
      assert.equal(callbackCalls.length, before + 1, `${name}:  no attempt after it`);
      if (name === "pause") assert.equal((await adminAction("unpause", { seat: "JET" })).status, 303);
    }
    const res = await wakePost("JET", wakeBytes());
    assert.deepEqual(await res.json(), { ok: true, subscribers: 0, delivered: 0, outcome: "no_subscriber" });
  });

  await step("a seat argument is a tool error, an unknown tool a protocol error, and a missing scope names the challenge", async () => {
    const r = await call(tokens.access_token, "whoami", { seat: "GROK-WEB" });
    assert.equal(r.json.result.isError, true, JSON.stringify(r.json));
    assert.equal(r.json.result.structuredContent, undefined);
    assert.equal(r.json.result._meta["agent-sync/error"].code, "invalid_argument");
    assert.match(r.json.result._meta["agent-sync/error"].message, /unknown argument 'seat'/);
    const unknown = await call(tokens.access_token, "hello");
    assert.ok(unknown.json.error, JSON.stringify(unknown.json));
    const before = zulip.requestsTo("POST", "messages").length;
    const w2 = await call(tokens.access_token, "post", { channel: "sandbox", topic: "t", text: "x" });
    assert.equal(w2.json.result.isError, true);
    assert.equal(w2.json.result.structuredContent, undefined);
    assert.ok(w2.json.result._meta["mcp/www_authenticate"][0].includes('error="insufficient_scope"'));
    assert.equal(zulip.requestsTo("POST", "messages").length, before);
  });

  await step("recall without its secrets:  a read-only grant reaches recall_search, which says not_configured;  contribute needs zulip:write", async () => {
    const init = await mcp(tokens.access_token, "initialize", INIT);
    assert.match(init.json.result.instructions, /BEGIN_UNTRUSTED_RECALL and END_UNTRUSTED_RECALL/);
    const r = await call(tokens.access_token, "recall_search", { query: "agent-sync bridge" });
    assert.equal(r.json.result.isError, true, JSON.stringify(r.json));
    assert.equal(r.json.result._meta["agent-sync/error"].code, "not_configured");
    const w = await call(tokens.access_token, "recall_contribute", { text: "x".repeat(50), category: "lesson" });
    assert.equal(w.json.result.isError, true);
    assert.ok(w.json.result._meta["mcp/www_authenticate"][0].includes('scope="zulip:write"'));
    // The Zulip tools are untouched by recall's missing config.
    const who = await call(tokens.access_token, "whoami");
    assert.equal(who.json.result.structuredContent.seat, "JET");
  });

  await step("DMs with a read-only grant:  dm_list and dm_read answer fenced, dm_send names the zulip:write challenge and sends nothing", async () => {
    const list = await call(tokens.access_token, "dm_list");
    assert.equal(list.json.result.isError, undefined, JSON.stringify(list.json));
    assert.deepEqual(list.json.result.structuredContent, { count: 0, ids: [], unread_total: 0 });
    assert.match(list.json.result.content[0].text, /^BEGIN_UNTRUSTED_ZULIP nonce=[0-9a-f]{16}$/m);
    const read = await call(tokens.access_token, "dm_read", { user_ids: [OWNER_ID] });
    assert.deepEqual(read.json.result.structuredContent, { count: 0, ids: [], next_since_id: null });
    const before = zulip.requestsTo("POST", "messages").length;
    const send = await call(tokens.access_token, "dm_send", { user_ids: [OWNER_ID], text: "x" });
    assert.equal(send.json.result.isError, true);
    assert.equal(send.json.result.structuredContent, undefined);
    assert.ok(send.json.result._meta["mcp/www_authenticate"][0].includes('scope="zulip:write"'));
    assert.equal(zulip.requestsTo("POST", "messages").length, before);
    const bad = await call(tokens.access_token, "dm_read", {});
    assert.equal(bad.json.result._meta["agent-sync/error"].code, "invalid_argument");
  });

  await step("pause:  tools say paused, refresh is temporarily_unavailable, and the grant survives", async () => {
    assert.equal((await adminAction("pause", { seat: "JET" })).status, 303);
    const r = await call(tokens.access_token, "whoami");
    assert.equal(r.json.result._meta["agent-sync/error"].code, "paused");
    const t = await token({ grant_type: "refresh_token", refresh_token: tokens.refresh_token, client_id: CHATGPT_CLIENT });
    assert.equal(t.body.error, "temporarily_unavailable");
    assert.equal((await adminAction("unpause", { seat: "JET" })).status, 303);
    const t2 = await token({ grant_type: "refresh_token", refresh_token: tokens.refresh_token, client_id: CHATGPT_CLIENT });
    assert.equal(t2.status, 200, JSON.stringify(t2.body));
    tokens = t2.body;
  });

  await step("two parallel approvals leave one grant", async () => {
    assert.equal((await adminAction("arm", { seat: "JET" })).status, 303);
    const a = pkce();
    const b = pkce();
    const h1 = await openConsent(a.challenge, "st-a");
    const h2 = await openConsent(b.challenge, "st-b");
    const [r1, r2] = await Promise.all([
      browser("/authorize", { method: "POST", form: { handle: h1, seat: "JET", decision: "approve", scope: "zulip:write" } }),
      browser("/authorize", { method: "POST", form: { handle: h2, seat: "JET", decision: "approve", scope: "zulip:write" } }),
    ]);
    const statuses = [r1.status, r2.status].sort();
    assert.deepEqual(statuses, [302, 409]);
    const admin = await (await browser("/admin")).text();
    assert.equal((admin.match(/ChatGPT <small>/g) ?? []).length, 1, "exactly one grant for JET");
  });

  await step("the new grant replaced the old:  old tokens get 401 and old refresh gets invalid_grant", async () => {
    const r = await call(tokens.access_token, "whoami");
    assert.equal(r.status, 401);
    const t = await token({ grant_type: "refresh_token", refresh_token: tokens.refresh_token, client_id: CHATGPT_CLIENT });
    assert.equal(t.body.error, "invalid_grant");
  });

  await step("a reused consent handle and a cross-family seat are refused", async () => {
    assert.equal((await adminAction("arm", { seat: "JET" })).status, 303);
    const h = await openConsent(pkce().challenge, "st-x");
    const cross = await browser("/authorize", { method: "POST", form: { handle: h, seat: "GROK-WEB", decision: "approve", scope: "zulip:read" } });
    assert.equal(cross.status, 400);
    assert.match(await cross.text(), /That seat cannot be bound to this app/);
    const reused = await browser("/authorize", { method: "POST", form: { handle: h, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(reused.status, 400);
    assert.match(await reused.text(), /This Page Expired/);
    assert.equal((await adminAction("disarm", { seat: "JET" })).status, 303);
  });

  await step("deny sends access_denied with iss and state, and closes the window", async () => {
    assert.equal((await adminAction("arm", { seat: "JET" })).status, 303);
    const handle = await openConsent(pkce().challenge, "st-deny");
    const res = await browser("/authorize", { method: "POST", form: { handle, seat: "JET", decision: "deny" } });
    assert.equal(res.status, 302);
    const loc = new URL(res.headers.get("location"));
    assert.equal(`${loc.origin}${loc.pathname}`, CHATGPT_REDIRECT);
    assert.equal(loc.searchParams.get("error"), "access_denied");
    assert.equal(loc.searchParams.get("iss"), issuer);
    assert.equal(loc.searchParams.get("state"), "st-deny");
    assert.equal(res.headers.get("referrer-policy"), "same-origin");
    assert.match(await (await browser("/admin")).text(), /<strong>JET<\/strong><\/td><td>Not armed/);
  });

  await step("an expired arming window refuses the approval with 409 and leaves the epoch alone", async () => {
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "agent-sync-mcp");
    const jet = ns.get(ns.idFromName("JET"));
    const before = (await jet.getState()).epoch;
    await jet.arm({ by: "test", ttlMs: 2500 });
    const handle = await openConsent(pkce().challenge, "st-late");
    await new Promise((resolve) => setTimeout(resolve, 2700));
    const res = await browser("/authorize", { method: "POST", form: { handle, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 409);
    assert.match(await res.text(), /Arming Window Closed/);
    assert.equal((await jet.getState()).epoch, before);
  });

  await step("a transient client-document failure while naming the client does not burn the arming window", async () => {
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "agent-sync-mcp");
    const jet = ns.get(ns.idFromName("JET"));
    assert.equal((await adminAction("arm", { seat: "JET" })).status, 303);
    const before = (await jet.getState()).epoch;
    const handle = await openConsent(pkce().challenge, "st-flaky");
    failChatgptCimd = 1;
    const res = await browser("/authorize", { method: "POST", form: { handle, seat: "JET", decision: "approve", scope: "zulip:read" } });
    failChatgptCimd = 0;
    assert.equal(res.status, 302, await res.clone().text());
    assert.equal((await jet.getState()).epoch, before + 1, "approved exactly once");
  });

  await step("an epoch bump alone gives 401 on /mcp and invalid_grant on refresh", async () => {
    assert.equal((await adminAction("arm", { seat: "JET" })).status, 303);
    const p = pkce();
    const h = await openConsent(p.challenge, "st-e");
    const res = await browser("/authorize", { method: "POST", form: { handle: h, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302);
    const c = new URL(res.headers.get("location")).searchParams.get("code");
    const t = await token({ grant_type: "authorization_code", code: c, code_verifier: p.verifier, redirect_uri: CHATGPT_REDIRECT, client_id: CHATGPT_CLIENT, resource: RESOURCE });
    assert.equal(t.status, 200);
    assert.equal(await seatOf(t.body.access_token), "JET");
    // Bump the epoch directly in the Durable Object, leaving the KV grant in place.
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "agent-sync-mcp");
    await ns.get(ns.idFromName("JET")).bumpEpoch({ by: "test", reason: "epoch_only" });
    const dead = await call(t.body.access_token, "whoami");
    assert.equal(dead.status, 401);
    assert.match(dead.headers.get("www-authenticate"), /error="invalid_token"/);
    const r = await token({ grant_type: "refresh_token", refresh_token: t.body.refresh_token, client_id: CHATGPT_CLIENT });
    assert.equal(r.body.error, "invalid_grant");
  });

  await step("revoke all and bump epoch kills the live grant", async () => {
    assert.equal((await adminAction("revoke", { seat: "JET" })).status, 303);
    const admin = await (await browser("/admin")).text();
    assert.equal((admin.match(/ChatGPT <small>/g) ?? []).length, 0);
  });

  let grokCimdTokens;
  let grokManualTokens;
  let grokManualClientId;
  await step("Grok connects through its published client metadata document and posts as GROK-WEB in #sandbox", async () => {
    // Unarmed GROK-WEB fetches nothing.
    outbound.length = 0;
    const who = { clientId: GROK_CLIENT, redirect: GROK_REDIRECT };
    const unarmed = await browser(`/authorize?${authorizeQuery(pkce().challenge, "g-0", who)}`);
    assert.equal(unarmed.status, 403);
    await unarmed.arrayBuffer();
    assert.ok(!outbound.includes(GROK_CLIENT), "no CIMD fetch while GROK-WEB is unarmed");

    assert.equal((await adminAction("arm", { seat: "GROK-WEB" })).status, 303);
    const g = pkce();
    const page = await browser(`/authorize?${authorizeQuery(g.challenge, "g-1", who)}`);
    const html = await page.text();
    assert.equal(page.status, 200, html.slice(0, 300));
    assert.match(html, /Published by <strong>grok\.com<\/strong>/);
    assert.match(html, /name="seat" value="GROK-WEB"/);
    const handle = html.match(/name="handle" value="([^"]+)"/)[1];
    // ChatGPT's seat cannot be bound to Grok's request.
    const wrong = await browser("/authorize", { method: "POST", form: { handle, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(wrong.status, 400);
    await wrong.arrayBuffer();
    const again = await openConsent(g.challenge, "g-2", who);
    const res = await browser("/authorize", { method: "POST", form: [["handle", again], ["seat", "GROK-WEB"], ["decision", "approve"], ["scope", "zulip:read"], ["scope", "zulip:write"]] });
    assert.equal(res.status, 302, await res.clone().text());
    const loc = new URL(res.headers.get("location"));
    assert.equal(`${loc.origin}${loc.pathname}`, GROK_REDIRECT);
    assert.equal(loc.searchParams.get("iss"), issuer);
    // Grok's document says token_endpoint_auth_method none:  client_id in the form, no secret.
    const t = await token({ grant_type: "authorization_code", code: loc.searchParams.get("code"), code_verifier: g.verifier, redirect_uri: GROK_REDIRECT, client_id: GROK_CLIENT, resource: RESOURCE });
    assert.equal(t.status, 200, JSON.stringify(t.body));
    grokCimdTokens = t.body;
    assert.equal(await seatOf(t.body.access_token), "GROK-WEB");
    // No WAKE_HMAC_KEY_GROK_WEB, so no events:  Grok never lists an event that cannot fire.
    const grokDiscover = await mcpModern(t.body.access_token, "server/discover");
    assert.deepEqual(grokDiscover.json.result.capabilities, { tools: {} });
    const write = await call(t.body.access_token, "post", { channel: "sandbox", topic: "hosted smoke", text: "Hello from Grok.  Two sentences." });
    assert.equal(write.status, 200, JSON.stringify(write.json));
    const posted = zulip.messages.at(-1);
    assert.deepEqual(write.json.result.structuredContent, { id: posted.id, channel_id: STREAMS.sandbox, duplicate: false });
    assert.equal(posted.sender_email, "grok-web-bot@simplewithus.zulipchat.com", "posted with GROK-WEB's own key");
    assert.equal(posted.subject, "hosted smoke");
    assert.equal(posted.content, "[GROK-WEB] Hello from Grok.\u00a0 Two sentences.");
    const read = await call(t.body.access_token, "read_topic", { channel: "sandbox", topic: "hosted smoke", include_self: true });
    assert.deepEqual(read.json.result.structuredContent.ids, [posted.id]);
    assert.match(read.json.result.content[0].text, /^BEGIN_UNTRUSTED_ZULIP nonce=[0-9a-f]{16}$/m);
    const off = await call(t.body.access_token, "post", { channel: "general", topic: "t", text: "x" });
    assert.equal(off.json.result._meta["agent-sync/error"].code, "channel_not_allowed");
    // Direct messages through the Worker:  send as Grok's own bot, read the conversation back, see Jay's answer in inbox.
    const dmSent = await call(t.body.access_token, "dm_send", { user_ids: [OWNER_ID], text: "Direct note from Grok.  Please read." });
    assert.equal(dmSent.status, 200, JSON.stringify(dmSent.json));
    const dm = zulip.messages.at(-1);
    assert.deepEqual(dmSent.json.result.structuredContent, { id: dm.id, user_ids: [OWNER_ID], duplicate: false });
    assert.equal(dm.type, "private");
    assert.equal(dm.sender_email, "grok-web-bot@simplewithus.zulipchat.com");
    assert.equal(dm.content, "[GROK-WEB→JAY] Direct note from Grok.\u00a0 Please read.");
    const answer = zulip.addDm("Jay Wedgeworth", [], "Got it, thanks");
    const dmRead = await call(t.body.access_token, "dm_read", { emails: ["jay@zulip.test"] });
    assert.deepEqual(dmRead.json.result.structuredContent.ids, [dm.id, answer.id]);
    assert.match(dmRead.json.result.content[0].text, /^BEGIN_UNTRUSTED_ZULIP nonce=[0-9a-f]{16}$/m);
    const dmList = await call(t.body.access_token, "dm_list", {});
    assert.deepEqual(dmList.json.result.structuredContent, { count: 1, ids: [answer.id], unread_total: 1 });
    const inboxDm = await call(t.body.access_token, "inbox", {});
    assert.deepEqual(inboxDm.json.result.structuredContent.ids, [answer.id]);
    assert.match(inboxDm.json.result.content[0].text, /"dm":true/);
    // The admin page shows the call rows and the role check, never a body.
    const admin = await (await browser("/admin")).text();
    assert.match(admin, /role 400, checked/);
    assert.ok(admin.includes("hosted smoke") && !admin.includes("Two sentences") && !admin.includes("Direct note"));
    const refreshed = await token({ grant_type: "refresh_token", refresh_token: t.body.refresh_token, client_id: GROK_CLIENT });
    assert.equal(refreshed.status, 200, JSON.stringify(refreshed.body));
    grokCimdTokens = refreshed.body;
  });

  await step("Grok manual-form fallback:  a hand client with a blank secret field, and a foreign redirect is refused and logged", async () => {
    assert.equal((await adminAction("create_manual_client", { seat: "GROK-WEB" })).status, 303);
    const admin = await (await browser("/admin")).text();
    const clientId = admin.match(/<td><code>([A-Za-z0-9_-]{8,})<\/code><\/td><td>Grok \(manual form, GROK-WEB\)/)[1];
    grokManualClientId = clientId;
    assert.ok(admin.includes(GROK_REDIRECT), "the hand client takes the configured redirect, not a placeholder");
    assert.ok(!admin.includes("no-redirect-yet"));

    const foreign = new URLSearchParams({ response_type: "code", client_id: clientId, redirect_uri: "https://grok.com/oauth/callback", code_challenge: pkce().challenge, code_challenge_method: "S256", resource: RESOURCE, scope: "zulip:read" });
    const refused = await browser(`/authorize?${foreign}`);
    assert.equal(refused.status, 400);
    await refused.arrayBuffer();
    const after = await (await browser("/admin")).text();
    assert.ok(after.includes("https://grok.com/oauth/callback"), "the foreign redirect is in the refusal log");
    assert.ok(after.split("Refused Authorize Requests")[1].split("Refused Token Requests")[0].includes("mail@jays.services"), "with the Access email that made the attempt");

    // Positive round trip as a public client.  The token form carries `client_secret=` (blank), which the gate drops.
    assert.equal((await adminAction("arm", { seat: "GROK-WEB" })).status, 303);
    const g = pkce();
    const who = { clientId, redirect: GROK_REDIRECT };
    const page = await (await browser(`/authorize?${authorizeQuery(g.challenge, "g-m", who)}`)).text();
    assert.match(page, /Registered by hand/);
    const handle = page.match(/name="handle" value="([^"]+)"/)[1];
    const res = await browser("/authorize", { method: "POST", form: { handle, seat: "GROK-WEB", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302, await res.clone().text());
    const code = new URL(res.headers.get("location")).searchParams.get("code");
    const t = await token({ grant_type: "authorization_code", code, code_verifier: g.verifier, redirect_uri: GROK_REDIRECT, client_id: clientId, client_secret: "", resource: RESOURCE });
    assert.equal(t.status, 200, JSON.stringify(t.body));
    assert.equal(await seatOf(t.body.access_token), "GROK-WEB");
    // D6:  one grant per seat, so the document-based Grok grant is gone.
    const old = await call(grokCimdTokens.access_token, "whoami");
    assert.equal(old.status, 401);
    grokManualTokens = t.body;
  });

  await step("the real SDK client:  a tool error round-trips as isError, never a protocol failure", async () => {
    const transport = new StreamableHTTPClientTransport(new URL(`${HOST}/mcp`), {
      fetch: (url, init = {}) => {
        const headers = new Headers(init.headers);
        headers.set("Authorization", `Bearer ${grokManualTokens.access_token}`);
        return mf.dispatchFetch(String(url), { ...init, headers });
      },
    });
    const client = new Client({ name: "sdk-round-trip", version: "0" });
    await client.connect(transport);
    try {
      const listed = await client.listTools();
      assert.deepEqual(listed.tools.map((t) => t.name), HOSTED_NAMES);
      const refused = await client.callTool({ name: "read_topic", arguments: { channel: "general", topic: "t" } });
      assert.equal(refused.isError, true);
      assert.equal(JSON.parse(refused.content[0].text).code, "channel_not_allowed");
      const who = await client.callTool({ name: "whoami", arguments: {} });
      assert.equal(who.structuredContent.seat, "GROK-WEB");
    } finally {
      await client.close();
    }
  });

  await step("a refresh token replayed after rotation pauses the seat", async () => {
    const first = await token({ grant_type: "refresh_token", refresh_token: grokManualTokens.refresh_token, client_id: grokManualClientId });
    assert.equal(first.status, 200, JSON.stringify(first.body));
    const second = await token({ grant_type: "refresh_token", refresh_token: first.body.refresh_token, client_id: grokManualClientId });
    assert.equal(second.status, 200, JSON.stringify(second.body));
    // The original token is now two rotations old:  neither current nor previous.
    const replay = await token({ grant_type: "refresh_token", refresh_token: grokManualTokens.refresh_token, client_id: grokManualClientId });
    assert.equal(replay.body.error, "invalid_grant");
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "agent-sync-mcp");
    assert.equal((await ns.get(ns.idFromName("GROK-WEB")).getState()).paused, true);
    const r = await call(second.body.access_token, "whoami");
    assert.equal(r.json.result._meta["agent-sync/error"].code, "paused");
  });

  await step("ECHO and INSTINCT share one loopback callback:  the armed seat decides, and a client is bound to its seat", async () => {
    const LOOP = "http://127.0.0.1:8737/callback";
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "agent-sync-mcp");
    const echoGate = ns.get(ns.idFromName("ECHO"));
    const instinctGate = ns.get(ns.idFromName("INSTINCT"));
    assert.equal((await adminAction("create_manual_client", { seat: "ECHO" })).status, 303);
    assert.equal((await adminAction("create_manual_client", { seat: "INSTINCT" })).status, 303);
    const admin = await (await browser("/admin")).text();
    const echoClient = admin.match(/<td><code>([A-Za-z0-9_-]{8,})<\/code><\/td><td>Echo \(manual form, ECHO\)<\/td><td>ECHO<\/td><td>http:\/\/127\.0\.0\.1:8737\/callback</)[1];
    const instinctClient = admin.match(/<td><code>([A-Za-z0-9_-]{8,})<\/code><\/td><td>Instinct \(manual form, INSTINCT\)<\/td><td>INSTINCT</)[1];
    const refusalLog = async () => (await (await browser("/admin")).text()).split("Refused Authorize Requests")[1].split("Refused Token Requests")[0];
    const open = (clientId, state) => browser(`/authorize?${authorizeQuery(pkce().challenge, state, { clientId, redirect: LOOP })}`);

    // Neither armed:  nothing is expected.
    let res = await open(echoClient, "e-0");
    assert.match(await res.text(), /No Connection Expected/);
    assert.match(await refusalLog(), /not_armed<\/td><td><code>[A-Za-z0-9_-]+<\/code><\/td><td><code>http:\/\/127\.0\.0\.1:8737\/callback/);

    // Both armed:  refused as ambiguous, and logged.
    assert.equal((await adminAction("arm", { seat: "ECHO" })).status, 303);
    assert.equal((await adminAction("arm", { seat: "INSTINCT" })).status, 303);
    res = await open(echoClient, "e-both");
    assert.equal(res.status, 409);
    assert.match(await res.text(), /More Than One Seat Armed/);
    assert.match(await refusalLog(), /ambiguous_armed_seats/);
    assert.equal((await adminAction("disarm", { seat: "INSTINCT" })).status, 303);

    // Only ECHO armed:  INSTINCT's client cannot mint an ECHO grant.
    res = await open(instinctClient, "e-cross");
    assert.equal(res.status, 400);
    assert.match(await res.text(), /not bound to ECHO/);
    assert.match(await refusalLog(), /client_seat_mismatch/);

    // Only ECHO armed, ECHO's client:  consent names ECHO and the loopback target.
    const e = pkce();
    let page = await (await browser(`/authorize?${authorizeQuery(e.challenge, "e-ok", { clientId: echoClient, redirect: LOOP })}`)).text();
    assert.match(page, /as ECHO\?/);
    assert.match(page, /A program on the computer running this browser \(127\.0\.0\.1:8737\)/);
    let handle = page.match(/name="handle" value="([^"]+)"/)[1];
    // A tampered seat on the form is refused (the armed seat is ECHO).
    const tampered = await browser("/authorize", { method: "POST", form: { handle, seat: "INSTINCT", decision: "approve", scope: "zulip:read" } });
    assert.equal(tampered.status, 409, "the armed seat is ECHO, so an INSTINCT approval is refused");
    await tampered.arrayBuffer();
    assert.equal((await echoGate.getState()).armed, true, "a refused consent does not spend the window");
    page = await (await browser(`/authorize?${authorizeQuery(e.challenge, "e-ok2", { clientId: echoClient, redirect: LOOP })}`)).text();
    handle = page.match(/name="handle" value="([^"]+)"/)[1];
    res = await browser("/authorize", { method: "POST", form: { handle, seat: "ECHO", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302, await res.clone().text());
    let loc = new URL(res.headers.get("location"));
    assert.equal(`${loc.origin}${loc.pathname}`, LOOP);
    let t = await token({ grant_type: "authorization_code", code: loc.searchParams.get("code"), code_verifier: e.verifier, redirect_uri: LOOP, client_id: echoClient, resource: RESOURCE });
    assert.equal(t.status, 200, JSON.stringify(Object.keys(t.body)));
    assert.ok(t.body.refresh_token.startsWith("ECHO:"), "the grant belongs to ECHO");

    // INSTINCT armed, consent opened, then ECHO armed too:  the approval is re-resolved and refused.
    assert.equal((await adminAction("arm", { seat: "INSTINCT" })).status, 303);
    const i = pkce();
    page = await (await browser(`/authorize?${authorizeQuery(i.challenge, "i-1", { clientId: instinctClient, redirect: LOOP })}`)).text();
    assert.match(page, /as INSTINCT\?/);
    handle = page.match(/name="handle" value="([^"]+)"/)[1];
    await echoGate.arm({ by: "test" });
    res = await browser("/authorize", { method: "POST", form: { handle, seat: "INSTINCT", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 409);
    await res.arrayBuffer();
    assert.equal((await instinctGate.getState()).armed, true, "nothing was spent");
    await echoGate.disarm({ by: "test", reason: "test" });

    // Only INSTINCT armed:  INSTINCT's grant.
    page = await (await browser(`/authorize?${authorizeQuery(i.challenge, "i-2", { clientId: instinctClient, redirect: LOOP })}`)).text();
    handle = page.match(/name="handle" value="([^"]+)"/)[1];
    res = await browser("/authorize", { method: "POST", form: { handle, seat: "INSTINCT", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302, await res.clone().text());
    loc = new URL(res.headers.get("location"));
    t = await token({ grant_type: "authorization_code", code: loc.searchParams.get("code"), code_verifier: i.verifier, redirect_uri: LOOP, client_id: instinctClient, resource: RESOURCE });
    assert.equal(t.status, 200, JSON.stringify(Object.keys(t.body)));
    assert.ok(t.body.refresh_token.startsWith("INSTINCT:"), "the grant belongs to INSTINCT");
    // Another port on the loopback host is not the allowlisted callback.
    res = await browser(`/authorize?${authorizeQuery(pkce().challenge, "i-port", { clientId: instinctClient, redirect: "http://127.0.0.1:9999/callback" })}`);
    assert.equal(res.status, 400);
    await res.arrayBuffer();
  });

  console.log(`\n${passed} passed`);
} finally {
  await mf.dispose();
}
