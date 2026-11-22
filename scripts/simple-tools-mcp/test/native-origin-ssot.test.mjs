import test from "node:test";
import assert from "node:assert/strict";
import { randomBytes, webcrypto, createHmac } from "node:crypto";
import { nativeOriginResolverFromSecrets } from "../src/native-origin-client.js";
import { _resetInfisicalForTests } from "../src/infisical.js";

globalThis.crypto ??= webcrypto;
const first = "019a1234-abcd-7123-8123-0123456789ab";
const second = "019a1234-abcd-7123-8123-0123456789ac";
const endpoint = "https://operator.example.test/registry/v1/review-origin";
const names = ["REVIEW_ORIGIN_READ_URL", "REVIEW_ORIGIN_CODEX_READ_KEY"];
const context = { seat: "CODEX", session: first };
const envFor = (mode = "required") => ({ INFISICAL_MODE: mode,
  INFISICAL_SERVICE_TOKEN: randomBytes(24).toString("hex"),
  INFISICAL_WORKSPACE_ID: "fixture-fleet-ops", INFISICAL_CACHE_TTL_S: "10" });

async function isolated(run) {
  const original = globalThis.fetch;
  _resetInfisicalForTests();
  try { await run(); }
  finally { globalThis.fetch = original; _resetInfisicalForTests(); }
}

test("origin secrets are lazy and never read for a different seat", async () => isolated(async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("unexpected-fetch"); };
  const resolver = nativeOriginResolverFromSecrets(envFor());
  assert.equal(calls, 0);
  await assert.rejects(resolver({ ...context, seat: "WISE" }));
  assert.equal(calls, 0);
}));

test("origin reads the fixed existing store and signs each exact invocation, cached without CF copies", async () => isolated(async () => {
  const key = randomBytes(32).toString("hex");
  const env = { ...envFor(), REVIEW_ORIGIN_READ_URL: "https://wrong.example.test/registry/v1/review-origin",
    REVIEW_ORIGIN_CODEX_READ_KEY: randomBytes(32).toString("hex") };
  const reads = [], projections = [];
  globalThis.fetch = async (input, init) => {
    const url = new URL(input);
    assert.equal(init.redirect, "manual");
    if (url.origin === "https://app.infisical.com") {
      assert.equal(init.method ?? "GET", "GET");
      assert.equal(url.searchParams.get("workspaceId"), env.INFISICAL_WORKSPACE_ID);
      assert.equal(url.searchParams.get("environment"), "prod");
      assert.equal(url.searchParams.get("secretPath"), "/zulip");
      const name = decodeURIComponent(url.pathname.split("/").at(-1));
      assert.ok(names.includes(name));
      reads.push(name);
      return Response.json({ secret: { secretValue: name === names[0] ? endpoint : key } });
    }
    assert.equal(url.origin + url.pathname, endpoint);
    assert.equal(init.method, "GET");
    const stamp = init.headers["X-Simple-Tools-Timestamp"];
    const signed = `simple-tools-review-origin/1\nGET\n${url.pathname}\n${url.search.slice(1)}\n${stamp}`;
    assert.equal(init.headers["X-Simple-Tools-Signature"], createHmac("sha256", key).update(signed).digest("hex"));
    const session = url.searchParams.get("native_session_id");
    projections.push(session);
    return Response.json({ origin_session_claim: session, origin_letter_claim: "AA", origin_generation_claim: 7 });
  };
  const resolver = nativeOriginResolverFromSecrets(env);
  for (const session of [first, second]) {
    const result = await resolver({ seat: "CODEX", session });
    assert.equal(result.origin_session_claim, session);
  }
  assert.deepEqual(reads.sort(), [...names].sort());
  assert.deepEqual(projections, [first, second]);
}));

test("off mode cannot use a direct Worker copy", async () => isolated(async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("unexpected-fetch"); };
  const resolver = nativeOriginResolverFromSecrets({ ...envFor("off"),
    REVIEW_ORIGIN_READ_URL: endpoint, REVIEW_ORIGIN_CODEX_READ_KEY: randomBytes(32).toString("hex") });
  await assert.rejects(resolver(context));
  assert.equal(calls, 0);
}));

test("missing or refused source never falls through to a direct Worker copy", async () => {
  for (const mode of ["fallback", "required"]) for (const status of [404, 403, 503]) {
    await isolated(async () => {
      let projectionCalls = 0;
      globalThis.fetch = async (input) => {
        if (new URL(input).origin !== "https://app.infisical.com") projectionCalls++;
        return new Response("held", { status });
      };
      const resolver = nativeOriginResolverFromSecrets({ ...envFor(mode),
        REVIEW_ORIGIN_READ_URL: endpoint, REVIEW_ORIGIN_CODEX_READ_KEY: randomBytes(32).toString("hex") });
      await assert.rejects(resolver(context));
      assert.equal(projectionCalls, 0);
    });
  }
});

test("unsafe or incomplete store configuration holds before projection", async () => {
  for (const values of [["http://operator.example.test/registry/v1/review-origin", randomBytes(32).toString("hex")],
    [endpoint + "?key=forbidden", randomBytes(32).toString("hex")], [endpoint, "short"]]) {
    await isolated(async () => {
      let projectionCalls = 0;
      globalThis.fetch = async (input) => {
        const url = new URL(input);
        if (url.origin !== "https://app.infisical.com") { projectionCalls++; throw new Error("unexpected-projection"); }
        const name = decodeURIComponent(url.pathname.split("/").at(-1));
        return Response.json({ secret: { secretValue: values[names.indexOf(name)] } });
      };
      await assert.rejects(nativeOriginResolverFromSecrets(envFor())(context));
      assert.equal(projectionCalls, 0);
    });
  }
});
