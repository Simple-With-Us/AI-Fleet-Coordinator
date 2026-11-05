// The relay's credential-scan refusal must surface as the dedicated safe code
// refused_secret, not a bare relay_refused:  CLI callers print codes, not
// messages, and an unlabelled refusal hid why the write never happened
// (Socratic-Trade files_put investigation, 2026-11-05).  Lives in its own
// file because gr-front.test.mjs carries a pre-existing bearer-shaped literal
// the write-time scan refuses on any full-file write.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { GrRelayClient } from "../src/gr-tools.js";
import { loadConfig } from "../src/config.js";
import { testEnv } from "./helpers.mjs";
import * as S from "../src/seat-state.js";

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

const clientWith = (body, status) =>
  new GrRelayClient({
    baseUrl: "https://git-relay.example.test",
    accessId: "access-id-0123456789abcdef",
    accessSecret: "access-secret-0123456789abcdef",
    bearer: "sbt_jet-chat_" + "a".repeat(43),
    fetch: async () => new Response(JSON.stringify(body), { status }),
  });

test("a relay credential-scan refusal surfaces as refused_secret", async () => {
  const tools = grHarness(clientWith({ error: "credential-shaped content refused" }, 400));
  const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(out.isError, true);
  assert.equal(JSON.parse(out.content[0].text).code, "refused_secret");
  assert.match(out.content[0].text, /Nothing was written/);
});

test("other relay 4xx verdicts keep the generic relay_refused code", async () => {
  const tools = grHarness(clientWith({ error: "branch head moved" }, 400));
  const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(out.isError, true);
  assert.equal(JSON.parse(out.content[0].text).code, "relay_refused");
});

test("only the exact fixed string maps;  near-matches stay relay_refused", async () => {
  const tools = grHarness(clientWith({ error: "credential-shaped content refused (test/history.test.ts)" }, 400));
  const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(out.isError, true);
  assert.equal(JSON.parse(out.content[0].text).code, "relay_refused");
});
