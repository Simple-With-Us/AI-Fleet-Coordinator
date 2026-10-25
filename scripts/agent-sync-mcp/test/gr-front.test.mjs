// The GR-MCP front (src/gr-tools.js + the HostedTools wiring):  a pinned
// upstream, Access service-token headers, the caller's bearer passed through,
// and fail-closed behaviour when the front is not configured.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { grUpstreamUrl, grClientFromResolved, GrRelayClient, GR_SECRETS, GR_TOOLS } from "../src/gr-tools.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());

test("grUpstreamUrl pins a bare https origin and refuses everything else", () => {
  assert.equal(grUpstreamUrl("https://git-relay.jays.services"), "https://git-relay.jays.services");
  assert.equal(grUpstreamUrl("https://git-relay.jays.services/"), "https://git-relay.jays.services");
  for (const bad of ["", null, undefined, "http://git-relay.jays.services", "https://git-relay.jays.services/v1", "https://user:pw@git-relay.jays.services", "https://git-relay.jays.services?x=1", "not a url"]) {
    assert.equal(grUpstreamUrl(bad), null, JSON.stringify(bad));
  }
});

test("grClientFromResolved needs url, both Access credentials and a bearer", () => {
  const env = { GR_UPSTREAM_URL: "https://git-relay.jays.services" };
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
    baseUrl: "https://git-relay.jays.services",
    accessId: "id", accessSecret: "secret", bearer: "sbt_jet-chat_abc",
    fetch: async (url, init) => {
      seen.push({ url, init });
      return new Response(JSON.stringify({ number: 7, url: "https://x/pr/7" }), { status: 200 });
    },
  });
  const { status, body } = await client.call("open_pr", { repo: "a/b", base: "main", head: "f", title: "t", body: "b" });
  assert.equal(status, 200);
  assert.equal(body.number, 7);
  assert.equal(seen[0].url, "https://git-relay.jays.services/v1/open_pr");
  assert.equal(seen[0].init.headers["CF-Access-Client-Id"], "id");
  assert.equal(seen[0].init.headers["CF-Access-Client-Secret"], "secret");
  assert.equal(seen[0].init.headers["Authorization"], "Bearer sbt_jet-chat_abc");
  assert.deepEqual(client.secrets(), ["id", "secret", "sbt_jet-chat_abc"]);
});

test("GrRelayClient maps an unreachable upstream and a bad answer to fixed statuses", async () => {
  const down = new GrRelayClient({ baseUrl: "https://git-relay.jays.services", accessId: "i", accessSecret: "s", bearer: "b", fetch: async () => { throw new Error("ECONNREFUSED"); } });
  assert.equal((await down.call("open_pr", {})).status, 0);
  const garbage = new GrRelayClient({ baseUrl: "https://git-relay.jays.services", accessId: "i", accessSecret: "s", bearer: "b", fetch: async () => new Response("not json", { status: 200 }) });
  assert.equal((await garbage.call("open_pr", {})).status, 502);
  const refused = new GrRelayClient({ baseUrl: "https://git-relay.jays.services", accessId: "i", accessSecret: "s", bearer: "b", fetch: async () => new Response(JSON.stringify({ error: "policy_denied" }), { status: 403 }) });
  const { status, body } = await refused.call("merge_pr", {});
  assert.equal(status, 403);
  assert.equal(body.error, "policy_denied");
  const midbody = new GrRelayClient({ baseUrl: "https://git-relay.jays.services", accessId: "i", accessSecret: "s", bearer: "b", fetch: async () => ({ status: 200, text: async () => { throw new Error("stream reset"); } }) });
  const cut = await midbody.call("open_pr", {});
  assert.equal(cut.status, 0);
  assert.equal(cut.body.error, "upstream_unreachable");
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
    baseUrl: "https://git-relay.jays.services",
    accessId: "i", accessSecret: "s", bearer: "b",
    fetch: async (url, init) => { seen.push({ url, body: JSON.parse(init.body) }); return new Response(JSON.stringify({ number: 9, url: "u" }), { status: 200 }); },
  });
  const tools = grHarness(gr);
  const result = await tools.call("gr_open_pr", { repo: "a/b", base: "main", head: "f", title: "t", body: "b" });
  assert.ok(!result.isError, `expected success, got ${result.content?.[0]?.text}`);
  assert.equal(seen[0].url, "https://git-relay.jays.services/v1/open_pr");
  assert.equal(seen[0].body.repo, "a/b");
});

test("gr tools answer not_configured when the front is absent, and a relay 4xx surfaces as relay_refused", async () => {
  const bare = grHarness(null);
  const missing = await bare.call("gr_merge_pr", { repo: "a/b", number: 1 });
  assert.equal(missing.isError, true);
  assert.match(missing.content[0].text, /not configured/i);
  assert.match(missing.content[0].text, /GR_UPSTREAM_URL/);

  const refusing = new GrRelayClient({ baseUrl: "https://git-relay.jays.services", accessId: "access-id-0123456789abcdef", accessSecret: "access-secret-0123456789abcdef", bearer: "sbt_jet-chat_" + "a".repeat(43), fetch: async () => new Response(JSON.stringify({ error: "seat ceiling does not cover permission 'issues'" }), { status: 403 }) });
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
