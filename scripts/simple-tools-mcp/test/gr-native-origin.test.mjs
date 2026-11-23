import test from "node:test";
import { webcrypto, randomBytes } from "node:crypto";
import { _resetInfisicalForTests } from "../src/infisical.js";
import { nativeOriginResolver } from "../src/native-origin-client.js";
globalThis.crypto ??= webcrypto;
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { GrRelayClient } from "../src/gr-tools.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

function harness() {
  const sent = [];
  const store = S.memoryStore();
  const gate = { store };
  const now = Date.parse("2026-11-21T23:00:00Z");
  for (const key of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) {
    gate[key] = (args) => S[key](store, args, now);
  }
  gate.roleGet = () => S.roleGet(store, now);
  const gr = new GrRelayClient({
    baseUrl: "https://git-relay.example.test", accessId: "test-access-id",
    accessSecret: "test-access-secret", bearer: "sbt_codex_testbearer",
    fetch: async (url, init) => {
      sent.push({ url: String(url), args: JSON.parse(init.body) });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });
  const tools = new HostedTools({
    seat: "CODEX", scopes: ["zulip:read", "zulip:write"], clientId: "sbt-bearer",
    grantRef: "abc123", paused: false, config: loadConfig(testEnv()), zulip: null,
    key: "", gate, cf: {}, gr, now: () => now, sleep: async () => {}, log: () => {},
  });
  return { tools, sent };
}
const base = { repo: "a/b", base: "main", head: "feature", title: "Change", body: "Test change." };
const claims = { origin_session_claim: "019a1234-abcd-7123-8123-0123456789ab", origin_letter_claim: "AA", origin_generation_claim: 7 };

test("native seat claims pass the full hosted schema and serialized relay request unchanged", async () => {
  const { tools, sent } = harness();
  const result = await tools.call("gr_open_pr", { ...base, ...claims });
  assert.ok(!result.isError, result.content?.[0]?.text);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "https://git-relay.example.test/v1/open_pr");
  for (const [key, value] of Object.entries(claims)) assert.equal(sent[0].args[key], value);
});

test("ordinary PR calls need no claims, and unrelated credential fields remain refused", async () => {
  const { tools, sent } = harness();
  assert.ok(!(await tools.call("gr_open_pr", base)).isError);
  assert.ok(!Object.keys(sent[0].args).some((key) => key.startsWith("origin_")));
  for (const over of [{ origin_session_token: "forbidden" }, { origin_letter_claim: "A" },
    { origin_generation_claim: 0 }, { origin_generation_claim: true },
    { origin_session_claim: "../wrong" }]) {
    const before = sent.length;
    assert.ok((await tools.call("gr_open_pr", { ...base, ...claims, ...over })).isError);
    assert.equal(sent.length, before);
  }
});


test("exact and invalid metadata cannot bypass unavailable projection using explicit claims", async () => {
  for (const invocation of [{ state: "exact", session: claims.origin_session_claim }, { state: "invalid", session: null }]) {
    const { tools, sent } = harness();
    assert.ok((await tools.call("gr_open_pr", { ...base, ...claims }, invocation)).isError);
    assert.equal(sent.length, 0);
  }
});

test("exact metadata snapshots projection and holds explicit mismatches", async () => {
  const { tools, sent } = harness();
  const original = globalThis.fetch;
  let reads = 0;
  _resetInfisicalForTests();
  const projectionKey = randomBytes(32).toString("hex");
  tools.workerEnv = { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "fixture-fleet-ops",
    INFISICAL_SERVICE_TOKEN: randomBytes(24).toString("hex") };
  globalThis.fetch = async (url, init) => {
    const parsed = new URL(url);
    if (parsed.origin === "https://app.infisical.com") {
      assert.equal(parsed.searchParams.get("secretPath"), "/zulip");
      const name = decodeURIComponent(parsed.pathname.split("/").at(-1));
      assert.ok(["REVIEW_ORIGIN_READ_URL", "REVIEW_ORIGIN_CODEX_READ_KEY"].includes(name));
      return Response.json({ secret: { secretValue: name === "REVIEW_ORIGIN_READ_URL"
        ? "https://operator.example.test/registry/v1/review-origin" : projectionKey } });
    }
    reads++;
    assert.equal(parsed.searchParams.get("native_session_id"), claims.origin_session_claim);
    assert.equal(init.redirect, "manual");
    assert.equal(init.headers["X-Simple-Tools-Signature"].length, 64);
    return Response.json(claims);
  };
  try {
    const context = { state: "exact", session: claims.origin_session_claim };
    assert.ok(!(await tools.call("gr_open_pr", base, context)).isError);
    for (const [key, value] of Object.entries(claims)) assert.equal(sent[0].args[key], value);
    for (const over of [{ origin_session_claim: "019a1234-abcd-7123-8123-0123456789ac" }, { origin_letter_claim: "BB" }, { origin_generation_claim: 8 }]) {
      assert.ok((await tools.call("gr_open_pr", { ...base, ...claims, ...over }, context)).isError);
    }
    assert.equal(reads, 4);
    assert.equal(sent.length, 1);
    globalThis.fetch = async () => new Response("held", { status: 503 });
    assert.ok((await tools.call("gr_open_pr", { ...base, ...claims }, context)).isError);
    assert.equal(sent.length, 1);
  } finally { globalThis.fetch = original; _resetInfisicalForTests(); }
});

test("projection client refuses unsafe endpoints, redirects, bad and large responses", async () => {
  const env = { REVIEW_ORIGIN_READ_URL: "https://operator.example.test/registry/v1/review-origin", REVIEW_ORIGIN_CODEX_READ_KEY: "test-only-".repeat(5) };
  for (const url of ["http://operator.example.test/registry/v1/review-origin", "https://operator.example.test/registry/v1/history", env.REVIEW_ORIGIN_READ_URL + "?key=x"]) {
    assert.equal(nativeOriginResolver({ ...env, REVIEW_ORIGIN_READ_URL: url }), null);
  }
  for (const response of [new Response("redirect", { status: 302 }), new Response("x".repeat(1025)), new Response("not-json")]) {
    const resolver = nativeOriginResolver(env, { fetcher: async () => response });
    await assert.rejects(resolver({ seat: "CODEX", session: claims.origin_session_claim }));
  }
});
