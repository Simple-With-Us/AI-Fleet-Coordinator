// One workerd integration test (spec 6, Worker suite, Phase 0 subset).
// Runs the bundled Worker in Miniflare (from the pinned wrangler) with fake
// Access certificates and ChatGPT's real CIMD document served by a mocked
// outbound fetch, then walks the whole Phase 0 flow.
//
//   npm ci --ignore-scripts && npm run test:workerd
//
// It needs node_modules, so CI runs only the plain `node --test` suites.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import path from "node:path";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import TOOLS from "../src/tools.phase0.json" with { type: "json" };
import { ROOT, wranglerConfig, testEnv, CHATGPT_CLIENT, CHATGPT_REDIRECT, RESOURCE } from "./helpers.mjs";

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

const outbound = [];
async function outboundFetch(request) {
  outbound.push(request.url);
  if (request.url === `https://${TEAM}/cdn-cgi/access/certs`) return Response.json(jwks);
  if (request.url === CHATGPT_CLIENT) return Response.json(CHATGPT_CIMD);
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
        bindings: testEnv({ ACCESS_AUD: AUD }),
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
      for (const url of [`${HOST}/.well-known/oauth-authorization-server`, "https://evil.example/mcp", "https://agent-sync-mcp.example.workers.dev/.well-known/oauth-authorization-server"]) {
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
function remember(res) {
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
  if (method === "POST" && origin) Object.assign(headers, { Origin: HOST, "Sec-Fetch-Site": "same-origin" });
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

function authorizeQuery(challenge, state = "st-1") {
  return new URLSearchParams({
    response_type: "code",
    client_id: CHATGPT_CLIENT,
    redirect_uri: CHATGPT_REDIRECT,
    code_challenge: challenge,
    code_challenge_method: "S256",
    resource: RESOURCE,
    scope: "zulip:read zulip:write",
    state,
  }).toString();
}

async function openConsent(challenge, state) {
  const res = await browser(`/authorize?${authorizeQuery(challenge, state)}`);
  const html = await res.text();
  assert.equal(res.status, 200, html.slice(0, 300));
  return html.match(/name="handle" value="([^"]+)"/)[1];
}

async function token(form) {
  const res = await mf.dispatchFetch(`${HOST}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
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

const INIT = { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "phase0-test", version: "0" } };

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
    const res = await browser("/authorize", { method: "POST", form: { handle, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302, await res.clone().text());
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

  await step("tools/list matches tools.phase0.json, and hello shows JET", async () => {
    const init = await mcp(tokens.access_token, "initialize", INIT);
    assert.equal(init.status, 200, JSON.stringify(init.json));
    assert.match(init.json.result.instructions, /one bot, JET\./);
    const list = await mcp(tokens.access_token, "tools/list");
    assert.equal(list.status, 200);
    const tools = list.json.result.tools;
    assert.deepEqual(tools.map((t) => t.name), TOOLS.map((t) => t.name));
    for (const t of TOOLS) {
      const got = tools.find((x) => x.name === t.name);
      assert.deepEqual(got.inputSchema, t.inputSchema, `${t.name} inputSchema`);
      assert.deepEqual(got.annotations, t.annotations, `${t.name} annotations`);
    }
    const hello = await mcp(tokens.access_token, "tools/call", { name: "hello", arguments: {} });
    assert.deepEqual(hello.json.result.structuredContent, { seat: "JET", scopes: ["zulip:read"], client_id: CHATGPT_CLIENT });
  });

  await step("a seat argument is a validation error, and a missing scope names the challenge", async () => {
    const r = await mcp(tokens.access_token, "tools/call", { name: "hello", arguments: { seat: "GROK-WEB" } });
    assert.ok(r.json.error || r.json.result?.isError, JSON.stringify(r.json));
    const w2 = await mcp(tokens.access_token, "tools/call", { name: "hello_write", arguments: { note: "x" } });
    assert.equal(w2.json.result.isError, true);
    assert.equal(w2.json.result.structuredContent, undefined);
    assert.ok(w2.json.result._meta["mcp/www_authenticate"][0].includes('error="insufficient_scope"'));
  });

  await step("pause:  tools say paused, refresh is temporarily_unavailable, and the grant survives", async () => {
    assert.equal((await adminAction("pause", { seat: "JET" })).status, 303);
    const r = await mcp(tokens.access_token, "tools/call", { name: "hello", arguments: {} });
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
    const r = await mcp(tokens.access_token, "tools/call", { name: "hello", arguments: {} });
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

  await step("an epoch bump alone gives 401 on /mcp and invalid_grant on refresh", async () => {
    assert.equal((await adminAction("arm", { seat: "JET" })).status, 303);
    const p = pkce();
    const h = await openConsent(p.challenge, "st-e");
    const res = await browser("/authorize", { method: "POST", form: { handle: h, seat: "JET", decision: "approve", scope: "zulip:read" } });
    assert.equal(res.status, 302);
    const c = new URL(res.headers.get("location")).searchParams.get("code");
    const t = await token({ grant_type: "authorization_code", code: c, code_verifier: p.verifier, redirect_uri: CHATGPT_REDIRECT, client_id: CHATGPT_CLIENT, resource: RESOURCE });
    assert.equal(t.status, 200);
    const ok = await mcp(t.body.access_token, "tools/call", { name: "hello", arguments: {} });
    assert.equal(ok.json.result.structuredContent.seat, "JET");
    // Bump the epoch directly in the Durable Object, leaving the KV grant in place.
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE", "agent-sync-mcp");
    await ns.get(ns.idFromName("JET")).bumpEpoch({ by: "test", reason: "epoch_only" });
    const dead = await mcp(t.body.access_token, "tools/call", { name: "hello", arguments: {} });
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

  await step("Grok manual client:  created with a placeholder, first attempt refused and logged", async () => {
    assert.equal((await adminAction("create_grok_client")).status, 303);
    const admin = await (await browser("/admin")).text();
    const clientId = admin.match(/<td><code>([A-Za-z0-9_-]{8,})<\/code><\/td><td>Grok \(manual form, GROK-WEB\)/)[1];
    const q = new URLSearchParams({ response_type: "code", client_id: clientId, redirect_uri: "https://grok.com/oauth/callback", code_challenge: pkce().challenge, code_challenge_method: "S256", resource: RESOURCE, scope: "zulip:read" });
    const res = await browser(`/authorize?${q}`);
    assert.equal(res.status, 400);
    await res.arrayBuffer();
    const after = await (await browser("/admin")).text();
    assert.ok(after.includes("https://grok.com/oauth/callback"), "Grok's redirect is in the refusal log");
  });

  console.log(`\n${passed} passed`);
} finally {
  await mf.dispose();
}
