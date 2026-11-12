// AFC-722:  the gr_* relay verbs write the house sentence gap, honour sentence_gap false, and report a gap
// they could not write.  Kept out of test/gr-front.test.mjs, whose bytes are pinned by
// docs/security/preservation-pending-manifest.json (existing_exact_attestations).

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { GrRelayClient } from "../src/gr-tools.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";

const config = loadConfig(testEnv());

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
    seat: "JET-CHAT", scopes: ["zulip:read", "zulip:write"], clientId: "sbt-bearer", grantRef: "abc123", paused: false,
    config, zulip: null, key: "", gate: memoryGate(clock), cf: {}, gr, now: () => clock.now, sleep: async () => {}, log: () => {},
  });
}

test("AFC-722:  gr_* prose reaches git-relay with the house sentence gap, and sentence_gap false sends it byte for byte", async () => {
  const sent = [];
  const tools = grHarness(
    new GrRelayClient({
      baseUrl: "https://git-relay.example.test",
      accessId: "test-access-id", accessSecret: "test-access-secret", bearer: "sbt_jet-chat_testbearer",
      fetch: async (url, init) => {
        sent.push({ action: String(url).split("/v1/")[1], body: JSON.parse(init.body) });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      },
    }),
  );
  const prose = "Fixed it. The test passes.  Next is `npm test`. then deploy.";
  const gapped = "Fixed it.  The test passes.  Next is `npm test`.  then deploy.";
  for (const [name, args] of [
    ["gr_comment_pr", { repo: "a/b", number: 1, body: prose }],
    ["gr_reply_thread", { repo: "a/b", number: 1, comment_id: 2, body: prose }],
    ["gr_edit_pr", { repo: "a/b", number: 1, body: prose }],
    ["gr_open_pr", { repo: "a/b", base: "main", head: "f", title: "Fix it. Now", body: prose }],
  ]) {
    const res = await tools.call(name, args);
    assert.ok(!res.isError, `${name}: ${res.content?.[0]?.text}`);
    const last = sent.at(-1);
    assert.equal(last.body.body, gapped, name);
    assert.ok(!Object.hasOwn(last.body, "sentence_gap"), `${name} never forwards the option`);
    if (name === "gr_open_pr") assert.equal(last.body.title, "Fix it. Now", "the title is left as written");
  }
  const raw = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: prose, sentence_gap: false });
  assert.ok(!raw.isError);
  assert.equal(sent.at(-1).body.body, prose);
  assert.ok(!Object.hasOwn(sent.at(-1).body, "sentence_gap"));
  const already = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: gapped });
  assert.ok(!already.isError);
  assert.equal(sent.at(-1).body.body, gapped, "text that already has the gap is unchanged");
});

test("AFC-722 review:  a non-workflow body pushed past the relay cap by the gap is sent as typed, and the result says so", async () => {
  const sent = [];
  const tools = grHarness(
    new GrRelayClient({
      baseUrl: "https://git-relay.example.test",
      accessId: "test-access-id", accessSecret: "test-access-secret", bearer: "sbt_jet-chat_testbearer",
      fetch: async (url, init) => {
        sent.push(JSON.parse(init.body));
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      },
    }),
  );
  const body = "Word. Next ".repeat(2978);
  const res = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body });
  assert.ok(!res.isError, res.content?.[0]?.text);
  assert.equal(sent.at(-1).body, body);
  assert.ok(res.content.some((item) => /sent as typed, without the U\+00A0 gap/.test(item.text)), "the notice is in the result");
  const small = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "One. Two." });
  assert.equal(small.content.length, 1, "no notice when the gap was written");
});
