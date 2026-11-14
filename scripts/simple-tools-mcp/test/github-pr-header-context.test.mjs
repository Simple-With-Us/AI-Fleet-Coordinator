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

test("actual hosted PR titles and comments compact while PR descriptions keep full context", async () => {
  const { tools, sent } = harness();
  const body = `${full}\nOne. Two.\n[AG: AA.1.M 📁OPS] quoted`;
  const compactComment = `${compact}\nOne.\u00a0 Two.\n[AG: AA.1.M 📁OPS] quoted`;
  const fullDescription = `${full}\nOne.\u00a0 Two.\n[AG: AA.1.M 📁OPS] quoted`;
  for (const [action, extra] of [
    ["gr_open_pr", { base: "main", head: "feature", title: `${full} A. B` }],
    ["gr_edit_pr", { number: 1, title: `${full} A. B` }],
    ["gr_comment_pr", { number: 1 }],
    ["gr_reply_thread", { number: 1, comment_id: 2 }],
  ]) {
    const result = await tools.call(action, { repo: "example/header-fixture", body, ...extra });
    assert.ok(!result.isError, result.content?.[0]?.text);
    assert.equal(sent.at(-1).args.body,
      ["gr_open_pr", "gr_edit_pr"].includes(action) ? fullDescription : compactComment, action);
    if (extra.title) assert.equal(sent.at(-1).args.title, `${compact} A. B`, "title prose remains literal");
  }
  const gg = await tools.call("gr_edit_pr", { repo: "example/header-fixture", number: 1,
    body: "[CODEX: GG.1.S 📁AFC] supplied attribution" });
  assert.ok(!gg.isError, gg.content?.[0]?.text);
  assert.equal(sent.at(-1).args.body, "[CODEX: GG.1.S 📁AFC] supplied attribution");
});

test("actual hosted PR descriptions keep full headers after initial readiness bullets", async () => {
  const { tools, sent } = harness();
  const prefix = "- Pass Required CI\n- Keep The Draft Hold\n\n";
  const body = `${prefix}${full}\nSummary\n${full} quoted later`;
  const expected = `${prefix}${full}\nSummary\n${full} quoted later`;
  for (const [action, extra] of [
    ["gr_open_pr", { base: "main", head: "feature", title: `${full} Title`, draft: true }],
    ["gr_edit_pr", { number: 1, title: `${full} Title` }],
    ["gr_wf_pr_create", { base: "main", head: "feature", title: `${full} Title`, expected_head_sha: sha }],
    ["gr_wf_pr_edit", { number: 1, expected_head_sha: sha, expected_base_ref: "main" }],
  ]) {
    const result = await tools.call(action, { repo: "example/header-fixture", body, ...extra });
    assert.ok(!result.isError, `${action}: ${result.content?.[0]?.text}`);
    assert.equal(sent.at(-1).args.body, expected, action);
    if (extra.title) assert.equal(sent.at(-1).args.title, `${compact} Title`);
  }
  for (const action of ["comment_pr", "reply_thread", "wf_review_submit", "wf_issue_comment"]) {
    assert.equal(gapGitHubArgs(action, { body }, { seat: "CODEX" }).body, body, action);
  }
  assert.equal(gapGitHubArgs("edit_pr", { title: body }, { seat: "CODEX" }).title, body);
});

test("actual hosted description edits preserve quoted, fenced and arbitrary later samples", async () => {
  const { tools, sent } = harness();
  for (const body of [
    `- Pass Required CI\n\nExample:\n${full}`,
    `- Pass Required CI\n\n> ${full}`,
    `- Pass Required CI\n\n\`\`\`text\n${full}\n\`\`\``,
    `- Pass Required CI\n\n    ${full}`,
    `- Pass Required CI\n\n  ${full}`,
    `- Pass Required CI\n${full}`,
    `- \`\`\`text\n\n${full}\n  \`\`\``,
    `@kody start-review\n1. Pass Required CI\n\n> ${full}`,
    `@kody start-review\n1. Pass Required CI\n\n  ${full}`,
    `1000000000) Example\n\n${full}`,
    `\t${full}`,
    `    ${full}`,
  ]) {
    const result = await tools.call("gr_edit_pr", { repo: "example/header-fixture", number: 1, body });
    assert.ok(!result.isError, result.content?.[0]?.text);
    assert.equal(sent.at(-1).args.body, body);
  }
});

test("actual hosted descriptions preserve full headers with command, criteria and indentation", async () => {
  const { tools, sent } = harness();
  for (const [prefix, leading] of [
    ["@kody start-review\r\n- Pass Required CI\r\n\r\n", ""],
    ["1. Pass Required CI\n999999999) Keep The Hold\n\n", ""],
    ["@kody start-review\n1. Pass Required CI\n\n", ""],
    ["", "   "],
  ]) {
    const body = `${prefix}${leading}${full}\nSummary\n${full} later sample`;
    const expected = `${prefix}${leading}${full}\nSummary\n${full} later sample`;
    for (const [action, extra] of [
      ["gr_open_pr", { base: "main", head: "feature", title: `${full} Title` }],
      ["gr_edit_pr", { number: 1, title: `${full} Title` }],
    ]) {
      const result = await tools.call(action, { repo: "example/header-fixture", body, ...extra });
      assert.ok(!result.isError, result.content?.[0]?.text);
      assert.equal(sent.at(-1).args.body, expected);
      assert.equal(sent.at(-1).args.title, `${compact} Title`);
    }
  }
  const sample = `@kody start-review\n1. Example\n\n${full}`;
  for (const [action, extra] of [
    ["gr_comment_pr", { number: 1 }],
    ["gr_wf_review_submit", { number: 1, commit_id: sha, event: "COMMENT" }],
  ]) {
    const result = await tools.call(action, { repo: "example/header-fixture", body: sample, ...extra });
    assert.ok(!result.isError, result.content?.[0]?.text);
    assert.equal(sent.at(-1).args.body, sample);
  }
});

test("actual hosted PR prose keeps indented samples literal while titles keep their existing matcher", async () => {
  const { tools, sent } = harness();
  for (const lead of ["\t", "\n", "    "]) {
    for (const command of ["", "@kody start-review\r\n"]) {
      const body = `${command}${lead}${full} sample`;
      for (const [action, extra] of [
        ["gr_comment_pr", { number: 1 }],
        ["gr_reply_thread", { number: 1, comment_id: 2 }],
        ["gr_wf_review_submit", { number: 1, commit_id: sha, event: "COMMENT" }],
      ]) {
        const result = await tools.call(action, { repo: "example/header-fixture", body, ...extra });
        assert.ok(!result.isError, result.content?.[0]?.text);
        assert.equal(sent.at(-1).args.body, body, action);
      }
    }
    for (const [action, extra] of [
      ["gr_open_pr", { base: "main", head: "feature" }],
      ["gr_edit_pr", { number: 1 }],
    ]) {
      const title = `${lead}${full} Title`;
      const result = await tools.call(action, { repo: "example/header-fixture", title, body: "Body", ...extra });
      assert.ok(!result.isError, result.content?.[0]?.text);
      assert.equal(sent.at(-1).args.title, `${lead}${compact} Title`, action);
      assert.equal(sent.at(-1).args.body, "Body", action);
    }
  }
  for (const lead of ["", " ", "   "]) {
    const result = await tools.call("gr_comment_pr", { repo: "example/header-fixture", number: 1,
      body: `@kody start-review\r\n${lead}${full} Body` });
    assert.ok(!result.isError, result.content?.[0]?.text);
    assert.equal(sent.at(-1).args.body, `@kody start-review\r\n${lead}${compact} Body`);
  }
});

test("PR titles compact supplied headers while descriptions preserve the original text", async () => {
  const { tools, sent } = harness();
  const prefix = "- Pass Required CI\n\n";
  for (const [header, expectedHeader] of [
    ["[CODEX·deadbeef 📁AFC → CODEX GG]", "[CODEX → CODEX GG]"],
    ["[CODEX: EE.1.S 📁,OPS → CODEX GG, AG]", compact],
    ["[CODEX 📁AFC📁OPS]", "[CODEX]"],
    ["[CODEX 📁AFC → CODEX GG, ,AG]", "[CODEX 📁AFC → CODEX GG, ,AG]"],
  ]) {
    for (const [action, extra] of [
      ["gr_open_pr", { base: "main", head: "feature" }],
      ["gr_edit_pr", { number: 1 }],
    ]) {
      const result = await tools.call(action, { repo: "example/header-fixture", ...extra,
        title: `${header} Title`, body: `${prefix}${header}\nSummary` });
      assert.ok(!result.isError, result.content?.[0]?.text);
      assert.equal(sent.at(-1).args.title, `${expectedHeader} Title`);
      assert.equal(sent.at(-1).args.body, `${prefix}${header}\nSummary`);
    }
  }
});

test("workflow PR titles and review comments compact while descriptions and issues keep their context", async () => {
  const { tools, sent } = harness();
  const args = { repo: "example/header-fixture", number: 1, body: `${full} body` };
  for (const [action, extra] of [
    ["gr_wf_pr_edit", { expected_head_sha: sha, expected_base_ref: "main", title: `${full} Title` }],
    ["gr_wf_review_submit", { commit_id: sha, event: "COMMENT" }],
    ["gr_wf_review_inline", { commit_id: sha, path: "file.py", line: 1, side: "RIGHT" }],
  ]) {
    const result = await tools.call(action, { ...args, ...extra });
    assert.ok(!result.isError, `${action}: ${result.content?.[0]?.text}`);
    assert.equal(sent.at(-1).args.body,
      action === "gr_wf_pr_edit" ? `${full} body` : `${compact} body`, action);
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

test("description byte-cap fallback preserves its full header and comment opt-out remains literal", async () => {
  const { tools, sent } = harness();
  const body = `${full}\n${"Word. Next ".repeat(5940)}`;
  const result = await tools.call("gr_wf_pr_edit", { repo: "example/header-fixture", number: 1,
    expected_head_sha: sha, expected_base_ref: "main", body });
  assert.ok(!result.isError, result.content?.[0]?.text);
  assert.equal(sent.at(-1).args.body, `${full}\n${"Word. Next ".repeat(5940)}`);
  assert.ok(result.content.some((part) => part.text?.includes("sent as typed")));
  await runWithGap(false, () => assert.equal(gapGitHubArgs("comment_pr", { body }).body, body));
});
