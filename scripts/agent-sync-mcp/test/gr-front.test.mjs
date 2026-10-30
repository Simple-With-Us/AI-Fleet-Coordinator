// The GR-MCP front (src/gr-tools.js + the HostedTools wiring):  a pinned
// upstream, Access service-token headers, the caller's bearer passed through,
// and fail-closed behaviour when the front is not configured.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { grUpstreamUrl, grClientFromResolved, GrRelayClient, GR_SECRETS, GR_TOOLS } from "../src/gr-tools.js";
import { resolveGrSecrets } from "../src/gr-tools.js";
import { resolveGrVerifyKey, GR_VERIFY_KEY_VAR } from "../src/gr-verify.js";
import { INFISICAL_EXPECT_DEFAULT } from "../src/infisical-manifest.js";
import { _resetInfisicalForTests } from "../src/infisical.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());

test("grUpstreamUrl pins a bare https origin and refuses everything else", () => {
  assert.equal(grUpstreamUrl("https://git-relay.example.test"), "https://git-relay.example.test");
  assert.equal(grUpstreamUrl("https://git-relay.example.test/"), "https://git-relay.example.test");
  for (const bad of ["", null, undefined, "http://git-relay.example.test", "https://git-relay.example.test/v1", "https://user:pw@git-relay.example.test", "https://git-relay.example.test?x=1", "not a url"]) {
    assert.equal(grUpstreamUrl(bad), null, JSON.stringify(bad));
  }
});

test("grClientFromResolved needs url, both Access credentials and a bearer", () => {
  const env = { GR_UPSTREAM_URL: "https://git-relay.example.test" };
  const pair = { accessId: "id", accessSecret: "secret" };
  assert.ok(grClientFromResolved(pair, env, { bearer: "gbb_fixer_abc" }));
  assert.equal(grClientFromResolved({ accessId: "", accessSecret: "secret" }, env, { bearer: "gbb_fixer_abc" }), null);
  assert.equal(grClientFromResolved(pair, env, { bearer: "" }), null);
  assert.equal(grClientFromResolved(pair, env, {}), null);
  assert.equal(grClientFromResolved(pair, {}, { bearer: "gbb_fixer_abc" }), null);
});

test("GrRelayClient posts the action with Access headers and the caller's bearer", async () => {
  const seen = [];
  const client = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "id", accessSecret: "secret", bearer: "sbt_jet-chat_abc",
    fetch: async (url, init) => {
      seen.push({ url, init });
      return new Response(JSON.stringify({ number: 7, url: "https://x/pr/7" }), { status: 200 });
    },
  });
  const { status, body } = await client.call("open_pr", { repo: "a/b", base: "main", head: "f", title: "t", body: "b" });
  assert.equal(status, 200);
  assert.equal(body.number, 7);
  assert.equal(seen[0].url, "https://git-relay.example.test/v1/open_pr");
  assert.equal(seen[0].init.headers["CF-Access-Client-Id"], "id");
  assert.equal(seen[0].init.headers["CF-Access-Client-Secret"], "secret");
  assert.equal(seen[0].init.headers["Authorization"], "Bearer sbt_jet-chat_abc");
  assert.deepEqual(client.secrets(), ["id", "secret", "sbt_jet-chat_abc"]);
});

test("GrRelayClient maps an unreachable upstream and a bad answer to fixed statuses", async () => {
  const down = new GrRelayClient({ baseUrl: "https://git-relay.example.test", accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43), fetch: async () => { throw new Error("ECONNREFUSED"); } });
  assert.equal((await down.call("open_pr", {})).status, 0);
  const garbage = new GrRelayClient({ baseUrl: "https://git-relay.example.test", accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43), fetch: async () => new Response("not json", { status: 200 }) });
  assert.equal((await garbage.call("open_pr", {})).status, 502);
  const refused = new GrRelayClient({ baseUrl: "https://git-relay.example.test", accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43), fetch: async () => new Response(JSON.stringify({ error: "policy_denied" }), { status: 403 }) });
  const { status, body } = await refused.call("merge_pr", {});
  assert.equal(status, 403);
  assert.equal(body.error, "policy_denied");
  const midbody = new GrRelayClient({ baseUrl: "https://git-relay.example.test", accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43), fetch: async () => ({ status: 200, text: async () => { throw new Error("stream reset"); } }) });
  const cut = await midbody.call("open_pr", {});
  assert.equal(cut.status, 0);
  assert.equal(cut.body.error, "upstream_unreachable");
});

test("a minter-minted assertion that echoes back in any response is refused", async () => {
  let assertion = "";
  const echoing = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef",
    minter: async () => { assertion = "gra2_" + "x".repeat(80); return assertion; },
    fetch: async () => new Response(JSON.stringify({ echoed: assertion }), { status: 200 }),
  });
  const echoed = await echoing.call("open_pr", {});
  assert.equal(echoed.status, 502);
  assert.equal(echoed.body.error, "upstream_echo");
  // A clean answer passes with the minted assertion sent as the bearer.
  const seen = [];
  const clean = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef",
    minter: async ({ action, body }) => `gra2_${action}_${"y".repeat(80)}`,
    fetch: async (url, init) => { seen.push(init.headers["Authorization"]); return new Response(JSON.stringify({ ok: true }), { status: 200 }); },
  });
  const ok = await clean.call("open_pr", { repo: "a/b" });
  assert.equal(ok.status, 200);
  assert.match(seen[0], /^Bearer gra2_open_pr_/);
});

test("an interleaved call's assertion in a response is refused by the family fallback", async () => {
  // Call B's response echoes call A's token:  B's request-scoped guard cannot
  // name A's token, so the assertion FAMILY pattern is what refuses it.  No
  // history of prior mints is retained or needed.
  const others = "gra2_" + "q".repeat(60) + "_" + "0".repeat(64);
  const client = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef",
    minter: async () => "gra2_" + "r".repeat(60) + "_" + "1".repeat(64),
    fetch: async () => new Response(JSON.stringify({ note: `saw ${others}` }), { status: 200 }),
  });
  const r = await client.call("open_pr", {});
  assert.equal(r.status, 502);
  assert.equal(r.body.error, "upstream_echo");
});

test("scrubbing never depends on history:  100 in-flight mints each guard their own token", async () => {
  let n = 0;
  const client = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef",
    minter: async () => `gra2_${String(++n).padStart(61, "m")}_` + "2".repeat(64),
    fetch: async (url, init) => {
      const mine = init.headers["Authorization"].slice("Bearer ".length);
      // Every call's own token echoes back:  each must be refused no matter
      // how many other mints happened around it.
      return new Response(JSON.stringify({ mine }), { status: 200 });
    },
  });
  const results = await Promise.all(Array.from({ length: 100 }, () => client.call("open_pr", {})));
  for (const r of results) {
    assert.equal(r.status, 502);
    assert.equal(r.body.error, "upstream_echo");
  }
  assert.equal(n, 100);
});

test("a minter that cannot mint fails the call closed, never unauthenticated", async () => {
  let calls = 0;
  const client = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef",
    minter: async () => null,
    fetch: async () => { calls++; return new Response("{}", { status: 200 }); },
  });
  const r = await client.call("open_pr", {});
  assert.equal(r.status, 503);
  assert.equal(r.body.error, "not_configured");
  assert.equal(calls, 0);
});

/** A minimal HostedTools over an in-memory gate, mirroring hosted-tools.test.mjs. */
function memoryGate(clock) {
  const store = S.memoryStore();
  const g = { store };
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) {
    g[name] = (options) => S[name](store, options, clock.now);
  }
  g.roleGet = () => S.roleGet(store, clock.now);
  g.callTail = (n) => S.callTail(store, n);
  return g;
}

function grHarness(gr) {
  const clock = { now: Date.parse("2026-10-25T14:00:00Z") };
  return new HostedTools({
    seat: "JET-CHAT",
    scopes: ["zulip:read", "zulip:write"],
    clientId: "sbt-bearer",
    grantRef: "abc123",
    paused: false,
    config,
    zulip: null,
    key: "",
    gate: memoryGate(clock),
    cf: {},
    gr,
    now: () => clock.now,
    sleep: async () => {},
    log: () => {},
  });
}

test("gr_open_pr runs through the front and returns the relay's answer", async () => {
  const seen = [];
  const gr = new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43),
    fetch: async (url, init) => { seen.push({ url, body: JSON.parse(init.body) }); return new Response(JSON.stringify({ number: 9, url: "u" }), { status: 200 }); },
  });
  const tools = grHarness(gr);
  const result = await tools.call("gr_open_pr", { repo: "a/b", base: "main", head: "f", title: "t", body: "b" });
  assert.ok(!result.isError, `expected success, got ${result.content?.[0]?.text}`);
  assert.equal(seen[0].url, "https://git-relay.example.test/v1/open_pr");
  assert.equal(seen[0].body.repo, "a/b");
});

test("gr tools answer not_configured when the front is absent, and a relay 4xx surfaces as relay_refused", async () => {
  const bare = grHarness(null);
  const missing = await bare.call("gr_merge_pr", { repo: "a/b", number: 1 });
  assert.equal(missing.isError, true);
  assert.match(missing.content[0].text, /not configured/i);
  assert.match(missing.content[0].text, /GR_UPSTREAM_URL/);

  const refusing = new GrRelayClient({ baseUrl: "https://git-relay.example.test", accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43), fetch: async () => new Response(JSON.stringify({ error: "seat ceiling does not cover permission 'issues'" }), { status: 403 }) });
  const tools = grHarness(refusing);
  const denied = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(denied.isError, true);
  assert.match(denied.content[0].text, /seat ceiling/);
});

test("the seven gr tools are registered and mint_token is not among them", () => {
  assert.deepEqual(GR_TOOLS.map((t) => t.name), ["gr_open_pr", "gr_edit_pr", "gr_reply_thread", "gr_comment_pr", "gr_merge_pr", "gr_workflow_dispatch", "gr_resolve_thread"]);
  assert.ok(!GR_TOOLS.some((t) => t.name.includes("mint")));
  assert.equal(Object.keys(GR_SECRETS).length, 2);
});

test("gr_resolve_thread requires repo and thread_id: the relay validates repo on every action (kody 4372745715)", () => {
  const tool = GR_TOOLS.find((t) => t.name === "gr_resolve_thread");
  assert.deepEqual([...tool.inputSchema.required].sort(), ["repo", "thread_id"]);
  assert.ok(tool.inputSchema.properties.repo);
  assert.equal(tool.inputSchema.additionalProperties, false);
});

test("gr_edit_pr schema refuses a neither-field call at the boundary", () => {
  // Kody 4372745459:  an edit with neither title nor body can only fail
  // downstream;  the schema itself must not admit it.
  const edit = GR_TOOLS.find((t) => t.name === "gr_edit_pr");
  assert.equal(edit.inputSchema.additionalProperties, false);
  assert.deepEqual(edit.inputSchema.anyOf, [{ required: ["title"] }, { required: ["body"] }]);
  assert.deepEqual(edit.inputSchema.required, ["repo", "number"]);
});

test("gr_workflow_dispatch bounds inputs at the Worker boundary before any upstream call", async () => {
  // Kody 4372745946:  flat object, <=32 keys, scalar values <=1024 chars,
  // no unknown fields;  an over-limit call refuses as invalid_argument and
  // the relay never sees it.
  let upstream = 0;
  const tools = grHarness(
    new GrRelayClient({
      baseUrl: "https://git-relay.example.test",
      accessId: "test-access-id", accessSecret: "test-access-secret", bearer: "sbt_jet-chat_testbearer",
      fetch: async () => {
        upstream++;
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      },
    }),
  );
  const base = { repo: "a/b", workflow: "ci.yml", ref: "main" };
  const tooMany = { ...base, inputs: Object.fromEntries(Array.from({ length: 33 }, (_, i) => [`k${i}`, "v"])) };
  const tooLong = { ...base, inputs: { message: "x".repeat(1025) } };
  const nested = { ...base, inputs: { env: { nested: true } } };
  const unknown = { ...base, bogus: true };
  for (const args of [tooMany, tooLong, nested, unknown]) {
    const res = await tools.call("gr_workflow_dispatch", args);
    assert.equal(res.isError, true);
    assert.match(res.content[0].text, /invalid|argument|inputs/i);
  }
  assert.equal(upstream, 0, "no over-limit call reached the relay");
  const ok = await tools.call("gr_workflow_dispatch", { ...base, inputs: { message: "ship it", dry: true, count: 2 } });
  assert.equal(upstream, 1, "an in-limit call travels");
  assert.ok(!ok.isError, `expected success, got ${ok.content?.[0]?.text}`);
});

test("the Access pair hydrates from /gr-front, never /tools; the verify key stays at /tools", async () => {
  // Tailnet qualification for the /gr-front move:  both GR_ACCESS_* reads hit
  // the Worker-only folder;  the verify key's folder is unchanged.
  _resetInfisicalForTests();
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return new Response(JSON.stringify({ secret: { secretValue: "v" } }), { status: 200 });
  };
  const env = { INFISICAL_SERVICE_TOKEN: "st-test", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_MODE: "required" };
  const pair = await resolveGrSecrets(env);
  assert.deepEqual(pair, { accessId: "v", accessSecret: "v" });
  assert.equal(calls.length, 2, "exactly the two GR_ACCESS reads");
  for (const url of calls) {
    assert.ok(url.includes("secretPath=%2Fgr-front"), `expected /gr-front read, got ${url}`);
    assert.ok(!url.includes("secretPath=%2Ftools"), `the Access pair must never read /tools: ${url}`);
  }
  const names = calls.map((url) => decodeURIComponent(url));
  assert.ok(names.some((url) => url.includes(GR_SECRETS.accessId)) && names.some((url) => url.includes(GR_SECRETS.accessSecret)));
  _resetInfisicalForTests();
  calls.length = 0;
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    if (String(url).includes("/api/v3/secrets/raw/")) return new Response(JSON.stringify({ secret: { secretValue: "k" } }), { status: 200 });
    return new Response(JSON.stringify({ secrets: [{ secretKey: GR_VERIFY_KEY_VAR, secretValue: "k" }] }), { status: 200 });
  };
  await resolveGrVerifyKey(env);
  assert.ok(calls.length > 0);
  for (const url of calls) assert.ok(url.includes("secretPath=%2Ftools"), `verify key reads stay at /tools: ${url}`);
  _resetInfisicalForTests();
});

test("the Infisical manifest carries no Access-pair entries before provisioning", () => {
  // GR reads are optional by design:  an absent pair is capability-off, never
  // a hard error, so the manifest must NOT expect either GR_ACCESS name.
  for (const entry of INFISICAL_EXPECT_DEFAULT) {
    assert.ok(!entry.includes("GR_ACCESS"), `manifest must not expect the Access pair: ${entry}`);
    assert.ok(!entry.startsWith("/gr-front"), `manifest must not expect /gr-front entries: ${entry}`);
  }
});

test("grCall maps relay outcomes precisely and answers in the MCP result shape", async () => {
  // Realistic-length fake credentials:  one-letter fakes would trip the
  // error scrubber (hosted-tools scrub) and mangle the asserted text.
  const mk = (response) =>
    grHarness(
      new GrRelayClient({
        baseUrl: "https://git-relay.example.test",
        accessId: "test-access-id", accessSecret: "test-access-secret", bearer: "sbt_jet-chat_testbearer",
        fetch: async () => response,
      }),
    );
  // 200: text content plus structuredContent, like every other hosted tool (kody 4372746898)
  const okTools = mk(new Response(JSON.stringify({ number: 9, url: "u" }), { status: 200 }));
  const okRes = await okTools.call("gr_open_pr", { repo: "a/b", base: "main", head: "f", title: "t", body: "b" });
  assert.ok(!okRes.isError, `expected success, got ${okRes.content?.[0]?.text}`);
  assert.equal(okRes.content[0].type, "text");
  assert.deepEqual(okRes.structuredContent, { number: 9, url: "u" });
  // 502 with outcome "unknown": the write may have landed - never a refusal, never a blind retry (kody 4372747157)
  const unknownTools = mk(new Response(JSON.stringify({ error: "github call outcome unknown", outcome: "unknown", retryable: false }), { status: 502 }));
  const unknownRes = await unknownTools.call("gr_merge_pr", { repo: "a/b", number: 1 });
  assert.equal(unknownRes.isError, true);
  assert.match(unknownRes.content[0].text, /outcome is unknown/);
  assert.match(unknownRes.content[0].text, /Do not retry/);
  // 502 from an unreadable upstream (client-side) stays unavailable
  const garbledTools = mk(new Response("not json", { status: 200 }));
  const garbledRes = await garbledTools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "b" });
  assert.equal(garbledRes.isError, true);
  assert.match(garbledRes.content[0].text, /could not read/);
  // 503: retryable minting outage, not a refusal (kody 4372747157)
  const busyTools = mk(new Response(JSON.stringify({ error: "app token minting unavailable", retryable: true }), { status: 503 }));
  const busyRes = await busyTools.call("gr_edit_pr", { repo: "a/b", number: 1, title: "t" });
  assert.equal(busyRes.isError, true);
  assert.match(busyRes.content[0].text, /try again shortly/i);
  assert.doesNotMatch(busyRes.content[0].text, /refused/i);
  // Any other 5xx is outcome_unknown, never a refusal:  it can follow an
  // accepted mutation (kody 4372747157)
  for (const [status, body] of [
    [500, JSON.stringify({ error: "internal error" })],
    [504, JSON.stringify({ error: "gateway timeout" })],
  ]) {
    const t = mk(new Response(body, { status }));
    const r = await t.call("gr_merge_pr", { repo: "a/b", number: 1 });
    assert.equal(r.isError, true);
    assert.match(r.content[0].text, /outcome is unknown/, `status ${status}`);
    assert.match(r.content[0].text, /Do not retry/, `status ${status}`);
    assert.doesNotMatch(r.content[0].text, /refused/i, `status ${status}`);
  }
  // A real 5xx whose body cannot be read (edge HTML page) is likewise outcome
  // unknown - it may have followed an accepted write
  const edgeTools = mk(new Response("<html>Bad Gateway</html>", { status: 503 }));
  const edgeRes = await edgeTools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "b" });
  assert.equal(edgeRes.isError, true);
  assert.match(edgeRes.content[0].text, /outcome is unknown/);
  // A body outside the response envelope schema is a mis-answering upstream,
  // not a result (kody 4372745180)
  for (const raw of [JSON.stringify({ error: 42 }), JSON.stringify("just a string")]) {
    const t = mk(new Response(raw, { status: 200 }));
    const r = await t.call("gr_comment_pr", { repo: "a/b", number: 1, body: "b" });
    assert.equal(r.isError, true);
    assert.match(r.content[0].text, /could not read/, raw);
  }
  // A non-object success body is a mis-answering upstream, not a result
  const arrayTools = mk(new Response(JSON.stringify([1, 2]), { status: 200 }));
  const arrayRes = await arrayTools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "b" });
  assert.equal(arrayRes.isError, true);
  assert.match(arrayRes.content[0].text, /could not read/);
});
