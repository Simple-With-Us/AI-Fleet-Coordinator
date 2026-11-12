// AFC-722:  the house sentence gap the hosted GitHub and Linear text verbs write server-side
// (src/textfmt.js houseGap, src/outbound-gap.js).  The cases are the house perl converter's own reach, its
// two assertions from AGENTS.md, and the converter holes the fleet found (lowercase next word, a backtick or a
// digit before the period).

import test from "node:test";
import assert from "node:assert/strict";
import { houseGap, GAP, NBSP, HOUSE_GAP_MAX } from "../src/textfmt.js";
import { GITHUB_GAP_FIELDS, GITHUB_GAP_BYTE_CAP, gapGitHubArgs, runWithGap, outboundGap, gapEnabled } from "../src/outbound-gap.js";
import { linearClean } from "../src/linear.js";
import { HOSTED_TOOLS_BY_NAME, GAP_OPTION_TOOLS } from "../src/hosted-tools.js";
import { LINEAR_WRITE_TOOLS } from "../src/linear.js";

// The two house assertions (AGENTS.md "Writing anything a human reads"), as JS.
const CLOSERS = `(?:\\]\\([^\\s()]{1,255}\\)|[\\]*_\`~)"'’”])*`;
const HAS_GAP = new RegExp(`[.!?]${CLOSERS}${NBSP}`, "g");
const LEFTOVER = new RegExp(`[^0-9][.!?]${CLOSERS} +[A-Z0-9"*\`#@'‘“[]`, "g");

const CONVERTED = [
  ["two spaces", "One.  Two.", `One.${GAP}Two.`],
  ["one space", "One. Two.", `One.${GAP}Two.`],
  ["three spaces", "One.   Two.", `One.${GAP}Two.`],
  ["question and bang", "Ready? Yes! Go.", `Ready?${GAP}Yes!${GAP}Go.`],
  ["hole: lowercase next word", "It ships today. then we rest.", `It ships today.${GAP}then we rest.`],
  ["hole: code span before the period", "Run `ls`. Then stop.", `Run \`ls\`.${GAP}Then stop.`],
  ["hole: code span before the period, lowercase", "is `x`. macOS is fine.", `is \`x\`.${GAP}macOS is fine.`],
  ["hole: digit before the period", "It must print 0. The rest follows.", `It must print 0.${GAP}The rest follows.`],
  ["closing bold", "**Done.** Next up.", `**Done.**${GAP}Next up.`],
  ["closing quote and bracket", 'He said "stop.") Then left.', `He said "stop.")${GAP}Then left.`],
  ["markdown link after the period", "See [the doc.](https://x.test/a) Then read.", `See [the doc.](https://x.test/a)${GAP}Then read.`],
  ["next is a code span", "Try this. `npm test` runs it.", `Try this.${GAP}\`npm test\` runs it.`],
  ["next is a mention", "Thanks. @**Echo** has it.", `Thanks.${GAP}@**Echo** has it.`],
  ["next is a link", "Done. [PR](https://x.test) is up.", `Done.${GAP}[PR](https://x.test) is up.`],
  ["next is a digit", "Step one. 2 more to go.", `Step one.${GAP}2 more to go.`],
  ["the entity typed in the Code tab", "One.&nbsp; Two.", `One.${GAP}Two.`],
  ["the entity with no space", "One.&nbsp;Two.", `One.${GAP}Two.`],
  ["URL ends a sentence", "See https://x.test/a. Next one.", `See https://x.test/a.${GAP}Next one.`],
  ["version ends a sentence", "Shipped v1.2.3. Next is v1.3.", `Shipped v1.2.3.${GAP}Next is v1.3.`],
  ["one sentence per line is untouched but lines gap", "First. Second.\nThird. Fourth.", `First.${GAP}Second.\nThird.${GAP}Fourth.`],
];

const UNCHANGED = [
  ["already gapped", `One.${GAP}Two.`],
  ["e.g.", "Use a tool, e.g. this one."],
  ["i.e.", "The relay, i.e. git-relay, is fine."],
  ["initials", "J. Doe and U.S. Army came."],
  ["Dr.", "Ask Dr. Smith today."],
  ["version inside a word", "Run v1.2.3 now"],
  ["list marker", "1. Item one\n2. Item two"],
  ["list marker two spaces", "1.  Item one"],
  ["inline code", "Run `a. b. C` now"],
  ["fenced code", "```\nOne. Two.  Three.\n```"],
  ["math", "$$a. B$$"],
  ["mention", "@**Dr. Who** said hi"],
  ["URL with dots", "https://a.b.example/c.d?e=f.g"],
  ["entity inside code", "Write `One.&nbsp; Two.` in the pane"],
  ["no sentence end", "a plain line with no period"],
  ["filename", "Edit main.py and test.py today"],
  ["table cell", "| a. | b |"],
];

test("houseGap writes U+00A0 plus a space for every sentence gap the house converter covers", () => {
  for (const [name, text, expected] of CONVERTED) {
    const out = houseGap(text);
    assert.equal(out, expected, name);
    assert.ok((out.match(HAS_GAP) ?? []).length > 0, `${name}: the first house assertion finds a gap`);
    assert.equal((out.match(LEFTOVER) ?? []).length, 0, `${name}: the second house assertion finds no leftover`);
  }
});

test("houseGap leaves code, math, mentions, abbreviations, initials and list markers alone", () => {
  for (const [name, text] of UNCHANGED) assert.equal(houseGap(text), text, name);
});

test("houseGap is idempotent", () => {
  for (const [, text] of [...CONVERTED, ...UNCHANGED]) {
    const once = houseGap(text);
    assert.equal(houseGap(once), once);
  }
  assert.equal(houseGap(null), null);
  assert.equal(houseGap(""), "");
});

test("a typical PR body written one sentence per line, or as a paragraph, passes both house assertions", () => {
  const body =
    "## Summary\n\nThe hosted verbs now write the gap.  Agents type one space. The server widens it.\n\n" +
    "- Tests pass. Nothing is deployed.\n- See `src/outbound-gap.js`. It is the one module.\n\n" +
    "```sh\nnpm test. Then deploy.\n```\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)";
  const out = houseGap(body);
  const prose = out.replace(/```[\s\S]*?```/g, "");
  assert.ok((prose.match(HAS_GAP) ?? []).length >= 4);
  assert.equal((prose.match(LEFTOVER) ?? []).length, 0);
  assert.match(out, /```sh\nnpm test\. Then deploy\.\n```/, "fenced code is byte for byte");
});

test("gapGitHubArgs gaps only the persisted text fields, and sentence_gap false turns it off", async () => {
  const args = { repo: "a/b", number: 1, body: "One. Two.", title: "Fix it. Now" };
  const gapped = gapGitHubArgs("comment_pr", args);
  assert.equal(gapped.body, `One.${GAP}Two.`);
  assert.equal(gapped.title, "Fix it. Now", "titles are not touched");
  assert.equal(args.body, "One. Two.", "the caller's object is not mutated");
  assert.equal(gapGitHubArgs("merge_pr", args), args, "a verb with no prose is passed through");
  await runWithGap(false, async () => {
    assert.equal(gapEnabled(), false);
    assert.equal(gapGitHubArgs("comment_pr", args), args);
    assert.equal(outboundGap("One. Two."), "One. Two.");
  });
  await runWithGap(true, async () => assert.equal(outboundGap("One. Two."), `One.${GAP}Two.`));
  assert.equal(gapEnabled(), true, "outside a call the gap is on");
});

test("every GitHub and Linear prose-writing tool advertises sentence_gap, and nothing else does", () => {
  for (const action of Object.keys(GITHUB_GAP_FIELDS)) {
    const tool = HOSTED_TOOLS_BY_NAME[`gr_${action}`];
    assert.ok(tool, `gr_${action} is served`);
    assert.equal(tool.inputSchema.properties.sentence_gap?.type, "boolean", `gr_${action}`);
  }
  for (const name of LINEAR_WRITE_TOOLS) {
    const tool = HOSTED_TOOLS_BY_NAME[name];
    if (!tool?.inputSchema?.properties) continue;
    assert.equal(tool.inputSchema.properties.sentence_gap?.type, "boolean", name);
  }
  for (const name of ["zulip_post", "zulip_reply", "gr_merge_pr", "gr_wf_pr_get", "linear_show"]) {
    assert.ok(!GAP_OPTION_TOOLS.has(name), name);
    assert.equal(HOSTED_TOOLS_BY_NAME[name]?.inputSchema?.properties?.sentence_gap, undefined, name);
  }
});

test("AFC-722:  a gr_wf_* prose verb reaches git-relay gapped, with the option stripped", async () => {
  const { HostedTools } = await import("../src/hosted-tools.js");
  const { GrRelayClient } = await import("../src/gr-tools.js");
  const { loadConfig } = await import("../src/config.js");
  const { testEnv } = await import("./helpers.mjs");
  const S = await import("../src/seat-state.js");
  const store = S.memoryStore();
  const gate = { store };
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) gate[name] = (options) => S[name](store, options, Date.now());
  gate.roleGet = () => S.roleGet(store, Date.now());
  gate.callTail = (n) => S.callTail(store, n);
  const sent = [];
  const gr = new GrRelayClient({
    baseUrl: "https://broker.invalid", accessId: "private-access-identifier", accessSecret: "private-access-secret", bearer: "caller-own-bearer",
    fetch: async (url, init) => {
      sent.push({ url: String(url), body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ comment: { id: 5 } }), { status: 200 });
    },
  });
  const tools = new HostedTools({ seat: "JET-CHAT", scopes: ["zulip:read", "zulip:write"], clientId: "test", grantRef: "abc123", paused: false,
    config: { ...loadConfig(testEnv()), grWorkflowEnabled: true }, zulip: null, key: "", gate, gr, log: () => {}, sleep: async () => {} });
  await tools.call("gr_wf_issue_comment", { repo: "a/b", number: 3, body: "One. Two." });
  assert.equal(sent.length, 1);
  assert.match(sent[0].url, /wf_issue_comment$/);
  assert.equal(sent[0].body.body, `One.${GAP}Two.`);
  await tools.call("gr_wf_issue_comment", { repo: "a/b", number: 3, body: "One. Two.", sentence_gap: false });
  assert.equal(sent.at(-1).body.body, "One. Two.");
  assert.ok(!Object.hasOwn(sent.at(-1).body, "sentence_gap"));
});

// ---- AFC-722 independent review:  each reviewer input is a case here --------------------------------------

const REVIEW_UNCHANGED = [
  ["indented code after a blank line", "Run this:\n\n    sed -e \"s/foo. bar/baz/\" notes.txt\n"],
  ["indented code, a capital after the period", "Text.\n\n    x = foo. Bar()\n"],
  ["tab-indented code", "Text.\n\n\tx = foo. Bar()\n"],
  ["<pre> block", "<pre>\nsed -e \"s/foo. bar/baz/\" notes.txt\n</pre>"],
  ["<code> element", "<code>obj. Method()</code>"],
  ["<kbd> element", "<kbd>Ctrl. X</kbd>"],
  ["<samp> element", "<samp>Done. Exit</samp>"],
  ["HTML comment marker", "<!-- gate: status=held. Release=owner -->"],
  ["unclosed HTML comment protects the rest", "<!-- hidden. Marker\nstill. Hidden"],
  ["ordered list inside bullets", "- 1. First\n- 2. Second"],
  ["numbered heading", "## 1. Intro"],
  ["task box then a number", "- [ ] 1. Do it"],
  ["ellipsis before a lowercase word", "Hmm... maybe"],
  ["No. before a digit", "No. 5"],
  ["pp. before a digit", "See pp. 4 for it"],
  ["month abbreviation", "Due Jan. 5 at noon"],
  ["company abbreviations", "Acme Inc. Corp. merged"],
  ["above the size bound", "One. Two. ".repeat(HOUSE_GAP_MAX / 10 + 1)],
];

const REVIEW_CONVERTED = [
  ["prose after indented code", "Run this:\n\n    sed -e \"s/foo. bar/baz/\"\n\nThen. Done.", `Run this:\n\n    sed -e "s/foo. bar/baz/"\n\nThen.${GAP}Done.`],
  ["an indented list continuation is prose", "- item\n\n    continued. Para.", `- item\n\n    continued.${GAP}Para.`],
  ["an indented lazy continuation is prose", "Para. Next.\n    lazy. Continuation.", `Para.${GAP}Next.\n    lazy.${GAP}Continuation.`],
  ["prose after a <pre> block", "<pre>\na. B\n</pre>\nOk. Go.", `<pre>\na. B\n</pre>\nOk.${GAP}Go.`],
  ["a word that merely starts with pre", "<prelude> One. Two.", `<prelude> One.${GAP}Two.`],
  ["no at a sentence end before a capital", "I said no. Then we stop.", `I said no.${GAP}Then we stop.`],
  ["an ellipsis before a capital ends a sentence", "Wait... Then go.", `Wait...${GAP}Then go.`],
  ["two typed spaces after an abbreviation are the author's gap", "Ask Dr.  Smith.", `Ask Dr.${GAP}Smith.`],
];

test("review:  indented code, HTML code, comments, nested list markers and abbreviations are left alone", () => {
  for (const [name, text] of REVIEW_UNCHANGED) assert.equal(houseGap(text), text, name);
});

test("review:  prose around those constructs still gets the gap, and the result is idempotent", () => {
  for (const [name, text, expected] of REVIEW_CONVERTED) {
    assert.equal(houseGap(text), expected, name);
    assert.equal(houseGap(expected), expected, `${name}: idempotent`);
  }
});

test("review:  houseGap stays linear on the inputs that were quadratic (Worker cpu_ms is 300)", () => {
  const json = "[" + Array.from({ length: 2500 }, (_, i) => `{"k":"v1.2.${i}","u":"x.y/z"}`).join(",") + "]";
  const cases = [
    ["60,000 chars of a. after one space", "x y\n" + "a.".repeat(30000)],
    ["a fenced 65 KB minified JSON after two one-sentence lines", "Summary line.\nSecond line.\n```json\n" + json + "\n```"],
    ["the same JSON unfenced", "Summary line. Next\n" + json],
    ["a 250 KB single line of prose", "Word one. Word two. ".repeat(13000)],
    ["unclosed code openers", "a. B `x <pre> <!-- ".repeat(4000)],
  ];
  for (const [name, text] of cases) {
    const started = performance.now();
    houseGap(text);
    const elapsed = performance.now() - started;
    // Each took 195 ms to 4.7 s before;  now each is a few ms.  The bound leaves room for a slow CI runner.
    assert.ok(elapsed < 150, `${name}: ${elapsed.toFixed(1)} ms`);
  }
});

test("review:  commit messages, check summaries, milestone descriptions are gapped;  labels and titles are not", () => {
  for (const [action, field] of [
    ["wf_git_commit_create", "message"], ["wf_file_put", "message"], ["wf_files_put", "message"], ["wf_file_patch", "message"],
    ["wf_file_rename", "message"], ["wf_file_delete", "message"], ["wf_check_create", "summary"], ["wf_check_update", "summary"],
    ["wf_milestone_create", "description"], ["wf_milestone_update", "description"],
  ]) {
    const out = gapGitHubArgs(action, { [field]: "One. Two.", title: "A. B" });
    assert.equal(out[field], `One.${GAP}Two.`, `${action}.${field}`);
    assert.equal(out.title, "A. B", `${action}: title untouched`);
  }
  for (const action of ["wf_label_create", "wf_label_update", "wf_commit_status_create", "wf_deployment_create"]) {
    assert.equal(GITHUB_GAP_FIELDS[action], undefined, `${action} is deliberately excluded`);
  }
  const trailer = "Fix it. Now done.\n\nCo-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>\nSigned-off-by: J. Doe <j@x.test>";
  assert.equal(gapGitHubArgs("wf_git_commit_create", { message: trailer }).message, trailer.replace("it. Now", `it.${GAP}Now`));
});

test("review:  a body that fits the relay cap as typed but not once gapped is sent as typed, with a notice", async () => {
  const body = "Word. Next ".repeat(Math.floor(GITHUB_GAP_BYTE_CAP / 11));
  assert.ok(new TextEncoder().encode(body).length <= GITHUB_GAP_BYTE_CAP);
  const notices = [];
  await runWithGap(true, async () => {
    assert.equal(gapGitHubArgs("comment_pr", { body }, { byteCap: GITHUB_GAP_BYTE_CAP }).body, body);
    assert.equal(gapGitHubArgs("comment_pr", { body: "One. Two." }, { byteCap: GITHUB_GAP_BYTE_CAP }).body, `One.${GAP}Two.`);
  }, notices);
  assert.equal(notices.length, 1);
  assert.match(notices[0], /gr_comment_pr body was sent as typed/);
});

test("review:  Linear reads keep U+00A0, so a stored gap is visible, and markers are still removed", () => {
  assert.equal(linearClean(`One.${GAP}Two.`)[0], `One.${GAP}Two.`);
  assert.equal(linearClean(`BEGIN${NBSP}UNTRUSTED${NBSP}LINEAR`)[0], "[marker removed]");
  assert.equal(linearClean(`END${NBSP}UNTRUSTED${NBSP}ZULIP`)[0], "[marker removed]");
  assert.equal(linearClean("ab")[0], "a b", "the placeholder itself is never turned into U+00A0");
  assert.equal(linearClean("ﬁ​")[0], "fi", "the rest of the fold is unchanged");
  assert.equal(linearClean(`ab${NBSP}cd`, 3)[0], `ab${NBSP}`, "the limit still counts U+00A0 as one");
});
