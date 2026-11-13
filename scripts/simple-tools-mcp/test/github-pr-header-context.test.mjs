import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { normalizeHeader } from "../src/contract.js";
import { gapGitHubArgs, runWithGap } from "../src/outbound-gap.js";
import { loadConfig } from "../src/config.js";
import * as S from "../src/seat-state.js";
import { testEnv } from "./helpers.mjs";

const full = "[CODEX: EE.1.S 📁AFC, OPS → CODEX GG, AG]";
const compact = "[CODEX:\u200a\u200a\u200aEE.1.S → CODEX GG,\u200a\u200aAG]";
const sha = "a".repeat(40);

function harness() {
  const sent = [];
  const store = S.memoryStore();
  const gate = {};
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"])
    gate[name] = (options) => S[name](store, options, Date.now());
  gate.roleGet = () => S.roleGet(store, Date.now());
  const call = async (action, args) => {
    sent.push({ action, args: JSON.parse(JSON.stringify(args)) });
    return { status: 200, body: { ok: true } };
  };
  const tools = new HostedTools({
    seat: "CODEX", scopes: ["zulip:read", "zulip:write"], clientId: "fixture", grantRef: "fixture",
    paused: false, config: { ...loadConfig(testEnv()), grWorkflowEnabled: true },
    zulip: null, key: "", gate, gr: { call, callWorkflow: call, secrets: () => [] }, log: () => {}, sleep: async () => {},
  });
  return { tools, sent };
}

test("actual hosted PR title/body/comment/reply writes use compact context without changing identity", async () => {
  const { tools, sent } = harness();
  const body = `${full}\nOne. Two.\n[AG: AA.1.M 📁OPS] quoted`;
  const expected = `${compact}\nOne.\u00a0 Two.\n[AG: AA.1.M 📁OPS] quoted`;
  for (const [action, extra] of [
    ["gr_open_pr", { base: "main", head: "feature", title: `${full} A. B` }],
    ["gr_edit_pr", { number: 1, title: `${full} A. B` }],
    ["gr_comment_pr", { number: 1 }],
    ["gr_reply_thread", { number: 1, comment_id: 2 }],
  ]) {
    const result = await tools.call(action, { repo: "example/header-fixture", body, ...extra });
    assert.ok(!result.isError, result.content?.[0]?.text);
    assert.equal(sent.at(-1).args.body, expected, action);
    if (extra.title) assert.equal(sent.at(-1).args.title, `${compact} A. B`, "title prose remains literal");
  }
  const gg = await tools.call("gr_edit_pr", { repo: "example/header-fixture", number: 1,
    body: "[CODEX: GG.1.S 📁AFC] supplied attribution" });
  assert.ok(!gg.isError, gg.content?.[0]?.text);
  assert.equal(sent.at(-1).args.body, "[CODEX:\u200a\u200a\u200aGG.1.S] supplied attribution");
});

test("workflow PR writes compact while ordinary issue and Linear contexts retain repository names", async () => {
  const { tools, sent } = harness();
  const args = { repo: "example/header-fixture", number: 1, body: `${full} body` };
  for (const [action, extra] of [
    ["gr_wf_pr_edit", { expected_head_sha: sha, expected_base_ref: "main", title: `${full} Title` }],
    ["gr_wf_review_submit", { commit_id: sha, event: "COMMENT" }],
    ["gr_wf_review_inline", { commit_id: sha, path: "file.py", line: 1, side: "RIGHT" }],
  ]) {
    const result = await tools.call(action, { ...args, ...extra });
    assert.ok(!result.isError, `${action}: ${result.content?.[0]?.text}`);
    assert.equal(sent.at(-1).args.body, `${compact} body`, action);
    if (extra.title) assert.equal(sent.at(-1).args.title, `${compact} Title`);
  }
  const issue = await tools.call("gr_wf_issue_comment", { ...args });
  assert.ok(!issue.isError, issue.content?.[0]?.text);
  assert.equal(sent.at(-1).args.body, args.body);
  assert.equal(tools.linearBody(args.body, null), normalizeHeader(args.body, { seat: "CODEX" }));
});

test("Kody command remains literal line one and only its own following header is compacted", async () => {
  const { tools, sent } = harness();
  const body = `@kody start-review\n${full}\nFull SHA ${sha}`;
  const result = await tools.call("gr_comment_pr", { repo: "example/header-fixture", number: 1, body });
  assert.ok(!result.isError, result.content?.[0]?.text);
  assert.equal(sent.at(-1).args.body, `@kody start-review\n${compact}\nFull SHA ${sha}`);
  const foreign = "[AG: AA.1.M 📁OPS] quoted";
  assert.equal(gapGitHubArgs("comment_pr", { body: foreign }, { seat: "CODEX" }).body, foreign);
});

test("byte-cap fallback keeps the compact header and explicit byte-for-byte opt-out remains literal", async () => {
  const { tools, sent } = harness();
  const body = `${full}\n${"Word. Next ".repeat(5940)}`;
  const result = await tools.call("gr_wf_pr_edit", { repo: "example/header-fixture", number: 1,
    expected_head_sha: sha, expected_base_ref: "main", body });
  assert.ok(!result.isError, result.content?.[0]?.text);
  assert.equal(sent.at(-1).args.body, `${compact}\n${"Word. Next ".repeat(5940)}`);
  assert.ok(result.content.some((part) => part.text?.includes("sent as typed")));
  await runWithGap(false, () => assert.equal(gapGitHubArgs("comment_pr", { body }).body, body));
});
