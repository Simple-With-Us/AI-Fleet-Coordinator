// Run after integration.workerd.mjs, against the same freshly built Worker.
// No OAuth fetch warms this request's provider helper before the callback.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { webcrypto } from "node:crypto";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { ROOT, testEnv } from "./helpers.mjs";
import { mintGrAssertion, sha256HexText } from "../src/gr-assert.js";
import { wakeSignature } from "../src/wake.js";

globalThis.crypto ??= webcrypto;
const channelKey = "fixture-channel-" + "k".repeat(40);
const signingKey = "s".repeat(48);
const origin = "https://mcp-fixture.example";
const script = readFileSync(process.env.GR_ASSERT_TEST_BUNDLE ?? path.join(ROOT, "dist/index.js"), "utf8")
  .replace(/var PUBLIC_HOST = [^;]+;/, 'var PUBLIC_HOST = "mcp-fixture.example";');
const envFor = (overrides) => testEnv({
  HOSTED_SEATS: "CODEX", EVENTS_ONLY_SEATS: "JET",
  ZULIP_EMAIL_CODEX: "codex-bot@simplewithus.zulipchat.com",
  INFISICAL_MODE: "required", INFISICAL_SERVICE_TOKEN: "fixture-broad",
  INFISICAL_WORKSPACE_ID: "fixture", INFISICAL_BASE_URL: "https://fixture-infisical.example",
  GR_ASSERT_INFISICAL_SERVICE_TOKEN: "fixture-isolated",
  ...overrides,
});
const start = (overrides) => new Miniflare(convertV4MiniflareOptions({
  upstream: origin,
  workers: [{
    name: "cold-gr-callback", modules: true, script,
    compatibilityDate: "2026-09-01", compatibilityFlags: ["nodejs_compat"],
    bindings: envFor(overrides), kvNamespaces: ["OAUTH_KV"],
    durableObjects: { SEAT_GATE: { className: "SeatGate", useSQLite: true } },
    outboundService: async (request) => {
      const url = new URL(request.url);
      assert.equal(url.hostname, "fixture-infisical.example");
      const rows = [
        { secretKey: "GR_BEARER_VERIFY_KEY", secretValue: channelKey },
        { secretKey: "GR_ASSERTION_SIGN_KEY", secretValue: signingKey },
      ];
      if (url.pathname.endsWith("/raw")) return Response.json({ secrets: rows, totalCount: rows.length });
      const secret = rows.find((row) => url.pathname.endsWith("/" + row.secretKey));
      return secret ? Response.json({ secret }) : new Response("absent", { status: 404 });
    },
  }],
}));
// The live-grant cache off, so every verify lists:  the grant checks themselves.
let mf = start({ GATE_IDENTITY_CACHE_MS: "0" });
try {
  const kv = await mf.getKVNamespace("OAUTH_KV");
  const grantId = "fixture-grant";
  await kv.put(`grant:CODEX:${grantId}`, JSON.stringify({
    id: grantId, userId: "CODEX", expiresAt: Math.floor(Date.now() / 1000) + 3600,
  }));
  const ns = await mf.getDurableObjectNamespace("SEAT_GATE");
  const gate = ns.get(ns.idFromName("CODEX"));
  await gate.arm({ by: "fixture" });
  const approved = await gate.approve({ by: "fixture" });
  const bodyHash = await sha256HexText("{}");
  const claims = {
    key: signingKey, seat: "CODEX", action: "wf_repo_get", bodySha256: bodyHash,
    scopes: ["zulip:read"], epoch: approved.epoch, approvedAt: Date.now(), phase: 2,
    grantRef: (await sha256HexText(grantId)).slice(0, 12),
  };
  const verify = async (token) => {
    const body = JSON.stringify({
      contract: "gr-assert-verify/1", token, action: "wf_repo_get",
      body_sha256: bodyHash, sent_at: Math.floor(Date.now() / 1000),
    });
    const response = await mf.dispatchFetch(origin + "/internal/gr-assert-verify", {
      method: "POST", headers: {
        Host: "mcp-fixture.example", "Content-Type": "application/json",
        "X-Simple-Tools-Signature": await wakeSignature(channelKey, new TextEncoder().encode(body)),
      }, body,
    });
    assert.equal(response.status, 200, await response.clone().text());
    return response.json();
  };
  const token = await mintGrAssertion(claims);
  assert.deepEqual(await verify(token), { ok: true, seat: "CODEX" });
  assert.deepEqual(await verify(token), { ok: false, reason: "replayed" });
  const fresh = await mintGrAssertion(claims);
  await kv.delete(`grant:CODEX:${grantId}`);
  assert.deepEqual(await verify(fresh), { ok: false, reason: "grant_invalid" });
  // A lookup miss must not burn the nonce. Restoring this fixture grant
  // allows the same assertion through its first successful verification.
  await kv.put(`grant:CODEX:${grantId}`, JSON.stringify({ id: grantId, userId: "CODEX" }));
  assert.deepEqual(await verify(fresh), { ok: true, seat: "CODEX" });
  console.log("Cold callback: live grant succeeds, replay refuses, missing grant preserves nonce.");
} finally {
  await mf.dispose();
}

// The default window (AFC-617):  a positive grant listing holds, so a deleted grant
// can lag, and the epoch check stays live on every call regardless.
mf = start({});
try {
  const kv = await mf.getKVNamespace("OAUTH_KV");
  const grantId = "fixture-grant";
  await kv.put(`grant:CODEX:${grantId}`, JSON.stringify({ id: grantId, userId: "CODEX", expiresAt: Math.floor(Date.now() / 1000) + 3600 }));
  const ns = await mf.getDurableObjectNamespace("SEAT_GATE");
  const gate = ns.get(ns.idFromName("CODEX"));
  await gate.arm({ by: "fixture" });
  const approved = await gate.approve({ by: "fixture" });
  const bodyHash = await sha256HexText("{}");
  const claims = {
    key: signingKey, seat: "CODEX", action: "wf_repo_get", bodySha256: bodyHash,
    scopes: ["zulip:read"], epoch: approved.epoch, approvedAt: Date.now(), phase: 2,
    grantRef: (await sha256HexText(grantId)).slice(0, 12),
  };
  const verify = async (token) => {
    const body = JSON.stringify({ contract: "gr-assert-verify/1", token, action: "wf_repo_get", body_sha256: bodyHash, sent_at: Math.floor(Date.now() / 1000) });
    const response = await mf.dispatchFetch(origin + "/internal/gr-assert-verify", {
      method: "POST",
      headers: { Host: "mcp-fixture.example", "Content-Type": "application/json", "X-Simple-Tools-Signature": await wakeSignature(channelKey, new TextEncoder().encode(body)) },
      body,
    });
    assert.equal(response.status, 200, await response.clone().text());
    return response.json();
  };
  assert.deepEqual(await verify(await mintGrAssertion(claims)), { ok: true, seat: "CODEX" });
  await kv.delete(`grant:CODEX:${grantId}`);
  assert.deepEqual(await verify(await mintGrAssertion(claims)), { ok: true, seat: "CODEX" }, "the listing is cached");
  await gate.bumpEpoch({ by: "fixture" });
  assert.deepEqual(await verify(await mintGrAssertion(claims)), { ok: false, reason: "grant_invalid" }, "the epoch check is immediate");
  console.log("Cached grant listing: no KV list on a repeat, and an epoch bump still refuses at once.");
} finally {
  await mf.dispose();
}

// The grant-listing budget through the real route:  past it a call that misses the
// positive cache answers 503, which the relay caches against that one assertion, and
// the jti stays unspent because the listing runs before the claim.  Two minutes' worth
// of tries, so a rollover in the middle cannot pass this by luck.
mf = start({ GATE_IDENTITY_CACHE_MS: "0" });
try {
  const kv = await mf.getKVNamespace("OAUTH_KV");
  const grantId = "fixture-grant";
  await kv.put(`grant:CODEX:${grantId}`, JSON.stringify({ id: grantId, userId: "CODEX", expiresAt: Math.floor(Date.now() / 1000) + 3600 }));
  const ns = await mf.getDurableObjectNamespace("SEAT_GATE");
  const gate = ns.get(ns.idFromName("CODEX"));
  await gate.arm({ by: "fixture" });
  const approved = await gate.approve({ by: "fixture" });
  const bodyHash = await sha256HexText("{}");
  let tripped = null;
  for (let i = 0; i <= 60 && !tripped; i++) {
    const token = await mintGrAssertion({
      key: signingKey, seat: "CODEX", action: "wf_repo_get", bodySha256: bodyHash,
      scopes: ["zulip:read"], epoch: approved.epoch, approvedAt: Date.now(), phase: 2,
      grantRef: (await sha256HexText(grantId)).slice(0, 12),
    });
    const body = JSON.stringify({ contract: "gr-assert-verify/1", token, action: "wf_repo_get", body_sha256: bodyHash, sent_at: Math.floor(Date.now() / 1000) });
    const response = await mf.dispatchFetch(origin + "/internal/gr-assert-verify", {
      method: "POST",
      headers: { Host: "mcp-fixture.example", "Content-Type": "application/json", "X-Simple-Tools-Signature": await wakeSignature(channelKey, new TextEncoder().encode(body)) },
      body,
    });
    if (response.status === 200) assert.equal((await response.json()).ok, true);
    else tripped = response;
  }
  assert.ok(tripped, "the grant-listing budget never tripped");
  assert.equal(tripped.status, 503);
  assert.equal(tripped.headers.get("retry-after"), "60");
  await tripped.text();
  console.log("Grant-listing budget: a call past it answers 503 on the assert route.");
} finally {
  await mf.dispose();
}

// The Git bearer verify cache through the real route.  The default, with
// GATE_IDENTITY_CACHE_MS at its compiled hour, verifies live;  only
// GR_VERIFY_CACHE_MS turns the window on, even with the identity cache off.
for (const [overrides, cached] of [[{}, false], [{ GR_VERIFY_CACHE_MS: "60000", GATE_IDENTITY_CACHE_MS: "0" }, true]]) {
  mf = start(overrides);
  try {
    const hash = await sha256HexText("fixture-bearer");
    const ns = await mf.getDurableObjectNamespace("SEAT_GATE");
    const gate = ns.get(ns.idFromName("CODEX"));
    await gate.gbBearerMint({ hash, by: "fixture", expiresAt: Date.now() + 600_000 });
    const verify = async () => {
      const body = JSON.stringify({ contract: "gr-bearer-verify/1", identity: "CODEX", hash, sent_at: Math.floor(Date.now() / 1000) });
      const response = await mf.dispatchFetch(origin + "/internal/gr-bearer-verify", {
        method: "POST",
        headers: { Host: "mcp-fixture.example", "Content-Type": "application/json", "X-Simple-Tools-Signature": await wakeSignature(channelKey, new TextEncoder().encode(body)) },
        body,
      });
      assert.equal(response.status, 200, await response.clone().text());
      return response.json();
    };
    assert.equal((await verify()).ok, true);
    await gate.setPaused({ paused: true, by: "fixture" });
    const paused = await verify();
    if (cached) assert.equal(paused.ok, true, "the window holds the positive answer");
    else assert.deepEqual(paused, { ok: false, reason: "paused" }, "off by default:  a pause bites on the next call");
  } finally {
    await mf.dispose();
  }
}
console.log("Git bearer verify: live by default, cached only by GR_VERIFY_CACHE_MS.");
