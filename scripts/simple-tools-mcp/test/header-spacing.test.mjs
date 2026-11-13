import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { composeBody, MAX_CONTENT_LENGTH, normalizeHeader, tagPrefix, UsageError } from "../src/contract.js";
import { gapGitHubArgs } from "../src/outbound-gap.js";

const fixtures = JSON.parse(readFileSync(new URL("../../simple_tools/tests/header-spacing.json", import.meta.url), "utf8"));
if (!Array.isArray(fixtures) || fixtures.length === 0 || !fixtures.every((entry) =>
  entry !== null && typeof entry === "object" && !Array.isArray(entry) &&
  ["name", "input", "expected"].every((key) => typeof entry[key] === "string") &&
  (entry.seat === undefined || entry.seat === null || typeof entry.seat === "string") &&
  ["repos", "peers"].every((key) => entry[key] === undefined || (Array.isArray(entry[key]) && entry[key].every((v) => typeof v === "string")))
)) throw new Error("Invalid header-spacing.json fixture shape");

test("OPS-1086 golden headers survive JSON and UTF-8 without changing body gaps", () => {
  for (const entry of fixtures) {
    const options = entry.seat ? { seat: entry.seat } : {};
    const actual = normalizeHeader(entry.input, options);
    assert.equal(actual, entry.expected, entry.name);
    assert.equal(normalizeHeader(actual, options), actual, entry.name);
    const wire = Buffer.from(JSON.stringify({ content: actual }), "utf8");
    assert.equal(JSON.parse(wire.toString("utf8")).content, entry.expected, entry.name);
  }
});

test("hosted post serializes 3/2/1 hair spaces and distinct repo/recipient commas", () => {
  const actual = composeBody(tagPrefix("CODEX", "deadbeef"),
    "[CODEX: EE.1.S 📁AFC, OPS → CODEX EE] Body.\u00a0 Next.", { labels: ["AG"] });
  assert.equal(actual, fixtures[0].expected);
  assert.equal(/:(\u200a+)EE/u.exec(actual)[1], "\u200a".repeat(3));
  assert.equal(/S(\u200a+)📁/u.exec(actual)[1], "\u200a".repeat(2));
  assert.equal(/📁(\u200a+)AFC/u.exec(actual)[1], "\u200a");
  assert.equal(/AFC,(\u200a+)OPS/u.exec(actual)[1], "\u200a");
  assert.equal(/CODEX EE,(\u200a+)AG/u.exec(actual)[1], "\u200a".repeat(2));
  assert.ok(!actual.includes("deadbeef"));
  assert.ok(actual.includes(" → CODEX EE,"));
  assert.ok(actual.endsWith("Body.\u00a0 Next."));
  assert.equal(composeBody(tagPrefix("CODEX", "deadbeef"),
    "[CODEX: EE.1.S 📁AFC, OPS -> CODEX EE] Body.\u00a0 Next.", { labels: ["AG"] }), fixtures[0].expected);
});

test("the hosted content cap applies after header serialization", () => {
  const prefix = "[CODEX: EE.1.S 📁AFC] ";
  const input = prefix + "x".repeat(MAX_CONTENT_LENGTH - Array.from(prefix).length);
  assert.throws(() => composeBody(tagPrefix("CODEX", null), input), UsageError);
});

test("quiet own headers retain one no-wake marker and all addressed peers", () => {
  const body = "[CODEX: EE.1.S 📁AFC, OPS → CODEX EE] Body.\u00a0 Next.";
  for (const marker of [":eyes: ", "👀 "]) {
    assert.equal(composeBody(tagPrefix("CODEX", "deadbeef"), marker + body, { labels: ["AG"] }), marker + fixtures[0].expected);
  }
  assert.equal(composeBody(tagPrefix("CODEX", "deadbeef"), "[CODEX·oldtag->AG] hi", { labels: ["CURSOR"] }),
    "[CODEX·deadbeef → AG,\u200a\u200aCURSOR] hi");
});

test("quiet marker survives hosted composition with the same header bytes", () => {
  const input = ":eyes: " + fixtures[0].input;
  assert.equal(composeBody(tagPrefix("CODEX", "deadbeef"), input), ":eyes: " + fixtures[0].expected);
});

test("GitHub prose verbs preserve all exact header spaces through gap handling and JSON", () => {
  const body = fixtures[0].expected;
  for (const verb of ["open_pr", "edit_pr", "comment_pr", "reply_thread", "wf_pr_edit", "wf_issue_comment"]) {
    const wire = JSON.parse(JSON.stringify(gapGitHubArgs(verb, { body })));
    assert.equal(wire.body, body, verb);
  }
});
