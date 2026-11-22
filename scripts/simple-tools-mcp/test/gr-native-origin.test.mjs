import test from "node:test";
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
