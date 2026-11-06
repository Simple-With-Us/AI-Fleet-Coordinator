// The relay's credential-scan refusal must surface as the dedicated safe code
// refused_secret, not a bare relay_refused:  CLI callers print codes, not
// messages, and an unlabelled refusal hid why the write never happened
// (Socratic-Trade files_put investigation, 2026-11-05).  Lives in its own
// file because gr-front.test.mjs carries a pre-existing bearer-shaped literal
// the write-time scan refuses on any full-file write.
//
// Two transports, two real relay phrasings (git-relay/git_relay):
//  - seven-action path:  the pre-dispatch scanner's SecretInputError becomes
//    400 {"error": "args.body looks like it contains a credential (github_token); refusing to relay it"}
//  - workflow verbs:  file_put/files_put raise Refused("credential-shaped content refused"),
//    which callWorkflow collapses to workflow_request_refused with a
//    machine-readable reason:"credential_scan" marker.

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

function grHarness(gr, configOverride = config) {
  const clock = { now: Date.parse("2026-10-25T14:00:00Z") };
  return new HostedTools({
    seat: "JET-CHAT",
    scopes: ["zulip:read", "zulip:write"],
    clientId: "sbt-bearer",
    grantRef: "abc123",
    paused: false,
    config: configOverride,
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

const codeOf = (out) => JSON.parse(out.content[0].text).code;

test("a seven-action credential-scan refusal surfaces as refused_secret", async () => {
  const tools = grHarness(clientWith({ error: "args.body looks like it contains a credential (github_token); refusing to relay it" }, 400));
  const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(out.isError, true);
  assert.equal(codeOf(out), "refused_secret");
  assert.match(out.content[0].text, /Nothing was written/);
});

test("other relay 4xx verdicts keep the generic relay_refused code", async () => {
  const tools = grHarness(clientWith({ error: "branch head moved" }, 400));
  const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(out.isError, true);
  assert.equal(codeOf(out), "relay_refused");
});

test("only the anchored scanner family maps;  near-matches stay relay_refused", async () => {
  for (const error of [
    "credential-shaped content refused (test/history.test.ts)",
    "it looks like it contains a credential, honestly",
    "args.body looks like it contains a credential (github_token); refusing to relay it (test/history.test.ts)",
  ]) {
    const tools = grHarness(clientWith({ error }, 400));
    const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
    assert.equal(out.isError, true);
    assert.equal(codeOf(out), "relay_refused", error);
  }
});

test("a malformed 4xx body keeps the generic relay_refused code", async () => {
  // Straight to grCall with a stub client:  the zod guard refuses to classify
  // a body whose shape was never validated.  The over-long string and the
  // credential-shaped prose ride an unvalidated body;  deleting the guard
  // would let the string reach the message and fail every assertion here.
  const tools = grHarness({
    call: async () => ({
      status: 400,
      body: { error: "x".repeat(1500), note: "credential-shaped content refused" },
    }),
    secrets: () => [],
  });
  const out = await tools.call("gr_comment_pr", { repo: "a/b", number: 1, body: "x" });
  assert.equal(out.isError, true);
  const payload = JSON.parse(out.content[0].text);
  assert.equal(payload.code, "relay_refused");
  assert.equal(payload.message, "refused");
  assert.ok(!out.content[0].text.includes("credential-shaped"));
});

test("every fixed workflow credential refusal surfaces as refused_secret through callWorkflow", async () => {
  // The vouched family from git-relay/git_relay/workflow_verbs.py:  the
  // "credential-shaped ..." strings and the secrets-API refusal.
  for (const error of [
    "credential-shaped content refused",
    "credential-shaped binary blob refused",
    "credential-shaped file path refused",
    "credential-shaped repository metadata name refused",
    "invalid or secret variable value; this is not a secrets API",
  ]) {
    const tools = grHarness(clientWith({ error }, 400), { ...config, grWorkflowEnabled: true });
    const out = await tools.call("gr_wf_files_put", {
      repo: "a/b",
      branch: "jet/x",
      message: "m",
      expected_head_sha: "a".repeat(40),
      files: [{ path: "x.txt", content_b64: "eA==" }],
    });
    assert.equal(out.isError, true);
    assert.equal(codeOf(out), "refused_secret", error);
    assert.match(out.content[0].text, /Nothing was written/);
  }
});

test("other workflow 4xx verdicts keep the generic relay_refused code", async () => {
  const tools = grHarness(
    clientWith({ error: "branch head moved" }, 400),
    { ...config, grWorkflowEnabled: true },
  );
  const out = await tools.call("gr_wf_files_put", {
    repo: "a/b",
    branch: "jet/x",
    message: "m",
    expected_head_sha: "a".repeat(40),
    files: [{ path: "x.txt", content_b64: "eA==" }],
  });
  assert.equal(out.isError, true);
  assert.equal(codeOf(out), "relay_refused");
});
